"""Daily on-disk website snapshot. User reads never use the network."""
import asyncio
from contextlib import contextmanager, closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import zlib

from .source import SourceError


def key(params):
    return json.dumps(sorted((k,str(v)) for k,v in params.items()),separators=(',',':'))


class DailySnapshot:
    def __init__(self, path, *, min_interval=0.25):
        self.path = Path(path)
        self.min_interval = min_interval
        self.next_request = 0
        self.request_lock = asyncio.Lock()
        self.last_error = False
        self.after_publish = None
        self.on_progress = None

    @property
    def available(self):
        return self.path.is_file()

    async def wait_for_request(self):
        async with self.request_lock:
            delay = self.next_request-time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_request = time.monotonic()+self.min_interval

    def get(self, params):
        if not self.available:
            raise SourceError('Ежедневная локальная копия ещё не загружена. Дождитесь фонового обновления.')
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                row = db.execute('SELECT payload FROM responses WHERE key=?',(key(params),)).fetchone()
            if not row:
                raise SourceError('Этих данных нет в ежедневной локальной копии. Обновление доступно владельцу бота.')
            data = json.loads(zlib.decompress(row[0]))
        except (sqlite3.Error,ValueError,zlib.error) as error:
            raise SourceError('Не удалось прочитать ежедневную локальную копию.') from None
        if self.last_error:
            data = copy.deepcopy(data)
            meta = data.get('meta')
            if not isinstance(meta,dict):
                meta = data['meta'] = {}
            meta['stale'] = True
            meta['warning'] = ' '.join(filter(None,(meta.get('warning'),'Ежедневное обновление не удалось. Используется предыдущая локальная копия.')))
        return data

    def info(self):
        if not self.available:
            return {}
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                row = db.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
            return json.loads(row[0]) if row else {}
        except (sqlite3.Error,ValueError):
            return {}

    def progress(self, phase, detail, **fields):
        if self.on_progress:
            self.on_progress(phase,detail,**fields)

    @contextmanager
    def stage(self, *, merge=False):
        self.progress('prepare','Готовлю временную базу; предыдущая копия остаётся доступной')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.daily-snapshot-',dir=self.path.parent)
        os.close(fd)
        db = sqlite3.connect(name)
        try:
            if merge:
                if not self.available:
                    raise SourceError('Сначала выполните полное обновление: локальная копия ещё не создана.')
                original = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
                try:
                    original.backup(db)
                finally:
                    original.close()
            db.executescript('CREATE TABLE IF NOT EXISTS responses(key TEXT PRIMARY KEY,payload BLOB NOT NULL); CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);')
            writer = SnapshotWriter(db)
            yield writer
            self.progress('save','Сохраняю скачанные данные в локальную базу')
            db.commit()
            self.progress('check','Проверяю целостность новой базы расписания')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise SourceError('Проверка локальной копии не пройдена.')
            db.close()
            with open(name,'rb') as saved:
                os.fsync(saved.fileno())
            self.progress('publish','Заменяю предыдущую базу проверенной новой копией')
            os.replace(name,self.path)
            if self.after_publish:
                self.progress('backup','Создаю резервную копию расписания',backup_state='running')
                result = self.after_publish(self.path)
                ok = result is not False
                self.progress('backup','Резервная копия создана' if ok else 'Не удалось создать резервную копию',backup_state='completed' if ok else 'failed')
        finally:
            db.close()
            Path(name).unlink(missing_ok=True)


class SnapshotWriter:
    def __init__(self, db):
        self.db = db

    def put(self, params, payload):
        encoded = zlib.compress(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode(),level=3)
        self.db.execute('INSERT INTO responses VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',(key(params),encoded))

    def summary(self, value):
        self.db.execute("INSERT INTO metadata VALUES ('summary',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(value,ensure_ascii=False),))
