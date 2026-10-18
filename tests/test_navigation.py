from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bgmu_bot.source import Choice, Menu, Schedule, Section, SourceError
from bgmu_bot.ui import BotUI


class FakeSource:
    """Only an isolated test double, never used to serve real schedules."""

    def __init__(self):
        self.requested = []
        self.fail = False

    async def menu(self, path):
        if not path:
            return Menu("Выберите факультет", (Choice("faculty", "Лечебный"),))
        if len(path) == 1:
            return Menu("Выберите курс", (Choice("course", "1 курс"),))
        if len(path) == 2:
            return Menu("Выберите группу", (Choice("group", "101"),))
        return None

    async def schedule(self, path, day):
        self.requested.append((path, day))
        if self.fail:
            raise SourceError("upstream unavailable")
        return Schedule("Группа 101", day, (Section("Лекции", ("09:00 — Анатомия",)),))


def make_update(data=None, text=None):
    message = SimpleNamespace(reply_text=AsyncMock(), text=text)
    query = None if data is None else SimpleNamespace(
        data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(callback_query=query, effective_message=message)


async def select(ui, context, action, value=""):
    update = make_update(f"{ui.state(context)['nonce']}:{action}:{value}")
    await ui.callback(update, context)
    return update


async def navigate_to_date(ui, context):
    await ui.start(make_update(), context)
    for _ in range(3):
        await select(ui, context, "pick", "0")


@pytest.mark.asyncio
async def test_complete_navigation_fetches_each_schedule_again():
    source = FakeSource()
    ui = BotUI(source)
    context = SimpleNamespace(user_data={})
    await navigate_to_date(ui, context)
    assert ui.state(context)["ready"]
    assert not source.requested
    for _ in range(2):
        update = await select(ui, context, "day", "2026-10-21")
        assert "Анатомия" in update.callback_query.edit_message_text.call_args.args[0]
    assert source.requested == [
        (("faculty", "course", "group"), date(2026, 10, 21)),
        (("faculty", "course", "group"), date(2026, 10, 21)),
    ]
    assert set(ui.state(context)) == {"path", "labels", "choices", "ready", "nonce", "query"}


@pytest.mark.asyncio
async def test_other_users_and_old_buttons_do_not_change_selection():
    ui = BotUI(FakeSource())
    first = SimpleNamespace(user_data={})
    other = SimpleNamespace(user_data={})
    await ui.start(make_update(), first)
    stale = make_update(f"{ui.state(first)['nonce']}:pick:0")
    await select(ui, first, "pick", "0")
    await ui.callback(stale, first)
    assert stale.callback_query.answer.call_args.kwargs["show_alert"]
    assert ui.state(first)["path"] == ("faculty",)
    assert ui.state(other)["path"] == ()


@pytest.mark.asyncio
async def test_failed_upstream_can_be_retried_and_back_keeps_parent():
    source = FakeSource()
    ui = BotUI(source)
    context = SimpleNamespace(user_data={})
    await navigate_to_date(ui, context)
    source.fail = True
    failed = await select(ui, context, "day", "2026-10-21")
    assert "Не удалось" in failed.callback_query.edit_message_text.call_args.args[0]
    source.fail = False
    successful = await select(ui, context, "day", "2026-10-21")
    assert "Анатомия" in successful.callback_query.edit_message_text.call_args.args[0]
    await select(ui, context, "back")
    assert ui.state(context)["path"] == ("faculty", "course")
    assert not ui.state(context)["ready"]


@pytest.mark.asyncio
async def test_invalid_typed_date_does_not_request_schedule():
    source = FakeSource()
    ui = BotUI(source)
    context = SimpleNamespace(user_data={})
    await navigate_to_date(ui, context)
    update = make_update(text="31.02.2026")
    await ui.text(update, context)
    assert "ДД.ММ.ГГГГ" in update.effective_message.reply_text.call_args.args[0]
    assert not source.requested


@pytest.mark.asyncio
async def test_typed_date_requests_selected_path():
    source = FakeSource()
    ui = BotUI(source)
    context = SimpleNamespace(user_data={})
    await navigate_to_date(ui, context)
    await ui.text(make_update(text="21.10.2026"), context)
    assert source.requested == [(("faculty", "course", "group"), date(2026, 10, 21))]


@pytest.mark.asyncio
async def test_large_catalog_pagination_and_search_select_the_correct_item():
    class LargeSource(FakeSource):
        async def menu(self, path):
            if path:
                return None
            return Menu("Выберите группу", tuple(Choice(str(index), f"Л-{index:03}") for index in range(50)))

    ui = BotUI(LargeSource())
    context = SimpleNamespace(user_data={})
    await ui.start(make_update(), context)
    second_page = await select(ui, context, "page", "1")
    keyboard = second_page.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "Л-012"
    assert len(keyboard.inline_keyboard) <= ui.PAGE_SIZE + 3
    search = make_update(text="л 025")
    await ui.text(search, context)
    assert len(ui.state(context)["choices"]) == 1
    assert ui.state(context)["choices"][0].key == "25"
    await select(ui, context, "pick", "0")
    assert ui.state(context)["path"] == ("25",)
