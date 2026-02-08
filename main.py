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
    Message, InlineKeyboardButton, InlineKeyboardMarkup,
    CallbackQuery
)
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ---------------------
# Конфигурация
# ---------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Railway переменные окружения
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "/data/medbot.db")  # Для Railway volume
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
PORT = int(os.getenv("PORT", 8000))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL", "")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения Railway")

# Инициализация временной зоны (zoneinfo вместо pytz)
try:
    USER_TIMEZONE = ZoneInfo(TIMEZONE)
except Exception as e:
    logger.warning(f"Ошибка временной зоны {TIMEZONE}, используем UTC: {e}")
    USER_TIMEZONE = ZoneInfo("UTC")

# ---------------------
# Инициализация бота (aiogram 3.7.0+ синтаксис)
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
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка БД: {e}")
        raise
    finally:
        conn.close()

def init_db():
    """Инициализация базы данных"""
    with get_db_connection() as conn:
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS medications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            dose TEXT,
            times TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS glucose_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mmol REAL,
            mg INTEGER,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS pressure_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            sys INTEGER,
            dia INTEGER,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS med_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            med_name TEXT,
            taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Индексы для производительности
        c.execute("CREATE INDEX IF NOT EXISTS idx_medications_user ON medications(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_glucose_user_date ON glucose_logs(user_id, logged_at)")
        
        logger.info(f"База данных инициализирована по пути: {DB_PATH}")

# ---------------------
# FSM состояния
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
    """Получение текущего времени в часовом поясе пользователя"""
    return datetime.now(USER_TIMEZONE)

def format_time_for_display(dt):
    """Форматирование времени для отображения"""
    return dt.strftime("%H:%M")

def parse_times(times_str):
    """Парсинг времени из строки"""
    pattern = r'(\d{1,2}):(\d{2})'
    matches = re.findall(pattern, times_str)
    return [f"{int(h):02d}:{m}" for h, m in matches]

def mmol_to_mg(value):
    """Конвертация ммоль/л в мг/дл"""
    return round(value * 18, 1)

def validate_input_length(text, max_length=100):
    """Валидация длины ввода"""
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
        [InlineKeyboardButton(text="🩸 Глюкоза", callback_data="add_glucose")],
        [InlineKeyboardButton(text="❤️ Давление", callback_data="add_pressure")]
    ])

def meds_list_kb(meds):
    buttons = []
    for med in meds:
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑 {med[2]} ({med[3]})", 
                callback_data=f"del_med_{med[0]}"
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
    """Команда /start"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM users WHERE user_id = ?", (message.from_user.id,))
            user = c.fetchone()
        
        if user:
            await message.answer(f"👋 С возвращением, {user[0]}!", reply_markup=main_menu())
            logger.info(f"Пользователь {message.from_user.id} вернулся")
        else:
            await state.set_state(Onboarding.name)
            await message.answer("👋 Привет! Я *МедНапоминалка*\n\nКак к Вам обращаться?")
            logger.info(f"Новый пользователь {message.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка в start: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже.")

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    """Команда /menu"""
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    help_text = """
📖 *Помощь по боту МедНапоминалка*

*Основные функции:*
• Добавление лекарств и времени приёма
• Автоматические напоминания
• Отслеживание глюкозы и давления
• Статистика приёма

*Команды:*
/start - Начало работы
/menu - Главное меню
/help - Эта справка
/time - Текущее время
/version - Версия бота

*Формат ввода:*
Время: `08:00, 14:00, 20:00`
Глюкоза: `5.4` или `6.2` (ммоль/л)
Давление: `120/80`
    """
    await message.answer(help_text, reply_markup=back_menu())

@dp.message(Command("time"))
async def cmd_time(message: Message):
    """Показывает текущее время"""
    user_time = get_current_user_time().strftime("%H:%M:%S %Z")
    await message.answer(f"🕒 *Ваше текущее время:* {user_time}\n🌍 *Часовой пояс:* {TIMEZONE}")

@dp.message(Command("version"))
async def cmd_version(message: Message):
    """Показывает версии"""
    import aiogram
    await message.answer(
        f"📦 *Версии:*\n"
        f"• Python: {sys.version.split()[0]}\n"
        f"• Aiogram: {aiogram.__version__}\n"
        f"• Режим: {'Railway' if RAILWAY_STATIC_URL else 'Разработка'}"
    )

@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    """Обработка имени при регистрации"""
    try:
        name = message.text.strip()
        
        if not validate_input_length(name, 50):
            await message.answer("❌ Имя слишком длинное (макс. 50 символов)")
            return
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)", 
                     (message.from_user.id, name))
        
        await state.clear()
        await message.answer(f"Рад знакомству, {name} 🙂", reply_markup=main_menu())
        logger.info(f"Пользователь {message.from_user.id} зарегистрирован как {name}")
    except Exception as e:
        logger.error(f"Ошибка в onboarding_name: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")

# ---------------------
# Обработчики лекарств
# ---------------------
@dp.callback_query(F.data == "add_med")
async def add_med_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления лекарства"""
    await state.set_state(AddMed.name)
    await callback.message.answer("Введите название лекарства:")
    await callback.answer()

@dp.message(AddMed.name)
async def add_med_name(message: Message, state: FSMContext):
    """Получение названия лекарства"""
    name = message.text.strip()
    
    if not validate_input_length(name, 100):
        await message.answer("❌ Название слишком длинное (макс. 100 символов)")
        return
    
    await state.update_data(name=name)
    await state.set_state(AddMed.dose)
    await message.answer("Введите дозировку (например: 500 мг):")

@dp.message(AddMed.dose)
async def add_med_dose(message: Message, state: FSMContext):
    """Получение дозировки"""
    dose = message.text.strip()
    
    if not validate_input_length(dose, 50):
        await message.answer("❌ Дозировка слишком длинная (макс. 50 символов)")
        return
    
    await state.update_data(dose=dose)
    await state.set_state(AddMed.times)
    await message.answer("Введите время приёма (например: 08:00, 20:00):")

@dp.message(AddMed.times)
async def add_med_times(message: Message, state: FSMContext):
    """Получение времени приёма"""
    try:
        data = await state.get_data()
        times = parse_times(message.text)
        
        if not times:
            await message.answer("❌ Неверный формат времени. Используйте формат: 08:00, 14:00")
            return
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO medications (user_id, name, dose, times) VALUES (?, ?, ?, ?)",
                     (message.from_user.id, data["name"], data["dose"], ",".join(times)))
        
        await state.clear()
        await message.answer(
            f"💊 *{data['name']}* добавлено!\n"
            f"Дозировка: {data['dose']}\n"
            f"Время: {', '.join(times)}\n"
            f"⌚ Текущее время: {format_time_for_display(get_current_user_time())}",
            reply_markup=main_menu()
        )
        logger.info(f"Пользователь {message.from_user.id} добавил лекарство {data['name']}")
    except Exception as e:
        logger.error(f"Ошибка в add_med_times: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")
        await state.clear()

@dp.callback_query(F.data == "list_meds")
async def list_meds(callback: CallbackQuery):
    """Показ списка лекарств"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, name, dose, times FROM medications WHERE user_id = ?", 
                     (callback.from_user.id,))
            meds = c.fetchall()
        
        if not meds:
            await callback.message.edit_text("У вас нет лекарств.", reply_markup=main_menu())
        else:
            text = "📋 *Ваши лекарства:*\n\n"
            for med in meds:
                text += f"💊 *{med[2]}*\n   {med[3]} в {med[4]}\n\n"
            await callback.message.edit_text(text, reply_markup=meds_list_kb(meds))
        
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в list_meds: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("del_med_"))
async def delete_med(callback: CallbackQuery):
    """Удаление лекарства"""
    try:
        med_id = int(callback.data.split("_")[2])
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM medications WHERE id = ? AND user_id = ?", 
                     (med_id, callback.from_user.id))
            med = c.fetchone()
            
            if med:
                c.execute("DELETE FROM medications WHERE id = ?", (med_id,))
                await callback.answer(f"🗑 {med[0]} удалено")
                logger.info(f"Пользователь {callback.from_user.id} удалил лекарство {med[0]}")
            else:
                await callback.answer("Лекарство не найдено")
        
        await list_meds(callback)
    except Exception as e:
        logger.error(f"Ошибка в delete_med: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)

# ---------------------
# Глюкоза и давление (сокращённо)
# ---------------------
@dp.callback_query(F.data == "add_glucose")
async def glucose_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления глюкозы"""
    await state.set_state(AddGlucose.value)
    await callback.message.answer("Введите уровень глюкозы (например: `5.4` или `6.2`)")
    await callback.answer()

@dp.message(AddGlucose.value)
async def glucose_value(message: Message, state: FSMContext):
    """Получение значения глюкозы"""
    try:
        text = message.text.replace(",", ".")
        match = re.findall(r"(\d+\.?\d*)", text)
        
        if not match:
            await message.answer("❌ Неверный формат. Введите число, например: `5.4`")
            return

        value = float(match[0])
        
        if value < 0 or value > 50:
            await message.answer("❌ Недопустимое значение глюкозы (диапазон: 0-50)")
            return
        
        mmol = value
        mg = int(mmol_to_mg(mmol))

        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO glucose_logs (user_id, mmol, mg) VALUES (?, ?, ?)",
                     (message.from_user.id, mmol, mg))

        await state.clear()
        alert = "\n\n⚠️ *Низкий уровень!*" if mmol < 3.9 else "\n\n⚠️ *Высокий уровень!*" if mmol > 13.9 else ""
        await message.answer(
            f"🩸 {mmol:.1f} mmol/L (~{mg} mg/dL){alert}",
            reply_markup=main_menu()
        )
        logger.info(f"Пользователь {message.from_user.id} записал глюкозу: {mmol} mmol/L")
    except Exception as e:
        logger.error(f"Ошибка в glucose_value: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")
        await state.clear()

# ---------------------
# Планировщик напоминаний
# ---------------------
async def send_reminder(user_id: int, med_id: int, name: str, dose: str):
    """Отправка одного напоминания"""
    try:
        await bot.send_message(
            user_id,
            f"⏰ Время принять *{name}*\nДозировка: {dose}",
            reply_markup=reminder_kb(med_id)
        )
        logger.info(f"Отправлено напоминание пользователю {user_id}: {name}")
    except Exception as e:
        logger.error(f"Не удалось отправить напоминание пользователю {user_id}: {e}")

async def reminder_loop():
    """Планировщик напоминаний"""
    last_check_minute = None
    
    while True:
        try:
            now = get_current_user_time()
            current_minute = format_time_for_display(now)
            
            if current_minute != last_check_minute:
                last_check_minute = current_minute
                logger.info(f"Проверка напоминаний: {current_minute}")
                
                with get_db_connection() as conn:
                    c = conn.cursor()
                    c.execute("SELECT id, user_id, name, dose, times FROM medications")
                    meds = c.fetchall()
                    
                    for med_id, user_id, name, dose, times_str in meds:
                        times_list = [t.strip() for t in times_str.split(",")]
                        if current_minute in times_list:
                            # Проверка недавнего приёма (последние 15 минут)
                            fifteen_mins_ago = (now - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
                            c.execute(
                                "SELECT id FROM med_logs WHERE user_id = ? AND med_name LIKE ? AND taken_at > ?",
                                (user_id, f"{name}%", fifteen_mins_ago)
                            )
                            if not c.fetchone():
                                await send_reminder(user_id, med_id, name, dose)
            
            seconds_until_next_minute = 60 - now.second
            await asyncio.sleep(seconds_until_next_minute)
        except Exception as e:
            logger.error(f"Ошибка в reminder_loop: {e}")
            await asyncio.sleep(60)

# ---------------------
# Запуск приложения
# ---------------------
async def on_startup():
    """Действия при запуске бота"""
    init_db()
    
    # Настройка вебхука для Railway
    if RAILWAY_STATIC_URL:
        webhook_url = f"{RAILWAY_STATIC_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url)
        logger.info(f"Вебхук установлен: {webhook_url}")
    else:
        await bot.delete_webhook()
        logger.info("Запуск в режиме разработки")
    
    # Запускаем планировщик напоминаний
    asyncio.create_task(reminder_loop())

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Остановка бота...")
    await bot.session.close()

async def main_webhook():
    """Запуск через вебхук (для Railway)"""
    await on_startup()
    
    # Создаем aiohttp приложение
    app = web.Application()
    
    # Создаем обработчик вебхуков aiogram
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    
    # Регистрируем обработчик
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    # Настраиваем приложение aiogram
    setup_application(app, dp, bot=bot)
    
    # Запускаем сервер
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Railway предоставляет PORT переменную
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)
    await site.start()
    
    logger.info(f"Сервер запущен на порту {PORT}")
    
    # Бесконечно ждем
    await asyncio.Event().wait()

async def main_polling():
    """Запуск через поллинг (для разработки)"""
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        # Определяем режим запуска
        if RAILWAY_STATIC_URL:
            asyncio.run(main_webhook())
        else:
            asyncio.run(main_polling())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
