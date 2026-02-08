import asyncio
import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup,
    CallbackQuery
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.enums.parse_mode import ParseMode

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
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", f"/webhook/{TOKEN}")
WEBHOOK_URL = os.getenv("RAILWAY_STATIC_URL", "")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения Railway")

# Инициализация временной зоны
try:
    USER_TIMEZONE = pytz.timezone(TIMEZONE)
except pytz.exceptions.UnknownTimeZoneError:
    logger.warning(f"Неизвестная временная зона {TIMEZONE}, используем Europe/Moscow")
    USER_TIMEZONE = pytz.timezone("Europe/Moscow")

# ---------------------
# Инициализация бота
# ---------------------
bot = Bot(token=TOKEN, parse_mode=ParseMode.MARKDOWN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ВАШ СУЩЕСТВУЮЩИЙ КОД:
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
    """Инициализация базы данных с индексами"""
    with get_db_connection() as conn:
        c = conn.cursor()
        # ... (ваш существующий код создания таблиц и индексов)
        logger.info(f"База данных инициализирована по пути: {DB_PATH}")

# ---------------------
# FSM состояния (ваши состояния)
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
# Утилиты (ваши утилиты)
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

# ... (все остальные ваши функции: клавиатуры, обработчики команд и т.д.)
# НЕ УДАЛЯЙТЕ ВАШ СУЩЕСТВУЮЩИЙ КОД!

# ---------------------
# Планировщик напоминаний (адаптированный)
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

async def send_reminders(current_time: str):
    """Отправка напоминаний для текущего времени"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, name, dose, times FROM medications")
            meds = c.fetchall()
            
            for med_id, user_id, name, dose, times_str in meds:
                times_list = [t.strip() for t in times_str.split(",")]
                
                if current_time in times_list:
                    # Проверка недавнего приёма
                    if not recently_taken(conn, user_id, med_id, name):
                        await send_reminder(user_id, med_id, name, dose)
                    else:
                        logger.info(f"⏭️ Напоминание пропущено - недавний приём")
    except Exception as e:
        logger.error(f"Ошибка в send_reminders: {e}")

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
                await send_reminders(current_minute)
            
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
    
    # Если есть WEBHOOK_URL (Railway продакшен), настраиваем вебхук
    if WEBHOOK_URL and len(WEBHOOK_URL) > 10:
        webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url)
        logger.info(f"Вебхук установлен на: {webhook_url}")
    else:
        # Режим разработки
        logger.info("Запуск в режиме разработки")
    
    # Запускаем планировщик напоминаний
    asyncio.create_task(reminder_loop())

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Остановка бота...")
    await bot.session.close()

async def main_webhook():
    """Запуск через вебхук (для Railway)"""
    from aiohttp import web
    
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
    
    # Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Режим запуска определяем по наличию WEBHOOK_URL
    if WEBHOOK_URL and len(WEBHOOK_URL) > 10:
        # Режим вебхука для Railway
        asyncio.run(main_webhook())
    else:
        # Режим поллинга для разработки
        asyncio.run(main_polling())
