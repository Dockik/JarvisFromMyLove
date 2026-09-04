"""Локальный запуск в режиме long polling (без вебхука).

Использование:
    set POLLING=1
    python run_polling.py
"""
import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from bot.db import init_db
from bot.handlers import callbacks, messages, start
from bot.scheduler import setup_scheduler


async def main() -> None:
    await init_db()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_routers(start.router, callbacks.router, messages.router)
    setup_scheduler(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    print("Bot started in polling mode. Ctrl+C to stop.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
