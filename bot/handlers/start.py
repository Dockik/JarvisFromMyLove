from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from ..db import SessionLocal, get_or_create_user
from ..keyboards import MAIN_MENU
from .. import pending

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
        user = await get_or_create_user(
            session, message.from_user.id, message.from_user.username, message.from_user.first_name
        )
    if user.display_name is None:
        pending.NAME_ASK.add(message.chat.id)
        await message.answer(
            WELCOME + "\n\n❓ И сразу вопрос: как к тебе обращаться? Напиши имя одним сообщением.",
            reply_markup=MAIN_MENU,
        )
    else:
        await message.answer(
            f"С возвращением, {user.display_name}! 👋\n\n" + WELCOME,
            reply_markup=MAIN_MENU,
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=MAIN_MENU)
