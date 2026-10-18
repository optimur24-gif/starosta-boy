import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from bgmu_bot.backups import BackupStore
from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.refresh import morning_refresh, launch_morning_refresh, stop_background
from bgmu_bot.shared_schedule import SharedSchedule
from bgmu_bot.snapshot import DailySnapshot
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response, event

PATH=('group','26','1','Лечебный факультет','1','1')


def local(source,tmp_path):
    source.use_snapshot(tmp_path/'schedule.sqlite3')
    source.snapshot.min_interval=0


@pytest.mark.asyncio
async def test_all_entity_types_and_unsaved_groups_are_downloaded_once_and_read_offline(tmp_path):
    calls=[];unavailable=[False]
    def response(request):
        assert not unavailable[0], 'User requests must not access the website'
        calls.append(dict(request.url.params))
        return httpx.Response(200,json=api_response(request))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        result=await source.refresh(())
        assert result['groups']==3 and result['teachers']==0 and result['rooms']==0
        assert not any(call.get('type') in ('teacher','room') for call in calls)
        assert len(calls)==5 # period, group catalog, three schedules
        unavailable[0]=True;source.cache.replace({})
        for path in (PATH,('group','26','1','Лечебный факультет','2','2')):
            assert (await source.schedule(path,datetime(2026,10,21).date())).sections
        await source.search_groups('Л-201')
        for entry in source.cache.entries.values():entry.expires_at=0
        await source.schedule(PATH,datetime(2026,10,21).date())
        assert len(calls)==5
        with pytest.raises(SourceError):await source._api(action='schedule',type='group',id='99999',year='26',semester='1')
        restarted=BGMUSource(website);local(restarted,tmp_path)
        assert (await restarted.schedule(PATH,datetime(2026,10,21).date())).sections
        assert len(calls)==5


@pytest.mark.asyncio
async def test_missing_snapshot_and_missing_entity_never_fall_back_to_network(tmp_path):
    transport=httpx.MockTransport(lambda _:pytest.fail('Unexpected website request'))
    async with WebsiteClient(transport=transport) as website:
        source=BGMUSource(website);local(source,tmp_path)
        with pytest.raises(SourceError,match='локальная копия'):
            await source.current_group_start()
        with pytest.raises(SourceError):await source.schedule(PATH,datetime(2026,10,21).date())
        assert not source.snapshot.available


@pytest.mark.asyncio
async def test_failed_update_keeps_entire_previous_generation_and_backup(tmp_path):
    fail=[False];subject=['First']
    def response(request):
        query=dict(request.url.params)
        if fail[0] and query.get('action')=='schedule' and query.get('id')=='3':return httpx.Response(503)
        return httpx.Response(200,json=api_response(request,events=[event(subject[0])]))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        backup=BackupStore(tmp_path/'backups');source.snapshot.after_publish=backup.save_snapshot
        await source.refresh()
        previous=source.snapshot.path.read_bytes();archived=(backup.directory/'schedule.sqlite3').read_bytes()
        fail[0]=True;subject[0]='New partial'
        with pytest.raises(SourceError):await source.refresh()
        assert source.snapshot.path.read_bytes()==previous
        assert (backup.directory/'schedule.sqlite3').read_bytes()==archived
        assert [p.name for p in tmp_path.glob('.daily-snapshot-*')]==[]
        offline=await source._schedule_data(PATH)
        assert offline['events'][0]['subject']=='First' and offline['meta']['stale']


@pytest.mark.asyncio
async def test_atomic_publish_serves_old_copy_until_download_finishes(tmp_path):
    entered=asyncio.Event();release=asyncio.Event();updating=[False]
    async def response(request):
        q=dict(request.url.params)
        if updating[0] and q.get('action')=='schedule' and q.get('id')=='3':
            entered.set();await release.wait()
        return httpx.Response(200,json=api_response(request,events=[event('New' if updating[0] else 'Old')]))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        await source.refresh();updating[0]=True
        task=asyncio.create_task(source.refresh());await entered.wait()
        source.cache.replace({})
        assert (await source._schedule_data(PATH))['events'][0]['subject']=='Old'
        release.set();await task
        assert (await source._schedule_data(PATH))['events'][0]['subject']=='New'


@pytest.mark.asyncio
async def test_course_update_preserves_other_schedules_and_rolls_back_on_error(tmp_path):
    subject=['Old'];fail=[False]
    def response(request):
        q=dict(request.url.params)
        if fail[0] and q.get('action')=='schedule':return httpx.Response(503)
        return httpx.Response(200,json=api_response(request,events=[event(subject[0])]))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        await source.refresh();subject[0]='New'
        result=await source.refresh_course('Лечебный факультет','1',period=('26','1'))
        assert result['groups']==1
        assert (await source._schedule_data(PATH))['events'][0]['subject']=='New'
        assert (await source._schedule_data(('group','26','1','Лечебный факультет','2','2')))['events'][0]['subject']=='Old'
        before=source.snapshot.path.read_bytes();fail[0]=True
        with pytest.raises(SourceError):await source.refresh_course('Лечебный факультет','1',period=('26','1'))
        assert source.snapshot.path.read_bytes()==before


@pytest.mark.asyncio
async def test_more_than_memory_cache_limit_survives_restart(tmp_path):
    def response(request):
        q=dict(request.url.params);payload=api_response(request)
        if q['action']=='catalog' and q['type']=='group':
            payload['items']=[{'id':i,'name':f'Л-{i}','faculty':'Faculty','course':1} for i in range(1,151)]
        return httpx.Response(200,json=payload)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        assert (await source.refresh())['groups']==150
        source._request_api=AsyncMock(side_effect=AssertionError('Network forbidden'))
        for gid in range(1,151):
            await source._schedule_data(('group','26','1','Faculty','1',str(gid)))
        assert len(source.cache.entries)<=128
        await source._schedule_data(('group','26','1','Faculty','1','1'))
        source._request_api.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_update_once_per_day_even_after_failure_and_restart(tmp_path):
    calls=[]
    def response(request):
        calls.append(request);return httpx.Response(503)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        profiles=ProfileStore(tmp_path/'prefs.sqlite3');ctx=NS(job=NS(data=(source,profiles)))
        await morning_refresh(ctx);assert len(calls)==1
        await morning_refresh(ctx);assert len(calls)==1
        restarted=BGMUSource(website);local(restarted,tmp_path)
        await morning_refresh(NS(job=NS(data=(restarted,profiles))))
        assert len(calls)==1
        with pytest.raises(SourceError):await restarted._schedule_data(PATH)
        assert len(calls)==1
        assert profiles.metadata('last_refresh_error')


@pytest.mark.asyncio
async def test_background_download_can_be_cancelled_without_partial_publication(tmp_path):
    entered=asyncio.Event()
    async def response(request):
        entered.set();await asyncio.Event().wait()
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        profiles=ProfileStore(tmp_path/'prefs.sqlite3');ctx=NS(job=NS(data=(source,profiles)))
        await launch_morning_refresh(ctx)
        await entered.wait()
        await stop_background(source)
        assert not source.snapshot.available
        assert not list(tmp_path.glob('.daily-snapshot-*'))


@pytest.mark.asyncio
async def test_both_bots_share_p114_without_any_additional_http(tmp_path):
    calls=[]
    def response(request):
        q=dict(request.url.params);calls.append(q);payload=api_response(request)
        if q['action']=='catalog' and q['type']=='group':payload['items'].append({'id':114,'name':'П-114','faculty':'Педиатрический факультет','course':1})
        if q['action']=='schedule' and q['id']=='114':payload['entity']['name']='П-114'
        return httpx.Response(200,json=payload)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        await source.refresh();count=len(calls)
        feed=SharedSchedule(source);await feed.refresh();await feed.refresh(force=True)
        assert feed.ready and len(feed.rows)==1
        assert len(calls)==count


@pytest.mark.asyncio
async def test_daily_gate_checks_again_after_concurrent_update(tmp_path):
    entered=asyncio.Event();release=asyncio.Event();calls=[]
    async def response(request):
        calls.append(request)
        if len(calls)==1:
            entered.set();await release.wait()
        return httpx.Response(200,json=api_response(request))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        profiles=ProfileStore(tmp_path/'prefs.db');ctx=NS(job=NS(data=(source,profiles)))
        first=asyncio.create_task(morning_refresh(ctx));await entered.wait()
        second=asyncio.create_task(morning_refresh(ctx));release.set()
        await asyncio.gather(first,second)
        assert len(calls)==5
        await morning_refresh(ctx);assert len(calls)==5


@pytest.mark.asyncio
async def test_site_request_starts_are_rate_limited(tmp_path,monkeypatch):
    clock=[0.0];delays=[]
    monkeypatch.setattr('bgmu_bot.snapshot.time.monotonic',lambda:clock[0])
    async def sleep(delay):delays.append(delay);clock[0]+=delay
    monkeypatch.setattr('bgmu_bot.snapshot.asyncio.sleep',sleep)
    snapshot=DailySnapshot(tmp_path/'snapshot.sqlite3')
    for _ in range(3):await snapshot.wait_for_request()
    assert delays==pytest.approx([0.25,0.25])


@pytest.mark.asyncio
async def test_parallel_download_is_bounded_and_timeout_keeps_previous_copy(tmp_path):
    blocking=[False];active=[0];peak=[0]
    async def response(request):
        query=dict(request.url.params)
        if query.get('action')=='schedule':
            active[0]+=1;peak[0]=max(peak[0],active[0])
            try:
                if blocking[0]:await asyncio.Event().wait()
                else:await asyncio.sleep(0.005)
            finally:active[0]-=1
        data=api_response(request)
        if query.get('action')=='catalog':
            data['items'].append({**data['items'][0],'id':4,'name':'Л-104'})
        return httpx.Response(200,json=data)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);local(source,tmp_path)
        await source.refresh()
        assert peak[0]==4 and active[0]==0
        previous=source.snapshot.path.read_bytes()
        blocking[0]=True;source.refresh_timeout=0.03
        with pytest.raises(SourceError,match='15 минут'):
            await source.refresh()
        assert active[0]==0
        assert source.refresh_progress['state']=='failed'
        assert source.refresh_progress['active_downloads']==[]
        assert source.snapshot.path.read_bytes()==previous
        assert not list(tmp_path.glob('.daily-snapshot-*'))
        assert not source.refresh_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['teacher','room'])
async def test_removed_sections_reject_old_buttons_without_website_calls(tmp_path,kind):
    async with WebsiteClient(transport=httpx.MockTransport(lambda _:pytest.fail('Unexpected request'))) as website:
        source=BGMUSource(website);local(source,tmp_path)
        assert {item.key for item in (await source.menu(())).choices}=={'group'}
        with pytest.raises(SourceError):await source.menu((kind,'26','1'))
