import os
from bgmu_bot.compact import day_label
import re
import html
import random
import shutil
import sqlite3
import logging
import asyncio
import sys
import hashlib
from contextlib import contextmanager
from html.parser import HTMLParser
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
    # Защита: бот не падает, если python-dotenv временно не установлен
    def load_dotenv(*args, **kwargs):
        return False

from aiogram import Dispatcher, F, Router
from .policy import ScopedBot as Bot, ScopeMiddleware, EXPECTED_BOT_ID, GROUP_CHAT_ID, RequestCooldownMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, CallbackQuery, Message, ChatPermissions, User, ErrorEvent, BotCommand, MessageOriginUser
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ================= НАСТРОЙКИ (с защитой от падения) =================
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)


def required_int_env(name: str, *, positive: bool = False) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
        if value == 0 or (positive and value < 0):
            raise ValueError
        return value
    except ValueError as exc:
        raise RuntimeError(f"Укажите корректный {name} в .env или переменных окружения.") from exc


# ================== ВСТАВЬТЕ ТОКЕН БОТА СЮДА (если не используете .env) ==================
# Пример: BOT_TOKEN = "TOKEN_FROM_SECURE_ENVIRONMENT"
# Сейчас оставьте пустым, если токен хранится в файле .env
BOT_TOKEN = os.getenv("RESTRICTED_BOT_TOKEN", "").strip()
# =====================================================================================
if not BOT_TOKEN:
    raise RuntimeError("Не указан RESTRICTED_BOT_TOKEN")
if not BOT_TOKEN.split(":", 1)[0].isdigit() or int(BOT_TOKEN.split(":", 1)[0]) != EXPECTED_BOT_ID:
    raise RuntimeError("Управление разрешено только исходному боту из прикреплённого файла")
MAIN_ADMIN_ID = 1153815035
try:
    TIMEZONE = ZoneInfo(os.getenv("TZ_NAME", "Asia/Yekaterinburg"))
except (ValueError, KeyError) as exc:
    raise RuntimeError("Неверный TZ_NAME. Пример: Asia/Yekaterinburg.") from exc
DATA_DIR = os.getenv("DATA_DIR", ".data/restricted")
# ===============================================================================

DB_PATH = os.path.join(DATA_DIR, "bot_data.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[RotatingFileHandler(os.path.join(DATA_DIR, "bot.log"), maxBytes=5_000_000,
                                 backupCount=3, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
from bgmu_bot.__main__ import RedactingFormatter
for handler in logging.getLogger().handlers:
    handler.setFormatter(RedactingFormatter([BOT_TOKEN]))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
common_router = Router()
admin_router = Router()
group_router = Router()
scheduler: AsyncIOScheduler | None = None
START_TIME = datetime.now(TIMEZONE)
SCHEDULE_FEED = None

from bgmu_bot.shared_schedule import SharedSchedule, SharedScheduleMiddleware


DAYS_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS_RU = {"mon": "Понедельник", "tue": "Вторник", "wed": "Среда", "thu": "Четверг",
           "fri": "Пятница", "sat": "Суббота", "sun": "Воскресенье"}
DAYS_KW = {"понедельник": "mon", "вторник": "tue", "среда": "wed", "четверг": "thu",
           "пятница": "fri", "суббота": "sat", "воскресенье": "sun",
           "пн": "mon", "вт": "tue", "ср": "wed", "чт": "thu", "пт": "fri", "сб": "sat", "вс": "sun"}
DAYS_KW.update({"среду": "wed", "пятницу": "fri", "субботу": "sat"})
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]

DEFAULT_SETTINGS = {
    "morning_time": "07:30",
    "evening_enabled": "1",
    "evening_time": "20:00",
    "hw_reminder_time": "19:00",
    "class_reminder_minutes": "10",
    "public_commands": "1",
}


# ============================ БАЗА ДАННЫХ ============================
def get_db(*, live=True):
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=FULL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=15000;")
    if live and SCHEDULE_FEED is not None:
        SCHEDULE_FEED.attach(conn)
    return conn


@contextmanager
def db_session(*, live=True):
    conn = get_db(live=live)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def feature_message_allowed(message: Message, public: bool = True) -> bool:
    user = message.from_user
    if not user or user.is_bot:
        return False
    admin = is_admin(user.id)
    if message.chat.type == "private":
        return admin
    return message.chat.id == GROUP_CHAT_ID and (admin or (public and get_setting("public_commands") == "1"))


def init_db():
    conn = get_db(live=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS schedule (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, time TEXT, subject TEXT, room TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS group_members (user_id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE IF NOT EXISTS homework (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, task TEXT, deadline TEXT, created_at TEXT, created_by INTEGER);
        CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, text TEXT, run_at TEXT);
        CREATE TABLE IF NOT EXISTS links (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, date TEXT);
        CREATE TABLE IF NOT EXISTS warns (user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS admin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER, action TEXT);
    """)
    conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (MAIN_ADMIN_ID,))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(group_members)")]
    if "last_seen" not in cols:
        conn.execute("ALTER TABLE group_members ADD COLUMN last_seen TEXT")

    cols_sched = [r[1] for r in conn.execute("PRAGMA table_info(schedule)")]
    if "date" not in cols_sched:
        conn.execute("ALTER TABLE schedule ADD COLUMN date TEXT")

    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()
    with db_session(live=False) as conn:
        reminder_cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}
        for name, definition in {"attempts": "INTEGER NOT NULL DEFAULT 0", "last_error": "TEXT"}.items():
            if name not in reminder_cols:
                conn.execute(f"ALTER TABLE reminders ADD COLUMN {name} {definition}")
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule(date);
            CREATE INDEX IF NOT EXISTS idx_homework_deadline ON homework(deadline);
            CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
            CREATE INDEX IF NOT EXISTS idx_reminders_chat_run ON reminders(chat_id, run_at);
            CREATE TABLE IF NOT EXISTS notification_log (
                key TEXT PRIMARY KEY, sent_at TEXT NOT NULL
            );
        """)
    for name in ("init_study_db", "init_tools_db"):
        initializer = globals().get(name)
        if initializer:
            initializer()


def get_setting(key: str, default: str | None = None) -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default if default is not None else DEFAULT_SETTINGS.get(key, "")
    return row["value"]


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    conn = get_db()
    res = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return res is not None


def log_action(user_id: int, action: str):
    try:
        conn = get_db()
        conn.execute("INSERT INTO admin_log (ts, user_id, action) VALUES (?, ?, ?)",
                     (get_now().strftime("%d.%m %H:%M"), user_id, action))
        conn.execute("DELETE FROM admin_log WHERE id NOT IN (SELECT id FROM admin_log ORDER BY id DESC LIMIT 200)")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"log_action: {e}")


def backup_database(reason: str = "auto"):
    src = dst = None
    try:
        if not os.path.exists(DB_PATH):
            return None
        reason = re.sub(r"[^a-zA-Z0-9_-]", "_", reason)[:40] or "auto"
        timestamp = get_now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = os.path.join(BACKUP_DIR, f"bot_data_{timestamp}_{reason}.db")
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Проверка резервной копии не пройдена")
        dst.close()
        src.close()
        dst = src = None
        backups = list_backups()
        while len(backups) > 20:
            os.remove(os.path.join(BACKUP_DIR, backups.pop(0)))
        return os.path.basename(backup_path)
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")
        return None
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def list_backups() -> list[str]:
    return sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")
                  and os.path.isfile(os.path.join(BACKUP_DIR, f))
                  and not os.path.islink(os.path.join(BACKUP_DIR, f)))


def restore_backup(filename: str) -> bool:
    src = dst = None
    try:
        if filename != os.path.basename(filename) or filename not in list_backups():
            return False
        path = os.path.join(BACKUP_DIR, filename)
        # Сначала читаем выбранную копию: ротация не сможет удалить её под ногами.
        src = sqlite3.connect(path)
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return False
        required = {"admins", "schedule", "settings", "homework", "reminders", "links", "events",
                    "group_members", "warns", "admin_log"}
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables):
            return False
        expected_columns = {
            "admins": {"user_id"}, "schedule": {"id", "time", "subject", "room"},
            "settings": {"key", "value"}, "homework": {"id", "subject", "task", "deadline", "created_at", "created_by"},
            "reminders": {"id", "chat_id", "text", "run_at"}, "links": {"id", "title", "url"},
            "events": {"id", "title", "date"}, "group_members": {"user_id", "username"},
            "warns": {"user_id", "count"}, "admin_log": {"id", "ts", "user_id", "action"},
        }
        for table, columns in expected_columns.items():
            actual = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                return False
        if not backup_database("before_restore"):
            return False
        dst = sqlite3.connect(DB_PATH)
        src.backup(dst)
        return True
    except Exception as e:
        logger.error(f"Ошибка восстановления: {e}")
        return False
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()


def backup_key(filename: str) -> str:
    return hashlib.sha256(filename.encode("utf-8")).hexdigest()[:20]


def resolve_backup(reference: str) -> str | None:
    backups = list_backups()
    # Старые уже отправленные кнопки с индексом также остаются рабочими.
    if reference.isdigit() and len(reference) < 10:
        idx = int(reference)
        return backups[idx] if 0 <= idx < len(backups) else None
    return next((name for name in backups if backup_key(name) == reference), None)


def touch_member(user: User):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO group_members (user_id, username, last_seen) VALUES (?, ?, ?)",
                     (user.id, user.full_name or user.username or "Участник", get_now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"touch_member: {e}")


# ============================ УТИЛИТЫ ============================
def get_now() -> datetime:
    return datetime.now(TIMEZONE)

def get_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())

def fmt_short(d: date) -> str:
    return d.strftime("%d.%m")

def esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def mention(user_id: int, name: str) -> str:
    return f"<a href='tg://user?id={user_id}'>{esc(name or 'Участник')}</a>"


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTHS_RU[d.month - 1]}"


def parse_duration(text: str) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*(s|с|m|м|h|ч|d|д)?\s*", text.lower())
    if not m:
        return None
    units = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}
    seconds = int(m.group(1)) * units.get(m.group(2) or "m", 60)
    return seconds if 0 < seconds <= 366 * 86400 else None


def parse_time_range(time_str: str):
    try:
        parts = time_str.replace("–", "-").replace("—", "-").split("-")
        if len(parts) > 2:
            return None, None
        start = datetime.strptime(parts[0].strip(), "%H:%M").time()
        end = datetime.strptime(parts[1].strip(), "%H:%M").time() if len(parts) > 1 else None
        if end is not None and end <= start:
            return None, None
        return start, end
    except Exception:
        return None, None


def parse_date(s: str) -> date | None:
    s = s.strip().lower()
    today = get_now().date()
    if s == "сегодня":
        return today
    if s == "завтра":
        return today + timedelta(days=1)
    if s == "послезавтра":
        return today + timedelta(days=2)
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", s)
    if m:
        for year in range(today.year, min(today.year + 9, 10000)):
            try:
                d = date(year, int(m.group(2)), int(m.group(1)))
                if d >= today:
                    return d
            except ValueError:
                continue
        return None
    return None


def days_left_str(d: date) -> str:
    delta = (d - get_now().date()).days
    if delta < 0:
        return f"просрочено на {-delta} дн."
    if delta == 0:
        return "сегодня!"
    if delta == 1:
        return "завтра"
    return f"через {delta} дн."


CLASS_LINE_RE = re.compile(r"(?:(?:[•●▪◦*—–-]|✅|📌|⏰)\s*)?(?:\d+[\.\)]\s*)?(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\s+(.+?)(?:\s*\(([^)]+)\))?$")


def parse_class_line(line: str) -> dict | None:
    m = CLASS_LINE_RE.fullmatch(line.strip())
    if not m:
        return None
    start, end = parse_time_range(f"{m.group(1)}-{m.group(2)}")
    subject = m.group(3).strip()
    room = (m.group(4) or "—").strip()
    if not start or not end or not subject or len(subject) > 200 or len(room) > 100:
        return None
    return {"time": f"{start:%H:%M}-{end:%H:%M}", "subject": subject, "room": room}


def parse_week_text(text: str, monday: date) -> dict:
    result: dict[str, list] = {}
    current_date = None
    for line in text.splitlines():
        l = line.strip()
        if not l:
            continue
        cls = parse_class_line(l)
        if cls and current_date:
            result.setdefault(current_date, []).append(cls)
            continue
        low = l.lower().strip(" :📅*_")
        for kw, code in DAYS_KW.items():
            if low == kw or low.startswith(kw + " ") or (len(kw) > 2 and kw in low):
                day_idx = DAYS_ORDER.index(code)
                current_date = (monday + timedelta(days=day_idx)).isoformat()
                result.setdefault(current_date, [])
                break
    return result


def validate_class_text(text: str) -> tuple[list[dict], list[str]]:
    classes, errors = [], []
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        item = parse_class_line(line)
        if item:
            classes.append(item)
        else:
            errors.append(f"Строка {index}: {line.strip()[:100]}")
    if not classes and not errors:
        errors.append("Нет строк с парами.")
    if len(classes) > 100:
        errors.append("Слишком много пар: максимум 100 за одно добавление.")
    return classes, errors


def validate_week_text(text: str, monday: date) -> tuple[dict, list[str]]:
    """Прежние заголовки дней; ошибочный текст никогда не попадает в запись."""
    parsed, errors = {}, []
    current = None
    for index, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        item = parse_class_line(line)
        if item:
            if current is None:
                errors.append(f"Строка {index}: перед парой укажите день недели.")
            else:
                parsed[current].append(item)
            continue
        low = line.lower().strip(" :📅*_")
        day = next((code for kw, code in DAYS_KW.items()
                    if low == kw or low.startswith(kw + " ") or (len(kw) > 2 and kw in low)), None)
        # Не принимаем испорченную строку с временем за заголовок дня.
        if day and not re.search(r"\d{1,2}:\d{2}", line):
            current = (monday + timedelta(days=DAYS_ORDER.index(day))).isoformat()
            parsed.setdefault(current, [])
        elif low not in ("пар нет", "нет пар", "выходной", "—", "-") or current is None:
            errors.append(f"Строка {index}: {line[:100]}")
    if not any(parsed.values()):
        errors.append("Не распознано ни одной пары. Для очистки используйте кнопку удаления дня/недели.")
    if sum(map(len, parsed.values())) > 300:
        errors.append("Максимум 300 пар за одну загрузку.")
    return parsed, errors


def get_classes_for_date(d: date) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM schedule WHERE date = ? AND date IS NOT NULL", (d.isoformat(),)).fetchall()
    conn.close()
    classes = [dict(r) for r in rows]
    classes.sort(key=lambda c: parse_time_range(c["time"])[0] or datetime.min.time())
    return classes


def fmt_class(c: dict) -> str:
    if SCHEDULE_FEED is not None:
        kind = getattr(SCHEDULE_FEED,'lesson_kinds',{}).get(c.get('id'),'📚 Занятие')
        return f"<b>{esc(c['time'])} · {esc(kind)} · {esc(c['subject'])}</b>\n{esc(c['room'])}"
    return f"⏰ {esc(c['time'])} | {esc(c['subject'])} | 🚪 {esc(c['room'])}"


# ---------- домашка ----------
def get_homework(include_expired: bool = False) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM homework ORDER BY deadline IS NULL, deadline, id").fetchall()
    conn.close()
    today = get_now().date().isoformat()
    hw = [dict(r) for r in rows]
    if not include_expired:
        hw = [h for h in hw if not h["deadline"] or h["deadline"] >= today]
    return hw


def homework_for_date(d: date) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM homework WHERE deadline = ? ORDER BY id", (d.isoformat(),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_homework(raw: str, user_id: int) -> tuple[bool, str]:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return False, "⚠️ Формат: <code>Предмет | Задание | 25.10</code> (дата необязательна)"
    if len(parts) > 3:
        return False, "⚠️ Используйте два разделителя |: Предмет | Задание | Дата."
    if len(parts[0]) > 200 or len(parts[1]) > 3000:
        return False, "⚠️ Предмет — до 200 символов, задание — до 3000."
    deadline = None
    if len(parts) >= 3 and parts[2]:
        d = parse_date(parts[2])
        if not d:
            return False, "❌ Не понял дату. Примеры: <code>25.10</code>, <code>25.10.2025</code>, <code>завтра</code>"
        deadline = d.isoformat()
    conn = get_db()
    conn.execute("INSERT INTO homework (subject, task, deadline, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
                 (parts[0], parts[1], deadline, get_now().isoformat(), user_id))
    conn.commit()
    conn.close()
    log_action(user_id, f"ДЗ добавлено: {parts[0]}")
    return True, "✅ Домашка добавлена!"


def delete_homework(hw_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
    conn.commit()
    conn.close()
    if cur.rowcount > 0:
        with db_session() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='homework_progress'").fetchone():
                conn.execute("DELETE FROM homework_progress WHERE homework_id = ?", (hw_id,))
    return cur.rowcount > 0


def fmt_hw(h: dict, with_id: bool = True) -> str:
    dl = ""
    if h["deadline"]:
        d = date.fromisoformat(h["deadline"])
        dl = f"\n   📅 до {d.strftime('%d.%m')} ({days_left_str(d)})"
    prefix = f"<code>#{h['id']}</code> " if with_id else ""
    return f"📝 {prefix}<b>{esc(h['subject'])}</b>: {esc(h['task'])}{dl}"


def homework_text(include_expired=False, with_id=True) -> str:
    hw = get_homework(include_expired)
    if not hw:
        return "📝 <b>Домашки нет!</b> 🎉"
    return "📝 <b>Домашнее задание:</b>\n\n" + "\n\n".join(fmt_hw(h, with_id) for h in hw)


# ---------- ссылки ----------
def get_links() -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM links ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def links_text(with_id=True) -> str:
    links = get_links()
    if not links:
        return "🔗 Ссылок пока нет."
    lines = []
    for l in links:
        prefix = f"<code>#{l['id']}</code> " if with_id else ""
        lines.append(f"{prefix}<a href='{esc(l['url'])}'>{esc(l['title'])}</a>")
    return "🔗 <b>Полезные ссылки:</b>\n\n" + "\n".join(lines)


def add_link(raw: str) -> tuple[bool, str]:
    from urllib.parse import urlsplit
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2 or not parts[0] or not parts[1].startswith(("http://", "https://", "tg://")):
        return False, "⚠️ Формат: <code>Название | https://ссылка</code>"
    try:
        url = urlsplit(parts[1])
        valid = bool(url.netloc) and not any(ch.isspace() or ord(ch) < 32 for ch in parts[1])
    except ValueError:
        valid = False
    if not valid or len(parts[0]) > 200 or len(parts[1]) > 2000:
        return False, "⚠️ Проверьте ссылку. Название — до 200 символов, адрес — до 2000."
    conn = get_db()
    conn.execute("INSERT INTO links (title, url) VALUES (?, ?)", (parts[0], parts[1]))
    conn.commit()
    conn.close()
    return True, "✅ Ссылка добавлена!"


# ---------- события ----------
def get_events(upcoming_only=True) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM events ORDER BY date").fetchall()
    conn.close()
    ev = [dict(r) for r in rows]
    if upcoming_only:
        today = get_now().date().isoformat()
        ev = [e for e in ev if e["date"] >= today]
    return ev


def events_for_date(d: date) -> list:
    return [e for e in get_events(False) if e["date"] == d.isoformat()]


def events_text() -> str:
    ev = get_events()
    if not ev:
        return "📆 Ближайших событий нет."
    lines = [f"<code>#{e['id']}</code> <b>{esc(e['title'])}</b> — {date.fromisoformat(e['date']).strftime('%d.%m')} ({days_left_str(date.fromisoformat(e['date']))})" for e in ev]
    return "📆 <b>События и дедлайны:</b>\n\n" + "\n".join(lines)


# ---------- варны ----------
def change_warns(user_id: int, delta: int) -> int:
    conn = get_db()
    row = conn.execute("SELECT count FROM warns WHERE user_id = ?", (user_id,)).fetchone()
    count = max(0, (row["count"] if row else 0) + delta)
    conn.execute("INSERT OR REPLACE INTO warns (user_id, count) VALUES (?, ?)", (user_id, count))
    conn.commit()
    conn.close()
    return count


def reset_warns(user_id: int):
    conn = get_db()
    conn.execute("DELETE FROM warns WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# ---------- отправка ----------
class _HTMLTokens(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        self.tokens.append(("open", self.get_starttag_text(), tag))

    def handle_endtag(self, tag):
        self.tokens.append(("close", f"</{tag}>", tag))

    def handle_startendtag(self, tag, attrs):
        self.tokens.append(("atom", self.get_starttag_text(), tag))

    def handle_data(self, data):
        self.tokens.append(("text", data, ""))

    def handle_entityref(self, name):
        self.tokens.append(("atom", f"&{name};", ""))

    def handle_charref(self, name):
        self.tokens.append(("atom", f"&#{name};", ""))


def split_html_text(text: str, limit: int = 4000) -> list[str]:
    """Разбивает HTML, закрывая и открывая теги на границах сообщений."""
    def size(value: str) -> int:
        # Telegram измеряет смещения сущностей в UTF-16; запас учитывает и emoji.
        return len(value.encode("utf-16-le")) // 2

    if size(text) <= limit:
        return [text]
    if limit < 64:
        raise ValueError("Слишком маленький лимит сообщения")
    parser = _HTMLTokens()
    parser.feed(text)
    parser.close()
    chunks, stack = [], []
    current = ""
    has_content = False

    def closing(items):
        return "".join(f"</{tag}>" for tag, _ in reversed(items))

    def flush():
        nonlocal current, has_content
        if has_content:
            chunks.append(current + closing(stack))
        current = "".join(raw for _, raw in stack)
        has_content = False

    for kind, raw, tag in parser.tokens:
        if kind == "open":
            new_stack = stack + [(tag, raw)]
            if size(current) + size(raw) + size(closing(new_stack)) > limit:
                flush()
            if size(current) + size(raw) + size(closing(new_stack)) > limit:
                raise ValueError("HTML-тег превышает лимит сообщения")
            current += raw
            stack = new_stack
        elif kind == "close":
            if not stack or stack[-1][0] != tag:
                raise ValueError("Несбалансированные HTML-теги")
            current += raw
            stack.pop()
        elif kind == "atom":
            if size(current) + size(raw) + size(closing(stack)) > limit:
                flush()
            if size(current) + size(raw) + size(closing(stack)) > limit:
                raise ValueError("HTML-сущность превышает лимит сообщения")
            current += raw
            has_content = True
        else:
            while raw:
                capacity = limit - size(current) - size(closing(stack))
                if capacity <= 0:
                    flush()
                    capacity = limit - size(current) - size(closing(stack))
                if capacity <= 0:
                    raise ValueError("Слишком длинные HTML-теги")
                take = min(capacity, len(raw))
                if size(raw[:take]) > capacity:
                    low, high = 0, take
                    while low < high:
                        middle = (low + high + 1) // 2
                        if size(raw[:middle]) <= capacity:
                            low = middle
                        else:
                            high = middle - 1
                    take = low
                if not take:
                    if has_content:
                        flush()
                        continue
                    raise ValueError("HTML-теги не оставляют места для текста")
                if take < len(raw):
                    boundary = max(raw.rfind("\n", 0, take), raw.rfind(" ", 0, take))
                    if boundary >= take // 2:
                        take = boundary + 1
                current += raw[:take]
                raw = raw[take:]
                has_content = True
                if raw:
                    flush()
    if stack:
        raise ValueError("Незакрытые HTML-теги")
    if has_content:
        chunks.append(current)
    return chunks or [text]


async def _send_with_retry(chat_id: int, text: str, **kwargs):
    for attempt in range(3):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as exc:
            if attempt == 2 or exc.retry_after > 30:
                raise
            await asyncio.sleep(exc.retry_after + 0.1)
        except TelegramNetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)


async def safe_send(chat_id, text, **kwargs) -> Message | None:
    if SCHEDULE_FEED is not None and SCHEDULE_FEED.warning:
        text += "\n\n⚠️ " + esc(SCHEDULE_FEED.warning)
    try:
        chunks = split_html_text(text)
        last = None
        for index, chunk in enumerate(chunks):
            options = dict(kwargs)
            if index < len(chunks) - 1:
                options.pop("reply_markup", None)
            last = await _send_with_retry(chat_id, chunk, **options)
        return last
    except Exception as e:
        logger.error(f"Ошибка отправки в {chat_id}: {e}")
        return None


async def safe_edit(call: CallbackQuery, text: str, markup=None):
    if SCHEDULE_FEED is not None and SCHEDULE_FEED.warning:
        text += "\n\n⚠️ " + esc(SCHEDULE_FEED.warning)
    if not call.message:
        return None
    if len(text.encode("utf-16-le")) // 2 > 4000:
        chunks = split_html_text(text)
        try:
            await call.message.edit_text(chunks[0], reply_markup=None, disable_web_page_preview=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                return await safe_send(call.message.chat.id, text, reply_markup=markup, disable_web_page_preview=True)
        for index, chunk in enumerate(chunks[1:], 1):
            await safe_send(call.message.chat.id, chunk,
                            reply_markup=markup if index == len(chunks) - 1 else None,
                            disable_web_page_preview=True)
        return None
    try:
        await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        if "message is not modified" not in str(e):
            try:
                await safe_send(call.message.chat.id, text, reply_markup=markup, disable_web_page_preview=True)
            except Exception as e2:
                logger.error(f"safe_edit: {e2}")


# ============================ НАПОМИНАНИЯ (персистентные) ============================
def add_reminder(chat_id: int, text: str, run_at: datetime) -> int:
    if not text.strip() or len(text) > 3500:
        raise ValueError("Текст напоминания: от 1 до 3500 символов.")
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=TIMEZONE)
    if run_at <= get_now():
        raise ValueError("Напоминание должно быть в будущем.")
    conn = get_db()
    cur = conn.execute("INSERT INTO reminders (chat_id, text, run_at) VALUES (?, ?, ?)",
                       (chat_id, text, run_at.isoformat()))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    if scheduler is not None:
        scheduler.add_job(fire_reminder, "date", run_date=run_at, args=[rid], id=f"rem_{rid}", replace_existing=True,
                          misfire_grace_time=None)
    return rid


async def fire_reminder(rid: int):
    with db_session() as conn:
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
    if not row:
        return
    sent = await safe_send(row["chat_id"], f"⏰ <b>НАПОМИНАНИЕ!</b>\n\n{esc(row['text'])}")
    with db_session() as conn:
        if sent:
            # Не удаляем напоминание, которое администратор успел перенести во время отправки.
            conn.execute("DELETE FROM reminders WHERE id = ? AND run_at = ?", (rid, row["run_at"]))
        else:
            attempts = int(row["attempts"] or 0) + 1
            delay = min(3600, 60 * (2 ** min(attempts - 1, 6)))
            retry_at = get_now() + timedelta(seconds=delay)
            changed = conn.execute("UPDATE reminders SET attempts = ?, last_error = ?, run_at = ? WHERE id = ? AND run_at = ?",
                                   (attempts, "Не удалось отправить сообщение", retry_at.isoformat(), rid, row["run_at"])).rowcount
            if changed and scheduler is not None:
                scheduler.add_job(fire_reminder, "date", run_date=retry_at, args=[rid], id=f"rem_{rid}",
                                  replace_existing=True, misfire_grace_time=None)
    if not sent:
        logger.warning("Напоминание #%s сохранено для повторной отправки", rid)


def delete_reminder(rid: int, chat_id: int | None = None) -> bool:
    with db_session() as conn:
        query = "DELETE FROM reminders WHERE id = ?"
        params = [rid]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)
        deleted = conn.execute(query, params).rowcount > 0
    if deleted and scheduler is not None and scheduler.get_job(f"rem_{rid}"):
        scheduler.remove_job(f"rem_{rid}")
    return deleted


def reschedule_reminder(rid: int, chat_id: int, run_at: datetime) -> bool:
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=TIMEZONE)
    if run_at <= get_now():
        raise ValueError("Укажите будущую дату и время.")
    with db_session() as conn:
        changed = conn.execute("UPDATE reminders SET run_at = ?, attempts = 0, last_error = NULL WHERE id = ? AND chat_id = ?",
                               (run_at.isoformat(), rid, chat_id)).rowcount > 0
    if changed and scheduler is not None:
        scheduler.add_job(fire_reminder, "date", run_date=run_at, args=[rid], id=f"rem_{rid}",
                          replace_existing=True, misfire_grace_time=None)
    return changed


def restore_reminders():
    if scheduler is None:
        return
    for job in scheduler.get_jobs():
        if job.id.startswith("rem_"):
            scheduler.remove_job(job.id)
    conn = get_db()
    rows = conn.execute("SELECT * FROM reminders").fetchall()
    conn.close()
    now = get_now()
    for r in rows:
        try:
            run_at = datetime.fromisoformat(r["run_at"])
        except (TypeError, ValueError):
            logger.warning("Некорректная дата напоминания #%s; запись сохранена", r["id"])
            continue
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=TIMEZONE)
        if run_at <= now:
            run_at = now + timedelta(seconds=5)
        scheduler.add_job(fire_reminder, "date", run_date=run_at, args=[r["id"]], id=f"rem_{r['id']}", replace_existing=True,
                          misfire_grace_time=None)
    if rows:
        logger.info(f"Восстановлено напоминаний: {len(rows)}")


# ============================ FSM ============================
class ParseText(StatesGroup):
    waiting_text = State()

class AddClass(StatesGroup):
    waiting_text = State()

class AddAnnouncement(StatesGroup):
    waiting_text = State()
    waiting_confirm = State()

class AddNewAdmin(StatesGroup):
    waiting_id = State()

class AddHomework(StatesGroup):
    waiting_text = State()

class AddLink(StatesGroup):
    waiting_text = State()

class SetSetting(StatesGroup):
    waiting_value = State()


# ============================ МЕНЮ ============================
def Btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def get_main_menu():
    b = InlineKeyboardBuilder()
    b.row(Btn("📅 Расписание", "menu_schedule"), Btn("📝 Домашка", "menu_hw"))
    b.row(Btn("📢 Объявление", "menu_announce"), Btn("🔗 Ссылки", "menu_links"))
    b.row(Btn("⚙️ Настройки", "menu_settings"), Btn("👑 Управление", "menu_admin"))
    b.row(Btn("🎓 Учёба и прогресс", "menu_study"), Btn("🛠 Инструменты", "menu_tools"))
    b.row(Btn("ℹ️ Инфо / Команды", "menu_info"))
    return b.as_markup()


def get_week_menu(monday: date):
    b = InlineKeyboardBuilder()
    saturday = monday + timedelta(days=5)
    week_str = f"{fmt_short(monday)}-{fmt_short(saturday)}"

    b.row(
        Btn("◀️ Пред.", f"week_{ (monday - timedelta(days=7)).isoformat() }"),
        Btn(week_str, "ignore"),
        Btn("След. ▶️", f"week_{ (monday + timedelta(days=7)).isoformat() }")
    )

    days = []
    conn = get_db()
    for i in range(6):
        d = monday + timedelta(days=i)
        cnt = conn.execute("SELECT COUNT(*) FROM schedule WHERE date = ?", (d.isoformat(),)).fetchone()[0]
        mark = "• " if cnt > 0 else ""
        days.append(Btn(f"{mark}{['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'][i]} {fmt_short(d)}", f"date_{d.isoformat()}"))
    conn.close()

    b.row(days[0], days[1], days[2])
    b.row(days[3], days[4], days[5])

    if SCHEDULE_FEED is None:
        b.row(Btn("📥 Загрузить неделю текстом", f"parse_week_{monday.isoformat()}"))
    else:
        b.row(Btn("🌐 П-114 · данные общего бота", "ignore"))
    b.row(Btn("🧰 Инструменты недели", f"sch_tools_{monday.isoformat()}"))
    sunday = monday + timedelta(days=6)
    b.row(Btn(f"Вс {fmt_short(sunday)}", f"date_{sunday.isoformat()}"))
    if SCHEDULE_FEED is None:
        b.row(Btn("🗑 Очистить эту неделю", f"clear_week_{monday.isoformat()}"))
    b.row(Btn("🔙 В главное меню", "main_menu"))
    return b.as_markup()


def page_slice(rows: list, page: int, size: int = 20):
    pages = max(1, (len(rows) + size - 1) // size)
    page = max(0, min(int(page), pages - 1))
    return rows[page * size:(page + 1) * size], page, pages


def add_page_buttons(builder, page: int, pages: int, prefix: str):
    buttons = []
    if page > 0:
        buttons.append(Btn("◀️", f"{prefix}{page - 1}"))
    if pages > 1:
        buttons.append(Btn(f"{page + 1}/{pages}", "ignore"))
    if page + 1 < pages:
        buttons.append(Btn("▶️", f"{prefix}{page + 1}"))
    if buttons:
        builder.row(*buttons)


def render_date(d: date, page: int = 0):
    classes = get_classes_for_date(d)
    selected, page, pages = page_slice(classes, page, 15)
    b = InlineKeyboardBuilder()
    day_name = DAYS_RU[DAYS_ORDER[d.weekday()]]
    text = f"<b>П-114</b>\n{day_label(d)}\n\n" if SCHEDULE_FEED is not None else f"📅 <b>{day_name}, {fmt_date(d)}</b>\n\n"
    if classes:
        if pages > 1:
            text += f"Всего пар: {len(classes)} · страница {page + 1}/{pages}\n\n"
        for c in selected:
            text += fmt_class(c) + "\n\n"
            if SCHEDULE_FEED is None:
                b.row(Btn(f"🗑 {c['time']} {c['subject'][:20]}", f"cls_del_{c['id']}"),
                  Btn("✏️", f"sch_edit_{c['id']}"))
    else:
        text += "На сайте нет занятий на эту дату.\n" if SCHEDULE_FEED is not None else "Пусто. Добавьте пары!\n"
    add_page_buttons(b, page, pages, f"date_page_{d.isoformat()}_")
    if SCHEDULE_FEED is None:
        b.row(Btn("➕ Добавить пару", f"add_class_{d.isoformat()}"))
        b.row(Btn("📋 Копировать день", f"sch_copy_day_{d.isoformat()}"))
    b.row(Btn("🔙 К неделе", f"week_{get_monday(d).isoformat()}"))
    return text, b.as_markup()


def main_menu_text() -> str:
    now = get_now()
    hw_count = len(get_homework())
    day_name = DAYS_RU[DAYS_ORDER[now.weekday()]]
    return (f"👋 <b>Панель старосты</b>\n\n📅 Сегодня: {day_name}, {fmt_date(now.date())}\n"
            f"📚 Пар сегодня: {len(get_classes_for_date(now.date()))}\n📝 Активных ДЗ: {hw_count}")


def get_help_text(admin: bool) -> str:
    text = ("🤖 <b>Команды (без /):</b>\n\n"
            "📅 <b>Расписание:</b>\n• <code>сегодня</code> / <code>завтра</code>\n• <code>расписание на среду</code>\n"
            "• <code>неделя</code> / <code>след неделя</code>\n• <code>следующая пара</code> / <code>сейчас</code> / <code>до конца пары</code>\n\n"
            "📝 <b>Учёба:</b>\n• <code>дз</code> — домашка\n• <code>ссылки</code>\n• <code>события</code> — дедлайны\n\n"
            "🎲 <b>Прочее:</b>\n• <code>монетка</code> / <code>кубик</code> / <code>мой id</code>\n")
    text += ("\n✨ <b>Новые возможности:</b>\n"
             "• <code>мое дз</code> / <code>выполненное дз</code> / <code>просроченные дз</code>\n"
             "• <code>дз готово 5</code> / <code>дз не готово 5</code> — личные отметки\n"
             "• <code>план</code> / <code>план завтра</code> — пары, ДЗ и события\n"
             "• <code>найти запрос</code> / <code>найти пары запрос</code> / <code>аудитория А-101</code>\n"
             "• <code>нагрузка</code> — число пар и учебных часов\n"
             "• <code>расписание csv</code> / <code>расписание ics</code> — файлы недели\n")
    if admin:
        text += ("\n👑 <b>Для старосты и замов:</b>\n"
                 "📢 • <code>кал текст</code> / <code>тег всех</code> / <code>опрос Текст</code> / <code>перекличка</code> / <code>рандом</code>\n"
                 "📝 • <code>дз добавить Предмет | Задание | 25.10</code> / <code>дз удалить 3</code>\n"
                 "🔗 • <code>ссылка добавить Название | url</code> / <code>ссылка удалить 2</code>\n"
                 "📆 • <code>событие добавить Сессия | 15.01</code> / <code>событие удалить 1</code>\n"
                 "⏰ • <code>напомни через 20m Текст</code> / <code>напомни в 18:30 Текст</code> / <code>напоминания</code>\n"
                 "🛠 • <code>закрепи</code> / <code>открепи</code> / <code>статистика</code> / <code>экспорт</code>\n"
                 "🛡 <b>Модерация (ответом):</b> <code>мут 20m</code> / <code>размут</code> / <code>бан</code> / <code>разбан</code> / <code>удали</code>\n"
                 "   <code>варн [причина]</code> / <code>варны</code> / <code>снять варн</code> (3 варна = мут на сутки)\n"
                 "\n⚙️ Настройки и расписание — в личке бота.")
        text += ("\n\n🛠 <b>Дополнения для администратора:</b>\n"
                 "• <code>дз изменить 5 Предмет | Задание | 25.10</code>\n"
                 "• <code>напомни 25.10.2026 18:30 Текст</code>\n"
                 "• <code>напоминание удалить 3</code> / <code>напоминание перенести 3 26.10.2026 19:00</code>\n"
                 "• <code>опрос Вопрос | Вариант А | Вариант Б</code>\n"
                 "В меню: редактирование и копирование пар, управление событиями, диагностика, "
                 "выгрузка данных и настройка приветствия.")
    return text


# ============================ ОБЩИЕ КОМАНДЫ ============================
@common_router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await state.clear()
    if not is_admin(message.from_user.id):
        return await message.answer(
            f"Этот бот только для старосты и замов.\n\nТвой ID: <code>{message.from_user.id}</code>\n"
            f"Отправь его старосте, если ты зам.")
    await message.answer(main_menu_text(), reply_markup=get_main_menu())


@common_router.message(Command("id", "myid"))
async def cmd_id(message: Message):
    text = f"🆔 Твой ID: <code>{message.from_user.id}</code>"
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        text += f"\n👤 ID {esc(u.full_name)}: <code>{u.id}</code>"
    if message.chat.type != "private":
        text += f"\n💬 ID чата: <code>{message.chat.id}</code>"
    await message.answer(text)


@common_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id) and message.chat.type == "private":
        await message.answer("❌ Отменено.", reply_markup=get_main_menu())


@common_router.message(Command("help"))
async def cmd_help(message: Message):
    if message.from_user and (message.chat.type == "private" or message.chat.id == GROUP_CHAT_ID):
        await safe_send(message.chat.id, get_help_text(is_admin(message.from_user.id)))


@common_router.message(F.chat.type == "private", F.text.lower().in_({"отмена", "назад"}))
async def cancel_text(message: Message, state: FSMContext):
    await cmd_cancel(message, state)


class CallbackAcknowledgementMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        finally:
            try:
                await event.answer()
            except Exception:
                pass


class GroupMemberTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if event.chat.id == GROUP_CHAT_ID and event.from_user and not event.from_user.is_bot:
            touch_member(event.from_user)
        return await handler(event, data)


fallback_router = Router(name="fallback")


@fallback_router.callback_query()
async def stale_callback(call: CallbackQuery):
    await call.answer("Кнопка недоступна или устарела. Откройте меню заново командой /start.", show_alert=True)


@dp.errors()
async def handle_unexpected_error(event: ErrorEvent):
    logger.error("Ошибка обработчика: %s", type(event.exception).__name__,
                 exc_info=(type(event.exception), event.exception, event.exception.__traceback__))
    call = event.update.callback_query
    message = event.update.message
    if call:
        try:
            await call.answer("Не удалось обработать кнопку. Откройте меню заново.", show_alert=True)
        except Exception:
            pass
    elif message and feature_message_allowed(message, public=False):
        await safe_send(message.chat.id, "❌ Не удалось обработать запрос. Данные сохранены; попробуйте снова или /cancel.")
    return True


def configure_dispatcher():
    if common_router.parent_router is not None:
        return
    cooldown = RequestCooldownMiddleware(MAIN_ADMIN_ID, os.environ.get("ADMIN_USERNAME", "Timur0_15"))
    dp.message.outer_middleware(ScopeMiddleware())
    dp.message.outer_middleware(cooldown)
    dp.message.outer_middleware(SharedScheduleMiddleware(lambda: SCHEDULE_FEED))
    dp.callback_query.outer_middleware(ScopeMiddleware())
    dp.callback_query.outer_middleware(cooldown)
    dp.callback_query.outer_middleware(SharedScheduleMiddleware(lambda: SCHEDULE_FEED))
    dp.callback_query.outer_middleware(CallbackAcknowledgementMiddleware())
    dp.message.outer_middleware(GroupMemberTrackingMiddleware())
    for router in (common_router, schedule_extra_router, study_router, tools_router, admin_router, group_router, fallback_router):
        dp.include_router(router)


# ============================ АДМИН-ПАНЕЛЬ (ЛИЧКА) ============================
admin_router.message.filter(F.chat.type == "private", lambda m: m.from_user and is_admin(m.from_user.id))
admin_router.callback_query.filter(lambda c: c.message and c.message.chat.type == "private" and is_admin(c.from_user.id))


@admin_router.callback_query(F.data == "ignore")
async def handle_ignore(call: CallbackQuery):
    await call.answer()


@admin_router.callback_query(F.data == "main_menu")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, main_menu_text(), get_main_menu())


@admin_router.callback_query(F.data == "menu_info")
async def menu_info_call(call: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.row(Btn("🔙 Назад", "main_menu"))
    await safe_edit(call, get_help_text(is_admin(call.from_user.id)), b.as_markup())


# ---------- расписание календарь ----------
@admin_router.callback_query(F.data == "menu_schedule")
async def menu_schedule(call: CallbackQuery, state: FSMContext):
    await state.clear()
    monday = get_monday(get_now().date())
    week_str = f"Неделя {fmt_short(monday)}-{fmt_short(monday + timedelta(days=5))}"
    await safe_edit(call, f"📅 <b>{week_str}</b>\n\nВыбери день (• = есть пары):", get_week_menu(monday))


@admin_router.callback_query(F.data.startswith("week_"))
async def show_week(call: CallbackQuery, state: FSMContext):
    await state.clear()
    monday = date.fromisoformat(call.data.replace("week_", ""))
    week_str = f"Неделя {fmt_short(monday)}-{fmt_short(monday + timedelta(days=5))}"
    await safe_edit(call, f"📅 <b>{week_str}</b>\n\nВыбери день (• = есть пары):", get_week_menu(monday))


@admin_router.callback_query(F.data.regexp(r"^date_\d{4}-\d{2}-\d{2}$"))
async def show_date(call: CallbackQuery, state: FSMContext):
    await state.clear()
    d = date.fromisoformat(call.data.replace("date_", ""))
    text, markup = render_date(d)
    await safe_edit(call, text, markup)


@admin_router.callback_query(F.data.regexp(r"^date_page_\d{4}-\d{2}-\d{2}_\d+$"))
async def show_date_page(call: CallbackQuery, state: FSMContext):
    await state.clear()
    day, page = call.data.removeprefix("date_page_").rsplit("_", 1)
    text, markup = render_date(date.fromisoformat(day), int(page))
    await safe_edit(call, text, markup)


@admin_router.callback_query(F.data.startswith("add_class_"))
async def add_class_start(call: CallbackQuery, state: FSMContext):
    d_iso = call.data.replace("add_class_", "")
    d = date.fromisoformat(d_iso)
    await state.update_data(date=d_iso)
    await state.set_state(AddClass.waiting_text)
    await safe_edit(call, f"➕ <b>{fmt_date(d)}</b>\nВведи пару (или несколько строк):\n"
                          f"<code>13:00-14:30 Педиатрия (А-101)</code>\n\n/cancel — отмена")


@admin_router.message(AddClass.waiting_text)
async def add_class_process(message: Message, state: FSMContext):
    data = await state.get_data()
    d_iso = data["date"]
    classes, errors = validate_class_text(message.text or "")
    if errors:
        return await safe_send(message.chat.id, "❌ Данные не изменены. Исправьте строки:\n" +
                               "\n".join(esc(e) for e in errors[:10]) +
                               "\nФормат: <code>13:00-14:30 Предмет (Ауд)</code>")
    conn = get_db()
    for c in classes:
        conn.execute("INSERT INTO schedule (date, time, subject, room) VALUES (?, ?, ?, ?)",
                     (d_iso, c["time"], c["subject"], c["room"]))
    conn.commit()
    conn.close()
    await state.clear()
    log_action(message.from_user.id, f"Добавлено пар: {len(classes)} ({d_iso})")
    text, markup = render_date(date.fromisoformat(d_iso))
    await safe_send(message.chat.id, f"✅ Добавлено пар: {len(classes)}\n\n" + text, reply_markup=markup)


@admin_router.callback_query(F.data.startswith("parse_week_"))
async def start_parse_week(call: CallbackQuery, state: FSMContext):
    monday_iso = call.data.replace("parse_week_", "")
    await state.update_data(monday=monday_iso)
    await state.set_state(ParseText.waiting_text)
    await safe_edit(call, f"📥 Скинь текст расписания для недели с {fmt_short(date.fromisoformat(monday_iso))}:\n\n"
                          "<code>Понедельник\n1. 13:00-14:30 Педиатрия (А-101)\n2. 14:40-16:10 Хирургия (Б-203)\n\n"
                          "Вторник\n...</code>\n\n⚠️ Дни из текста перезапишут эти дни в базе.\n/cancel — отмена")


@admin_router.message(ParseText.waiting_text)
async def process_parse_week(message: Message, state: FSMContext):
    data = await state.get_data()
    monday = date.fromisoformat(data["monday"])
    parsed, errors = validate_week_text(message.text or "", monday)
    if errors:
        return await safe_send(message.chat.id, "❌ Расписание не изменено. Исправьте строки и отправьте текст ещё раз:\n" +
                               "\n".join(esc(e) for e in errors[:10]) + "\n/cancel — отмена")

    if not backup_database("before_parse"):
        return await message.answer("❌ Не удалось создать бэкап. Расписание не изменено; попробуйте позже.")
    conn = get_db()
    total = 0
    for d_iso, day_classes in parsed.items():
        conn.execute("DELETE FROM schedule WHERE date = ?", (d_iso,))
        for c in day_classes:
            conn.execute("INSERT INTO schedule (date, time, subject, room) VALUES (?, ?, ?, ?)",
                         (d_iso, c["time"], c["subject"], c["room"]))
            total += 1
    conn.commit()
    conn.close()
    await state.clear()
    log_action(message.from_user.id, f"Загружено расписание текстом: {total} пар")
    await message.answer(f"✅ Обновлено!\nПар загружено: {total}\n💾 Бэкап создан.", reply_markup=get_week_menu(monday))


@admin_router.callback_query(F.data.startswith("cls_del_"))
async def delete_class(call: CallbackQuery):
    class_id = int(call.data.replace("cls_del_", ""))
    conn = get_db()
    row = conn.execute("SELECT date FROM schedule WHERE id = ?", (class_id,)).fetchone()
    if not row or not row["date"]:
        conn.close()
        return await call.answer("Уже удалено.")
    if not backup_database("before_del_class"):
        conn.close()
        return await call.answer("Не удалось создать бэкап. Пара не удалена.", show_alert=True)
    conn.execute("DELETE FROM schedule WHERE id = ?", (class_id,))
    conn.commit()
    conn.close()
    await call.answer("Удалено!")
    text, markup = render_date(date.fromisoformat(row["date"]))
    await safe_edit(call, text, markup)


@admin_router.callback_query(F.data.startswith("clear_week_"))
async def confirm_clear_week(call: CallbackQuery):
    monday_iso = call.data.replace("clear_week_", "")
    b = InlineKeyboardBuilder()
    b.row(Btn(f"✅ ДА, очистить неделю", f"do_clear_week_{monday_iso}"), Btn("❌ Отмена", f"week_{monday_iso}"))
    await safe_edit(call, f"⚠️ Точно удалить ВСЕ пары с {fmt_short(date.fromisoformat(monday_iso))} по {fmt_short(date.fromisoformat(monday_iso) + timedelta(days=6))}?", b.as_markup())


@admin_router.callback_query(F.data.startswith("do_clear_week_"))
async def execute_clear_week(call: CallbackQuery):
    monday_iso = call.data.replace("do_clear_week_", "")
    monday = date.fromisoformat(monday_iso)
    dates = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
    if not backup_database("before_clear_week"):
        return await call.answer("Не удалось создать бэкап. Неделя не изменена.", show_alert=True)
    conn = get_db()
    for d in dates:
        conn.execute("DELETE FROM schedule WHERE date = ?", (d,))
    conn.commit()
    conn.close()
    log_action(call.from_user.id, f"Удалена неделя с {monday_iso}")
    await call.answer("Неделя очищена.")
    await safe_edit(call, f"✅ Неделя полностью очищена.", get_week_menu(monday))


# ---------- домашка ----------
def hw_menu(page: int = 0):
    b = InlineKeyboardBuilder()
    rows, page, pages = page_slice(get_homework(), page, 8)
    for h in rows:
        b.row(Btn(f"🗑 #{h['id']} {h['subject'][:25]}", f"hw_del_{h['id']}"))
    add_page_buttons(b, page, pages, "hw_page_")
    b.row(Btn("➕ Добавить", "hw_add"), Btn("🧹 Удалить просроченные", "hw_clean"))
    b.row(Btn("📤 Отправить в группу", "hw_post"))
    b.row(Btn("🔙 Назад", "main_menu"))
    return b.as_markup()


def homework_page_text(page: int = 0):
    homework = get_homework()
    if not homework:
        return homework_text()
    rows, page, pages = page_slice(homework, page, 8)
    label = f"Всего: {len(homework)} · страница {page + 1}/{pages}\n\n" if pages > 1 else ""
    return "📝 <b>Домашнее задание:</b>\n\n" + label + "\n\n".join(fmt_hw(h) for h in rows)


@admin_router.callback_query(F.data.regexp(r"^hw_page_\d+$"))
async def homework_page(call: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(call.data.removeprefix("hw_page_"))
    await safe_edit(call, homework_page_text(page), hw_menu(page))


@admin_router.callback_query(F.data == "menu_hw")
async def menu_hw(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, homework_page_text(), hw_menu())


@admin_router.callback_query(F.data == "hw_add")
async def hw_add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddHomework.waiting_text)
    await safe_edit(call, "📝 Введи домашку (можно указать любую дату):\n<code>Предмет | Задание | 25.10</code>\n(дата необязательна)\n\n/cancel — отмена")


@admin_router.message(AddHomework.waiting_text)
async def hw_add_process(message: Message, state: FSMContext):
    ok, msg = add_homework(message.text or "", message.from_user.id)
    if not ok:
        return await message.answer(msg)
    await state.clear()
    await safe_send(message.chat.id, msg + "\n\n" + homework_page_text(), reply_markup=hw_menu())


@admin_router.callback_query(F.data.startswith("hw_del_"))
async def hw_del(call: CallbackQuery):
    delete_homework(int(call.data.replace("hw_del_", "")))
    await call.answer("Удалено")
    await safe_edit(call, homework_page_text(), hw_menu())


@admin_router.callback_query(F.data == "hw_clean")
async def hw_clean(call: CallbackQuery):
    conn = get_db()
    cur = conn.execute("DELETE FROM homework WHERE deadline IS NOT NULL AND deadline < ?", (get_now().date().isoformat(),))
    conn.commit()
    conn.close()
    await call.answer(f"Удалено просроченных: {cur.rowcount}")
    await safe_edit(call, homework_page_text(), hw_menu())


@admin_router.callback_query(F.data == "hw_post")
async def hw_post(call: CallbackQuery):
    sent = await safe_send(GROUP_CHAT_ID, homework_text(with_id=False), disable_web_page_preview=True)
    await call.answer("✅ Отправлено в группу" if sent else "❌ Ошибка отправки")


# ---------- ссылки ----------
def links_menu(page: int = 0):
    b = InlineKeyboardBuilder()
    rows, page, pages = page_slice(get_links(), page, 20)
    for l in rows:
        b.row(Btn(f"🗑 #{l['id']} {l['title'][:25]}", f"lnk_del_{l['id']}"))
    add_page_buttons(b, page, pages, "lnk_page_")
    b.row(Btn("➕ Добавить", "lnk_add"), Btn("📤 В группу", "lnk_post"))
    b.row(Btn("🔙 Назад", "main_menu"))
    return b.as_markup()


def links_page_text(page: int = 0):
    links = get_links()
    if not links:
        return links_text()
    rows, page, pages = page_slice(links, page, 20)
    label = f"Всего: {len(links)} · страница {page + 1}/{pages}\n\n" if pages > 1 else ""
    return "🔗 <b>Полезные ссылки:</b>\n\n" + label + "\n".join(
        f"<code>#{item['id']}</code> <a href='{esc(item['url'])}'>{esc(item['title'])}</a>" for item in rows
    )


@admin_router.callback_query(F.data.regexp(r"^lnk_page_\d+$"))
async def link_page(call: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(call.data.removeprefix("lnk_page_"))
    await safe_edit(call, links_page_text(page), links_menu(page))


@admin_router.callback_query(F.data == "menu_links")
async def menu_links(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, links_page_text(), links_menu())


@admin_router.callback_query(F.data == "lnk_add")
async def lnk_add_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddLink.waiting_text)
    await safe_edit(call, "🔗 Введи ссылку:\n<code>Название | https://...</code>\n\n/cancel — отмена")


@admin_router.message(AddLink.waiting_text)
async def lnk_add_process(message: Message, state: FSMContext):
    ok, msg = add_link(message.text or "")
    if not ok:
        return await message.answer(msg)
    await state.clear()
    await safe_send(message.chat.id, msg + "\n\n" + links_page_text(), reply_markup=links_menu(), disable_web_page_preview=True)


@admin_router.callback_query(F.data.startswith("lnk_del_"))
async def lnk_del(call: CallbackQuery):
    conn = get_db()
    conn.execute("DELETE FROM links WHERE id = ?", (int(call.data.replace("lnk_del_", "")),))
    conn.commit()
    conn.close()
    await call.answer("Удалено")
    await safe_edit(call, links_page_text(), links_menu())


@admin_router.callback_query(F.data == "lnk_post")
async def lnk_post(call: CallbackQuery):
    sent = await safe_send(GROUP_CHAT_ID, links_text(with_id=False), disable_web_page_preview=True)
    await call.answer("✅ Отправлено" if sent else "❌ Ошибка")


# ---------- объявления ----------
@admin_router.callback_query(F.data == "menu_announce")
async def menu_announce(call: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.row(Btn("🔄 Изменение", "ann_reason_change"), Btn("❌ Отмена пары", "ann_reason_cancel"))
    b.row(Btn("📢 Сбор", "ann_reason_meeting"), Btn("📝 Домашка", "ann_reason_hw"))
    b.row(Btn("✍️ Своя", "ann_reason_custom"))
    b.row(Btn("🔙 Назад", "main_menu"))
    await safe_edit(call, "📢 Выберите причину:", b.as_markup())


@admin_router.callback_query(F.data.startswith("ann_reason_"))
async def set_reason(call: CallbackQuery, state: FSMContext):
    reasons = {"change": "Изменение расписания", "cancel": "Отмена пары", "meeting": "Общий сбор",
               "hw": "Домашнее задание", "custom": ""}
    reason = reasons.get(call.data.replace("ann_reason_", ""), "")
    await state.update_data(reason=reason)
    await state.set_state(AddAnnouncement.waiting_text)
    r = f"Причина: <b>{esc(reason)}</b>\n\n" if reason else ""
    await safe_edit(call, f"{r}Введите текст объявления:\n\n/cancel — отмена")


@admin_router.message(AddAnnouncement.waiting_text)
async def preview_announcement(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        return await message.answer("Введите текст объявления или /cancel.")
    data = await state.get_data()
    reason = data.get("reason", "")
    body = f"📢 <b>ВНИМАНИЕ!</b>\n\n" + (f"📌 <b>{esc(reason)}</b>\n\n" if reason else "") + esc(message.text or "")
    await state.update_data(body=body)
    await state.set_state(AddAnnouncement.waiting_confirm)
    b = InlineKeyboardBuilder()
    b.row(Btn("✅ Отправить", "ann_send"), Btn("📌 Отправить и закрепить", "ann_send_pin"))
    b.row(Btn("❌ Отмена", "main_menu"))
    await safe_send(message.chat.id, f"👀 <b>Предпросмотр:</b>\n\n{body}", reply_markup=b.as_markup())


@admin_router.callback_query(F.data.in_({"ann_send", "ann_send_pin"}), AddAnnouncement.waiting_confirm)
async def send_announcement(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sent = await safe_send(GROUP_CHAT_ID, data["body"])
    if sent and call.data == "ann_send_pin":
        try:
            await bot.pin_chat_message(GROUP_CHAT_ID, sent.message_id)
        except Exception as e:
            logger.error(f"pin: {e}")
    await state.clear()
    log_action(call.from_user.id, "Объявление в группу")
    await safe_edit(call, "✅ Отправлено!" if sent else "❌ Ошибка отправки. Бот в группе?", get_main_menu())


# ---------- настройки ----------
def settings_menu():
    b = InlineKeyboardBuilder()
    ev = get_setting("evening_enabled") == "1"
    pub = get_setting("public_commands") == "1"
    b.row(Btn(f"🌅 Утренняя рассылка: {get_setting('morning_time')}", "set_morning_time"))
    b.row(Btn(f"🌙 Вечерний анонс: {'вкл' if ev else 'выкл'}", "toggle_evening"),
          Btn(f"🕗 {get_setting('evening_time')}", "set_evening_time"))
    b.row(Btn(f"📝 Напоминание о ДЗ: {get_setting('hw_reminder_time')}", "set_hw_reminder_time"))
    b.row(Btn(f"🔔 За сколько мин. до пары: {get_setting('class_reminder_minutes')} (0 = выкл)", "set_class_reminder_minutes"))
    b.row(Btn(f"👥 Команды для всех: {'вкл' if pub else 'выкл'}", "toggle_public"))
    b.row(Btn("🔙 Назад", "main_menu"))
    return b.as_markup()


@admin_router.callback_query(F.data == "menu_settings")
async def menu_settings(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "⚙️ <b>Настройки</b>\n\n«Команды для всех» — разрешить обычным участникам "
                          "смотреть расписание, ДЗ и ссылки в группе.", settings_menu())


@admin_router.callback_query(F.data == "toggle_evening")
async def toggle_evening(call: CallbackQuery):
    set_setting("evening_enabled", "0" if get_setting("evening_enabled") == "1" else "1")
    await safe_edit(call, "⚙️ <b>Настройки</b>", settings_menu())


@admin_router.callback_query(F.data == "toggle_public")
async def toggle_public(call: CallbackQuery):
    set_setting("public_commands", "0" if get_setting("public_commands") == "1" else "1")
    await safe_edit(call, "⚙️ <b>Настройки</b>", settings_menu())


@admin_router.callback_query(F.data.startswith("set_"))
async def set_setting_start(call: CallbackQuery, state: FSMContext):
    key = call.data.replace("set_", "")
    if key not in DEFAULT_SETTINGS:
        return await call.answer("Неизвестная настройка")
    await state.update_data(key=key)
    await state.set_state(SetSetting.waiting_value)
    hint = "минуты, например <code>10</code> (0 — выключить)" if key == "class_reminder_minutes" else "время в формате <code>ЧЧ:ММ</code>"
    await safe_edit(call, f"Введи {hint}:\n\n/cancel — отмена")


@admin_router.message(SetSetting.waiting_value)
async def set_setting_process(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["key"]
    val = (message.text or "").strip()
    if key == "class_reminder_minutes":
        if not val.isdigit() or int(val) > 180:
            return await message.answer("❌ Нужно число от 0 до 180.")
    else:
        if not re.fullmatch(r"\d{1,2}:\d{2}", val):
            return await message.answer("❌ Формат ЧЧ:ММ, например 07:30")
        try:
            val = datetime.strptime(val, "%H:%M").strftime("%H:%M")
        except ValueError:
            return await message.answer("❌ Неверное время.")
    set_setting(key, val)
    apply_schedule_settings()
    await state.clear()
    log_action(message.from_user.id, f"Настройка {key} = {val}")
    await message.answer("✅ Сохранено!", reply_markup=settings_menu())


# ---------- управление ----------
def admin_menu_text_and_markup():
    conn = get_db()
    admins = conn.execute("SELECT user_id FROM admins").fetchall()
    conn.close()
    b = InlineKeyboardBuilder()
    text = "👑 <b>Админы:</b>\n"
    for a in admins:
        uid = a["user_id"]
        text += f"• <code>{uid}</code>{' (главный)' if uid == MAIN_ADMIN_ID else ''}\n"
        if uid != MAIN_ADMIN_ID:
            b.row(Btn(f"❌ Убрать {uid}", f"adm_del_{uid}"))
    b.row(Btn("➕ Добавить зама", "add_admin"))
    b.row(Btn("💾 Бэкап", "manual_backup"), Btn("📂 Восстановить", "restore_list"))
    b.row(Btn("📜 Журнал", "admin_log"), Btn("📊 Статистика", "admin_stats"))
    b.row(Btn("🔙 Назад", "main_menu"))
    return text, b.as_markup()


@admin_router.callback_query(F.data == "menu_admin")
async def menu_admin(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = admin_menu_text_and_markup()
    await safe_edit(call, text, markup)


@admin_router.callback_query(F.data == "add_admin")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != MAIN_ADMIN_ID:
        return await call.answer("Добавлять замов может главный админ.", show_alert=True)
    await state.set_state(AddNewAdmin.waiting_id)
    await safe_edit(call, "Перешли сообщение от зама или введи его ID (он может узнать его командой /id).\n\n/cancel — отмена")


@admin_router.message(AddNewAdmin.waiting_id)
async def add_admin_process(message: Message, state: FSMContext):
    if message.from_user.id != MAIN_ADMIN_ID:
        await state.clear()
        return await message.answer("Добавлять замов может главный админ.")
    new_id = None
    if isinstance(message.forward_origin, MessageOriginUser):
        new_id = message.forward_origin.sender_user.id
    elif message.text and message.text.strip().isdigit():
        new_id = int(message.text.strip())
    if not new_id or not 0 < new_id <= 9223372036854775807:
        return await message.answer("❌ Не удалось определить ID (у пользователя может быть скрыт профиль — пусть пришлёт /id).")
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_id,))
    conn.commit()
    conn.close()
    await state.clear()
    log_action(message.from_user.id, f"Добавлен админ {new_id}")
    text, markup = admin_menu_text_and_markup()
    await message.answer(f"✅ Зам <code>{new_id}</code> добавлен!\n\n{text}", reply_markup=markup)


@admin_router.callback_query(F.data.startswith("adm_del_"))
async def admin_remove(call: CallbackQuery):
    uid = int(call.data.replace("adm_del_", ""))
    if uid == MAIN_ADMIN_ID:
        return await call.answer("Нельзя удалить главного.")
    if call.from_user.id != MAIN_ADMIN_ID:
        return await call.answer("Только главный админ может убирать замов.", show_alert=True)
    conn = get_db()
    conn.execute("DELETE FROM admins WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()
    log_action(call.from_user.id, f"Удалён админ {uid}")
    await call.answer("Убран.")
    text, markup = admin_menu_text_and_markup()
    await safe_edit(call, text, markup)


@admin_router.callback_query(F.data == "manual_backup")
async def manual_backup(call: CallbackQuery):
    created = backup_database("manual")
    await call.answer("✅ Бэкап создан!" if created else "❌ Не удалось создать бэкап.")


@admin_router.callback_query(F.data == "restore_list")
async def restore_list(call: CallbackQuery):
    backups = list_backups()[-8:][::-1]
    if not backups:
        return await call.answer("Бэкапов нет.")
    b = InlineKeyboardBuilder()
    for name in backups:
        pretty = name.replace("bot_data_", "").replace(".db", "")
        b.row(Btn(f"📂 {pretty}", f"restore_ask_{backup_key(name)}"))
    b.row(Btn("🔙 Назад", "menu_admin"))
    await safe_edit(call, "📂 Выбери бэкап для восстановления:", b.as_markup())


@admin_router.callback_query(F.data.startswith("restore_ask_"))
async def restore_ask(call: CallbackQuery):
    name = resolve_backup(call.data.replace("restore_ask_", ""))
    if not name:
        return await call.answer("Не найден.")
    b = InlineKeyboardBuilder()
    b.row(Btn("✅ ДА, восстановить", f"restore_go_{backup_key(name)}"), Btn("❌ Отмена", "menu_admin"))
    await safe_edit(call, f"⚠️ Восстановить из <code>{esc(name)}</code>?\nТекущая база будет заменена (перед этим сделаю бэкап).", b.as_markup())


@admin_router.callback_query(F.data.startswith("restore_go_"))
async def restore_go(call: CallbackQuery):
    if call.from_user.id != MAIN_ADMIN_ID:
        return await call.answer("Только главный админ.", show_alert=True)
    name = resolve_backup(call.data.replace("restore_go_", ""))
    if not name:
        return await call.answer("Не найден.")
    ok = restore_backup(name)
    if ok:
        init_db()
        restore_reminders()
        apply_schedule_settings()
    log_action(call.from_user.id, f"Восстановление из {name}: {'OK' if ok else 'ошибка'}")
    text, markup = admin_menu_text_and_markup()
    await safe_edit(call, ("✅ База восстановлена!\n\n" if ok else "❌ Ошибка восстановления.\n\n") + text, markup)


@admin_router.callback_query(F.data == "admin_log")
async def admin_log_view(call: CallbackQuery):
    conn = get_db()
    rows = conn.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()
    text = "📜 <b>Последние действия:</b>\n\n" + ("\n".join(f"<code>{r['ts']}</code> [{r['user_id']}] {esc(r['action'])}" for r in rows) or "пусто")
    b = InlineKeyboardBuilder()
    b.row(Btn("🔙 Назад", "menu_admin"))
    await safe_edit(call, text, b.as_markup())


@admin_router.callback_query(F.data == "admin_stats")
async def admin_stats_menu(call: CallbackQuery):
    b = InlineKeyboardBuilder()
    b.row(Btn("🔙 Назад", "menu_admin"))
    await safe_edit(call, stats_text(), b.as_markup())


# ============================ ГРУППА ============================
group_router.message.filter(F.chat.id == GROUP_CHAT_ID)


@group_router.message(F.new_chat_members)
async def on_join(message: Message):
    for u in message.new_chat_members:
        if u.is_bot:
            continue
        touch_member(u)
        if get_setting("welcome_enabled", "1") == "1":
            await safe_send(message.chat.id, render_welcome_message(u, message.chat.title or ""))


@group_router.message(F.left_chat_member)
async def on_left(message: Message):
    conn = get_db()
    conn.execute("DELETE FROM group_members WHERE user_id = ?", (message.left_chat_member.id,))
    conn.commit()
    conn.close()


@group_router.message(F.text)
async def group_commands(message: Message):
    try:
        user = message.from_user
        if not user or user.is_bot:
            return
        touch_member(user)
        text = message.text.strip()
        low = text.lower()
        if not low:
            return
        admin = is_admin(user.id)
        if (admin or get_setting("public_commands") == "1") and await handle_public_command(message, text, low):
            return
        if admin:
            await handle_admin_command(message, text, low)
    except Exception as e:
        logger.exception(f"Ошибка в group_commands: {e}")


async def handle_public_command(message: Message, text: str, low: str) -> bool:
    chat_id = message.chat.id
    now = get_now()

    if low in ("расписание на сегодня", "что сегодня", "пары сегодня", "сегодня", "расписание"):
        await send_schedule_to_chat(chat_id, now.date())
    elif low in ("расписание на завтра", "что завтра", "пары завтра", "завтра"):
        await send_schedule_to_chat(chat_id, now.date() + timedelta(days=1))
    elif low.startswith("расписание на "):
        day = low.replace("расписание на ", "").strip()
        if day in ("неделю", "неделя"):
            await send_week_schedule(chat_id, now.date())
        elif day in ("следующую неделю", "след неделю"):
            await send_week_schedule(chat_id, now.date() + timedelta(days=7))
        elif day in DAYS_KW:
            target_idx = DAYS_ORDER.index(DAYS_KW[day])
            curr_idx = now.weekday()
            days_ahead = target_idx - curr_idx
            if days_ahead <= 0: days_ahead += 7
            if days_ahead == 7 and target_idx == curr_idx: days_ahead = 0
            await send_schedule_to_chat(chat_id, now.date() + timedelta(days=days_ahead))
        else:
            return False
    elif low in ("вся неделя", "неделя"):
        await send_week_schedule(chat_id, now.date())
    elif low in ("следующая неделя", "расписание на следующую неделю", "след неделя"):
        await send_week_schedule(chat_id, now.date() + timedelta(days=7))
    elif low in ("следующая пара", "что дальше", "дальше"):
        await show_next_class(chat_id, now)
    elif low in ("какая пара сейчас", "текущая пара", "сейчас"):
        await show_current_class(chat_id, now)
    elif low in ("до конца пары", "сколько до конца", "до конца"):
        await show_time_left(chat_id, now)
    elif low in ("дз", "домашка", "домашнее задание", "что по дз"):
        await safe_send(chat_id, homework_text(with_id=is_admin(message.from_user.id)))
    elif low in ("дз все", "дз всё", "вся домашка"):
        await safe_send(chat_id, homework_text(include_expired=True))
    elif low in ("ссылки", "полезные ссылки"):
        await safe_send(chat_id, links_text(with_id=is_admin(message.from_user.id)), disable_web_page_preview=True)
    elif low in ("события", "дедлайны", "что скоро"):
        await safe_send(chat_id, events_text())
    elif low in ("помощь", "команды", "помоги", "хелп"):
        await show_help(chat_id, is_admin(message.from_user.id))
    elif low in ("мой id", "мой айди", "айди"):
        await message.reply(f"🆔 <code>{message.from_user.id}</code>")
    elif low in ("монетка", "орёл или решка", "орел или решка"):
        await message.reply(f"🪙 {random.choice(['Орёл 🦅', 'Решка 🔢'])}")
    elif low in ("кубик", "брось кубик"):
        await bot.send_dice(chat_id)
    else:
        return False
    return True


async def handle_admin_command(message: Message, text: str, low: str):
    chat_id = message.chat.id
    now = get_now()
    reply = message.reply_to_message
    target = reply.from_user if reply else None
    if target and (low in ("бан", "мут", "варн") or low.startswith(("мут ", "варн "))):
        if target.is_bot or is_admin(target.id):
            return await message.answer("🛡 Ботов и администраторов бота нельзя наказывать этой командой.")

    # ---- объявления ----
    if low == "кал" or low.startswith("кал "):
        msg_text = text[3:].strip()
        body = f"📢 <b>ВНИМАНИЕ!</b>\n\n{esc(msg_text)}" if msg_text else "📢 <b>ВНИМАНИЕ ВСЕЙ ГРУППЕ!</b> 📢"
        await safe_send(chat_id, body)
    elif low in ("тег всех", "внимание всем", "сбор", "всем внимание", "@all", "@все"):
        await tag_everyone(chat_id)
    elif low.startswith("опрос "):
        question = text[6:].strip()
        try:
            await bot.send_poll(chat_id, question=question[:300], options=["Иду", "Не иду", "Опоздаю", "Дистант"], is_anonymous=False)
        except Exception as e:
            await message.answer(f"❌ Ошибка опроса: {esc(e)}")
    elif low in ("перекличка", "кто на паре"):
        try:
            await bot.send_poll(chat_id, question="📋 Перекличка! Кто где?", options=["✅ Тут", "🏃 Опоздаю", "❌ Отсутствую", "💻 Дистант"], is_anonymous=False)
        except Exception as e:
            await message.answer(f"❌ Ошибка: {esc(e)}")
    elif low in ("рандом", "кто", "выбрать", "кто дежурный"):
        conn = get_db()
        members = conn.execute("SELECT user_id, username FROM group_members").fetchall()
        conn.close()
        if not members:
            return await message.answer("Некого выбирать — список участников пуст.")
        chosen = random.choice(members)
        await safe_send(chat_id, f"🎲 Мой выбор: {mention(chosen['user_id'], chosen['username'])}!")
    # ---- домашка / ссылки / события ----
    elif low.startswith("дз добавить "):
        ok, msg = add_homework(text[len("дз добавить "):], message.from_user.id)
        await message.reply(msg)
    elif low.startswith("дз удалить "):
        arg = low.replace("дз удалить ", "").strip().lstrip("#")
        if arg.isdigit() and delete_homework(int(arg)):
            await message.reply(f"✅ ДЗ #{arg} удалено.")
        else:
            await message.reply("❌ Не нашёл такое ДЗ. Посмотри номера: <code>дз</code>")
    elif low.startswith("ссылка добавить "):
        ok, msg = add_link(text[len("ссылка добавить "):])
        await message.reply(msg)
    elif low.startswith("ссылка удалить "):
        arg = low.replace("ссылка удалить ", "").strip().lstrip("#")
        if arg.isdigit():
            conn = get_db()
            cur = conn.execute("DELETE FROM links WHERE id = ?", (int(arg),))
            conn.commit()
            conn.close()
            await message.reply("✅ Удалено." if cur.rowcount else "❌ Не нашёл.")
    elif low.startswith("событие добавить "):
        parts = [p.strip() for p in text[len("событие добавить "):].split("|")]
        d = parse_date(parts[1]) if len(parts) >= 2 else None
        if not d or not parts[0] or len(parts[0]) > 200 or len(parts) != 2:
            return await message.reply("⚠️ Формат: <code>событие добавить Сессия | 15.01</code>")
        conn = get_db()
        conn.execute("INSERT INTO events (title, date) VALUES (?, ?)", (parts[0], d.isoformat()))
        conn.commit()
        conn.close()
        await message.reply(f"✅ Событие «{esc(parts[0])}» — {d.strftime('%d.%m.%Y')} ({days_left_str(d)})")
    elif low.startswith("событие удалить "):
        arg = low.replace("событие удалить ", "").strip().lstrip("#")
        if arg.isdigit():
            conn = get_db()
            cur = conn.execute("DELETE FROM events WHERE id = ?", (int(arg),))
            conn.commit()
            conn.close()
            await message.reply("✅ Удалено." if cur.rowcount else "❌ Не нашёл.")
    # ---- инструменты ----
    elif low in ("экспорт расписания", "экспорт"):
        await export_schedule(chat_id)
    elif low.startswith("напомни через "):
        await handle_reminder(message, text)
    elif low.startswith("напомни в "):
        await handle_reminder_at(message, text)
    elif low in ("напоминания", "список напоминаний"):
        conn = get_db()
        rows = conn.execute("SELECT * FROM reminders WHERE chat_id = ? ORDER BY run_at", (chat_id,)).fetchall()
        conn.close()
        if not rows:
            return await message.reply("⏰ Активных напоминаний нет.")
        lines = [f"<code>#{r['id']}</code> {datetime.fromisoformat(r['run_at']).strftime('%d.%m %H:%M')} — {esc(r['text'])}" for r in rows]
        await safe_send(chat_id, "⏰ <b>Напоминания:</b>\n" + "\n".join(lines))
    elif low == "закрепи" and reply:
        try:
            await reply.pin(disable_notification=False)
            await message.answer("📌 Закреплено.")
        except Exception as e:
            await message.answer(f"❌ Не могу: {esc(e)}")
    elif low == "открепи" and reply:
        try:
            await reply.unpin()
            await message.answer("📌 Откреплено.")
        except Exception as e:
            await message.answer(f"❌ Не могу: {esc(e)}")
    elif low in ("статистика", "стата", "стат"):
        await safe_send(chat_id, stats_text())
    # ---- модерация ----
    elif re.fullmatch(r"мут(\s+.+)?", low) and reply:
        if not target:
            return await message.answer("❌ Не вижу юзера.")
        duration_str = low[3:].strip()
        seconds = parse_duration(duration_str) if duration_str else None
        if not seconds:
            return await message.answer("⚠️ Укажи время: <code>мут 20m</code>, <code>мут 2h</code>, <code>мут 1d</code>")
        await mute_user(chat_id, target, seconds, message)
    elif low == "размут" and target:
        try:
            await bot.restrict_chat_member(chat_id, target.id, permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True))
            await message.answer(f"🔊 {esc(target.full_name)} размучен.")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {esc(e)}")
    elif low == "бан" and target:
        try:
            await bot.ban_chat_member(chat_id, target.id)
            log_action(message.from_user.id, f"Бан {target.id}")
            await message.answer(f"🔨 {esc(target.full_name)} забанен.")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {esc(e)}")
    elif low == "разбан" and target:
        try:
            await bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
            await message.answer(f"✅ {esc(target.full_name)} разбанен.")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {esc(e)}")
    elif low == "удали" and reply:
        try:
            await reply.delete()
            await message.delete()
        except Exception as e:
            await message.answer(f"❌ Не могу: {esc(e)}")
    elif (low == "варн" or low.startswith("варн ")) and target:
        reason = text[4:].strip()
        count = change_warns(target.id, +1)
        txt = f"⚠️ {mention(target.id, target.full_name)} получает предупреждение <b>{count}/3</b>"
        if reason:
            txt += f"\nПричина: {esc(reason)}"
        await safe_send(chat_id, txt)
        log_action(message.from_user.id, f"Варн {target.id} ({count})")
        if count >= 3:
            if await mute_user(chat_id, target, 86400, message, prefix="🚫 3 предупреждения! "):
                reset_warns(target.id)
    elif low == "варны" and target:
        conn = get_db()
        row = conn.execute("SELECT count FROM warns WHERE user_id = ?", (target.id,)).fetchone()
        conn.close()
        await message.reply(f"⚠️ У {esc(target.full_name)}: <b>{row['count'] if row else 0}/3</b> предупреждений")
    elif low in ("снять варн", "снять предупреждение") and target:
        count = change_warns(target.id, -1)
        await message.reply(f"✅ Снято. Теперь у {esc(target.full_name)}: {count}/3")


async def mute_user(chat_id: int, target: User, seconds: int, message: Message, prefix: str = ""):
    if not 30 <= seconds <= 365 * 86400:
        await message.answer("⚠️ Мут должен длиться от 30 секунд до 365 дней.")
        return False
    try:
        until = get_now() + timedelta(seconds=seconds)
        await bot.restrict_chat_member(chat_id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=until)
        human = f"{seconds // 86400} д." if seconds >= 86400 else f"{seconds // 3600} ч." if seconds >= 3600 else f"{seconds // 60} мин." if seconds >= 60 else f"{seconds} сек."
        await message.answer(f"{prefix}🔇 {esc(target.full_name)} в муте на {human}")
        log_action(message.from_user.id, f"Мут {target.id} на {human}")
        return True
    except Exception as e:
        await message.answer(f"❌ Ошибка (бот должен быть админом): {esc(e)}")
        return False


# ---------- вывод в чат ----------
async def send_schedule_to_chat(chat_id, target_date: date):
    classes = get_classes_for_date(target_date)
    if SCHEDULE_FEED is not None:
        text = f"<b>П-114</b>\n{day_label(target_date)}\n\n" + ("\n\n".join(fmt_class(c) for c in classes) if classes else "На сайте нет занятий на эту дату.")
        await safe_send(chat_id, text)
        return
    day_name = DAYS_RU[DAYS_ORDER[target_date.weekday()]]
    if classes:
        text = f"📅 <b>Расписание на {day_name.lower()} ({fmt_date(target_date)}):</b>\n\n" + "\n".join(fmt_class(c) for c in classes)
    else:
        text = f"🎉 На {day_name.lower()} ({fmt_date(target_date)}) пар нет!"
    await safe_send(chat_id, text)


async def send_week_schedule(chat_id, target_date: date):
    monday = get_monday(target_date)
    if SCHEDULE_FEED is not None:
        text = "<b>П-114</b>\n"
        for i in range(7):
            d = monday + timedelta(days=i)
            classes = get_classes_for_date(d)
            text += f"\n{day_label(d)}\n\n" + ("\n\n".join(fmt_class(c) for c in classes) if classes else "На сайте нет занятий на эту дату.") + "\n"
        await safe_send(chat_id, text)
        return
    days = 7 if get_classes_for_date(monday + timedelta(days=6)) else 6
    last_day = monday + timedelta(days=days - 1)
    text = f"📅 <b>РАСПИСАНИЕ (Неделя {fmt_short(monday)}-{fmt_short(last_day)}):</b>\n\n"
    for i in range(days):
        d = monday + timedelta(days=i)
        classes = get_classes_for_date(d)
        text += f"<b>{DAYS_RU[DAYS_ORDER[i]]} ({fmt_short(d)})</b>\n"
        text += ("\n".join("  " + fmt_class(c) for c in classes) if classes else "  — пар нет —") + "\n\n"
    await safe_send(chat_id, text)


async def export_schedule(chat_id):
    now = get_now().date()
    monday = get_monday(now)
    text = ""
    for i in range(7):
        d = monday + timedelta(days=i)
        classes = get_classes_for_date(d)
        if not classes:
            continue
        text += f"{DAYS_RU[DAYS_ORDER[i]]}\n" + "\n".join(f"{idx}. {c['time']} {c['subject']} ({c['room']})" for idx, c in enumerate(classes, 1)) + "\n\n"
    if not text.strip():
        text = "Расписание пустое."
    await safe_send(chat_id, f"📋 <b>Экспорт этой недели</b> (этот текст можно снова загрузить в бота):\n\n<code>{esc(text)}</code>")


async def show_next_class(chat_id, now: datetime):
    for c in get_classes_for_date(now.date()):
        start, _ = parse_time_range(c["time"])
        if start and start > now.time():
            start_dt = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
            mins = int((start_dt - now).total_seconds() // 60)
            if SCHEDULE_FEED is not None:
                await safe_send(chat_id, f"<b>П-114</b>\n{day_label(now)}\n\n" + fmt_class(c))
            else:
                await safe_send(chat_id, f"⏭ <b>Следующая пара</b> (через {mins} мин.):\n🕐 {esc(c['time'])}\n📚 {esc(c['subject'])}\n🚪 {esc(c['room'])}")
            return
    await safe_send(chat_id, "🎉 На сегодня больше пар нет!")


async def show_current_class(chat_id, now: datetime):
    for c in get_classes_for_date(now.date()):
        start, end = parse_time_range(c["time"])
        if start and end and start <= now.time() < end:
            if SCHEDULE_FEED is not None:
                await safe_send(chat_id, f"<b>П-114</b>\n{day_label(now)}\n\n" + fmt_class(c))
            else:
                await safe_send(chat_id, f"📖 <b>Сейчас идёт:</b>\n🕐 {esc(c['time'])}\n📚 {esc(c['subject'])}\n🚪 {esc(c['room'])}")
            return
    await safe_send(chat_id, "😴 Сейчас нет пары.")


async def show_time_left(chat_id, now: datetime):
    for c in get_classes_for_date(now.date()):
        start, end = parse_time_range(c["time"])
        if start and end and start <= now.time() < end:
            end_dt = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
            mins = int((end_dt - now).total_seconds() // 60)
            await safe_send(chat_id, f"⏳ До конца пары «{esc(c['subject'])}» осталось <b>{mins} мин.</b>")
            return
    await safe_send(chat_id, "😴 Сейчас нет пары — нечего ждать.")


async def tag_everyone(chat_id):
    conn = get_db()
    members = conn.execute("SELECT user_id, username FROM group_members").fetchall()
    conn.close()
    if not members:
        return await safe_send(chat_id, "Список пуст — я запоминаю тех, кто писал в чат.")
    header = "📢 <b>ВНИМАНИЕ ВСЕЙ ГРУППЕ!</b>\n\n"
    tags = ""
    for m in members:
        tag = f"👤 {mention(m['user_id'], m['username'])}\n"
        if len(header + tags + tag) > 3900:
            await safe_send(chat_id, header + tags)
            tags = ""
        tags += tag
    if tags:
        await safe_send(chat_id, header + tags)


async def handle_reminder(message: Message, text: str):
    m = re.match(r"напомни через (\d+\s*(?:s|с|m|м|h|ч|d|д)?)\s+(.+)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return await message.answer("⚠️ Формат: <code>напомни через 20m Текст</code>")
    seconds = parse_duration(m.group(1))
    if not seconds:
        return await message.answer("❌ Не понял время.")
    run_time = get_now() + timedelta(seconds=seconds)
    add_reminder(message.chat.id, m.group(2).strip(), run_time)
    await message.answer(f"⏰ Напомню в {run_time.strftime('%H:%M')} ({m.group(1).strip()}):\n«{esc(m.group(2).strip())}»")


async def handle_reminder_at(message: Message, text: str):
    m = re.match(r"напомни в (\d{1,2}:\d{2})\s+(.+)", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return await message.answer("⚠️ Формат: <code>напомни в 18:30 Текст</code>")
    try:
        t = datetime.strptime(m.group(1), "%H:%M").time()
    except ValueError:
        return await message.answer("❌ Неверное время.")
    now = get_now()
    run_time = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if run_time <= now:
        run_time += timedelta(days=1)
    add_reminder(message.chat.id, m.group(2).strip(), run_time)
    when = "сегодня" if run_time.date() == now.date() else "завтра"
    await message.answer(f"⏰ Напомню {when} в {run_time.strftime('%H:%M')}:\n«{esc(m.group(2).strip())}»")


def stats_text() -> str:
    conn = get_db()
    members = conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0]
    classes = conn.execute("SELECT COUNT(*) FROM schedule WHERE date IS NOT NULL").fetchone()[0]
    admins = conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    reminders = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    conn.close()
    uptime = get_now() - START_TIME
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m = rem // 60
    return (f"📊 <b>Статистика:</b>\n\n👥 Участников: <b>{members}</b>\n📚 Пар в базе: <b>{classes}</b>\n"
            f"📝 Активных ДЗ: <b>{len(get_homework())}</b>\n📆 Событий: <b>{len(get_events())}</b>\n"
            f"⏰ Напоминаний: <b>{reminders}</b>\n👑 Админов: <b>{admins}</b>\n💾 Бэкапов: <b>{len(list_backups())}</b>\n"
            f"🕐 Время: <b>{get_now().strftime('%H:%M:%S')}</b> | Аптайм: <b>{h}ч {m}м</b>\n"
            f"📅 Сегодня: <b>{fmt_date(get_now().date())}</b>")


async def show_help(chat_id, admin: bool):
    await safe_send(chat_id, get_help_text(admin))


# Additional schedule tools. This fragment is inserted into bot.py after the
# existing handlers. Existing date-based adding and week-text parsing stay intact.
import csv
import hashlib
import io
import json
import uuid
from collections import Counter
from datetime import timezone as _schedule_utc
from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile


schedule_extra_router = Router(name="schedule_extras")


class ScheduleExtrasState(StatesGroup):
    edit_text = State()
    edit_confirm = State()
    copy_target = State()
    copy_confirm = State()
    search_text = State()


class SchedulePlanChanged(ValueError):
    """The schedule changed after a preview; a new preview is required."""


def schedule_parse_date(value: str) -> date:
    """Use the original date parser, while rejecting unrecognised dates."""
    parsed = parse_date(str(value).strip())
    if parsed is None:
        raise ValueError("Укажи дату: 25.10.2026, 2026-10-25, сегодня или завтра.")
    return parsed


def schedule_validate_class(raw: str) -> dict:
    """Validate one lesson using the established lesson input format."""
    if len(raw) > 1000 or len(raw.strip().splitlines()) != 1:
        raise ValueError("Введи одну пару, не длиннее 1000 символов.")
    match = CLASS_LINE_RE.fullmatch(raw.strip())
    if not match:
        raise ValueError("Формат: 13:00-14:30 Предмет (Аудитория).")
    parsed = parse_class_line(raw)
    if parsed is None:
        raise ValueError("Проверь формат пары, время и длину названия предмета.")
    start, end = parse_time_range(parsed["time"])
    if start is None or end is None or end <= start:
        raise ValueError("Проверь время: окончание пары должно быть позже начала.")
    subject, room = parsed["subject"].strip(), parsed["room"].strip()
    if not subject or len(subject) > 200 or len(room) > 100:
        raise ValueError("Предмет — 1–200 символов, аудитория — до 100 символов.")
    if any(ord(char) < 32 for char in subject + room):
        raise ValueError("Текст пары содержит недопустимые управляющие символы.")
    return {"time": f"{start:%H:%M}-{end:%H:%M}", "subject": subject, "room": room or "—"}


def schedule_sort_key(row: dict):
    start, _ = parse_time_range(row.get("time") or "")
    return row.get("date") or "", start or datetime.min.time(), row.get("id") or 0


def _schedule_preview_class(row: dict) -> str:
    # Historical data can predate input length limits. Keep Telegram previews small.
    limited = {}
    for field, maximum in (("time", 40), ("subject", 100), ("room", 40)):
        value = str(row.get(field) or "—")
        limited[field] = value if len(value) <= maximum else value[:maximum - 1] + "…"
    limited["id"] = row.get("id")
    return fmt_class(limited)


def schedule_rows_between(start: date, end: date, connection=None) -> list[dict]:
    if end < start:
        raise ValueError("Конец периода должен быть не раньше начала.")
    if connection is not None:
        result = connection.execute(
            "SELECT id, date, time, subject, room FROM schedule WHERE date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return sorted((dict(row) for row in result), key=schedule_sort_key)
    with db_session() as connection:
        return schedule_rows_between(start, end, connection)


def schedule_get_class(class_id: int, connection=None) -> dict | None:
    if connection is not None:
        row = connection.execute(
            "SELECT id, date, time, subject, room FROM schedule WHERE id=?", (class_id,)
        ).fetchone()
        return dict(row) if row and row["date"] else None
    with db_session() as connection:
        return schedule_get_class(class_id, connection)


def schedule_snapshot(rows: list[dict]) -> str:
    encoded = json.dumps(sorted(rows, key=schedule_sort_key), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def schedule_build_copy_plan(source: date, target: date, days: int = 1) -> dict:
    """Snapshot both sides. Empty source days never erase target lessons."""
    if days not in (1, 7):
        raise ValueError("Можно копировать один день или семь дней недели.")
    if source == target:
        raise ValueError("Исходная дата и дата назначения совпадают.")
    source_end = source + timedelta(days=days - 1)
    target_end = target + timedelta(days=days - 1)
    if max(source, target) <= min(source_end, target_end):
        raise ValueError("Исходный период и период назначения не должны пересекаться.")
    with db_session() as connection:
        # Explicit read transaction keeps source and destination snapshots consistent.
        connection.execute("BEGIN")
        source_rows = schedule_rows_between(source, source_end, connection)
        target_rows = schedule_rows_between(target, target_end, connection)
    if not source_rows:
        raise ValueError("В выбранном периоде нет пар для копирования.")
    by_target = {}
    for row in source_rows:
        destination = (date.fromisoformat(row["date"]) + (target - source)).isoformat()
        by_target.setdefault(destination, []).append(
            {"time": row["time"], "subject": row["subject"], "room": row["room"]}
        )
    occupied = sorted({row["date"] for row in target_rows if row["date"] in by_target})
    return {
        "source": source.isoformat(), "target": target.isoformat(), "days": days,
        "source_snapshot": schedule_snapshot(source_rows),
        "target_snapshot": schedule_snapshot(target_rows), "by_target": by_target,
        "occupied": occupied, "token": uuid.uuid4().hex[:16],
    }


def schedule_apply_copy_plan(plan: dict, replace_occupied: bool = False,
                             skip_occupied: bool = False) -> dict:
    """Atomic copy with optimistic checks; replacement requires an explicit flag."""
    if replace_occupied and skip_occupied:
        raise ValueError("Выбери только один способ обработки занятых дней.")
    source, target = date.fromisoformat(plan["source"]), date.fromisoformat(plan["target"])
    days = int(plan["days"])
    if days not in (1, 7) or source == target:
        raise ValueError("Недействительный план копирования.")
    occupied = set(plan["occupied"])
    if occupied and not replace_occupied and not skip_occupied:
        raise ValueError("Занятые дни требуют явного подтверждения замены или пропуска.")
    targets = {key: value for key, value in plan["by_target"].items()
               if not (skip_occupied and key in occupied)}
    if not targets:
        raise ValueError("После пропуска занятых дней копировать нечего.")
    if not backup_database("before_schedule_copy"):
        raise ValueError("Не удалось создать резервную копию; расписание не изменено.")
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_source = schedule_rows_between(source, source + timedelta(days=days - 1), connection)
        current_target = schedule_rows_between(target, target + timedelta(days=days - 1), connection)
        if (schedule_snapshot(current_source) != plan["source_snapshot"] or
                schedule_snapshot(current_target) != plan["target_snapshot"]):
            raise SchedulePlanChanged("Расписание изменилось после предпросмотра. Создай новый предпросмотр.")
        for destination, lessons in targets.items():
            if destination in occupied:
                connection.execute("DELETE FROM schedule WHERE date=?", (destination,))
            connection.executemany(
                "INSERT INTO schedule (date,time,subject,room) VALUES (?,?,?,?)",
                [(destination, lesson["time"], lesson["subject"], lesson["room"])
                 for lesson in lessons],
            )
    return {"days": len(targets), "classes": sum(map(len, targets.values())),
            "skipped_days": len(occupied) if skip_occupied else 0}


def schedule_update_class(class_id: int, expected: dict, lesson: dict) -> dict:
    """Update in place, preserving the row id and its original calendar date."""
    validated = schedule_validate_class(
        f"{lesson['time']} {lesson['subject']} ({lesson['room']})"
    )
    if not backup_database("before_schedule_edit"):
        raise ValueError("Не удалось создать резервную копию; расписание не изменено.")
    with db_session() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = schedule_get_class(class_id, connection)
        if not current or current != expected:
            raise SchedulePlanChanged("Пара уже изменена или удалена. Открой её снова.")
        connection.execute("UPDATE schedule SET time=?,subject=?,room=? WHERE id=?",
                           (validated["time"], validated["subject"], validated["room"], class_id))
    return {**expected, **validated}


def schedule_search_rows(query: str, field: str = "all", start: date | None = None,
                         end: date | None = None, limit: int = 50) -> tuple[list[dict], int]:
    query = query.strip().casefold()
    if not query or len(query) > 100:
        raise ValueError("Запрос должен содержать от 1 до 100 символов.")
    if field not in ("all", "subject", "room"):
        raise ValueError("Поиск доступен по предмету и аудитории.")
    start = start or get_now().date()
    end = end or (start + timedelta(days=366))
    fields = ("subject", "room") if field == "all" else (field,)
    found = [row for row in schedule_rows_between(start, end)
             if any(query in str(row.get(column) or "").casefold() for column in fields)]
    return found[:max(1, min(int(limit), 100))], len(found)


def schedule_week_load(rows: list[dict], monday: date) -> dict:
    """Compute known teaching minutes and independently flag timetable collisions."""
    result = {"classes": 0, "minutes": 0, "unknown_duration": 0,
              "subjects": {}, "days": [], "overlaps": []}
    subject_counts = Counter()
    for offset in range(7):
        current = monday + timedelta(days=offset)
        lessons = sorted((row for row in rows if row.get("date") == current.isoformat()),
                         key=schedule_sort_key)
        minutes, intervals = 0, []
        for lesson in lessons:
            result["classes"] += 1
            subject_counts[str(lesson.get("subject") or "Без предмета")] += 1
            start, end = parse_time_range(lesson.get("time") or "")
            if start is None or end is None or end <= start:
                result["unknown_duration"] += 1
                continue
            a, b = start.hour * 60 + start.minute, end.hour * 60 + end.minute
            minutes += b - a
            for previous_start, previous_end, previous in intervals:
                if max(a, previous_start) < min(b, previous_end):
                    result["overlaps"].append({"date": current.isoformat(),
                                               "first": previous, "second": lesson})
            intervals.append((a, b, lesson))
        result["minutes"] += minutes
        result["days"].append({"date": current.isoformat(), "classes": len(lessons), "minutes": minutes})
    result["subjects"] = dict(subject_counts.most_common())
    return result


def schedule_render_load(rows: list[dict], monday: date) -> str:
    load = schedule_week_load(rows, monday)
    text = (f"📊 <b>Нагрузка {monday:%d.%m.%Y}–{monday + timedelta(days=6):%d.%m.%Y}</b>\n"
            f"Пар: <b>{load['classes']}</b> · предметов: {len(load['subjects'])}\n"
            f"Учебное время: <b>{load['minutes'] // 60} ч {load['minutes'] % 60} мин</b>\n\n")
    for day in load["days"]:
        d = date.fromisoformat(day["date"])
        text += f"{DAYS_RU[DAYS_ORDER[d.weekday()]]}: {day['classes']} пар, {day['minutes']} мин\n"
    if load["subjects"]:
        text += "\n<b>По предметам:</b>\n"
        shown = 0
        for subject, count in load["subjects"].items():
            display = subject if len(subject) <= 100 else subject[:99] + "…"
            line = f"• {esc(display)} — {count}\n"
            if len(text) + len(line) > 3200:
                break
            text += line
            shown += 1
        if shown < len(load["subjects"]):
            text += f"Ещё предметов: {len(load['subjects']) - shown}.\n"
    if load["unknown_duration"]:
        text += f"\n\n⚠️ У {load['unknown_duration']} пар длительность не определена."
    if load["overlaps"]:
        text += f"\n⚠️ Пересечений по времени: {len(load['overlaps'])}. Сумма учитывает обе пары."
    return text


def _schedule_csv_safe(value) -> str:
    text = "" if value is None else str(value)
    # Spreadsheet formula protection, including values prefixed with whitespace.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")):
        return "'" + text
    return text


def schedule_csv_bytes(rows: list[dict], timezone_name: str | None = None) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["Дата", "Время", "Предмет", "Аудитория", "Часовой пояс"])
    for row in sorted(rows, key=schedule_sort_key):
        writer.writerow([_schedule_csv_safe(row.get(field)) for field in ("date", "time", "subject", "room")]
                        + [_schedule_csv_safe(timezone_name or str(TIMEZONE))])
    return output.getvalue().encode("utf-8-sig")


def _schedule_ics_escape(value) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _schedule_ics_fold(line: str) -> str:
    """RFC 5545: fold UTF-8 content lines at 75 octets without splitting a character."""
    pieces, current, size = [], "", 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            pieces.append(current)
            current, size = " ", 1
        current += character
        size += width
    pieces.append(current)
    return "\r\n".join(pieces)


def schedule_ics_bytes(rows: list[dict], timezone_name: str | None = None,
                       generated_at: datetime | None = None) -> bytes:
    """Export RFC 5545 UTC instants interpreted in the configured IANA timezone.

    X-WR-TIMEZONE declares the display timezone. UTC DTSTART/DTEND also work in
    clients without an IANA timezone database and avoid DST drift on import.
    Lessons with invalid historical times retain their date as all-day entries.
    """
    tz = ZoneInfo(timezone_name) if timezone_name else TIMEZONE
    generated = generated_at or datetime.now(_schedule_utc.utc)
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=_schedule_utc.utc)
    stamp = generated.astimezone(_schedule_utc.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//School Schedule Bot//RU",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Расписание занятий",
             "X-WR-TIMEZONE:" + _schedule_ics_escape(str(tz))]
    for index, row in enumerate(sorted(rows, key=schedule_sort_key)):
        try:
            lesson_date = date.fromisoformat(row["date"])
        except (ValueError, TypeError, KeyError):
            continue
        start, end = parse_time_range(row.get("time") or "")
        identity = f"{row.get('id', index)}-{lesson_date.isoformat()}"
        lines += ["BEGIN:VEVENT", "UID:" + identity + "@school-schedule.local", "DTSTAMP:" + stamp]
        if start is None or (end is not None and end <= start):
            lines += ["DTSTART;VALUE=DATE:" + lesson_date.strftime("%Y%m%d"),
                      "DTEND;VALUE=DATE:" + (lesson_date + timedelta(days=1)).strftime("%Y%m%d")]
        else:
            begin = datetime.combine(lesson_date, start, tz).astimezone(_schedule_utc.utc)
            lines += ["DTSTART:" + begin.strftime("%Y%m%dT%H%M%SZ")]
            if end is not None:
                finish = datetime.combine(lesson_date, end, tz).astimezone(_schedule_utc.utc)
                lines += ["DTEND:" + finish.strftime("%Y%m%dT%H%M%SZ")]
        lines += ["SUMMARY:" + _schedule_ics_escape(row.get("subject")),
                  "LOCATION:" + _schedule_ics_escape(row.get("room")),
                  "DESCRIPTION:" + _schedule_ics_escape(f"Время: {row.get('time') or 'не указано'}; часовой пояс: {tz}"),
                  "END:VEVENT"]
    lines += ["END:VCALENDAR"]
    return ("\r\n".join(_schedule_ics_fold(line) for line in lines) + "\r\n").encode("utf-8")


def get_schedule_tools_menu(monday: date):
    builder = InlineKeyboardBuilder()
    if SCHEDULE_FEED is None:
        builder.row(Btn("📋 Копировать неделю", f"sch_copy_week_{monday.isoformat()}"))
    builder.row(Btn("🔎 Найти предмет / аудиторию", f"sch_search_{monday.isoformat()}"))
    builder.row(Btn("📊 Нагрузка недели", f"sch_load_{monday.isoformat()}"))
    builder.row(Btn("📄 CSV", f"sch_csv_{monday.isoformat()}"), Btn("📆 Календарь ICS", f"sch_ics_{monday.isoformat()}"))
    builder.row(Btn("🔙 К неделе", f"week_{monday.isoformat()}"))
    return builder.as_markup()


def _schedule_private_message_allowed(message: Message) -> bool:
    return bool(message.chat.type == "private" and message.from_user and
                not message.from_user.is_bot and is_admin(message.from_user.id))


def _schedule_admin_callback(call: CallbackQuery) -> bool:
    return bool(call.from_user and is_admin(call.from_user.id) and call.message and
                call.message.chat.type == "private")


def _schedule_callback_date(call: CallbackQuery, prefix: str) -> date | None:
    try:
        return date.fromisoformat((call.data or "")[len(prefix):])
    except (TypeError, ValueError):
        return None


def _schedule_back_markup(monday: date):
    builder = InlineKeyboardBuilder()
    builder.row(Btn("🔙 Инструменты", f"sch_tools_{monday.isoformat()}"))
    return builder.as_markup()


async def _schedule_send_search(message: Message, query: str, field: str = "all"):
    try:
        rows, total = schedule_search_rows(query, field)
    except ValueError as error:
        return await message.answer("❌ " + esc(error))
    first = get_now().date()
    last = first + timedelta(days=366)
    text = (f"🔎 <b>{esc(query)}</b> · {first:%d.%m.%Y}–{last:%d.%m.%Y}\n"
            f"Найдено пар: <b>{total}</b>\n\n")
    if not rows:
        return await message.answer(text + "Совпадений нет.")
    chunks = []
    for row in rows:
        d = date.fromisoformat(row["date"])
        line = f"📅 {d:%d.%m.%Y}\n{_schedule_preview_class(row)}\n\n"
        if len(text) + len(line) > 3700:
            chunks.append(text)
            text = ""
        text += line
    if total > len(rows):
        text += f"Показаны первые {len(rows)} результатов. Уточни запрос."
    chunks.append(text)
    for chunk in chunks:
        await safe_send(message.chat.id, chunk)


async def _schedule_send_export(message: Message, monday: date, kind: str):
    rows = schedule_rows_between(monday, monday + timedelta(days=6))
    if not rows:
        return await message.answer("📭 На эту неделю расписание не заполнено.")
    content = schedule_csv_bytes(rows) if kind == "csv" else schedule_ics_bytes(rows)
    document = BufferedInputFile(content, filename=f"schedule_{monday.isoformat()}.{kind}")
    caption = (f"📅 Неделя {monday:%d.%m.%Y}–{monday + timedelta(days=6):%d.%m.%Y}\n"
               f"Пар: {len(rows)} · часовой пояс: {esc(TIMEZONE)}")
    await message.answer_document(document, caption=caption)


@schedule_extra_router.callback_query(F.data.startswith("sch_tools_"), _schedule_admin_callback)
async def schedule_tools_callback(call: CallbackQuery, state: FSMContext):
    monday = _schedule_callback_date(call, "sch_tools_")
    if monday is None:
        return await call.answer("Неверная дата.", show_alert=True)
    await state.clear()
    await safe_edit(call, f"🧰 <b>Инструменты расписания</b>\nНеделя с {monday:%d.%m.%Y}",
                    get_schedule_tools_menu(monday))


@schedule_extra_router.callback_query(F.data.startswith("sch_edit_"), _schedule_admin_callback)
async def schedule_edit_start(call: CallbackQuery, state: FSMContext):
    try:
        row = schedule_get_class(int(call.data[len("sch_edit_"):]))
    except (TypeError, ValueError):
        row = None
    if row is None:
        return await call.answer("Пара не найдена.", show_alert=True)
    await state.clear()
    await state.update_data(schedule_edit_original=row)
    await state.set_state(ScheduleExtrasState.edit_text)
    await safe_edit(call, f"✏️ <b>Редактирование пары {esc(row['date'])}</b>\n{fmt_class(row)}\n\n"
                    "Пришли новую строку в прежнем формате:\n"
                    "<code>13:00-14:30 Предмет (Аудитория)</code>\n/cancel — отмена")


@schedule_extra_router.message(ScheduleExtrasState.edit_text, _schedule_private_message_allowed)
async def schedule_edit_text(message: Message, state: FSMContext):
    try:
        lesson = schedule_validate_class(message.text or "")
    except ValueError as error:
        return await message.answer("❌ " + esc(error))
    data = await state.get_data()
    original = data.get("schedule_edit_original")
    if not original:
        await state.clear()
        return await message.answer("Открой пару снова.")
    token = uuid.uuid4().hex[:16]
    await state.update_data(schedule_edit_lesson=lesson, schedule_edit_token=token)
    await state.set_state(ScheduleExtrasState.edit_confirm)
    builder = InlineKeyboardBuilder()
    builder.row(Btn("✅ Сохранить", f"sch_editok_{token}"), Btn("❌ Отмена", f"date_{original['date']}"))
    await message.answer(f"✏️ <b>Предпросмотр</b> · {esc(original['date'])}\n"
                         f"Было: {fmt_class(original)}\nБудет: {fmt_class(lesson)}",
                         reply_markup=builder.as_markup())


@schedule_extra_router.callback_query(F.data.startswith("sch_editok_"), _schedule_admin_callback)
async def schedule_edit_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if (await state.get_state() != ScheduleExtrasState.edit_confirm.state or
            data.get("schedule_edit_token") != call.data[len("sch_editok_"):]):
        return await call.answer("Этот предпросмотр устарел. Открой пару снова.", show_alert=True)
    original = data["schedule_edit_original"]
    try:
        schedule_update_class(original["id"], original, data["schedule_edit_lesson"])
    except ValueError as error:
        await state.clear()
        return await safe_edit(call, "❌ " + esc(error), get_week_menu(get_monday(date.fromisoformat(original["date"]))))
    await state.clear()
    log_action(call.from_user.id, f"Изменена пара #{original['id']} ({original['date']})")
    text, markup = render_date(date.fromisoformat(original["date"]))
    await safe_edit(call, "✅ Пара сохранена.\n\n" + text, markup)


@schedule_extra_router.callback_query(F.data.startswith("sch_copy_day_"), _schedule_admin_callback)
@schedule_extra_router.callback_query(F.data.startswith("sch_copy_week_"), _schedule_admin_callback)
async def schedule_copy_start(call: CallbackQuery, state: FSMContext):
    weekly = call.data.startswith("sch_copy_week_")
    source = _schedule_callback_date(call, "sch_copy_week_" if weekly else "sch_copy_day_")
    if source is None:
        return await call.answer("Неверная дата.", show_alert=True)
    source = get_monday(source) if weekly else source
    await state.clear()
    await state.update_data(schedule_copy_source=source.isoformat(), schedule_copy_days=7 if weekly else 1)
    await state.set_state(ScheduleExtrasState.copy_target)
    note = "Дата будет приведена к понедельнику выбранной недели.\n" if weekly else ""
    await safe_edit(call, f"📋 <b>Копирование {'недели' if weekly else 'дня'} с {source:%d.%m.%Y}</b>\n\n"
                    "Укажи дату назначения: <code>25.10.2026</code>.\n" + note +
                    "Перед копированием покажу предпросмотр. Пустые исходные дни пропускаются.\n/cancel — отмена")


def schedule_render_copy_plan(plan: dict) -> str:
    count = sum(map(len, plan["by_target"].values()))
    text = (f"📋 <b>Предпросмотр копирования</b>\n{esc(plan['source'])} → {esc(plan['target'])}\n"
            f"Пар: {count} · дней с парами: {len(plan['by_target'])}\n\n")
    shown = 0
    for destination, lessons in sorted(plan["by_target"].items()):
        text += f"<b>{esc(destination)}</b>" + (" ⚠️ занят" if destination in plan["occupied"] else "") + "\n"
        shown_in_day = 0
        for lesson in lessons:
            line = _schedule_preview_class(lesson) + "\n"
            if shown < 16 and len(text) + len(line) < 2700:
                text += line
                shown += 1
                shown_in_day += 1
        if shown_in_day < len(lessons):
            text += f"Всего в этот день: {len(lessons)} пар.\n"
        text += "\n"
    if count > shown:
        text += f"Показано подробно {shown} пар из {count}.\n"
    if plan["occupied"]:
        text += "⚠️ Занятые дни можно пропустить либо <b>полностью заменить</b> после явного подтверждения.\n"
    else:
        text += "Все целевые дни свободны.\n"
    text += "Пустые исходные дни сохранят расписание назначения."
    return text


@schedule_extra_router.message(ScheduleExtrasState.copy_target, _schedule_private_message_allowed)
async def schedule_copy_target(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("schedule_copy_source"):
        await state.clear()
        return await message.answer("Выбери исходный день или неделю снова.")
    try:
        target = schedule_parse_date(message.text or "")
        if data["schedule_copy_days"] == 7:
            target = get_monday(target)
        plan = schedule_build_copy_plan(date.fromisoformat(data["schedule_copy_source"]), target,
                                        data["schedule_copy_days"])
    except ValueError as error:
        return await message.answer("❌ " + esc(error))
    await state.update_data(schedule_copy_plan=plan)
    await state.set_state(ScheduleExtrasState.copy_confirm)
    builder = InlineKeyboardBuilder()
    if plan["occupied"]:
        if len(plan["occupied"]) < len(plan["by_target"]):
            builder.row(Btn("✅ Копировать только в свободные дни", f"sch_copyok_skip_{plan['token']}"))
        builder.row(Btn("⚠️ Подтверждаю замену занятых дней", f"sch_copyok_replace_{plan['token']}"))
    else:
        builder.row(Btn("✅ Скопировать", f"sch_copyok_safe_{plan['token']}"))
    builder.row(Btn("❌ Отмена", f"week_{get_monday(target).isoformat()}"))
    await safe_send(message.chat.id, schedule_render_copy_plan(plan), reply_markup=builder.as_markup())


@schedule_extra_router.callback_query(F.data.startswith("sch_copyok_"), _schedule_admin_callback)
async def schedule_copy_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    plan = data.get("schedule_copy_plan")
    parts = call.data[len("sch_copyok_"):].split("_", 1)
    if (await state.get_state() != ScheduleExtrasState.copy_confirm.state or not plan or
            len(parts) != 2 or parts[1] != plan["token"] or parts[0] not in ("safe", "skip", "replace")):
        return await call.answer("Этот предпросмотр устарел. Начни копирование снова.", show_alert=True)
    try:
        result = schedule_apply_copy_plan(plan, replace_occupied=parts[0] == "replace",
                                          skip_occupied=parts[0] == "skip")
    except ValueError as error:
        await state.clear()
        return await safe_edit(call, "❌ " + esc(error), get_week_menu(get_monday(date.fromisoformat(plan["target"]))))
    await state.clear()
    log_action(call.from_user.id, f"Скопировано {result['classes']} пар: {plan['source']} → {plan['target']}; {parts[0]}")
    await safe_edit(call, f"✅ Скопировано пар: {result['classes']}, дней: {result['days']}.\n"
                    f"Пропущено занятых дней: {result['skipped_days']}.",
                    get_week_menu(get_monday(date.fromisoformat(plan["target"]))))


@schedule_extra_router.callback_query(F.data.startswith("sch_search_"), _schedule_admin_callback)
async def schedule_search_start(call: CallbackQuery, state: FSMContext):
    monday = _schedule_callback_date(call, "sch_search_")
    if monday is None:
        return await call.answer("Неверная дата.", show_alert=True)
    await state.clear()
    await state.update_data(schedule_search_monday=monday.isoformat())
    await state.set_state(ScheduleExtrasState.search_text)
    await safe_edit(call, "🔎 Пришли предмет или аудиторию. Поиск среди будущих пар на год вперёд.\n"
                    "Пример: <code>хирургия</code> или <code>А-101</code>.\n/cancel — отмена")


@schedule_extra_router.message(ScheduleExtrasState.search_text, _schedule_private_message_allowed)
async def schedule_search_text(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query or len(query) > 100:
        return await message.answer("Укажи от 1 до 100 символов.")
    await state.clear()
    await _schedule_send_search(message, query)


@schedule_extra_router.callback_query(F.data.startswith("sch_load_"), _schedule_admin_callback)
async def schedule_load_callback(call: CallbackQuery, state: FSMContext):
    monday = _schedule_callback_date(call, "sch_load_")
    if monday is None:
        return await call.answer("Неверная дата.", show_alert=True)
    await state.clear()
    monday = get_monday(monday)
    rows = schedule_rows_between(monday, monday + timedelta(days=6))
    await safe_edit(call, schedule_render_load(rows, monday), _schedule_back_markup(monday))


@schedule_extra_router.callback_query(F.data.startswith("sch_csv_"), _schedule_admin_callback)
@schedule_extra_router.callback_query(F.data.startswith("sch_ics_"), _schedule_admin_callback)
async def schedule_export_callback(call: CallbackQuery, state: FSMContext):
    kind = "csv" if call.data.startswith("sch_csv_") else "ics"
    monday = _schedule_callback_date(call, f"sch_{kind}_")
    if monday is None:
        return await call.answer("Неверная дата.", show_alert=True)
    await state.clear()
    await call.answer()
    await _schedule_send_export(call.message, get_monday(monday), kind)


_SCHEDULE_QUERY_RE = (
    r"(?i)^(?:/find_schedule(?:@[\w]+)?|найти пары|поиск расписания|аудитория)(?:\s+.*)?$"
)
_SCHEDULE_LOAD_RE = r"(?i)^(?:/weekload(?:@[\w]+)?|нагрузка)(?:\s+.*)?$"
_SCHEDULE_EXPORT_RE = (
    r"(?i)^(?:/schedule_(?:csv|ics)(?:@[\w]+)?|расписание (?:csv|ics))(?:\s+.*)?$"
)


@schedule_extra_router.message(StateFilter(None), F.text.regexp(_SCHEDULE_QUERY_RE))
async def schedule_search_command(message: Message):
    if not feature_message_allowed(message, public=True):
        return
    text = message.text.strip()
    match = re.match(r"(?i)^(?:/find_schedule(?:@[\w]+)?|найти пары|поиск расписания|аудитория)\s*(.*)$", text)
    query = match.group(1).strip() if match else ""
    if not query:
        return await message.answer("🔎 <code>найти пары Хирургия</code> или <code>аудитория А-101</code>")
    field = "room" if text.casefold().startswith("аудитория") else "all"
    await _schedule_send_search(message, query, field)


@schedule_extra_router.message(StateFilter(None), F.text.regexp(_SCHEDULE_LOAD_RE))
async def schedule_load_command(message: Message):
    if not feature_message_allowed(message, public=True):
        return
    match = re.match(r"(?i)^(?:/weekload(?:@[\w]+)?|нагрузка)\s*(.*)$", message.text.strip())
    try:
        requested = schedule_parse_date(match.group(1)) if match and match.group(1).strip() else get_now().date()
    except ValueError as error:
        return await message.answer("❌ " + esc(error))
    monday = get_monday(requested)
    await message.answer(schedule_render_load(schedule_rows_between(monday, monday + timedelta(days=6)), monday))


@schedule_extra_router.message(StateFilter(None), F.text.regexp(_SCHEDULE_EXPORT_RE))
async def schedule_export_command(message: Message):
    if not feature_message_allowed(message, public=True):
        return
    match = re.match(r"(?i)^(?:/schedule_(csv|ics)(?:@[\w]+)?|расписание (csv|ics))\s*(.*)$", message.text.strip())
    if not match:
        return
    kind = (match.group(1) or match.group(2)).lower()
    try:
        requested = schedule_parse_date(match.group(3)) if match.group(3).strip() else get_now().date()
    except ValueError as error:
        return await message.answer("❌ " + esc(error))
    await _schedule_send_export(message, get_monday(requested), kind)


# ====================== ДОПОЛНИТЕЛЬНЫЕ ФУНКЦИИ УЧЁБЫ ======================
# Existing homework, links and events stay in their original tables and formats.
study_router = Router(name="study_extras")


class StudyStates(StatesGroup):
    homework_edit = State()
    event_add = State()
    event_edit = State()
    search = State()


def init_study_db():
    with db_session() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS homework_progress (
                user_id INTEGER NOT NULL,
                homework_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, homework_id)
            );
            CREATE INDEX IF NOT EXISTS idx_hw_progress_homework
                ON homework_progress (homework_id);
            CREATE INDEX IF NOT EXISTS idx_homework_deadline ON homework (deadline);
            CREATE INDEX IF NOT EXISTS idx_events_date ON events (date);
            CREATE TRIGGER IF NOT EXISTS cleanup_deleted_homework_progress
                AFTER DELETE ON homework
                BEGIN
                    DELETE FROM homework_progress WHERE homework_id = OLD.id;
                END;
        """)
        conn.execute("DELETE FROM homework_progress WHERE homework_id NOT IN (SELECT id FROM homework)")


def study_public_allowed(message: Message) -> bool:
    return feature_message_allowed(message, public=True)


def study_admin_allowed(message: Message) -> bool:
    return feature_message_allowed(message, public=False)


async def study_private_admin(call: CallbackQuery) -> bool:
    if (not isinstance(call.message, Message) or call.message.chat.type != "private"
            or not is_admin(call.from_user.id)):
        await call.answer("Управление доступно администраторам в личке бота.", show_alert=True)
        return False
    return True


def study_id(raw: str) -> int | None:
    raw = raw.strip().lstrip("#")
    if not raw.isascii() or not raw.isdigit() or len(raw) > 10:
        return None
    value = int(raw)
    return value if 0 < value <= 2147483647 else None


def study_date_label(raw: str | None) -> str:
    if not raw:
        return "без срока"
    try:
        return date.fromisoformat(raw).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return esc(raw)


def get_study_menu():
    b = InlineKeyboardBuilder()
    b.row(Btn("📋 План на сегодня", "study_plan:0"), Btn("📋 План на завтра", "study_plan:1"))
    b.row(Btn("✅ Мои задания", "study_personal"), Btn("⌛ Просроченные ДЗ", "study_overdue"))
    b.row(Btn("✏️ Редактировать ДЗ", "study_hw_list:0"), Btn("📆 События", "study_events:0"))
    b.row(Btn("🔎 Поиск", "study_search"), Btn("📝 Домашка", "menu_hw"))
    b.row(Btn("🔙 В главное меню", "main_menu"))
    return b.as_markup()


def study_menu_text() -> str:
    return ("🎓 <b>Учёба и личный прогресс</b>\n\n"
            "Отмечайте свою домашку выполненной — общие задания останутся для всей группы.\n"
            "<code>мое дз</code> · <code>просроченные дз</code> · <code>выполненное дз</code>\n"
            "<code>дз готово 5</code> / <code>дз не готово 5</code>\n"
            "<code>план завтра</code> · <code>найти математика</code>\n\n"
            "Администраторы могут редактировать задания и управлять событиями.")


def personal_homework(user_id: int, view: str = "pending") -> list[dict]:
    with db_session() as conn:
        rows = conn.execute("""
            SELECT h.*, p.completed_at FROM homework h
            LEFT JOIN homework_progress p ON p.homework_id = h.id AND p.user_id = ?
            ORDER BY h.deadline IS NULL, h.deadline, h.id
        """, (user_id,)).fetchall()
    today = get_now().date().isoformat()
    if view == "done":
        return [dict(r) for r in rows if r["completed_at"]]
    if view == "overdue":
        return [dict(r) for r in rows if not r["completed_at"] and r["deadline"] and r["deadline"] < today]
    return [dict(r) for r in rows if not r["completed_at"]]


def set_homework_completed(user_id: int, homework_id: int, completed: bool) -> bool:
    with db_session() as conn:
        if not conn.execute("SELECT 1 FROM homework WHERE id = ?", (homework_id,)).fetchone():
            return False
        if completed:
            conn.execute("""INSERT INTO homework_progress (user_id, homework_id, completed_at)
                VALUES (?, ?, ?) ON CONFLICT(user_id, homework_id)
                DO UPDATE SET completed_at = excluded.completed_at""",
                         (user_id, homework_id, get_now().isoformat()))
        else:
            conn.execute("DELETE FROM homework_progress WHERE user_id = ? AND homework_id = ?",
                         (user_id, homework_id))
    return True


def personal_homework_page(user_id: int, view: str = "pending", page: int = 0):
    rows = personal_homework(user_id, view)
    labels = {"pending": "Мои невыполненные задания", "done": "Мои выполненные задания",
              "overdue": "Мои просроченные задания"}
    page_count = max(1, (len(rows) + 5) // 6)
    page = max(0, min(page, page_count - 1))
    selected = rows[page * 6:(page + 1) * 6]
    text = f"{'✅' if view == 'done' else '📝'} <b>{labels.get(view, labels['pending'])}</b>\n"
    text += f"Всего: {len(rows)} · страница {page + 1}/{page_count}\n\n"
    b = InlineKeyboardBuilder()
    if not rows:
        text += "Заданий в этом списке нет. 🎉"
    for h in selected:
        mark = "✅" if h["completed_at"] else ("⌛" if h["deadline"] and h["deadline"] < get_now().date().isoformat() else "📝")
        text += (f"{mark} <code>#{h['id']}</code> <b>{esc(h['subject'])}</b>\n"
                 f"{esc(h['task'])}\n📅 {study_date_label(h['deadline'])}\n\n")
        action = "undo" if h["completed_at"] else "done"
        b.row(Btn(f"{'↩️ Вернуть' if action == 'undo' else '✅ Готово'} #{h['id']}",
                  f"study_complete:{user_id}:{h['id']}:{action}:{view}:{page}"))
    navigation = []
    if page > 0:
        navigation.append(Btn("◀️", f"study_mine:{user_id}:{view}:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(Btn("▶️", f"study_mine:{user_id}:{view}:{page + 1}"))
    if navigation:
        b.row(*navigation)
    b.row(Btn("📝 Осталось", f"study_mine:{user_id}:pending:0"),
          Btn("✅ Готово", f"study_mine:{user_id}:done:0"),
          Btn("⌛ Просрочено", f"study_mine:{user_id}:overdue:0"))
    text += "\nОтметки относятся только к вам. Команда: <code>дз готово ID</code>."
    return text, b.as_markup()


async def study_personal_callback_allowed(call: CallbackQuery, owner: int) -> bool:
    if owner != call.from_user.id:
        await call.answer("Это личный список другого участника. Напишите «мое дз» для своего.", show_alert=True)
        return False
    if not isinstance(call.message, Message):
        await call.answer("Откройте новый список командой «мое дз».", show_alert=True)
        return False
    # The callback's user, rather than the bot author of the message, is checked.
    source = call.message.model_copy(update={"from_user": call.from_user})
    if not feature_message_allowed(source, public=True):
        await call.answer("Команды учёбы сейчас недоступны в этом чате.", show_alert=True)
        return False
    return True


@study_router.callback_query(F.data == "menu_study")
async def study_menu_open(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await call.answer()
    await safe_edit(call, study_menu_text(), get_study_menu())


@study_router.message(StateFilter(None), F.text.regexp(r"(?i)^\s*(мое дз|моё дз|моя домашка|мои задания|выполненное дз|просроченные дз|дз просрочено)\s*$"), study_public_allowed)
async def study_mine_command(message: Message):
    low = (message.text or "").strip().lower()
    view = "done" if low == "выполненное дз" else ("overdue" if "просроч" in low else "pending")
    text, markup = personal_homework_page(message.from_user.id, view)
    await safe_send(message.chat.id, text, reply_markup=markup)


@study_router.message(StateFilter(None), F.text.regexp(r"(?i)^\s*дз\s+(?:не\s+готово|готово|сделано)\b"), study_public_allowed)
async def study_complete_command(message: Message):
    match = re.fullmatch(r"\s*дз\s+(не\s+готово|готово|сделано)\s+(#?\d+)\s*", message.text or "", re.I)
    homework_id = study_id(match.group(2)) if match else None
    if not homework_id:
        return await safe_send(message.chat.id, "Формат: <code>дз готово 5</code> или <code>дз не готово 5</code>.")
    completed = not match.group(1).lower().startswith("не")
    if not set_homework_completed(message.from_user.id, homework_id, completed):
        return await safe_send(message.chat.id, f"Задание <code>#{homework_id}</code> не найдено.")
    await safe_send(message.chat.id, f"{'✅ Выполнено' if completed else '↩️ Возвращено в работу'}: <code>#{homework_id}</code>.\n"
                    "Отметка сохранена для вас. Общая домашка остаётся в списке группы.")


@study_router.callback_query(F.data.regexp(r"^study_mine:\d+:(pending|done|overdue):\d+$"))
async def study_mine_page(call: CallbackQuery):
    _, owner, view, page = call.data.split(":")
    if not await study_personal_callback_allowed(call, int(owner)):
        return
    await call.answer()
    text, markup = personal_homework_page(int(owner), view, min(int(page), 100000))
    await safe_edit(call, text, markup)


@study_router.callback_query(F.data.regexp(r"^study_complete:\d+:\d+:(done|undo):(pending|done|overdue):\d+$"))
async def study_complete_callback(call: CallbackQuery):
    _, owner, homework_id, action, view, page = call.data.split(":")
    if not await study_personal_callback_allowed(call, int(owner)):
        return
    parsed_id = study_id(homework_id)
    if not parsed_id or not set_homework_completed(int(owner), parsed_id, action == "done"):
        await call.answer("Задание уже удалено из общего списка.", show_alert=True)
    else:
        await call.answer("✅ Отметка сохранена для вас." if action == "done" else "↩️ Задание снова в работе.")
    text, markup = personal_homework_page(int(owner), view, min(int(page), 100000))
    await safe_edit(call, text, markup)


@study_router.callback_query(F.data.in_({"study_personal", "study_overdue"}))
async def study_mine_from_menu(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await call.answer()
    view = "overdue" if call.data == "study_overdue" else "pending"
    text, markup = personal_homework_page(call.from_user.id, view)
    b = InlineKeyboardBuilder.from_markup(markup)
    b.row(Btn("🔙 К учёбе", "menu_study"))
    await safe_edit(call, text, b.as_markup())


def study_daily_plan(d: date) -> str:
    text = f"📋 <b>План на {study_date_label(d.isoformat())}</b>\n\n📚 <b>Расписание</b>\n"
    classes = get_classes_for_date(d)
    text += "\n".join(fmt_class(c) for c in classes) if classes else "Пар нет."
    homework = homework_for_date(d)
    text += "\n\n📝 <b>ДЗ со сроком на этот день</b>\n"
    text += "\n\n".join(f"<code>#{h['id']}</code> <b>{esc(h['subject'])}</b>: {esc(h['task'])}" for h in homework) if homework else "Заданий с этим сроком нет."
    events = events_for_date(d)
    text += "\n\n📆 <b>События</b>\n"
    text += "\n".join(f"<code>#{e['id']}</code> {esc(e['title'])}" for e in events) if events else "Событий нет."
    undated = [h for h in get_homework() if not h["deadline"]]
    if undated:
        text += f"\n\n🗂 ДЗ без срока: {len(undated)}. Посмотреть: <code>дз</code>."
    return text


@study_router.message(StateFilter(None), F.text.regexp(r"(?i)^\s*план(?:\s|$)"), study_public_allowed)
async def study_plan_command(message: Message):
    arg = (message.text or "").strip()[4:].strip()
    d = parse_date(arg or "сегодня")
    if not d:
        return await safe_send(message.chat.id, "Укажите дату: <code>план завтра</code> или <code>план 25.10</code>.")
    await safe_send(message.chat.id, study_daily_plan(d))


@study_router.callback_query(F.data.in_({"study_plan:0", "study_plan:1"}))
async def study_plan_callback(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await call.answer()
    d = get_now().date() + timedelta(days=int(call.data.rsplit(":", 1)[1]))
    b = InlineKeyboardBuilder()
    b.row(Btn("🔙 К учёбе", "menu_study"))
    await safe_edit(call, study_daily_plan(d), b.as_markup())


def study_search_text(query: str, scope: str = "all") -> str:
    query = query.strip()
    if not 2 <= len(query) <= 120:
        return "Введите от 2 до 120 символов: <code>найти математика</code>."
    needle = query.casefold()
    results = []
    count = 0
    categories = []
    if scope in ("all", "hw"):
        categories.append(("Домашка", get_homework(True), ("subject", "task"), "hw"))
    if scope in ("all", "links"):
        categories.append(("Ссылки", get_links(), ("title", "url"), "links"))
    if scope in ("all", "events"):
        categories.append(("События", get_events(False), ("title",), "events"))
    for label, rows, fields, kind in categories:
        matches = [r for r in rows if any(needle in str(r.get(f) or "").casefold() for f in fields)]
        count += len(matches)
        if not matches:
            continue
        lines = [f"<b>{label}: {len(matches)}</b>"]
        for r in matches[:15]:
            if kind == "hw":
                # Limit previews; the full task is available in homework lists.
                task = str(r["task"] or "")
                preview = task[:500] + ("…" if len(task) > 500 else "")
                lines.append(f"📝 <code>#{r['id']}</code> <b>{esc(r['subject'])}</b>: {esc(preview)}\n📅 {study_date_label(r['deadline'])}")
            elif kind == "links":
                url = str(r["url"] or "")
                title = esc(r["title"])
                lines.append(f"🔗 <code>#{r['id']}</code> " + (f"<a href='{esc(url)}'>{title}</a>" if url.startswith(("http://", "https://", "tg://")) else f"{title}: {esc(url)}"))
            else:
                lines.append(f"📆 <code>#{r['id']}</code> {esc(r['title'])} — {study_date_label(r['date'])}")
        if len(matches) > 15:
            lines.append(f"Показаны первые 15 из {len(matches)}. Уточните запрос.")
        results.append("\n\n".join(lines))
    header = f"🔎 <b>Поиск: {esc(query)}</b>\nНайдено: {count}. Поиск включает архивные записи.\n\n"
    return header + ("\n\n".join(results) if results else "Совпадений нет.")


@study_router.message(StateFilter(None), F.text.regexp(r"(?i)^\s*(найти|поиск)(?:\s|$)"), study_public_allowed)
async def study_search_command(message: Message):
    parts = (message.text or "").strip().split(maxsplit=1)
    query = parts[1] if len(parts) == 2 else ""
    scope = "all"
    scoped = re.match(r"^(дз|ссылки|ссылку|события|событие)\s+(.+)$", query, re.I | re.S)
    if scoped:
        scope = {"дз": "hw", "ссылки": "links", "ссылку": "links", "события": "events", "событие": "events"}[scoped.group(1).lower()]
        query = scoped.group(2)
    await safe_send(message.chat.id, study_search_text(query, scope), disable_web_page_preview=True)


@study_router.callback_query(F.data == "study_search")
async def study_search_start(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await state.set_state(StudyStates.search)
    await call.answer()
    await safe_edit(call, "🔎 Введите слово или фразу для поиска в ДЗ, ссылках и событиях.\n\n/cancel — отмена.")


@study_router.message(StudyStates.search, study_admin_allowed)
async def study_search_process(message: Message, state: FSMContext):
    if not message.text or not 2 <= len(message.text.strip()) <= 120:
        return await safe_send(message.chat.id, "Введите текст от 2 до 120 символов или /cancel.")
    await state.clear()
    await safe_send(message.chat.id, study_search_text(message.text), reply_markup=get_study_menu(), disable_web_page_preview=True)


def study_parse_homework(raw: str):
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
        return None, "Формат: <code>Предмет | Задание | 25.10</code>. Дата необязательна."
    if len(parts[0]) > 200 or len(parts[1]) > 3000:
        return None, "Предмет должен быть до 200 символов, задание — до 3000."
    deadline = None
    if len(parts) == 3 and parts[2]:
        d = parse_date(parts[2])
        if not d:
            return None, "Не удалось понять дату. Примеры: <code>25.10.2026</code>, <code>завтра</code>."
        deadline = d.isoformat()
    return (parts[0], parts[1], deadline), ""


def study_update_homework(homework_id: int, raw: str, user_id: int) -> tuple[bool, str]:
    values, error = study_parse_homework(raw)
    if values is None:
        return False, error
    with db_session() as conn:
        cur = conn.execute("UPDATE homework SET subject = ?, task = ?, deadline = ? WHERE id = ?", (*values, homework_id))
    if not cur.rowcount:
        return False, f"Задание <code>#{homework_id}</code> не найдено."
    log_action(user_id, f"ДЗ #{homework_id} отредактировано: {values[0]}")
    return True, f"✅ Задание <code>#{homework_id}</code> обновлено. ID и личные отметки сохранены."


def study_homework_edit_menu(page: int = 0):
    rows = get_homework(True)
    pages = max(1, (len(rows) + 7) // 8)
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    for h in rows[page * 8:(page + 1) * 8]:
        b.row(Btn(f"✏️ #{h['id']} {h['subject'][:32]}", f"study_hw_edit:{h['id']}"))
    nav = []
    if page:
        nav.append(Btn("◀️", f"study_hw_list:{page - 1}"))
    if page + 1 < pages:
        nav.append(Btn("▶️", f"study_hw_list:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(Btn("🔙 К учёбе", "menu_study"))
    text = f"✏️ <b>Редактирование домашки</b>\nВсего: {len(rows)} · страница {page + 1}/{pages}\n\n"
    text += "Выберите задание. Его номер и отметки участников сохранятся." if rows else "Заданий пока нет. Добавление остаётся в разделе «Домашка»."
    return text, b.as_markup()


@study_router.message(StateFilter(None), F.text.regexp(r"(?i)^\s*дз\s+(изменить|редактировать)(?:\s|$)"), study_admin_allowed)
async def study_homework_edit_command(message: Message):
    match = re.fullmatch(r"\s*дз\s+(?:изменить|редактировать)\s+(#?\d+)\s*(?:\|\s*|\s+)(.+)", message.text or "", re.I | re.S)
    homework_id = study_id(match.group(1)) if match else None
    if not homework_id:
        return await safe_send(message.chat.id, "Формат: <code>дз изменить 5 Предмет | Задание | 25.10</code>.")
    _, result = study_update_homework(homework_id, match.group(2), message.from_user.id)
    await safe_send(message.chat.id, result)


@study_router.callback_query(F.data.regexp(r"^study_hw_list:\d+$"))
async def study_homework_list(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await call.answer()
    text, markup = study_homework_edit_menu(int(call.data.split(":")[1]))
    await safe_edit(call, text, markup)


@study_router.callback_query(F.data.regexp(r"^study_hw_edit:\d+$"))
async def study_homework_edit_start(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    homework_id = study_id(call.data.split(":")[1])
    with db_session() as conn:
        h = conn.execute("SELECT * FROM homework WHERE id = ?", (homework_id,)).fetchone()
    if not h:
        return await call.answer("Задание уже удалено.", show_alert=True)
    await state.clear()
    await state.set_state(StudyStates.homework_edit)
    await state.update_data(study_homework_id=homework_id)
    await call.answer()
    await safe_edit(call, f"✏️ <b>Редактирование ДЗ #{homework_id}</b>\n\n{fmt_hw(dict(h))}\n\n"
                    "Введите обновлённые данные:\n<code>Предмет | Задание | 25.10</code>\n"
                    "Дата необязательна. Если её не указать, срок будет убран.\n\n/cancel — отмена.")


@study_router.message(StudyStates.homework_edit, study_admin_allowed)
async def study_homework_edit_process(message: Message, state: FSMContext):
    data = await state.get_data()
    if not message.text:
        return await safe_send(message.chat.id, "Отправьте данные текстом или /cancel.")
    ok, result = study_update_homework(data.get("study_homework_id", 0), message.text, message.from_user.id)
    if not ok:
        return await safe_send(message.chat.id, result + "\n/cancel — отмена.")
    await state.clear()
    await safe_send(message.chat.id, result, reply_markup=get_study_menu())


def study_event_values(raw: str):
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2 or not parts[0] or len(parts[0]) > 200:
        return None, "Формат: <code>Название | 25.10</code>. Название — от 1 до 200 символов."
    d = parse_date(parts[1])
    if not d:
        return None, "Не удалось понять дату. Примеры: <code>25.10.2026</code>, <code>завтра</code>."
    return (parts[0], d.isoformat()), ""


def study_event_page(page: int = 0):
    rows = get_events(False)
    pages = max(1, (len(rows) + 7) // 8)
    page = max(0, min(page, pages - 1))
    b = InlineKeyboardBuilder()
    text = f"📆 <b>Управление событиями</b>\nВсего: {len(rows)} · страница {page + 1}/{pages}\n\n"
    for e in rows[page * 8:(page + 1) * 8]:
        text += f"<code>#{e['id']}</code> <b>{esc(e['title'])}</b> — {study_date_label(e['date'])}\n"
        b.row(Btn(f"✏️ #{e['id']}", f"study_event_edit:{e['id']}"),
              Btn(f"🗑 #{e['id']}", f"study_event_delete:{e['id']}"))
    if not rows:
        text += "Событий пока нет."
    nav = []
    if page:
        nav.append(Btn("◀️", f"study_events:{page - 1}"))
    if page + 1 < pages:
        nav.append(Btn("▶️", f"study_events:{page + 1}"))
    if nav:
        b.row(*nav)
    b.row(Btn("➕ Добавить событие", "study_event_add"))
    b.row(Btn("📤 Опубликовать ближайшие", "study_events_preview"))
    b.row(Btn("🔙 К учёбе", "menu_study"))
    return text, b.as_markup()


@study_router.callback_query(F.data.regexp(r"^study_events:\d+$"))
async def study_events_menu(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await call.answer()
    text, markup = study_event_page(int(call.data.split(":")[1]))
    await safe_edit(call, text, markup)


@study_router.callback_query(F.data == "study_event_add")
async def study_event_add_start(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    await state.set_state(StudyStates.event_add)
    await call.answer()
    await safe_edit(call, "➕ <b>Новое событие</b>\n\n<code>Название | 25.10</code>\n"
                    "Можно указать год или написать «завтра».\n\n/cancel — отмена.")


@study_router.message(StudyStates.event_add, study_admin_allowed)
async def study_event_add_process(message: Message, state: FSMContext):
    values, error = study_event_values(message.text or "")
    if not values:
        return await safe_send(message.chat.id, error + "\n/cancel — отмена.")
    with db_session() as conn:
        event_id = conn.execute("INSERT INTO events (title, date) VALUES (?, ?)", values).lastrowid
    log_action(message.from_user.id, f"Событие #{event_id} добавлено: {values[0]}")
    await state.clear()
    text, markup = study_event_page()
    await safe_send(message.chat.id, f"✅ Событие <code>#{event_id}</code> добавлено.\n\n" + text, reply_markup=markup)


@study_router.callback_query(F.data.regexp(r"^study_event_edit:\d+$"))
async def study_event_edit_start(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    event_id = study_id(call.data.split(":")[1])
    with db_session() as conn:
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return await call.answer("Событие уже удалено.", show_alert=True)
    await state.clear()
    await state.set_state(StudyStates.event_edit)
    await state.update_data(study_event_id=event_id)
    await call.answer()
    await safe_edit(call, f"✏️ <b>Событие #{event_id}</b>\n{esc(event['title'])} — {study_date_label(event['date'])}\n\n"
                    "Введите новые данные: <code>Название | 25.10</code>\n\n/cancel — отмена.")


@study_router.message(StudyStates.event_edit, study_admin_allowed)
async def study_event_edit_process(message: Message, state: FSMContext):
    values, error = study_event_values(message.text or "")
    if not values:
        return await safe_send(message.chat.id, error + "\n/cancel — отмена.")
    data = await state.get_data()
    event_id = data.get("study_event_id", 0)
    with db_session() as conn:
        cur = conn.execute("UPDATE events SET title = ?, date = ? WHERE id = ?", (*values, event_id))
    await state.clear()
    if not cur.rowcount:
        return await safe_send(message.chat.id, "Событие уже удалено. Изменения не сохранены.", reply_markup=get_study_menu())
    log_action(message.from_user.id, f"Событие #{event_id} обновлено: {values[0]}")
    text, markup = study_event_page()
    await safe_send(message.chat.id, f"✅ Событие <code>#{event_id}</code> обновлено.\n\n" + text, reply_markup=markup)


@study_router.callback_query(F.data.regexp(r"^study_event_delete:\d+$"))
async def study_event_delete_preview(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    event_id = study_id(call.data.split(":")[1])
    with db_session() as conn:
        event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return await call.answer("Событие уже удалено.", show_alert=True)
    await call.answer()
    b = InlineKeyboardBuilder()
    b.row(Btn("🗑 Удалить", f"study_event_confirm_delete:{event_id}"), Btn("Отмена", "study_events:0"))
    await safe_edit(call, f"Удалить событие <code>#{event_id}</code>?\n\n"
                    f"<b>{esc(event['title'])}</b> — {study_date_label(event['date'])}", b.as_markup())


@study_router.callback_query(F.data.regexp(r"^study_event_confirm_delete:\d+$"))
async def study_event_delete_confirm(call: CallbackQuery):
    if not await study_private_admin(call):
        return
    event_id = study_id(call.data.split(":")[1])
    with db_session() as conn:
        cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    if cur.rowcount:
        log_action(call.from_user.id, f"Событие #{event_id} удалено")
    await call.answer("Событие удалено." if cur.rowcount else "Событие уже удалено.")
    text, markup = study_event_page()
    await safe_edit(call, text, markup)


@study_router.callback_query(F.data == "study_events_preview")
async def study_events_post_preview(call: CallbackQuery, state: FSMContext):
    if not await study_private_admin(call):
        return
    await state.clear()
    if not get_events():
        return await call.answer("Ближайших событий нет.", show_alert=True)
    await call.answer()
    b = InlineKeyboardBuilder()
    b.row(Btn("📤 Отправить в группу", "study_events_publish"), Btn("Отмена", "study_events:0"))
    await safe_edit(call, "📤 <b>Публикация в группу</b>\n\n" + events_text(), b.as_markup())


@study_router.callback_query(F.data == "study_events_publish")
async def study_events_publish(call: CallbackQuery):
    if not await study_private_admin(call):
        return
    if not get_events():
        return await call.answer("Ближайших событий уже нет.", show_alert=True)
    await call.answer()
    sent = await safe_send(GROUP_CHAT_ID, events_text())
    if sent:
        log_action(call.from_user.id, "Ближайшие события опубликованы в группе")
    text, markup = study_event_page()
    await safe_edit(call, ("✅ События отправлены в группу.\n\n" if sent else "❌ Не удалось отправить события. Проверьте доступ бота к группе.\n\n") + text, markup)


# ============================ ДОПОЛНИТЕЛЬНЫЕ ИНСТРУМЕНТЫ ============================
# This fragment is included in bot.py and uses its existing database and helpers.
import json
import string
import tempfile

from aiogram.filters import StateFilter
from aiogram.types import BufferedInputFile, InputPollOption


TOOLS_DEFAULT_WELCOME = (
    "👋 Добро пожаловать, {name}!\n"
    "Напиши {help}, чтобы узнать, что я умею."
)
TOOLS_EXPORT_LIMIT = 48 * 1024 * 1024
TOOLS_REMINDER_PAGE_SIZE = 6

tools_router = Router()
tools_router.callback_query.filter(
    lambda call: bool(
        call.from_user and call.message
        and call.message.chat.type == "private"
        and is_admin(call.from_user.id)
    )
)


class ToolWelcome(StatesGroup):
    waiting_text = State()


class ToolReminderShift(StatesGroup):
    waiting_datetime = State()


def init_tools_db():
    """Add defaults without overwriting existing preferences or data."""
    with db_session() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            [("welcome_enabled", "1"), ("welcome_template", TOOLS_DEFAULT_WELCOME)],
        )


def _tools_private_admin(message: Message) -> bool:
    return message.chat.type == "private" and feature_message_allowed(message)


def _tools_can_use_callback(call: CallbackQuery) -> bool:
    # Also check inside handlers: callbacks can become stale after admin removal.
    return bool(
        call.from_user and call.message and call.message.chat.type == "private"
        and is_admin(call.from_user.id)
    )


def tools_menu(user_id: int):
    b = InlineKeyboardBuilder()
    b.row(Btn("🩺 Диагностика", "tools_diagnostics"))
    b.row(Btn("📤 Экспорт учебных данных", "tools_export_json"))
    b.row(Btn("⏰ Управление напоминаниями", "tools_reminders_0"))
    b.row(Btn("👋 Приветствие участников", "tools_welcome"))
    if user_id == MAIN_ADMIN_ID:
        b.row(Btn("💾 Скачать полную базу", "tools_snapshot"))
    b.row(Btn("🔙 Главное меню", "main_menu"))
    return b.as_markup()


def tools_help_text() -> str:
    return (
        "🛠 <b>Инструменты</b>\n\n"
        "Диагностика, выгрузка учебных данных, управление напоминаниями и приветствием.\n\n"
        "<b>Новые команды администратора:</b>\n"
        "• <code>напомни 25.10.2026 18:30 Подготовить доклад</code>\n"
        "• <code>напоминания</code> — список с номерами\n"
        "• <code>напоминание удалить 3</code>\n"
        "• <code>напоминание перенести 3 26.10.2026 19:00</code>\n"
        "• <code>опрос Вопрос | Вариант А | Вариант Б</code>\n\n"
        "Напоминания создаются в чате, где отправлена команда. "
        "В личном меню видны напоминания группы и твоей лички. "
        "Все даты и время — в часовом поясе бота."
    )


def _tools_welcome_parts(template: str):
    if not template or len(template) > 1500:
        raise ValueError("Приветствие должно содержать от 1 до 1500 символов.")
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError("Проверь парные фигурные скобки в шаблоне.") from exc
    count = 0
    for _, field, spec, conversion in parts:
        if field is not None:
            count += 1
            if field not in {"name", "chat", "id", "help"} or spec or conversion:
                raise ValueError("Разрешены только {name}, {chat}, {id} и {help}.")
    if count > 8:
        raise ValueError("В одном приветствии можно использовать до 8 подстановок.")
    return parts


def render_welcome_message(user: User, chat_title: str = "") -> str:
    """Treat the template as plain text and escape every user-provided value."""
    template = get_setting("welcome_template", TOOLS_DEFAULT_WELCOME)
    try:
        parts = _tools_welcome_parts(template)
    except ValueError:
        parts = _tools_welcome_parts(TOOLS_DEFAULT_WELCOME)
    values = {
        "name": mention(user.id, user.full_name or user.username or "Участник"),
        "chat": esc(chat_title or "наша группа"),
        "id": f"<code>{user.id}</code>",
        "help": "<code>помощь</code>",
    }
    return "".join(esc(literal) + (values[field] if field is not None else "")
                   for literal, field, _, _ in parts)


def _tools_welcome_menu():
    enabled = get_setting("welcome_enabled", "1") == "1"
    b = InlineKeyboardBuilder()
    b.row(Btn("🔔 Приветствие: " + ("включено" if enabled else "выключено"), "tools_welcome_toggle"))
    b.row(Btn("✏️ Изменить текст", "tools_welcome_edit"), Btn("↩️ Стандартный текст", "tools_welcome_reset"))
    b.row(Btn("🔙 Инструменты", "menu_tools"))
    return b.as_markup()


def _tools_welcome_text() -> str:
    return (
        "👋 <b>Приветствие новых участников</b>\n\n"
        "Текущий шаблон:\n<pre>" + esc(get_setting("welcome_template", TOOLS_DEFAULT_WELCOME)) + "</pre>\n\n"
        "Подстановки: <code>{name}</code> — имя со ссылкой, "
        "<code>{chat}</code> — название группы, <code>{id}</code> — ID участника, "
        "<code>{help}</code> — команда помощи. HTML в шаблоне отображается как обычный текст."
    )


def build_study_export() -> bytes:
    """Export a whitelist of learning fields, never users, tokens or settings."""
    queries = {
        "schedule": "SELECT id, date, time, subject, room FROM schedule ORDER BY date, time, id",
        "homework": "SELECT id, subject, task, deadline FROM homework ORDER BY id",
        "links": "SELECT id, title, url FROM links ORDER BY id",
        "events": "SELECT id, title, date FROM events ORDER BY date, id",
    }
    with db_session() as conn:
        conn.execute("BEGIN")
        payload = {name: [dict(row) for row in conn.execute(query).fetchall()]
                   for name, query in queries.items()}
    payload = {"format": "school_bot_study_export", "version": 1,
               "exported_at": get_now().isoformat(), "timezone": str(TIMEZONE), **payload}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def build_database_snapshot() -> bytes:
    """Use SQLite's backup API to capture committed WAL data consistently."""
    descriptor, snapshot_path = tempfile.mkstemp(prefix="school_bot_snapshot_", suffix=".db")
    os.close(descriptor)
    source = destination = None
    try:
        source = sqlite3.connect(DB_PATH, timeout=15)
        destination = sqlite3.connect(snapshot_path, timeout=15)
        source.backup(destination)
        destination.close()
        destination = None
        if os.path.getsize(snapshot_path) > TOOLS_EXPORT_LIMIT:
            raise ValueError("База превышает лимит отправки 48 МБ.")
        with open(snapshot_path, "rb") as snapshot:
            return snapshot.read()
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        try:
            os.unlink(snapshot_path)
        except FileNotFoundError:
            pass


async def _tools_deliver_export(chat_id: int, actor_id: int, full_database: bool = False):
    # A private chat ID equals its owner's ID. Recheck access before exporting.
    if chat_id != actor_id or not is_admin(actor_id):
        return
    if full_database and actor_id != MAIN_ADMIN_ID:
        return await safe_send(chat_id, "🔒 Полную базу может скачать только главный администратор.")
    try:
        builder = build_database_snapshot if full_database else build_study_export
        data = await asyncio.to_thread(builder)
        if len(data) > TOOLS_EXPORT_LIMIT:
            return await safe_send(chat_id, "⚠️ Файл превышает лимит отправки 48 МБ.")
        suffix = "db" if full_database else "json"
        filename = f"school_bot_{get_now().strftime('%Y%m%d_%H%M%S')}.{suffix}"
        caption = ("💾 Полная резервная копия SQLite. Содержит личные данные участников и администраторов."
                   if full_database else "📚 Расписание, домашние задания, ссылки и события. Данные аккаунтов не включены.")
        await bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=caption)
        log_action(actor_id, "Выгружена полная база" if full_database else "Экспорт учебных данных JSON")
    except ValueError:
        await safe_send(chat_id, "⚠️ Не удалось подготовить файл. Лимит выгрузки — 48 МБ.")
    except Exception as exc:
        logger.warning("Выгрузка данных не удалась (%s)", type(exc).__name__)
        await safe_send(chat_id, "❌ Не удалось отправить файл. Попробуй ещё раз позже.")


async def _tools_send_export(message: Message, full_database: bool = False):
    if not _tools_private_admin(message):
        return
    await _tools_deliver_export(message.chat.id, message.from_user.id, full_database)


async def tools_diagnostics_text() -> str:
    lines = ["🩺 <b>Диагностика</b>"]
    try:
        with db_session() as conn:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                      for name in ("schedule", "homework", "links", "events", "reminders")}
        lines.append("\n💾 SQLite: " + ("✅ целостность подтверждена" if check == "ok" else "⚠️ проверка выявила ошибки"))
        lines.append(f"Журнал: <code>{esc(journal)}</code>; версия SQLite: <code>{sqlite3.sqlite_version}</code>")
        lines.append("Пар: {schedule}; ДЗ: {homework}; ссылок: {links}; событий: {events}; напоминаний: {reminders}".format(**counts))
    except Exception as exc:
        logger.warning("Проверка SQLite не удалась (%s)", type(exc).__name__)
        lines.append("\n💾 SQLite: ❌ проверка недоступна")
    try:
        running = bool(scheduler and getattr(scheduler, "running", False))
        jobs = scheduler.get_jobs() if scheduler else []
        lines.append(f"\n⏱ Планировщик: {'✅ запущен' if running else '⚠️ не запущен'}; задач: {len(jobs)}")
    except Exception as exc:
        logger.warning("Проверка планировщика не удалась (%s)", type(exc).__name__)
        lines.append("\n⏱ Планировщик: ❌ проверка недоступна")
    lines.append(f"🌍 Часовой пояс: <code>{esc(str(TIMEZONE))}</code>")
    lines.append(f"🕒 Время бота: {get_now().strftime('%d.%m.%Y %H:%M:%S')}")
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(GROUP_CHAT_ID, me.id)
        status = getattr(member.status, "value", member.status)
        labels = {"administrator": "администратор", "creator": "владелец", "member": "участник",
                  "restricted": "ограничен", "left": "вышел", "kicked": "заблокирован"}
        lines.append("\n🤖 Бот в группе: " + esc(labels.get(str(status), str(status))))
        if status in {"administrator", "creator"}:
            permissions = [("удаление сообщений", "can_delete_messages"),
                           ("ограничение участников", "can_restrict_members"),
                           ("закрепление сообщений", "can_pin_messages")]
            for label, attribute in permissions:
                allowed = status == "creator" or bool(getattr(member, attribute, False))
                lines.append(f"{'✅' if allowed else '⚠️'} {label}")
        elif status == "member":
            lines.append("⚠️ Для модерации и закрепления нужны права администратора.")
        elif status == "restricted":
            lines.append("Отправка сообщений: " + ("✅" if getattr(member, "can_send_messages", False) else "❌"))
        else:
            lines.append("⚠️ Добавь бота в группу и предоставь необходимые права.")
    except Exception as exc:
        logger.warning("Проверка прав бота не удалась (%s)", type(exc).__name__)
        lines.append("\n🤖 Права в группе: ❌ не удалось проверить. Проверь доступ бота к группе.")
    return "\n".join(lines)


def _tools_reminder_chats(message: Message) -> tuple[int, ...]:
    if message.chat.type == "private":
        return tuple(dict.fromkeys((message.chat.id, GROUP_CHAT_ID)))
    return (message.chat.id,)


def _tools_get_reminder(rid: int, chat_ids: tuple[int, ...]):
    if not 0 < rid <= 9223372036854775807:
        return None
    marks = ",".join("?" for _ in chat_ids)
    with db_session() as conn:
        return conn.execute(
            f"SELECT * FROM reminders WHERE id = ? AND chat_id IN ({marks})", (rid, *chat_ids),
        ).fetchone()


def _tools_reminder_time(raw: str) -> str:
    try:
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=TIMEZONE)
        return value.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return "дата требует исправления"


def tools_reminders_view(message: Message, page: int = 0):
    chat_ids = _tools_reminder_chats(message)
    marks = ",".join("?" for _ in chat_ids)
    with db_session() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM reminders WHERE chat_id IN ({marks})", chat_ids).fetchone()[0]
        pages = max(1, (total + TOOLS_REMINDER_PAGE_SIZE - 1) // TOOLS_REMINDER_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        rows = conn.execute(
            f"SELECT * FROM reminders WHERE chat_id IN ({marks}) ORDER BY run_at, id LIMIT ? OFFSET ?",
            (*chat_ids, TOOLS_REMINDER_PAGE_SIZE, page * TOOLS_REMINDER_PAGE_SIZE),
        ).fetchall()
    lines = [f"⏰ <b>Напоминания ({total})</b>"]
    b = InlineKeyboardBuilder()
    for row in rows:
        target = "группа" if row["chat_id"] == GROUP_CHAT_ID else "личные"
        body = str(row["text"] or "")
        preview = body[:240] + ("…" if len(body) > 240 else "")
        lines.append(f"\n<b>#{row['id']}</b> · {esc(_tools_reminder_time(row['run_at']))} · {target}\n{esc(preview)}")
        b.row(Btn(f"🗑 #{row['id']}", f"tools_reminder_delete_{row['id']}"),
              Btn(f"🕒 Перенести #{row['id']}", f"tools_reminder_shift_{row['id']}"))
    if not rows:
        lines.append("\nАктивных напоминаний нет.")
    if pages > 1:
        buttons = []
        if page > 0:
            buttons.append(Btn("◀️", f"tools_reminders_{page - 1}"))
        buttons.append(Btn(f"{page + 1}/{pages}", f"tools_reminders_{page}"))
        if page + 1 < pages:
            buttons.append(Btn("▶️", f"tools_reminders_{page + 1}"))
        b.row(*buttons)
    lines.append("\nСоздать: <code>напомни 25.10.2026 18:30 Текст</code>")
    b.row(Btn("🔙 Инструменты", "menu_tools"))
    return "\n".join(lines), b.as_markup()


def parse_tools_datetime(raw: str) -> datetime:
    try:
        value = datetime.strptime(raw.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE)
    except ValueError as exc:
        raise ValueError("Нужны дата и время в формате ДД.ММ.ГГГГ ЧЧ:ММ.") from exc
    if value <= get_now():
        raise ValueError("Дата и время должны быть в будущем.")
    return value


def parse_custom_poll(text: str) -> tuple[str, list[str]]:
    parts = [part.strip() for part in re.sub(r"^опрос\s+", "", text.strip(), count=1, flags=re.IGNORECASE).split("|")]
    question, options = parts[0], parts[1:]
    if not question or len(question) > 300:
        raise ValueError("Вопрос должен содержать от 1 до 300 символов.")
    if not 2 <= len(options) <= 10:
        raise ValueError("Добавь от 2 до 10 вариантов ответа через символ |.")
    if any(not option or len(option) > 100 for option in options):
        raise ValueError("Каждый вариант должен содержать от 1 до 100 символов.")
    if len({option.casefold() for option in options}) != len(options):
        raise ValueError("Варианты ответа должны различаться.")
    return question, options


@tools_router.callback_query(F.data == "menu_tools")
async def menu_tools(call: CallbackQuery, state: FSMContext):
    if not _tools_can_use_callback(call):
        return
    await state.clear()
    await call.answer()
    await safe_edit(call, tools_help_text(), tools_menu(call.from_user.id))


@tools_router.callback_query(F.data == "tools_diagnostics")
async def tools_diagnostics_callback(call: CallbackQuery):
    if not _tools_can_use_callback(call):
        return
    await call.answer("Проверяю…")
    await safe_edit(call, await tools_diagnostics_text(), tools_menu(call.from_user.id))


@tools_router.callback_query(F.data.in_({"tools_export_json", "tools_snapshot"}))
async def tools_export_callback(call: CallbackQuery):
    if not _tools_can_use_callback(call):
        return
    if call.data == "tools_snapshot" and call.from_user.id != MAIN_ADMIN_ID:
        return await call.answer("Только для главного администратора", show_alert=True)
    await call.answer("Готовлю файл…")
    # Use callback identity: the menu message itself is authored by the bot.
    await _tools_deliver_export(call.message.chat.id, call.from_user.id, call.data == "tools_snapshot")


@tools_router.callback_query(F.data.startswith("tools_reminders_"))
async def tools_reminders_callback(call: CallbackQuery, state: FSMContext):
    if not _tools_can_use_callback(call):
        return
    arg = call.data.removeprefix("tools_reminders_")
    if not arg.isdigit():
        return await call.answer("Некорректная страница", show_alert=True)
    await state.clear()
    await call.answer()
    text, markup = tools_reminders_view(call.message, int(arg))
    await safe_edit(call, text, markup)


@tools_router.callback_query(F.data.startswith("tools_reminder_delete_"))
async def tools_reminder_delete_callback(call: CallbackQuery):
    if not _tools_can_use_callback(call):
        return
    arg = call.data.removeprefix("tools_reminder_delete_")
    if not arg.isdigit():
        return await call.answer("Некорректный номер", show_alert=True)
    row = _tools_get_reminder(int(arg), _tools_reminder_chats(call.message))
    removed = bool(row and delete_reminder(int(arg), chat_id=row["chat_id"]))
    if removed:
        log_action(call.from_user.id, f"Удалено напоминание #{arg}")
    await call.answer("Удалено" if removed else "Напоминание уже отправлено или удалено")
    text, markup = tools_reminders_view(call.message)
    await safe_edit(call, text, markup)


@tools_router.callback_query(F.data.startswith("tools_reminder_shift_"))
async def tools_reminder_shift_callback(call: CallbackQuery, state: FSMContext):
    if not _tools_can_use_callback(call):
        return
    arg = call.data.removeprefix("tools_reminder_shift_")
    row = _tools_get_reminder(int(arg), _tools_reminder_chats(call.message)) if arg.isdigit() else None
    if not row:
        return await call.answer("Напоминание уже отправлено или удалено", show_alert=True)
    await state.set_state(ToolReminderShift.waiting_datetime)
    await state.update_data(tools_reminder_id=row["id"], tools_reminder_chat=row["chat_id"])
    await call.answer()
    await safe_edit(call, f"🕒 Новая дата и время для напоминания <b>#{row['id']}</b>:\n"
                    "<code>25.10.2026 18:30</code>\n\nОтмена: /cancel")


@tools_router.callback_query(F.data == "tools_welcome")
async def tools_welcome_callback(call: CallbackQuery, state: FSMContext):
    if not _tools_can_use_callback(call):
        return
    await state.clear()
    await call.answer()
    await safe_edit(call, _tools_welcome_text(), _tools_welcome_menu())


@tools_router.callback_query(F.data == "tools_welcome_toggle")
async def tools_welcome_toggle(call: CallbackQuery):
    if not _tools_can_use_callback(call):
        return
    value = "0" if get_setting("welcome_enabled", "1") == "1" else "1"
    set_setting("welcome_enabled", value)
    log_action(call.from_user.id, "Приветствие " + ("включено" if value == "1" else "выключено"))
    await call.answer("Сохранено")
    await safe_edit(call, _tools_welcome_text(), _tools_welcome_menu())


@tools_router.callback_query(F.data == "tools_welcome_reset")
async def tools_welcome_reset(call: CallbackQuery):
    if not _tools_can_use_callback(call):
        return
    set_setting("welcome_template", TOOLS_DEFAULT_WELCOME)
    log_action(call.from_user.id, "Восстановлен стандартный текст приветствия")
    await call.answer("Стандартный текст восстановлен")
    await safe_edit(call, _tools_welcome_text(), _tools_welcome_menu())


@tools_router.callback_query(F.data == "tools_welcome_edit")
async def tools_welcome_edit(call: CallbackQuery, state: FSMContext):
    if not _tools_can_use_callback(call):
        return
    await state.set_state(ToolWelcome.waiting_text)
    await call.answer()
    await safe_edit(call, "✏️ Отправь новое приветствие (до 1500 символов).\n"
                    "Подстановки: <code>{name}</code>, <code>{chat}</code>, <code>{id}</code>, <code>{help}</code>.\n"
                    "Пример: <code>Привет, {name}! Добро пожаловать в {chat}.</code>\n\nОтмена: /cancel")


# FSM handlers must precede the stateless text-command handlers.
@tools_router.message(ToolWelcome.waiting_text, F.chat.type == "private", F.text)
async def tools_welcome_save(message: Message, state: FSMContext):
    if not _tools_private_admin(message):
        return await state.clear()
    template = message.text.strip()
    try:
        _tools_welcome_parts(template)
    except ValueError as exc:
        return await safe_send(message.chat.id, "⚠️ " + esc(str(exc)) + "\nОтмена: /cancel")
    set_setting("welcome_template", template)
    log_action(message.from_user.id, "Обновлено приветствие участников")
    await state.clear()
    await safe_send(message.chat.id, "✅ Приветствие сохранено.\n\nПример:\n" +
                    render_welcome_message(message.from_user, "Наша группа"), reply_markup=_tools_welcome_menu())


@tools_router.message(ToolReminderShift.waiting_datetime, F.chat.type == "private", F.text)
async def tools_reminder_shift_save(message: Message, state: FSMContext):
    if not _tools_private_admin(message):
        return await state.clear()
    data = await state.get_data()
    rid = data.get("tools_reminder_id")
    chat_id = data.get("tools_reminder_chat")
    if not isinstance(rid, int) or chat_id not in _tools_reminder_chats(message):
        await state.clear()
        return await safe_send(message.chat.id, "⚠️ Открой список напоминаний заново.")
    try:
        run_at = parse_tools_datetime(message.text)
    except ValueError as exc:
        return await safe_send(message.chat.id, "⚠️ " + esc(str(exc)) + "\nОтмена: /cancel")
    moved = reschedule_reminder(rid, chat_id, run_at)
    await state.clear()
    if moved:
        log_action(message.from_user.id, f"Перенесено напоминание #{rid}")
    await safe_send(message.chat.id, (f"✅ Напоминание #{rid} перенесено на {run_at.strftime('%d.%m.%Y %H:%M')}."
                                    if moved else "⚠️ Напоминание уже отправлено или удалено."))
    text, markup = tools_reminders_view(message)
    await safe_send(message.chat.id, text, reply_markup=markup)


@tools_router.message(StateFilter(None), F.chat.type == "private", F.text.strip().casefold().in_({"инструменты", "диагностика", "экспорт данных", "скачать базу"}))
async def tools_private_commands(message: Message):
    if not _tools_private_admin(message):
        return
    low = message.text.strip().casefold()
    if low == "инструменты":
        await safe_send(message.chat.id, tools_help_text(), reply_markup=tools_menu(message.from_user.id))
    elif low == "диагностика":
        await safe_send(message.chat.id, await tools_diagnostics_text(), reply_markup=tools_menu(message.from_user.id))
    else:
        await _tools_send_export(message, full_database=low == "скачать базу")


@tools_router.message(StateFilter(None), Command("tools"), F.chat.type == "private")
async def tools_command(message: Message):
    if _tools_private_admin(message):
        await safe_send(message.chat.id, tools_help_text(), reply_markup=tools_menu(message.from_user.id))


@tools_router.message(StateFilter(None), F.text.regexp(r"(?is)^\s*опрос\s+.+\|"))
async def tools_custom_poll(message: Message):
    if not feature_message_allowed(message, public=False):
        return
    try:
        question, choices = parse_custom_poll(message.text)
    except ValueError as exc:
        return await safe_send(message.chat.id, "⚠️ " + esc(str(exc)) +
                               "\nФормат: <code>опрос Вопрос | Вариант А | Вариант Б</code>")
    try:
        await bot.send_poll(message.chat.id, question=question,
                            options=[InputPollOption(text=choice) for choice in choices], is_anonymous=False)
        log_action(message.from_user.id, "Создан опрос с собственными вариантами")
    except Exception as exc:
        logger.warning("Создание опроса не удалось (%s)", type(exc).__name__)
        await safe_send(message.chat.id, "❌ Не удалось создать опрос. Проверь права бота и попробуй позже.")


@tools_router.message(StateFilter(None), F.text.regexp(r"(?is)^\s*напомни\s+\d{1,2}[.]\d{1,2}[.]\d{4}(?:\s|$)"))
async def tools_dated_reminder(message: Message):
    if not feature_message_allowed(message, public=False):
        return
    match = re.fullmatch(r"\s*напомни\s+(\d{1,2}[.]\d{1,2}[.]\d{4}\s+\d{1,2}:\d{2})\s+(.+?)\s*", message.text,
                         flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return await safe_send(message.chat.id, "⚠️ Формат: <code>напомни 25.10.2026 18:30 Текст</code>")
    body = match.group(2).strip()
    if not body or len(body) > 3000:
        return await safe_send(message.chat.id, "⚠️ Текст напоминания должен содержать от 1 до 3000 символов.")
    try:
        run_at = parse_tools_datetime(match.group(1))
        rid = add_reminder(message.chat.id, body, run_at)
    except ValueError as exc:
        return await safe_send(message.chat.id, "⚠️ " + esc(str(exc)))
    log_action(message.from_user.id, f"Создано напоминание #{rid}")
    await safe_send(message.chat.id, f"⏰ Напоминание <b>#{rid}</b> на {run_at.strftime('%d.%m.%Y %H:%M')}:\n{esc(body)}")


@tools_router.message(StateFilter(None), F.text.strip().casefold().in_({"напоминания", "список напоминаний"}))
async def tools_reminders_command(message: Message):
    if not feature_message_allowed(message, public=False):
        return
    if message.chat.type == "private":
        text, markup = tools_reminders_view(message)
        return await safe_send(message.chat.id, text, reply_markup=markup)
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM reminders WHERE chat_id = ? ORDER BY run_at, id", (message.chat.id,)).fetchall()
    if not rows:
        return await safe_send(message.chat.id, "⏰ Активных напоминаний нет.")
    lines = [f"• <b>#{row['id']}</b> {esc(_tools_reminder_time(row['run_at']))} — {esc(row['text'])}" for row in rows]
    await safe_send(message.chat.id, "⏰ <b>Напоминания:</b>\n" + "\n".join(lines) +
                    "\n\nУдалить: <code>напоминание удалить НОМЕР</code>\n"
                    "Перенести: <code>напоминание перенести НОМЕР ДД.ММ.ГГГГ ЧЧ:ММ</code>")


@tools_router.message(StateFilter(None), F.text.regexp(r"(?is)^\s*(?:напоминание\s+(?:удалить|перенести)|удали\s+напоминание)(?:\s|$)"))
async def tools_reminder_manage_command(message: Message):
    if not feature_message_allowed(message, public=False):
        return
    low = message.text.strip().casefold()
    delete_match = re.fullmatch(r"(?:напоминание\s+удалить|удали\s+напоминание)\s+#?(\d+)", low)
    move_match = re.fullmatch(r"напоминание\s+перенести\s+#?(\d+)\s+(.+)", low)
    if not delete_match and not move_match:
        return await safe_send(message.chat.id, "⚠️ Формат:\n<code>напоминание удалить 3</code>\n"
                               "<code>напоминание перенести 3 25.10.2026 18:30</code>")
    raw_id = (delete_match or move_match).group(1)
    if len(raw_id) > 19:
        return await safe_send(message.chat.id, "⚠️ Некорректный номер напоминания.")
    rid = int(raw_id)
    if rid <= 0 or rid > 9223372036854775807:
        return await safe_send(message.chat.id, "⚠️ Некорректный номер напоминания.")
    row = _tools_get_reminder(rid, _tools_reminder_chats(message))
    if not row:
        return await safe_send(message.chat.id, "⚠️ Напоминание не найдено в этом чате или уже отправлено.")
    if delete_match:
        changed = delete_reminder(rid, chat_id=row["chat_id"])
        if changed:
            log_action(message.from_user.id, f"Удалено напоминание #{rid}")
        return await safe_send(message.chat.id, f"✅ Напоминание #{rid} удалено." if changed else "⚠️ Напоминание уже отправлено или удалено.")
    try:
        run_at = parse_tools_datetime(move_match.group(2))
    except ValueError as exc:
        return await safe_send(message.chat.id, "⚠️ " + esc(str(exc)))
    changed = reschedule_reminder(rid, row["chat_id"], run_at)
    if changed:
        log_action(message.from_user.id, f"Перенесено напоминание #{rid}")
    await safe_send(message.chat.id, (f"✅ Напоминание #{rid} перенесено на {run_at.strftime('%d.%m.%Y %H:%M')}."
                                    if changed else "⚠️ Напоминание уже отправлено или удалено."))


# ============================ ПЛАНИРОВЩИК ============================
def notification_was_sent(key: str) -> bool:
    with db_session() as conn:
        return conn.execute("SELECT 1 FROM notification_log WHERE key = ?", (key,)).fetchone() is not None


def remember_notification(key: str):
    with db_session() as conn:
        conn.execute("INSERT OR IGNORE INTO notification_log (key, sent_at) VALUES (?, ?)", (key, get_now().isoformat()))
        conn.execute("DELETE FROM notification_log WHERE sent_at < ?", ((get_now() - timedelta(days=60)).isoformat(),))


def build_digest(d: date, greeting: str) -> str | None:
    classes = get_classes_for_date(d)
    if SCHEDULE_FEED is not None:
        return f"<b>П-114</b>\n{day_label(d)}\n\n" + ("\n\n".join(fmt_class(c) for c in classes) if classes else "На сайте нет занятий на эту дату.")
    hw = homework_for_date(d)
    ev = events_for_date(d)
    if not classes and not hw and not ev and d.weekday() == 6:
        return None
    day_name = DAYS_RU[DAYS_ORDER[d.weekday()]]
    text = f"{greeting} <b>{day_name}, {fmt_date(d)}</b>\n\n"
    text += ("📅 <b>Пары:</b>\n" + "\n".join(fmt_class(c) for c in classes)) if classes else "🎉 Пар нет!"
    if hw:
        text += "\n\n📝 <b>Сдать в этот день:</b>\n" + "\n".join(f"• <b>{esc(h['subject'])}</b>: {esc(h['task'])}" for h in hw)
    if ev:
        text += "\n\n📆 <b>События:</b>\n" + "\n".join(f"• {esc(e['title'])}" for e in ev)
    return text


async def send_morning_digest():
    if not await refresh_live_schedule():
        return
    today = get_now().date()
    key = f"morning:{today.isoformat()}"
    text = build_digest(today, "☀️ Доброе утро!")
    if not text or notification_was_sent(key):
        return
    old = get_setting("last_schedule_msg_id", "")
    sent = await safe_send(GROUP_CHAT_ID, text)
    if sent:
        set_setting("last_schedule_msg_id", str(sent.message_id))
        remember_notification(key)
    if old and sent:
        try:
            await bot.delete_message(GROUP_CHAT_ID, int(old))
        except Exception:
            pass


async def send_evening_preview():
    if not await refresh_live_schedule():
        return
    if get_setting("evening_enabled") != "1":
        return
    target = get_now().date() + timedelta(days=1)
    key = f"evening:{target.isoformat()}"
    text = build_digest(target, "🌙 Завтра:")
    if text and not notification_was_sent(key):
        if await safe_send(GROUP_CHAT_ID, text):
            remember_notification(key)


async def send_hw_reminder():
    tomorrow = get_now().date() + timedelta(days=1)
    key = f"hw:{tomorrow.isoformat()}"
    hw = homework_for_date(tomorrow)
    if not hw or notification_was_sent(key):
        return
    text = "📝 <b>Напоминание: завтра сдавать!</b>\n\n" + "\n".join(f"• <b>{esc(h['subject'])}</b>: {esc(h['task'])}" for h in hw)
    if await safe_send(GROUP_CHAT_ID, text):
        remember_notification(key)


async def check_class_reminders():
    if not await refresh_live_schedule():
        return
    try:
        mins = int(get_setting("class_reminder_minutes") or 0)
    except ValueError:
        mins = 0
    if mins <= 0:
        return
    now = get_now()
    target_dt = now + timedelta(minutes=mins)
    target = target_dt.strftime("%H:%M")
    for c in get_classes_for_date(target_dt.date()):
        start, _ = parse_time_range(c["time"])
        if start and start.strftime("%H:%M") == target:
            key = f"class:{c['id']}:{target_dt.date().isoformat()}:{c['time']}:{mins}"
            if not notification_was_sent(key):
                sent = await safe_send(GROUP_CHAT_ID, f"🔔 Через {mins} мин. пара: <b>{esc(c['subject'])}</b>\n🕐 {esc(c['time'])} | 🚪 {esc(c['room'])}")
                if sent:
                    remember_notification(key)


def reschedule_daily(job_id: str, func, time_str: str):
    if scheduler is None:
        return
    try:
        h, m = map(int, time_str.split(":"))
        if not 0 <= h <= 23 or not 0 <= m <= 59:
            raise ValueError("Неверное время")
    except Exception:
        setting_key = {"morning": "morning_time", "evening": "evening_time", "hw": "hw_reminder_time"}.get(job_id, "")
        h, m = map(int, DEFAULT_SETTINGS.get(setting_key, "08:00").split(":"))
    scheduler.add_job(func, "cron", hour=h, minute=m, id=job_id, replace_existing=True,
                      coalesce=True, max_instances=1, misfire_grace_time=300)


def apply_schedule_settings():
    reschedule_daily("morning", send_morning_digest, get_setting("morning_time"))
    reschedule_daily("evening", send_evening_preview, get_setting("evening_time"))
    reschedule_daily("hw", send_hw_reminder, get_setting("hw_reminder_time"))


async def refresh_live_schedule():
    if SCHEDULE_FEED is None:
        return True
    await SCHEDULE_FEED.refresh_safely()
    return SCHEDULE_FEED.ready


async def run_restricted(feed, *, handle_signals=True):
    global scheduler, SCHEDULE_FEED
    me = await bot.get_me()
    if me.id != EXPECTED_BOT_ID:
        await bot.session.close()
        raise RuntimeError("Личность исходного бота не подтверждена")
    if os.path.exists(DB_PATH):
        backup_database("before_startup_migration")
    init_db()
    SCHEDULE_FEED = feed
    backup_database("startup")
    scheduler = AsyncIOScheduler(timezone=TIMEZONE, job_defaults={"coalesce": True, "max_instances": 1,
                                                                "misfire_grace_time": 120})
    apply_schedule_settings()
    scheduler.add_job(check_class_reminders, "cron", minute="*", id="class_check", replace_existing=True)
    scheduler.add_job(backup_database, "cron", hour=2, minute=0, id="backup", replace_existing=True)
    restore_reminders()
    scheduler.start()

    configure_dispatcher()

    logger.info(f"✅ Бот запущен! Время: {get_now().strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        with db_session() as conn:
            reminder_count = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        await safe_send(MAIN_ADMIN_ID, f"✅ Бот запущен в {get_now().strftime('%H:%M:%S')}\nНапоминаний восстановлено: {reminder_count}")
        try:
            await bot.set_my_commands([BotCommand(command="start", description="Меню бота"),
                                       BotCommand(command="help", description="Все команды"),
                                       BotCommand(command="id", description="Узнать свой ID"),
                                       BotCommand(command="cancel", description="Отменить текущий ввод")])
        except Exception as exc:
            logger.warning("Не удалось обновить меню команд: %s", type(exc).__name__)
        await bot.delete_webhook(drop_pending_updates=os.getenv("DROP_PENDING_UPDATES", "0") == "1")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False, handle_signals=handle_signals)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        backup_database("shutdown")


async def main():
    # Compatibility entrypoint also uses the website instead of a manual timetable.
    from bgmu_bot.bgmu import BGMUSource
    from bgmu_bot.website import WebsiteClient
    async with WebsiteClient.from_environment() as website:
        feed = SharedSchedule(BGMUSource(website, cache_ttl=300))
        await feed.refresh_safely()
        await run_restricted(feed)


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            init_db()
            with db_session() as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("Ошибка целостности SQLite")
            configure_dispatcher()
            print(f"Проверка OK. Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}, TZ {TIMEZONE}. Telegram не запрашивался.")
        else:
            asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
