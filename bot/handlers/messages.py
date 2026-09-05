from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import Message

from ..config import get_tz
from ..db import SessionLocal, get_or_create_user
from ..gemini import ParsedIntent, ParsedMessage, chat_answer, parse_text, parse_voice
from ..keyboards import MAIN_MENU, confirm_card
from ..pending import Pending, put_pending
from ..views import find_for_delete, today_view

log = logging.getLogger(__name__)
router = Router()


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


async def _handle_message(message: Message, parsed: ParsedMessage, raw_text: str, tzname: str) -> None:
    chat_id = message.chat.id
    handled = False

    for item in parsed.items:
        if item.intent in ("add_event", "add_task", "add_goal"):
            key = put_pending(Pending(intent=item, chat_id=chat_id, text=raw_text))
            prefix = {"add_event": "evadd", "add_task": "tkadd", "add_goal": "gladd"}[item.intent]
            preview = _preview(item, tzname)
            await message.answer(preview, reply_markup=confirm_card(prefix, key))
            handled = True
        elif item.intent == "delete":
            await _handle_delete(message, item, raw_text)
            handled = True
        elif item.intent == "query":
            await _handle_query(message, item, raw_text)
            handled = True

    if not handled:
        await _answer_chat(message, parsed, raw_text, tzname)


async def _handle_delete(message: Message, item: ParsedIntent, raw_text: str) -> None:
    title = item.title or raw_text
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        found = await find_for_delete(session, user, title)
    if not found:
        await message.answer(f"Не нашёл «{title}». Попробуйте уточнить название.")
        return
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
    question = raw_text or parsed.transcript or parsed.answer or ""
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        text = await chat_answer(question, tzname)
    except Exception:
        log.exception("Gemini chat failed")
        text = parsed.answer or "Не получилось найти ответ, попробуйте ещё раз 🙏"
    await message.answer(text, reply_markup=MAIN_MENU)


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
    lines.append("\nСохраняем?")
    return "\n".join(lines)


@router.message(F.text)
async def on_text(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        parsed = await parse_text(message.text, user.tz)
    except Exception:
        log.exception("Gemini parse failed")
        await message.answer("Не получилось разобрать сообщение, попробуйте ещё раз 🙏")
        return
    await _handle_message(message, parsed, message.text or "", user.tz)


@router.message(F.voice | F.audio_note)
async def on_voice(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    await message.bot.send_chat_action(message.chat.id, "typing")
    voice = message.voice or message.audio_note
    file = await message.bot.get_file(voice.file_id)
    buf = await message.bot.download_file(file.file_path)
    ogg_bytes = buf.read()
    try:
        parsed = await parse_voice(ogg_bytes, user.tz)
    except Exception:
        log.exception("Gemini voice parse failed")
        await message.answer("Не получилось разобрать голосовое, попробуйте ещё раз 🙏")
        return
    await _handle_message(message, parsed, parsed.transcript or "", user.tz)
