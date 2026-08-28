import os
import re
import random
import shutil
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= ВСТАВЬ СЮДА СВОИ ДАННЫЕ =================
# 1. Вставь сюда НОВЫЙ токен от BotFather (после того как отзовешь старый)
BOT_TOKEN = "8975598877:AAEpVo2aAN76aIuuhPtBbcRLAEbKaHMb_wY"

# 2. Вставь сюда ID группы из адресной строки (например: "-1001234567890")
GROUP_CHAT_ID = -1000000000000 
# ============================================================

# Твой ID уже вписан
MAIN_ADMIN_ID = 1153815035  
# Часовой пояс МСК+2 (Екатеринбург)
TIMEZONE = ZoneInfo("Asia/Yekaterinburg")

DB_PATH = "bot_data.db"
BACKUP_DIR = "backups"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
scheduler = None

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)''')
    c.execute(f'INSERT OR IGNORE INTO admins (user_id) VALUES ({MAIN_ADMIN_ID})')
    c.execute('''CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, time TEXT, subject TEXT, room TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_members (user_id INTEGER PRIMARY KEY, username TEXT)''')
    conn.commit()
    conn.close()

def backup_database(reason: str = "auto"):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        timestamp = get_now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(BACKUP_DIR, f"bot_data_{timestamp}_{reason}.db")
        shutil.copy2(DB_PATH, backup_path)
        backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
        while len(backups) > 20:
            os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")

def is_admin(user_id: int) -> bool:
    conn = get_db()
    res = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return res is not None

def get_now() -> datetime:
    return datetime.now(TIMEZONE)

DAYS_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS_RU = {"mon": "Понедельник", "tue": "Вторник", "wed": "Среда", "thu": "Четверг", "fri": "Пятница", "sat": "Суббота", "sun": "Воскресенье"}
WEEKDAY_MAP = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}

def parse_duration(text: str) -> int | None:
    match = re.search(r'(\d+)\s*(s|с|m|м|h|ч|d|д)?', text.lower())
    if not match: return None
    val = int(match.group(1))
    unit = match.group(2) or 'm'
    units = {'s': 1, 'с': 1, 'm': 60, 'м': 60, 'h': 3600, 'ч': 3600, 'd': 86400, 'д': 86400}
    return val * units.get(unit, 60)

def parse_time_range(time_str: str):
    try:
        parts = time_str.replace('–', '-').split('-')
        start = datetime.strptime(parts[0].strip(), "%H:%M").time()
        end = datetime.strptime(parts[1].strip(), "%H:%M").time() if len(parts) > 1 else None
        return start, end
    except Exception: return None, None

def parse_schedule_text(text: str) -> dict:
    days_keywords = {"понедельник": "mon", "вторник": "tue", "среда": "wed", "четверг": "thu", "пятница": "fri", "суббота": "sat", "воскресенье": "sun"}
    result = {}
    current_day = None
    for line in text.strip().split('\n'):
        line_lower = line.lower().strip()
        if not line_lower: continue
        matched_day = False
        for kw, code in days_keywords.items():
            if kw in line_lower:
                current_day = code
                if current_day not in result: result[current_day] = []
                matched_day = True
                break
        if matched_day: continue
        if current_day is None: continue
        m = re.search(r'(?:\d+[\.\)]\s*)?(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s+(.+?)\s*\(([^)]+)\)', line)
        if m:
            result[current_day].append({"time": f"{m.group(1)}-{m.group(2)}", "subject": m.group(3).strip(), "room": m.group(4).strip()})
            continue
        m = re.search(r'(?:\d+[\.\)]\s*)?(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s+(.+)', line)
        if m:
            result[current_day].append({"time": f"{m.group(1)}-{m.group(2)}", "subject": m.group(3).strip(), "room": "—"})
    return result

def get_classes_for_day(day_code: str) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM schedule WHERE day = ? ORDER BY time", (day_code,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_full_schedule() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT * FROM schedule ORDER BY time").fetchall()
    conn.close()
    result = {d: [] for d in DAYS_ORDER}
    for r in rows:
        if r['day'] in result: result[r['day']].append(dict(r))
    for d in result: result[d].sort(key=lambda x: x['time'])
    return result

async def safe_send(chat_id, text, parse_mode=None):
    try:
        await bot.send_message(chat_id, text, parse_mode=parse_mode)
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        return False

async def send_reminder(chat_id: int, text: str):
    await safe_send(chat_id, f"⏰ <b>НАПОМИНАНИЕ!</b>\n\n{text}", parse_mode="HTML")

class ParseText(StatesGroup): waiting_text = State()
class AddAnnouncement(StatesGroup): waiting_reason = State(); waiting_text = State()
class AddNewAdmin(StatesGroup): waiting_id = State()
class ConfirmDeleteDay(StatesGroup): waiting_confirm = State()
class ConfirmDeleteAll(StatesGroup): waiting_confirm = State()

def get_main_menu():
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="📅 Расписание", callback_data="menu_schedule"), InlineKeyboardButton(text="📢 Объявления", callback_data="menu_announce"))
    b.row(InlineKeyboardButton(text="⚙️ Управление", callback_data="menu_admin"))
    return b.as_markup()

def get_days_menu():
    b = InlineKeyboardBuilder()
    for name, code in [("Понедельник","mon"),("Вторник","tue"),("Среда","wed"),("Четверг","thu"),("Пятница","fri"),("Суббота","sat")]:
        b.row(InlineKeyboardButton(text=name, callback_data=f"day_{code}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить ВСЁ расписание", callback_data="delete_all_schedule"))
    b.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    return b.as_markup()

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("Этот бот только для старосты и замов.")
    now = get_now()
    days_ru = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
    months_ru = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
    date_str = f"*Сегодня:* {days_ru[now.weekday()]}, {now.day} {months_ru[now.month-1]}"
    await message.answer(f"👋 Главное меню старосты.\n\n{date_str}", reply_markup=get_main_menu(), parse_mode="Markdown")

@router.callback_query(F.data == "main_menu")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("👋 Главное меню старосты. Выбирай:", reply_markup=get_main_menu())

@router.callback_query(F.data == "menu_schedule")
async def menu_schedule(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("📅 Выберите день недели:", reply_markup=get_days_menu())

@router.callback_query(F.data.startswith("day_"))
async def show_day(call: CallbackQuery, state: FSMContext):
    await state.clear()
    day_code = call.data.split("_")[1]
    day_name = DAYS_RU.get(day_code, day_code)
    classes = get_classes_for_day(day_code)
    b = InlineKeyboardBuilder()
    text = f"📅 *{day_name}*\n\n"
    if classes:
        for cls in classes:
            text += f"⏰ {cls['time']} | {cls['subject']} | 🚪 {cls['room']}\n"
            b.row(InlineKeyboardButton(text=f"🗑 Удалить {cls['time']}", callback_data=f"del_{cls['id']}"))
    else: text += "Пусто. Добавьте пары!\n"
    b.row(InlineKeyboardButton(text="📥 Загрузить из текста", callback_data=f"parse_text_{day_code}"))
    b.row(InlineKeyboardButton(text="🗑 Удалить ВЕСЬ день", callback_data=f"delete_day_{day_code}"))
    b.row(InlineKeyboardButton(text="🔙 К дням", callback_data="menu_schedule"))
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("parse_text_"))
async def start_parse_text(call: CallbackQuery, state: FSMContext):
    target_day = call.data.split("_")[2]
    await state.update_data(target_day=target_day)
    await call.message.edit_text("📥 Скинь мне текст расписания.\nМожно сразу несколько дней:\n`Понедельник`\n`1. 13:00-14:30 Математика (А-101)`", parse_mode="Markdown")
    await state.set_state(ParseText.waiting_text)

@router.message(ParseText.waiting_text)
async def process_parse_text(message: Message, state: FSMContext):
    data = await state.get_data()
    target_day = data.get('target_day')
    parsed = parse_schedule_text(message.text)
    if not parsed:
        classes = []
        for line in message.text.strip().split('\n'):
            m = re.search(r'(?:\d+[\.\)]\s*)?(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})\s+(.+?)(?:\s*\(([^)]+)\))?$', line.strip())
            if m: classes.append({"time": f"{m.group(1)}-{m.group(2)}", "subject": m.group(3).strip(), "room": m.group(4).strip() if m.group(4) else "—"})
        if classes: parsed = {target_day: classes}
        else:
            await message.answer("❌ Не удалось распознать пары. Данные НЕ удалены.")
            await state.clear(); return
    backup_database("before_parse")
    conn = get_db()
    total = 0
    for day_code, day_classes in parsed.items():
        conn.execute("DELETE FROM schedule WHERE day = ?", (day_code,))
        for cls in day_classes:
            conn.execute("INSERT INTO schedule (day, time, subject, room) VALUES (?, ?, ?, ?)", (day_code, cls['time'], cls['subject'], cls['room']))
            total += 1
    conn.commit(); conn.close()
    await state.clear()
    days_added = ", ".join([DAYS_RU.get(d, d) for d in parsed.keys()])
    await message.answer(f"✅ Обновлено!\nДни: {days_added}\nПар: {total}\n💾 Бэкап создан.", reply_markup=get_days_menu())

@router.callback_query(F.data.startswith("del_"))
async def delete_class(call: CallbackQuery):
    class_id = int(call.data.split("_")[1])
    backup_database("before_del_class")
    conn = get_db()
    conn.execute("DELETE FROM schedule WHERE id = ?", (class_id,))
    conn.commit(); conn.close()
    await call.answer("Удалено!"); await show_day(call)

@router.callback_query(F.data.startswith("delete_day_"))
async def confirm_delete_day(call: CallbackQuery, state: FSMContext):
    day_code = call.data.split("_")[2]
    day_name = DAYS_RU.get(day_code, day_code)
    await state.update_data(day_code=day_code); await state.set_state(ConfirmDeleteDay.waiting_confirm)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=f"✅ ДА, удалить {day_name}", callback_data=f"confirm_del_day_{day_code}"), InlineKeyboardButton(text="❌ Отмена", callback_data="menu_schedule"))
    await call.message.edit_text(f"⚠️ Удалить ВСЕ пары за *{day_name}*?", reply_markup=b.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("confirm_del_day_"))
async def execute_delete_day(call: CallbackQuery, state: FSMContext):
    day_code = call.data.split("_")[3]
    backup_database(f"before_del_day_{day_code}")
    conn = get_db(); conn.execute("DELETE FROM schedule WHERE day = ?", (day_code,)); conn.commit(); conn.close()
    await state.clear(); await call.answer("День удалён.")
    await call.message.edit_text(f"✅ День {DAYS_RU.get(day_code, '')} очищен.", reply_markup=get_days_menu())

@router.callback_query(F.data == "delete_all_schedule")
async def confirm_delete_all(call: CallbackQuery, state: FSMContext):
    await state.set_state(ConfirmDeleteAll.waiting_confirm)
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ ДА, удалить ВСЁ", callback_data="confirm_del_all"), InlineKeyboardButton(text="❌ Отмена", callback_data="menu_schedule"))
    await call.message.edit_text("⚠️ Удалить ВСЁ расписание за ВСЕ дни?", reply_markup=b.as_markup())

@router.callback_query(F.data == "confirm_del_all")
async def execute_delete_all(call: CallbackQuery, state: FSMContext):
    backup_database("before_del_all")
    conn = get_db(); conn.execute("DELETE FROM schedule"); conn.commit(); conn.close()
    await state.clear(); await call.answer("Всё удалено.")
    await call.message.edit_text("✅ Расписание полностью очищено.", reply_markup=get_days_menu())

@router.callback_query(F.data == "menu_announce")
async def menu_announce(call: CallbackQuery, state: FSMContext):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🔄 Изменение", callback_data="ann_reason_change"), InlineKeyboardButton(text="❌ Отмена", callback_data="ann_reason_cancel"))
    b.row(InlineKeyboardButton(text="📢 Сбор", callback_data="ann_reason_meeting"), InlineKeyboardButton(text="✍️ Своя", callback_data="ann_reason_custom"))
    b.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    await call.message.edit_text("📢 Выберите причину:", reply_markup=b.as_markup())

@router.callback_query(F.data.startswith("ann_reason_"))
async def set_reason(call: CallbackQuery, state: FSMContext):
    reasons = {"change": "Изменение расписания", "cancel": "Отмена пары", "meeting": "Общий сбор", "custom": "Своя причина"}
    reason = reasons.get(call.data.replace("ann_reason_", ""), "")
    await state.update_data(reason=reason)
    await call.message.edit_text(f"Причина: *{reason}*\n\nВведите текст:", parse_mode="Markdown")
    await state.set_state(AddAnnouncement.waiting_text)

@router.message(AddAnnouncement.waiting_text)
async def send_announcement(message: Message, state: FSMContext):
    data = await state.get_data()
    text = f"📢 *ВНИМАНИЕ!*\n\n📌 {data['reason']}\n\n{message.text}"
    ok = await safe_send(GROUP_CHAT_ID, text, parse_mode="Markdown")
    await message.answer("✅ Отправлено!" if ok else "❌ Ошибка.")
    await state.clear()

@router.callback_query(F.data == "menu_admin")
async def menu_admin(call: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="➕ Добавить зама", callback_data="add_admin"))
    b.row(InlineKeyboardButton(text="💾 Создать бэкап", callback_data="manual_backup"))
    b.row(InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu"))
    conn = get_db(); admins = conn.execute("SELECT user_id FROM admins").fetchall(); conn.close()
    text = "⚙️ Админы:\n" + "\n".join([f"- `{a['user_id']}`" for a in admins])
    await call.message.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data == "add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("Перешлите сообщение от зама или введите его ID:")
    await state.set_state(AddNewAdmin.waiting_id)

@router.message(AddNewAdmin.waiting_id)
async def add_admin_process(message: Message, state: FSMContext):
    new_id = None
    if message.forward_from: new_id = message.forward_from.id
    elif message.text and message.text.isdigit(): new_id = int(message.text)
    if new_id:
        conn = get_db(); conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_id,)); conn.commit(); conn.close()
        await message.answer(f"✅ Зам {new_id} добавлен!", reply_markup=get_main_menu())
    else: await message.answer("Не удалось определить ID.")
    await state.clear()

@router.callback_query(F.data == "manual_backup")
async def manual_backup(call: CallbackQuery):
    backup_database("manual")
    await call.answer("✅ Бэкап создан!")

@router.message(F.chat.id == GROUP_CHAT_ID)
async def group_commands(message: Message):
    try:
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO group_members (user_id, username) VALUES (?, ?)", (message.from_user.id, message.from_user.username or message.from_user.first_name))
        conn.commit(); conn.close()
        if not is_admin(message.from_user.id): return
        text_original = message.text.strip() if message.text else ""
        text_lower = text_original.lower()
        if not text_lower: return
        now = get_now()
        current_time = now.time()
        today_code = WEEKDAY_MAP.get(now.weekday())

        if text_lower in ["расписание на сегодня", "что сегодня", "пары сегодня", "сегодня"]:
            await send_schedule_to_chat(message.chat.id, today_code, "сегодня")
        elif text_lower in ["расписание на завтра", "что завтра", "пары завтра", "завтра"]:
            tomorrow_code = WEEKDAY_MAP.get((now + timedelta(days=1)).weekday())
            await send_schedule_to_chat(message.chat.id, tomorrow_code, "завтра")
        elif text_lower.startswith("расписание на "):
            day_name = text_lower.replace("расписание на ", "").strip()
            days_map = {"понедельник":"mon","вторник":"tue","среда":"wed","четверг":"thu","пятница":"fri","суббота":"sat","воскресенье":"sun"}
            if day_name in days_map: await send_schedule_to_chat(message.chat.id, days_map[day_name], day_name)
        elif text_lower in ["расписание на неделю", "вся неделя", "неделя"]: await send_week_schedule(message.chat.id)
        elif text_lower in ["экспорт расписания", "экспорт"]: await export_schedule(message.chat.id)
        elif text_lower in ["следующая пара", "что дальше", "дальше"]: await show_next_class(message.chat.id, today_code, current_time)
        elif text_lower in ["какая пара сейчас", "текущая пара", "сейчас"]: await show_current_class(message.chat.id, today_code, current_time)
        elif text_lower == "кал" or text_lower.startswith("кал "):
            msg_text = text_original[4:].strip()
            if msg_text: await safe_send(message.chat.id, f"📢 <b>ВНИМАНИЕ!</b>\n\n{msg_text}", parse_mode="HTML")
            else: await safe_send(message.chat.id, "📢 <b>ВНИМАНИЕ ВСЕЙ ГРУППЕ!</b> 📢", parse_mode="HTML")
        elif text_lower in ["тег всех", "внимание всем", "сбор", "всем внимание"]: await tag_everyone(message.chat.id)
        elif text_lower.startswith("опрос "):
            question = text_original[6:].strip()
            try: await bot.send_poll(chat_id=message.chat.id, question=question, options=["Иду", "Не иду", "Опоздаю", "Дистант"], is_anonymous=False)
            except Exception as e: await message.answer(f"❌ Ошибка опроса: {e}")
        elif text_lower in ["рандом", "кто", "выбрать", "кто дежурный"]:
            conn = get_db(); members = conn.execute("SELECT user_id, username FROM group_members").fetchall(); conn.close()
            if not members: return await message.answer("Некого выбирать.")
            chosen = random.choice(members)
            await safe_send(message.chat.id, f"🎲 Мой выбор: <a href='tg://user?id={chosen['user_id']}'>{chosen['username'] or 'Участник'}</a>!", parse_mode="HTML")
        elif text_lower.startswith("напомни через "): await handle_reminder(message, text_original)
        elif text_lower == "закрепи" and message.reply_to_message:
            try: await message.reply_to_message.pin(disable_notification=False); await message.answer("📌 Закреплено.")
            except Exception as e: await message.answer(f"❌ Не могу: {e}")
        elif text_lower == "открепи" and message.reply_to_message:
            try: await message.reply_to_message.unpin(); await message.answer("📌 Откреплено.")
            except Exception as e: await message.answer(f"❌ Не могу: {e}")
        elif text_lower in ["статистика", "стата", "стат"]: await show_stats(message.chat.id)
        elif text_lower in ["помощь", "команды", "помоги"]: await show_help(message.chat.id)
        elif text_lower.startswith("мут") and message.reply_to_message:
            target = message.reply_to_message.from_user
            if not target: return await message.answer("❌ Не вижу юзера.")
            duration_str = text_lower.replace("мут", "").strip()
            seconds = parse_duration(duration_str)
            if not seconds: return await message.answer("⚠️ Укажи время! <code>мут 20m</code>", parse_mode="HTML")
            try:
                await bot.restrict_chat_member(message.chat.id, target.id, permissions=types.ChatPermissions(can_send_messages=False), until_date=now.timestamp() + seconds)
                await message.answer(f"🔇 {target.full_name} в муте на {duration_str}.")
            except Exception as e: await message.answer(f"❌ Ошибка: {e}")
        elif text_lower == "бан" and message.reply_to_message:
            target = message.reply_to_message.from_user
            if not target: return await message.answer("❌ Не вижу юзера.")
            try: await bot.ban_chat_member(message.chat.id, target.id); await message.answer(f"🔨 {target.full_name} забанен.")
            except Exception as e: await message.answer(f"❌ Ошибка: {e}")
        elif text_lower == "удали" and message.reply_to_message:
            try: await message.reply_to_message.delete(); await message.delete()
            except Exception as e: await message.answer(f"❌ Не могу: {e}")
    except Exception as e: logger.error(f"Ошибка в group_commands: {e}")

async def send_schedule_to_chat(chat_id, day_code, day_name):
    classes = get_classes_for_day(day_code)
    if classes:
        text = f"📅 <b>Расписание на {day_name.capitalize()}:</b>\n\n" + "\n".join([f"⏰ {c['time']} | {c['subject']} | 🚪 {c['room']}" for c in classes])
    else: text = f"🎉 На {day_name.capitalize()} пар нет!"
    await safe_send(chat_id, text, parse_mode="HTML")

async def send_week_schedule(chat_id):
    schedule = get_full_schedule()
    text = "📅 <b>РАСПИСАНИЕ НА НЕДЕЛЮ:</b>\n\n"
    for day_code in DAYS_ORDER:
        classes = schedule.get(day_code, [])
        text += f"<b>{DAYS_RU.get(day_code, day_code)}</b>\n"
        if classes: text += "\n".join([f"  ⏰ {c['time']} | {c['subject']} | 🚪 {c['room']}" for c in classes]) + "\n"
        else: text += "  — пар нет —\n"
        text += "\n"
    if len(text) > 4000:
        for i in range(0, len(text), 4000): await safe_send(chat_id, text[i:i+4000], parse_mode="HTML")
    else: await safe_send(chat_id, text, parse_mode="HTML")

async def export_schedule(chat_id):
    schedule = get_full_schedule()
    text = ""
    for day_code in DAYS_ORDER:
        classes = schedule.get(day_code, [])
        if not classes: continue
        text += f"{DAYS_RU.get(day_code, day_code)}\n"
        for i, c in enumerate(classes, 1): text += f"{i}. {c['time']} {c['subject']} ({c['room']})\n"
        text += "\n"
    if not text.strip(): text = "Расписание пустое."
    await safe_send(chat_id, f"📋 <b>Экспорт:</b>\n\n{text}", parse_mode="HTML")

async def show_next_class(chat_id, today_code, current_time):
    classes = get_classes_for_day(today_code)
    for c in classes:
        start, _ = parse_time_range(c['time'])
        if start and start > current_time:
            await safe_send(chat_id, f"⏭ <b>Следующая:</b>\n🕐 {c['time']}\n📚 {c['subject']}\n🚪 {c['room']}", parse_mode="HTML"); return
    await safe_send(chat_id, "🎉 На сегодня больше пар нет!")

async def show_current_class(chat_id, today_code, current_time):
    classes = get_classes_for_day(today_code)
    for c in classes:
        start, end = parse_time_range(c['time'])
        if start and end and start <= current_time <= end:
            await safe_send(chat_id, f"📖 <b>Сейчас:</b>\n🕐 {c['time']}\n📚 {c['subject']}\n🚪 {c['room']}", parse_mode="HTML"); return
    await safe_send(chat_id, "😴 Сейчас нет пары.")

async def tag_everyone(chat_id):
    conn = get_db(); members = conn.execute("SELECT user_id, username FROM group_members").fetchall(); conn.close()
    if not members: return await safe_send(chat_id, "Список пуст.")
    header = "📢 <b>ВНИМАНИЕ ВСЕЙ ГРУППЕ!</b>\n\n"
    tags = ""
    for m in members:
        tag = f"👤 <a href='tg://user?id={m['user_id']}'>{m['username'] or 'Участник'}</a>\n"
        if len(header + tags + tag) > 3900:
            await safe_send(chat_id, header + tags, parse_mode="HTML"); tags = ""
        tags += tag
    if tags: await safe_send(chat_id, header + tags, parse_mode="HTML")

async def handle_reminder(message: Message, text_original: str):
    global scheduler
    match = re.match(r'напомни через (\d+\s*(?:s|с|m|м|h|ч|d|д)?)\s+(.+)', text_original, re.IGNORECASE)
    if not match: return await message.answer("⚠️ Формат: <code>напомни через 20m Текст</code>", parse_mode="HTML")
    time_str = match.group(1); reminder_text = match.group(2).strip()
    seconds = parse_duration(time_str)
    if not seconds: return await message.answer("❌ Не понял время.")
    run_time = get_now() + timedelta(seconds=seconds)
    scheduler.add_job(send_reminder, 'date', run_date=run_time, args=[message.chat.id, reminder_text])
    await message.answer(f"⏰ Напоминание через {time_str}:\n«{reminder_text}»")

async def show_stats(chat_id):
    conn = get_db()
    members_count = conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0]
    classes_count = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
    admins_count = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    backups_count = len([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')]) if os.path.exists(BACKUP_DIR) else 0
    conn.close()
    text = (f"📊 <b>Статистика:</b>\n\n👥 Участников: <b>{members_count}</b>\n📚 Пар: <b>{classes_count}</b>\n"
            f"👑 Админов: <b>{admins_count}</b>\n💾 Бэкапов: <b>{backups_count}</b>\n"
            f"🕐 Время: <b>{get_now().strftime('%H:%M:%S')}</b>\n📅 Сегодня: <b>{DAYS_RU.get(WEEKDAY_MAP[get_now().weekday()], '?')}</b>")
    await safe_send(chat_id, text, parse_mode="HTML")

async def show_help(chat_id):
    text = ("🤖 <b>Команды (без /):</b>\n\n📅 <b>Расписание:</b>\n• <code>расписание на сегодня</code> / <code>завтра</code>\n"
            "• <code>расписание на [день]</code>\n• <code>расписание на неделю</code>\n• <code>следующая пара</code>\n"
            "• <code>какая пара сейчас</code>\n• <code>экспорт расписания</code>\n\n"
            "📢 <b>Объявления:</b>\n• <code>кал</code> / <code>кал текст</code>\n• <code>тег всех</code>\n• <code>опрос Текст</code>\n\n"
            "🛠 <b>Инструменты:</b>\n• <code>напомни через 20m Текст</code>\n• <code>закрепи</code> / <code>открепи</code>\n"
            "• <code>рандом</code>\n• <code>статистика</code>\n\n"
            "🛡 <b>Модерация (ответом):</b>\n• <code>мут 20m</code> / <code>мут 30s</code> / <code>мут 2h</code>\n• <code>бан</code> / <code>удали</code>")
    await safe_send(chat_id, text, parse_mode="HTML")

async def send_daily_schedule():
    today_code = WEEKDAY_MAP.get(get_now().weekday())
    if not today_code or today_code == "sun": return
    classes = get_classes_for_day(today_code)
    day_name = DAYS_RU.get(today_code, "")
    conn = get_db()
    old_msg = conn.execute("SELECT value FROM settings WHERE key='last_schedule_msg_id'").fetchone()
    if old_msg and old_msg['value']:
        try: await bot.delete_message(GROUP_CHAT_ID, int(old_msg['value']))
        except Exception: pass
    if classes:
        text = f"☀️ <b>Доброе утро! Расписание на {day_name}:</b>\n\n" + "\n".join([f"⏰ {c['time']} | {c['subject']} | 🚪 {c['room']}" for c in classes])
    else: text = f"☀️ <b>Доброе утро!</b> 🎉 Сегодня ({day_name}) пар нет!"
    try:
        sent = await bot.send_message(GROUP_CHAT_ID, text, parse_mode="HTML")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_schedule_msg_id', ?)", (str(sent.message_id),)); conn.commit()
    except Exception as e: logger.error(f"Ошибка утра: {e}")
    conn.close()

async def main():
    global scheduler
    init_db()
    if os.path.exists(DB_PATH): backup_database("startup")
    scheduler = AsyncIOScheduler(timezone="Asia/Yekaterinburg")
    scheduler.add_job(send_daily_schedule, 'cron', hour=7, minute=30)
    scheduler.add_job(backup_database, 'cron', hour=2, minute=0)
    scheduler.start()
    logger.info(f"✅ Бот запущен! Время: {get_now().strftime('%Y-%m-%d %H:%M:%S')}")
    await dp.start_polling(bot)

if __name__ == '__main__':
    main()
