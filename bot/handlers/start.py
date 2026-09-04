from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..db import SessionLocal, get_or_create_user
from ..keyboards import MAIN_MENU

router = Router()

WELCOME = (
    "👋 Привет! Я твой личный ИИ-ассистент.\n\n"
    "Просто пиши мне текстом или голосом:\n"
    '• «Встреча с врачом завтра в 15:00»\n'
    "• «Купить продукты до вечера»\n"
    "• «Цель — выучить английский до июня»\n"
    "• «Что у меня на сегодня?»\n\n"
    "Я напомню о событии заранее и пришлю список дел утром.\n"
    "Утренний дайджест по умолчанию в 07:00 (настраивается в ⚙️ Настройках)."
)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        await get_or_create_user(session, message.from_user.id, message.from_user.username)
    await message.answer(WELCOME, reply_markup=MAIN_MENU)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=MAIN_MENU)
