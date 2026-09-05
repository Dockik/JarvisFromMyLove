from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import get_tz
from .db import Event, Goal, ReminderLog, SessionLocal, Subtask, User
from .keyboards import event_actions, subtask_done_kb
from .views import today_view

log = logging.getLogger(__name__)

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def check_reminders(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        events = list(
            await session.scalars(
                select(Event).where(Event.done.is_(False), Event.starts_at > now)
            )
        )
        events = [
            e
            for e in events
            if _aware(e.starts_at) - timedelta(minutes=e.remind_before_min) <= now
        ]
        for ev in events:
            already = await session.scalar(
                select(ReminderLog).where(
                    and_(ReminderLog.event_id == ev.id, ReminderLog.kind == "pre")
                )
            )
            if already:
                continue
            user = await session.get(User, ev.user_id)
            if user is None:
                continue
            local_start = ev.starts_at.astimezone(get_tz(user.tz))
            try:
                await bot.send_message(
                    user.tg_id,
                    f"⏰ Напоминание: через {ev.remind_before_min} мин — "
                    f"<b>{ev.title}</b>\nНачало в {local_start.strftime('%H:%M')}",
                    reply_markup=event_actions(ev.id),
                )
            except Exception:
                log.exception("Failed to send reminder to %s", user.tg_id)
                continue
            session.add(ReminderLog(event_id=ev.id, kind="pre"))
        await session.commit()


async def check_digests(bot: Bot) -> None:
    async with SessionLocal() as session:
        users = list(await session.scalars(select(User)))
        for user in users:
            local_now = datetime.now(get_tz(user.tz))
            hhmm = local_now.strftime("%H:%M")
            if hhmm != user.digest_time:
                continue
            if user.last_digest_date == local_now.date():
                continue
            text = await _digest_text(session, user)
            try:
                await bot.send_message(user.tg_id, text, reply_markup=_digest_kb())
            except Exception:
                log.exception("Failed to send digest to %s", user.tg_id)
                continue
            user.last_digest_date = local_now.date()
        await session.commit()


async def _digest_text(session: AsyncSession, user: User) -> str:
    text = await today_view(session, user)
    local_now = datetime.now(get_tz(user.tz))
    rows = list(
        await session.execute(
            select(Subtask, Goal)
            .join(Goal, Subtask.goal_id == Goal.id)
            .where(
                Subtask.user_id == user.id,
                Subtask.done.is_(False),
                Goal.done.is_(False),
                Subtask.weekday == local_now.weekday(),
            )
            .order_by(Subtask.time_str)
        )
    )
    if rows:
        lines = [f"• {s.time_str} — {s.title} (цель «{g.title}»)" for s, g in rows]
        text += "\n\n🎯 <b>Цели на сегодня:</b>\n" + "\n".join(lines)
    return "☀️ Доброе утро!\n\n" + text


def _digest_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📅 Обновить", callback_data="view:today")]]
    )


async def check_goal_subtasks(bot: Bot) -> None:
    """Напоминания подзадач цели: за час и в назначенное время + автопродление плана."""
    async with SessionLocal() as session:
        rows = list(
            await session.execute(
                select(Subtask, Goal, User)
                .join(Goal, Subtask.goal_id == Goal.id)
                .join(User, Subtask.user_id == User.id)
                .where(Subtask.done.is_(False), Goal.done.is_(False))
            )
        )
        now_utc = datetime.now(timezone.utc)
        regen: list[tuple[Goal, User]] = []
        for sub, goal, user in rows:
            local_now = datetime.now(get_tz(user.tz))
            if sub.weekday != local_now.weekday():
                continue
            # Напоминание ЗА ЧАС до времени подзадачи
            try:
                h, m = map(int, sub.time_str.split(":"))
            except ValueError:
                h, m = 18, 0
            occurs_at = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
            mins_left = (occurs_at - local_now).total_seconds() / 60
            if sub.pre_reminded_on != local_now.date() and 0 < mins_left <= 60:
                sub.pre_reminded_on = local_now.date()
                try:
                    await bot.send_message(
                        user.tg_id,
                        f"⏰ Через час: <b>{sub.title}</b>\n🎯 Цель «{goal.title}»",
                        reply_markup=subtask_done_kb(sub.id),
                    )
                except Exception:
                    log.exception("Subtask pre-reminder failed for %s", user.tg_id)
            # Напоминание В НАЗНАЧЕННОЕ ВРЕМЯ
            if sub.time_str > local_now.strftime("%H:%M"):
                continue
            if sub.last_reminded_on == local_now.date():
                continue
            sub.last_reminded_on = local_now.date()
            try:
                await bot.send_message(
                    user.tg_id,
                    f"🎯 Цель «{goal.title}»:\n<b>{sub.title}</b>",
                    reply_markup=subtask_done_kb(sub.id),
                )
            except Exception:
                log.exception("Subtask reminder failed for %s", user.tg_id)
            # План истёк? Планируем автопродление (раз в день)
            if (
                goal.plan_expires_at is not None
                and _aware(goal.plan_expires_at) < now_utc
                and goal.plan_gen_on != local_now.date()
            ):
                goal.plan_gen_on = local_now.date()
                regen.append((goal, user))
        await session.commit()

    for goal, user in regen:
        await _regen_goal_plan(bot, goal.id, user.tg_id)


async def _regen_goal_plan(bot: Bot, goal_id: int, tg_id: int) -> None:
    from .goalplan import create_goal_plan

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.tg_id == tg_id))
        goal = await session.get(Goal, goal_id)
        if user is None or goal is None or goal.done:
            return
        try:
            await bot.send_message(tg_id, f"🔄 Неделя по цели «{goal.title}» закончилась — обновляю план…")
            text = await create_goal_plan(session, user, goal)
        except Exception:
            log.exception("Goal auto-replan failed")
            await bot.send_message(tg_id, "Не смог обновить план — скажите «обнови план», и я повторю.")
            return
    await bot.send_message(tg_id, text)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(check_reminders, "interval", seconds=60, args=[bot], id="reminders")
    scheduler.add_job(check_digests, "interval", seconds=60, args=[bot], id="digests")
    scheduler.add_job(check_goal_subtasks, "interval", seconds=60, args=[bot], id="goal_subtasks")
    scheduler.start()
    log.info("Scheduler started")
    return scheduler
