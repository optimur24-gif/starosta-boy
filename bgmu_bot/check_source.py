"""Check website reachability on the deployment host without saving a page."""

import asyncio
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys

from .bgmu import BGMUSource
from .presentation import render_schedule, render_week, today
from .source import SourceError
from .website import WebsiteClient


async def check() -> int:
    try:
        async with WebsiteClient.from_environment() as website:
            print("Соединение с сайтом: " + ("через BGMU_PROXY_URL" if website.uses_proxy else "стандартное"))
            response = await website.get()
            print(f"bgmu-time.ru: HTTP {response.status_code}, получено {len(response.content)} байт.")
            print("Доступ к странице проверен. Разбор расписания этой командой не проверяется.")
    except (SourceError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


async def verify_live(website, *, include_linked=False):
    """Exercise the adapter against real data without storing lessons."""
    source = BGMUSource(website, cache_ttl=300)
    period = await source._api(action="period")
    year, semester = str(period["period"]["year"]), str(period["period"]["semester"])
    scenarios = []
    offline_cases = []
    personal_path = None
    prefixes = await source.group_prefixes()
    for mode in (await source.menu(())).choices:
        years = await source.menu((mode.key,))
        if year not in {choice.key for choice in years.choices}:
            raise SourceError("Текущий учебный год отсутствует в каталоге.")
        path = (mode.key, year, semester)
        if mode.key == "group":
            faculties = (await source.menu(path)).choices
            faculty = next((choice for choice in faculties if choice.label == "Лечебный факультет"), faculties[0])
            path += (faculty.key,)
            courses = (await source.menu(path)).choices
            course = next((choice for choice in courses if choice.key == "1"), courses[0])
            path += (course.key,)
        entities = (await source.menu(path)).choices
        if not entities:
            raise SourceError("Каталог источника пуст: функциональная проверка не выполнена.")
        entity = next((choice for choice in entities if
                       (mode.key == "group" and choice.label == "Л-101") or
                       (mode.key == "room" and choice.key == "1169")), entities[0])
        path += (entity.key,)
        subgroups = await source.menu(path)
        if subgroups is not None:
            path += (next(choice.key for choice in subgroups.choices if choice.key == "*"),)
        if mode.key == "group":
            prefix_matches = await source.groups_with_prefix(source.group_prefix(entity.label))
            if not any(match.path[5] == path[5] for match in prefix_matches):
                raise SourceError("Выбор по обозначению группы потерял выбранную группу.")
            matches = await source.search_groups(entity.label)
            if not any(match.path[5] == path[5] for match in matches):
                raise SourceError("Быстрый поиск потерял выбранную группу.")
            resolved = await source.resolve_group(path)
            if resolved.path != path:
                raise SourceError("Личная группа неверно определена в текущем периоде.")
            personal_path = path
        data = await source._schedule_data(path)
        available_dates = sorted({event["date"] for event in data["events"] if event.get("date")})
        day = date.fromisoformat(available_dates[0]) if available_dates else today()
        schedule = await source.schedule(path, day)
        if available_dates and not schedule.sections:
            raise SourceError("День с занятиями источника отобразился пустым.")
        offline_cases.append((path,day,schedule))
        messages = render_schedule(schedule)
        week = await source.week(path, day)
        week_messages = render_week(week)
        if len(week) != 7 or not any(item.day == day and item.sections == schedule.sections for item in week):
            raise SourceError("Недельное расписание потеряло выбранный день.")
        if any(len(message.encode("utf-16-le")) // 2 > 4096 for message in week_messages):
            raise SourceError("Недельный ответ превышает ограничение Telegram.")
        if not messages or any(len(message.encode("utf-16-le")) // 2 > 4096 for message in messages):
            raise SourceError("Ответ не соответствует ограничениям Telegram.")
        checked_subgroups = []
        if subgroups is not None:
            for choice in subgroups.choices[1:3]:
                filtered = await source.schedule(path[:-1] + (choice.key,), day)
                if not render_schedule(filtered):
                    raise SourceError("Ответ для подгруппы не сформирован.")
                checked_subgroups.append(choice.key)
        scenarios.append({
            "mode": mode.key, "entity": entity.label, "date": day.isoformat(),
            "semester_events": len(data["events"]), "day_lessons": len(schedule.sections),
            "telegram_messages": len(messages), "subgroups_checked": checked_subgroups,
            "week_days": len(week), "week_messages": len(week_messages),
        })
    # Validate the production disk-read path with a transport that fails on HTTP.
    import tempfile
    import httpx
    from .snapshot import DailySnapshot
    with tempfile.TemporaryDirectory() as directory:
        snapshot = DailySnapshot(Path(directory)/'schedule.sqlite3')
        with snapshot.stage() as writer:
            for key,entry in source.cache.entries.items():
                writer.put(dict(key),entry.value)
            writer.summary({'smoke_test':True})
        def reject_network(request):
            raise AssertionError('Offline user read attempted a website request')
        async with WebsiteClient(transport=httpx.MockTransport(reject_network)) as offline_website:
            for _ in range(2):
                restored = BGMUSource(offline_website)
                restored.use_snapshot(snapshot.path)
                for path,day,expected in offline_cases:
                    if await restored.schedule(path,day) != expected:
                        raise SourceError('Локальная копия изменила расписание после перезапуска.')
    refreshed = await source.refresh((personal_path,) if personal_path else ())
    result = {"year": year, "semester": semester, "scenarios": scenarios,
              "personal_group_lookup": personal_path is not None, "group_prefixes": prefixes, "refresh": refreshed,
              "offline_snapshot": {"restart_passes":2,"entity_types_checked":len(scenarios),"website_requests_during_reads":0}}
    if include_linked:
        from .shared_schedule import SharedSchedule
        import sqlite3
        feed = SharedSchedule(source)
        await feed.refresh()
        with sqlite3.connect(':memory:') as db:
            feed.attach(db)
            count = db.execute('SELECT COUNT(*) FROM schedule').fetchone()[0]
        if count != len(feed.rows):
            raise SourceError('Передача расписания П-114 в исходный бот потеряла занятия.')
        result['linked_schedule'] = {'group': feed.group, 'semester_events': count, 'memory_only': True}
        from .bgmu import course_key
        cohorts = {}
        for item in await source._catalog(('group', year, semester)):
            faculty, course = str(item.get('faculty') or ''), course_key(item)
            if faculty and course in ('1', '2', '3', '4', '5', '6'):
                cohorts[(faculty, course)] = cohorts.get((faculty, course), 0) + 1
        if not cohorts:
            raise SourceError('Сайт не вернул курсы для проверки обновления.')
        faculty, course = min(cohorts, key=lambda key: (cohorts[key], key))
        course_result = await source.refresh_course(faculty, course, period=(year, semester))
        course_result.pop('group_ids')
        result['course_refresh'] = course_result
    return result


async def live_check(report_path=None):
    report = {"status": "failed", "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        async with WebsiteClient.from_environment() as website:
            report["proxy_configured"] = website.uses_proxy
            report.update(await verify_live(website, include_linked=True))
        report["status"] = "passed"
    except (SourceError, ValueError, KeyError, IndexError, StopIteration) as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, SourceError):
            report["message"] = str(error)
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Проверить меню и реальное расписание всех трёх разделов")
    parser.add_argument("--report", help="Записать результат и счётчики, без расписания")
    args = parser.parse_args()
    return asyncio.run(live_check(args.report) if args.live else check())


if __name__ == "__main__":
    raise SystemExit(main())
