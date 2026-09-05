from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.types import InlineKeyboardMarkup

from .config import get_tz
from .db import Event, Goal, ReminderLog, Task, User

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt_dt(dt: datetime, tzname: str) -> str:
    local = _aware(dt).astimezone(get_tz(tzname))
    return f"{WEEKDAYS[local.weekday()]} {local.strftime('%d.%m %H:%M')}"


def _time_until(dt: datetime, tzname: str) -> str:
    delta = _aware(dt) - datetime.now(tz=get_tz(tzname))
    mins = int(delta.total_seconds() // 60)
    if mins < 0:
        return "прошло"
    h, m = divmod(mins, 60)
    if h and m:
        return f"через {h} ч {m} мин"
    if h:
        return f"через {h} ч"
    return f"через {m} мин"


def fmt_event(e: Event, tzname: str, show_countdown: bool = True) -> str:
    local = _aware(e.starts_at).astimezone(get_tz(tzname))
    line = f"• {local.strftime('%d.%m %H:%M')} — {e.title}"
    if show_countdown:
        line += f" ({_time_until(e.starts_at, tzname)})"
    return line


def fmt_task(t: Task, tzname: str) -> str:
    mark = "✔️" if t.done else ("🔴" if t.priority == "high" else "▫️")
    due = (
        f" (до {_aware(t.due_at).astimezone(get_tz(tzname)).strftime('%d.%m %H:%M')})"
        if t.due_at
        else ""
    )
    return f"{mark} {t.title}{due}"


async def today_view(session: AsyncSession, user: User) -> str:
    tz = get_tz(user.tz)
    now = datetime.now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    events = list(
        await session.scalars(
            select(Event)
            .where(
                Event.user_id == user.id,
                Event.done.is_(False),
                Event.starts_at >= day_start,
                Event.starts_at < day_end,
            )
            .order_by(Event.starts_at)
        )
    )
    tasks = list(
        await session.scalars(
            select(Task).where(
                Task.user_id == user.id,
                Task.done.is_(False),
                or_(Task.due_at.is_(None), Task.due_at < day_end),
            )
        )
    )
    lines = [f"📅 Дела на {now.strftime('%d.%m')} ({WEEKDAYS[now.weekday()]}):"]
    if events:
        lines.append("\n<b>События:</b>")
        lines += [fmt_event(e, user.tz) for e in events]
    if tasks:
        lines.append("\n<b>Задачи:</b>")
        lines += [fmt_task(t, user.tz) for t in tasks]
    if not events and not tasks:
        lines.append("\nНа сегодня ничего не запланировано. Отдыхайте! 🌿")
    return "\n".join(lines)


async def tasks_view(session: AsyncSession, user: User) -> str:
    tasks = list(
        await session.scalars(
            select(Task)
            .where(Task.user_id == user.id, Task.done.is_(False))
            .order_by(Task.due_at.is_(None), Task.due_at)
        )
    )
    if not tasks:
        return "Задач нет. Напоминания с конкретным временем — это события, они живут в разделе «📅 Сегодня»."
    return "✅ <b>Активные задачи:</b>\n" + "\n".join(fmt_task(t, user.tz) for t in tasks)


async def goals_view(session: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    from .keyboards import goals_kb

    goals = list(
        await session.scalars(
            select(Goal).where(Goal.user_id == user.id, Goal.done.is_(False))
        )
    )
    if not goals:
        return "Целей пока нет. Расскажите, к чему стремитесь — я запомню!", None
    lines = ["🎯 <b>Цели:</b>\nНажмите на цель, чтобы открыть её план 👇"]
    for g in goals:
        target = f" (до {g.target_date.strftime('%d.%m.%Y')})" if g.target_date else ""
        lines.append(f"• {g.title}{target}")
    return "\n".join(lines), goals_kb([(g.id, g.title) for g in goals])


async def save_intent(session: AsyncSession, user: User, intent, created_goals: list | None = None) -> str:
    """Сохраняет распознанное действие в БД. Возвращает строку-отчёт для чата."""
    if intent.intent == "add_event":
        dt = None
        if intent.starts_at:
            s = intent.starts_at.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                dt = None
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=get_tz(user.tz))
        if dt is None:
            return f"⚠️ Не распознал время события «{intent.title or '?'}» — напишите его ещё раз с временем."
        ev = Event(
            user_id=user.id,
            title=intent.title or "Событие",
            starts_at=dt.astimezone(timezone.utc),
            remind_before_min=intent.remind_before_minutes or 60,
            notes=intent.answer,
        )
        session.add(ev)
        await session.commit()
        return f"📌 Событие сохранено: <b>{ev.title}</b>"

    if intent.intent == "add_task":
        due = None
        if intent.due_at:
            s = intent.due_at.replace("Z", "+00:00")
            try:
                due = datetime.fromisoformat(s).astimezone(timezone.utc)
            except ValueError:
                due = None
        tk = Task(
            user_id=user.id,
            title=intent.title or "Задача",
            due_at=due,
            priority=intent.priority or "normal",
        )
        session.add(tk)
        await session.commit()
        return f"✅ Задача добавлена: <b>{tk.title}</b>"

    if intent.intent == "add_goal":
        td = None
        if intent.target_date:
            try:
                td = date.fromisoformat(intent.target_date)
            except ValueError:
                td = None
        gl = Goal(user_id=user.id, title=intent.title or "Цель", target_date=td)
        session.add(gl)
        await session.commit()
        if created_goals is not None:
            created_goals.append(gl)
        return f"🎯 Цель записана: <b>{gl.title}</b>"

    return f"⚠️ Не понял, что сохранять ({intent.intent})."


async def find_for_delete(session: AsyncSession, user: User, title: str) -> list[tuple[str, int, str]]:
    """Ищет события/задачи/цели по подстроке. Возвращает (kind, id, label)."""
    like = f"%{title[:60]}%"
    found: list[tuple[str, int, str]] = []
    for e in await session.scalars(
        select(Event).where(
            Event.user_id == user.id,
            Event.done.is_(False),
            func.lower(Event.title).ilike(func.lower(like)),
        )
    ):
        found.append(("ev", e.id, f"Событие: {e.title} ({_fmt_dt(e.starts_at, user.tz)})"))
    for t in await session.scalars(
        select(Task).where(
            Task.user_id == user.id, Task.done.is_(False), Task.title.ilike(like)
        )
    ):
        found.append(("tk", t.id, f"Задача: {t.title}"))
    for g in await session.scalars(
        select(Goal).where(
            Goal.user_id == user.id, Goal.done.is_(False), Goal.title.ilike(like)
        )
    ):
        found.append(("gl", g.id, f"Цель: {g.title}"))
    return found


async def cancel_plans(session: AsyncSession, user: User, scope: str = "today") -> str:
    """Помечает выполненными/отменёнными события и задачи."""
    tz = get_tz(user.tz)
    now = datetime.now(tz)
    start = end = None
    if scope == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)

    q = select(Event).where(Event.user_id == user.id, Event.done.is_(False))
    if start is not None:
        q = q.where(Event.starts_at >= start, Event.starts_at < end)
    events = list(await session.scalars(q))

    qt = select(Task).where(Task.user_id == user.id, Task.done.is_(False))
    tasks = list(await session.scalars(qt))

    for e in events:
        e.done = True
    for t in tasks:
        t.done = True
    await session.commit()

    period = "на сегодня" if scope == "today" else "вообще все"
    parts = []
    if events:
        parts.append(f"событий: {len(events)}")
    if tasks:
        parts.append(f"задач: {len(tasks)}")
    if not parts:
        return f"Отменять нечего — активных дел {period} не нашлось 🙂"
    return f"🧹 Отменил {period}: " + ", ".join(parts) + "."


async def reschedule(session: AsyncSession, user: User, title: str, new_dt: datetime) -> str:
    """Переносит событие на новое время."""
    found = await find_for_delete(session, user, title)
    ev_matches = [f for f in found if f[0] == "ev"]
    if not ev_matches:
        if found:
            return "Переносить по времени я умею только события (с конкретным временем)."
        return f"Не нашёл «{title}» для переноса. Попробуйте уточнить название."
    kind, obj_id, label = ev_matches[0]
    ev = await session.get(Event, obj_id)
    if ev is None:
        return f"Не нашёл «{title}» для переноса."
    old = _fmt_dt(ev.starts_at, user.tz)
    ev.starts_at = new_dt.astimezone(timezone.utc)
    from sqlalchemy import delete as sa_delete

    await session.execute(sa_delete(ReminderLog).where(ReminderLog.event_id == ev.id))
    await session.commit()
    return f"🔁 Перенёс: <b>{ev.title}</b>\nБыло: {old}\nСтало: {_fmt_dt(ev.starts_at, user.tz)}"
