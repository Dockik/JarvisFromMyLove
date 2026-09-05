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

log = logging.getLogger(__name__)

client = genai.Client(
    api_key=settings.gemini_api_key,
    http_options=types.HttpOptions(timeout=30_000),
)


class ParsedIntent(BaseModel):
    """Одно действие из сообщения."""

    intent: Literal["add_event", "add_task", "add_goal", "query", "delete", "chat"]
    title: Optional[str] = None
    starts_at: Optional[str] = None  # ISO 8601 с offset
    due_at: Optional[str] = None
    target_date: Optional[str] = None  # YYYY-MM-DD
    remind_before_minutes: Optional[int] = None
    priority: Optional[Literal["low", "normal", "high"]] = None
    answer: Optional[str] = None  # комментарий к записи / краткий ответ на query
    # Только для intent=chat — чтобы ответить без квоты поиска:
    weather_city: Optional[str] = None  # город вопроса про погоду
    weather_hours: Optional[int] = None  # на сколько часов вперёд
    currency: Optional[str] = None  # ISO-код валюты, курс которой спрашивают
    currency_base: Optional[str] = None  # в чём считать, по умолчанию RUB


class ParsedMessage(BaseModel):
    """Разбор всего сообщения: может содержать несколько действий сразу."""

    transcript: Optional[str] = None  # расшифровка сообщения (важно для голосовых)
    items: list[ParsedIntent] = []
    answer: Optional[str] = None


SYSTEM_PROMPT = """Ты — мозг личного ассистента «Джарвис» в Telegram. Пользователь пишет по-русски \
текстом или голосом. Верни ТОЛЬКО JSON по схеме:
- transcript: что сказал пользователь своими словами (для текста — сам текст).
- items: список действий, 0 / 1 или несколько, если в сообщении несколько просьб.
- answer: короткая реплика-подтверждение для пользователя.

Действия (поле intent каждого item):
- intent=add_event: напоминание или событие с КОНКРЕТНЫМ временем («через час», «завтра в 15:00», \
«в пятницу вечером», позвонить, встретиться). title, starts_at (ISO 8601 с offset таймзоны). \
remind_before_minutes: 60 по умолчанию; если сказано «напомни за X минут» — X.
- intent=add_task: дело без точного времени (купить, отправить, прочитать). title, при дедлайне due_at (ISO 8601), priority.
- intent=add_goal: ДОЛГОСРОЧНАЯ цель — недели и месяцы («выучить испанский к лету», «накопить к Новому году»). \
title, target_date (YYYY-MM-DD).
- intent=query: вопрос про СВОЁ расписание/задачи/цели («что у меня завтра?»). Кратко ответь в answer.
- intent=delete: удалить/отменить/выполнить что-то существующее. title — что именно. \
«я сделал(а) X» — это тоже delete (пометка выполненным).
- intent=chat: любой запрос не про планирование — погода, курс валют, новости, общий вопрос, болтовня, \
благодарность. В answer ничего не пиши — ассистент ответит отдельным шагом.
  * Если спросили про ПОГОДУ: заполни weather_city (если город не назван — «Москва») и weather_hours \
(на сколько часов, по умолчанию 3, максимум 24). Ответ придёт из метео-API.
  * Если спросили про КУРС ВАЛЮТ: заполни currency (ISO-код, например USD, EUR) и currency_base \
(в чём считать, по умолчанию RUB). Ответ придёт из API курсов.

Важно:
- Одно сообщение может нести несколько просьб: «напомни через час позвонить маме, и запиши, \
что хочу выучить испанский к лету» → два items: add_event (позвонить маме) и add_goal (испанский).
- «напомнить через 1 час 5 минут написать любимой» → add_event, starts_at = текущее время + 1 ч 5 мин.
- Событие/напоминание — всегда конкретный момент времени; цель — растянутый срок (к лету, за месяц).
- Относительные даты вычисляй от текущего времени: {now}. Таймзона пользователя: {tz}.
- Если ничего распознать не удалось — верни один item с intent=chat и пустым остальным."""

CHAT_SYSTEM_PROMPT = """Ты — Джарвис, личный ассистент пользователя в Telegram. Отвечай по-русски: \
дружелюбно, по делу и кратко (1–5 предложений), без таблиц и заголовков. \
Для погоды, курсов валют, новостей, расписаний и любых актуальных данных обязательно \
используй поиск Google и опирайся на свежие результаты. Если спросили про погоду — \
напиши условия и температуру по часам на запрошенный период, будет ли нужен зонт. \
Текущие дата и время: {now}. Таймзона пользователя: {tz}."""


def _system_prompt(user_tz: str) -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    return SYSTEM_PROMPT.format(now=now, tz=user_tz)


def _chat_system_prompt(user_tz: str) -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    return CHAT_SYSTEM_PROMPT.format(now=now, tz=user_tz)


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

OWNER_NAME = "Данил"

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


async def chat_answer(question: str, user_tz: str) -> str:
    """Ответ на свободный вопрос с поиском Google (погода, курсы и т.п.)."""
    last_exc: Exception | None = None
    for round_no in range(MAX_ROUNDS):
        if round_no:
            await asyncio.sleep(RETRY_PAUSE_SEC)
        for model in MODEL_CHAIN:
            try:
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=question,
                    config=types.GenerateContentConfig(
                        system_instruction=_chat_system_prompt(user_tz),
                        tools=[types.Tool(google_search=types.GoogleSearch())],
                        temperature=0.4,
                    ),
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


async def hold_phrase() -> str | None:
    """Живая фраза «уже работаю над этим» — генерируется отдельно, чтобы не быть шаблонной."""
    try:
        resp = await client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=HOLD_PROMPT.format(name=OWNER_NAME),
            config=types.GenerateContentConfig(temperature=1.0),
        )
        text = (resp.text or "").strip().strip('"«»').strip()
        return text or None
    except Exception:  # noqa: BLE001
        log.warning("hold_phrase failed", exc_info=True)
        return None


async def parse_text(text: str, user_tz: str) -> ParsedMessage:
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
