import asyncio
from datetime import date, datetime
import importlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.refresh import refresh_saved_groups
from bgmu_bot.shared_schedule import SharedSchedule, SharedScheduleMiddleware
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response, event


def respond(request, *, subject='Педиатрия', fail=False, duplicate=False):
    params = dict(request.url.params)
    if fail and params.get('action') == 'schedule' and params.get('id') == '114':
        return httpx.Response(503)
    data = api_response(request)
    if params['action'] == 'catalog' and params['type'] == 'group':
        data['items'].append({'id': 114, 'name': 'П-114', 'faculty': 'Педиатрический факультет', 'course': 1})
    if params['action'] == 'schedule' and params['id'] == '114':
        lesson = event(subject)
        lesson['id'] = 'p114-event-1'
        lesson['details'][0].update(groupId=114, group='П-114')
        lesson['groups'] = [{'id': 114, 'name': 'П-114'}]
        data['entity']['name'] = 'П-114'
        data['events'] = [lesson, lesson] if duplicate else [lesson]
    return httpx.Response(200, json=data)


@pytest.fixture
def original(tmp_path, monkeypatch):
    monkeypatch.setenv('RESTRICTED_BOT_TOKEN', '8591550954:offline-test')
    monkeypatch.setenv('DATA_DIR', str(tmp_path / 'original'))
    monkeypatch.setenv('BGMU_DATA_DIR', str(tmp_path / 'preferences'))
    module = importlib.import_module('restricted_bot.bot')
    (tmp_path / 'original').mkdir(exist_ok=True)
    (tmp_path / 'original' / 'backups').mkdir(exist_ok=True)
    monkeypatch.setattr(module, 'DB_PATH', str(tmp_path / 'original' / 'bot_data.db'))
    monkeypatch.setattr(module, 'BACKUP_DIR', str(tmp_path / 'original' / 'backups'))
    monkeypatch.setattr(module, 'SCHEDULE_FEED', None)
    module.init_db()
    return module


@pytest.mark.asyncio
async def test_shared_response_is_reused_and_old_bot_reads_memory_not_manual_table(original):
    calls = []
    def transport(request):
        calls.append(dict(request.url.params))
        return respond(request)
    async with WebsiteClient(transport=httpx.MockTransport(transport)) as website:
        source = BGMUSource(website, cache_ttl=300)
        match, = await source.search_groups('П-114')
        # Public bot fetches first, and the private bot consumes the same response.
        public = await source.schedule(match.path, date(2026, 10, 21))
        feed = SharedSchedule(source)
        await feed.refresh()
        assert feed.group == 'П-114' and feed.ready
        assert sum(call['action'] == 'schedule' for call in calls) == 1
        with original.db_session() as db:
            db.execute("INSERT INTO schedule(date,time,subject,room) VALUES ('2026-10-21','10:00-11:00','Старое ручное','старое')")
            db.execute("INSERT INTO homework(subject,task,deadline) VALUES ('Предмет','Задача','2026-10-21')")
        original.SCHEDULE_FEED = feed
        classes = original.get_classes_for_date(date(2026, 10, 21))
        assert len(classes) == len(public.sections) == 1
        assert classes[0]['subject'].startswith(public.sections[0].entries[0])
        assert 'Преподаватель' in classes[0]['room'] and 'подгр. А' in classes[0]['room']
        rows = original.schedule_rows_between(date(2026, 10, 19), date(2026, 10, 25))
        assert len(rows) == 1 and rows[0]['subject'].startswith('Педиатрия')
        assert 'Педиатрия' in original.build_digest(date(2026, 10, 21), 'Доброе утро')
        assert 'Педиатрия' in original.schedule_csv_bytes(rows).decode('utf-8-sig')
        with original.db_session() as db:
            assert db.execute('SELECT task FROM homework').fetchone()[0] == 'Задача'
            with pytest.raises(sqlite3.IntegrityError, match='read-only'):
                db.execute('DELETE FROM schedule')
        # Disk and backups still contain only previous manual records, never site rows.
        with sqlite3.connect(original.DB_PATH) as disk:
            assert disk.execute('SELECT subject FROM schedule').fetchall() == [('Старое ручное',)]
        backup = original.backup_database('shared_test')
        with sqlite3.connect(original.BACKUP_DIR + '/' + backup) as disk:
            assert disk.execute('SELECT subject FROM schedule').fetchall() == [('Старое ручное',)]
        # Migrations/restoring metadata must operate on main, not the read-only TEMP table.
        original.init_db()
        assert original.get_classes_for_date(date(2026, 10, 21))[0]['subject'].startswith('Педиатрия')
        text, markup = original.render_date(date(2026, 10, 21))
        assert 'Педиатрия' in text
        assert not any(button.callback_data.startswith(('cls_del_', 'add_class_', 'sch_edit_', 'sch_copy')) for row in markup.inline_keyboard for button in row)


@pytest.mark.asyncio
async def test_general_admin_refresh_updates_private_bot_and_failure_keeps_snapshot(tmp_path):
    options = {'subject': 'Старый предмет', 'fail': False}
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: respond(r, **options))) as website:
        source = BGMUSource(website, cache_ttl=300)
        feed = SharedSchedule(source)
        source.schedule_consumers = [feed]
        await feed.refresh()
        profiles = ProfileStore(tmp_path / 'prefs.sqlite3')
        options['subject'] = 'Новый предмет'
        result = await refresh_saved_groups(source, profiles)
        assert result['linked_group_refreshed'] is True
        assert feed.rows[0][3].startswith('Новый предмет')
        snapshot = feed.rows
        options['fail'] = True
        result = await refresh_saved_groups(source, profiles)
        assert result['linked_group_refreshed'] is False
        assert feed.rows == snapshot and feed.ready
        assert 'предыдущая копия' in feed.warning


@pytest.mark.asyncio
async def test_repeated_site_ids_have_unique_rows_and_wrong_group_keeps_snapshot():
    duplicate = [False]
    async with WebsiteClient(transport=httpx.MockTransport(lambda r: respond(r, duplicate=duplicate[0]))) as website:
        source = BGMUSource(website, cache_ttl=300)
        feed = SharedSchedule(source)
        await feed.refresh()
        duplicate[0] = True
        source.cache.replace({})
        await feed.refresh(force=True)
        assert len(feed.rows) == 2 and feed.rows[0][0] != feed.rows[1][0]
        previous = feed.rows
        original = source._schedule_data
        async def wrong_group(path):
            data = await original(path)
            return dict(data, entity={'name': 'Л-101'})
        source._schedule_data = wrong_group
        with pytest.raises(SourceError, match='другой группы'):
            await feed.refresh(force=True)
        assert feed.rows == previous


@pytest.mark.asyncio
async def test_private_morning_digest_and_class_reminders_use_website(original, monkeypatch):
    async with WebsiteClient(transport=httpx.MockTransport(respond)) as website:
        feed = SharedSchedule(BGMUSource(website, cache_ttl=300))
        await feed.refresh()
        original.SCHEDULE_FEED = feed
        monkeypatch.setattr(original, 'get_now', lambda: datetime(2026, 10, 21, 7, 30, tzinfo=original.TIMEZONE))
        sent = AsyncMock(return_value=SimpleNamespace(message_id=55))
        monkeypatch.setattr(original, 'safe_send', sent)
        await original.send_morning_digest()
        assert sent.call_args.args[0] == original.GROUP_CHAT_ID
        assert 'Педиатрия' in sent.call_args.args[1]
        await original.send_morning_digest()
        assert sent.await_count == 1  # Existing notification dedupe still applies.
        monkeypatch.setattr(original, 'get_now', lambda: datetime(2026, 10, 21, 8, 50, tzinfo=original.TIMEZONE))
        await original.check_class_reminders()
        assert 'Педиатрия' in sent.call_args.args[1] and '10 мин.' in sent.call_args.args[1]


@pytest.mark.asyncio
async def test_unavailable_feed_does_not_report_empty_schedule_or_disable_moderation():
    feed = SimpleNamespace(ready=False, refresh_safely=AsyncMock(return_value=False))
    middleware = SharedScheduleMiddleware(lambda: feed)
    handler = AsyncMock()
    schedule = SimpleNamespace(text='сегодня', answer=AsyncMock())
    await middleware(handler, schedule, {})
    handler.assert_not_awaited()
    assert 'недоступно' in schedule.answer.call_args.args[0]
    moderation = SimpleNamespace(text='мут 20m', answer=AsyncMock())
    await middleware(handler, moderation, {})
    assert handler.await_count == 1
    assert feed.refresh_safely.await_count == 1  # No website wait for moderation.
    edit = SimpleNamespace(data='sch_copy_week_2026-10-21', answer=AsyncMock())
    await middleware(handler, edit, {})
    assert handler.await_count == 1
    assert 'Ручное изменение отключено' in edit.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_joint_runner_passes_the_same_source_and_stops_both_on_failure(original, monkeypatch):
    from bgmu_bot import runner
    monkeypatch.setenv('RESTRICTED_BOT_TOKEN', '8591550954:offline-test')
    monkeypatch.setattr(runner.WebsiteClient, 'from_environment', lambda: WebsiteClient(transport=httpx.MockTransport(respond)))
    captured = {}
    stopped = asyncio.Event()
    def create(token, source, **kwargs):
        captured['source'] = source
        return 'public_app'
    async def public(app):
        await asyncio.sleep(0)
        raise RuntimeError('test_public_failure')
    async def private(feed, **kwargs):
        captured['feed'] = feed
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(runner, 'create_application', create)
    monkeypatch.setattr(runner, 'public_polling', public)
    monkeypatch.setattr(original, 'run_restricted', private)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'add_signal_handler', lambda *args: None)
    monkeypatch.setattr(loop, 'remove_signal_handler', lambda *args: None)
    with pytest.raises(RuntimeError, match='test_public_failure'):
        await runner.run_together('12345:offline-test')
    assert captured['feed'].source is captured['source']
    assert captured['feed'].ready and stopped.is_set()
