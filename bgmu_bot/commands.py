"""Russian schedule commands shared by private and group conversations."""

from dataclasses import dataclass
from datetime import date, timedelta
import re

from .presentation import parse_date

WEEKDAYS = {
    "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "среда": 2, "среду": 2,
    "ср": 2, "четверг": 3, "чт": 3, "пятница": 4, "пятницу": 4, "пт": 4,
    "суббота": 5, "субботу": 5, "сб": 5, "воскресенье": 6, "вс": 6,
}


@dataclass(frozen=True)
class Request:
    kind: str
    day: date


def schedule_request(text, day):
    low = re.sub(r"\s+", " ", text.strip().casefold().replace("ё", "е"))
    if low.startswith('/'):
        head, _, tail = low.partition(' ')
        head = head.split('@', 1)[0]
        low = {'/today': 'сегодня', '/tomorrow': 'завтра', '/week': 'неделя', '/left': 'до конца', '/schedule': 'расписание'}.get(head, head)
        if tail:
            low += ' на ' + tail if low == 'расписание' else ' ' + tail
    if low in ('до конца', 'до конца пары', 'сколько до конца'):
        return Request('left', day)
    if low in ('сейчас', 'какая пара сейчас', 'текущая пара'):
        return Request('current', day)
    if low in ('следующая пара', 'что дальше'):
        return Request('next', day)
    if low in ('неделя', 'вся неделя', 'расписание недели', 'расписание на неделю', 'расписание на неделя'):
        return Request('week', day)
    if low in ('след неделя', 'следующая неделя', 'расписание на следующую неделю'):
        return Request('week', day + timedelta(days=7))
    if low in ('расписание', 'сегодня', 'что сегодня', 'пары сегодня'):
        return Request('day', day)
    if low in ('завтра', 'что завтра', 'пары завтра'):
        return Request('day', day + timedelta(days=1))
    if low == 'расписание на':
        raise ValueError('На какой день? Напиши, например: расписание на завтра, расписание на среду или расписание на 25.10.')
    if low.startswith('расписание на '):
        value = low[len('расписание на '):]
        if value == 'сегодня':
            return Request('day', day)
        if value == 'завтра':
            return Request('day', day + timedelta(days=1))
        if value in WEEKDAYS:
            return Request('day', day + timedelta(days=(WEEKDAYS[value] - day.weekday()) % 7))
        if re.fullmatch(r'\d{1,2}\.\d{1,2}', value):
            value += f'.{day.year}'
        try:
            return Request('day', parse_date(value))
        except ValueError:
            raise ValueError('Напиши, например: расписание на среду, расписание на 21.10 или расписание на 21.10.2026.') from None
    return None
