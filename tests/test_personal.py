import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.cache import ResponseCache
from bgmu_bot.presentation import render_week
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.refresh import morning_refresh, refresh_saved_groups
from bgmu_bot.source import GroupUnavailable, SourceError
from bgmu_bot.ui import ADMIN, BIND, TODAY, TOMORROW, WEEK, BotUI, create_application
from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response

PATH = ('group', '26', '1', 'Лечебный факультет', '1', '1', 'А')
LABELS = ('Группы', '2026/2027', 'Осенний семестр', 'Лечебный факультет', '1 курс', 'Л-101', 'Подгруппа А')


def update(*, text=None, data=None, uid=123, username=None):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=uid, username=username),
        effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        callback_query=None if data is None else SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()),
    )


async def click(ui, context, action, value='', **kwargs):
    result = update(data=f"{ui.state(context)['nonce']}:{action}:{value}", **kwargs)
    await ui.callback(result, context)
    return result


@pytest.fixture
def profiles(tmp_path):
    return ProfileStore(tmp_path / 'private' / 'preferences.sqlite3')


def test_profiles_survive_restart_and_deletion_is_per_user(profiles):
    profiles.save(123, PATH, LABELS)
    profiles.save(456, PATH[:-1], LABELS[:-1])
    reopened = ProfileStore(profiles.path)
    assert reopened.get(123).path == PATH
    assert reopened.get(456).path == PATH[:-1]
    reopened.delete(123)
    assert reopened.get(123) is None
    assert reopened.get(456) is not None
    assert reopened.path.stat().st_mode & 0o777 == 0o600
    with reopened.connection() as db:
        columns = [row[1] for row in db.execute('PRAGMA table_info(profiles)')]
    assert columns == ['user_id', 'path', 'labels', 'updated_at']


@pytest.mark.asyncio
async def test_cache_coalesces_requests_expires_and_marks_fallback():
    now = [0]
    cache = ResponseCache(ttl=5, clock=lambda: now[0])
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return {'value': calls}

    results = await asyncio.gather(*(cache.get('x', load) for _ in range(20)))
    assert calls == 1 and all(value == {'value': 1} for value in results)
    now[0] = 6
    assert (await cache.get('x', load))['value'] == 2
    now[0] = 12
    broken = AsyncMock(side_effect=SourceError('offline'))
    stale = await cache.get('x', broken)
    assert stale['meta']['stale'] and stale['value'] == 2
    assert 'предыдущая копия' in stale['meta']['warning']
    assert 'meta' not in cache.entries['x'].value
    with pytest.raises(SourceError):
        await cache.get('uncached', broken)


@pytest.mark.asyncio
async def test_refresh_generation_cannot_be_overwritten_by_inflight_request():
    cache = ResponseCache(ttl=300, max_entries=2)
    started, release = asyncio.Event(), asyncio.Event()

    async def old_request():
        started.set()
        await release.wait()
        return {'version': 'old'}

    task = asyncio.create_task(cache.get('x', old_request))
    await started.wait()
    cache.replace({'x': {'version': 'new'}, 'y': {}, 'z': {}})
    release.set()
    await task
    assert list(cache.entries) == ['y', 'z']
    assert len(cache.entries) == 2


@pytest.mark.asyncio
async def test_real_adapter_personal_search_week_and_atomic_refresh(profiles):
    requests = []
    version = [1]
    broken = [False]

    def respond(request):
        params = dict(request.url.params)
        requests.append(params)
        if broken[0] and params.get('type') == 'group':
            return httpx.Response(503)
        data = api_response(request)
        data['version'] = version[0]
        return httpx.Response(200, json=data)

    async with WebsiteClient(transport=httpx.MockTransport(respond)) as website:
        source = BGMUSource(website, cache_ttl=300)
        match, = await source.search_groups('л 101')
        assert match.path == PATH[:-1]
        resolved = await source.resolve_group(PATH)
        assert resolved.labels == LABELS
        week = await source.week(PATH, date(2026, 10, 21))
        assert [day.day for day in week] == [date(2026, 10, 19) + timedelta(days=i) for i in range(7)]
        assert sum(item['action'] == 'schedule' for item in requests) == 1
        messages = render_week(week)
        assert 'Биология' in '\n'.join(messages)
        assert '19.10.2026' in messages[0] and '25.10.2026' in '\n'.join(messages)
        profiles.save(123, PATH, LABELS)
        source.cache.entries['obsolete'] = None
        version[0] = 2
        result = await refresh_saved_groups(source, profiles)
        assert result['groups'] == 1 and result['catalogs'] == 1
        assert 'obsolete' not in source.cache.entries
        assert all(entry.value['version'] == 2 for entry in source.cache.entries.values())
        assert profiles.get(123).path == PATH
        assert profiles.metadata('last_refresh') == result
        previous = source.cache.entries.copy()
        broken[0] = True
        with pytest.raises(SourceError):
            await refresh_saved_groups(source, profiles)
        assert source.cache.entries == previous
        assert profiles.metadata('last_refresh') == result
        assert profiles.metadata('last_refresh_error')['type'] == 'SourceError'
        assert profiles.get(123).path == PATH


@pytest.mark.asyncio
async def test_current_period_resolution_updates_labels_and_rejects_missing_group():
    def respond(request):
        data = api_response(request)
        if request.url.params['action'] == 'period':
            data['period']['semester'] = 2
        return httpx.Response(200, json=data)

    async with WebsiteClient(transport=httpx.MockTransport(respond)) as website:
        source = BGMUSource(website)
        match = await source.resolve_group(PATH)
        assert match.path[2] == '2' and match.labels[2] == 'Весенний семестр'
        with pytest.raises(GroupUnavailable):
            await source.resolve_group(PATH[:5] + ('99999', 'А'))


@pytest.mark.asyncio
async def test_personal_binding_from_search_saves_subgroup_and_skips_future_menus(profiles, monkeypatch):
    monkeypatch.setattr('bgmu_bot.ui.today', lambda: date(2026, 10, 21))
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website, cache_ttl=300)
        source.week = AsyncMock(wraps=source.week)
        source.schedule = AsyncMock(wraps=source.schedule)
        ui = BotUI(source, profiles)
        context = SimpleNamespace(user_data={})
        await ui.start(update(), context)
        await ui.text(update(text='л 101'), context)
        assert len(ui.state(context)['choices']) == 1
        await click(ui, context, 'pick', '0')
        await click(ui, context, 'pick', '1')  # subgroup А
        assert profiles.get(123).path == PATH
        assert ui.state(context)['screen'] == 'home'
        # A new user context simulates a restart, without walking the menus.
        context = SimpleNamespace(user_data={})
        await ui.start(update(), context)
        await ui.text(update(text=TODAY), context)
        assert source.schedule.call_args.args == (PATH, date(2026, 10, 21))
        await ui.text(update(text=TOMORROW), context)
        assert source.schedule.call_args.args == (PATH, date(2026, 10, 22))
        await ui.text(update(text=WEEK), context)
        assert source.week.await_count == 1
        await ui.text(update(text=BIND), context)
        await click(ui, context, 'back')  # cancel replacement
        assert profiles.get(123).path == PATH
        await ui.show_group(update(uid=456), SimpleNamespace(user_data={}))
        assert profiles.get(456) is None
        await click(ui, context, 'forget', uid=456)
        assert profiles.get(123).path == PATH


@pytest.mark.asyncio
async def test_only_actual_admin_username_can_refresh_and_see_button(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website, cache_ttl=300)
        source.refresh = AsyncMock(wraps=source.refresh)
        ui = BotUI(source, profiles)
        normal = update(username='someone')
        admin = update(username='tIMUR0_15')
        buttons = lambda who: [button.text for row in ui.home_keyboard(who).keyboard for button in row]
        assert ADMIN not in buttons(normal)
        assert ADMIN in buttons(admin)
        context = SimpleNamespace(user_data={})
        unauthorized = update(text=ADMIN, username='someone')
        await ui.text(unauthorized, context)
        source.refresh.assert_not_awaited()
        assert 'администратору' in unauthorized.effective_message.reply_text.call_args.args[0]
        await ui.admin_refresh(admin, context)
        assert source.refresh.await_count == 1
        assert '✅' in admin.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_daily_job_uses_confirmed_ufa_time_and_same_safe_refresh(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website)
        application = create_application('123:offline-test', source, profiles=profiles)
        job, = application.job_queue.get_jobs_by_name('bgmu-morning-refresh')
        assert job.name == 'bgmu-morning-refresh'
        assert str(job.job.trigger.timezone) == 'Asia/Yekaterinburg'
        fields = {field.name: str(field) for field in job.job.trigger.fields}
        assert fields['hour'] == '7' and fields['minute'] == '0'
        assert source.cache.ttl == 86400
        assert source.snapshot is not None
        source.snapshot.min_interval = 0
        profiles.save(123, PATH, LABELS)
        await morning_refresh(SimpleNamespace(job=job))
        assert profiles.metadata('last_refresh')['groups'] == 3
        assert profiles.metadata('last_refresh')['teachers'] == 0
        assert profiles.metadata('last_refresh')['rooms'] == 0
        assert profiles.get(123).path == PATH


@pytest.mark.asyncio
async def test_scheduled_failure_retains_preferences_and_is_recorded(profiles):
    profiles.save(123, PATH, LABELS)
    source = SimpleNamespace(refresh=AsyncMock(side_effect=SourceError('offline')))
    await morning_refresh(SimpleNamespace(job=SimpleNamespace(data=(source, profiles))))
    assert profiles.get(123).path == PATH
    assert profiles.metadata('last_refresh_error')['type'] == 'SourceError'
