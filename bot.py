import os
import sqlite3
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

# Токен и настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 727996140

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "barber_base.db"

# Инициализация базы данных (создаем обе таблицы)
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # Таблица записей
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
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT
        )
    """)
    conn.commit()
    conn.close()

# Обработчик команды /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Сохраняем пользователя в базу при старте
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, full_name) VALUES (?, ?, ?)",
                (message.from_user.id, message.from_user.username, message.from_user.full_name))
    conn.commit()
    conn.close()

    await message.answer(
        "Привет! Добро пожаловать в наш барбершоп. Выберите нужную услугу:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="💇‍♂️ Записаться")]],
            resize_keyboard=True
        )
    )

# Запуск бота
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
