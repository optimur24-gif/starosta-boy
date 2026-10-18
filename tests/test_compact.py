from datetime import date
from html import unescape
import re

import httpx
import pytest

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.compact import compact_details, short_teacher
from bgmu_bot.presentation import render_schedule, render_week
from bgmu_bot.shared_schedule import SharedSchedule
from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response, event
from test_shared_schedule import original, respond


@pytest.mark.parametrize('name,expected', [
    ('Иванов Иван Иванович', 'Иванов И.И.'), ('Петрова Анна Сергеевна', 'Петрова А.С.'),
    ('Салтыков-Щедрин Михаил Евграфович', 'Салтыков-Щедрин М.Е.'),
    ('Иванов И. И.', 'Иванов И.И.'), ('Иванов И.И.', 'Иванов И.И.'),
    ('Абдельгани Яссер Осама Сабер', 'Абдельгани Я.О.С.'),
    ('', 'Преподаватель не указан'), ('Преподаватель уточняется', 'Преподаватель уточняется'),
])
def test_short_names(name, expected):
    assert short_teacher(name) == expected


def test_rooms_teachers_and_subgroups_are_paired():
    rows = [dict(groupId=1, subgroup='А', room='101', building='Корпус 1', teacher='Иванов Иван Иванович'),
            dict(groupId=1, subgroup='Б', room='202', building='Корпус 2', teacher='Петров Петр Петрович')]
    result = compact_details(rows)
    assert result == ('подгр. А: 101, Корпус 1 · Иванов И.И.', 'подгр. Б: 202, Корпус 2 · Петров П.П.')


@pytest.mark.asyncio
async def test_public_schedule_has_only_requested_fields_and_week_keeps_all_dates():
    def response(request):
        data = api_response(request, events=[event(teacher='Иванов Иван Иванович')])
        return httpx.Response(200, json=data)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source = BGMUSource(website)
        path = ('group', '26', '1', 'Лечебный факультет', '1', '1', '*')
        schedule = await source.schedule(path, date(2026, 10, 21))
        text = '\n'.join(render_schedule(schedule))
        assert text.startswith('<b>Л-101</b>\n21.10.2026')
        assert '09:00–10:35 · 🧪 Практика · Биология' in text
        assert 'Кабинет 1, Корпус 1 · Иванов И.И.' in text
        for removed in ('Практическое занятие', 'Обновление на сайте', 'UTC+5', 'Группы:', 'Осенний семестр', 'Иван Иванович'):
            assert removed not in text
        week = '\n'.join(render_week(await source.week(path, date(2026, 10, 21))))
        assert week.startswith('<b>Л-101</b>\n\n<b>19.10.2026 · Понедельник</b>')
        assert week.count('21.10.2026') == 1 and '25.10.2026' in week
        assert 'Иванов И.И.' in week


@pytest.mark.asyncio
async def test_public_and_original_bot_use_identical_schedule_fields(original):
    def response(request):
        result = respond(request)
        data = result.json()
        if request.url.params['action'] == 'schedule':
            data['events'][0]['details'][0]['teacher'] = 'Иванов Иван Иванович'
        return httpx.Response(200, json=data)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source = BGMUSource(website, cache_ttl=300)
        feed = SharedSchedule(source)
        await feed.refresh()
        original.SCHEDULE_FEED = feed
        public = '\n'.join(render_schedule(await source.schedule(feed.path, date(2026, 10, 21))))
        private = original.build_digest(date(2026, 10, 21), 'Доброе утро')
        plain = lambda value: re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', '', value))).strip()
        assert plain(public) == plain(private)
        assert 'Иванов И.И.' in private and 'Практическое занятие' not in private


@pytest.mark.parametrize('kind,label', [('Лекция','🎓 Лекция'),('Практическое занятие','🧪 Практика'),
    ('Лабораторная работа','🔬 Лабораторная'),('Семинар','💬 Семинар'),('Экзамен','📝 Экзамен'),
    ('Зачёт','✅ Зачёт'),('Консультация','📚 Консультация'),('', '📚 Занятие')])
def test_lesson_types_remain_distinct(kind,label):
    from bgmu_bot.compact import lesson_kind
    assert lesson_kind(kind)==label


@pytest.mark.asyncio
async def test_week_names_all_days_and_daily_types_are_preserved():
    from bgmu_bot.compact import DAY_NAMES
    def response(request):
        lecture=event('Анатомия');lecture['kind']='Лекция'
        practice=event('Биология');practice.update(kind='Практическое занятие',start='10:50',end='12:25')
        return httpx.Response(200,json=api_response(request,events=[lecture,practice]))
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website)
        path=('group','26','1','Лечебный факультет','1','1','*')
        daily='\n'.join(render_schedule(await source.schedule(path,date(2026,10,21))))
        weekly='\n'.join(render_week(await source.week(path,date(2026,10,21))))
        assert '21.10.2026 · Среда' in daily
        for name in DAY_NAMES:assert name in weekly
        for text in (daily,weekly):
            assert '🎓 Лекция · Анатомия' in text and '🧪 Практика · Биология' in text
