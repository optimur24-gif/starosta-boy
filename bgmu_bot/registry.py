"""Connection metadata observer; disclosed message journaling is handled separately."""

import re

from .owner_alerts import notify_owner, deliver_refresh_alert


class RegistryObserver:
    def __init__(self, profiles, is_admin=None, admin_username=None):
        self.profiles = profiles
        self.is_admin = is_admin or (lambda update: False)
        self.admin_username = admin_username

    async def notify(self, context, kind, text):
        return await notify_owner(self.profiles,getattr(context,'bot',None),text,
                                  kind=kind,expected_username=self.admin_username)

    @staticmethod
    def group_notice(chat, actor=None):
        lines = ['👥 Новый чат подключён к боту', (chat.title or 'Без названия')[:128], f'ID: {chat.id}']
        if getattr(chat, 'username', None):
            lines.append('https://t.me/'+chat.username)
        if actor:
            lines.append('Добавил: '+('@'+actor.username if getattr(actor,'username',None) else str(actor.id)))
        return '\n'.join(lines)


    async def handle(self, update, context):
        event = getattr(update, 'my_chat_member', None)
        if event:
            chat, new, old = event.chat, event.new_chat_member, event.old_chat_member
            present = new.status in ('member','administrator') or (new.status == 'restricted' and getattr(new,'is_member',False))
            if chat.type == 'private':
                self.profiles.contact_active(chat.id, present)
            elif chat.type in ('group','supergroup'):
                actor = event.from_user if old.status in ('left','kicked') and present else None
                new_chat = self.profiles.observe_chat(chat.id, title=chat.title, username=getattr(chat,'username',None), status=new.status,
                                           added_by=actor.id if actor else None, added_by_username=getattr(actor,'username',None))
                if new_chat and present:
                    await self.notify(context, 'groups', self.group_notice(chat, actor))
                if not present:
                    self.profiles.deactivate_chat(chat.id)
            return
        user, chat = update.effective_user, update.effective_chat
        if not user or getattr(user,'is_bot',False) or not chat:
            return
        if chat.type == 'private':
            referral = None
            text = getattr(update.effective_message, 'text', '') or ''
            match = re.fullmatch(r'/start(?:@\w+)?\s+([A-Za-z0-9_-]{1,64})', text)
            if match:
                referral = match[1]
            new_user = self.profiles.touch_contact(user.id, getattr(user,'username',None), getattr(user,'full_name',None), referral)

            if self.is_admin(update):
                self.profiles.set_metadata('owner_alert_recipient', {'id': chat.id, 'username': user.username.casefold()})
                await deliver_refresh_alert(self.profiles,getattr(context,'bot',None),expected_username=self.admin_username)
            elif new_user:
                tag = '@'+user.username if getattr(user,'username',None) else 'Тег не установлен'
                await self.notify(context, 'users', '\n'.join([
                    '👤 Новый пользователь бота', (getattr(user,'full_name',None) or 'Без имени')[:128],
                    tag, f'ID: {user.id}']))

        elif chat.type in ('group','supergroup'):
            message = update.effective_message
            if not getattr(message,'sender_chat',None):
                self.profiles.observe_member(chat.id,user)
            for joined in getattr(message,'new_chat_members',None) or ():
                self.profiles.observe_member(chat.id,joined)
            departed = getattr(message,'left_chat_member',None)
            if departed:
                self.profiles.observe_member(chat.id,departed,active=False)
            new_chat = self.profiles.observe_chat(chat.id,title=chat.title,username=getattr(chat,'username',None))
            if new_chat:
                await self.notify(context, 'groups', self.group_notice(chat))
