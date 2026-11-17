import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

# Твой реальный токен
TOKEN = "8946831317:AAEgn7tYUp90K3QFrAIwqpTtE5zQAxh_fL0"

# Веб-сервер-заглушка для Render, чтобы он видел открытый порт и не вырубал бота
async def handle(request):
    return web.Response(text="Bot is running and alive 24/7!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render передает порт через системные аргументы или берем 10000
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# Основной диспетчер бота
dp = Dispatcher()

@dp.message(types.Command("start"))
async def command_start_handler(message: types.Message):
    await message.answer("Салам! Бот успешно запущен и работает 24/7 без отключений!")

async def main():
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    
    # Запускаем фоновый веб-сервер для Render и пуллинг одновременно
    asyncio.create_task(web_server())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
