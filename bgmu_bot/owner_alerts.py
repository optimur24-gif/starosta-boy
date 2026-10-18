"""Private owner notifications, bound to a verified Telegram account."""
from datetime import datetime
import logging
import uuid
import asyncio

from telegram.error import TelegramError

from .presentation import TIMEZONE
from .errors import error_description


async def notify_owner(profiles, bot, text, *, kind, expected_username=None):
    if bot is None or profiles.metadata('owner_alerts_'+kind) is False:
        return False
    owner = profiles.metadata('owner_alert_recipient')
    if not owner:
        return False
    expected = expected_username or owner['username']
    try:
        chat = await bot.get_chat(owner['id'])
        if chat.type != 'private' or (chat.username or '').casefold() != expected.casefold():
            return False
        await bot.send_message(owner['id'],text,parse_mode=None)
        return True
    except TelegramError:
        return False


async def deliver_refresh_alert(profiles, bot, *, expected_username=None):
    lock = getattr(profiles,'refresh_alert_lock',None)
    if lock is None:
        lock = profiles.refresh_alert_lock = asyncio.Lock()
    async with lock:
        pending = profiles.metadata('pending_refresh_alert')
        if not pending:
            return False
        delivered = await notify_owner(profiles,bot,pending['text'],kind='refresh_errors',expected_username=expected_username)
        if delivered:
            # Do not clear a newer failure that arrived while Telegram responded.
            current = profiles.metadata('pending_refresh_alert')
            if current and current.get('id') == pending['id']:
                profiles.set_metadata('pending_refresh_alert',None)
        return delivered


async def report_refresh_failure(source, profiles, bot, error):
    now = datetime.now(TIMEZONE)
    snapshot = getattr(source,'snapshot',None)
    summary = snapshot.info() if snapshot is not None else {}
    previous = summary.get('full_refresh_at')
    if previous:
        previous = datetime.fromisoformat(previous).astimezone(TIMEZONE).strftime('%d.%m.%Y %H:%M')
    reason = error_description(error,source)
    text = (f'⚠️ Не удалось обновить расписание с сайта\n{now:%d.%m.%Y %H:%M}, Уфа\n'
            f'Причина: {reason}\n'
            + (f'Предыдущая локальная копия сохранена: {previous}, Уфа.\nБоты продолжают отвечать из неё.'
               if previous else 'Первоначальная локальная копия ещё не создана; расписание пока недоступно.')
            + '\nПовторить обновление вручную: /admin → «Обновить данные».')
    profiles.set_metadata('pending_refresh_alert',{'id':uuid.uuid4().hex,'text':text})
    try:
        await deliver_refresh_alert(profiles,bot,expected_username=getattr(source,'owner_username',None))
    except Exception as failure:
        # Preserve the original refresh error and the pending notification.
        logging.getLogger(__name__).warning('Не удалось доставить уведомление об обновлении (%s)',type(failure).__name__)
