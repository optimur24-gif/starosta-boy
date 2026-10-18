"""One private, atomically replaced snapshot per backup type."""

from datetime import datetime, timezone
import gzip
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile


class BackupStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.last_error = None

    def _replace(self, name, write):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            fd, temporary = tempfile.mkstemp(prefix='.'+name+'-', dir=self.directory)
            os.close(fd)  # mkstemp creates a private 0600 file.
            write(Path(temporary))
            with open(temporary, 'rb') as saved:
                os.fsync(saved.fileno())
            os.replace(temporary, self.directory/name)
            temporary = None
            self.last_error = None
            return True
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            self.last_error = type(error).__name__
            logging.getLogger(__name__).warning('Резервная копия не обновлена (%s)', self.last_error)
            return False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def save_settings(self, source):
        return self.save_sqlite(source,'settings.sqlite3',strip_outbox=True)

    def save_snapshot(self, source):
        success = self.save_sqlite(source,'schedule.sqlite3')
        if success:
            try:
                (self.directory/'schedule.json.gz').unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning('Не удалось удалить прежний формат копии расписания')
        return success

    def save_sqlite(self, source, filename, *, strip_outbox=False):
        def write(path):
            original = sqlite3.connect(source, timeout=5)
            copy = sqlite3.connect(path)
            try:
                original.backup(copy)
                copy.execute('PRAGMA journal_mode=DELETE')
                # Restoring preferences must never replay an old announcement queue.
                tables = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                with copy:
                    for table in (('broadcast_recipients','broadcasts') if strip_outbox else ()):
                        if table in tables:
                            copy.execute('DELETE FROM '+table)
                if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Invalid SQLite backup')
            finally:
                copy.close()
                original.close()
        return self._replace(filename,write)

    def save_schedule(self, entries):
        # Catalog-only startup and failed source requests must not erase a timetable.
        if not any(dict(key).get('action') == 'schedule' for key in entries):
            return False
        snapshot = {'version': 1, 'saved_at': datetime.now(timezone.utc).isoformat(),
                    'entries': [{'params': dict(key), 'payload': entry.value} for key, entry in entries.items()]}
        def write(path):
            with gzip.open(path, 'wt', encoding='utf-8') as saved:
                json.dump(snapshot, saved, ensure_ascii=False, separators=(',', ':'))
        return self._replace('schedule.json.gz', write)
