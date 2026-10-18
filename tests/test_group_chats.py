from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import Forbidden, NetworkError

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.commands import schedule_request
from bgmu_bot.group_chats import GroupUI, time_answer
from bgmu_bot.notifications import broadcast, scheduled_notifications
from bgmu_bot.presentation import TIMEZONE, render_schedule
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.source import Schedule, Section, SourceError
from bgmu_bot.ui import create_application
from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response
from test_personal import PATH, LABELS


@pytest.mark.parametrize('text,kind,day', [
    ('сегодня', 'day', date(2026, 10, 21)), ('ЗАВТРА', 'day', date(2026, 10, 22)),
    ('расписание на завтра', 'day', date(2026, 10, 22)),
    ('расписание на среду', 'day', date(2026, 10, 21)),
    ('расписание на вторник', 'day', date(2026, 10, 27)),
    ('расписание на 25.10', 'day', date(2026, 10, 25)),
    ('расписание на 25.10.2027', 'day', date(2027, 10, 25)),
    ('расписание на 2026-12-31', 'day', date(2026, 12, 31)),
    ('расписание недели', 'week', date(2026, 10, 21)),
    ('расписание на неделю', 'week', date(2026, 10, 21)),
    ('след неделя', 'week', date(2026, 10, 28)),
    ('до конца', 'left', date(2026, 10, 21)),
    ('/left@TestBot', 'left', date(2026, 10, 21)),
    ('/schedule@TestBot 25.10', 'day', date(2026, 10, 25)),
    ('сейчас', 'current', date(2026, 10, 21)),
    ('следующая пара', 'next', date(2026, 10, 21)),
])
def test_commands(text, kind, day):
    request = schedule_request(text, date(2026, 10, 21))
    assert (request.kind, request.day) == (kind, day)


def test_unrelated_text_and_invalid_date():
    assert schedule_request('мут 20m', date(2026, 10, 21)) is None
    assert schedule_request('мне сегодня нравится погода', date(2026, 10, 21)) is None
    with pytest.raises(ValueError):
        schedule_request('расписание на 31.02', date(2026, 10, 21))
    with pytest.raises(ValueError, match='На какой день'):
        schedule_request('расписание на', date(2026, 10, 21))


def context(*, admin=True):
    bot = SimpleNamespace(id=12345, username='TestBot', send_message=AsyncMock(), get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator' if admin else 'member')))
    return SimpleNamespace(chat_data={}, user_data={}, bot=bot, args=[])


def update(text=None, data=None, chat_id=-1001):
    message = SimpleNamespace(text=text, reply_text=AsyncMock(), new_chat_members=[])
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id, type='supergroup'),
                           effective_user=SimpleNamespace(id=123, is_bot=False), effective_message=message,
                           my_chat_member=None,
                           callback_query=None if data is None else SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()))


async def click(ui, ctx, index):
    event = update(data=f"g:{ui.state(ctx)['nonce']}:pick:{index}")
    await ui.callback(event, ctx)
    return event


@pytest.fixture
def profiles(tmp_path):
    return ProfileStore(tmp_path / 'prefs.sqlite3')


@pytest.mark.asyncio
async def test_join_prefixes_binding_permissions_and_isolation(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        ui = GroupUI(BGMUSource(website, cache_ttl=300), profiles)
        ctx = context()
        event = update()
        event.effective_message.new_chat_members = [SimpleNamespace(id=12345)]
        await ui.joined(event, ctx)
        assert ui.state(ctx)['options'] == ('Л-', 'П-')
        await ui.joined(event, ctx)
        assert event.effective_message.reply_text.await_count == 2  # help + menu once
        nonce = ui.state(ctx)['nonce']
        ctx.bot.get_chat_member.return_value.status = 'member'
        denied = await click(ui, ctx, 0)
        assert denied.callback_query.answer.call_args.kwargs['show_alert']
        assert ui.state(ctx)['nonce'] == nonce
        ctx.bot.get_chat_member.return_value.status = 'administrator'
        await click(ui, ctx, 0)  # Л-
        buttons = ui.state(ctx)['options']
        assert [item.labels[5] for item in buttons] == ['Л-101', 'Л-201']
        await click(ui, ctx, 0)  # Л-101
        await click(ui, ctx, 1)  # Подгруппа А
        assert profiles.chat_group(-1001).path == PATH
        assert profiles.get(123) is None
        assert profiles.chat_group(-1002) is None
        assert profiles.subscribed_chats() == (-1001,)
        assert PATH in profiles.paths()
        reopened = ProfileStore(profiles.path)
        assert reopened.chat_group(-1001).labels == LABELS


@pytest.mark.asyncio
async def test_commands_answer_any_member_without_mutating_personal_group(profiles, monkeypatch):
    monkeypatch.setattr('bgmu_bot.group_chats.current_time', lambda: datetime(2026, 10, 21, 9, 30, tzinfo=TIMEZONE))
    profiles.save_chat(-1001, PATH, LABELS)
    profiles.save(123, PATH[:-1], LABELS[:-1])
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        ui = GroupUI(BGMUSource(website), profiles)
        ctx = context(admin=False)
        for text in ('сегодня', 'расписание недели', 'до конца', '/today@TestBot'):
            event = update(text)
            await ui.text(event, ctx)
            body = '\n'.join(call.args[0] for call in event.effective_message.reply_text.call_args_list)
            assert 'Биология' in body
            if text == 'до конца':
                assert '65 мин.' in body
        foreign = update('/today@OtherBot')
        await ui.text(foreign, ctx)
        foreign.effective_message.reply_text.assert_not_awaited()
        ignored = update('мут 20m')
        await ui.text(ignored, ctx)
        ignored.effective_message.reply_text.assert_not_awaited()
        assert profiles.get(123).path == PATH[:-1]
        assert profiles.chat_group(-1001).path == PATH


@pytest.mark.asyncio
async def test_time_queries_at_boundaries(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source = BGMUSource(website)
        at = lambda h, m: datetime(2026, 10, 21, h, m, tzinfo=TIMEZONE)
        assert '1 мин.' in await time_answer(source, PATH, 'left', at(10, 34))
        assert 'нет пары' in await time_answer(source, PATH, 'left', at(10, 35))
        assert '09:00–10:35 · 🧪 Практика · Биология' in await time_answer(source, PATH, 'next', at(8, 59))
        assert '09:00–10:35 · 🧪 Практика · Биология' in await time_answer(source, PATH, 'current', at(9, 0))


@pytest.mark.asyncio
async def test_notifications_dedupe_across_restart_and_chat_isolation(profiles):
    profiles.save_chat(-1001, PATH, LABELS)
    profiles.save_chat(-1002, PATH, LABELS)
    profiles.chat_notifications(-1002, False)
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source, bot = BGMUSource(website), context().bot
        await broadcast(source, profiles, bot, 'morning', date(2026, 10, 21))
        assert bot.send_message.await_count == 1
        assert bot.send_message.call_args.args[0] == -1001
        assert 'Биология' in bot.send_message.call_args.args[1]
        reopened = ProfileStore(profiles.path)
        await broadcast(source, reopened, bot, 'morning', date(2026, 10, 21))
        assert bot.send_message.await_count == 1
        await broadcast(source, reopened, bot, 'evening', date(2026, 10, 22))
        assert bot.send_message.await_count == 2
        assert '22.10.2026' in bot.send_message.call_args.args[1]
        reopened.migrate_chat(-1001, -1003)
        assert reopened.subscribed_chats() == (-1003,)
        assert reopened.delivery(-1003, 'morning', '2026-10-21')[1] == 1


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_block_other_chats_and_can_retry(profiles):
    for chat_id in (-1001, -1002):
        profiles.save_chat(chat_id, PATH, LABELS)
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        source, bot = BGMUSource(website), context().bot
        async def send(chat_id, *args, **kwargs):
            if chat_id == -1001:
                raise Forbidden('removed')
        bot.send_message.side_effect = send
        await broadcast(source, profiles, bot, 'morning', date(2026, 10, 21))
        assert profiles.subscribed_chats() == (-1002,)
        assert profiles.delivery(-1002, 'morning', '2026-10-21')[1] == 1


@pytest.mark.asyncio
async def test_partial_broadcast_resumes_without_repeating_sent_parts(profiles):
    profiles.save_chat(-1001, PATH, LABELS)
    schedule = Schedule('Л-101', date(2026, 10, 21), tuple(Section(f'Пара {i}', ('Предмет ' * 200,)) for i in range(5)))
    source = SimpleNamespace(resolve_group=AsyncMock(return_value=SimpleNamespace(path=PATH, labels=LABELS)), schedule=AsyncMock(return_value=schedule))
    bot = context().bot
    parts = render_schedule(schedule)
    assert len(parts) > 1
    bot.send_message.side_effect = [None, NetworkError('timeout')]
    await broadcast(source, profiles, bot, 'morning', date(2026, 10, 21))
    assert profiles.delivery(-1001, 'morning', '2026-10-21') == (1, 0)
    bot.send_message.reset_mock(side_effect=True)
    await broadcast(source, ProfileStore(profiles.path), bot, 'morning', date(2026, 10, 21))
    assert bot.send_message.await_count == len(parts)-1
    assert parts[1] in bot.send_message.call_args_list[0].args[1]
    assert profiles.delivery(-1001, 'morning', '2026-10-21')[1] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('hour,minute,kind,day', [(7, 30, 'morning', 21), (20, 0, 'evening', 22), (7, 50, 'morning', 21), (12, 0, None, None)])
async def test_scheduler_ufa_time_and_target_dates(profiles, monkeypatch, hour, minute, kind, day):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, hour, minute, tzinfo=TIMEZONE)
    monkeypatch.setattr('bgmu_bot.notifications.datetime', Clock)
    mocked = AsyncMock()
    monkeypatch.setattr('bgmu_bot.notifications.broadcast', mocked)
    ctx = SimpleNamespace(job=SimpleNamespace(data=('source', profiles)), bot='bot')
    await scheduled_notifications(ctx)
    if kind:
        assert mocked.call_args.args == ('source', profiles, 'bot', kind, date(2026, 10, day))
    else:
        mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_admin_commands_and_opt_out_permissions(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        ui, ctx = GroupUI(BGMUSource(website), profiles), context(admin=False)
        await ui.notifications(update('/notifications_off'), ctx)
        assert not profiles.subscribed_chats()
        ctx.bot.get_chat_member.return_value.status = 'administrator'
        ctx.args = ['Л-101']
        await ui.configure(update('/group Л-101'), ctx)
        await click(ui, ctx, 0)
        await click(ui, ctx, 1)
        assert profiles.subscribed_chats() == (-1001,)
        await ui.notifications(update('/notifications_off@TestBot'), ctx)
        assert profiles.subscribed_chats() == ()
        await ui.notifications(update('/notifications_on@TestBot'), ctx)
        assert profiles.subscribed_chats() == (-1001,)


@pytest.mark.asyncio
async def test_membership_update_without_message_and_leaving_chat(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        ui, ctx = GroupUI(BGMUSource(website), profiles), context()
        event = update()
        event.effective_message = None
        event.my_chat_member = SimpleNamespace(chat=event.effective_chat, old_chat_member=SimpleNamespace(status='left'), new_chat_member=SimpleNamespace(status='member'))
        await ui.joined(event, ctx)
        assert ctx.bot.send_message.await_count == 2
        assert ui.state(ctx)['options'] == ('Л-', 'П-')
        profiles.save_chat(-1001, PATH, LABELS)
        event.my_chat_member.new_chat_member.status = 'left'
        await ui.joined(event, ctx)
        assert profiles.subscribed_chats() == ()
        assert not ctx.chat_data.get('join_prompt_sent')


@pytest.mark.asyncio
async def test_group_configuration_falls_back_to_admin_list_when_bot_is_not_admin(profiles):
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=api_response(r)))) as website:
        ui, ctx = GroupUI(BGMUSource(website), profiles), context()
        from telegram.error import BadRequest
        ctx.bot.get_chat_member.side_effect = BadRequest('not available')
        ctx.bot.get_chat_administrators = AsyncMock(return_value=[SimpleNamespace(user=SimpleNamespace(id=123))])
        assert await ui.allowed(update(), ctx)
        ctx.bot.get_chat_administrators.return_value = []
        assert not await ui.allowed(update(), ctx)
