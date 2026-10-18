"""Pass one website response to both bots without persisting timetable rows."""

import asyncio
from datetime import date
import hashlib
import json
import logging
import re
import time

from aiogram import BaseMiddleware

from .compact import compact_details, lesson_kind
from .commands import schedule_request
from .presentation import today
from .source import SourceError


def normalize_group(value):
    return re.sub(r'[^\w]', '', value.casefold())


class SharedSchedule:
    group = 'П-114'

    def __init__(self, source):
        self.source = source
        self.rows = ()
        self.path = None
        self.ready = False
        self.warning = ''
        self.updated = 0
        self.generation = -1
        self.retry_at = 0
        self.lock = asyncio.Lock()

    async def refresh(self, *, force=False):
        async with self.lock:
            if not force and self.ready and time.monotonic()-self.updated < 60 and self.generation == self.source.cache.generation:
                return
            matches = await self.source.search_groups(self.group)
            exact = [match for match in matches if normalize_group(match.labels[5]) == normalize_group(self.group)]
            if len(exact) != 1:
                raise SourceError('Группа П-114 не найдена однозначно в текущем периоде сайта.')
            path = exact[0].path + ('*',)
            payload = await self.source._schedule_data(path)
            if normalize_group(str(payload['entity'].get('name') or '')) != normalize_group(self.group):
                raise SourceError('Сайт вернул расписание другой группы вместо П-114.')
            rows = []
            lesson_kinds = {}
            occurrences = {}
            for event in payload['events']:
                if not event['date']:
                    continue
                try:
                    date.fromisoformat(event['date'])
                except ValueError:
                    raise SourceError('Сайт вернул неверную дату занятия П-114.') from None
                # A site lesson ID may recur on several dates or subgroups.
                identity = json.dumps(event, ensure_ascii=False, sort_keys=True)
                occurrence = occurrences.get(identity, 0)
                occurrences[identity] = occurrence + 1
                row_id = int(hashlib.sha256(f'{identity}#{occurrence}'.encode()).hexdigest()[:15], 16)
                lesson_kinds[row_id] = lesson_kind(event.get('kind'))
                subject = str(event.get('subject') or 'Предмет уточняется')
                location = '\n'.join(compact_details(event['details']))
                rows.append((row_id, event['date'], '–'.join(str(event.get(key) or '') for key in ('start', 'end')), subject, location))
            # Publish a complete semester at once; a failure leaves the old snapshot.
            self.rows = tuple(rows)
            self.lesson_kinds = lesson_kinds
            self.path = path
            self.ready = True
            meta = payload.get('meta') or {}
            self.warning = 'Источник вернул сохранённую копию: возможны изменения расписания.' if meta.get('stale') else ''
            if meta.get('warning'):
                self.warning = ' '.join(filter(None, (self.warning, str(meta['warning']))))
            if payload.get('undatedRecords'):
                self.warning += ' На сайте есть записи без точной даты.'
            self.updated = time.monotonic()
            self.generation = self.source.cache.generation

    async def refresh_safely(self, *, force=False):
        if not force and time.monotonic() < self.retry_at:
            return False
        try:
            await self.refresh(force=force)
            self.retry_at = 0
            return True
        except SourceError as error:
            self.retry_at = time.monotonic() + 30
            self.warning = ('Не удалось прочитать локальное расписание П-114. ' if getattr(self.source,'snapshot',None) is not None else 'Не удалось обновить П-114 с сайта. ') + ('Показана предыдущая копия.' if self.ready else 'Расписание пока недоступно.')
            logging.getLogger(__name__).warning('Обновление П-114 не выполнено (%s)', type(error).__name__)
            return False

    def attach(self, connection):
        # TEMP shadows the legacy on-disk table for every existing read path:
        # commands, digests, current lesson, search, statistics and CSV/ICS.
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute('CREATE TEMP TABLE schedule(id INTEGER PRIMARY KEY,date TEXT,time TEXT,subject TEXT,room TEXT)')
        connection.executemany('INSERT INTO temp.schedule VALUES (?,?,?,?,?)', self.rows)
        connection.commit()
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            connection.execute(f"""CREATE TEMP TRIGGER website_schedule_{action} BEFORE {action} ON temp.schedule
                BEGIN SELECT RAISE(ABORT, 'Website schedule is read-only'); END""")


WRITE_CALLBACKS = ('add_class_', 'parse_week_', 'cls_del_', 'clear_week_', 'do_clear_week_', 'sch_edit', 'sch_copy')
SCHEDULE_CALLBACKS = ('week_', 'date_', 'menu_schedule', 'sch_', 'main_menu', 'study_plan')


class SharedScheduleMiddleware(BaseMiddleware):
    def __init__(self, feed_getter):
        self.feed_getter = feed_getter

    async def __call__(self, handler, event, data):
        feed = self.feed_getter()
        if feed is None:
            return await handler(event, data)
        callback = getattr(event, 'data', '') or ''
        if callback.startswith(WRITE_CALLBACKS):
            await event.answer('Расписание П-114 поступает с сайта через общего бота. Ручное изменение отключено.', show_alert=True)
            return
        text = getattr(event, 'text', '') or ''
        try:
            request = schedule_request(text, today())
        except ValueError:
            request = True
        related = request or callback.startswith(SCHEDULE_CALLBACKS) or re.match(r'(?i)^\s*(?:расписание|найти|аудитория|нагрузка|план|/start)', text)
        # Moderation/homework must not wait for a website timeout.
        if not related:
            return await handler(event, data)
        await feed.refresh_safely()
        if not feed.ready:
            if callback:
                await event.answer('Расписание П-114 пока недоступно. Попробуйте позже.', show_alert=True)
            else:
                await event.answer('⚠️ Расписание П-114 пока недоступно. Попробуйте позже.')
            return
        return await handler(event, data)
