"""Fixed boundaries taken from the user's original bot."""

from aiogram import BaseMiddleware, Bot

EXPECTED_BOT_ID = 8591550954
GROUP_CHAT_ID = -1004345960675


class ScopedBot(Bot):
    async def __call__(self, method, request_timeout=None):
        if self.id != EXPECTED_BOT_ID:
            raise ValueError("Chat management is restricted to the original bot")
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None:
            # Positive IDs are private admin conversations; group targets are fixed.
            try:
                numeric = int(chat_id)
            except (ValueError, TypeError):
                raise ValueError("Named chat targets are not allowed") from None
            if numeric < 0 and numeric != GROUP_CHAT_ID:
                raise ValueError("Chat management is restricted to the original chat")
        return await super().__call__(method, request_timeout=request_timeout)


class ScopeMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
        bot = data.get("bot")
        if not chat or not bot or bot.id != EXPECTED_BOT_ID:
            return
        if chat.type != "private" and chat.id != GROUP_CHAT_ID:
            return
        return await handler(event, data)


class RequestCooldownMiddleware(BaseMiddleware):
    """Limit commands/buttons without throttling membership service events."""

    def __init__(self, owner_id, owner_username, limiter=None):
        from bgmu_bot.traffic import Cooldown
        self.owner_id = owner_id
        self.owner_username = owner_username.lstrip('@').casefold()
        self.limiter = limiter or Cooldown()

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        if not user or user.id == self.owner_id or (
            self.owner_username and (getattr(user, 'username', None) or '').casefold() == self.owner_username
        ):
            return await handler(event, data)
        if getattr(event, 'new_chat_members', None) or getattr(event, 'left_chat_member', None):
            return await handler(event, data)
        callback = getattr(event, 'data', None) is not None
        if not callback and not getattr(event, 'text', None):
            return await handler(event, data)
        if self.limiter.allow(user.id):
            return await handler(event, data)
        if callback:
            await event.answer('Подожди 2 секунды между запросами.')
