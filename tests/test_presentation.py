from datetime import date
from html import unescape
import re

import pytest

from bgmu_bot.presentation import calendar_keyboard, parse_date, render_schedule
from bgmu_bot.source import Schedule, Section


def test_calendar_includes_leap_day_and_cross_year_navigation():
    keyboard = calendar_keyboard(date(2024, 2, 1), "nonce").inline_keyboard
    callbacks = [button.callback_data for row in keyboard for button in row]
    assert "nonce:day:2024-02-29" in callbacks
    assert "nonce:day:2024-02-30" not in callbacks
    january = calendar_keyboard(date(2026, 1, 1), "nonce").inline_keyboard
    assert any(button.callback_data == "nonce:month:2025-12-01" for row in january for button in row)
    assert max(len(value.encode()) for value in callbacks) <= 64


@pytest.mark.parametrize("value", ["21.10.2026", "2026-10-21", " 21.10.2026 "])
def test_supported_date_formats(value):
    assert parse_date(value) == date(2026, 10, 21)


def test_sections_html_and_long_messages():
    entry = "<&>😀" * 2000
    schedule = Schedule("Группа <101>", date(2026, 10, 21), (
        Section("Лекции", (entry,)), Section("Практические занятия", ("Биология",)),
    ))
    messages = render_schedule(schedule)
    assert len(messages) > 1
    assert all(len(message.encode("utf-16-le")) // 2 < 4096 for message in messages)
    assert all("Группа &lt;101&gt;" in message for message in messages)
    assert any("Практические занятия" in message for message in messages)
    plain = "".join(unescape(re.sub(r"</?b>", "", message)) for message in messages)
    assert plain.count("😀") == entry.count("😀")
    assert "<101>" in plain


def test_no_lessons_has_explicit_result():
    message, = render_schedule(Schedule("101", date(2026, 10, 21), ()))
    assert "На сайте нет занятий" in message


@pytest.mark.parametrize("month", [date(1, 1, 1), date(9999, 12, 1)])
def test_calendar_handles_date_boundaries(month):
    assert calendar_keyboard(month, "nonce").inline_keyboard
