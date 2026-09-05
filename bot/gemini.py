from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
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


class SubtaskPlan(BaseModel):
    title: str
    weekday: int
    time: str = "18:00"


class GoalPlan(BaseModel):
    plan: str
    subtasks: list[SubtaskPlan] = []


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


PLAN_RULES = """Ты — личный коуч-ассистент. Пользователь поставил цель. Составь КОНКРЕТНЫЙ план \
на ближайшую неделю — готовый к исполнению «из коробки», а не общие советы.

Жёсткие правила:
- Используй КАЖДУЮ деталь из контекста пользователя (цифры, ограничения, предпочтения) — это требования, а не фон.
- Каждый пункт — конкретное действие: не «сбалансированное меню на 1500 ккал», а \
«Пн: завтрак — омлет из 3 яиц + овсянка 60 г (~450 ккал); обед — куриная грудка 150 г + гречка + овощи (~550 ккал)». \
Не «занимайся языком», а «Пн: урок 1 — 50 базовых слов, 40 минут».
- План ПО ДНЯМ недели: «Пн: …», «Вт: …» … Для диет — меню по приёмам пищи с блюдами и калориями; \
для обучения — темы и длительность; для спорта — упражнения, подходы, минуты.
- 6–12 строк. Без Markdown-заголовков и без общих фраз («следите за питанием», «регулярно занимайтесь»).

Цель: {title}
Дедлайн: {target}
Контекст пользователя: {context}
Текущая дата: {now}, таймзона: {tz}. Планируй дни, которые ещё впереди на этой неделе."""

# Для GigaChat: план обычным текстом (строгий JSON у GigaChat нестабилен)
GIGA_PLAN_TEXT_PROMPT = PLAN_RULES + "\n\nВерни ТОЛЬКО сам текст плана, без JSON и без пояснений."

GIGA_SUBTASKS_PROMPT = """Выбери из недельного плана 2-5 напоминаний. НЕ копируй строки плана — \
придумай короткое действие-напоминание: например «Приготовить ужин по плану», \
«Закупиться продуктами на неделю», «Взвеситься и записать вес», «Урок по плану: 40 минут».
Верни ТОЛЬКО валидный JSON без текста вокруг: {{"subtasks": [{{"weekday": 0, "time": "19:00", "title": "..."}}]}}, \
где weekday: 0=Пн..6=Вс, time "ЧЧ:ММ" (8:00-21:00). НЕ БОЛЬШЕ ОДНОЙ подзадачи на день.
План:
{plan}
Сегодня: {now}, таймзона: {tz}. Бери только дни, которые ещё впереди на этой неделе."""

# Для Gemini: тот же план, но сразу со схемой JSON
GOAL_PLAN_PROMPT = PLAN_RULES + """
Верни JSON: {{"plan": "текст плана", "subtasks": [{{"weekday": 0, "time": "19:00", "title": "короткое конкретное действие"}}]}}."""


async def generate_goal_plan(
    title: str, context: str, target_date: date | None, user_tz: str
) -> "GoalPlan":
    fmt = dict(
        title=title,
        target=target_date.strftime("%d.%m.%Y") if target_date else "не указан",
        context=context or "нет",
        now=_now_str(user_tz),
        tz=user_tz,
    )
    if giga.enabled():
        try:
            plan_text = (
                await giga.chat(
                    [
                        {"role": "system", "content": GIGA_PLAN_TEXT_PROMPT.format(**fmt)},
                        {"role": "user", "content": "Составь план недели."},
                    ],
                    max_tokens=1500,
                )
            ).strip()
            if plan_text:
                sub_data = await giga.chat_json(
                    GIGA_SUBTASKS_PROMPT.format(
                        plan=plan_text[:3000], now=fmt["now"], tz=user_tz
                    ),
                    "Извлеки подзадачи из плана.",
                    max_tokens=700,
                )
                parsed = GoalPlan(
                    plan=plan_text,
                    subtasks=[SubtaskPlan.model_validate(s) for s in sub_data.get("subtasks", [])],
                )
                return _sanitize_plan(parsed, user_tz)
        except Exception:
            log.warning("GigaChat goal plan failed, fallback to Gemini", exc_info=True)
    user_msg = GOAL_PLAN_PROMPT.format(**fmt)
    last_exc: Exception | None = None
    for model in MODEL_CHAIN:
        try:
            resp = await client.aio.models.generate_content(
                model=model,
                contents=user_msg,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GoalPlan,
                    temperature=0.4,
                ),
            )
            return _sanitize_plan(
                GoalPlan.model_validate(json.loads(resp.text or "{}")), user_tz
            )
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if _is_retryable(e):
                log.warning("Gemini plan %s failed (%s)", model, getattr(e, "code", None))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def _sanitize_plan(parsed: "GoalPlan", user_tz: str) -> "GoalPlan":
    """Нормализует подзадачи: валидные weekday/time, только будущие дни недели."""
    tz = get_tz(user_tz)
    now = datetime.now(tz)
    out = []
    for s in parsed.subtasks:
        wd = s.weekday if 0 <= s.weekday <= 6 else 0
        t = s.time or "18:00"
        try:
            h, m = map(int, t.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            t = f"{h:02d}:{m:02d}"
        except ValueError:
            t = "18:00"
        # Пропускаем дни недели, которые на этой неделе уже прошли (после текущего времени)
        days_ahead = (wd - now.weekday()) % 7
        if days_ahead == 0:
            try:
                hh, mm = map(int, t.split(":"))
                if (hh, mm) <= (now.hour, now.minute):
                    continue
            except ValueError:
                pass
        elif days_ahead > 6:
            continue
        if s.title and s.title.strip():
            out.append(SubtaskPlan(title=s.title.strip()[:200], weekday=wd, time=t))
    # Не больше одной подзадачи на день
    seen: set[int] = set()
    out = [s for s in out if s.weekday not in seen and not seen.add(s.weekday)]
    return GoalPlan(plan=parsed.plan or "", subtasks=out)


SYSTEM_PROMPT = """Ты — мозг личного ассистента «Джарвис» в Telegram. Пользователь пишет по-русски \
текстом или голосом. Верни ТОЛЬКО JSON по схеме:
- transcript: что сказал пользователь своими словами (для текста — сам текст).
- items: список действий, 0 / 1 или несколько, если в сообщении несколько просьб.
- answer: короткая реплика-подтверждение для пользователя.
- now_assumed: текущий момент, ОТ КОТОРОГО ты считал даты — дословно из строки «Текущее время» ниже.

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
- intent=cancel_plans: МАССОВОЕ удаление/очистка/отмена — «очисти все мои цели, задачи и события», \
«удали все мои цели», «сотри все задачи», «отмени все планы на сегодня». \
scope: today — только если явно сказано «на сегодня»; иначе all. \
target: что чистить — goals (цели), tasks (задачи), events (события) или all (всё сразу). \
Примеры: «удали все мои цели» → target=goals, scope=all; «очисти всё» → target=all, scope=all; \
«отмени планы на сегодня» → target=all, scope=today.
- intent=reschedule: перенести существующую вещь на другое время — «перенеси звонок маме на завтра в 15:00». \
title — что перенести, starts_at — НОВОЕ время (ISO 8601).
- intent=chat: любой запрос не про планирование — погода, курс валют, новости, общий вопрос, болтовня, \
благодарность. В answer ничего не пиши — ассистент ответит отдельным шагом.
  * Если спросили про ПОГОДУ: заполни weather_city (если город не назван — «Москва»). \
Окно прогноза: weather_hours — СКОЛЬКО ЧАСОВ интересует (обычно 3, максимум 24), \
weather_start_hours — через сколько часов от СЕЙЧАС это окно начинается (по умолчанию 0). \
Примеры: «погода на ближайшие 3 часа» → hours=3, start=0; «погода завтра в 6 утра» \
(сейчас 18:30) → hours=3, start=12; «а в 15:00?» (сейчас 12:00) → hours=2, start=3. \
Ответ придёт из метео-API.
  * Если спросили про КУРС ВАЛЮТ: заполни currency (ISO-код, например USD, EUR) и currency_base \
(в чём считать, по умолчанию RUB). Ответ придёт из API курсов.

Важно:
- Одно сообщение может нести несколько просьб: «напомни через час позвонить маме, и запиши, \
что хочу выучить испанский к лету» → два items: add_event (позвонить маме) и add_goal (испанский).
- «напомнить через 1 час 5 минут написать любимой» → add_event, starts_at = текущее время + 1 ч 5 мин.
- Событие/напоминание — всегда конкретный момент времени; цель — растянутый срок (к лету, за месяц).
- Текущее время: {now}. Таймзона: {tz}. Все относительные сроки («через час», «завтра») считай \
СТРОГО от этого момента. СЧИТАЙ ДАТЫ ВНИМАТЕЛЬНО: «завтра» — следующий календарный день после \
указанного «Текущее время», «послезавтра» — через два. Перед ответом сверь день и месяц.
- Если ничего распознать не удалось — верни один item с intent=chat и пустым остальным."""

CHAT_SYSTEM_PROMPT = """Ты — Джарвис, личный ассистент пользователя в Telegram. Отвечай по-русски: \
дружелюбно, по делу и кратко (1–5 предложений), без таблиц и заголовков. \
{internet_rule} \
Если спросили про погоду — напиши условия и температуру по часам на запрошенный период, будет ли нужен зонт. \
Текущие дата и время: {now}. Таймзона пользователя: {tz}."""

INTERNET_ON = "Для погоды, курсов валют, новостей и любых актуальных данных обязательно используй поиск Google и опирайся на свежие результаты."
INTERNET_OFF = "У тебя нет доступа в интернет: не выдумывай точные актуальные цифры (курсы, счёт матчей и т.п.) и честно говори, если не уверен."


def _system_prompt(user_tz: str) -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    return SYSTEM_PROMPT.format(now=now, tz=user_tz)


def _chat_system_prompt(user_tz: str, internet: bool = True) -> str:
    now = datetime.now(get_tz(user_tz)).isoformat(timespec="minutes")
    rule = INTERNET_ON if internet else INTERNET_OFF
    return CHAT_SYSTEM_PROMPT.format(now=now, tz=user_tz, internet_rule=rule)


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


async def _gemini_chain(question: str, user_tz: str, use_search: bool) -> str:
    last_exc: Exception | None = None
    for round_no in range(MAX_ROUNDS):
        if round_no:
            await asyncio.sleep(RETRY_PAUSE_SEC)
        for model in MODEL_CHAIN:
            try:
                config = types.GenerateContentConfig(
                    system_instruction=_chat_system_prompt(user_tz, internet=use_search),
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


async def chat_answer(question: str, user_tz: str) -> str:
    """Свободное общение: GigaChat как живой ассистент, при сбое — Gemini (поиск → без поиска)."""
    if giga.enabled():
        try:
            answer = await giga.chat(
                [
                    {"role": "system", "content": _chat_system_prompt(user_tz, internet=False)},
                    {"role": "user", "content": question},
                ]
            )
            if answer and answer.strip():
                return answer.strip()
        except Exception:
            log.warning("GigaChat chat failed, fallback to Gemini search", exc_info=True)
    try:
        return await _gemini_chain(question, user_tz, use_search=True)
    except Exception as e:
        if getattr(e, "code", None) != 429:
            raise
        # Квота поиска исчерпана — пробуем обычную генерацию (у неё отдельная квота)
        log.warning("Gemini search quota exhausted, retrying without search")
        return await _gemini_chain(question, user_tz, use_search=False)


async def hold_phrase() -> str | None:
    """Живая фраза «уже работаю над этим» — генерируется отдельно, чтобы не быть шаблонной."""
    prompt = HOLD_PROMPT.format(name=OWNER_NAME)
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
