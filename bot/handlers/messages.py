from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import Message

from ..config import get_tz
from ..db import SessionLocal, get_or_create_user
from ..gemini import (
    ParsedIntent,
    ParsedMessage,
    chat_answer,
    hold_phrase,
    parse_text,
    parse_voice,
)
from ..keyboards import MAIN_MENU, confirm_card
from ..pending import PendingGroup, chat_groups, pop_group, put_group
from ..views import find_for_delete, save_intent, today_view
from .. import webdata

log = logging.getLogger(__name__)
router = Router()

CONFIRM_WORDS = {
    "да", "давай", "ок", "окей", "ага", "угу", "yes", "y", "+", "верно",
    "именно", "точно", "подтверждаю", "сохраняй", "сохранить", "записывай",
    "запиши", "всё", "все", "давай всё", "согласен", "согласна", "ок давай",
}
CANCEL_WORDS = {
    "нет", "не", "не надо", "не нужно", "не сохраняй", "не записывай",
    "отмена", "отмени", "cancel", "отстань", "забудь", "стоп",
}


def _norm(s: str) -> str:
    return s.strip().lower().strip(" !.,?;:")


def _parse_iso(s: str | None, tzname: str) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_tz(tzname))
    return dt.astimezone(timezone.utc)


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "запись"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "записи"
    return "записей"


def _preview(intent: ParsedIntent, tzname: str) -> str:
    tz = get_tz(tzname)
    emoji = {"add_event": "📌", "add_task": "✅", "add_goal": "🎯"}[intent.intent]
    kind = {"add_event": "Событие", "add_task": "Задача", "add_goal": "Цель"}[intent.intent]
    lines = [f"{emoji} <b>{kind}:</b> {intent.title or '—'}"]
    if intent.intent == "add_event" and intent.starts_at:
        dt = _parse_iso(intent.starts_at, tzname)
        if dt:
            lines.append(f"🕐 Когда: {dt.astimezone(tz).strftime('%d.%m.%Y %H:%M')}")
    if intent.intent == "add_task" and intent.due_at:
        dt = _parse_iso(intent.due_at, tzname)
        if dt:
            lines.append(f"⏳ Дедлайн: {dt.astimezone(tz).strftime('%d.%m.%Y %H:%M')}")
    if intent.intent == "add_goal" and intent.target_date:
        lines.append(f"🏁 Срок: {intent.target_date}")
    if intent.priority and intent.priority != "normal":
        lines.append(f"⚡️ Приоритет: {intent.priority}")
    return "\n".join(lines)


def _preview_group(intents: list[ParsedIntent], tzname: str) -> str:
    cards = [_preview(i, tzname) for i in intents]
    if len(cards) == 1:
        return cards[0] + "\n\nСохраняем?"
    head = f"Понял вас, у меня {len(cards)} {_plural(len(cards))}:"
    return head + "\n\n" + "\n\n".join(cards) + "\n\nСохраняем всё?"


async def _handle_message(message: Message, parsed: ParsedMessage, raw_text: str, tzname: str) -> None:
    chat_id = message.chat.id
    adds = [i for i in parsed.items if i.intent in ("add_event", "add_task", "add_goal")]
    if adds:
        key = put_group(PendingGroup(intents=adds, chat_id=chat_id, text=raw_text))
        await message.answer(_preview_group(adds, tzname), reply_markup=confirm_card("grp", key))
        return

    for item in parsed.items:
        if item.intent == "delete":
            await _handle_delete(message, item, raw_text)
            return
        if item.intent == "query":
            await _handle_query(message, item, raw_text)
            return

    await _answer_chat(message, parsed, raw_text, tzname)


async def _handle_delete(message: Message, item: ParsedIntent, raw_text: str) -> None:
    title = item.title or raw_text
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        found = await find_for_delete(session, user, title)
    if not found:
        await message.answer(f"Не нашёл «{title}». Попробуйте уточнить название.")
        return
    from ..keyboards import event_actions, goal_actions, task_actions

    kb_map = {"ev": event_actions, "tk": task_actions, "gl": goal_actions}
    for kind, obj_id, label in found[:5]:
        await message.answer(f"🗑 {label}", reply_markup=kb_map[kind](obj_id))


async def _handle_query(message: Message, item: ParsedIntent, raw_text: str) -> None:
    low = raw_text.lower()
    if any(w in low for w in ("сегодня", "день", "расписание", "план")):
        async with SessionLocal() as session:
            user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
            await message.answer(await today_view(session, user), reply_markup=MAIN_MENU)
    else:
        await message.answer(item.answer or "Не совсем понял вопрос, уточните?", reply_markup=MAIN_MENU)


async def _answer_chat(message: Message, parsed: ParsedMessage, raw_text: str, tzname: str) -> None:
    chat_item = next((i for i in parsed.items if i.intent == "chat"), None)

    # Погода и курсы — через бесплатные API, без расхода квоты Gemini
    try:
        if chat_item and chat_item.weather_city:
            text = await webdata.get_weather(
                chat_item.weather_city,
                chat_item.weather_hours or 3,
                chat_item.weather_start_hours or 0,
            )
            await message.answer(text, reply_markup=MAIN_MENU)
            return
        if chat_item and chat_item.currency:
            text = await webdata.get_rate(chat_item.currency, chat_item.currency_base or "RUB")
            await message.answer(text, reply_markup=MAIN_MENU)
            return
    except Exception:
        log.exception("Weather/rate API failed")
        text = "Не смог получить данные, попробуйте ещё раз 🙏"
        await message.answer(text, reply_markup=MAIN_MENU)
        return

    question = raw_text or parsed.transcript or parsed.answer or ""
    hold = asyncio.create_task(hold_phrase())
    answer_task = asyncio.create_task(chat_answer(question, tzname))
    await message.bot.send_chat_action(message.chat.id, "typing")
    await asyncio.wait([hold, answer_task], return_when=asyncio.FIRST_COMPLETED)
    if not answer_task.done() and hold.done() and hold.exception() is None and hold.result():
        await message.answer(hold.result())
        await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        text = await answer_task
    except Exception as e:
        log.exception("Gemini chat failed")
        if getattr(e, "code", None) == 429:
            text = (
                "Дневной лимит поисковых ответов на сегодня исчерпан 😔 "
                "Погоду и курсы валют я всё равно подскажу — просто спросите. "
                "Планирование работает как обычно."
            )
        else:
            text = parsed.answer or "Не получилось найти ответ, попробуйте ещё раз 🙏"
    await message.answer(text, reply_markup=MAIN_MENU)


async def _confirm_pending(message: Message) -> None:
    groups = chat_groups(message.chat.id)
    if not groups:
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        lines: list[str] = []
        for key, group in groups:
            for intent in group.intents:
                lines.append(await save_intent(session, user, intent))
            pop_group(key)
    await message.answer("\n".join(lines), reply_markup=MAIN_MENU)


async def _cancel_pending(message: Message) -> None:
    for key, _ in chat_groups(message.chat.id):
        pop_group(key)
    await message.answer("Отменил, ничего не сохранял 🚫")


@router.message(F.text)
async def on_text(message: Message) -> None:
    text = message.text or ""
    low = _norm(text)

    # Быстрое подтверждение/отмена карточек одним словом
    if low in CONFIRM_WORDS or low in CANCEL_WORDS:
        if chat_groups(message.chat.id):
            if low in CONFIRM_WORDS:
                await _confirm_pending(message)
            else:
                await _cancel_pending(message)
            return

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

    hold = asyncio.create_task(hold_phrase())
    parse_task = asyncio.create_task(parse_text(text, user.tz))
    await message.bot.send_chat_action(message.chat.id, "typing")
    await asyncio.wait([hold, parse_task], return_when=asyncio.FIRST_COMPLETED)
    if not parse_task.done() and hold.done() and hold.exception() is None and hold.result():
        await message.answer(hold.result())
        await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        parsed = await parse_task
    except Exception:
        log.exception("Gemini parse failed")
        await message.answer("Не получилось разобрать сообщение, попробуйте ещё раз 🙏")
        return
    await _handle_message(message, parsed, text, user.tz)


@router.message(F.voice | F.audio_note)
async def on_voice(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    voice = message.voice or message.audio_note
    file = await message.bot.get_file(voice.file_id)
    buf = await message.bot.download_file(file.file_path)
    ogg_bytes = buf.read()

    hold = asyncio.create_task(hold_phrase())
    parse_task = asyncio.create_task(parse_voice(ogg_bytes, user.tz))
    await message.bot.send_chat_action(message.chat.id, "typing")
    await asyncio.wait([hold, parse_task], return_when=asyncio.FIRST_COMPLETED)
    if not parse_task.done() and hold.done() and hold.exception() is None and hold.result():
        await message.answer(hold.result())
        await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        parsed = await parse_task
    except Exception:
        log.exception("Gemini voice parse failed")
        await message.answer("Не получилось разобрать голосовое, попробуйте ещё раз 🙏")
        return
    await _handle_message(message, parsed, parsed.transcript or "", user.tz)
