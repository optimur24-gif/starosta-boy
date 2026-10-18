"""Disclosed group journals: no recording before a visible chat notice."""

from contextlib import closing
import asyncio
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import time

from telegram.error import TelegramError

from .presentation import TIMEZONE


def single_line(value):
    return str(value).replace('\\','\\\\').replace('\r','\\r').replace('\n','\\n').replace('\x00','')


def message_line(message, *, edited=False):
    sender = getattr(message,'sender_chat',None) or getattr(message,'from_user',None)
    if not sender:
        author = 'Автор не указан'
    else:
        tag = getattr(sender,'username',None)
        name = getattr(sender,'full_name',None) or getattr(sender,'title',None) or 'Без имени'
        author = ('@'+tag if tag else str(name)) + f' (ID {sender.id})'
    content = getattr(message,'text',None)
    if content is None:
        markers = (('photo','Фото'),('video','Видео'),('animation','Анимация'),('document','Документ'),
                   ('voice','Голосовое'),('audio','Аудио'),('video_note','Видеосообщение'),
                   ('sticker','Стикер'),('contact','Контакт'),('location','Геопозиция'),
                   ('venue','Место'),('poll','Опрос'),('dice','Кубик'))
        label = next((label for field,label in markers if getattr(message,field,None)),None)
        if label is None:
            return None
        content = '['+label+']'
        if getattr(message,'caption',None):content += ' '+message.caption
    when = getattr(message,'edit_date',None) if edited else None
    when = when or message.date
    stamp = when.astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')
    kind = ' · правка' if edited else ''
    return f'{stamp}, Уфа · #{message.message_id}{kind} · {single_line(author)}: {single_line(content)}\n'


class JournalStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory/'state.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY, notice_id INTEGER NOT NULL DEFAULT 0,
                    signature TEXT, present INTEGER NOT NULL DEFAULT 1,
                    clear_through INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS entries (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    PRIMARY KEY(chat_id,message_id,fingerprint)
                );
            ''')
        self.path.chmod(0o600)

    def log_path(self, chat_id):
        if not isinstance(chat_id,int) or chat_id >= 0:raise ValueError('Group chat required')
        return self.directory/f'chat_{chat_id}.txt'

    def state(self, chat_id):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM chats WHERE chat_id=?',(chat_id,)).fetchone()
        return dict(row) if row else None

    def notified(self, chat_id, message_id, signature):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''INSERT INTO chats(chat_id,notice_id,signature) VALUES (?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET notice_id=excluded.notice_id,
                signature=excluded.signature,present=1''',(chat_id,message_id,signature))

    def removed(self, chat_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''INSERT INTO chats(chat_id,present) VALUES (?,0)
                ON CONFLICT(chat_id) DO UPDATE SET present=0''',(chat_id,))

    def append(self, chat_id, message, line):
        signature = hashlib.sha256(line.encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT notice_id,present,clear_through FROM chats WHERE chat_id=?',(chat_id,)).fetchone()
            if not state or not state[1] or message.message_id <= max(state[0],state[2]):return False
            if db.execute('SELECT 1 FROM entries WHERE chat_id=? AND message_id=? AND fingerprint=?',
                          (chat_id,message.message_id,signature)).fetchone():return False
            # Only numeric chat IDs enter paths. One UTF-8 line; newlines are escaped.
            fd = os.open(self.log_path(chat_id),os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
            with os.fdopen(fd,'a',encoding='utf-8') as saved:
                saved.write(line)
                saved.flush()
            db.execute('INSERT INTO entries VALUES (?,?,?)',(chat_id,message.message_id,signature))
        return True

    def info(self, chat_id):
        path = self.log_path(chat_id)
        with closing(sqlite3.connect(self.path)) as db:
            count = db.execute('SELECT COUNT(*) FROM entries WHERE chat_id=?',(chat_id,)).fetchone()[0]
        return count,path.stat().st_size if path.exists() else 0

    def clear(self, chat_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            last = db.execute('SELECT MAX(message_id) FROM entries WHERE chat_id=?',(chat_id,)).fetchone()[0] or 0
            db.execute('UPDATE chats SET clear_through=MAX(clear_through,?) WHERE chat_id=?',(last,chat_id))
            # Keep the visible notice and recording state. Clearing does not turn logging off.
            fd = os.open(self.log_path(chat_id),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            os.close(fd)
            db.execute('DELETE FROM entries WHERE chat_id=?',(chat_id,))


class JournalObserver:
    """Write messages only when the runtime supplies a disclosure receipt."""
    def __init__(self, profiles, store, owner_username=None):
        self.profiles,self.store = profiles,store

    async def handle(self, update, context, *, disclosed=False):
        if disclosed is not True:return
        chat = update.effective_chat
        if not chat or chat.type not in ('group','supergroup'):return
        message = getattr(update,'message',None) or getattr(update,'edited_message',None)
        if not message:return
        user = getattr(message,'from_user',None)
        if user and user.is_bot and not getattr(message,'sender_chat',None):return
        try:
            line = message_line(message,edited=bool(getattr(update,'edited_message',None)))
            if line:self.store.append(chat.id,message,line)
        except (OSError,sqlite3.Error) as error:
            logging.getLogger(__name__).warning('Запись журнала чата %s не выполнена (%s)',chat.id,type(error).__name__)


def _notice_text(owner):
    return 'приятного испоьзования'


class JournalNotices:
    def __init__(self, profiles, store, owner_username):
        self.profiles, self.store = profiles, store
        self.notice = _notice_text(owner_username)
        if not isinstance(self.notice, str) or not self.notice.strip():
            raise ValueError('Empty journal disclosure')
        self.signature = hashlib.sha256(self.notice.encode()).hexdigest()
        self.locks = [asyncio.Lock() for _ in range(32)]
        self.retry_after = {}

    def lock(self, chat_id):
        return self.locks[abs(chat_id) % len(self.locks)]

    async def ensure_notice(self, bot, chat_id, *, rejoined=False):
        state = self.store.state(chat_id)
        if not rejoined and state:
            if not state['present']: return False
            if state['notice_id'] and state['signature'] == self.signature: return True
        if time.monotonic() < self.retry_after.get(chat_id, 0): return False
        try:
            sent = await bot.send_message(chat_id, self.notice, parse_mode=None)
            self.store.notified(chat_id, sent.message_id, self.signature)
        except (TelegramError, OSError, sqlite3.Error) as error:
            if len(self.retry_after) >= 10000:
                self.retry_after.pop(next(iter(self.retry_after)))
            self.retry_after[chat_id] = time.monotonic() + 60
            logging.getLogger(__name__).warning(
                'Журнал чата %s не запущен: предупреждение не доставлено (%s)',
                chat_id, type(error).__name__,
            )
            return False
        self.retry_after.pop(chat_id, None)
        return True

    async def handle(self, update, context):
        chat = update.effective_chat
        if not chat or chat.type not in ('group','supergroup'):return False
        async with self.lock(chat.id):
            return await self._handle(update,context,chat)

    async def _handle(self, update, context, chat):
        event = getattr(update,'my_chat_member',None)
        if event:
            new,old = event.new_chat_member,event.old_chat_member
            present = new.status in ('member','administrator') or (new.status=='restricted' and getattr(new,'is_member',False))
            if not present:
                self.store.removed(chat.id)
                self.retry_after.pop(chat.id,None)
                return False
            state = self.store.state(chat.id)
            rejoined = old.status in ('left','kicked') or bool(state and not state['present'])
            return await self.ensure_notice(context.bot,chat.id,rejoined=rejoined)
        message = getattr(update,'message',None) or getattr(update,'edited_message',None)
        if not message:return False
        if getattr(message,'migrate_to_chat_id',None):
            self.store.removed(chat.id)
            return False
        state = self.store.state(chat.id)
        already_disclosed = bool(state and state['present'] and state['notice_id'] and state['signature']==self.signature)
        if not await self.ensure_notice(context.bot,chat.id):return False
        joined = getattr(message,'new_chat_members',None) or ()
        if already_disclosed and any(not member.is_bot for member in joined):
            try:await context.bot.send_message(chat.id,self.notice,parse_mode=None)
            except TelegramError:pass
        return True

    async def announce_existing(self, context):
        # Existing installations must also disclose before their first recorded message.
        for chat in self.profiles.registered_chats():
            if not chat['active'] or chat['bot_status'] in ('left','kicked'):continue
            async with self.lock(chat['chat_id']):
                try:await self.ensure_notice(context.bot,chat['chat_id'])
                except (OSError,sqlite3.Error):
                    logging.getLogger(__name__).warning('Не удалось подготовить журнал чата %s',chat['chat_id'])


class JournalRuntime:
    def __init__(self, profiles, store, owner_username):
        self.profiles = profiles
        self.writer = JournalObserver(profiles,store,owner_username)
        self.notices = None
        self.last_error = object()
        try:
            self.notices = JournalNotices(profiles,store,owner_username)
        except Exception as error:
            self.report_error(error)
        else:
            self.report_error(None)

    def report_error(self, error):
        kind = type(error).__name__ if error is not None else None
        if kind == self.last_error:return
        self.last_error = kind
        self.profiles.set_metadata('journal_notice_error',kind)
        if kind:
            logging.getLogger(__name__).warning('Модуль предупреждения недоступен (%s). Запись чатов заблокирована; расписание продолжает работать.',kind)

    async def handle(self, update, context):
        if self.notices is None:return
        try:
            disclosed = await self.notices.handle(update,context)
        except Exception as error:
            self.report_error(error)
            return
        if disclosed is True:
            self.report_error(None)
            await self.writer.handle(update,context,disclosed=True)

    async def announce_existing(self, context):
        if self.notices is None:return
        try:
            await self.notices.announce_existing(context)
        except Exception as error:
            self.report_error(error)
