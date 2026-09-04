from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request

from .config import settings
from .db import init_db
from .handlers import callbacks, messages, start
from .scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
dp.include_routers(start.router, callbacks.router, messages.router)

app = FastAPI()
scheduler = None


@app.on_event("startup")
async def on_startup() -> None:
    global scheduler
    await init_db()
    scheduler = setup_scheduler(bot)
    if settings.base_url:
        await bot.set_webhook(
            settings.webhook_url,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=True,
        )
        log.info("Webhook set: %s", settings.webhook_url)
    else:
        log.warning("BASE_URL is empty — webhook not set, use polling mode")
    await bot.delete_my_commands()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if scheduler:
        scheduler.shutdown(wait=False)
    await bot.session.close()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post(settings.webhook_path)
async def webhook(request: Request) -> dict:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if settings.base_url and secret != settings.webhook_secret:
        raise HTTPException(status_code=403, detail="Bad secret")
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
