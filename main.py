import asyncio
import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import pytz  # Нужно установить: pip install pytz

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ---------------------
# Конфигурация
# ---------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения")

DB_PATH = os.getenv("DB_PATH", "medbot.db")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")  # Можно изменить через переменную окружения

# Инициализация временной зоны
try:
    USER_TIMEZONE = pytz.timezone(TIMEZONE)
except pytz.exceptions.UnknownTimeZoneError:
    USER_TIMEZONE = pytz.timezone("Europe/Moscow")
    logger.warning(f"Неизвестная временная зона {TIMEZONE}, используем Europe/Moscow")

# ---------------------
# Инициализация
# ---------------------
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------
# Утилиты для работы со временем
# ---------------------
def get_current_user_time():
    """Получение текущего времени в часовом поясе пользователя"""
    return datetime.now(USER_TIMEZONE)

def format_time_for_display(dt):
    """Форматирование времени для отображения"""
    return dt.strftime("%H:%M")

def format_time_for_storage(dt):
    """Форматирование времени для хранения"""
    return dt.strftime("%H:%M")

def parse_user_time(time_str, reference_date=None):
    """Парсинг времени из строки с учетом часового пояса пользователя"""
    try:
        if reference_date is None:
            reference_date = get_current_user_time().date()
        
        # Парсим время
        time_match = re.match(r'(\d{1,2}):(\d{2})', time_str.strip())
        if not time_match:
            return None
            
        hour, minute = map(int, time_match.groups())
        
        # Создаем datetime в часовом поясе пользователя
        dt = USER_TIMEZONE.localize(
            datetime.combine(reference_date, datetime.time(hour=hour, minute=minute))
        )
        
        return dt
    except Exception as e:
        logger.error(f"Ошибка парсинга времени {time_str}: {e}")
        return None

# ---------------------
# Функции для работы с напоминаниями
# ---------------------
def recently_taken(conn, user_id: int, med_id: int, med_name: str) -> bool:
    """Проверка, не отметили ли приём лекарства в последние 15 минут"""
    try:
        c = conn.cursor()
        fifteen_mins_ago = (get_current_user_time() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        
        # Проверяем по имени лекарства, так как в med_logs хранится только имя
        c.execute(
            "SELECT id FROM med_logs WHERE user_id = ? AND med_name LIKE ? AND taken_at > ?",
            (user_id, f"{med_name}%", fifteen_mins_ago)
        )
        result = c.fetchone() is not None
        logger.info(f"Проверка недавнего приёма {med_name} для пользователя {user_id}: {'Да' if result else 'Нет'}")
        return result
    except Exception as e:
        logger.error(f"Ошибка в recently_taken: {e}")
        return False

async def send_reminders():
    """Отправка напоминаний для текущего времени в часовом поясе пользователя"""
    try:
        current_time = format_time_for_display(get_current_user_time())
        logger.info(f"Проверка напоминаний для времени пользователя: {current_time}")
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, user_id, name, dose, times FROM medications")
            meds = c.fetchall()
            
            logger.info(f"Найдено лекарств в БД: {len(meds)}")
            
            for med_id, user_id, name, dose, times_str in meds:
                times_list = [t.strip() for t in times_str.split(",")]
                logger.debug(f"Лекарство: {name}, время: {times_str}, проверяем {current_time}")
                
                if current_time in times_list:
                    logger.info(f"✅ Совпадение времени! Отправка напоминания пользователю {user_id}")
                    # Проверяем, не отметили ли недавно
                    if not recently_taken(conn, user_id, med_id, name):
                        await send_reminder(user_id, med_id, name, dose)
                    else:
                        logger.info(f"⏭️ Напоминание для пользователя {user_id} пропущено - недавно отмечен приём")
    except Exception as e:
        logger.error(f"Ошибка в send_reminders: {e}", exc_info=True)

async def reminder_loop():
    """Улучшенный планировщик с учетом часового пояса"""
    last_check_minute = None
    logger.info(f"Планировщик напоминаний запущен в часовом поясе {TIMEZONE}")
    
    while True:
        try:
            now = get_current_user_time()
            current_minute = format_time_for_display(now)
            
            # Отправляем напоминания только раз в минуту
            if current_minute != last_check_minute:
                last_check_minute = current_minute
                await send_reminders()
            
            # Спим до следующей минуты
            seconds_until_next_minute = 60 - now.second
            await asyncio.sleep(seconds_until_next_minute)
        except Exception as e:
            logger.error(f"Ошибка в reminder_loop: {e}")
            await asyncio.sleep(60)

# ---------------------
# Обработка времени при добавлении лекарства
# ---------------------
@dp.message(AddMed.times)
async def add_med_times(message: Message, state: FSMContext):
    """Получение времени приёма с валидацией"""
    try:
        data = await state.get_data()
        times_str = message.text.strip()
        
        # Парсим и валидируем время
        times = []
        for time_part in times_str.split(","):
            time_part = time_part.strip()
            dt = parse_user_time(time_part)
            if dt:
                times.append(format_time_for_storage(dt))
            else:
                await message.answer(
                    f"❌ Неверный формат времени: '{time_part}'. Используйте формат: 08:00, 14:00"
                )
                return
        
        if not times:
            await message.answer("❌ Не указано ни одного корректного времени.")
            return
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO medications (user_id, name, dose, times) VALUES (?, ?, ?, ?)",
                      (message.from_user.id, data["name"], data["dose"], ",".join(times)))
        
        await state.clear()
        user_time = get_current_user_time().strftime("%H:%M")
        await message.answer(
            f"💊 *{data['name']}* добавлено!\n"
            f"Дозировка: {data['dose']}\n"
            f"Время: {', '.join(times)}\n"
            f"⌚ Текущее время в вашем поясе: {user_time}",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        logger.info(f"Пользователь {message.from_user.id} добавил лекарство {data['name']} на время {', '.join(times)}")
    except Exception as e:
        logger.error(f"Ошибка в add_med_times: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")
        await state.clear()

# ---------------------
# Добавьте также команду для проверки времени
# ---------------------
@dp.message(Command("time"))
async def cmd_time(message: Message):
    """Показывает текущее время в часовом поясе бота"""
    server_time = datetime.now().strftime("%H:%:%S %Z")
    user_time = get_current_user_time().strftime("%H:%M:%S %Z")
    
    await message.answer(
        f"🕒 *Время сервера:* {server_time}\n"
        f"🕒 *Ваше время:* {user_time}\n"
        f"🌍 *Часовой пояс:* {TIMEZONE}",
        parse_mode="Markdown"
    )
