from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

from bgmu_bot.profiles import ProfileStore
from bgmu_bot.registry import RegistryObserver
from test_admin_tools import panel, update


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path/'prefs.db')


def person(uid,username=None,name='Student',bot=False):
    return NS(id=uid,username=username,full_name=name,is_bot=bot)


@pytest.mark.asyncio
async def test_known_members_are_per_chat_and_not_personal_broadcast_contacts(store):
    registry=RegistryObserver(store)
    u=update(uid=2,username='Student',kind='group',text='Message body must not be archived')
    await registry.handle(u,None)
    await registry.handle(u,None)
    assert store.chat_member_count(-10)==1
    assert store.chat_members(-10)[0]['username']=='Student'
    assert store.registered_contacts()==[]
    assert store.announcement_targets('users')==()
    with store.connection() as db:
        assert 'Message body must not be archived' not in '\n'.join(db.iterdump())
    u.effective_chat.id=-20
    await registry.handle(u,None)
    assert store.chat_member_count(-20)==1
    assert store.chat_member_count(-10)==1


@pytest.mark.asyncio
async def test_join_leave_rename_and_anonymous_sender_handling(store):
    registry=RegistryObserver(store)
    u=update(uid=2,username='Admin',kind='group')
    u.effective_message.new_chat_members=[person(3,'Joined'),person(4,'Robot',bot=True)]
    await registry.handle(u,None)
    assert store.chat_member_count(-10)==2
    u.effective_message.new_chat_members=[]
    u.effective_message.left_chat_member=person(3,'Joined')
    await registry.handle(u,None)
    assert {m['user_id']:m for m in store.chat_members(-10)}[3]['active']==0
    u.effective_message.left_chat_member=None
    u.effective_user=person(3,'Renamed')
    await registry.handle(u,None)
    assert {m['user_id']:m for m in store.chat_members(-10)}[3]['username']=='Renamed'
    assert {m['user_id']:m for m in store.chat_members(-10)}[3]['active']==1
    u.effective_user=person(5,'Hidden')
    u.effective_message.sender_chat=NS(id=-10)
    await registry.handle(u,None)
    assert store.chat_member_count(-10)==2


def test_persistence_migration_and_unchanged_member_does_not_rewrite_backup(store):
    store.observe_chat(-10,title='Study')
    store.observe_member(-10,person(2,'Student'))
    backup=store.backups.directory/'settings.sqlite3'
    previous=backup.read_bytes()
    stamp=backup.stat().st_mtime_ns
    store.observe_member(-10,person(2,'Student'))
    assert backup.read_bytes()==previous and backup.stat().st_mtime_ns==stamp
    reopened=ProfileStore(store.path)
    assert reopened.chat_member_count(-10)==1
    reopened.migrate_chat(-10,-10010)
    assert reopened.chat_members(-10010)[0]['user_id']==2


@pytest.mark.asyncio
async def test_owner_member_pages_and_refresh_only_administrators_not_all_members(store):
    store.observe_chat(-10,title='Study')
    for uid in range(2,12):store.observe_member(-10,person(uid,f'user{uid}'))
    p=panel(store)
    ctx=NS(user_data={},bot=NS(get_chat_member_count=AsyncMock(return_value=55),
        get_chat_administrators=AsyncMock(return_value=[NS(user=person(20,'Admin'),is_anonymous=False),
                                                     NS(user=person(21,'Hidden'),is_anonymous=True)])))
    await p.root(update(),ctx)
    async def click(action,value='-10,0',username='Owner',kind='private'):
        nonce=ctx.user_data['admin_tools']['nonce']
        u=update(username=username,kind=kind,data=f'a:{nonce}:{action}:{value}')
        await p.callback(u,ctx)
        return u
    await click('members_refresh',username='Intruder')
    await click('members_refresh',kind='group')
    ctx.bot.get_chat_administrators.assert_not_called()
    u=await click('members_refresh')
    text=u.callback_query.edit_message_text.call_args.args[0]
    assert '55' in text and 'неполный список' in text
    assert store.chat_member_count(-10)==11
    assert all(m['user_id']!=21 for m in store.chat_members(-10,limit=100))
    second=await click('members','-10,1')
    assert 'ID ' in second.callback_query.edit_message_text.call_args.args[0]
    unknown=await click('members','-99,0')
    assert 'Чат не найден' in unknown.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_telegram_denial_does_not_remove_known_members(store):
    store.observe_chat(-10,title='Study');store.observe_member(-10,person(2,'Student'))
    p=panel(store);ctx=NS(user_data={},bot=NS(get_chat_member_count=AsyncMock(side_effect=Forbidden('denied')),
                                           get_chat_administrators=AsyncMock(side_effect=Forbidden('denied'))))
    await p.root(update(),ctx)
    nonce=ctx.user_data['admin_tools']['nonce']
    u=update(data=f'a:{nonce}:members_refresh:-10,0')
    await p.callback(u,ctx)
    text=u.callback_query.edit_message_text.call_args.args[0]
    assert '@Student' in text and 'недоступен' in text
    assert store.chat_member_count(-10)==1
