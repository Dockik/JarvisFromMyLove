from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_tz
from .db import Event, Goal, Task, User

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
        return "Задач нет. Напишите их мне текстом или голосом!"
    return "✅ <b>Активные задачи:</b>\n" + "\n".join(fmt_task(t, user.tz) for t in tasks)


async def goals_view(session: AsyncSession, user: User) -> str:
    goals = list(
        await session.scalars(
            select(Goal).where(Goal.user_id == user.id, Goal.done.is_(False))
        )
    )
    if not goals:
        return "Целей пока нет. Расскажите, к чему стремитесь — я запомню!"
    lines = ["🎯 <b>Цели:</b>"]
    for g in goals:
        target = f" (до {g.target_date.strftime('%d.%m.%Y')})" if g.target_date else ""
        lines.append(f"• {g.title}{target}")
    return "\n".join(lines)


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
