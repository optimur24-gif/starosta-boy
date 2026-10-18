import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram.error import Forbidden

from bgmu_bot.bgmu import BGMUSource
from bgmu_bot.owner_alerts import report_refresh_failure, deliver_refresh_alert
from bgmu_bot.profiles import ProfileStore
from bgmu_bot.refresh import morning_refresh
from bgmu_bot.registry import RegistryObserver
from bgmu_bot.source import SourceError
from bgmu_bot.website import WebsiteClient
from test_admin_tools import panel, update

PATH=('group','26','1','Faculty','1','1','A')
LABELS=('Groups','Year','Semester','Медицинский факультет','1 курс','П-114','Подгруппа А')


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path/'prefs.db')


def owner_bot(username='Owner'):
    return NS(get_chat=AsyncMock(return_value=NS(type='private',username=username)),send_message=AsyncMock())


@pytest.mark.asyncio
async def test_selected_group_details_visible_only_to_private_owner(store):
    store.touch_contact(2,'Student');store.save(2,PATH,LABELS)
    store.touch_contact(3,'Unbound')
    p=panel(store);ctx=NS(user_data={})
    await p.root(update(),ctx)
    async def click(username='Owner',kind='private'):
        nonce=ctx.user_data['admin_tools']['nonce']
        u=update(username=username,kind=kind,data=f'a:{nonce}:users:0')
        await p.callback(u,ctx)
        return u
    allowed=await click()
    text=allowed.callback_query.edit_message_text.call_args.args[0]
    assert '@Student' in text and 'П-114' in text
    assert 'Медицинский факультет' in text and '1 курс' in text and 'Подгруппа А' in text
    assert 'Учебная группа: не выбрана' in text
    for username,kind in (('NotOwner','private'),('Owner','group')):
        denied=await click(username,kind)
        denied.callback_query.edit_message_text.assert_not_called()
        assert denied.callback_query.answer.call_args.kwargs['show_alert']


@pytest.mark.asyncio
async def test_user_pages_include_updated_saved_choice_without_network(store):
    for uid in range(2,9):store.touch_contact(uid,'student'+str(uid))
    store.save(2,PATH,LABELS)
    p=panel(store);ctx=NS(user_data={})
    await p.root(update(),ctx)
    await p.users(update(),ctx,page=1)
    store.save(2,PATH[:-1],LABELS[:-1][:-1]+('Л-222',))
    u=update();await p.users(u,ctx,page=1)
    # Every page is well below Telegram's UTF-16 text limit.
    text=u.effective_message.reply_text.call_args.args[0]
    assert 'Л-222' in text and 'П-114' not in text
    assert len(text.encode('utf-16-le'))//2 <= 4096


@pytest.mark.asyncio
async def test_scheduled_failure_notifies_only_owner_once_no_extra_site_retries(store,tmp_path):
    store.set_metadata('owner_alert_recipient',{'id':1,'username':'owner'})
    calls=[]
    def response(request):calls.append(request);return httpx.Response(503)
    async with WebsiteClient(transport=httpx.MockTransport(response)) as website:
        source=BGMUSource(website);source.use_snapshot(tmp_path/'schedule.sqlite3');source.snapshot.min_interval=0
        bot=owner_bot();ctx=NS(job=NS(data=(source,store)),bot=bot)
        await morning_refresh(ctx)
        await morning_refresh(ctx)
        assert len(calls)==1 and bot.send_message.await_count==1
        assert bot.send_message.call_args.args[0]==1
        text=bot.send_message.call_args.args[1]
        assert 'Не удалось обновить' in text and 'HTTP 503' in text and 'ещё не создана' in text
        assert store.metadata('pending_refresh_alert') is None


@pytest.mark.asyncio
async def test_pending_failure_delivered_on_owner_start_only_and_not_twice(store):
    source=NS(snapshot=None)
    await report_refresh_failure(source,store,None,SourceError('HTTP 503'))
    assert store.metadata('pending_refresh_alert')
    registry=RegistryObserver(store,lambda u:u.effective_user.username=='Owner','Owner')
    bot=owner_bot();ctx=NS(bot=bot)
    await registry.handle(update(uid=2,username='Student'),ctx)
    bot.send_message.assert_not_called()
    await registry.handle(update(text='/start'),ctx)
    await registry.handle(update(text='/start'),ctx)
    assert bot.send_message.await_count==1
    assert store.metadata('pending_refresh_alert') is None


@pytest.mark.asyncio
async def test_failed_telegram_delivery_keeps_pending_error_and_verifies_username(store):
    store.set_metadata('owner_alert_recipient',{'id':1,'username':'owner'})
    bot=owner_bot('RenamedAccount')
    await report_refresh_failure(NS(snapshot=None,owner_username='Owner'),store,bot,SourceError('HTTP 503'))
    bot.send_message.assert_not_called()
    assert store.metadata('pending_refresh_alert')
    bot.get_chat.return_value.username='Owner';bot.send_message.side_effect=Forbidden('blocked')
    assert not await deliver_refresh_alert(store,bot,expected_username='Owner')
    assert store.metadata('pending_refresh_alert')
    bot.send_message.side_effect=None
    assert await deliver_refresh_alert(store,bot,expected_username='Owner')
    assert store.metadata('pending_refresh_alert') is None


@pytest.mark.asyncio
async def test_refresh_alert_switch_independent_and_owner_only(store):
    store.set_metadata('owner_alert_recipient',{'id':1,'username':'owner'})
    store.set_metadata('owner_alerts_users',False);store.set_metadata('owner_alerts_groups',False)
    bot=owner_bot()
    source=NS(snapshot=NS(info=lambda:{'full_refresh_at':'2026-10-17T08:00:00+00:00'}))
    await report_refresh_failure(source,store,bot,SourceError('HTTP 503'))
    assert bot.send_message.await_count==1
    assert 'Предыдущая локальная копия сохранена' in bot.send_message.call_args.args[1]
    p=panel(store);ctx=NS(user_data={});await p.root(update(),ctx)
    nonce=ctx.user_data['admin_tools']['nonce']
    await p.callback(update(username='Intruder',data=f'a:{nonce}:alert_set:refresh_errors_off'),ctx)
    assert store.metadata('owner_alerts_refresh_errors') is None
    await p.callback(update(data=f'a:{nonce}:alert_set:refresh_errors_off'),ctx)
    assert store.metadata('owner_alerts_refresh_errors') is False
    await report_refresh_failure(source,store,bot,SourceError('HTTP 503'))
    assert bot.send_message.await_count==1 and store.metadata('pending_refresh_alert')


@pytest.mark.asyncio
async def test_concurrent_pending_delivery_sends_one_message(store):
    store.set_metadata('owner_alert_recipient',{'id':1,'username':'owner'})
    await report_refresh_failure(NS(snapshot=None),store,None,SourceError('HTTP 503'))
    bot=owner_bot()
    await asyncio.gather(deliver_refresh_alert(store,bot),deliver_refresh_alert(store,bot))
    assert bot.send_message.await_count==1
