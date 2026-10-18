from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, NetworkError, RetryAfter
from telegram.ext import ApplicationHandlerStop

from bgmu_bot.admin_panel import AdminPanel
from bgmu_bot.broadcasts import broadcast_tick
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.registry import RegistryObserver
from bgmu_bot.traffic import Cooldown, TrafficGuard


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path/'prefs.db')


def update(uid=1, username='Owner', kind='private', text='hello', data=None):
    return NS(effective_user=NS(id=uid,username=username,full_name='Test',is_bot=False),
              effective_chat=NS(id=uid if kind=='private' else -10,type=kind,title='Study'),
              effective_message=NS(text=text,reply_text=AsyncMock()),my_chat_member=None,
              callback_query=NS(data=data,answer=AsyncMock(),edit_message_text=AsyncMock()) if data else None)


def panel(store):
    return AdminPanel(store,NS(is_admin=lambda u:u.effective_user.username=='Owner'))


@pytest.mark.asyncio
async def test_registry_only_personal_interactions_and_no_body(store):
    registry=RegistryObserver(store)
    await registry.handle(update(text='private message body'),None)
    await registry.handle(update(uid=2,kind='group'),None)
    contact=store.registered_contacts()[0]
    assert contact['user_id']==1 and contact['username']=='Owner'
    assert 'private message body' not in str(contact)
    assert len(store.registered_contacts())==1
    store.contact_subscription(1,False)
    await registry.handle(update(username='Renamed'),None)
    assert store.registered_contacts()[0]['username']=='Renamed'
    assert store.announcement_targets('users')==()


@pytest.mark.asyncio
async def test_role_changes_track_actor_without_messages(store):
    u=update(kind='group')
    u.my_chat_member=NS(chat=NS(id=-10,type='group',title='Study',username=None),
        new_chat_member=NS(status='member'),old_chat_member=NS(status='left'),from_user=u.effective_user)
    observer=RegistryObserver(store)
    await observer.handle(u,None)
    u.my_chat_member.old_chat_member.status='member'
    u.my_chat_member.new_chat_member.status='administrator'
    await observer.handle(u,None)
    row=store.registered_chats()[0]
    assert row['added_by']==1 and row['bot_status']=='administrator'


def test_migration_keeps_metadata_optout_and_check_does_not_reactivate(store):
    store.observe_chat(-10,title='Study',username='study_public',status='member',added_by=1)
    store.group_announcements(-10,False)
    store.migrate_chat(-10,-10010)
    rows={row['chat_id']:row for row in store.registered_chats()}
    assert rows[-10010]['title']=='Study' and not rows[-10010]['announcements']
    store.observe_chat(-10,status='unavailable')
    assert not {r['chat_id']:r for r in store.registered_chats()}[-10]['active']


@pytest.mark.asyncio
async def test_owner_lists_and_private_only_callbacks(store):
    store.touch_contact(2,'student','Student')
    store.observe_chat(-10,title='Study',username='study_public',status='member')
    p=panel(store);context=NS(user_data={})
    u=update()
    await p.root(u,context)
    assert 'Пользователи' in str(u.effective_message.reply_text.call_args)
    nonce=context.user_data['admin_tools']['nonce']
    click=update(data=f'a:{nonce}:users:0')
    await p.callback(click,context)
    assert '@student' in click.callback_query.edit_message_text.call_args.args[0]
    assert 'https://t.me/student' in click.callback_query.edit_message_text.call_args.args[0]
    for bad in (update(username='Other',data=f'a:{nonce}:users:0'),update(kind='group',data=f'a:{nonce}:users:0')):
        await p.callback(bad,context)
        bad.callback_query.edit_message_text.assert_not_called()
        bad.callback_query.answer.assert_awaited()
    await p.chats(u,context)
    assert 'https://t.me/study_public' in u.effective_message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_preview_confirmation_and_duplicate_callback(store):
    store.touch_contact(2,'student')
    p=panel(store);ctx=NS(user_data={});u=update()
    await p.root(u,ctx)
    async def click(action,value=''):
        nonce=ctx.user_data['admin_tools']['nonce']
        await p.callback(update(data=f'a:{nonce}:{action}:{value}'),ctx)
    await click('audience','users')
    await p.text(update(text='Announcement'),ctx)
    assert store.active_broadcast() is None
    nonce=ctx.user_data['admin_tools']['nonce']
    await p.callback(update(data=f'a:{nonce}:send:'),ctx)
    first=store.active_broadcast()
    assert first and first['body']=='Announcement'
    await p.callback(update(data=f'a:{nonce}:send:'),ctx)
    assert store.recent_broadcasts(1)==(first['id'],)


@pytest.mark.asyncio
async def test_worker_skips_optout_blocks_and_does_not_duplicate(store):
    for uid in (1,2,3): store.touch_contact(uid)
    job=store.queue_broadcast(9,'hello','users',[1,1,2,3])
    store.contact_subscription(2,False)
    bot=NS(send_message=AsyncMock(side_effect=[None,Forbidden('blocked')]))
    ctx=NS(job=NS(data=store),bot=bot)
    await broadcast_tick(ctx)
    await broadcast_tick(ctx)
    report=store.broadcast_report(job,9)
    assert report['counts']=={'sent':1,'opted_out':1,'blocked':1}
    assert report['state']=='finished' and bot.send_message.await_count==2
    assert not store.announcement_allowed(3)


@pytest.mark.asyncio
async def test_retry_and_uncertain_delivery_after_restart(store):
    store.touch_contact(1)
    job=store.queue_broadcast(9,'hello','users',[1])
    bot=NS(send_message=AsyncMock(side_effect=RetryAfter(10)))
    ctx=NS(job=NS(data=store),bot=bot)
    await broadcast_tick(ctx)
    assert store.broadcast_report(job,9)['counts']=={'pending':1}
    await broadcast_tick(ctx)
    assert bot.send_message.await_count==1
    store.pause_broadcast(job,0)
    bot.send_message.side_effect=NetworkError('timeout')
    await broadcast_tick(ctx)
    assert store.broadcast_report(job,9)['counts']=={'uncertain':1}
    job2=store.queue_broadcast(9,'again','users',[1])
    store.claim_recipient(job2)
    store.recover_broadcasts()
    assert store.broadcast_report(job2,9)['counts']=={'uncertain':1}


def test_cancel_and_clear_only_owners_completed_jobs(store):
    store.touch_contact(1)
    first=store.queue_broadcast(9,'first','users',[1])
    second=store.queue_broadcast(9,'second','users',[1])
    store.cancel_broadcast(first,8)
    assert store.broadcast_report(first,9)['state']=='queued'
    store.cancel_broadcast(first,9)
    assert store.clear_broadcast_history(9)==1
    assert store.active_broadcast()['id']==second
    assert len(store.registered_contacts())==1


@pytest.mark.asyncio
async def test_cooldown_owner_exempt_group_conversation_ignored():
    now=[0.0];limiter=Cooldown(clock=lambda:now[0],max_users=2)
    guard=TrafficGuard(lambda u:u.effective_user.username=='Owner',limiter)
    ordinary=update(uid=2,username='Student')
    await guard.handle(ordinary,None)
    with pytest.raises(ApplicationHandlerStop): await guard.handle(ordinary,None)
    now[0]=2
    await guard.handle(ordinary,None)
    for _ in range(3): await guard.handle(update(),None)
    for _ in range(3): await guard.handle(update(uid=3,username='Student',kind='group',text='ordinary conversation'),None)
    assert 3 not in limiter.users


@pytest.mark.asyncio
async def test_selected_groups_and_admin_role_revoked_before_confirmation(store):
    for cid in (-10,-20,-30): store.observe_chat(cid,title=str(cid),status='member')
    store.group_announcements(-30,False)
    p=panel(store);ctx=NS(user_data={});await p.root(update(),ctx)
    async def click(action,value='',username='Owner'):
        nonce=ctx.user_data['admin_tools']['nonce']
        await p.callback(update(username=username,data=f'a:{nonce}:{action}:{value}'),ctx)
    await click('audience','selected')
    await click('toggle','-20')
    await click('toggle','-30')
    assert ctx.user_data['admin_tools']['draft']['selected']==[-20]
    await click('compose')
    await p.text(update(text='Selected group only'),ctx)
    await click('send',username='NotOwner')
    assert store.active_broadcast() is None
    await click('send')
    job=store.active_broadcast()
    assert store.claim_recipient(job['id'])[0]==-20
    assert store.claim_recipient(job['id']) is None


@pytest.mark.asyncio
async def test_original_bot_cooldown_and_owner_exception():
    from restricted_bot.policy import RequestCooldownMiddleware
    clock=[0.0]
    middleware=RequestCooldownMiddleware(99,'Owner',Cooldown(clock=lambda:clock[0]))
    handler=AsyncMock()
    e=NS(from_user=NS(id=2,username='Student'),text='/today')
    await middleware(handler,e,{})
    await middleware(handler,e,{})
    assert handler.await_count==1
    clock[0]=2
    await middleware(handler,e,{})
    assert handler.await_count==2
    e.from_user.username='Owner'
    await middleware(handler,e,{})
    await middleware(handler,e,{})
    assert handler.await_count==4


@pytest.mark.asyncio
async def test_owner_alerts_first_contact_only_and_no_group_messages(store):
    registry=RegistryObserver(store,lambda u:u.effective_user.username=='Owner','Owner')
    bot=NS(get_chat=AsyncMock(return_value=NS(type='private',username='Owner')),send_message=AsyncMock())
    ctx=NS(bot=bot)
    await registry.handle(update(),ctx)
    student=update(uid=2,username='Student')
    await registry.handle(student,ctx)
    await registry.handle(student,ctx)
    assert bot.send_message.await_count==1
    assert bot.send_message.call_args.args[0]==1
    assert '@Student' in bot.send_message.call_args.args[1]
    group=update(uid=3,username='Student',kind='group')
    await registry.handle(group,ctx)
    await registry.handle(group,ctx)
    assert bot.send_message.await_count==2
    assert 'Study' in bot.send_message.call_args.args[1]
    assert bot.send_message.call_args.args[0]==1
    group.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_alert_settings_separate_persist_and_forbidden_does_not_break(store):
    registry=RegistryObserver(store,lambda u:u.effective_user.username=='Owner','Owner')
    bot=NS(get_chat=AsyncMock(return_value=NS(type='private',username='Owner')),send_message=AsyncMock())
    ctx=NS(bot=bot)
    await registry.handle(update(),ctx)
    store.set_metadata('owner_alerts_users',False)
    await registry.handle(update(uid=2,username='Student'),ctx)
    bot.send_message.assert_not_called()
    reopened=ProfileStore(store.path)
    assert reopened.metadata('owner_alerts_users') is False
    bot.send_message.side_effect=Forbidden('owner blocked')
    await registry.handle(update(uid=3,username='Student',kind='group'),ctx)
    assert bot.send_message.await_count==1
    store.set_metadata('owner_alerts_groups',False)
    other=update(uid=4,username='Student',kind='group')
    other.effective_chat.id=-20
    await registry.handle(other,ctx)
    assert bot.send_message.await_count==1


@pytest.mark.asyncio
async def test_alerts_do_not_send_to_account_with_changed_username(store):
    registry=RegistryObserver(store,lambda u:u.effective_user.username=='Owner','Owner')
    ctx=NS(bot=NS(get_chat=AsyncMock(return_value=NS(type='private',username='SomeoneElse')),send_message=AsyncMock()))
    await registry.handle(update(),ctx)
    await registry.handle(update(uid=2,username='Student'),ctx)
    ctx.bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_only_owner_can_change_alerts_and_nonce_prevents_toggle_replay(store):
    p=panel(store);ctx=NS(user_data={})
    await p.root(update(),ctx)
    async def click(action,value='',username='Owner'):
        nonce=ctx.user_data['admin_tools']['nonce']
        u=update(username=username,data=f'a:{nonce}:{action}:{value}')
        await p.callback(u,ctx)
        return u
    await click('alerts')
    await click('alert_set','users_off',username='Intruder')
    assert store.metadata('owner_alerts_users') is None
    old=await click('alert_set','users_off')
    assert store.metadata('owner_alerts_users') is False
    assert store.metadata('owner_alerts_groups') is None
    await p.callback(old,ctx)
    assert store.metadata('owner_alerts_users') is False
    await click('alert_set','users_on')
    assert store.metadata('owner_alerts_users') is True
