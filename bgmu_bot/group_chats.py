"""Shared group schedule setup, commands and private-independent preferences."""

from datetime import datetime, timedelta
from html import escape
import math
import re
import secrets

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from .commands import schedule_request
from .compact import day_label
from .presentation import TIMEZONE, render_schedule, render_week
from .source import GroupUnavailable, SourceError
from .guide import JOURNAL_GUIDE

HELP = (
    "🎓 Расписание для этого чата\n\n"
    "Администратор чата выбирает группу командой /group. Можно сразу: /group Л-101.\n"
    "Команды: сегодня · завтра · расписание недели · до конца\n"
    "Также: расписание на среду · расписание на 21.10 · сейчас · следующая пара.\n"
    "Утром в 07:30 — на сегодня; вечером в 20:00 — на завтра (Уфа).\n"
    "/notifications_on и /notifications_off — рассылка (для администратора чата).\n"
    "Чтобы видеть команды без /, бот должен быть администратором либо иметь отключённый Privacy Mode в BotFather.\n\n"
    + JOURNAL_GUIDE
)


def current_time():
    return datetime.now(TIMEZONE)


async def time_answer(source, path, kind, now):
    schedule = await source.schedule(path, now.date())
    intervals = []
    for section in schedule.sections:
        match = re.search(r"(\d{1,2}:\d{2})\s*[–—-]\s*(\d{1,2}:\d{2})", section.title)
        if not match:
            continue
        try:
            start, end = [now.replace(hour=int(value.split(':')[0]), minute=int(value.split(':')[1]), second=0, microsecond=0) for value in match.groups()]
        except ValueError:
            continue
        if end <= start:
            end += timedelta(days=1)
        intervals.append((start, end, section))
    selected = next(((start, end, section) for start, end, section in sorted(intervals, key=lambda row: row[0])
                     if (start > now if kind == 'next' else start <= now < end)), None)
    if selected:
        start, end, section = selected
        subject = escape(section.entries[0] if section.entries else 'Пара')
        if kind == 'left':
            text = f"⏳ До конца пары «{subject}» — <b>{math.ceil((end-now).total_seconds()/60)} мин.</b>"
        elif kind == 'next':
            details = section.short_entries if section.short_entries is not None else section.entries[1:]
            text = f"<b>{escape(schedule.title)}</b>\n{day_label(now)}\n\n<b>{escape(section.short_title or section.title)}</b>\n" + '\n'.join(escape(value) for value in details)
        else:
            details = section.short_entries if section.short_entries is not None else section.entries[1:]
            text = f"<b>{escape(schedule.title)}</b>\n{day_label(now)}\n\n<b>{escape(section.short_title or section.title)}</b>\n" + '\n'.join(escape(value) for value in details)
    else:
        text = '🌿 На сегодня больше пар нет.' if kind == 'next' else '🌿 Сейчас нет пары.'
    notices = schedule.display_notices if schedule.display_notices is not None else schedule.notices
    if notices:
        text += '\n\n⚠️ ' + escape(' '.join(notices))
    return text


class GroupUI:
    PAGE_SIZE = 12

    def __init__(self, source, profiles):
        self.source, self.profiles = source, profiles

    @staticmethod
    def state(context):
        return context.chat_data.setdefault('group_navigation', {})

    async def allowed(self, update, context, *, alert=False):
        user = update.effective_user
        if user is None or user.is_bot:
            allowed = False
        else:
            try:
                member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
                allowed = member.status in ('administrator', 'creator')
            except TelegramError:
                try:
                    admins = await context.bot.get_chat_administrators(update.effective_chat.id)
                    allowed = any(member.user.id == user.id for member in admins)
                except TelegramError:
                    allowed = False
        if not allowed:
            if update.callback_query:
                await update.callback_query.answer('Группу и рассылку настраивает администратор чата.', show_alert=True)
            elif alert:
                await update.effective_message.reply_text('Группу и рассылку настраивает администратор чата.')
        return allowed

    @staticmethod
    async def send(update, text, keyboard=None):
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.effective_message.reply_text(text, reply_markup=keyboard)

    async def joined(self, update, context):
        if update.effective_chat.type not in ('group', 'supergroup'):
            return
        event = update.my_chat_member
        if event:
            new, old = event.new_chat_member, event.old_chat_member
            present = new.status in ('member', 'administrator') or (new.status == 'restricted' and new.is_member)
            if not present:
                self.profiles.deactivate_chat(event.chat.id)
                context.chat_data.pop('join_prompt_sent', None)
                return
            if old.status not in ('left', 'kicked'):
                return
        else:
            members = update.effective_message.new_chat_members or ()
            if not any(member.id == context.bot.id for member in members):
                return
            # MY_CHAT_MEMBER is the canonical event; the service message is a fallback.
            if context.chat_data.get('join_prompt_sent'):
                return
        if context.chat_data.get('join_prompt_sent'):
            return
        self.profiles.register_chat(update.effective_chat.id)
        if update.effective_message:
            await update.effective_message.reply_text(HELP)
        else:
            await context.bot.send_message(update.effective_chat.id, HELP)
        await self.prefixes(update, context)
        context.chat_data['join_prompt_sent'] = True

    async def prefixes(self, update, context):
        try:
            options = await self.source.group_prefixes()
        except SourceError:
            await self.send_safe(update, context, '⚠️ Сейчас не удалось получить группы с сайта. Администратор может повторить /group позже.')
            return
        state = self.state(context)
        state.update(stage='prefix', options=options, page=0, nonce=secrets.token_hex(4))
        await self.render(update, context)

    async def send_safe(self, update, context, text, keyboard=None):
        if update.effective_message:
            await self.send(update, text, keyboard)
        else:
            await context.bot.send_message(update.effective_chat.id, text, reply_markup=keyboard)

    async def render(self, update, context):
        state = self.state(context)
        options, stage = state['options'], state['stage']
        count = len(options)
        page_size = 36 if stage == 'prefix' else self.PAGE_SIZE
        state['page'] = min(max(0, state.get('page', 0)), max(0, (count-1)//page_size))
        start = state['page'] * page_size
        state['nonce'] = secrets.token_hex(4)
        key = lambda action, value='': f"g:{state['nonce']}:{action}:{value}"
        rows = []
        buttons = []
        for i in range(start, min(start+page_size, count)):
            item = options[i]
            label = item if stage == 'prefix' else (' · '.join((item.labels[5], item.labels[3], item.labels[4])) if stage == 'group' else item.label)
            buttons.append(Button(label, callback_data=key('pick', str(i))))
        columns = 3 if stage == 'prefix' else 1
        rows.extend(buttons[index:index+columns] for index in range(0, len(buttons), columns))
        pages = []
        if start:
            pages.append(Button('‹ Назад', callback_data=key('page', str(state['page']-1))))
        if start+page_size < count:
            pages.append(Button('Далее ›', callback_data=key('page', str(state['page']+1))))
        if pages:
            rows.append(pages)
        if stage != 'prefix':
            rows.append([Button('← Все типы групп', callback_data=key('prefix'))])
        titles = {'prefix': '🎓 Какую группу закрепить за чатом?\nВыбери обозначение группы, как на сайте:',
                  'group': '👥 Выбери полный номер группы:', 'subgroup': '👥 Выбери подгруппу:'}
        text = titles[stage]
        if not count:
            text += '\nГруппы не найдены. Попробуй /group Л-101.'
        text += '\n\nНастройку подтверждает администратор чата.\nБыстрый выбор: /group Л-101'
        await self.send_safe(update, context, text, InlineKeyboardMarkup(rows))

    async def configure(self, update, context):
        if not await self.allowed(update, context, alert=True):
            return
        self.profiles.register_chat(update.effective_chat.id)
        query = ' '.join(getattr(context, 'args', ()) or ())
        if not query:
            await self.prefixes(update, context)
            return
        try:
            options = await self.source.search_groups(query)
        except SourceError:
            await self.send_safe(update, context, '⚠️ Сайт временно недоступен. Повтори /group позже.')
            return
        self.state(context).update(stage='group', options=options, page=0)
        await self.render(update, context)

    async def callback(self, update, context):
        if not await self.allowed(update, context):
            return
        parts = (update.callback_query.data or '').split(':', 3)
        state = self.state(context)
        if len(parts) != 4 or parts[1] != state.get('nonce'):
            await update.callback_query.answer('Меню устарело. Открой /group.', show_alert=True)
            return
        await update.callback_query.answer()
        _, _, action, value = parts
        try:
            if action == 'prefix':
                await self.prefixes(update, context)
            elif action == 'page' and value.isdigit():
                state['page'] = int(value)
                await self.render(update, context)
            elif action == 'pick' and value.isdigit() and int(value) < len(state['options']):
                item = state['options'][int(value)]
                if state['stage'] == 'prefix':
                    state.update(stage='group', options=await self.source.groups_with_prefix(item), page=0)
                    await self.render(update, context)
                elif state['stage'] == 'group':
                    menu = await self.source.menu(item.path)
                    if menu is None:
                        await self.finish(update, context, item.path, item.labels)
                    else:
                        state.update(stage='subgroup', path=item.path, labels=item.labels, options=menu.choices, page=0)
                        await self.render(update, context)
                else:
                    await self.finish(update, context, state['path']+(item.key,), state['labels']+(item.label,))
        except SourceError:
            await self.send_safe(update, context, '⚠️ Сайт временно недоступен. Прежняя группа сохранена. Повтори /group позже.')

    async def finish(self, update, context, path, labels):
        self.profiles.save_chat(update.effective_chat.id, path, labels)
        self.state(context).clear()
        delivery = 'Рассылка: 07:30 на сегодня, 20:00 на завтра (Уфа).' if update.effective_chat.id in self.profiles.subscribed_chats() else 'Рассылка отключена. Включить: /notifications_on.'
        await self.send_safe(update, context, f'✅ За чатом закреплена {labels[5]}' + (f' · {labels[6]}' if len(labels)>6 else '') +
                             '\n\nНапиши: сегодня · завтра · расписание недели · до конца\n' + delivery)

    async def notifications(self, update, context):
        if not await self.allowed(update, context, alert=True):
            return
        self.profiles.register_chat(update.effective_chat.id)
        enabled = update.effective_message.text.split('@', 1)[0].split(' ', 1)[0] == '/notifications_on'
        self.profiles.chat_notifications(update.effective_chat.id, enabled)
        await update.effective_message.reply_text('🔔 Рассылка включена. Группа выбирается через /group.' if enabled else '🔕 Рассылка отключена. Команды расписания продолжают работать.')

    async def announcements(self,update,context):
        if not await self.allowed(update,context,alert=True):
            return
        enabled = update.effective_message.text.split('@',1)[0].split(' ',1)[0] == '/announcements_on'
        self.profiles.group_announcements(update.effective_chat.id,enabled)
        await update.effective_message.reply_text('Сервисные объявления владельца бота включены.' if enabled else 'Сервисные объявления владельца бота отключены. Расписание продолжает работать.')

    async def help(self, update, context):
        await update.effective_message.reply_text(HELP)

    async def text(self, update, context):
        user = update.effective_user
        if not user or user.is_bot:
            return
        text = update.effective_message.text or ''
        # Ignore slash commands explicitly addressed to another bot in the same chat.
        head = text.split(' ', 1)[0]
        if head.startswith('/') and '@' in head and head.split('@', 1)[1].casefold() != context.bot.username.casefold():
            return
        try:
            request = schedule_request(text, current_time().date())
        except ValueError as error:
            await update.effective_message.reply_text(str(error))
            return
        if request is None:
            return
        saved = self.profiles.chat_group(update.effective_chat.id)
        if saved is None:
            await update.effective_message.reply_text('🎓 Администратору чата нужно выбрать группу: /group или /group Л-101.')
            return
        try:
            match = await self.source.resolve_group(saved.path)
            if match.path != saved.path or match.labels != saved.labels:
                self.profiles.save_chat(update.effective_chat.id, match.path, match.labels)
            if request.kind == 'week':
                messages = render_week(await self.source.week(match.path, request.day))
            elif request.kind == 'day':
                messages = render_schedule(await self.source.schedule(match.path, request.day))
            else:
                messages = [await time_answer(self.source, match.path, request.kind, current_time())]
            for message in messages:
                await update.effective_message.reply_text(message, parse_mode=ParseMode.HTML)
        except GroupUnavailable:
            await update.effective_message.reply_text('Группа отсутствует в новом периоде. Администратор может выбрать её заново: /group.')
        except SourceError:
            await update.effective_message.reply_text('⚠️ Не удалось получить расписание с сайта. Попробуй позже.')

    async def migrate(self, update, context):
        message = update.effective_message
        if message.migrate_to_chat_id:
            self.profiles.migrate_chat(message.chat_id, message.migrate_to_chat_id)
        elif message.migrate_from_chat_id:
            self.profiles.migrate_chat(message.migrate_from_chat_id, message.chat_id)
