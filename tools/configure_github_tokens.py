"""Assign the two supplied GitHub secrets to their verified bot ID roles."""
import os
from pathlib import Path
import re

SCHEDULE_BOT_ID = '8987612792'
MANAGEMENT_BOT_ID = '8591550954'
TOKEN = re.compile(r'(?<![A-Za-z0-9_-])(\d{8,12}:[A-Za-z0-9_-]{30,})(?![A-Za-z0-9_-])')


def assign_tokens(environment):
    found = {}
    for name in ('TELEGRAM_BOT_TOKEN', 'RESTRICTED_BOT_TOKEN'):
        for match in TOKEN.finditer(environment.get(name, '')):
            token = match.group(1)
            identity = token.split(':', 1)[0]
            if identity in found and found[identity] != token:
                raise ValueError('Two different tokens for one bot ID; correct the repository secrets.')
            found[identity] = token
    for identity, role in ((SCHEDULE_BOT_ID, 'TELEGRAM_BOT_TOKEN'), (MANAGEMENT_BOT_ID, 'RESTRICTED_BOT_TOKEN')):
        if identity not in found:
            raise ValueError(f'Missing valid token for bot ID {identity}. Add the correct {role} in Repository secrets.')
    return {'TELEGRAM_BOT_TOKEN': found[SCHEDULE_BOT_ID], 'RESTRICTED_BOT_TOKEN': found[MANAGEMENT_BOT_ID]}


def main():
    try:
        assigned = assign_tokens(os.environ)
    except ValueError as error:
        print('::error::' + str(error), flush=True)
        return 1
    target = os.environ.get('GITHUB_ENV')
    if not target:
        print('::error::This helper must run inside GitHub Actions.', flush=True)
        return 1
    # Register extracted values with the runner before any subsequent env logging.
    for token in assigned.values():
        print('::add-mask::' + token, flush=True)
    # GITHUB_ENV is the runner's supported private handoff.
    with Path(target).open('a') as output:
        for name, token in assigned.items():
            output.write(f'{name}={token}\n')
    print('Bot tokens assigned by expected IDs; values hidden.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
