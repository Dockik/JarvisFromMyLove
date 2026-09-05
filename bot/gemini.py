from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Literal, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel

from .config import get_tz, settings
from . import giga

log = logging.getLogger(__name__)

client = genai.Client(
    api_key=settings.gemini_api_key,
    http_options=types.HttpOptions(timeout=30_000),
)


class ParsedIntent(BaseModel):
    """Одно действие из сообщения."""

    intent: Literal[
        "add_event", "add_task", "add_goal", "query", "delete", "chat",
        "cancel_plans", "reschedule",
    ]
    title: Optional[str] = None
    starts_at: Optional[str] = None  # ISO 8601 с offset (для reschedule — НОВОЕ время)
    due_at: Optional[str] = None
    target_date: Optional[str] = None  # YYYY-MM-DD
    remind_before_minutes: Optional[int] = None
    priority: Optional[Literal["low", "normal", "high"]] = None
    answer: Optional[str] = None  # комментарий к записи / краткий ответ на query
    scope: Optional[Literal["today", "all"]] = None  # для cancel_plans
    target: Optional[Literal["events", "tasks", "goals", "all"]] = None  # что именно чистить
    # Только для intent=chat — чтобы ответить без квоты поиска:
    weather_city: Optional[str] = None  # город вопроса про погоду
    weather_hours: Optional[int] = None  # длительность окна прогноза в часах
    weather_start_hours: Optional[int] = None  # через сколько часов ОТ СЕЙЧАС начинается окно
    currency: Optional[str] = None  # ISO-код валюты, курс которой спрашивают
    currency_base: Optional[str] = None  # в чём считать, по умолчанию RUB


class ParsedMessage(BaseModel):
    """Разбор всего сообщения: может содержать несколько действий сразу."""

    transcript: Optional[str] = None  # расшифровка сообщения (важно для голосовых)
    items: list[ParsedIntent] = []
    answer: Optional[str] = None
    # Контроль арифметики времени: какой «сейчас» использовала модель
    now_assumed: Optional[str] = None


def _system_prompt(user_tz: str) -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    return SYSTEM_PROMPT.format(now=now, tz=user_tz)


def _chat_system_prompt(user_tz: str, internet: bool = True, name: str = "") -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    rule = INTERNET_ON if internet else INTERNET_OFF
    return CHAT_SYSTEM_PROMPT.format(now=now, tz=user_tz, internet_rule=rule, name=name or "друг")


# Живые модели ключа (по списку API): 3.5-flash основная, дальше свежие 3.7
# и облегчённые lite (реже перегружены). 2.5-flash для новых ключей закрыт (404),
# 3.6 стабильно перегружен. При 429/5xx идём к следующей модели; если упала вся
# цепочка — второй круг с паузой.
MODEL_CHAIN = [
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
]
MAX_ROUNDS = 2
RETRY_PAUSE_SEC = 3

HOLD_PROMPT = (
    "Придумай ОДНУ короткую живую фразу (максимум 8 слов) от личного ассистента "
    "для пользователя по имени {name}. Смысл фразы: «взял запрос в работу, уже разбираюсь». "
    "Тёплая, слегка неформальная, можно один эмодзи. Каждый раз разная. "
    "Без кавычек — только сама фраза."
)


def _is_retryable(e: Exception) -> bool:
    return getattr(e, "code", None) in (429, 500, 502, 503, 504)


async def _generate(contents, user_tz: str) -> ParsedMessage:
    last_exc: Exception | None = None
    for round_no in range(MAX_ROUNDS):
        if round_no:
            await asyncio.sleep(RETRY_PAUSE_SEC)
        for model in MODEL_CHAIN:
            try:
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=_system_prompt(user_tz),
                        response_mime_type="application/json",
                        response_schema=ParsedMessage,
                        temperature=0.2,
                    ),
                )
                return _parse(resp)
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if _is_retryable(e):
                    log.warning(
                        "Gemini %s failed (%s), round %s, trying next model",
                        model, getattr(e, "code", None), round_no + 1,
                    )
                    continue
                raise
    assert last_exc is not None
    raise last_exc


async def _gemini_chain(question: str, user_tz: str, use_search: bool, name: str = "") -> str:
    last_exc: Exception | None = None
    for round_no in range(MAX_ROUNDS):
        if round_no:
            await asyncio.sleep(RETRY_PAUSE_SEC)
        for model in MODEL_CHAIN:
            try:
                config = types.GenerateContentConfig(
                    system_instruction=_chat_system_prompt(user_tz, internet=use_search, name=name),
                    temperature=0.4,
                )
                if use_search:
                    config.tools = [types.Tool(google_search=types.GoogleSearch())]
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=question,
                    config=config,
                )
                return (resp.text or "").strip() or "Не нашёл ответа, попробуйте уточнить вопрос 🙏"
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if _is_retryable(e):
                    log.warning(
                        "Gemini chat %s failed (%s), round %s, trying next model",
                        model, getattr(e, "code", None), round_no + 1,
                    )
                    continue
                raise
    assert last_exc is not None
    raise last_exc


async def chat_answer(question: str, user_tz: str, name: str = "") -> str:
    """Свободное общение: GigaChat как живой ассистент, при сбое — Gemini (поиск → без поиска)."""
    if giga.enabled():
        try:
            answer = await giga.chat(
                [
                    {"role": "system", "content": _chat_system_prompt(user_tz, internet=False, name=name)},
                    {"role": "user", "content": question},
                ]
            )
            if answer and answer.strip():
                return answer.strip()
        except Exception:
            log.warning("GigaChat chat failed, fallback to Gemini search", exc_info=True)
    try:
        return await _gemini_chain(question, user_tz, use_search=True, name=name)
    except Exception as e:
        if getattr(e, "code", None) != 429:
            raise
        # Квота поиска исчерпана — пробуем обычную генерацию (у неё отдельная квота)
        log.warning("Gemini search quota exhausted, retrying without search")
        return await _gemini_chain(question, user_tz, use_search=False, name=name)


async def hold_phrase(name: str = "друг") -> str | None:
    """Живая фраза «уже работаю над этим» — генерируется отдельно, чтобы не быть шаблонной."""
    prompt = HOLD_PROMPT.format(name=name or "друг")
    if giga.enabled():
        try:
            text = (await giga.chat(
                [{"role": "user", "content": prompt}], temperature=1.1, max_tokens=60
            )).strip().strip('"«»').strip()
            if text:
                return text
        except Exception:
            log.warning("hold_phrase via GigaChat failed", exc_info=True)
    try:
        resp = await client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=1.0),
        )
        text = (resp.text or "").strip().strip('"«»').strip()
        return text or None
    except Exception:  # noqa: BLE001
        log.warning("hold_phrase failed", exc_info=True)
        return None


def _valid_iso(s: str | None) -> bool:
    if not s:
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _now_str(user_tz: str) -> str:
    return datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")


def _now_skew(parsed: ParsedMessage, user_tz: str) -> float:
    """Насколько модель ошиблась с «сейчас» (в секундах)."""
    if not parsed.now_assumed:
        return 0.0
    try:
        assumed = datetime.fromisoformat(parsed.now_assumed.replace("Z", "+00:00"))
        if assumed.tzinfo is None:
            assumed = assumed.replace(tzinfo=get_tz(user_tz))
        return abs((assumed - datetime.now(get_tz(user_tz))).total_seconds())
    except ValueError:
        return 0.0


async def parse_text(text: str, user_tz: str) -> ParsedMessage:
    # GigaChat — первичные мозги (без квот и лимитов Gemini), Gemini — запасной
    if giga.enabled():
        for attempt in range(2):
            try:
                data = await giga.chat_json(_system_prompt(user_tz), text)
                parsed = ParsedMessage.model_validate(data)
            except Exception:
                log.warning("GigaChat parse attempt %s failed", attempt + 1, exc_info=True)
                continue
            # Проверяем, что все даты — валидный ISO; если нет, просим переделать
            bad = [
                (i, i.starts_at) for i in parsed.items
                if i.intent == "add_event" and not _valid_iso(i.starts_at)
            ] + [
                (i, i.due_at) for i in parsed.items
                if i.intent == "add_task" and i.due_at and not _valid_iso(i.due_at)
            ]
            # Проверяем арифметику времени: модель считала не от того «сейчас»
            skew = _now_skew(parsed, user_tz)
            if not bad and skew < 900:
                return parsed
            if attempt == 1:
                break
            problems = []
            if bad:
                problems.append(
                    "поля с датами не в формате ISO 8601: "
                    + "; ".join(f"«{v}»" for _, v in bad)
                    + " (относительное время («через полтора часа») ВЫЧИСЛИ в абсолютную дату сам)"
                )
            if skew >= 900:
                problems.append(f"ты считала не от того времени: сейчас на самом деле {_now_str(user_tz)}")
            fix_note = (
                "ОШИБКА в твоём прошлом ответе: " + "; ".join(problems)
                + ". Верни JSON заново, все starts_at/due_at — валидный ISO 8601 datetime "
                "с offset таймзоны, пересчитанный от точного текущего времени."
            )
            try:
                data = await giga.chat_json(_system_prompt(user_tz) + "\n" + fix_note, text)
                parsed = ParsedMessage.model_validate(data)
            except Exception:
                log.warning("GigaChat fix attempt failed", exc_info=True)
                break
            if not [
                i for i in parsed.items
                if i.intent == "add_event" and not _valid_iso(i.starts_at)
            ] and _now_skew(parsed, user_tz) < 900:
                return parsed
            break
    return await _generate(text, user_tz)


async def parse_voice(ogg_bytes: bytes, user_tz: str) -> ParsedMessage:
    contents = types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=ogg_bytes, mime_type="audio/ogg"),
            types.Part(text="Расшифруй голосовое сообщение и разбери его по схеме."),
        ],
    )
    return await _generate(contents, user_tz)


def _parse(resp) -> ParsedMessage:
    raw = resp.text or "{}"
    log.info("Gemini raw: %s", raw[:800])
    parsed = ParsedMessage.model_validate(json.loads(raw))
    # Защита от пустого разбора
    if not parsed.items and not parsed.answer:
        parsed.items = [ParsedIntent(intent="chat")]
    return parsed
