"""Private user preferences. Timetable payloads are never written here."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from .backups import BackupStore
from .journal import JournalStore


@dataclass(frozen=True)
class SavedGroup:
    path: tuple[str, ...]
    labels: tuple[str, ...]


class ProfileStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backups = BackupStore(self.path.parent / 'backups')
        self.journals = JournalStore(self.path.parent / 'chat-journals')
        existed = self.path.exists()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY, path TEXT, labels TEXT,
                    active INTEGER NOT NULL DEFAULT 1, notifications INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS chat_deliveries (
                    chat_id INTEGER NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                    parts INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, fingerprint TEXT,
                    PRIMARY KEY(chat_id, kind, target)
                );
                CREATE TABLE IF NOT EXISTS access_blocks (
                    target_id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contacts (
                    user_id INTEGER PRIMARY KEY, username TEXT, name TEXT,
                    active INTEGER NOT NULL DEFAULT 1, announcements INTEGER NOT NULL DEFAULT 1,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, referral TEXT
                );
                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    username TEXT, name TEXT, active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(chat_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, body TEXT NOT NULL,
                    audience TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                    created_at TEXT NOT NULL, pause_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS broadcast_recipients (
                    job_id TEXT NOT NULL, recipient_id INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(job_id, recipient_id)
                );
                CREATE INDEX IF NOT EXISTS idx_broadcast_pending ON broadcast_recipients(job_id,state);
            """)
            if 'fingerprint' not in {row[1] for row in db.execute('PRAGMA table_info(chat_deliveries)')}:
                db.execute('ALTER TABLE chat_deliveries ADD COLUMN fingerprint TEXT')
            columns = {row[1] for row in db.execute('PRAGMA table_info(chats)')}
            for column, definition in {
                'title': 'TEXT', 'username': 'TEXT', 'bot_status': 'TEXT',
                'added_by': 'INTEGER', 'added_by_username': 'TEXT', 'added_at': 'TEXT',
                'updated_at': 'TEXT', 'announcements': 'INTEGER NOT NULL DEFAULT 1',
            }.items():
                if column not in columns:
                    db.execute(f'ALTER TABLE chats ADD COLUMN {column} {definition}')
            # Previously saved private groups imply a prior interaction with the bot.
            db.execute("""INSERT OR IGNORE INTO contacts(user_id,first_seen,last_seen)
                SELECT user_id,updated_at,updated_at FROM profiles""")
        if not existed:
            self.path.chmod(0o600)

    @classmethod
    def from_environment(cls):
        return cls(Path(os.environ.get("BGMU_DATA_DIR", ".data")) / "preferences.sqlite3")

    @contextmanager
    def connection(self, *, backup=False):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()
        if backup:
            self.backup_settings()

    def backup_settings(self):
        return self.backups.save_settings(self.path)

    def get(self, user_id):
        with self.connection() as db:
            row = db.execute("SELECT path, labels FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return SavedGroup(tuple(json.loads(row[0])), tuple(json.loads(row[1])))

    def save(self, user_id, path, labels):
        if len(path) not in (6, 7) or path[0] != "group" or len(labels) != len(path):
            raise ValueError("Only a complete personal group selection can be saved")
        with self.connection(backup=True) as db:
            db.execute("""
                INSERT INTO profiles(user_id, path, labels, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET path=excluded.path, labels=excluded.labels, updated_at=excluded.updated_at
            """, (user_id, json.dumps(path, ensure_ascii=False), json.dumps(labels, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))

    def delete(self, user_id):
        with self.connection(backup=True) as db:
            db.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))

    def paths(self):
        with self.connection() as db:
            rows = db.execute("SELECT path FROM profiles ORDER BY updated_at DESC").fetchall()
            rows += db.execute("SELECT path FROM chats WHERE active=1 AND path IS NOT NULL").fetchall()
        return tuple(tuple(json.loads(row[0])) for row in rows)

    def register_chat(self, chat_id):
        with self.connection(backup=True) as db:
            db.execute("INSERT INTO chats(chat_id) VALUES (?) ON CONFLICT(chat_id) DO UPDATE SET active=1", (chat_id,))

    def chat_group(self, chat_id):
        with self.connection() as db:
            row = db.execute("SELECT path, labels FROM chats WHERE chat_id=? AND path IS NOT NULL", (chat_id,)).fetchone()
        return SavedGroup(tuple(json.loads(row[0])), tuple(json.loads(row[1]))) if row else None

    def save_chat(self, chat_id, path, labels):
        if len(path) not in (6, 7) or path[0] != "group" or len(path) != len(labels):
            raise ValueError("Only a complete group can be assigned to a chat")
        with self.connection(backup=True) as db:
            db.execute("""INSERT INTO chats(chat_id,path,labels) VALUES (?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET path=excluded.path,labels=excluded.labels,active=1""",
                (chat_id, json.dumps(path, ensure_ascii=False), json.dumps(labels, ensure_ascii=False)))

    def chat_notifications(self, chat_id, enabled):
        with self.connection(backup=True) as db:
            db.execute("UPDATE chats SET notifications=? WHERE chat_id=?", (int(enabled), chat_id))

    def deactivate_chat(self, chat_id):
        with self.connection() as db:
            db.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))

    def subscribed_chats(self):
        with self.connection() as db:
            return tuple(row[0] for row in db.execute("SELECT chat_id FROM chats WHERE active=1 AND notifications=1 AND path IS NOT NULL AND chat_id NOT IN (SELECT target_id FROM access_blocks)"))

    def delivery(self, chat_id, kind, target):
        with self.connection() as db:
            row = db.execute("SELECT parts, completed FROM chat_deliveries WHERE chat_id=? AND kind=? AND target=?", (chat_id, kind, target)).fetchone()
        return row or (0, 0)

    def delivery_fingerprint(self, chat_id, kind, target):
        with self.connection() as db:
            row = db.execute('SELECT fingerprint FROM chat_deliveries WHERE chat_id=? AND kind=? AND target=?', (chat_id, kind, target)).fetchone()
        return row[0] if row else None

    def record_delivery(self, chat_id, kind, target, parts, completed=False, fingerprint=None):
        with self.connection() as db:
            db.execute("""INSERT INTO chat_deliveries VALUES (?,?,?,?,?,?)
                ON CONFLICT(chat_id,kind,target) DO UPDATE SET parts=excluded.parts,completed=excluded.completed,fingerprint=excluded.fingerprint""",
                (chat_id, kind, target, parts, int(completed), fingerprint))
            # Notification metadata only; no timetable bodies are stored.
            db.execute("DELETE FROM chat_deliveries WHERE target < date(?, '-60 days')", (target,))

    def migrate_chat(self, old_id, new_id):
        with self.connection(backup=True) as db:
            db.execute('INSERT OR IGNORE INTO chat_members SELECT ?,user_id,username,name,active FROM chat_members WHERE chat_id=?',(new_id,old_id))
            db.execute("INSERT OR IGNORE INTO access_blocks SELECT ?,actor_id,updated_at FROM access_blocks WHERE target_id=?", (new_id,old_id))
            db.execute("""INSERT INTO chats(chat_id,path,labels,active,notifications,title,username,bot_status,added_by,added_by_username,added_at,updated_at,announcements)
                SELECT ?,path,labels,active,notifications,title,username,bot_status,added_by,added_by_username,added_at,updated_at,announcements FROM chats WHERE chat_id=?
                ON CONFLICT(chat_id) DO NOTHING""", (new_id, old_id))
            db.execute("UPDATE chats SET active=0 WHERE chat_id=?", (old_id,))
            db.execute("""INSERT OR IGNORE INTO chat_deliveries SELECT ?,kind,target,parts,completed,fingerprint
                FROM chat_deliveries WHERE chat_id=?""", (new_id, old_id))

    def set_metadata(self, key, value):
        with self.connection(backup=True) as db:
            db.execute("INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False)))

    def metadata(self, key):
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def touch_contact(self, user_id, username=None, name=None, referral=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            new = db.execute('SELECT 1 FROM contacts WHERE user_id=?', (user_id,)).fetchone() is None
            db.execute('''INSERT INTO contacts(user_id,username,name,first_seen,last_seen,referral) VALUES (?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,name=excluded.name,
                last_seen=excluded.last_seen,active=1,referral=COALESCE(contacts.referral,excluded.referral)''',
                (user_id, username, name, now, now, referral))

        if new:
            self.backup_settings()
        return new

    def access_target(self, target_id):
        with self.connection() as db:
            if target_id > 0:
                row = db.execute('SELECT name,username FROM contacts WHERE user_id=?', (target_id,)).fetchone()
            else:
                row = db.execute('SELECT title,username FROM chats WHERE chat_id=?', (target_id,)).fetchone()
        return row

    def access_blocked(self, target_id):
        with self.connection() as db:
            return db.execute('SELECT 1 FROM access_blocks WHERE target_id=?', (target_id,)).fetchone() is not None

    def set_access_blocked(self, target_id, blocked, actor_id):
        with self.connection(backup=True) as db:
            if blocked:
                db.execute("INSERT INTO access_blocks VALUES (?,?,?) ON CONFLICT(target_id) DO UPDATE SET actor_id=excluded.actor_id,updated_at=excluded.updated_at",
                           (target_id,actor_id,datetime.now(timezone.utc).isoformat()))
            else:
                db.execute('DELETE FROM access_blocks WHERE target_id=?', (target_id,))

    def contact_subscription(self, user_id, enabled):
        with self.connection(backup=True) as db:
            db.execute('UPDATE contacts SET announcements=? WHERE user_id=?', (int(enabled), user_id))

    def contact_active(self, user_id, enabled):
        with self.connection(backup=True) as db:
            db.execute('UPDATE contacts SET active=? WHERE user_id=?', (int(enabled), user_id))

    def observe_chat(self, chat_id, *, title=None, username=None, status=None, added_by=None, added_by_username=None):
        now = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            new = db.execute('SELECT 1 FROM chats WHERE chat_id=?', (chat_id,)).fetchone() is None
            db.execute('''INSERT INTO chats(chat_id,title,username,bot_status,added_by,added_by_username,added_at,updated_at,active)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                title=COALESCE(excluded.title,chats.title),username=excluded.username,
                bot_status=COALESCE(excluded.bot_status,chats.bot_status),
                added_by=COALESCE(excluded.added_by,chats.added_by),
                added_by_username=CASE WHEN excluded.added_by IS NULL THEN chats.added_by_username ELSE excluded.added_by_username END,
                added_at=COALESCE(chats.added_at,excluded.added_at),updated_at=excluded.updated_at,
                active=CASE WHEN excluded.bot_status IS NULL OR excluded.bot_status = 'unavailable' THEN chats.active ELSE excluded.active END''',
                (chat_id, title, username, status, added_by, added_by_username, now, now, int(status not in ('left','kicked'))))

        if new or status is not None:
            self.backup_settings()
        return new

    def registered_contacts(self):
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM contacts ORDER BY active DESC,last_seen DESC,user_id")]

    def registered_chats(self):
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM chats ORDER BY active DESC,title,chat_id')]

    def observe_member(self, chat_id, user, *, active=True):
        if getattr(user,'is_bot',False):
            return
        with self.connection() as db:
            changed = db.execute("""INSERT INTO chat_members(chat_id,user_id,username,name,active) VALUES (?,?,?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET username=excluded.username,name=excluded.name,active=excluded.active
                WHERE chat_members.username IS NOT excluded.username OR chat_members.name IS NOT excluded.name
                OR chat_members.active != excluded.active""",
                (chat_id,user.id,getattr(user,'username',None),getattr(user,'full_name',None),int(active))).rowcount
        if changed:
            self.backup_settings()

    def chat_member_count(self, chat_id):
        with self.connection() as db:
            return db.execute('SELECT COUNT(*) FROM chat_members WHERE chat_id=?',(chat_id,)).fetchone()[0]

    def chat_members(self, chat_id, *, offset=0, limit=8):
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM chat_members WHERE chat_id=? ORDER BY active DESC,name,user_id LIMIT ? OFFSET ?',
                                                    (chat_id,limit,offset))]

    def group_announcements(self, chat_id, enabled):
        self.register_chat(chat_id)
        with self.connection(backup=True) as db:
            db.execute('UPDATE chats SET announcements=? WHERE chat_id=?', (int(enabled), chat_id))

    def announcement_targets(self, audience, selected=None):
        with self.connection() as db:
            if audience == 'users':
                rows = db.execute('SELECT user_id FROM contacts WHERE active=1 AND announcements=1 AND user_id NOT IN (SELECT target_id FROM access_blocks) ORDER BY user_id')
            else:
                rows = db.execute('SELECT chat_id FROM chats WHERE active=1 AND announcements=1 AND chat_id NOT IN (SELECT target_id FROM access_blocks) ORDER BY chat_id')
            ids = tuple(row[0] for row in rows)
        if audience == 'selected':
            selected = set(selected or ())
            return tuple(chat_id for chat_id in ids if chat_id in selected)
        return ids

    def announcement_allowed(self, recipient_id):
        if self.access_blocked(recipient_id):
            return False
        with self.connection() as db:
            if recipient_id > 0:
                row = db.execute('SELECT active,announcements FROM contacts WHERE user_id=?', (recipient_id,)).fetchone()
            else:
                row = db.execute('SELECT active,announcements FROM chats WHERE chat_id=?', (recipient_id,)).fetchone()
        return bool(row and row[0] and row[1])

    def audience_counts(self):
        with self.connection() as db:
            users = db.execute('SELECT COUNT(*) FROM contacts WHERE active=1').fetchone()[0]
            groups = db.execute('SELECT COUNT(*) FROM chats WHERE active=1').fetchone()[0]
        return {'users': users, 'groups': groups,
                'subscribed_users': len(self.announcement_targets('users')),
                'subscribed_groups': len(self.announcement_targets('groups'))}

    def queue_broadcast(self, owner_id, body, audience, recipients):
        import uuid
        job_id = uuid.uuid4().hex
        with self.connection() as db:
            db.execute('INSERT INTO broadcasts(id,owner_id,body,audience,created_at) VALUES (?,?,?,?,?)',
                       (job_id, owner_id, body, audience, datetime.now(timezone.utc).isoformat()))
            db.executemany('INSERT INTO broadcast_recipients(job_id,recipient_id) VALUES (?,?)',
                           ((job_id, recipient_id) for recipient_id in dict.fromkeys(recipients)))
        return job_id

    def recover_broadcasts(self):
        with self.connection() as db:
            # An interrupted request may already have reached Telegram; do not resend blindly.
            db.execute("UPDATE broadcast_recipients SET state='uncertain' WHERE state='sending'")

    def active_broadcast(self):
        with self.connection() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM broadcasts WHERE state IN ('queued','running') ORDER BY created_at,id LIMIT 1").fetchone()
        return dict(row) if row else None

    def claim_recipient(self, job_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute('SELECT state FROM broadcasts WHERE id=?', (job_id,)).fetchone()
            if not job or job[0] not in ('queued', 'running'):
                return None
            row = db.execute("SELECT recipient_id,attempts FROM broadcast_recipients WHERE job_id=? AND state='pending' ORDER BY recipient_id LIMIT 1", (job_id,)).fetchone()
            if row:
                db.execute("UPDATE broadcasts SET state='running' WHERE id=?", (job_id,))
                db.execute("UPDATE broadcast_recipients SET state='sending',attempts=attempts+1 WHERE job_id=? AND recipient_id=?", (job_id,row[0]))
            return row

    def finish_recipient(self, job_id, recipient_id, state):
        with self.connection() as db:
            db.execute('UPDATE broadcast_recipients SET state=? WHERE job_id=? AND recipient_id=?', (state,job_id,recipient_id))

    def pause_broadcast(self, job_id, until):
        with self.connection() as db:
            db.execute('UPDATE broadcasts SET pause_until=? WHERE id=?', (until,job_id))

    def finish_broadcast(self, job_id):
        with self.connection() as db:
            pending = db.execute("SELECT COUNT(*) FROM broadcast_recipients WHERE job_id=? AND state IN ('pending','sending')", (job_id,)).fetchone()[0]
            if not pending:
                db.execute("UPDATE broadcasts SET state='finished' WHERE id=? AND state IN ('running','queued')", (job_id,))

    def cancel_broadcast(self, job_id, owner_id):
        with self.connection() as db:
            row = db.execute('SELECT owner_id FROM broadcasts WHERE id=?', (job_id,)).fetchone()
            if not row or row[0] != owner_id:
                return False
            db.execute("UPDATE broadcasts SET state='cancelled' WHERE id=? AND state IN ('queued','running')", (job_id,))
            db.execute("UPDATE broadcast_recipients SET state='cancelled' WHERE job_id=? AND state='pending'", (job_id,))
        return True

    def broadcast_report(self, job_id, owner_id):
        with self.connection() as db:
            job = db.execute('SELECT state,audience FROM broadcasts WHERE id=? AND owner_id=?', (job_id,owner_id)).fetchone()
            if not job:
                return None
            counts = dict(db.execute('SELECT state,COUNT(*) FROM broadcast_recipients WHERE job_id=? GROUP BY state', (job_id,)).fetchall())
        return {'state': job[0], 'audience': job[1], 'counts': counts}

    def recent_broadcasts(self, owner_id, limit=10):
        with self.connection() as db:
            return tuple(row[0] for row in db.execute('SELECT id FROM broadcasts WHERE owner_id=? ORDER BY created_at DESC LIMIT ?', (owner_id,limit)))

    def redirect_broadcast(self, job_id, old_id, new_id):
        with self.connection() as db:
            exists = db.execute('SELECT 1 FROM broadcast_recipients WHERE job_id=? AND recipient_id=?', (job_id,new_id)).fetchone()
            if exists:
                db.execute("UPDATE broadcast_recipients SET state='migrated' WHERE job_id=? AND recipient_id=?", (job_id,old_id))
            else:
                db.execute("UPDATE broadcast_recipients SET recipient_id=?,state='pending' WHERE job_id=? AND recipient_id=?", (new_id,job_id,old_id))

    def clear_broadcast_history(self, owner_id):
        with self.connection() as db:
            db.execute("DELETE FROM broadcast_recipients WHERE job_id IN (SELECT id FROM broadcasts WHERE owner_id=? AND state IN ('finished','cancelled'))", (owner_id,))
            cursor = db.execute("DELETE FROM broadcasts WHERE owner_id=? AND state IN ('finished','cancelled')", (owner_id,))
            return cursor.rowcount
