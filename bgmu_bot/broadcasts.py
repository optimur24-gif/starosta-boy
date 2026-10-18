"""Explicit administrative announcements to users of this bot and connected chats."""

from datetime import timedelta
import time

from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter, TelegramError

PREFIX = '📣 Сообщение владельца бота\n\n'


async def broadcast_tick(context):
    profiles = context.job.data
    job = profiles.active_broadcast()
    if not job or job['pause_until'] > time.time():
        return
    for _ in range(3):
        target = profiles.claim_recipient(job['id'])
        if target is None:
            profiles.finish_broadcast(job['id'])
            break
        recipient_id, attempts = target
        if not profiles.announcement_allowed(recipient_id):
            profiles.finish_recipient(job['id'], recipient_id, 'opted_out')
            continue
        try:
            await context.bot.send_message(recipient_id, PREFIX+job['body'], parse_mode=None)
        except RetryAfter as error:
            delay = error.retry_after.total_seconds() if isinstance(error.retry_after,timedelta) else error.retry_after
            profiles.finish_recipient(job['id'], recipient_id, 'pending' if attempts < 3 else 'failed')
            profiles.pause_broadcast(job['id'], time.time()+delay+1)
            return
        except Forbidden:
            profiles.finish_recipient(job['id'], recipient_id, 'blocked')
            if recipient_id > 0:
                profiles.contact_active(recipient_id, False)
            else:
                profiles.deactivate_chat(recipient_id)
        except ChatMigrated as error:
            profiles.migrate_chat(recipient_id, error.new_chat_id)
            profiles.redirect_broadcast(job['id'], recipient_id, error.new_chat_id)
        except BadRequest:
            profiles.finish_recipient(job['id'], recipient_id, 'failed')
        except NetworkError:
            # Telegram might have accepted the request. Avoid unsolicited duplicates.
            profiles.finish_recipient(job['id'], recipient_id, 'uncertain')
        except TelegramError:
            profiles.finish_recipient(job['id'], recipient_id, 'failed')
        else:
            profiles.finish_recipient(job['id'], recipient_id, 'sent')
    profiles.finish_broadcast(job['id'])
