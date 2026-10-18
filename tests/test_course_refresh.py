from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.cache import Entry
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.refresh import refresh_course
from bgmu_bot.source import SourceError
from bgmu_bot.ui import ADMIN_COURSE, BotUI
from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response
from test_personal import PATH, LABELS, update, click


@pytest.mark.asyncio
async def test_course_refresh_fetches_only_selected_faculty_and_course_and_preserves_others():
    requested = []
    failed = [False]
    def respond(request):
        params = dict(request.url.params)
        requested.append(params)
        data = api_response(request)
        if params['action'] == 'catalog' and params['type'] == 'group':
            data['items'].append({'id': 4, 'name': 'Л-102', 'faculty': 'Лечебный факультет', 'course': 1})
        if failed[0] and params.get('id') == '4':
            return httpx.Response(503)
        return httpx.Response(200, json=data)
    async with WebsiteClient(transport=httpx.MockTransport(respond)) as website:
        source = BGMUSource(website, cache_ttl=300)
        # Another course is already cached; its contents and expiry must survive.
        unrelated_key = source._cache_key(dict(action='schedule', type='group', id='2', year='26', semester='1'))
        old = Entry({'success': True, 'version': 'old'}, 123.0)
        source.cache.entries[unrelated_key] = old
        result = await source.refresh_course('Лечебный факультет', '1', period=('26', '1'))
        assert result['groups'] == 2
        assert {p['id'] for p in requested if p['action'] == 'schedule'} == {'1', '4'}
        assert all(p.get('type') != 'teacher' for p in requested)
        assert source.cache.entries[unrelated_key] is old
        assert old.expires_at == 123.0
        previous = source.cache.entries.copy()
        failed[0] = True
        with pytest.raises(SourceError):
            await source.refresh_course('Лечебный факультет', '1', period=('26', '1'))
        assert source.cache.entries == previous
        requested.clear()
        with pytest.raises(SourceError, match='период изменился'):
            await source.refresh_course('Лечебный факультет', '1', period=('25', '2'))
        assert not any(p['action'] == 'schedule' for p in requested)


@pytest.mark.asyncio
async def test_empty_course_does_not_change_cache_or_request_other_groups():
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website, cache_ttl=300)
        source.cache.replace({'saved': {}})
        previous = source.cache.entries.copy()
        with pytest.raises(SourceError, match='нет групп'):
            await source.refresh_course('Лечебный факультет', '6', period=('26', '1'))
        assert source.cache.entries == previous


@pytest.mark.asyncio
async def test_admin_faculty_course_confirmation_flow_and_ordinary_users_cannot_trigger_it(tmp_path):
    profiles = ProfileStore(tmp_path / 'prefs.sqlite3')
    profiles.save(123, PATH, LABELS)
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website, cache_ttl=300)
        source.refresh_course = AsyncMock(wraps=source.refresh_course)
        ui = BotUI(source, profiles)
        ctx = SimpleNamespace(user_data={})
        buttons = lambda who: [button.text for row in ui.home_keyboard(who).keyboard for button in row]
        assert ADMIN_COURSE not in buttons(update(username='other'))
        assert ADMIN_COURSE in buttons(update(username='Timur0_15'))
        await ui.text(update(text=ADMIN_COURSE, username='other'), ctx)
        source.refresh_course.assert_not_awaited()
        await ui.text(update(text=ADMIN_COURSE, username='Timur0_15'), ctx)
        assert ui.state(ctx)['course_refresh']['stage'] == 'faculty'
        await click(ui, ctx, 'r_pick', '0', username='Timur0_15')
        assert ui.state(ctx)['course_refresh']['stage'] == 'course'
        await click(ui, ctx, 'r_pick', '0', username='Timur0_15')
        assert ui.state(ctx)['course_refresh']['stage'] == 'confirm'
        source.refresh_course.assert_not_awaited()
        denied = await click(ui, ctx, 'r_go', username='other')
        assert denied.callback_query.answer.call_args.kwargs['show_alert']
        source.refresh_course.assert_not_awaited()
        result = await click(ui, ctx, 'r_go', username='tIMUR0_15')
        assert source.refresh_course.await_count == 1
        assert source.refresh_course.call_args.args == ('Лечебный факультет', '1')
        assert profiles.get(123).path == PATH
        assert profiles.metadata('last_course_refresh')['groups'] == 1
        assert 'Обновлено групп: 1' in result.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_targeted_refresh_updates_linked_bot_only_if_its_group_is_on_course(tmp_path):
    profiles = ProfileStore(tmp_path / 'prefs.sqlite3')
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website, cache_ttl=300)
        selected = SimpleNamespace(path=PATH, refresh_safely=AsyncMock(return_value=True))
        other = SimpleNamespace(path=PATH[:5]+('3', '*'), refresh_safely=AsyncMock(return_value=True))
        source.schedule_consumers = [selected, other]
        result = await refresh_course(source, profiles, 'Лечебный факультет', '1', period=('26', '1'))
        selected.refresh_safely.assert_awaited_once_with(force=True)
        other.refresh_safely.assert_not_awaited()
        assert result['linked_group_refreshed']
