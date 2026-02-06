import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------
# Конфигурация
# ---------------------
TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN")
if TOKEN == "PASTE_YOUR_BOT_TOKEN":
    print("⚠️ Установите переменную окружения BOT_TOKEN!")

# ---------------------
# Инициализация
# ---------------------
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# ---------------------
# База данных
# ---------------------
DB_PATH = "medbot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(user_id)
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
    
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_PATH)

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

class DeleteMed(StatesGroup):
    confirm = State()

# ---------------------
# Утилиты
# ---------------------
def mmol_to_mg(value):
    return round(value * 18, 1)

def mg_to_mmol(value):
    return round(value / 18, 1)

def parse_times(times_str):
    """Парсит строку времени в список времен"""
    pattern = r'(\d{1,2}):(\d{2})'
    matches = re.findall(pattern, times_str)
    return [f"{int(h):02d}:{m}" for h, m in matches]

# ---------------------
# Клавиатуры
# ---------------------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить лекарство", callback_data="add_med")],
        [InlineKeyboardButton(text="📋 Мои лекарства", callback_data="list_meds")],
        [InlineKeyboardButton(text="🩸 Глюкоза", callback_data="add_glucose")],
        [InlineKeyboardButton(text="❤️ Давление", callback_data="add_pressure")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")]
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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name FROM users WHERE user_id = ?", (message.from_user.id,))
    user = c.fetchone()
    conn.close()
    
    if user:
        await message.answer(
            f"👋 С возвращением, {user[0]}!",
            reply_markup=main_menu()
        )
    else:
        await state.set_state(Onboarding.name)
        await message.answer(
            "👋 Привет! Я *МедНапоминалка*\n\nКак тебя называть?",
            parse_mode="Markdown"
        )

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)",
        (message.from_user.id, message.text)
    )
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer(
        f"Рад знакомству, {message.text} 🙂\n\nЯ буду напоминать о приёме лекарств!",
        reply_markup=main_menu()
    )

# ---------------------
# Добавление лекарства
# ---------------------
@dp.callback_query(F.data == "add_med")
async def add_med_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddMed.name)
    await callback.message.answer("Введите название лекарства:")
    await callback.answer()

@dp.message(AddMed.name)
async def add_med_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AddMed.dose)
    await message.answer("Введите дозировку (например: 500 мг, 1 таблетка):")

@dp.message(AddMed.dose)
async def add_med_dose(message: Message, state: FSMContext):
    await state.update_data(dose=message.text)
    await state.set_state(AddMed.times)
    await message.answer(
        "Введите время приёма в формате ЧЧ:ММ\n"
        "Можно несколько: `08:00, 14:00, 20:00`",
        parse_mode="Markdown"
    )

@dp.message(AddMed.times)
async def add_med_times(message: Message, state: FSMContext):
    data = await state.get_data()
    times = parse_times(message.text)
    
    if not times:
        await message.answer("❌ Неверный формат времени. Используйте ЧЧ:ММ")
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO medications (user_id, name, dose, times) VALUES (?, ?, ?, ?)",
        (message.from_user.id, data["name"], data["dose"], ",".join(times))
    )
    conn.commit()
    med_id = c.lastrowid
    conn.close()
    
    await state.clear()
    await message.answer(
        f"💊 *{data['name']}* добавлено!\n"
        f"Дозировка: {data['dose']}\n"
        f"Время: {', '.join(times)}",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# ---------------------
# Список лекарств
# ---------------------
@dp.callback_query(F.data == "list_meds")
async def list_meds(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, name, dose, times FROM medications WHERE user_id = ?",
        (callback.from_user.id,)
    )
    meds = c.fetchall()
    conn.close()
    
    if not meds:
        await callback.message.edit_text(
            "У вас пока нет добавленных лекарств.\nНажмите ➕ чтобы добавить.",
            reply_markup=main_menu()
        )
    else:
        text = "📋 *Ваши лекарства:*\n\n"
        for med in meds:
            text += f"💊 *{med[2]}*\n   Доза: {med[3]}\n   Время: {med[4]}\n\n"
        text += "Нажмите на лекарство, чтобы удалить:"
        
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=meds_list_kb(meds))
    
    await callback.answer()

@dp.callback_query(F.data.startswith("del_med_"))
async def delete_med(callback: CallbackQuery):
    med_id = int(callback.data.split("_")[2])
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name FROM medications WHERE id = ? AND user_id = ?", 
              (med_id, callback.from_user.id))
    med = c.fetchone()
    
    if med:
        c.execute("DELETE FROM medications WHERE id = ?", (med_id,))
        conn.commit()
        await callback.answer(f"🗑 {med[0]} удалено")
    conn.close()
    
    await list_meds(callback)

# ---------------------
# Глюкоза
# ---------------------
@dp.callback_query(F.data == "add_glucose")
async def glucose_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddGlucose.value)
    await callback.message.answer(
        "Введите уровень глюкозы:\n"
        "• `5.6 mmol` — в ммоль/л\n"
        "• `100 mg` — в мг/дл",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(AddGlucose.value)
async def glucose_value(message: Message, state: FSMContext):
    text = message.text.lower().replace(",", ".")
    match = re.findall(r"([\d.]+)\s*(mmol|mg)", text)
    
    if not match:
        await message.answer("❌ Неверный формат. Используйте: `5.6 mmol` или `100 mg`")
        return

    value, unit = match[0]
    value = float(value)

    if unit == "mg":
        mmol = mg_to_mmol(value)
        mg = int(value)
    else:
        mmol = value
        mg = int(mmol_to_mg(value))

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO glucose_logs (user_id, mmol, mg) VALUES (?, ?, ?)",
        (message.from_user.id, mmol, mg)
    )
    conn.commit()
    conn.close()

    await state.clear()

    alert = ""
    if mmol < 3.9:
        alert = "\n\n⚠️ *Гипогликемия!* Срочно съешьте что-то сладкое!"
    elif mmol > 13.9:
        alert = "\n\n⚠️ *Гипергликемия!* Обратитесь к врачу."

    status = "🟢 Норма" if 3.9 <= mmol <= 7.0 else "🟡 Выше нормы" if mmol <= 13.9 else "🔴 Опасно"

    await message.answer(
        f"🩸 *Глюкоза сохранена*\n\n"
        f"{mmol} mmol/L\n"
        f"~{mg} mg/dL\n\n"
        f"Статус: {status}{alert}",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# ---------------------
# Давление
# ---------------------
@dp.callback_query(F.data == "add_pressure")
async def pressure_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddPressure.value)
    await callback.message.answer(
        "Введите артериальное давление:\n"
        "Формат: `120/80`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.message(AddPressure.value)
async def pressure_value(message: Message, state: FSMContext):
    match = re.findall(r"(\d{2,3})\s*/\s*(\d{2,3})", message.text)
    
    if not match:
        await message.answer("❌ Неверный формат. Используйте: `120/80`")
        return

    sys, dia = map(int, match[0])

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO pressure_logs (user_id, sys, dia) VALUES (?, ?, ?)",
        (message.from_user.id, sys, dia)
    )
    conn.commit()
    conn.close()

    await state.clear()

    # Классификация ВОЗ
    if sys < 120 and dia < 80:
        status = "🟢 Оптимальное"
        alert = ""
    elif sys < 130 and dia < 85:
        status = "🟡 Нормальное"
        alert = ""
    elif sys < 140 and dia < 90:
        status = "🟡 Высокое нормальное"
        alert = "\n\n⚠️ Следите за давлением"
    elif sys < 160 and dia < 100:
        status = "🟠 Гипертония 1 ст."
        alert = "\n\n⚠️ Рекомендуется консультация врача"
    elif sys < 180 and dia < 110:
        status = "🔴 Гипертония 2 ст."
        alert = "\n\n⚠️ Обратитесь к врачу!"
    else:
        status = "🔴 Гипертония 3 ст."
        alert = "\n\n🚨 *Срочно обратитесь к врачу!*"

    if sys < 90 or dia < 60:
        status = "🔴 Гипотония"
        alert = "\n\n⚠️ Низкое давление!"

    await message.answer(
        f"❤️ *Давление сохранено*\n\n"
        f"{sys}/{dia} мм рт.ст.\n\n"
        f"Категория: {status}{alert}",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# ---------------------
# Статистика
# ---------------------
@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    
    # Последние 5 замеров глюкозы
    c.execute(
        "SELECT mmol, mg, logged_at FROM glucose_logs "
        "WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
        (callback.from_user.id,)
    )
    glucose = c.fetchall()
    
    # Последние 5 замеров давления
    c.execute(
        "SELECT sys, dia, logged_at FROM pressure_logs "
        "WHERE user_id = ? ORDER BY logged_at DESC LIMIT 5",
        (callback.from_user.id,)
    )
    pressure = c.fetchall()
    
    # Статистика приёма лекарств за сегодня
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute(
        "SELECT med_name, taken_at FROM med_logs "
        "WHERE user_id = ? AND DATE(taken_at) = ? ORDER BY taken_at DESC",
        (callback.from_user.id, today)
    )
    meds_today = c.fetchall()
    
    conn.close()
    
    text = "📊 *Статистика*\n\n"
    
    text += "🩸 *Глюкоза (последние 5):*\n"
    if glucose:
        for g in glucose:
            text += f"• {g[0]} mmol ({g[1]} mg) — {g[2][:16]}\n"
    else:
        text += "Нет данных\n"
    
    text += "\n❤️ *Давление (последние 5):*\n"
    if pressure:
        for p in pressure:
            text += f"• {p[0]}/{p[1]} — {p[2][:16]}\n"
    else:
        text += "Нет данных\n"
    
    text += f"\n💊 *Принято сегодня ({len(meds_today)}):*\n"
    if meds_today:
        for m in meds_today:
            time = m[1][11:16]
            text += f"• {m[0]} в {time}\n"
    else:
        text += "Пока ничего не отмечено\n"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_menu())
    await callback.answer()

# ---------------------
# Обработка приёма лекарства
# ---------------------
@dp.callback_query(F.data.startswith("taken_"))
async def med_taken(callback: CallbackQuery):
    med_id = callback.data.split("_")[1]
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, dose FROM medications WHERE id = ?", (med_id,))
    med = c.fetchone()
    
    if med:
        c.execute(
            "INSERT INTO med_logs (user_id, med_name) VALUES (?, ?)",
            (callback.from_user.id, f"{med[0]} {med[1]}")
        )
        conn.commit()
        
        await callback.answer("✅ Отмечено!")
        await callback.message.edit_text(
            f"✅ *{med[0]} {med[1]}*\n"
            f"Принято в {datetime.now().strftime('%H:%M')}",
            parse_mode="Markdown"
        )
    else:
        await callback.answer("Лекарство не найдено")
    
    conn.close()

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
    await callback.answer()

# ---------------------
# Планировщик напоминаний
# ---------------------
async def check_reminders():
    """Проверяет и отправляет напоминания"""
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    
    conn = get_db()
    c = conn.cursor()
    
    # Получаем все лекарства
    c.execute("SELECT id, user_id, name, dose, times FROM medications")
    meds = c.fetchall()
    
    for med in meds:
        med_id, user_id, name, dose, times_str = med
        times = times_str.split(",")
        
        if current_time in times:
            # Проверяем, не принимал ли уже пользователь это лекарство в ближайшие 30 минут
            thirty_mins_ago = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
            c.execute(
                "SELECT id FROM med_logs WHERE user_id = ? AND med_name LIKE ? AND taken_at > ?",
                (user_id, f"{name}%", thirty_mins_ago)
            )
            
            if not c.fetchone():
                try:
                    await bot.send_message(
                        user_id,
                        f"⏰ *Напоминание о приёме*\n\n"
                        f"💊 {name}\n"
                        f"Доза: {dose}",
                        parse_mode="Markdown",
                        reply_markup=reminder_kb(med_id)
                    )
                except Exception as e:
                    print(f"Ошибка отправки напоминания пользователю {user_id}: {e}")
    
    conn.close()

# ---------------------
# Запуск
# ---------------------
async def on_startup():
    scheduler.add_job(check_reminders, "cron", minute="*/1")
    scheduler.start()
    print("✅ Бот запущен! Планировщик активен.")

async def on_shutdown():
    scheduler.shutdown()
    await bot.session.close()
    print("🛑 Бот остановлен.")

async def main():
    await on_startup()
    try:
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
