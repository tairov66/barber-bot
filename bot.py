import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

# Токен берется из переменных окружения Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 7272996140

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "barber_base.db"

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
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    conn.close()

def get_booked_times(date_str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT time FROM bookings WHERE date = ? AND status != 'cancelled'", (date_str,))
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

def add_booking(user_id, user_name, phone, service, date_str, time_str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bookings (user_id, user_name, phone, service, date, time, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, user_name, phone, service, date_str, time_str, 'pending')
    )
    booking_id = cur.lastrowid
    conn.commit()
    conn.close()
    return booking_id

def update_booking_status(booking_id, status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    conn.close()

def get_booking_by_id(booking_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id, service, date, time, status FROM bookings WHERE id = ?", (booking_id,))
    row = cur.fetchone()
    conn.close()
    return row

def is_working_hours():
    now = datetime.now()
    current_hour = now.hour
    if 9 <= current_hour < 22:
        return True
    return False

SERVICES = {
    "haircut": {"name": "Мужская стрижка", "price": "600 сом"},
    "beard": {"name": "Стрижка бороды", "price": "400 сом"},
    "combo": {"name": "Комплекс (Стрижка + Борода)", "price": "900 сом"},
    "father_son": {"name": "Отец + Сынок", "price": "1100 сом"},
}

ALL_TIMES = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00", "20:00", "21:00"]

class Booking(StatesGroup):
    service = State()
    date = State()
    time = State()
    custom_time = State()
    phone = State()
    night_phone = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    kb = [
        [InlineKeyboardButton(text="✂️ Записаться онлайн", callback_data="menu_book")],
        [InlineKeyboardButton(text="💵 Цены и услуги", callback_data="menu_prices")],
        [InlineKeyboardButton(text="📍 Наш адрес в Оше", callback_data="menu_address")]
    ]
    await message.answer(
        "Приветствуем в нашем барбершопе в Оше! 💈\n\nВыберите нужное действие ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

@dp.callback_query(F.data == "menu_book")
async def start_booking(callback: types.CallbackQuery, state: FSMContext):
    if not is_working_hours():
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        try:
            await callback.message.delete()
        except:
            pass
        await callback.message.answer(
            "Мы уже закрыты, оставьте номер, мы перезвоним вам завтра ☺️",
            reply_markup=kb
        )
        await state.set_state(Booking.night_phone)
        return

    kb = [
        [InlineKeyboardButton(text=f"{v['name']} — {v['price']}", callback_data=f"srv_{k}")]
        for k, v in SERVICES.items()
    ]
    await callback.message.edit_text("Выбери нужную услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(Booking.service)

@dp.message(Booking.night_phone)
async def process_night_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    user_name = message.from_user.first_name or "Клиент"
    
    await message.answer(
        "✅ Спасибо! Мы получили ваш номер и перезвоним вам завтра.",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    if ADMIN_ID != 0:
        user_link = f'<a href="tg://user?id={message.from_user.id}">{user_name}</a>'
        admin_text = (
            f"🌙 <b>Ночная заявка (нерабочее время)!</b>\n\n"
            f"👤 Клиент: {user_link}\n"
            f"📞 Телефон: <code>{phone}</code>"
        )
        try:
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
            
    await state.clear()

@dp.callback_query(F.data == "menu_prices")
async def show_prices(callback: types.CallbackQuery):
    text = "💵 **Наши цены:**\n\n"
    for v in SERVICES.values():
        text += f"• {v['name']} — {v['price']}\n"
    kb = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]]
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "menu_address")
async def show_address(callback: types.CallbackQuery):
    kb = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_back")]]
    await callback.message.edit_text("📍 Мы находимся в городе Ош, ул. Ленина 123 (ориентир: центр города). Ждем тебя! 💈", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "menu_back")
async def back_to_menu(callback: types.CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="✂️ Записаться онлайн", callback_data="menu_book")],
        [InlineKeyboardButton(text="💵 Цены и услуги", callback_data="menu_prices")],
        [InlineKeyboardButton(text="📍 Наш адрес в Оше", callback_data="menu_address")]
    ]
    await callback.message.edit_text(
        "Приветствуем в нашем барбершопе в Оше! 💈\n\nВыберите нужное действие ниже:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

@dp.callback_query(Booking.service, F.data.startswith("srv_"))
async def process_service(callback: types.CallbackQuery, state: FSMContext):
    srv_key = callback.data.split("_", 1)[1]
    await state.update_data(service=SERVICES[srv_key]["name"], price=SERVICES[srv_key]["price"])
    
    today = datetime.now()
    kb = []
    for i in range(7):
        d = (today + timedelta(days=i)).strftime("%d.%m.%Y")
        label = "Сегодня" if i == 0 else ("Завтра" if i == 1 else d)
        kb.append([InlineKeyboardButton(text=f"{label} ({d})", callback_data=f"date_{d}")])
        
    await callback.message.edit_text("Выбери дату:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(Booking.date)

@dp.callback_query(Booking.date, F.data.startswith("date_"))
async def process_date(callback: types.CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_", 1)[1]
    booked = get_booked_times(date_str)
    free_times = [t for t in ALL_TIMES if t not in booked]
    
    await state.update_data(date=date_str)
    kb = []
    row = []
    for t in free_times:
        row.append(InlineKeyboardButton(text=t, callback_data=f"time_{t}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
        
    kb.append([InlineKeyboardButton(text="⌨️ Ввести свое время (например, 12:35)", callback_data="custom_time_btn")])
    
    await callback.message.edit_text(f"Свободное время на {date_str}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(Booking.time)

@dp.callback_query(Booking.time, F.data == "custom_time_btn")
async def ask_custom_time(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.message.delete()
    except:
        pass
    await callback.message.answer("Напиши удобное время вручную в формате ЧЧ:ММ (например: 12:35 или 15:45):")
    await state.set_state(Booking.custom_time)

@dp.message(Booking.custom_time)
async def process_custom_time(message: types.Message, state: FSMContext):
    time_str = message.text.strip()
    
    if ":" not in time_str or len(time_str) > 5:
        await message.answer("⚠️ Неверный формат! Напиши время в формате ЧЧ:ММ (например: 12:35):")
        return

    try:
        hour, minute = map(int, time_str.split(":"))
        if not (10 <= hour < 22 or (hour == 22 and minute == 0)):
            kb = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
                resize_keyboard=True,
                one_time_keyboard=True
            )
            await message.answer(
                "⚠️ Мы работаем с 10:00 до 22:00! Но не переживайте, оставьте свой номер, и мы свяжемся с вами завтра для уточнения записи 😊👍🏻",
                reply_markup=kb
            )
            await state.set_state(Booking.night_phone)
            return
    except ValueError:
        await message.answer("⚠️ Неверный формат времени! Напиши в формате ЧЧ:ММ (например: 14:30):")
        return

    data = await state.get_data()
    
    if time_str in get_booked_times(data["date"]):
        await message.answer("⚠️ К сожалению, это время уже занято. Выбери другое:")
        return

    await state.update_data(time=time_str)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Нажми кнопку ниже, чтобы отправить номер, или напиши его вручную:", reply_markup=kb)
    await state.set_state(Booking.phone)

@dp.callback_query(Booking.time, F.data.startswith("time_"))
async def process_time(callback: types.CallbackQuery, state: FSMContext):
    time_str = callback.data.split("_", 1)[1]
    await state.update_data(time=time_str)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    try:
        await callback.message.delete()
    except:
        pass
    await callback.message.answer("Нажми кнопку ниже, чтобы отправить номер, или напиши его вручную:", reply_markup=kb)
    await state.set_state(Booking.phone)

@dp.message(Booking.phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    data = await state.get_data()
    
    if not data or "time" not in data or "date" not in data:
        await message.answer("⚠️ Сессия устарела или произошла ошибка. Нажми /start для новой записи.", reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    if data["time"] in get_booked_times(data["date"]):
        await message.answer("⚠️ Упс! Пока ты вводил номер, это время уже заняли. Нажми /start и выбери другое.", reply_markup=types.ReplyKeyboardRemove())
        await state.clear()
        return

    user_name = message.from_user.first_name or "Клиент"
    booking_id = add_booking(message.from_user.id, user_name, phone, data["service"], data["date"], data["time"])
    
    await message.answer(
        f"✅ Запись отправлена на рассмотрение!\n\n"
        f"✂️ Услуга: {data['service']} ({data['price']})\n"
        f"📅 Дата: {data['date']}\n"
        f"⏰ Время: {data['time']}\n"
        f"📞 Телефон: {phone}\n\n"
        f"Администратор скоро свяжется с вами или подтвердит запись.",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    if ADMIN_ID != 0:
        user_link = f'<a href="tg://user?id={message.from_user.id}">{user_name}</a>'
        admin_text = (
            f"🔥 <b>Новая заявка на запись! (#ID {booking_id})</b>\n\n"
            f"👤 Клиент: {user_link}\n"
            f"📞 Телефон: <code>{phone}</code>\n"
            f"✂️ Услуга: {data['service']}\n"
            f"📅 Дата: {data['date']} в {data['time']}"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_ok_{booking_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm_no_{booking_id}")
            ]
        ])
        try:
            await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
            
    await state.clear()

@dp.callback_query(F.data.startswith("adm_"))
async def admin_action(callback: types.CallbackQuery):
    data_parts = callback.data.split("_")
    action = data_parts[1]
    booking_id = int(data_parts[2])
    
    booking = get_booking_by_id(booking_id)
    if not booking:
        await callback.answer("⚠️ Запись не найдена в базе!", show_alert=True)
        return
        
    client_id, service, date_str, time_str, status = booking
    
    if action == "ok":
        update_booking_status(booking_id, "confirmed")
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n<b>✅ СТАТУС: Подтверждено администратором</b>",
                parse_mode="HTML"
            )
        except:
            pass
        try:
            await bot.send_message(
                client_id,
                f"✅ Ваша запись на {date_str} в {time_str} ({service}) подтверждена администратором! Ждем вас в барбершопе 💈"
            )
        except:
            pass
        await callback.answer("Запись подтверждена!")
        
    elif action == "no":
        update_booking_status(booking_id, "cancelled")
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n<b>❌ СТАТУС: Отменено</b>",
                parse_mode="HTML"
            )
        except:
            pass
        try:
            await bot.send_message(
                client_id,
                f"❌ К сожалению, ваша запись на {date_str} в {time_str} была отменена. Можете записаться на другое время через /start"
            )
        except:
            pass
        await callback.answer("Запись отменена, слот освобожден!")

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
