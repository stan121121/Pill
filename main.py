import asyncio
import os
import re
import sqlite3
from datetime import datetime, timedelta

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
TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN")

# ---------------------
# Инициализация
# ---------------------
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------------------
# База данных (тот же код)
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

# ---------------------
# Утилиты
# ---------------------
def mmol_to_mg(value):
    return round(value * 18, 1)

def mg_to_mmol(value):
    return round(value / 18, 1)

def parse_times(times_str):
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
        await message.answer(f"👋 С возвращением, {user[0]}!", reply_markup=main_menu())
    else:
        await state.set_state(Onboarding.name)
        await message.answer("👋 Привет! Я *МедНапоминалка*\n\nКак тебя называть?", parse_mode="Markdown")

@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(Onboarding.name)
async def onboarding_name(message: Message, state: FSMContext):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, name) VALUES (?, ?)", 
              (message.from_user.id, message.text))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer(f"Рад знакомству, {message.text} 🙂", reply_markup=main_menu())

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
    await message.answer("Введите дозировку (например: 500 мг):")

@dp.message(AddMed.dose)
async def add_med_dose(message: Message, state: FSMContext):
    await state.update_data(dose=message.text)
    await state.set_state(AddMed.times)
    await message.answer("Введите время приёма (например: 08:00, 20:00):")

@dp.message(AddMed.times)
async def add_med_times(message: Message, state: FSMContext):
    data = await state.get_data()
    times = parse_times(message.text)
    
    if not times:
        await message.answer("❌ Неверный формат времени")
        return
    
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO medications (user_id, name, dose, times) VALUES (?, ?, ?, ?)",
              (message.from_user.id, data["name"], data["dose"], ",".join(times)))
    conn.commit()
    conn.close()
    
    await state.clear()
    await message.answer(f"💊 {data['name']} добавлено!", reply_markup=main_menu())

# ---------------------
# Список лекарств
# ---------------------
@dp.callback_query(F.data == "list_meds")
async def list_meds(callback: CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, name, dose, times FROM medications WHERE user_id = ?", 
              (callback.from_user.id,))
    meds = c.fetchall()
    conn.close()
    
    if not meds:
        await callback.message.edit_text("У вас нет лекарств.", reply_markup=main_menu())
    else:
        text = "📋 *Ваши лекарства:*\n\n"
        for med in meds:
            text += f"💊 *{med[2]}*\n   {med[3]} в {med[4]}\n\n"
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
    await callback.message.answer("Введите глюкозу: `5.6 mmol` или `100 mg`", parse_mode="Markdown")
    await callback.answer()

@dp.message(AddGlucose.value)
async def glucose_value(message: Message, state: FSMContext):
    text = message.text.lower().replace(",", ".")
    match = re.findall(r"([\d.]+)\s*(mmol|mg)", text)
    
    if not match:
        await message.answer("❌ Неверный формат")
        return

    value, unit = match[0]
    value = float(value)
    mmol = mg_to_mmol(value) if unit == "mg" else value
    mg = int(mmol_to_mg(value)) if unit == "mmol" else int(value)

    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO glucose_logs (user_id, mmol, mg) VALUES (?, ?, ?)",
              (message.from_user.id, mmol, mg))
    conn.commit()
    conn.close()

    await state.clear()
    alert = "\n\n⚠️ *Низкий уровень!*" if mmol < 3.9 else "\n\n⚠️ *Высокий уровень!*" if mmol > 13.9 else ""
    await message.answer(f"🩸 {mmol} mmol/L (~{mg} mg/dL){alert}", parse_mode="Markdown", reply_markup=main_menu())

# ---------------------
# Давление
# ---------------------
@dp.callback_query(F.data == "add_pressure")
async def pressure_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddPressure.value)
    await callback.message.answer("Введите давление: `120/80`", parse_mode="Markdown")
    await callback.answer()

@dp.message(AddPressure.value)
async def pressure_value(message: Message, state: FSMContext):
    match = re.findall(r"(\d{2,3})\s*/\s*(\d{2,3})", message.text)
    if not match:
        await message.answer("❌ Неверный формат")
        return

    sys, dia = map(int, match[0])
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO pressure_logs (user_id, sys, dia) VALUES (?, ?, ?)",
              (message.from_user.id, sys, dia))
    conn.commit()
    conn.close()

    await state.clear()
    alert = ""
    if sys >= 140 or dia >= 90:
        alert = "\n\n⚠️ *Повышенное давление*"
    elif sys < 90 or dia < 60:
        alert = "\n\n⚠️ *Пониженное давление*"
    
    await message.answer(f"❤️ {sys}/{dia} мм рт.ст.{alert}", parse_mode="Markdown", reply_markup=main_menu())

# ---------------------
# Статистика
# ---------------------
@dp.callback_query(F.data == "stats")
async def show_stats(callback: CallbackQuery):
    conn = get_db()
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
    conn.close()
    
    text = "📊 *Статистика*\n\n🩸 *Глюкоза:*\n"
    text += "\n".join([f"• {g[0]} mmol — {g[2][:16]}" for g in glucose]) if glucose else "Нет данных"
    
    text += "\n\n❤️ *Давление:*\n"
    text += "\n".join([f"• {p[0]}/{p[1]} — {p[2][:16]}" for p in pressure]) if pressure else "Нет данных"
    
    text += f"\n\n💊 *Сегодня принято ({len(meds_today)}):*\n"
    text += "\n".join([f"• {m[0]} в {m[1][11:16]}" for m in meds_today]) if meds_today else "Нет данных"
    
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_menu())
    await callback.answer()

# ---------------------
# Приём лекарства
# ---------------------
@dp.callback_query(F.data.startswith("taken_"))
async def med_taken(callback: CallbackQuery):
    med_id = callback.data.split("_")[1]
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, dose FROM medications WHERE id = ?", (med_id,))
    med = c.fetchone()
    
    if med:
        c.execute("INSERT INTO med_logs (user_id, med_name) VALUES (?, ?)",
                  (callback.from_user.id, f"{med[0]} {med[1]}"))
        conn.commit()
        await callback.answer("✅ Отмечено!")
        await callback.message.edit_text(f"✅ *{med[0]}* принято в {datetime.now().strftime('%H:%M')}", parse_mode="Markdown")
    else:
        await callback.answer("Лекарство не найдено")
    conn.close()

@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
    await callback.answer()

# ---------------------
# Планировщик на чистом asyncio
# ---------------------
async def reminder_loop():
    """Простой планировщик без внешних зависимостей"""
    while True:
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT id, user_id, name, dose, times FROM medications")
        meds = c.fetchall()
        
        for med in meds:
            med_id, user_id, name, dose, times_str = med
            if current_time in times_str.split(","):
                # Проверяем, не принимали ли уже
                thirty_mins_ago = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("SELECT id FROM med_logs WHERE user_id = ? AND med_name LIKE ? AND taken_at > ?",
                          (user_id, f"{name}%", thirty_mins_ago))
                if not c.fetchone():
                    try:
                        await bot.send_message(user_id, f"⏰ *{name}* {dose}", parse_mode="Markdown", reply_markup=reminder_kb(med_id))
                    except Exception as e:
                        print(f"Ошибка отправки: {e}")
        
        conn.close()
        await asyncio.sleep(60)  # Проверка каждую минуту

# ---------------------
# Запуск
# ---------------------
async def main():
    # Запускаем планировщик как фоновую задачу
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
