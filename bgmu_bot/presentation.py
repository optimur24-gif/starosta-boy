import calendar
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup

from .source import Schedule, Section
from .compact import day_label


TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
MONTHS = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def today() -> date:
    return datetime.now(TIMEZONE).date()


def parse_date(value: str) -> date:
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError("Введите дату в формате ДД.ММ.ГГГГ, например 21.10.2026.")


def calendar_keyboard(month: date, nonce: str) -> InlineKeyboardMarkup:
    def callback(action: str, value: str = "") -> str:
        return f"{nonce}:{action}:{value}"

    rows = [[Button(MONTHS[month.month - 1] + f" {month.year}", callback_data=callback("noop"))]]
    rows.append([Button(day, callback_data=callback("noop")) for day in WEEKDAYS])
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(month.year, month.month):
        rows.append([
            Button(
                str(day) if day else "·",
                callback_data=callback("day", date(month.year, month.month, day).isoformat())
                if day else callback("noop"),
            )
            for day in week
        ])
    first = month.replace(day=1)
    previous = (first - timedelta(days=1)).replace(day=1) if first > date.min else None
    following = (first + timedelta(days=32)).replace(day=1) if first < date(9999, 12, 1) else None
    rows.append([
        Button("‹ Месяц", callback_data=callback("month", previous.isoformat()) if previous else callback("noop")),
        Button("Месяц ›", callback_data=callback("month", following.isoformat()) if following else callback("noop")),
    ])
    current = today()
    rows.append([
        Button("Сегодня", callback_data=callback("day", current.isoformat())),
        Button("Завтра", callback_data=callback("day", (current + timedelta(days=1)).isoformat())),
    ])
    rows.append([Button("← Назад", callback_data=callback("back"))])
    return InlineKeyboardMarkup(rows)


def render_schedule(schedule: Schedule, *, show_date=True) -> list[str]:
    """Keep section headings and split before Telegram's message size limit."""
    heading = f"<b>{escape(schedule.title)}</b>" + ("\n"+day_label(schedule.day) if show_date else '')
    sections = list(schedule.sections) or [Section("🌿 Свободный день", ("На сайте нет занятий на эту дату.",))]
    notices = schedule.display_notices if schedule.display_notices is not None else schedule.notices
    if notices:
        sections.append(Section("⚠️", notices))

    messages: list[str] = []
    current = heading
    # The conservative limit applies before HTML escaping too: it leaves room
    # for headings and keeps each message below Telegram's UTF-16 limit.
    for section in sections:
        section_heading = f"<b>{escape(section.short_title or section.title)}</b>"
        entries = section.short_entries if section.short_entries is not None else section.entries
        for index, entry in enumerate(entries or ("Нет занятий.",)):
            # Escape after splitting so HTML entities are never cut in half.
            for offset in range(0, max(1, len(entry)), 450):
                piece = escape(entry[offset:offset + 450])
                prefix = "\n\n" + section_heading + "\n" if index == 0 and offset == 0 else "\n\n"
                addition = prefix + piece
                if len((current + addition).encode("utf-16-le")) // 2 > 3500:
                    messages.append(current)
                    current = heading + "\n\n" + section_heading + "\n" + piece
                else:
                    current += addition
    messages.append(current)
    return messages


def render_week(schedules):
    if not schedules:
        raise ValueError("A week must contain schedule days")
    first, last = schedules[0], schedules[-1]
    sections = []
    for schedule in schedules:
        title = day_label(schedule.day)
        lessons = tuple("\n".join((section.short_title or section.title,
                                  *(section.short_entries if section.short_entries is not None else section.entries))) for section in schedule.sections)
        sections.append(Section(title, lessons or ("На сайте нет занятий на эту дату.",)))
    combined = Schedule(
        first.title, first.day,
        tuple(sections), first.notices, first.source_url.replace("view=day", "view=week"), first.display_notices,
    )
    return render_schedule(combined, show_date=False)
