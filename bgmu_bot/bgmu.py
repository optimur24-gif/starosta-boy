"""Adapter for the API used by bgmu-time.ru/assets/app.js."""

from collections import OrderedDict
from contextlib import aclosing
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .cache import ResponseCache
from .compact import compact_details, lesson_kind
from .source import Choice, GroupMatch, GroupUnavailable, Menu, Schedule, Section, SourceError
from .website import WebsiteClient


TYPES = {"group": "Группы"}
SEMESTERS = {"1": "Осенний семестр", "2": "Весенний семестр"}


def natural_key(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r"(\d+)", str(value)))


def course_key(item):
    value = item.get("course")
    return str(value) if value not in (None, "", 0) else "_"


def readable_details(details):
    """Combine subgroup rows only when teacher and location match as on site."""
    combined = OrderedDict()
    for detail in details:
        identity = lambda key, label: str(detail.get(key) or detail.get(label) or "")
        key = (identity("groupId", "group"), identity("teacherId", "teacher"),
               identity("roomId", "room"), str(detail.get("building") or ""))
        if key not in combined:
            combined[key] = {"detail": detail, "subgroups": set(), "whole": False}
        row = combined[key]
        subgroup = str(detail.get("subgroup") or "").strip()
        if subgroup:
            row["subgroups"].add(subgroup)
        else:
            row["whole"] = True
    lines = []
    for row in combined.values():
        detail = row["detail"]
        group = str(detail.get("group") or "")
        subgroups = sorted(row["subgroups"], key=natural_key) if not row["whole"] else []
        audience = " · ".join(part for part in (
            group, "Подгруппа " + ", ".join(subgroups) if subgroups else "",
        ) if part)
        teacher = str(detail.get("teacher") or "Преподаватель уточняется")
        places = list(dict.fromkeys(str(detail.get(key) or "").strip() for key in ("room", "building")))
        places = [place for place in places if place and not any(place != other and place in other for other in places)]
        place = ", ".join(places) or "Место уточняется"
        lines.append("\n".join(part for part in ("👥 " + audience if audience else "", "👨‍🏫 " + teacher, "📍 " + place) if part))
    return lines


class BGMUSource:
    def __init__(self, website: WebsiteClient, *, cache_ttl=0):
        self.website = website
        self.cache = ResponseCache(ttl=cache_ttl)
        self.refresh_lock = asyncio.Lock()
        self.snapshot = None
        self.daily_refresh_lock = asyncio.Lock()
        self.refresh_progress = {}
        self.refresh_timeout = 900

    def use_snapshot(self, path):
        from .snapshot import DailySnapshot
        self.snapshot = DailySnapshot(path)
        self.snapshot.on_progress = self._progress
        self.cache.replace({})
        self.cache.ttl = 86400

    def _begin_progress(self, operation):
        self.refresh_progress = {'operation':operation,'completed':0,'state':'running',
                                 'started_at':datetime.now(timezone.utc).isoformat(),'history':[]}
        self._progress('prepare', 'Начинаю обновление локального расписания')

    def _progress(self, phase, detail, **fields):
        now = datetime.now(timezone.utc).isoformat()
        self.refresh_progress.update(phase=phase,detail=detail,updated_at=now,**fields)
        history = self.refresh_progress.setdefault('history',[])
        history.append({'at':now,'detail':detail})
        del history[:-8]

    def _finish_progress(self, state):
        self.refresh_progress.update(state=state,finished_at=datetime.now(timezone.utc).isoformat())
        if state == 'completed':
            self._progress('done','Обновление расписания завершено')

    async def _api(self, **params):
        async def load():
            if self.snapshot is not None:
                return self.snapshot.get(params)
            data = await self._request_api(**params)
            if params.get('action') == 'schedule':
                self._validate_schedule(data)
            return data
        return await self.cache.get(self._cache_key(params), load)

    @staticmethod
    def _cache_key(params):
        return tuple(sorted((key, str(value)) for key, value in params.items()))

    async def _request_api(self, *, _progress_name=None, **params):
        tracked = self.snapshot is not None
        if tracked:
            request = {key:str(value) for key,value in params.items() if key in ('action','type','id','year','semester')}
            kind = TYPES.get(params.get('type'),'')
            target = {'period':'учебный период','catalog':'каталог: '+kind,
                      'schedule':'расписание: '+(_progress_name or str(params.get('id','')))}.get(params.get('action'),'данные сайта')
            self._progress('waiting','Ожидаю интервал перед запросом: '+target,request=request)
            await self.snapshot.wait_for_request()
            self._progress('download','Обращаюсь к bgmu-time.ru, скачиваю '+target)
        response = await self.website.get("/api.php", params=params)
        if tracked:
            self._progress('validate','Ответ сайта получен, проверяю '+target)
        try:
            data = response.json()
        except ValueError:
            raise SourceError("Сайт вернул ответ неподходящего формата.") from None
        if not isinstance(data, dict) or data.get("success") is not True:
            raise SourceError("Источник не смог получить расписание. Попробуйте позже.")
        return data

    async def _catalog(self, path):
        data = await self._api(action="catalog", type=path[0], year=path[1], semester=path[2])
        return self._catalog_items(data)

    @staticmethod
    def _catalog_items(data):
        items = data.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) or not str(item.get("id", "")).isdigit() or not isinstance(item.get("name"), str)
            for item in items
        ):
            raise SourceError("Изменился формат каталога сайта.")
        return sorted(items, key=lambda item: natural_key(item["name"]))

    @staticmethod
    def _validate_path(path):
        if len(path) > 7 or (path and path[0] not in TYPES):
            raise SourceError("Выберите раздел заново через /start.")
        if len(path) > 1 and not path[1].isdigit():
            raise SourceError("Некорректный учебный год.")
        if len(path) > 2 and path[2] not in SEMESTERS:
            raise SourceError("Некорректный семестр.")
        if path and path[0] != "group" and len(path) > 4:
            raise SourceError("Выберите раздел заново.")

    @staticmethod
    def _entity_id(path):
        if not path:
            raise SourceError("Сначала выберите раздел расписания.")
        index = 5 if path[0] == "group" else 3
        if len(path) <= index or not path[index].isdigit() or int(path[index]) <= 0:
            raise SourceError("Выберите группу заново.")
        return path[index]

    async def _schedule_data(self, path):
        entity_id = self._entity_id(path)
        data = await self._api(action="schedule", type=path[0], id=entity_id, year=path[1], semester=path[2])
        self._validate_schedule(data)
        return data

    @staticmethod
    def _validate_schedule(data):
        events = data.get("events")
        if not isinstance(data.get("entity"), dict) or not isinstance(events, list):
            raise SourceError("Изменился формат расписания сайта.")
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("date"), str) or not isinstance(event.get("details"), list):
                raise SourceError("Изменился формат занятия в источнике.")
            if any(not isinstance(detail, dict) for detail in event["details"]):
                raise SourceError("Изменился формат подгрупп в источнике.")

    async def menu(self, path):
        self._validate_path(path)
        if not path:
            return Menu("Выберите раздел расписания", tuple(Choice(key, label) for key, label in TYPES.items()))
        if len(path) == 1:
            data = await self._api(action="period")
            years = data.get("years")
            if self.snapshot is not None and isinstance(years,dict):
                current_year = str((data.get('period') or {}).get('year',''))
                years = {key:value for key,value in years.items() if str(key) == current_year}
            if not isinstance(years, dict) or not years:
                raise SourceError("Сайт не вернул доступные учебные годы.")
            return Menu("Выберите учебный год", tuple(
                Choice(str(key), str(label)) for key, label in sorted(years.items(), key=lambda pair: natural_key(pair[0]), reverse=True)
            ))
        if len(path) == 2 and self.snapshot is not None:
            period = (await self._api(action='period'))['period']
            semester = str(period['semester'])
            return Menu('Выберите семестр',(Choice(semester,SEMESTERS[semester]),))
        if len(path) == 2:
            return Menu("Выберите семестр", tuple(Choice(key, label) for key, label in SEMESTERS.items()))
        if path[0] != "group":
            if len(path) == 3:
                items = await self._catalog(path)
                return Menu("Выберите преподавателя" if path[0] == "teacher" else "Выберите аудиторию", tuple(
                    Choice(str(item["id"]), item["name"] + (" — " + str(item["subtitle"]) if path[0] == "room" and item.get("subtitle") else "")) for item in items
                ))
            if len(path) == 4:
                return None
            raise SourceError("Выберите раздел заново.")
        if len(path) >= 6:
            if len(path) == 7:
                return None
            data = await self._schedule_data(path)
            subgroups = sorted({str(detail["subgroup"]) for event in data["events"]
                                for detail in event["details"] if detail.get("subgroup")}, key=natural_key)
            if not subgroups:
                return None
            return Menu("Выберите подгруппу", (Choice("*", "Все подгруппы"),) + tuple(
                Choice(subgroup, "Подгруппа " + subgroup) for subgroup in subgroups
            ))
        items = await self._catalog(path)
        if len(path) == 3:
            faculties = sorted({str(item.get("faculty") or "") for item in items}, key=natural_key)
            return Menu("Выберите факультет", tuple(Choice(value, value or "Факультет не указан") for value in faculties))
        items = [item for item in items if str(item.get("faculty") or "") == path[3]]
        if len(path) == 4:
            courses = sorted({course_key(item) for item in items}, key=natural_key)
            return Menu("Выберите курс", tuple(Choice(value, value + " курс" if value != "_" else "Курс не указан") for value in courses))
        items = [item for item in items if course_key(item) == path[4]]
        return Menu("Выберите группу", tuple(Choice(str(item["id"]), item["name"]) for item in items))

    async def schedule(self, path, day: date):
        self._validate_path(path)
        data = await self._schedule_data(path)
        return self._format_schedule(data, path, day)

    def _format_schedule(self, data, path, day):
        subgroup = path[6] if path[0] == "group" and len(path) == 7 and path[6] != "*" else ""
        events = sorted(
            (event for event in data["events"] if event["date"] == day.isoformat()),
            key=lambda event: (str(event.get("start") or "99:99"), str(event.get("end") or "")),
        )
        sections = []
        for event in events:
            details = event["details"]
            if subgroup and details:
                details = [detail for detail in details if not detail.get("subgroup") or str(detail["subgroup"]) == subgroup]
                if not details:
                    continue
            time = "–".join(str(event.get(key)) for key in ("start", "end") if event.get(key)) or "Время уточняется"
            kind = str(event.get("kind") or "Занятие")
            heading = f"{time} · {kind}" + (" · Цикл" if event.get("cycle") else "")
            entries = [str(event.get("subject") or "Предмет уточняется"), *readable_details(details)]
            groups = [str(group["name"]) for group in event.get("groups", []) if isinstance(group, dict) and group.get("name")]
            if groups:
                entries.insert(1, "Группы: " + ", ".join(dict.fromkeys(groups)))
            if event.get("comment"):
                entries.append(str(event["comment"]))
            sections.append(Section(heading, tuple(entries), time+' · '+lesson_kind(event.get('kind'))+' · '+entries[0], compact_details(details, show_subgroups=not bool(subgroup))))
        year = int(path[1])
        year_label = f"{2000 + year}/{2001 + year}" if year < 100 else str(year)
        notices = [f"{year_label} · {SEMESTERS[path[2]]}", "Время занятий: Уфа, UTC+5."]
        display_notices = []
        if data["entity"].get("subtitle"):
            notices.append(str(data["entity"]["subtitle"]))
        meta = data.get("meta") or {}
        if isinstance(meta, dict):
            if meta.get("stale"):
                notices.append("Сайт вернул сохранённую копию: возможны изменения расписания.")
                display_notices.append(notices[-1])
            if meta.get("warning"):
                notices.append(str(meta["warning"]))
                display_notices.append(notices[-1])
            if meta.get("updatedAt"):
                updated = str(meta["updatedAt"])
                try:
                    timestamp = datetime.fromisoformat(updated)
                    if timestamp.tzinfo is not None:
                        updated = timestamp.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m.%Y %H:%M") + " (Уфа)"
                except ValueError:
                    pass
                notices.append("Обновление на сайте: " + updated)
        if data.get("undatedRecords"):
            notices.append("В источнике есть записи без точной даты. Проверьте их в ИСУ БГМУ.")
            display_notices.append(notices[-1])
        params = dict(type=path[0], id=self._entity_id(path), year=path[1], semester=path[2], date=day.isoformat(), view="day")
        if subgroup:
            params["subgroup"] = subgroup
        title = str(data["entity"].get("name") or TYPES[path[0]])
        if subgroup:
            title += " · Подгруппа " + subgroup
        return Schedule(title, day, tuple(sections), tuple(notices), "https://bgmu-time.ru/?" + urlencode(params), tuple(display_notices))

    async def week(self, path, day):
        self._validate_path(path)
        data = await self._schedule_data(path)
        monday = day - timedelta(days=day.weekday())
        if monday > date.max - timedelta(days=6):
            raise SourceError("Неделя выходит за пределы допустимых дат.")
        return tuple(self._format_schedule(data, path, monday + timedelta(days=index)) for index in range(7))

    async def current_group_start(self):
        data = await self._api(action="period")
        period = data.get("period") or {}
        year, semester = str(period.get("year", "")), str(period.get("semester", ""))
        path = ("group", year, semester)
        self._validate_path(path)
        label = str((data.get("years") or {}).get(year, period.get("label") or year))
        return GroupMatch(path, ("Группы", label, SEMESTERS[semester]))

    @staticmethod
    def _group_match(start, item, subgroup=None):
        faculty = str(item.get("faculty") or "")
        course = course_key(item)
        path = start.path + (faculty, course, str(item["id"]))
        labels = start.labels + (faculty or "Факультет не указан", course + " курс" if course != "_" else "Курс не указан", item["name"])
        if subgroup is not None:
            path += (subgroup,)
            labels += ("Все подгруппы" if subgroup == "*" else "Подгруппа " + subgroup,)
        return GroupMatch(path, labels)

    async def search_groups(self, query):
        start = await self.current_group_start()
        items = await self._catalog(start.path)
        normalize = lambda value: re.sub(r"[^\w]", "", str(value).casefold().replace("ё", "е"))
        needle = normalize(query)
        return tuple(self._group_match(start, item) for item in items if needle in normalize(item["name"]))

    @staticmethod
    def group_prefix(name):
        return re.split(r"\d", name, maxsplit=1)[0].strip() or "Без префикса"

    async def group_prefixes(self):
        start = await self.current_group_start()
        items = await self._catalog(start.path)
        return tuple(sorted({self.group_prefix(item["name"]) for item in items}, key=natural_key))

    async def groups_with_prefix(self, prefix):
        start = await self.current_group_start()
        items = await self._catalog(start.path)
        return tuple(self._group_match(start, item) for item in items if self.group_prefix(item["name"]) == prefix)

    async def resolve_group(self, path):
        start = await self.current_group_start()
        items = await self._catalog(start.path)
        selected = next((item for item in items if str(item["id"]) == path[5]), None)
        if selected is None:
            raise GroupUnavailable("Группа отсутствует в текущем периоде. Выберите её заново.")
        return self._group_match(start, selected, path[6] if len(path) == 7 else None)

    async def refresh(self, paths=()):
        async with self.refresh_lock:
            if self.snapshot is not None:
                return await self._timed_snapshot_refresh(self._refresh_snapshot())
            # Build the next generation before discarding any current values.
            # A failed upstream request leaves the previous generation intact.
            period = await self._request_api(action="period")
            current = period.get("period") or {}
            year, semester = str(current.get("year", "")), str(current.get("semester", ""))
            self._validate_path(("group", year, semester))
            values = {self._cache_key({"action": "period"}): period}
            valid_groups = set()
            for entity_type in TYPES:
                params = dict(action="catalog", type=entity_type, year=year, semester=semester)
                catalog = await self._request_api(**params)
                self._catalog_items(catalog)
                values[self._cache_key(params)] = catalog
                if entity_type == "group":
                    valid_groups = {str(item["id"]) for item in catalog["items"]}
            group_ids = list(dict.fromkeys(path[5] for path in paths if len(path) >= 6 and path[0] == "group" and path[5] in valid_groups))
            limit = max(0, self.cache.max_entries - len(values))
            for group_id in group_ids[:limit]:
                params = dict(action="schedule", type="group", id=group_id, year=year, semester=semester)
                payload = await self._request_api(**params)
                self._validate_schedule(payload)
                values[self._cache_key(params)] = payload
            self.cache.replace(values)
            return {"refreshed_at": datetime.now(timezone.utc).isoformat(), "catalogs": len(TYPES),
                    "groups": min(len(group_ids), limit), "on_demand_groups": max(0, len(group_ids) - limit)}

    async def refresh_course(self, faculty, course, *, period):
        async with self.refresh_lock:
            if self.snapshot is not None:
                try:
                    return await self._timed_snapshot_refresh(self._refresh_course_snapshot(faculty,course,period=period))
                except asyncio.CancelledError:
                    self._finish_progress('cancelled')
                    raise
                except Exception:
                    self._finish_progress('failed')
                    raise
            current = await self._request_api(action='period')
            detected = current.get('period') or {}
            year, semester = str(detected.get('year', '')), str(detected.get('semester', ''))
            self._validate_path(('group', year, semester))
            if (year, semester) != tuple(period):
                raise SourceError('Учебный период изменился. Выберите факультет и курс заново.')
            params = dict(action='catalog', type='group', year=year, semester=semester)
            catalog = await self._request_api(**params)
            items = self._catalog_items(catalog)
            selected = [item for item in items if str(item.get('faculty') or '') == faculty and course_key(item) == course]
            if not selected:
                raise SourceError('На этом факультете и курсе нет групп. Выберите курс заново.')
            values = {}
            semaphore = asyncio.Semaphore(3)
            async def load(item):
                async with semaphore:
                    query = dict(action='schedule', type='group', id=str(item['id']), year=year, semester=semester)
                    payload = await self._request_api(**query)
                    self._validate_schedule(payload)
                    return self._cache_key(query), payload
            responses = await asyncio.gather(*(load(item) for item in selected), return_exceptions=True)
            for response in responses:
                if isinstance(response, BaseException):
                    raise response
                key, payload = response
                values[key] = payload
            # Publish only after the whole course succeeds; unrelated entries keep
            # their original age. Catalog/period remain available even for large courses.
            values[self._cache_key(params)] = catalog
            values[self._cache_key({'action': 'period'})] = current
            stale_groups = sum(bool(data['meta'].get('stale')) for key, data in values.items()
                               if dict(key).get('action') == 'schedule' and isinstance(data.get('meta'), dict))
            self.cache.merge(values)
            return {'refreshed_at': datetime.now(timezone.utc).isoformat(), 'faculty': faculty,
                    'course': course, 'groups': len(selected), 'cached_groups': min(len(selected), self.cache.max_entries-2),
                    'stale_groups': stale_groups,
                    'group_ids': [str(item['id']) for item in selected]}

    async def aclose(self):
        await self.website.aclose()

    async def _timed_snapshot_refresh(self, operation):
        try:
            async with asyncio.timeout(self.refresh_timeout):
                return await operation
        except TimeoutError:
            self._finish_progress('failed')
            self.snapshot.last_error = True
            raise SourceError('Обновление остановлено: превышен лимит 15 минут. '
                              'Сайт не успел отдать все данные; предыдущая копия сохранена.') from None

    async def _download_schedules(self, items, kind, year, semester):
        """Keep at most four requests in flight; the shared limiter spaces starts."""
        iterator = iter(items)
        pending = {}
        def start_next():
            item = next(iterator,None)
            if item is None:
                return False
            params = dict(action='schedule',type=kind,id=str(item['id']),year=year,semester=semester)
            task = asyncio.create_task(self._request_api(_progress_name=item['name'][:120],**params))
            pending[task] = (item,params)
            self.refresh_progress['active_downloads'] = [row[0]['name'][:120] for row in pending.values()]
            return True
        try:
            for _ in range(4):
                if not start_next():break
            while pending:
                done,_ = await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    item,params = pending[task]
                    try:
                        payload = task.result()
                    except Exception:
                        self._progress('download','Не удалось скачать расписание: '+item['name'][:120],request={k:str(v) for k,v in params.items()})
                        raise
                    del pending[task]
                    self.refresh_progress['active_downloads'] = [row[0]['name'][:120] for row in pending.values()]
                    self._progress('validate','Проверяю расписание: '+item['name'][:120],request={k:str(v) for k,v in params.items()})
                    yield item,params,payload
                    start_next()
        finally:
            for task in pending:task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
            self.refresh_progress['active_downloads'] = []

    async def _refresh_snapshot(self):
        self._begin_progress('full')
        logger = logging.getLogger(__name__)
        logger.info('Начата полная загрузка ежедневной локальной копии расписания')
        try:
            current = await self._request_api(action='period')
            detected = current.get('period') or {}
            year, semester = str(detected.get('year','')), str(detected.get('semester',''))
            self._validate_path(('group',year,semester))
            counts = {}
            stale = 0
            self._progress('save','Сохраняю локальную базу расписания')
            with self.snapshot.stage() as writer:
                writer.put({'action':'period'},current)
                for kind in TYPES:
                    query = dict(action='catalog',type=kind,year=year,semester=semester)
                    catalog = await self._request_api(**query)
                    items = self._catalog_items(catalog)
                    if kind == 'group' and not items:
                        raise SourceError('Сайт вернул пустой каталог групп. Предыдущая копия сохранена.')
                    writer.put(query,catalog)
                    counts[kind] = len(items)
                    self.refresh_progress.update(section=TYPES[kind],section_total=len(items),section_completed=0,entity_name='')
                    logger.info('Каталог %s: %s расписаний для загрузки',kind,len(items))
                    index = 0
                    async with aclosing(self._download_schedules(items,kind,year,semester)) as downloads:
                        async for item,params,payload in downloads:
                            index += 1
                            self._validate_schedule(payload)
                            stale += int(bool((payload.get('meta') or {}).get('stale')))
                            writer.put(params,payload)
                            self.refresh_progress['completed'] += 1
                            self.refresh_progress['section_completed'] = index
                            self._progress('stored','Загружено и записано: '+item['name'][:120])
                            if index % 50 == 0 or index == len(items):
                                logger.info('Загрузка %s: %s/%s',kind,index,len(items))
                self._progress('save','Сохраняю локальную базу расписания')
                result = {'refreshed_at':datetime.now(timezone.utc).isoformat(), 'catalogs':len(TYPES),
                          'groups':counts['group'],'teachers':0,'rooms':0,
                          'stale_schedules':stale,'on_demand_groups':0,'local_snapshot':True,
                          'year':year,'semester':semester}
                writer.summary({**result,'full_refresh_at':result['refreshed_at']})
            self._finish_progress('completed')
            logger.info('Ежедневная копия успешно обновлена: групп %s',counts['group'])
            self.snapshot.last_error = False
            self.cache.replace({})
            return result
        except asyncio.CancelledError:
            self._finish_progress('cancelled')
            raise
        except Exception:
            self._finish_progress('failed')
            self.snapshot.last_error = True
            self.cache.replace({})
            raise

    async def _refresh_course_snapshot(self, faculty, course, *, period):
        self._begin_progress('course')
        if not self.snapshot.available:
            raise SourceError('Сначала нужно полное обновление локальной копии.')
        detected = self.snapshot.get({'action':'period'})['period']
        if (str(detected['year']),str(detected['semester'])) != tuple(period):
            raise SourceError('Учебный период изменился. Выполните полное обновление.')
        current = await self._request_api(action='period')
        year, semester = str(current['period']['year']), str(current['period']['semester'])
        if (year,semester) != tuple(period):
            raise SourceError('Учебный период изменился. Выполните полное обновление.')
        query = dict(action='catalog',type='group',year=year,semester=semester)
        catalog = await self._request_api(**query)
        selected = [item for item in self._catalog_items(catalog) if str(item.get('faculty') or '') == faculty and course_key(item) == course]
        if not selected:
            raise SourceError('На выбранном факультете и курсе групп нет.')
        self.refresh_progress.update(section='Группы выбранного курса',section_total=len(selected),section_completed=0)
        stale = 0
        self._progress('save','Сохраняю локальную базу расписания')
        with self.snapshot.stage(merge=True) as writer:
            index = 0
            async with aclosing(self._download_schedules(selected,'group',year,semester)) as downloads:
                async for item,params,payload in downloads:
                    index += 1
                    self._validate_schedule(payload)
                    stale += int(bool((payload.get('meta') or {}).get('stale')))
                    writer.put(params,payload)
                    self.refresh_progress.update(completed=index,section_completed=index)
                    self._progress('stored','Загружено и записано: '+item['name'][:120])
            self._progress('save','Сохраняю локальную базу расписания')
            previous_catalog = self.snapshot.get(query)
            other_items = [item for item in self._catalog_items(previous_catalog)
                           if not (str(item.get('faculty') or '') == faculty and course_key(item) == course)]
            writer.put(query,{**previous_catalog,'items':other_items+selected})
            writer.put({'action':'period'},current)
            result = {'refreshed_at':datetime.now(timezone.utc).isoformat(),'faculty':faculty,'course':course,
                      'groups':len(selected),'cached_groups':len(selected),'stale_groups':stale,
                      'group_ids':[str(item['id']) for item in selected],'local_snapshot':True}
            summary = self.snapshot.info()
            summary['last_course_refresh'] = result
            writer.summary(summary)
        self.cache.replace({})
        self._finish_progress('completed')
        return result
