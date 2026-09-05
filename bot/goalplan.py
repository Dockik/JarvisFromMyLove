from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Goal, Subtask, User

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def goal_card_text(session: AsyncSession, goal: Goal, user: User) -> str:
    subtasks = list(
        await session.scalars(
            select(Subtask).where(Subtask.goal_id == goal.id, Subtask.done.is_(False)).order_by(Subtask.weekday, Subtask.time_str)
        )
    )
    lines = [f"🎯 <b>{goal.title}</b>"]
    if goal.target_date:
        lines.append(f"🏁 Дедлайн: {goal.target_date.strftime('%d.%m.%Y')}")
    lines.append("\n☀️ Каждое утро в дайджесте я буду напоминать тебе об этой цели.")
    if subtasks:
        lines.append("\n⏰ <b>Напоминания:</b>")
        for s in subtasks:
            lines.append(f"• {WEEKDAYS_RU[s.weekday]} {s.time_str} — {s.title}")
    return "\n".join(lines)
