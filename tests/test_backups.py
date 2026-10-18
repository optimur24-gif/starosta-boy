import gzip
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from bgmu_bot.backups import BackupStore
from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.cache import ResponseCache
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient
from test_admin_tools import panel, update
from test_bgmu import api_response

PATH=('group','26','1','Faculty','1','1')
LABELS=('Groups','2026','Semester','Faculty','1 курс','Л-101')


def read_settings(store):
    return sqlite3.connect(store.backups.directory/'settings.sqlite3')


def read_schedule(store):
    with gzip.open(store.directory/'schedule.json.gz','rt',encoding='utf-8') as saved:
        return json.load(saved)


def test_all_settings_copied_and_previous_file_replaced(tmp_path):
    profiles=ProfileStore(tmp_path/'preferences.sqlite3')
    profiles.save(1,PATH,LABELS)
    profiles.save(2,PATH,LABELS)
    profiles.save_chat(-10,PATH,LABELS)
    profiles.contact_subscription(1,False)
    profiles.set_access_blocked(2,True,99)
    profiles.set_metadata('owner_alerts_users',False)
    with read_settings(profiles) as db:
        assert db.execute('PRAGMA quick_check').fetchone()==('ok',)
        assert db.execute('SELECT COUNT(*) FROM profiles').fetchone()[0]==2
        assert db.execute('SELECT path FROM chats WHERE chat_id=-10').fetchone()
        assert db.execute('SELECT target_id FROM access_blocks').fetchone()==(2,)
        assert json.loads(db.execute("SELECT value FROM metadata WHERE key='owner_alerts_users'").fetchone()[0]) is False
    profiles.delete(1)
    with read_settings(profiles) as db:
        assert db.execute('SELECT user_id FROM profiles').fetchall()==[(2,)]
    files=list(profiles.backups.directory.iterdir())
    assert len(files)==1 and files[0].name=='settings.sqlite3'
    assert files[0].stat().st_mode & 0o777==0o600
    assert profiles.backups.directory.stat().st_mode & 0o777==0o700


def test_settings_backup_failure_keeps_last_good_copy(tmp_path,monkeypatch):
    profiles=ProfileStore(tmp_path/'prefs.db')
    profiles.save(1,PATH,LABELS)
    saved=(profiles.backups.directory/'settings.sqlite3').read_bytes()
    def fail(*args):raise OSError('disk full')
    monkeypatch.setattr('bgmu_bot.backups.os.replace',fail)
    profiles.delete(1)
    assert profiles.get(1) is None
    assert (profiles.backups.directory/'settings.sqlite3').read_bytes()==saved
    assert profiles.backups.last_error=='OSError'
    assert len(list(profiles.backups.directory.iterdir()))==1


@pytest.mark.asyncio
async def test_schedule_updates_cache_hit_and_failed_refresh_preserve_backup(tmp_path):
    backup=BackupStore(tmp_path/'backups')
    cache=ResponseCache(ttl=5,clock=lambda:0)
    cache.on_change=backup.save_schedule
    key=(('action','schedule'),('id','1'))
    first=AsyncMock(return_value={'events':[{'subject':'First'}]})
    await cache.get(key,first)
    initial=(backup.directory/'schedule.json.gz').read_bytes()
    await cache.get(key,first)
    assert first.await_count==1
    assert (backup.directory/'schedule.json.gz').read_bytes()==initial
    cache.entries[key].expires_at=-1
    result=await cache.get(key,AsyncMock(side_effect=SourceError('offline')))
    assert result['meta']['stale']
    assert (backup.directory/'schedule.json.gz').read_bytes()==initial
    await cache.get(key,AsyncMock(return_value={'events':[{'subject':'Second'}]}))
    assert read_schedule(backup)['entries'][0]['payload']['events'][0]['subject']=='Second'
    assert len(list(backup.directory.iterdir()))==1


def test_replace_and_targeted_merge_write_single_full_snapshot(tmp_path,monkeypatch):
    backup=BackupStore(tmp_path/'backups');cache=ResponseCache(ttl=300)
    cache.on_change=backup.save_schedule
    one=(('action','schedule'),('id','1'));two=(('action','schedule'),('id','2'))
    cache.replace({one:{'events':[]}})
    cache.merge({two:{'events':[]}})
    assert len(read_schedule(backup)['entries'])==2
    previous=(backup.directory/'schedule.json.gz').read_bytes()
    def fail(*args,**kwargs):raise OSError('full')
    monkeypatch.setattr('bgmu_bot.backups.os.replace',fail)
    cache.replace({one:{'events':[{'subject':'new'}]}})
    assert (backup.directory/'schedule.json.gz').read_bytes()==previous
    assert len(list(backup.directory.iterdir()))==1


@pytest.mark.asyncio
async def test_invalid_source_payload_does_not_replace_valid_backup(tmp_path):
    backup=BackupStore(tmp_path/'backups');broken=[False]
    def response(request):
        if broken[0] and request.url.params.get('action')=='schedule':
            return httpx.Response(200,json={'success':True,'events':'invalid'})
        return httpx.Response(200,json=api_response(request))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website,cache_ttl=300)
        source.cache.on_change=backup.save_schedule
        await source._schedule_data(PATH)
        previous=(backup.directory/'schedule.json.gz').read_bytes()
        for entry in source.cache.entries.values():entry.expires_at=0
        broken[0]=True
        stale=await source._schedule_data(PATH)
        assert stale['meta']['stale']
        assert (backup.directory/'schedule.json.gz').read_bytes()==previous
        with pytest.raises(SourceError):await source.refresh([PATH])
        assert (backup.directory/'schedule.json.gz').read_bytes()==previous


@pytest.mark.asyncio
async def test_backup_download_only_owner_private_and_fixed_filenames(tmp_path):
    profiles=ProfileStore(tmp_path/'prefs.db');profiles.save(1,PATH,LABELS)
    p=panel(profiles);ctx=NS(user_data={},bot=NS(send_document=AsyncMock()))
    await p.root(update(),ctx)
    async def click(action,value='',username='Owner',kind='private'):
        nonce=ctx.user_data['admin_tools']['nonce']
        await p.callback(update(username=username,kind=kind,data=f'a:{nonce}:{action}:{value}'),ctx)
    await click('backups')
    await click('backup_get','settings',username='Intruder')
    await click('backup_get','settings',kind='group')
    await click('backup_get','../../prefs.db')
    ctx.bot.send_document.assert_not_called()
    await click('backup_get','settings')
    assert ctx.bot.send_document.await_count==1
    assert ctx.bot.send_document.call_args.kwargs['chat_id']==1
    assert ctx.bot.send_document.call_args.kwargs['filename']=='settings.sqlite3'


def test_settings_backup_does_not_replay_broadcasts_and_supports_wal(tmp_path):
    profiles=ProfileStore(tmp_path/'prefs.db')
    profiles.touch_contact(1,'Owner')
    profiles.queue_broadcast(1,'Do not replay','users',[1])
    with profiles.connection() as db: db.execute('PRAGMA journal_mode=WAL')
    profiles.save(1,PATH,LABELS)
    assert profiles.active_broadcast() is not None
    with read_settings(profiles) as db:
        assert db.execute('SELECT COUNT(*) FROM broadcasts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM broadcast_recipients').fetchone()[0]==0
        assert db.execute('SELECT user_id FROM profiles').fetchone()==(1,)
        assert db.execute('PRAGMA journal_mode').fetchone()[0]=='delete'
    assert [p.name for p in profiles.backups.directory.iterdir()]==['settings.sqlite3']
