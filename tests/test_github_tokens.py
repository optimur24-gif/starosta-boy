import pytest

from tools.configure_github_tokens import assign_tokens

PUBLIC = '8987612792:' + 'a' * 35
RESTRICTED = '8591550954:' + 'b' * 35


@pytest.mark.parametrize('public,restricted', [(PUBLIC, RESTRICTED), (RESTRICTED, PUBLIC), ('"'+PUBLIC+'"', "'"+RESTRICTED+"'"), ('TELEGRAM_BOT_TOKEN='+PUBLIC, 'RESTRICTED_BOT_TOKEN='+RESTRICTED)])
def test_tokens_are_assigned_by_identity_despite_swapped_roles_or_pasted_labels(public, restricted):
    assert assign_tokens({'TELEGRAM_BOT_TOKEN': public, 'RESTRICTED_BOT_TOKEN': restricted}) == {'TELEGRAM_BOT_TOKEN': PUBLIC, 'RESTRICTED_BOT_TOKEN': RESTRICTED}


@pytest.mark.parametrize('values', [{}, {'TELEGRAM_BOT_TOKEN': PUBLIC}, {'RESTRICTED_BOT_TOKEN': '1234567890:' + 'c'*35}, {'TELEGRAM_BOT_TOKEN': PUBLIC+'\n8987612792:'+'d'*35, 'RESTRICTED_BOT_TOKEN': RESTRICTED}])
def test_missing_wrong_and_conflicting_identities_fail_without_printing_secrets(values):
    with pytest.raises(ValueError) as raised:
        assign_tokens(values)
    for value in (PUBLIC, RESTRICTED, 'c'*35, 'd'*35):
        assert value not in str(raised.value)
