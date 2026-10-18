import json

import httpx
import pytest

from bgmu_bot.website import WebsiteClient
from test_bgmu import api_response
from tools import fetch_schedule


@pytest.mark.asyncio
async def test_full_download_publishes_backup_and_verifies_all_groups_offline(tmp_path, monkeypatch):
    def respond(request):
        payload = api_response(request)
        params = dict(request.url.params)
        if params['action'] == 'catalog':
            payload['items'][2]['name'] = 'П-114'
        elif params['action'] == 'schedule' and params['id'] == '3':
            payload['entity']['name'] = 'П-114'
        return httpx.Response(200, json=payload)
    monkeypatch.setenv('BGMU_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(WebsiteClient, 'from_environment', classmethod(lambda cls: cls(transport=httpx.MockTransport(respond))))
    report = tmp_path / 'report.json'
    assert await fetch_schedule.fetch(report) == 0
    data = json.loads(report.read_text())
    assert data['status'] == 'passed'
    assert data['groups_checked'] == 3 and data['semester_events'] == 3
    assert data['linked_group_events'] == 1
    assert data['offline_reads_passed'] and data['backup_verified']
    assert (tmp_path / 'schedule.sqlite3').is_file()
    assert (tmp_path / 'backups/schedule.sqlite3').is_file()


@pytest.mark.asyncio
async def test_failed_download_preserves_previous_snapshot_and_reports_failure(tmp_path, monkeypatch):
    previous = b'previous snapshot must survive'
    (tmp_path / 'schedule.sqlite3').write_bytes(previous)
    monkeypatch.setenv('BGMU_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(WebsiteClient, 'from_environment', classmethod(lambda cls: cls(transport=httpx.MockTransport(lambda request: httpx.Response(503)))))
    report = tmp_path / 'report.json'
    assert await fetch_schedule.fetch(report) == 1
    assert json.loads(report.read_text())['status'] == 'failed'
    assert (tmp_path / 'schedule.sqlite3').read_bytes() == previous
    assert not list(tmp_path.glob('.daily-snapshot-*'))
