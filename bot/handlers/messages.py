from __future__ import annotations

import asyncio
import logging
import re
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
from ..keyboards import confirm_card, view_footer
from ..pending import NAME_ASK, PLAN_ASK, PendingGroup, chat_groups, pop_group, put_group
from ..views import cancel_plans, find_for_delete, goals_view, reschedule, save_intent, tasks_view, today_view
from .. import giga, webdata, pending

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


async def _handle_message(message: Message, parsed: ParsedMessage, raw_text: str, tzname: str, hold_sent: bool = False) -> None:
    chat_id = message.chat.id
    adds = [i for i in parsed.items if i.intent in ("add_event", "add_task", "add_goal")]
    if adds:
        key = put_group(PendingGroup(intents=adds, chat_id=chat_id, text=raw_text))
        await message.answer(_preview_group(adds, tzname), reply_markup=confirm_card("grp", key))
        return

    for item in parsed.items:
        if item.intent == "delete":
            bulk = _bulk_delete_target(item, raw_text)
            if bulk:
                await _handle_cancel_plans(message, item, target_override=bulk)
                return
            await _handle_delete(message, item, raw_text)
            return
        if item.intent == "cancel_plans":
            await _handle_cancel_plans(message, item)
            return
        if item.intent == "reschedule":
            await _handle_reschedule(message, item, raw_text, tzname)
            return
        if item.intent == "query":
            await _handle_query(message, item, raw_text)
            return

    # Страховка: «какие у меня цели / покажи задачи» — если парсер принял это за chat
    kind = _list_request_kind(raw_text)
    if kind:
        await _send_list(message, kind)
        return

    await _answer_chat(message, parsed, raw_text, tzname, hold_sent)


LIST_ADD_WORDS = (
    "запиши", "занеси", "добавь", "создай", "поставь", "новую цель", "новая цель",
    "новую задачу", "хочу",
)
LIST_CUES = ("какие", "покажи", "список", "что у меня", "мои", "все мои", "есть ли")


def _list_request_kind(text: str) -> str | None:
    """«Какие у меня цели» / «покажи задачи» → 'goals' | 'tasks', иначе None."""
    low = text.lower()
    if any(w in low for w in LIST_ADD_WORDS):
        return None
    if not any(w in low for w in LIST_CUES):
        return None
    if re.search(r"цел", low):
        return "goals"
    if re.search(r"задач", low):
        return "tasks"
    return None


BULK_GENERIC_TITLES = {
    "", "все", "всё", "все цели", "все мои цели", "все задачи", "все мои задачи",
    "все события", "все планы", "все мои планы", "все записи", "все дела",
    "все мои дела", "все напоминания", "всё расписание", "все расписание",
}


def _bulk_target(text: str) -> str | None:
    """«Удали все мои цели» / «очисти всё» → goals|tasks|events|all."""
    low = text.lower()
    has_goal = re.search(r"цел", low)
    has_task = re.search(r"задач|дел[аи]\b", low)
    has_event = re.search(r"событ|план|напоминан|запис", low)
    if has_goal and (has_task or has_event):
        return "all"
    if has_goal:
        return "goals"
    if has_task:
        return "tasks"
    if has_event:
        return "events"
    if re.search(r"\bвс[её]\b", low):
        return "all"
    return None


def _bulk_delete_target(item: ParsedIntent, raw_text: str) -> str | None:
    """Массовое удаление через intent=delete, если без конкретного названия."""
    title = (item.title or "").lower().strip()
    if title and title not in BULK_GENERIC_TITLES:
        return None
    return _bulk_target(raw_text or title)


async def _send_list(message: Message, kind: str) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        if kind == "goals":
            text, kb = await goals_view(session, user)
            await message.answer(text, reply_markup=kb or view_footer())
        else:
            text = await tasks_view(session, user)
            await message.answer(text, reply_markup=view_footer())


WD_MAP = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def _parse_plan_reply(text: str) -> tuple[int | None, set[int] | None, str | None]:
    """«на 2 недели, пн-пт, в 20:00» → (14, {0..4}, "20:00")."""
    low = text.lower()
    days: int | None = None
    m = re.search(r"(\d+)\s*недел", low)
    if "месяц" in low:
        days = 30
    elif m:
        days = min(60, max(1, int(m.group(1)) * 7))
    elif re.search(r"недел", low):
        days = 7
    else:
        m2 = re.search(r"(\d+)\s*дн", low)
        if m2:
            days = min(60, max(1, int(m2.group(1))))

    weekdays: set[int] | None = None
    rng = re.search(r"\b(пн|вт|ср|чт|пт|сб|вс)\s*[-–—]\s*(пн|вт|ср|чт|пт|сб|вс)\b", low)
    if rng:
        a, b = WD_MAP[rng.group(1)], WD_MAP[rng.group(2)]
        weekdays = set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
    else:
        found = {WD_MAP[w] for w in re.findall(r"\b(пн|вт|ср|чт|пт|сб|вс)\b", low)}
        if found and len(found) < 7:
            weekdays = found

    sub_time: str | None = None
    mt = re.search(r"(\d{1,2})[:.](\d{2})", low)
    if mt:
        h, mm = int(mt.group(1)), int(mt.group(2))
        if 0 <= h <= 23 and 0 <= mm <= 59:
            sub_time = f"{h:02d}:{mm:02d}"
    else:
        mt = re.search(r"\bв\s+(\d{1,2})\b", low)
        if mt and 0 <= int(mt.group(1)) <= 23:
            sub_time = f"{int(mt.group(1)):02d}:00"
    return days, weekdays, sub_time


async def _handle_plan_reply(message: Message, user, items: list[tuple[int, str]], text: str) -> None:
    days, weekdays, sub_time = _parse_plan_reply(text)
    if days is None:
        PLAN_ASK[message.chat.id] = items
        await message.answer(
            "Не понял срок 🤔 Напиши, например: «на неделю», "
            "«на 2 недели, пн-пт» или «на месяц, в 20:00»."
        )
        return
    from .callbacks import _generate_plans_for

    await _generate_plans_for(message, items, days, weekdays, sub_time)


async def _handle_cancel_plans(message: Message, item: ParsedIntent, target_override: str | None = None) -> None:
    scope = item.scope or "all"
    target = target_override or item.target or "all"
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        text = await cancel_plans(session, user, scope, target)
    await message.answer(text, reply_markup=view_footer())


async def _handle_reschedule(message: Message, item: ParsedIntent, raw_text: str, tzname: str) -> None:
    new_dt = _parse_iso(item.starts_at, tzname)
    if new_dt is None:
        await message.answer("Не распознал, на какое время перенести. Напишите, например: «перенеси на завтра в 15:00».")
        return
    title = item.title or raw_text
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        text = await reschedule(session, user, title, new_dt)
    await message.answer(text, reply_markup=view_footer())


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
    kind = _list_request_kind(raw_text)
    if kind in ("goals", "tasks"):
        await _send_list(message, kind)
        return
    if any(w in low for w in ("сегодня", "день", "расписание", "план")):
        async with SessionLocal() as session:
            user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
            await message.answer(await today_view(session, user), reply_markup=view_footer())
    else:
        await message.answer(item.answer or "Не совсем понял вопрос, уточните?", reply_markup=view_footer())


async def _race_hold(hold: asyncio.Task, main: asyncio.Task, message: Message) -> bool:
    """Шлёт фразу-ожидание, только если основной ответ ещё не готов. Максимум ОДНА фраза на сообщение."""
    await message.bot.send_chat_action(message.chat.id, "typing")
    await asyncio.wait([hold, main], return_when=asyncio.FIRST_COMPLETED)
    if not main.done() and hold.done() and hold.exception() is None and hold.result():
        await message.answer(hold.result())
        await message.bot.send_chat_action(message.chat.id, "typing")
        return True
    if main.done() and not hold.done():
        hold.cancel()
    return False


async def _answer_chat(message: Message, parsed: ParsedMessage, raw_text: str, tzname: str, hold_sent: bool = False) -> None:
    chat_item = next((i for i in parsed.items if i.intent == "chat"), None)

    async with SessionLocal() as session:
        u = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    name = u.display_name or message.from_user.first_name or "друг"

    # Погода и курсы — через бесплатные API, без расхода квоты Gemini
    try:
        if chat_item and chat_item.weather_city:
            text = await webdata.get_weather(
                chat_item.weather_city,
                chat_item.weather_hours or 3,
                chat_item.weather_start_hours or 0,
            )
            await message.answer(text, reply_markup=view_footer())
            return
        if chat_item and chat_item.currency:
            text = await webdata.get_rate(chat_item.currency, chat_item.currency_base or "RUB")
            await message.answer(text, reply_markup=view_footer())
            return
    except Exception:
        log.exception("Weather/rate API failed")
        text = "Не смог получить данные, попробуйте ещё раз 🙏"
        await message.answer(text, reply_markup=view_footer())
        return

    question = raw_text or parsed.transcript or parsed.answer or ""

    async def _answer():
        return await chat_answer(question, tzname, name)

    answer_task = asyncio.create_task(_answer())
    if hold_sent:
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
        await message.answer(text, reply_markup=view_footer())
        return

    hold = asyncio.create_task(hold_phrase(name))
    await _race_hold(hold, answer_task, message)
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
    await message.answer(text, reply_markup=view_footer())


async def _confirm_pending(message: Message) -> None:
    groups = chat_groups(message.chat.id)
    if not groups:
        return
    created: list = []
    contexts: list[str] = []
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        lines: list[str] = []
        for key, group in groups:
            for intent in group.intents:
                lines.append(await save_intent(session, user, intent, created_goals=created))
            contexts.append(group.text)
            pop_group(key)
    await message.answer("\n".join(lines), reply_markup=view_footer())
    from .callbacks import _spawn_goal_plans

    await _spawn_goal_plans(message, created, " ".join(contexts))


async def _cancel_pending(message: Message) -> None:
    for key, _ in chat_groups(message.chat.id):
        pop_group(key)
    await message.answer("Отменил, ничего не сохранял 🚫")


@router.message(F.text)
async def on_text(message: Message) -> None:
    text = message.text or ""
    low = _norm(text)
    chat_id = message.chat.id

    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)

    # Бот спросил имя — следующий ответ и есть имя
    if chat_id in NAME_ASK and not text.startswith("/"):
        NAME_ASK.discard(chat_id)
        name = text.strip().strip("«»\"'")[:40]
        if name:
            async with SessionLocal() as session:
                u = await get_or_create_user(session, message.from_user.id, message.from_user.username)
                u.display_name = name
                await session.commit()
            await message.answer(
                f"Приятно познакомиться, {name}! 👋 Так и буду к тебе обращаться.",
                reply_markup=view_footer(),
            )
            return

    # Бот ждёт срок плана по целям
    if chat_id in PLAN_ASK and not text.startswith("/"):
        items = PLAN_ASK.pop(chat_id)
        await _handle_plan_reply(message, user, items, text)
        return

    # Быстрое подтверждение/отмена карточек одним словом
    if low in CONFIRM_WORDS or low in CANCEL_WORDS:
        if chat_groups(chat_id):
            if low in CONFIRM_WORDS:
                await _confirm_pending(message)
            else:
                await _cancel_pending(message)
            return

    name = user.display_name or message.from_user.first_name or "друг"
    hold = asyncio.create_task(hold_phrase(name))
    parse_task = asyncio.create_task(parse_text(text, user.tz))
    hold_sent = await _race_hold(hold, parse_task, message)
    try:
        parsed = await parse_task
    except Exception:
        log.exception("Gemini parse failed")
        await message.answer("Не получилось разобрать сообщение, попробуйте ещё раз 🙏")
        return
    await _handle_message(message, parsed, text, user.tz, hold_sent)


@router.message(F.voice | F.audio_note)
async def on_voice(message: Message) -> None:
    async with SessionLocal() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
    voice = message.voice or message.audio_note
    file = await message.bot.get_file(voice.file_id)
    buf = await message.bot.download_file(file.file_path)
    ogg_bytes = buf.read()

    async def _voice_parsed():
        # 1) GigaChat: транскрипция + разбор (если у ключа есть доступ к аудио)
        if giga.enabled():
            try:
                wav = await asyncio.to_thread(giga.ogg_to_wav, ogg_bytes)
                text = await giga.transcribe(wav)
                if text:
                    return await parse_text(text, user.tz), text
            except Exception:
                log.warning("GigaChat voice path failed, fallback to Gemini", exc_info=True)
        # 2) Gemini: расшифровка + разбор одним запросом
        return await parse_voice(ogg_bytes, user.tz), ""

    hold = asyncio.create_task(hold_phrase(user.display_name or message.from_user.first_name or "друг"))
    parse_task = asyncio.create_task(_voice_parsed())
    hold_sent = await _race_hold(hold, parse_task, message)
    try:
        parsed, transcript = await parse_task
    except Exception:
        log.exception("Gemini voice parse failed")
        await message.answer("Не получилось разобрать голосовое, попробуйте ещё раз 🙏")
        return
    await _handle_message(message, parsed, transcript or parsed.transcript or "", user.tz, hold_sent)
