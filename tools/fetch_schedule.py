"""Download every configured group into the bot's persistent, verified snapshot."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import sqlite3
from pathlib import Path

import httpx

from bgmu_bot.backups import BackupStore
from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.shared_schedule import SharedSchedule
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient


async def fetch(report_path, *, reuse_recent=False):
    directory = Path(os.environ.get('BGMU_DATA_DIR', '.data'))
    report = {'status': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat()}
    try:
        backups = BackupStore(directory / 'backups')
        async with WebsiteClient.from_environment() as website:
            source = BGMUSource(website)
            source.use_snapshot(directory / 'schedule.sqlite3')
            source.snapshot.after_publish = backups.save_snapshot
            info = source.snapshot.info() if reuse_recent else {}
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(info.get('full_refresh_at', ''))).total_seconds()
                recent = 0 <= age <= 600
            except (ValueError, TypeError):
                recent = False
            if recent:
                report['snapshot'] = info
                report['reused_snapshot'] = True
                if not backups.save_snapshot(source.snapshot.path):
                    raise SourceError('Could not back up the restored schedule')
            else:
                report['snapshot'] = await source.refresh()
        # Prove that every catalog entry is readable from the published file,
        # with HTTP forbidden, including the management bot's shared П-114 feed.
        def forbid_http(request):
            raise AssertionError('Offline snapshot validation attempted HTTP')
        async with WebsiteClient(transport=httpx.MockTransport(forbid_http)) as website:
            offline = BGMUSource(website)
            offline.use_snapshot(directory / 'schedule.sqlite3')
            period = offline.snapshot.get({'action': 'period'})['period']
            year, semester = str(period['year']), str(period['semester'])
            items = await offline._catalog(('group', year, semester))
            events = 0
            for item in items:
                data = await offline._api(action='schedule', type='group', id=str(item['id']), year=year, semester=semester)
                offline._validate_schedule(data)
                events += len(data['events'])
            feed = SharedSchedule(offline)
            await feed.refresh()
            report.update(groups_checked=len(items), semester_events=events,
                          linked_group=feed.group, linked_group_events=len(feed.rows),
                          offline_reads_passed=True)
        if backups.last_error or not (backups.directory / 'schedule.sqlite3').is_file():
            raise SourceError('Schedule backup was not created')
        report.update(status='passed', snapshot_bytes=(directory / 'schedule.sqlite3').stat().st_size,
                      backup_verified=True)
    except (SourceError, OSError, ValueError, TimeoutError, sqlite3.Error) as error:
        report['error_type'] = type(error).__name__
        if isinstance(error, SourceError):
            report['message'] = str(error)
    finally:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', default='.runtime/full-schedule.json')
    parser.add_argument('--reuse-recent', action='store_true', help='Reuse a fully downloaded snapshot no older than 10 minutes; always verify all groups offline')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    return asyncio.run(fetch(args.report, reuse_recent=args.reuse_recent))


if __name__ == '__main__':
    raise SystemExit(main())
