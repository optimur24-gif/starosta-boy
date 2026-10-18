"""Test the complete daily-write path on distributed live samples, no Telegram."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

import httpx

from bgmu_bot.backups import BackupStore
from bgmu_bot.bgmu import BGMUSource, TYPES
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient


class SampleWebsite(WebsiteClient):
    def __init__(self):
        super().__init__()
        self.requests = 0
        self.last_request = {}
        self.catalog_sizes = {}
        self.last_response = {}
        self.offline = False

    async def get(self, path='/', *, params=None):
        if self.offline:
            raise AssertionError('A user read attempted a website request')
        self.requests += 1
        self.last_request = dict(params or {})
        response = await super().get(path,params=params)
        data = response.json()
        self.last_response = {'status':response.status_code,'bytes':len(response.content),
                              'success':data.get('success'),'events_type':type(data.get('events')).__name__}
        if params.get('action') == 'catalog' and data.get('success'):
            items = BGMUSource._catalog_items(data)
            self.catalog_sizes[params['type']] = len(items)
            indexes = {0,1,2,len(items)//3,len(items)//2,2*len(items)//3,len(items)-3,len(items)-2,len(items)-1}
            sampled = [item for i,item in enumerate(items) if i in indexes or item.get('name') in ('П-114','Л-101')]
            data = {**data,'items':sampled}
            response = httpx.Response(200,json=data,request=response.request)
        return response


async def main():
    report={'status':'failed','run_id':os.environ.get('GITHUB_RUN_ID'),'sampled':True,
            'telegram_bots_started':False,'checked_at':datetime.now(timezone.utc).isoformat()}
    async with SampleWebsite() as website:
        try:
            with tempfile.TemporaryDirectory() as folder:
                source=BGMUSource(website)
                source.use_snapshot(Path(folder)/'schedule.sqlite3')
                backups=BackupStore(Path(folder)/'backups')
                source.snapshot.after_publish=backups.save_snapshot
                report['snapshot'] = await source.refresh()
                assert (backups.directory/'schedule.sqlite3').exists()
                report['snapshot_bytes']=source.snapshot.path.stat().st_size
                website.offline=True
                period=source.snapshot.get({'action':'period'})['period']
                for kind in TYPES:
                    items=await source._catalog((kind,str(period['year']),str(period['semester'])))
                    for item in items:
                        payload=await source._api(action='schedule',type=kind,id=str(item['id']),year=str(period['year']),semester=str(period['semester']))
                        source._validate_schedule(payload)
                report['offline_reads_passed']=True
                report['status']='passed'
        except Exception as error:
            report['error_type']=type(error).__name__
            if isinstance(error,SourceError):report['error']=str(error)
        report.update(website_requests=website.requests,last_request=website.last_request,
                      last_response=website.last_response,full_catalog_counts=website.catalog_sizes)
    path=Path('verification/daily-diagnostic.json');path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
