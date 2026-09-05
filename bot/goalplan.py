from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_tz
from .db import Goal, Subtask, User, utcnow
from .gemini import generate_goal_plan

log = logging.getLogger(__name__)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _parse_time(s: str) -> tuple[int, int]:
    try:
        h, m = map(int, s.split(":"))
        return h, m
    except ValueError:
        return 18, 0


async def create_goal_plan(session: AsyncSession, user: User, goal: Goal, context: str = "") -> str:
    """Генерирует недельный план, сохраняет его и подзадачи. Возвращает текст карточки."""
    plan = await generate_goal_plan(goal.title, context, goal.target_date, user.tz)
    goal.plan = plan.plan
    goal.plan_expires_at = utcnow() + timedelta(days=7)
    goal.plan_gen_on = date.today()

    await session.execute(sa_delete(Subtask).where(Subtask.goal_id == goal.id))
    for s in plan.subtasks:
        session.add(
            Subtask(
                goal_id=goal.id,
                user_id=goal.user_id,
                title=s.title,
                weekday=s.weekday,
                time_str=s.time,
            )
        )
    await session.commit()
    return await goal_card_text(session, goal, user)


async def goal_card_text(session: AsyncSession, goal: Goal, user: User) -> str:
    subtasks = list(
        await session.scalars(
            select(Subtask).where(Subtask.goal_id == goal.id, Subtask.done.is_(False)).order_by(Subtask.weekday, Subtask.time_str)
        )
    )
    lines = [f"🎯 <b>{goal.title}</b>"]
    if goal.target_date:
        lines.append(f"🏁 Дедлайн: {goal.target_date.strftime('%d.%m.%Y')}")
    if goal.plan:
        expires = goal.plan_expires_at.astimezone(get_tz(user.tz)) if goal.plan_expires_at else None
        exp_str = expires.strftime("%d.%m") if expires else "?"
        lines.append(f"\n📋 <b>План недели (до {exp_str}):</b>\n{goal.plan}")
    if subtasks:
        lines.append("\n⏰ <b>Напоминания:</b>")
        for s in subtasks:
            lines.append(f"• {WEEKDAYS_RU[s.weekday]} {s.time_str} — {s.title}")
    else:
        lines.append("\n⏰ Напоминаний на эту неделю нет — нажмите «🔄 Новый план».")
    return "\n".join(lines)


async def expired_goals(session: AsyncSession) -> list[Goal]:
    return list(
        await session.scalars(
            select(Goal).where(
                Goal.done.is_(False),
                Goal.plan_expires_at.is_not(None),
                Goal.plan_expires_at < datetime.now(timezone.utc),
            )
        )
    )
