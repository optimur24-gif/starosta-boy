import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.methods import BanChatMember, SendMessage

from restricted_bot.policy import EXPECTED_BOT_ID, GROUP_CHAT_ID, ScopeMiddleware, ScopedBot


@pytest.mark.asyncio
@pytest.mark.parametrize('chat_id,bot_id,allowed', [
    (GROUP_CHAT_ID, EXPECTED_BOT_ID, True), (-100999999, EXPECTED_BOT_ID, False),
    (GROUP_CHAT_ID, 12345, False), (1153815035, EXPECTED_BOT_ID, True),
])
async def test_management_update_scope(chat_id, bot_id, allowed):
    middleware, handler = ScopeMiddleware(), AsyncMock()
    event = SimpleNamespace(chat=SimpleNamespace(id=chat_id, type='private' if chat_id > 0 else 'supergroup'))
    await middleware(handler, event, {'bot': SimpleNamespace(id=bot_id)})
    assert handler.await_count == int(allowed)
    callback = SimpleNamespace(message=event)
    handler.reset_mock()
    await middleware(handler, callback, {'bot': SimpleNamespace(id=bot_id)})
    assert handler.await_count == int(allowed)


@pytest.mark.asyncio
async def test_management_calls_are_bound_to_both_chat_and_bot():
    bot = ScopedBot(f'{EXPECTED_BOT_ID}:offline-test')
    try:
        with patch.object(Bot, '__call__', new_callable=AsyncMock) as network:
            await bot(BanChatMember(chat_id=GROUP_CHAT_ID, user_id=42))
            assert network.await_count == 1
            for chat in (-100999999, '@anotherchat'):
                with pytest.raises(ValueError):
                    await bot(BanChatMember(chat_id=chat, user_id=42))
            assert network.await_count == 1
        wrong = ScopedBot('12345:offline-test')
        try:
            with pytest.raises(ValueError):
                await wrong(SendMessage(chat_id=GROUP_CHAT_ID, text='test'))
        finally:
            await wrong.session.close()
    finally:
        await bot.session.close()


def test_original_bot_database_and_all_routers_start_without_network(tmp_path):
    env = dict(os.environ, RESTRICTED_BOT_TOKEN=f'{EXPECTED_BOT_ID}:offline-test', DATA_DIR=str(tmp_path))
    result = subprocess.run([os.sys.executable, '-m', 'restricted_bot', '--check'], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Проверка OK' in result.stdout
    assert (tmp_path / 'bot_data.db').exists()
    env['RESTRICTED_BOT_TOKEN'] = '12345:offline-test'
    wrong = subprocess.run([os.sys.executable, '-m', 'restricted_bot', '--check'], env=env, capture_output=True, text=True)
    assert wrong.returncode != 0
    assert 'только исходному боту' in wrong.stderr
