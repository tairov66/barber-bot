import os
import sqlite3
import logging
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# Заглушка веб-сервера для Render, чтобы он не ругался на порты
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

# Запускаем веб-сервер в отдельном потоке
Thread(target=run_web_server, daemon=True).start()

# Токен и настройки бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 727996140

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_NAME = "barber_base.db"

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

# Главное меню с кнопками
def get_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💇‍♂️ Записаться"), KeyboardButton(text="📋 Мои записи")],
            [KeyboardButton(text="📍 Адрес и контакты"), KeyboardButton(text="ℹ️ О нас")]
        ],
        resize_keyboard=True
    )

# Обработчик команды /start
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

# Обработчик кнопки «Адрес и контакты»
@dp.message(F.text == "📍 Адрес и контакты")
async def cmd_contacts(message: types.Message):
    await message.answer(
        "📍 **Наш адрес:** г. Ош\n"
        "📞 **Телефон:** +996 (XX) XX-XX-XX\n"
        "⏰ **График работы:** Каждый день с 10:00 до 20:00",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

# Обработчик кнопки «О нас»
@dp.message(F.text == "ℹ️ О нас")
async def cmd_about(message: types.Message):
    await message.answer(
        "✂️ Мы — лучший барбершоп в городе. Стильные стрижки, борода, качественный сервис и крутая атмосфера!",
        reply_markup=get_main_menu()
    )

# Запуск бота
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
