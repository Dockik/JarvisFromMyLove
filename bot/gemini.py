from __future__ import annotations

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
MODEL = "gemini-3.6-flash"


class ParsedIntent(BaseModel):
    intent: Literal[
        "add_event", "add_task", "add_goal", "query", "delete", "unknown"
    ]
    title: Optional[str] = None
    starts_at: Optional[str] = None  # ISO 8601 с offset
    due_at: Optional[str] = None
    target_date: Optional[str] = None  # YYYY-MM-DD
    duration_minutes: Optional[int] = None
    remind_before_minutes: Optional[int] = None
    priority: Optional[Literal["low", "normal", "high"]] = None
    answer: Optional[str] = None  # короткий ответ на query/unknown

SYSTEM_PROMPT = """Ты — парсер намерений личного ассистента. Пользователь пишет по-русски \
текстом или голосом. Твоя задача — вернуть ТОЛЬКО JSON по схеме.

Правила:
- intent=add_event: встреча/звонок/поездка/мероприятие с конкретным временем. Заполни title и starts_at (ISO 8601 с offset таймзоны пользователя). Если время не указано — intent=unknown.
- intent=add_task: дело/задача без привязки к точному времени или с дедлайном. Заполни title, при необходимости due_at (ISO 8601), priority.
- intent=add_goal: долгосрочная цель. title, при необходимости target_date (YYYY-MM-DD).
- intent=query: вопрос про расписание/задачи/цели. Кратко ответь в поле answer на русском.
- intent=delete: удалить/отменить/выполнить что-то существующее. title — что именно удалить/выполнить. Если "выполнить/сделал(а)" — это тоже delete (пометка как выполненное).
- intent=unknown: приветствия, болтовня, запросы не про планирование. Кратко ответь в answer.

Текущие дата и время: {now}. Таймзона пользователя: {tz}.
Относительные даты ("завтра", "в пятницу", "через час") вычисляй от текущего времени.
Если напоминание явно не указано, remind_before_minutes=60 для событий.
Поле answer заполняй всегда — короткая фраза-подтверждение, что распознано."""


def _system_prompt(user_tz: str) -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    return SYSTEM_PROMPT.format(now=now, tz=user_tz)


def _config(user_tz: str) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=_system_prompt(user_tz),
        response_mime_type="application/json",
        response_schema=ParsedIntent,
        temperature=0.2,
    )


# Живые модели ключа (по списку API): 3.5-flash основная, дальше свежие 3.7/3.8
# и облегчённая 3.5-lite. 2.5-flash для новых ключей закрыт (404), 3.6 сегодня
# стабильно перегружен. При 429/5xx пробуем следующую модель.
MODEL_CHAIN = [
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]


async def _generate(contents, user_tz: str) -> ParsedIntent:
    last_exc: Exception | None = None
    for model in MODEL_CHAIN:
        try:
            resp = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=_config(user_tz),
            )
            return _parse(resp)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            last_exc = e
            if code in (429, 500, 502, 503, 504):
                log.warning("Gemini %s failed (%s), trying next model", model, code)
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def parse_text(text: str, user_tz: str) -> ParsedIntent:
    return await _generate(text, user_tz)


async def parse_voice(ogg_bytes: bytes, user_tz: str) -> ParsedIntent:
    contents = types.Content(
        role="user",
        parts=[
            types.Part.from_bytes(data=ogg_bytes, mime_type="audio/ogg"),
            types.Part(text="Расшифруй голосовое сообщение и разбери его как намерение."),
        ],
    )
    return await _generate(contents, user_tz)


def _parse(resp) -> ParsedIntent:
    raw = resp.text or "{}"
    log.info("Gemini raw: %s", raw[:500])
    return ParsedIntent.model_validate(json.loads(raw))
