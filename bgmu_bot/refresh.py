"""Manual and scheduled refresh use the same atomic source operation."""

from datetime import datetime, timezone, time, timedelta
from .presentation import TIMEZONE
import logging
import asyncio
from .owner_alerts import report_refresh_failure
from .errors import error_description


async def refresh_saved_groups(source, profiles):
    try:
        result = await source.refresh(profiles.paths())
    except Exception as error:
        profiles.set_metadata("last_refresh_error", {
            "at": datetime.now(timezone.utc).isoformat(), "type": type(error).__name__,
            "message":error_description(error,source),
        })
        raise
    for consumer in getattr(source, 'schedule_consumers', ()):
        result['linked_group_refreshed'] = await consumer.refresh_safely(force=True)
    profiles.set_metadata("last_refresh", result)
    profiles.set_metadata("last_refresh_error", None)
    return result


async def morning_refresh(context):
    source, profiles = context.job.data
    try:
        if getattr(source,'snapshot',None) is None:
            await refresh_saved_groups(source,profiles)
            return
        async with source.daily_refresh_lock:
            # A manual/full update may already be running. Recheck freshness after
            # it publishes, instead of immediately downloading everything again.
            await source.refresh_lock.acquire()
            source.refresh_lock.release()
            now = datetime.now(TIMEZONE)
            expected = now.date() if now.time() >= time(7) else now.date()-timedelta(days=1)
            cutoff = datetime.combine(expected,time(7),tzinfo=TIMEZONE)
            saved = source.snapshot.info().get('full_refresh_at')
            if saved and datetime.fromisoformat(saved).astimezone(TIMEZONE) >= cutoff:
                return
            if profiles.metadata('snapshot_attempt_day') == expected.isoformat():
                return
            profiles.set_metadata('snapshot_attempt_day',expected.isoformat())
            await refresh_saved_groups(source,profiles)
    except Exception as error:
        logging.getLogger(__name__).warning("Утреннее обновление не выполнено: %s",error_description(error,source))
        await report_refresh_failure(source,profiles,getattr(context,'bot',None),error)


async def refresh_course(source, profiles, faculty, course, *, period):
    result = await source.refresh_course(faculty, course, period=period)
    ids = set(result.pop('group_ids'))
    for consumer in getattr(source, 'schedule_consumers', ()):
        path = consumer.path
        if path and path[5] in ids:
            result['linked_group_refreshed'] = await consumer.refresh_safely(force=True)
    profiles.set_metadata('last_course_refresh', result)
    return result


def start_background(source, coroutine):
    task = asyncio.create_task(coroutine)
    tasks = getattr(source,'background_tasks',None)
    if tasks is None:
        tasks = source.background_tasks = set()
    tasks.add(task)
    def finished(done):
        tasks.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logging.getLogger(__name__).error('Фоновое обновление завершилось ошибкой (%s)',type(done.exception()).__name__)
    task.add_done_callback(finished)
    return task


async def stop_background(source):
    tasks = list(getattr(source,'background_tasks',()))
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)


async def launch_morning_refresh(context):
    source, _ = context.job.data
    start_background(source,morning_refresh(context))
