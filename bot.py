import asyncio
import logging
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

TOKEN = "8946831317:AAEgn7tYUp90K3QFrAIwqpTtE5zQAxh_fL0"

async def handle(request):
    return web.Response(text="Bot is running and alive 24/7!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 10000
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

dp = Dispatcher()

@dp.message(Command("start"))
async def command_start_handler(message: types.Message):
    await message.answer("Салам! Бот успешно запущен и работает 24/7 без отключений!")

async def main():
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(web_server())
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
