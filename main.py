import asyncio
import os
import re
import sqlite3
import logging
import signal
import sys
from datetime import datetime, timedelta
from contextlib import contextmanager
from aiohttp import web
import pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup,
    CallbackQuery, Update
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import (
    SimpleRequestHandler,
    setup_application
)

# ---------------------
# Конфигурация логирования
# ---------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------
# Конфигурация (из переменных окружения Railway)
# ---------------------
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "medbot.db")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
RAILWAY_ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT", "production")
PORT = int(os.getenv("PORT", 8000))  # Railway предоставляет этот порт[citation:7]

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения Railway")

try:
    USER_TIMEZONE = pytz.timezone(TIMEZONE)
except pytz.exceptions.UnknownTimeZoneError:
    logger.warning(f"Неизвестная временная зона {TIMEZONE}, используем UTC")
    USER_TIMEZONE = pytz.UTC

# ---------------------
# Инициализация
# ---------------------
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Функция для получения текущего времени пользователя
def get_current_user_time():
    return datetime.now(USER_TIMEZONE)

# (ЗДЕСЬ ДОЛЖЕН БЫТЬ ВЕСЬ ВАШ ОСНОВНОЙ КОД БОТА:
# init_db, состояния (StatesGroup), утилиты, клавиатуры,
# обработчики команд (@dp.message, @dp.callback_query),
# и функция reminder_loop)
# ...
# ---------------------
# Запуск приложения
# ---------------------
async def on_startup():
    """Действия при запуске бота"""
    init_db()
    logger.info("Бот запущен успешно")
    
    # Если в продакшн-среде, настраиваем вебхук
    if RAILWAY_ENVIRONMENT == "production":
        # Получаем домен Railway (если установлен) или используем локальный URL для тестов
        webhook_host = os.getenv("RAILWAY_STATIC_URL", f"http://localhost:{PORT}")
        webhook_path = "/webhook"
        webhook_url = f"{webhook_host}{webhook_path}"
        
        # Удаляем старый вебхук и устанавливаем новый
        await bot.delete_webhook()
        await bot.set_webhook(webhook_url)
        logger.info(f"Вебхук установлен на: {webhook_url}")
    else:
        # В режиме разработки просто запускаем поллинг
        logger.info("Запуск в режиме разработки (polling)")

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Остановка бота...")
    if RAILWAY_ENVIRONMENT == "production":
        await bot.delete_webhook()
    await bot.session.close()
    logger.info("Бот остановлен")

async def main_webhook():
    """Запуск бота в режиме вебхука (для продакшна на Railway)"""
    await on_startup()
    
    # Запускаем планировщик напоминаний как фоновую задачу
    reminder_task = asyncio.create_task(reminder_loop())
    
    # Создаем aiohttp приложение
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    
    # Запускаем aiohttp сервер
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host='0.0.0.0', port=PORT)  # Важно для Railway[citation:7]
    await site.start()
    
    logger.info(f"Сервер вебхука запущен на 0.0.0.0:{PORT}")
    
    # Ожидаем завершения
    try:
        await asyncio.Event().wait()
    finally:
        reminder_task.cancel()
        await on_shutdown()
        await runner.cleanup()

async def main_polling():
    """Запуск бота в режиме поллинга (для локальной разработки)"""
    await on_startup()
    
    # Запускаем планировщик напоминаний как фоновую задачу
    reminder_task = asyncio.create_task(reminder_loop())
    
    # Запускаем поллинг
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        await on_shutdown()

if __name__ == "__main__":
    # Выбираем режим запуска в зависимости от окружения
    if RAILWAY_ENVIRONMENT == "production":
        # Запускаем в режиме вебхука
        asyncio.run(main_webhook())
    else:
        # Запускаем в режиме поллинга (для разработки)
        asyncio.run(main_polling())
