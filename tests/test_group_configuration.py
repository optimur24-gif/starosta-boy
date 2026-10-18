import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize('value', ['-1001234567890', '-1234567'])
def test_configured_group_is_shared_by_policy_and_bot(tmp_path, value):
    program = '''
from restricted_bot import bot, policy
assert bot.GROUP_CHAT_ID == policy.GROUP_CHAT_ID
assert str(bot.GROUP_CHAT_ID) == __import__('os').environ['GROUP_CHAT_ID']
'''
    env = dict(os.environ, GROUP_CHAT_ID=value, RESTRICTED_BOT_TOKEN='8591550954:offline-test', DATA_DIR=str(tmp_path))
    result = subprocess.run([sys.executable, '-c', program], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('value', ['0', '123456', 'not-a-group', ''])
def test_invalid_group_cannot_widen_management_scope(value):
    result = subprocess.run([sys.executable, '-c', 'import restricted_bot.policy'], env=dict(os.environ, GROUP_CHAT_ID=value), text=True, capture_output=True)
    assert result.returncode != 0
    assert 'GROUP_CHAT_ID must be a negative numeric' in result.stderr
