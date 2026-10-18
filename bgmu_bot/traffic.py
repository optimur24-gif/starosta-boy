"""Bounded request cooldown; ordinary group conversation is not processed."""

from collections import OrderedDict
import time

from telegram.ext import ApplicationHandlerStop

from .commands import schedule_request
from .presentation import today


class Cooldown:
    def __init__(self, seconds=2, max_users=10000, clock=time.monotonic):
        self.seconds, self.max_users, self.clock = seconds, max_users, clock
        self.users = OrderedDict()

    def allow(self, user_id):
        now = self.clock()
        previous = self.users.get(user_id)
        if previous is not None and now-previous < self.seconds:
            return False
        self.users[user_id] = now
        self.users.move_to_end(user_id)
        while len(self.users) > self.max_users:
            self.users.popitem(last=False)
        return True


def relevant_text(text):
    if not text:
        return False
    if text.startswith('/'):
        return True
    try:
        return schedule_request(text, today()) is not None
    except ValueError:
        return True


class TrafficGuard:
    def __init__(self, is_owner, limiter=None):
        self.is_owner = is_owner
        self.limiter = limiter or Cooldown()

    async def handle(self, update, context):
        user, chat = update.effective_user, update.effective_chat
        if not user or getattr(user, 'is_bot', False) or self.is_owner(update):
            return
        message = update.effective_message
        if not update.callback_query and (not message or getattr(message, 'new_chat_members', None) or getattr(message, 'migrate_to_chat_id', None) or getattr(message, 'migrate_from_chat_id', None)):
            return
        if not update.callback_query and not (chat and chat.type == 'private') and not relevant_text(getattr(message, 'text', None)):
            return
        if self.limiter.allow(user.id):
            return
        if update.callback_query:
            await update.callback_query.answer('Подожди 2 секунды между запросами.')
        raise ApplicationHandlerStop


class AccessGuard:
    """Persistent owner bans; account and group bans are independent."""

    def __init__(self, profiles, is_owner):
        self.profiles, self.is_owner = profiles, is_owner
        self.notices = Cooldown()

    async def handle(self, update, context):
        chat, user = update.effective_chat, update.effective_user
        if not chat or getattr(update,'my_chat_member',None):
            return
        message = update.effective_message
        if message and (getattr(message,'migrate_to_chat_id',None) or getattr(message,'migrate_from_chat_id',None)):
            return
        if chat.type == 'private' and self.is_owner(update):
            return
        if not (self.profiles.access_blocked(chat.id) or (user and self.profiles.access_blocked(user.id))):
            return
        text = '⛔ Доступ к боту отключён владельцем.'
        if update.callback_query:
            await update.callback_query.answer(text,show_alert=True)
        elif message and (chat.type == 'private' or relevant_text(getattr(message,'text',None))):
            if self.notices.allow((chat.id,user.id if user else 0)):
                await message.reply_text(text)
        raise ApplicationHandlerStop
