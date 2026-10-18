from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter
from telegram.ext import ApplicationHandlerStop

from bgmu_bot.broadcasts import broadcast_tick
from bgmu_bot.notifications import send_part
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.traffic import AccessGuard
from test_admin_tools import update, panel


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path/'prefs.db')


@pytest.mark.asyncio
async def test_blocked_user_cannot_use_private_or_group_commands_and_old_callbacks(store):
    store.touch_contact(2,'Student')
    store.set_access_blocked(2,True,1)
    guard=AccessGuard(store,lambda u:u.effective_user.username=='Owner')
    for u in (update(uid=2,username='Student'), update(uid=2,username='Student',kind='group',text='/today'),
              update(uid=2,username='Student',data='old:day:2026-10-21')):
        with pytest.raises(ApplicationHandlerStop): await guard.handle(u,None)
    await guard.handle(update(uid=3,username='Another',kind='group',text='/today'),None)
    store.set_access_blocked(2,False,1)
    await guard.handle(update(uid=2,username='Student'),None)


@pytest.mark.asyncio
async def test_chat_ban_applies_to_all_members_owner_can_still_manage_in_private(store):
    store.observe_chat(-10,title='Group')
    store.set_access_blocked(-10,True,1)
    guard=AccessGuard(store,lambda u:u.effective_user.username=='Owner')
    for username in ('Student','Owner'):
        with pytest.raises(ApplicationHandlerStop):
            await guard.handle(update(username=username,kind='group',text='/today'),None)
    await guard.handle(update(),None)
    ordinary=update(username='Student',kind='group',text='ordinary discussion')
    with pytest.raises(ApplicationHandlerStop): await guard.handle(ordinary,None)
    ordinary.effective_message.reply_text.assert_not_called()


def test_persistent_ban_survives_start_rejoin_and_migration(store):
    store.touch_contact(2,'Student');store.observe_chat(-10,title='Group')
    for target in (2,-10):store.set_access_blocked(target,True,1)
    reopened=ProfileStore(store.path)
    reopened.touch_contact(2,'NewUsername')
    reopened.register_chat(-10)
    reopened.observe_chat(-10,status='administrator')
    reopened.migrate_chat(-10,-10010)
    assert all(reopened.access_blocked(t) for t in (2,-10,-10010))


@pytest.mark.asyncio
async def test_queued_announcements_exclude_newly_blocked_recipient(store):
    for uid in (1,2):store.touch_contact(uid)
    store.observe_chat(-10,title='Group')
    job=store.queue_broadcast(1,'Hello','users',[1,2,-10])
    for target in (2,-10):store.set_access_blocked(target,True,1)
    assert store.announcement_targets('users')==(1,)
    assert store.announcement_targets('groups')==()
    ctx=NS(job=NS(data=store),bot=NS(send_message=AsyncMock()))
    await broadcast_tick(ctx)
    assert ctx.bot.send_message.await_count==1
    assert ctx.bot.send_message.call_args.args[0]==1
    assert store.broadcast_report(job,1)['counts']=={'sent':1,'opted_out':2}


def test_scheduled_chats_excluded_and_preferences_preserved(store):
    path=('group','26','1','Faculty','1','1');labels=('Groups','Year','Semester','Faculty','Course','P-114')
    store.save_chat(-10,path,labels)
    assert store.subscribed_chats()==(-10,)
    store.set_access_blocked(-10,True,1)
    assert store.subscribed_chats()==()
    assert store.chat_group(-10).path==path
    store.set_access_blocked(-10,False,1)
    assert store.subscribed_chats()==(-10,)


@pytest.mark.asyncio
async def test_schedule_retry_checks_ban_again_after_wait(store,monkeypatch):
    bot=NS(send_message=AsyncMock(side_effect=RetryAfter(1)))
    async def sleep(_):store.set_access_blocked(-10,True,1)
    monkeypatch.setattr('bgmu_bot.notifications.asyncio.sleep',sleep)
    sent=await send_part(bot,-10,'Schedule',allowed=lambda:not store.access_blocked(-10))
    assert sent is False and bot.send_message.await_count==1


@pytest.mark.asyncio
async def test_access_buttons_require_owner_confirmation_and_can_restore(store):
    store.touch_contact(1,'Owner');store.touch_contact(2,'Student')
    p=panel(store);ctx=NS(user_data={})
    await p.root(update(),ctx)
    async def click(action,value,username='Owner',kind='private'):
        nonce=ctx.user_data['admin_tools']['nonce']
        await p.callback(update(username=username,kind=kind,data=f'a:{nonce}:{action}:{value}'),ctx)
    await click('users','0')
    await click('access','2')
    assert not store.access_blocked(2)
    await click('access_deny','2',username='Student')
    await click('access_deny','2',kind='group')
    assert not store.access_blocked(2)
    await click('access_deny','2')
    assert store.access_blocked(2)
    await click('access_allow','2')
    assert not store.access_blocked(2)
    await click('access_deny','1')
    assert not store.access_blocked(1)
    await click('access_deny','999')
    assert not store.access_blocked(999)
