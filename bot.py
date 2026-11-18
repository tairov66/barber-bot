import os
import sqlite3
import logging
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

# Заглушка веб-сервера для Render
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

Thread(target=run_web_server, daemon=True).start()

# Настройки бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 727996140

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "barber_base.db"

# Состояния для FSM (процесс записи)
class BookingState(StatesGroup):
    waiting_for_service = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_phone = State()

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            phone TEXT,
            service TEXT,
            date TEXT,
            time TEXT,
            status TEXT DEFAULT 'confirmed'
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT
        )
    """)
    conn.commit()
    conn.close()

# Главное меню
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💇‍♂️ Записаться"), KeyboardButton(text="📋 Мои записи")],
            [KeyboardButton(text="📍 Адрес и контакты"), KeyboardButton(text="ℹ️ О нас")]
        ],
        resize_keyboard=True
    )

# Старт
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
                (message.from_user.id, message.from_user.username, message.from_user.full_name))
    conn.commit()
    conn.close()

    await message.answer(
        "👋 Привет! Добро пожаловать в наш барбершоп.\nВыберите нужный раздел в меню ниже:",
        reply_markup=get_main_menu()
    )

# Адрес и контакты
@dp.message(F.text == "📍 Адрес и контакты")
async def cmd_contacts(message: types.Message):
    await message.answer(
        "📍 **Наш адрес:** г. Ош\n"
        "📞 **Телефон:** +996 (XX) XX-XX-XX\n"
        "⏰ **График работы:** Каждый день с 10:00 до 20:00",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

# О нас
@dp.message(F.text == "ℹ️ О нас")
async def cmd_about(message: types.Message):
    await message.answer(
        "✂️ Мы — лучший барбершоп в городе. Стильные стрижки, борода, качественный сервис и крутая атмосфера!",
        reply_markup=get_main_menu()
    )

# --- ЛОГИКА КНОПКИ "Мои записи" ---
@dp.message(F.text == "📋 Мои записи")
async def cmd_my_bookings(message: types.Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT service, date, time FROM bookings WHERE user_id = ? AND status = 'confirmed'", (message.from_user.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await message.answer("У вас пока нет активных записей.", reply_markup=get_main_menu())
        return

    text = "📋 **Ваши активные записи:**\n\n"
    for idx, row in enumerate(rows, 1):
        text += f"{idx}. Услуга: {row[0]}\n📅 Дата: {row[1]} | ⏰ Время: {row[2]}\n\n"

    await message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")

# --- ЛОГИКА КНОПКИ "Записаться" ---
@dp.message(F.text == "💇‍♂️ Записаться")
async def cmd_book(message: types.Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✂️ Мужская стрижка — 800 сом", callback_data="srv_haircut")],
        [InlineKeyboardButton(text="🧔 Стрижка бороды — 500 сом", callback_data="srv_beard")],
        [InlineKeyboardButton(text="🔥 Комплекс (Стрижка + Борода) — 1200 сом", callback_data="srv_complex")]
    ])
    await message.answer("Выберите услугу:", reply_markup=keyboard)
    await state.set_state(BookingState.waiting_for_service)

@dp.callback_query(BookingState.waiting_for_service, F.data.startswith("srv_"))
async def process_service(callback: types.CallbackQuery, state: FSMContext):
    services = {
        "srv_haircut": "Мужская стрижка",
        "srv_beard": "Стрижка бороды",
        "srv_complex": "Комплекс (Стрижка + Борода)"
    }
    selected_service = services.get(callback.data, "Стрижка")
    await state.update_data(service=selected_service)

    await callback.message.edit_text(f"Вы выбрали: **{selected_service}**.\n\nВведите желаемую дату (например, завтра или 20.11):", parse_mode="Markdown")
    await state.set_state(BookingState.waiting_for_date)
    await callback.answer()

@dp.message(BookingState.waiting_for_date)
async def process_date(message: types.Message, state: FSMContext):
    await state.update_data(date=message.text)
    await message.answer("Введите удобное время (например, 14:00):")
    await state.set_state(BookingState.waiting_for_time)

@dp.message(BookingState.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    await state.update_data(time=message.text)
    await message.answer("Пожалуйста, отправьте ваш номер телефона для связи:")
    await state.set_state(BookingState.waiting_for_phone)

@dp.message(BookingState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text
    data = await state.get_data()
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bookings (user_id, user_name, phone, service, date, time)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (message.from_user.id, message.from_user.full_name, phone, data['service'], data['date'], data['time']))
    conn.commit()
    conn.close()

    await message.answer(
        f"✅ **Вы успешно записаны!**\n\n"
        f"💇‍♂️ Услуга: {data['service']}\n"
        f"📅 Дата: {data['date']}\n"
        f"⏰ Время: {data['time']}\n"
        f"📞 Телефон: {phone}\n\n"
        f"Ждем вас в барбершопе!",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )
    await state.clear()

# Запуск
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
