import asyncio
import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ---------------------
# Конфигурация логирования
# ---------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------------
# Конфигурация
# ---------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения")

DB_PATH = os.getenv("DB_PATH", "medbot.db")

# ---------------------
# Инициализация
# ---------------------
bot = Bot(token=TOKEN)
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
    """Инициализация базы данных с индексами"""
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
        
        # Создание индексов для производительности
        c.execute("CREATE INDEX IF NOT EXISTS idx_medications_user ON medications(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_glucose_user_date ON glucose_logs(user_id, logged_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pressure_user_date ON pressure_logs(user_id, logged_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_med_logs_user_date ON med_logs(user_id, taken_at)")
        
        logger.info("База данных инициализирована")

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
def mmol_to_mg(value):
    """Конвертация ммоль/л в мг/дл"""
    return round(value * 18, 1)

def mg_to_mmol(value):
    """Конвертация мг/дл в ммоль/л"""
    return round(value / 18, 1)

def parse_times(times_str):
    """Парсинг времени из строки"""
    pattern = r'(\d{1,2}):(\d{2})'
    matches = re.findall(pattern, times_str)
    return [f"{int(h):02d}:{m}" for h, m in matches]

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
# Команды
# ---------------------
@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    """Команда /start - приветствие и регистрация"""
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
            await message.answer("👋 Привет! Я *МедНапоминалка*\n\nКак к Вам обращаться?", parse_mode="Markdown")
            logger.info(f"Новый пользователь {message.from_user.id}")
    except Exception as e:
        logger.error(f"Ошибка в start: {e}")
        await message.answer("Произошла ошибка. Попробуйте позже.")

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    """Команда /menu - главное меню"""
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help - помощь"""
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

*Формат ввода:*
Время: `08:00, 14:00, 20:00`
Глюкоза: `5.4` или `6.2` (ммоль/л)
Давление: `120/80`

*Поддержка:* @support_bot
    """
    await message.answer(help_text, parse_mode="Markdown", reply_markup=back_menu())

@dp.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery):
    """Помощь через callback"""
    help_text = """
📖 *Помощь по боту МедНапоминалка*

*Основные функции:*
• Добавление лекарств и времени приёма
• Автоматические напоминания
• Отслеживание глюкозы и давления
• Статистика приёма

*Формат ввода:*
Время: `08:00, 14:00, 20:00`
Глюкоза: `5.4` или `6.2` (ммоль/л)
Давление: `120/80`
    """
    await callback.message.edit_text(help_text, parse_mode="Markdown", reply_markup=back_menu())
    await callback.answer()

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
# Добавление лекарства
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
            f"Время: {', '.join(times)}",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        logger.info(f"Пользователь {message.from_user.id} добавил лекарство {data['name']}")
    except Exception as e:
        logger.error(f"Ошибка в add_med_times: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")
        await state.clear()

# ---------------------
# Список лекарств
# ---------------------
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
            await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=meds_list_kb(meds))
        
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
# Глюкоза
# ---------------------
@dp.callback_query(F.data == "add_glucose")
async def glucose_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления глюкозы"""
    await state.set_state(AddGlucose.value)
    await callback.message.answer("Введите уровень глюкозы (например: `5.4` или `6.2`)", parse_mode="Markdown")
    await callback.answer()

@dp.message(AddGlucose.value)
async def glucose_value(message: Message, state: FSMContext):
    """Получение значения глюкозы"""
    try:
        text = message.text.replace(",", ".")
        
        # Пытаемся извлечь число
        match = re.findall(r"(\d+\.?\d*)", text)
        
        if not match:
            await message.answer("❌ Неверный формат. Введите число, например: `5.4`", parse_mode="Markdown")
            return

        value = float(match[0])
        
        # Валидация значений (mmol/L)
        if value < 0 or value > 50:
            await message.answer("❌ Недопустимое значение глюкозы (диапазон: 0-50)")
            return
        
        # Считаем, что значение в mmol/L
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
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        logger.info(f"Пользователь {message.from_user.id} записал глюкозу: {mmol} mmol/L")
    except ValueError:
        await message.answer("❌ Неверный формат числа")
    except Exception as e:
        logger.error(f"Ошибка в glucose_value: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")
        await state.clear()

# ---------------------
# Давление
# ---------------------
@dp.callback_query(F.data == "add_pressure")
async def pressure_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления давления"""
    await state.set_state(AddPressure.value)
    await callback.message.answer("Введите давление: `120/80`", parse_mode="Markdown")
    await callback.answer()

@dp.message(AddPressure.value)
async def pressure_value(message: Message, state: FSMContext):
    """Получение значения давления"""
    try:
        match = re.findall(r"(\d{2,3})\s*/\s*(\d{2,3})", message.text)
        if not match:
            await message.answer("❌ Неверный формат. Используйте: `120/80`", parse_mode="Markdown")
            return

        sys, dia = map(int, match[0])
        
        # Валидация значений
        if not (50 <= sys <= 250) or not (30 <= dia <= 150):
            await message.answer("❌ Недопустимые значения давления")
            return
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO pressure_logs (user_id, sys, dia) VALUES (?, ?, ?)",
                      (message.from_user.id, sys, dia))

        await state.clear()
        alert = ""
        if sys >= 140 or dia >= 90:
            alert = "\n\n⚠️ *Повышенное давление*"
        elif sys < 90 or dia < 60:
            alert = "\n\n⚠️ *Пониженное давление*"
        
        await message.answer(
            f"❤️ {sys}/{dia} мм рт.ст.{alert}",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        logger.info(f"Пользователь {message.from_user.id} записал давление: {sys}/{dia}")
    except Exception as e:
        logger.error(f"Ошибка в pressure_value: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")
        await state.clear()

# ---------------------
# Статистика
# ---------------------
@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    """Показ статистики"""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            
            c.execute("SELECT mmol, mg, logged_at FROM glucose_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
                      (callback.from_user.id,))
            glucose = c.fetchall()
            
            c.execute("SELECT sys, dia, logged_at FROM pressure_logs WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
                      (callback.from_user.id,))
            pressure = c.fetchall()
            
            today = datetime.now().strftime("%Y-%m-%d")
            c.execute("SELECT med_name, taken_at FROM med_logs WHERE user_id = ? AND DATE(taken_at) = ?",
                      (callback.from_user.id, today))
            meds_today = c.fetchall()
        
        text = "📊 *Статистика*\n\n🩸 *Глюкоза (последние 5):*\n"
        text += "\n".join([f"• {g[0]:.1f} mmol — {g[2][:16]}" for g in glucose]) if glucose else "Нет данных"
        
        text += "\n\n❤️ *Давление (последние 5):*\n"
        text += "\n".join([f"• {p[0]}/{p[1]} — {p[2][:16]}" for p in pressure]) if pressure else "Нет данных"
        
        text += f"\n\n💊 *Сегодня принято ({len(meds_today)}):*\n"
        text += "\n".join([f"• {m[0]} в {m[1][11:16]}" for m in meds_today]) if meds_today else "Нет данных"
        
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_menu())
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в show_stats: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)

# ---------------------
# Приём лекарства
# ---------------------
@dp.callback_query(F.data.startswith("taken_"))
async def med_taken(callback: CallbackQuery):
    """Отметка о приёме лекарства"""
    try:
        med_id = callback.data.split("_")[1]
        
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name, dose FROM medications WHERE id = ?", (med_id,))
            med = c.fetchone()
            
            if med:
                c.execute("INSERT INTO med_logs (user_id, med_name) VALUES (?, ?)",
                          (callback.from_user.id, f"{med[0]} {med[1]}"))
                await callback.answer("✅ Отмечено!")
                await callback.message.edit_text(
                    f"✅ *{med[0]}* принято в {datetime.now().strftime('%H:%M')}",
                    parse_mode="Markdown"
                )
                logger.info(f"Пользователь {callback.from_user.id} принял {med[0]}")
            else:
                await callback.answer("Лекарство не найдено")
    except Exception as e:
        logger.error(f"Ошибка в med_taken: {e}")
        await callback.answer("Произошла ошибка", show_alert=True)

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
    await callback.answer()

# ---------------------
# Планировщик напоминаний
# ---------------------
def recently_taken(conn, user_id: int, med_id: int, med_name: str) -> bool:
    """Проверка, не отметили ли приём лекарства в последние 15 минут"""
    try:
        c = conn.cursor()
        fifteen_mins_ago = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
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
        return False  # В случае ошибки разрешаем отправку

async def send_reminder(user_id: int, med_id: int, name: str, dose: str):
    """Отправка одного напоминания"""
    try:
        await bot.send_message(
            user_id,
            f"⏰ Время принять *{name}*\nДозировка: {dose}",
            parse_mode="Markdown",
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
            
            logger.info(f"Найдено лекарств в БД: {len(meds)}")
            
            for med_id, user_id, name, dose, times_str in meds:
                times_list = [t.strip() for t in times_str.split(",")]
                logger.info(f"Лекарство: {name}, время: {times_str}, проверяем {current_time}")
                logger.info(f"Список времени после split: {times_list}")
                
                if current_time in times_list:
                    logger.info(f"✅ Совпадение времени! Отправка напоминания пользователю {user_id}")
                    # Проверяем, не отметили ли недавно
                    if not recently_taken(conn, user_id, med_id, name):
                        await send_reminder(user_id, med_id, name, dose)
                    else:
                        logger.info(f"⏭️ Напоминание для пользователя {user_id} пропущено - недавно отмечен приём")
                else:
                    logger.debug(f"Нет совпадения: {current_time} не в {times_list}")
    except Exception as e:
        logger.error(f"Ошибка в send_reminders: {e}", exc_info=True)

async def reminder_loop():
    """Улучшенный планировщик с точной проверкой времени"""
    last_check_minute = None
    logger.info("Планировщик напоминаний запущен")
    
    while True:
        try:
            now = datetime.now()
            current_minute = now.strftime("%H:%M")
            
            # Отправляем напоминания только раз в минуту
            if current_minute != last_check_minute:
                last_check_minute = current_minute
                logger.info(f"Проверка напоминаний для времени: {current_minute}")
                await send_reminders(current_minute)
            
            # Спим до следующей минуты
            seconds_until_next_minute = 60 - now.second
            await asyncio.sleep(seconds_until_next_minute)
        except Exception as e:
            logger.error(f"Ошибка в reminder_loop: {e}")
            await asyncio.sleep(60)

# ---------------------
# Запуск
# ---------------------
async def on_startup():
    """Действия при запуске бота"""
    init_db()
    logger.info("Бот запущен успешно")

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("Бот остановлен")

async def main():
    """Главная функция"""
    try:
        await on_startup()
        
        # Запускаем планировщик как фоновую задачу
        reminder_task = asyncio.create_task(reminder_loop())
        
        # Запускаем polling
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Получен сигнал остановки")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
    finally:
        if 'reminder_task' in locals():
            reminder_task.cancel()
        await on_shutdown()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
