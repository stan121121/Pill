import asyncio
import os
import re
import sqlite3
import logging
import sys
from datetime import datetime, timedelta
from contextlib import contextmanager
from aiohttp import web
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ---------------------
# Конфигурация логирования
# ---------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------------------
# Конфигурация
# ---------------------
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "medbot.db")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
PORT = int(os.getenv("PORT", 8000))
WEBHOOK_PATH = "/webhook"
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения")

# Определяем режим работы
USE_WEBHOOK = bool(RAILWAY_PUBLIC_DOMAIN)

# Инициализация временной зоны
try:
    USER_TIMEZONE = ZoneInfo(TIMEZONE)
    logger.info(f"✅ Часовой пояс: {TIMEZONE}")
except Exception as e:
    logger.warning(f"⚠️ Ошибка часового пояса {TIMEZONE}, используем UTC: {e}")
    USER_TIMEZONE = ZoneInfo("UTC")
    TIMEZONE = "UTC"

# ---------------------
# Инициализация бота
# ---------------------
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------
# База данных
# ---------------------
@contextmanager
def get_db_connection():
    """Контекстный менеджер для работы с БД"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"❌ Ошибка БД: {e}", exc_info=True)
        raise
    finally:
        conn.close()

def init_db():
    """Инициализация базы данных"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS medications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                dose TEXT,
                times TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS glucose_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mmol REAL NOT NULL,
                mg INTEGER NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS pressure_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                sys INTEGER NOT NULL,
                dia INTEGER NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS med_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                med_name TEXT NOT NULL,
                taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
            
            # Индексы
            c.execute("CREATE INDEX IF NOT EXISTS idx_medications_user ON medications(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_medications_times ON medications(times)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_glucose_user_date ON glucose_logs(user_id, logged_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pressure_user_date ON pressure_logs(user_id, logged_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_med_logs_user_date ON med_logs(user_id, taken_at DESC)")
            
            c.execute("SELECT COUNT(*) as count FROM medications")
            med_count = c.fetchone()['count']
            
            logger.info(f"✅ База данных: {DB_PATH}")
            logger.info(f"📊 Лекарств в БД: {med_count}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации БД: {e}", exc_info=True)
        raise

# ---------------------
# FSM
# ---------------------
class Onboarding(StatesGroup):
    name = State()

class AddMed(StatesGroup):
    name = State()
    dose = State()
    times = State()

class AddGlucose(StatesGroup):
    value = State()

class AddPressure(StatesGroup):
    value = State()

# ---------------------
# Утилиты
# ---------------------
def get_current_user_time():
    return datetime.now(USER_TIMEZONE)

def format_time_for_display(dt):
    return dt.strftime("%H:%M")

def parse_times(times_str):
    pattern = r'(\d{1,2}):(\d{2})'
    matches = re.findall(pattern, times_str)
    result = [f"{int(h):02d}:{m}" for h, m in matches]
    return result

def mmol_to_mg(value):
    return round(value * 18, 1)

def validate_input_length(text, max_length=100):
    return len(text.strip()) <= max_length

# ---------------------
# Клавиатуры
# ---------------------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить лекарство", callback_data="add_med")],
        [InlineKeyboardButton(text="📋 Мои лекарства", callback_data="list_meds")],
        [InlineKeyboardButton(text="🩸 Глюкоза", callback_data="add_glucose")],
        [InlineKeyboardButton(text="❤️ Давление", callback_data="add_pressure")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help")]
    ])

def reminder_kb(med_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принял", callback_data=f"taken_{med_id}")],
        [InlineKeyboardButton(text="🩸 Глюкоза", callback_data="add_glucose")]
    ])

def meds_list_kb(meds):
    buttons = []
    for med in meds:
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑 {med['name']} ({med['dose']})", 
                callback_data=f"del_med_{med['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def back_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])

# ---------------------
# Обработчики команд
# ---------------------
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM users WHERE user_id = ?", (message.from_user.id,))
            user = c.fetchone()
        
        if user:
            await message.answer(f"👋 С возвращением, {user['name']}!", reply_markup=main_menu())
        else:
            await state.set_state(Onboarding.name)
            await message.answer("👋 Привет! Я *МедНапоминалка*\n\nКак к Вам обращаться?")
    except Exception as e:
        logger.error(f"❌ Ошибка start: {e}")
        await message.answer("Ошибка. Попробуйте /start")

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = f"""
📖 *Помощь*

*Команды:*
/start - Начало работы
/menu - Главное меню  
/time - Текущее время
/debug - Отладка

*Формат:*
Время: `08:00, 14:00, 20:00`
Глюкоза: `5.4` (ммоль/л)
Давление: `120/80`

🌍 Часовой пояс: {TIMEZONE}
    """
    await message.answer(help_text, reply_markup=back_menu())

@dp.message(Command("time"))
async def cmd_time(message: Message):
    now = get_current_user_time()
    await message.answer(
        f"🕒 *Время:* `{now.strftime('%H:%M:%S')}`\n"
        f"📅 *Дата:* `{now.strftime('%Y-%m-%d')}`\n"
        f"🌍 *Часовой пояс:* `{TIMEZONE}`"
    )

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            
            now = get_current_user_time()
            current_time = format_time_for_display(now)
            
            c.execute("SELECT * FROM medications WHERE user_id = ?", (message.from_user.id,))
            meds = c.fetchall()
            
            debug_info = f"""
🔍 *Отладка*

⏰ Время: `{now.strftime('%H:%M:%S %Z')}`
👤 ID: `{message.from_user.id}`
💊 Лекарств: {len(meds)}
🔄 Режим: {"Webhook" if USE_WEBHOOK else "Polling"}

"""
            
            if meds:
                debug_info += "*Лекарства:*\n"
                for med in meds:
                    times_list = [t.strip() for t in med['times'].split(",")]
                    match = "✅" if current_time in times_list else "⏰"
                    debug_info += f"{match} *{med['name']}* в `{med['times']}`\n"
            else:
                debug_info += "_Нет лекарств_"
            
            await message.answer(debug_info)
            
    except Exception as e:
        logger.error(f"❌ Ошибка debug: {e}")
        await message.answer("Ошибка")

@dp.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery):
    help_text = f"""
📖 *Помощь*

*Формат:*
Время: `08:00, 14:00, 20:00`
Глюкоза: `5.4` (ммоль/л)
Давление: `120/80`

🌍 Часовой пояс: {TIMEZONE}
    """
    await callback.message.edit_text(help_text, reply_markup=back_menu())
    await callback.answer()

@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    try:
        name = message.text.strip()
        
        if not validate_input_length(name, 50):
            await message.answer("❌ Имя слишком длинное")
            return
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)", 
                     (message.from_user.id, name))
        
        await state.clear()
        await message.answer(f"Рад знакомству, {name} 🙂", reply_markup=main_menu())
        logger.info(f"✅ Регистрация: {message.from_user.id}")
    except Exception as e:
        logger.error(f"❌ Ошибка регистрации: {e}")
        await message.answer("Ошибка")

# ---------------------
# Лекарства
# ---------------------
@dp.callback_query(F.data == "add_med")
async def add_med_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddMed.name)
    await callback.message.answer("Введите название лекарства:")
    await callback.answer()

@dp.message(AddMed.name)
async def add_med_name(message: Message, state: FSMContext):
    name = message.text.strip()
    
    if not validate_input_length(name, 100):
        await message.answer("❌ Слишком длинное")
        return
    
    await state.update_data(name=name)
    await state.set_state(AddMed.dose)
    await message.answer("Дозировка (например: 500 мг):")

@dp.message(AddMed.dose)
async def add_med_dose(message: Message, state: FSMContext):
    dose = message.text.strip()
    
    if not validate_input_length(dose, 50):
        await message.answer("❌ Слишком длинное")
        return
    
    await state.update_data(dose=dose)
    await state.set_state(AddMed.times)
    
    current_time = format_time_for_display(get_current_user_time())
    await message.answer(
        f"Время приёма (например: `{current_time}, 20:00`):\n\n"
        f"⏰ Сейчас: `{current_time}`"
    )

@dp.message(AddMed.times)
async def add_med_times(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        times = parse_times(message.text)
        
        if not times:
            await message.answer("❌ Неверный формат. Пример: `08:00, 20:00`")
            return
        
        times_str = ",".join(times)
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO medications (user_id, name, dose, times) VALUES (?, ?, ?, ?)",
                (message.from_user.id, data["name"], data["dose"], times_str)
            )
            med_id = c.lastrowid
        
        await state.clear()
        
        await message.answer(
            f"✅ *{data['name']}* добавлено!\n\n"
            f"💊 Доза: {data['dose']}\n"
            f"⏰ Время: {', '.join(times)}",
            reply_markup=main_menu()
        )
        logger.info(f"➕ Добавлено: user={message.from_user.id}, med_id={med_id}, times={times_str}")
    except Exception as e:
        logger.error(f"❌ Ошибка add_med_times: {e}")
        await message.answer("Ошибка")
        await state.clear()

@dp.callback_query(F.data == "list_meds")
async def list_meds(callback: CallbackQuery):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT * FROM medications WHERE user_id = ? ORDER BY name", 
                     (callback.from_user.id,))
            meds = c.fetchall()
        
        if not meds:
            await callback.message.edit_text("У вас нет лекарств", reply_markup=main_menu())
        else:
            text = "📋 *Ваши лекарства:*\n\n"
            for med in meds:
                text += f"💊 *{med['name']}*\n   {med['dose']} в {med['times']}\n\n"
            await callback.message.edit_text(text, reply_markup=meds_list_kb(meds))
        
        await callback.answer()
    except Exception as e:
        logger.error(f"❌ Ошибка list_meds: {e}")
        await callback.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("del_med_"))
async def delete_med(callback: CallbackQuery):
    try:
        med_id = int(callback.data.split("_")[2])
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM medications WHERE id = ? AND user_id = ?", 
                     (med_id, callback.from_user.id))
            med = c.fetchone()
            
            if med:
                c.execute("DELETE FROM medications WHERE id = ?", (med_id,))
                await callback.answer(f"🗑 {med['name']} удалено")
                logger.info(f"🗑 Удалено: user={callback.from_user.id}, med_id={med_id}")
            else:
                await callback.answer("Не найдено")
        
        await list_meds(callback)
    except Exception as e:
        logger.error(f"❌ Ошибка delete_med: {e}")
        await callback.answer("Ошибка", show_alert=True)

# ---------------------
# Глюкоза
# ---------------------
@dp.callback_query(F.data == "add_glucose")
async def glucose_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddGlucose.value)
    await callback.message.answer("Введите глюкозу (например: `5.4`)")
    await callback.answer()

@dp.message(AddGlucose.value)
async def glucose_value(message: Message, state: FSMContext):
    try:
        text = message.text.replace(",", ".")
        match = re.findall(r"(\d+\.?\d*)", text)
        
        if not match:
            await message.answer("❌ Неверный формат")
            return

        value = float(match[0])
        
        if not (0 <= value <= 50):
            await message.answer("❌ Значение 0-50")
            return
        
        mmol = value
        mg = int(mmol_to_mg(mmol))

        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO glucose_logs (user_id, mmol, mg) VALUES (?, ?, ?)",
                     (message.from_user.id, mmol, mg))

        await state.clear()
        
        if mmol < 3.9:
            alert = "\n\n⚠️ *Низкий уровень!*"
        elif mmol > 13.9:
            alert = "\n\n⚠️ *Высокий уровень!*"
        else:
            alert = "\n\n✅ *Норма*"
            
        await message.answer(
            f"🩸 {mmol:.1f} ммоль/л (~{mg} мг/дл){alert}",
            reply_markup=main_menu()
        )
        logger.info(f"🩸 Глюкоза: user={message.from_user.id}, value={mmol}")
    except ValueError:
        await message.answer("❌ Неверное число")
    except Exception as e:
        logger.error(f"❌ Ошибка glucose: {e}")
        await message.answer("Ошибка")
        await state.clear()

# ---------------------
# Давление
# ---------------------
@dp.callback_query(F.data == "add_pressure")
async def pressure_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddPressure.value)
    await callback.message.answer("Введите давление (например: `120/80`)")
    await callback.answer()

@dp.message(AddPressure.value)
async def pressure_value(message: Message, state: FSMContext):
    try:
        match = re.findall(r"(\d{2,3})\s*/\s*(\d{2,3})", message.text)
        if not match:
            await message.answer("❌ Неверный формат")
            return

        sys, dia = map(int, match[0])
        
        if not (50 <= sys <= 250) or not (30 <= dia <= 150):
            await message.answer("❌ Недопустимо")
            return
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO pressure_logs (user_id, sys, dia) VALUES (?, ?, ?)",
                     (message.from_user.id, sys, dia))

        await state.clear()
        
        if sys >= 140 or dia >= 90:
            alert = "\n\n⚠️ *Повышенное*"
        elif sys < 90 or dia < 60:
            alert = "\n\n⚠️ *Пониженное*"
        else:
            alert = "\n\n✅ *Норма*"
        
        await message.answer(
            f"❤️ {sys}/{dia} мм рт.ст.{alert}",
            reply_markup=main_menu()
        )
        logger.info(f"❤️ Давление: user={message.from_user.id}, value={sys}/{dia}")
    except Exception as e:
        logger.error(f"❌ Ошибка pressure: {e}")
        await message.answer("Ошибка")
        await state.clear()

# ---------------------
# Статистика
# ---------------------
@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            
            c.execute(
                "SELECT mmol, logged_at FROM glucose_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
                (callback.from_user.id,)
            )
            glucose = c.fetchall()
            
            c.execute(
                "SELECT sys, dia, logged_at FROM pressure_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
                (callback.from_user.id,)
            )
            pressure = c.fetchall()
            
            today = get_current_user_time().strftime("%Y-%m-%d")
            c.execute(
                "SELECT med_name, taken_at FROM med_logs WHERE user_id = ? AND DATE(taken_at) = ? ORDER BY taken_at DESC",
                (callback.from_user.id, today)
            )
            meds_today = c.fetchall()
        
        text = "📊 *Статистика*\n\n"
        
        text += "🩸 *Глюкоза:*\n"
        if glucose:
            for g in glucose:
                dt = g['logged_at'][:16]
                text += f"• {g['mmol']:.1f} ммоль/л — {dt}\n"
        else:
            text += "Нет данных\n"
        
        text += "\n❤️ *Давление:*\n"
        if pressure:
            for p in pressure:
                dt = p['logged_at'][:16]
                text += f"• {p['sys']}/{p['dia']} — {dt}\n"
        else:
            text += "Нет данных\n"
        
        text += f"\n💊 *Сегодня ({len(meds_today)}):*\n"
        if meds_today:
            for m in meds_today:
                time = m['taken_at'][11:16]
                text += f"• {m['med_name']} в {time}\n"
        else:
            text += "Нет приёмов\n"
        
        await callback.message.edit_text(text, reply_markup=back_menu())
        await callback.answer()
    except Exception as e:
        logger.error(f"❌ Ошибка stats: {e}")
        await callback.answer("Ошибка", show_alert=True)

# ---------------------
# Приём лекарства
# ---------------------
@dp.callback_query(F.data.startswith("taken_"))
async def med_taken(callback: CallbackQuery):
    try:
        med_id = int(callback.data.split("_")[1])
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name, dose FROM medications WHERE id = ?", (med_id,))
            med = c.fetchone()
            
            if med:
                c.execute("INSERT INTO med_logs (user_id, med_name) VALUES (?, ?)",
                         (callback.from_user.id, f"{med['name']} {med['dose']}"))
                
                time_str = get_current_user_time().strftime('%H:%M')
                await callback.answer("✅ Отмечено!")
                await callback.message.edit_text(
                    f"✅ *{med['name']}* принято в {time_str}"
                )
                logger.info(f"✅ Принято: user={callback.from_user.id}, med={med['name']}")
            else:
                await callback.answer("Не найдено")
    except Exception as e:
        logger.error(f"❌ Ошибка med_taken: {e}")
        await callback.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
    await callback.answer()

# ---------------------
# Планировщик (ОПТИМИЗИРОВАННЫЙ)
# ---------------------
async def send_reminder(user_id: int, med_id: int, name: str, dose: str):
    try:
        await bot.send_message(
            user_id,
            f"⏰ *Время принять лекарство!*\n\n"
            f"💊 {name}\n"
            f"📋 Дозировка: {dose}",
            reply_markup=reminder_kb(med_id)
        )
        logger.info(f"📤 Напоминание: user={user_id}, med={name}")
        return True
    except Exception as e:
        logger.error(f"❌ Не удалось отправить user={user_id}: {e}")
        return False

async def reminder_loop():
    last_check_minute = None
    logger.info("🚀 Планировщик запущен")
    
    while True:
        try:
            now = get_current_user_time()
            current_minute = format_time_for_display(now)
            
            if current_minute != last_check_minute:
                last_check_minute = current_minute
                logger.info(f"⏰ Проверка: {current_minute}")
                
                with get_db_connection() as conn:
                    c = conn.cursor()
                    
                    c.execute(
                        "SELECT id, user_id, name, dose, times FROM medications WHERE times LIKE ?",
                        (f"%{current_minute}%",)
                    )
                    meds = c.fetchall()
                    
                    if meds:
                        logger.info(f"📋 Найдено совпадений: {len(meds)}")
                    
                    for med in meds:
                        times_list = [t.strip() for t in med['times'].split(",")]
                        
                        if current_minute in times_list:
                            fifteen_mins_ago = (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                            c.execute(
                                "SELECT id FROM med_logs WHERE user_id = ? AND med_name LIKE ? AND taken_at > ?",
                                (med['user_id'], f"{med['name']}%", fifteen_mins_ago)
                            )
                            
                            if not c.fetchone():
                                await send_reminder(med['user_id'], med['id'], med['name'], med['dose'])
                            else:
                                logger.info(f"⏭️ Пропущено (принято): {med['name']}")
            
            seconds_until_next_minute = 60 - now.second
            await asyncio.sleep(max(1, seconds_until_next_minute))
            
        except Exception as e:
            logger.error(f"❌ Ошибка reminder_loop: {e}", exc_info=True)
            await asyncio.sleep(60)

# ---------------------
# Запуск
# ---------------------
async def on_startup():
    logger.info("=" * 50)
    logger.info("🚀 МедНапоминалка")
    logger.info(f"🔧 Режим: {'Webhook' if USE_WEBHOOK else 'Polling'}")
    logger.info(f"🌍 Часовой пояс: {TIMEZONE}")
    logger.info(f"📁 БД: {DB_PATH}")
    
    init_db()
    
    if USE_WEBHOOK:
        webhook_url = f"https://{RAILWAY_PUBLIC_DOMAIN}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info(f"🔗 Webhook: {webhook_url}")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("📡 Polling")
    
    asyncio.create_task(reminder_loop())
    
    logger.info("✅ Бот запущен!")
    logger.info("=" * 50)

async def on_shutdown():
    logger.info("👋 Остановка...")
    await bot.session.close()

async def main_webhook():
    await on_startup()
    
    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
    await site.start()
    
    logger.info(f"🌐 HTTP сервер: {PORT}")
    
    await asyncio.Event().wait()

async def main_polling():
    await on_startup()
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await on_shutdown()

if __name__ == "__main__":
    try:
        if USE_WEBHOOK:
            asyncio.run(main_webhook())
        else:
            asyncio.run(main_polling())
    except (KeyboardInterrupt, SystemExit):
        logger.info("👋 Бот остановлен")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}", exc_info=True)
