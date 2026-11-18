import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

BISHKEK_TZ = pytz.timezone("Asia/Bishkek")

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

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 727996140

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "barber_base.db"

class BookingState(StatesGroup):
    waiting_for_service = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_phone = State()

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT
        )
    """)
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
    conn.commit()
    conn.close()

def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💇‍♂️ Записаться"), KeyboardButton(text="📋 Мои записи")],
            [KeyboardButton(text="📍 Адрес и контакты"), KeyboardButton(text="ℹ️ О нас")]
        ],
        resize_keyboard=True
    )

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
        (message.from_user.id, message.from_user.username, message.from_user.full_name)
    )
    conn.commit()
    conn.close()

    await message.answer(
        "👋 Привет! Добро пожаловать в наш барбершоп.\nВыберите нужный раздел в меню ниже:",
        reply_markup=get_main_menu()
    )

@dp.message(F.text == "📍 Адрес и контакты")
async def cmd_contacts(message: types.Message):
    await message.answer(
        "📍 **Наш адрес:** г. Ош\n"
        "📞 **Телефон:** +996 (XX) XX-XX-XX\n"
        "⏰ **График работы:** Каждый день с 10:00 до 22:00",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

@dp.message(F.text == "ℹ️ О нас")
async def cmd_about(message: types.Message):
    await message.answer(
        "✂️ Мы — лучший барбершоп в городе. Стильные стрижки, борода, качественный сервис и крутая атмосфера!",
        reply_markup=get_main_menu()
    )

@dp.message(F.text == "📋 Мои записи")
async def cmd_my_bookings(message: types.Message):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, service, date, time FROM bookings WHERE user_id = ? AND status = 'confirmed'",
        (message.from_user.id,)
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await message.answer("У вас пока нет активных записей.", reply_markup=get_main_menu())
        return

    for row in rows:
        booking_id, service, date_str, time_str = row
        text = (
            f"📋 **Ваша запись:**\n"
            f"✂️ Услуга: {service}\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {time_str}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cancel_{booking_id}")]
        ])
        await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("cancel_"))
async def process_cancel(callback: types.CallbackQuery):
    booking_id = int(callback.data.split("_")[1])
    
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id, service, date, time FROM bookings WHERE id = ?", (booking_id,))
    booking = cur.fetchone()
    
    if booking:
        user_id, service, date_str, time_str = booking
        cur.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
        conn.commit()
        conn.close()

        await callback.message.edit_text(f"❌ Запись на {date_str} в {time_str} успешно отменена.")
        await callback.answer("Запись отменена.")

        try:
            admin_text = (
                f"⚠️ **ЗАПИСЬ ОТМЕНЕНА!**\n"
                f"👤 Клиент ID: `{user_id}`\n"
                f"✂️ Услуга: {service}\n"
                f"📅 Дата: {date_str}\n"
                f"🕒 Время: {time_str}\n"
                f"❌ Статус: Отменено"
            )
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу: {e}")
    else:
        conn.close()
        await callback.answer("Запись не найдена.")

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
    selected_service = services.get(callback.data, "Мужская стрижка")
    await state.update_data(service=selected_service)

    now_bishkek = datetime.now(BISHKEK_TZ)
    days_ru = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

    keyboard_buttons = []
    for i in range(7):
        target_date = now_bishkek + timedelta(days=i)
        day_name = days_ru[target_date.weekday()]
        month_name = months_ru[target_date.month - 1]
        date_str = f"{target_date.day} {month_name} {target_date.year}"
        btn_text = f"{target_date.day} {month_name} ({day_name})"
        keyboard_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"date_{date_str}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text("📅 Выберите удобную дату:", reply_markup=keyboard)
    await state.set_state(BookingState.waiting_for_date)
    await callback.answer()

@dp.callback_query(BookingState.waiting_for_date, F.data.startswith("date_"))
async def process_date(callback: types.CallbackQuery, state: FSMContext):
    selected_date = callback.data.replace("date_", "")
    await state.update_data(date=selected_date)

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT time FROM bookings WHERE date = ? AND status = 'confirmed'", (selected_date,))
    booked_times = [row[0] for row in cur.fetchall()]
    conn.close()

    now_bishkek = datetime.now(BISHKEK_TZ)
    months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    today_str = f"{now_bishkek.day} {months_ru[now_bishkek.month - 1]} {now_bishkek.year}"
    current_hour = now_bishkek.hour if selected_date == today_str else -1

    time_buttons = []
    for hour in range(10, 22):
        time_slot = f"{hour:02d}:00"
        
        if selected_date == today_str and hour <= current_hour:
            continue

        if time_slot in booked_times:
            btn_text = f"🔴 {time_slot} (Занято)"
            time_buttons.append([InlineKeyboardButton(text=btn_text, callback_data="booked_slot")])
        else:
            btn_text = f"🟢 {time_slot} (Свободно)"
            time_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"time_{time_slot}")])

    if not time_buttons:
        await callback.message.edit_text("На сегодня все свободное время уже прошло. Выберите другую дату.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=time_buttons)
    await callback.message.edit_text(f"🕒 Выбранная дата: **{selected_date}**\nВыберите доступное время:", reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(BookingState.waiting_for_time)
    await callback.answer()

@dp.callback_query(F.data == "booked_slot")
async def alert_booked(callback: types.CallbackQuery):
    await callback.answer("❌ Это время уже занято, выберите другой слот.", show_alert=True)

@dp.callback_query(BookingState.waiting_for_time, F.data.startswith("time_"))
async def process_time(callback: types.CallbackQuery, state: FSMContext):
    selected_time = callback.data.replace("time_", "")
    await state.update_data(time=selected_time)

    await callback.message.edit_text("📞 Пожалуйста, отправьте ваш номер телефона для подтверждения брони:")
    await state.set_state(BookingState.waiting_for_phone)
    await callback.answer()

@dp.message(BookingState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text
    data = await state.get_data()
    selected_date = data['date']
    selected_time = data['time']
    selected_service = data['service']

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM bookings WHERE date = ? AND time = ? AND status = 'confirmed'",
        (selected_date, selected_time)
    )
    existing = cur.fetchone()

    if existing:
        conn.close()
        await message.answer("⚠️ К сожалению, этот слот только что забронировали. Пожалуйста, начните запись заново через /start.")
        await state.clear()
        return

    cur.execute("""
        INSERT INTO bookings (user_id, user_name, phone, service, date, time, status)
        VALUES (?, ?, ?, ?, ?, ?, 'confirmed')
    """, (message.from_user.id, message.from_user.full_name, phone, selected_service, selected_date, selected_time))
    conn.commit()
    conn.close()

    success_text = (
        f"✅ **Вы успешно записаны!**\n\n"
        f"📅 Дата: {selected_date}\n"
        f"🕒 Время: {selected_time}\n"
        f"✂️ Услуга: {selected_service}\n"
        f"📍 Адрес барбершопа: г. Ош"
    )
    await message.answer(success_text, reply_markup=get_main_menu(), parse_mode="Markdown")

    username_info = f"@{message.from_user.username}" if message.from_user.username else "нет username"
    admin_text = (
        f"🔔 **НОВАЯ ЗАПИСЬ!**\n"
        f"👤 Клиент: {message.from_user.full_name}\n"
        f"📱 Telegram: {username_info} (ID: `{message.from_user.id}`)\n"
        f"📞 Телефон: {phone}\n"
        f"✂️ Услуга: {selected_service}\n"
        f"📅 Дата: {selected_date}\n"
        f"🕒 Время: {selected_time}\n"
        f"💈 Барбер: Основной зал\n"
        f"✅ Статус: Подтверждено"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление админу: {e}")

    await state.clear()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
