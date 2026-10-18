import errno
import sqlite3
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.errors import error_description
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.refresh import refresh_saved_groups
from bgmu_bot.snapshot import DailySnapshot
from bgmu_bot.source import SourceError
from bgmu_bot.ui import BotUI
from test_personal import update


@pytest.mark.asyncio
async def test_manual_error_includes_safe_reason_and_failed_request(tmp_path):
    profiles=ProfileStore(tmp_path/'prefs.db')
    source=NS(refresh=AsyncMock(side_effect=SourceError('Сайт вернул HTTP 403.')),
              current_group_start=AsyncMock(),refresh_progress={'request':{'action':'schedule','type':'group','id':'114'}})
    ui=BotUI(source,profiles,'Owner');u=update(username='Owner')
    await ui._admin_refresh_now(u,NS(user_data={}))
    text=u.effective_message.reply_text.call_args.args[0]
    assert 'Причина:' in text and 'HTTP 403' in text and 'ID 114' in text
    assert 'HTTP 403' in profiles.metadata('last_refresh_error')['message']


def test_diagnostics_distinguish_disk_and_database_and_hide_arbitrary_exception_values():
    assert 'Недостаточно места' in error_description(OSError(errno.ENOSPC,'private path'))
    assert 'прав' in error_description(OSError(errno.EACCES,'private path'))
    assert 'базы' in error_description(sqlite3.OperationalError('database is locked'))
    assert error_description(RuntimeError('SECRET-TOKEN'))=='RuntimeError'
    assert 'Сохранение' in error_description(SourceError('Ошибка'),NS(refresh_progress={'phase':'save'}))


def test_previous_snapshot_remains_readable_when_meta_is_null_after_failure(tmp_path):
    snapshot=DailySnapshot(tmp_path/'snapshot.sqlite3')
    params={'action':'schedule','type':'group','id':'1','year':'26','semester':'1'}
    with snapshot.stage() as writer:
        writer.put(params,{'success':True,'entity':{'name':'Group'},'events':[],'meta':None})
        writer.summary({'full_refresh_at':'2026-10-18T02:00:00+00:00'})
    snapshot.last_error=True
    loaded=snapshot.get(params)
    assert loaded['meta']['stale']
    assert 'предыдущая' in loaded['meta']['warning']


@pytest.mark.asyncio
async def test_update_status_is_private_owner_only_and_uses_local_metadata(tmp_path):
    from bgmu_bot.admin_panel import AdminPanel
    from test_admin_tools import update as panel_update
    profiles=ProfileStore(tmp_path/'prefs.db')
    profiles.set_metadata('last_refresh_error',{'type':'SourceError','message':'Сайт вернул HTTP 403.'})
    source=NS(snapshot=None,refresh_progress={'state':'failed','completed':12,'request':{'action':'schedule','id':'114'}})
    p=AdminPanel(profiles,NS(source=source,is_admin=lambda u:u.effective_user.username=='Owner'))
    ctx=NS(user_data={});await p.root(panel_update(),ctx)
    nonce=ctx.user_data['admin_tools']['nonce']
    for u in (panel_update(username='Student',data=f'a:{nonce}:refresh_status:'),
              panel_update(kind='group',data=f'a:{nonce}:refresh_status:')):
        await p.callback(u,ctx)
        u.callback_query.edit_message_text.assert_not_called()
    allowed=panel_update(data=f'a:{nonce}:refresh_status:')
    await p.callback(allowed,ctx)
    text=allowed.callback_query.edit_message_text.call_args.args[0]
    assert 'HTTP 403' in text and '12' in text and 'id=114' in text


@pytest.mark.asyncio
async def test_progress_tracks_inflight_entity_counts_and_publish_backup(tmp_path):
    import asyncio
    import httpx
    from bgmu_bot.website import WebsiteClient
    from test_bgmu import api_response
    entered=asyncio.Event();release=asyncio.Event()
    async def response(request):
        query=dict(request.url.params)
        if query.get('action')=='schedule' and query.get('id')=='1':
            entered.set();await release.wait()
        return httpx.Response(200,json=api_response(request))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);source.use_snapshot(tmp_path/'schedule.sqlite3')
        source.snapshot.min_interval=0
        source.snapshot.after_publish=lambda path:False
        task=asyncio.create_task(source.refresh());await entered.wait()
        try:
            progress=source.refresh_progress
            assert progress['state']=='running' and progress['phase'] in ('download','validate')
            assert progress['active_downloads']
            assert 'расписание:' in progress['detail']
            assert any('bgmu-time.ru' in row['detail'] for row in progress['history'])
            assert progress['section']=='Группы' and progress['section_total']==3
            assert 0 <= progress['completed'] <= 2
        finally:
            release.set();await task
        progress=source.refresh_progress
        assert progress['state']=='completed' and progress['completed']==3
        assert progress['backup_state']=='failed' and source.snapshot.available
        assert len(progress['history'])<=8
        details=' '.join(row['detail'] for row in progress['history'])
        assert 'целостность' in details and 'Не удалось создать резервную копию' in details
        # A new attempt must not inherit the failed backup status.
        source.snapshot.after_publish=lambda path:True
        await source.refresh_course('Лечебный факультет','1',period=('26','1'))
        assert source.refresh_progress['operation']=='course'
        assert source.refresh_progress['completed']==source.refresh_progress['section_total']
        assert source.refresh_progress['backup_state']=='completed'
        source._request_api=AsyncMock(side_effect=SourceError('Сайт вернул HTTP 503.'))
        with pytest.raises(SourceError):
            await source.refresh_course('Лечебный факультет','1',period=('26','1'))
        assert source.refresh_progress['state']=='failed'
        assert source.refresh_progress['finished_at']


@pytest.mark.asyncio
async def test_detailed_status_shows_local_stages_and_ufa_time(tmp_path):
    from bgmu_bot.admin_panel import AdminPanel
    from test_admin_tools import update as panel_update
    profiles=ProfileStore(tmp_path/'prefs.db')
    progress={'state':'running','completed':17,'section':'Группы','section_completed':17,
              'section_total':652,'detail':'Обращаюсь к bgmu-time.ru, скачиваю расписание: П-114',
              'started_at':'2026-10-18T02:00:00+00:00',
              'history':[{'at':'2026-10-18T02:01:00+00:00','detail':'Загружено и записано: П-113'}]}
    source=NS(snapshot=None,refresh_progress=progress)
    panel=AdminPanel(profiles,NS(source=source,is_admin=lambda _:True))
    u=panel_update();await panel.refresh_status(u,NS(user_data={}))
    text=u.effective_message.reply_text.call_args.args[0]
    assert '17/652' in text and 'П-114' in text and '07:01:00' in text
    assert 'Прошло:' in text and 'П-113' in text
