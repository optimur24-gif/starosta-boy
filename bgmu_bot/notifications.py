"""Durable delivery markers, without persisting schedule contents."""

import asyncio
from datetime import datetime, timedelta
import hashlib
import logging

from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError

from .presentation import TIMEZONE, render_schedule
from .source import GroupUnavailable, SourceError


async def send_part(bot, chat_id, text, *, allowed=None):
    if allowed is not None and not allowed():
        return False
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except RetryAfter as error:
        delay = error.retry_after.total_seconds() if isinstance(error.retry_after, timedelta) else error.retry_after
        if delay > 30:
            raise
        await asyncio.sleep(delay + 0.1)
        if allowed is not None and not allowed():
            return False
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    return True


async def broadcast(source, profiles, bot, kind, target):
    for chat_id in profiles.subscribed_chats():
        parts, completed = profiles.delivery(chat_id, kind, target.isoformat())
        if completed:
            continue
        saved = profiles.chat_group(chat_id)
        try:
            match = await source.resolve_group(saved.path)
            if match.path != saved.path or match.labels != saved.labels:
                profiles.save_chat(chat_id, match.path, match.labels)
            schedule = await source.schedule(match.path, target)
            messages = render_schedule(schedule)
            fingerprint = hashlib.sha256('\n'.join(messages).encode()).hexdigest()
            if parts and profiles.delivery_fingerprint(chat_id, kind, target.isoformat()) != fingerprint:
                parts = 0  # A changed response must be delivered from the beginning.
            for index, message in enumerate(messages):
                if index < parts:
                    continue
                if not await send_part(bot, chat_id, message, allowed=lambda: not profiles.access_blocked(chat_id)):
                    break
                profiles.record_delivery(chat_id, kind, target.isoformat(), index+1, fingerprint=fingerprint)
            else:
                profiles.record_delivery(chat_id, kind, target.isoformat(), len(messages), completed=True, fingerprint=fingerprint)
        except Forbidden:
            profiles.deactivate_chat(chat_id)
        except GroupUnavailable:
            if profiles.access_blocked(chat_id):
                continue
            try:
                await send_part(bot, chat_id, '⚠️ Группа отсутствует в текущем учебном периоде. Администратору чата нужно выбрать её заново: /group, затем /notifications_on.', allowed=lambda: not profiles.access_blocked(chat_id))
                profiles.chat_notifications(chat_id, False)
            except Forbidden:
                profiles.deactivate_chat(chat_id)
            except TelegramError:
                pass
        except (SourceError, TelegramError) as error:
            logging.getLogger(__name__).warning('Рассылка отложена (%s)', type(error).__name__)


async def scheduled_notifications(context):
    now = datetime.now(TIMEZONE)
    minutes = now.hour*60 + now.minute
    if 7*60+30 <= minutes < 8*60:
        kind, target = 'morning', now.date()
    elif 20*60 <= minutes < 20*60+30:
        kind, target = 'evening', now.date()+timedelta(days=1)
    else:
        return
    source, profiles = context.job.data
    await broadcast(source, profiles, context.bot, kind, target)
