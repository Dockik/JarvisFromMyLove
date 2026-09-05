from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import get_tz
from .db import Event, Goal, ReminderLog, SessionLocal, User
from .keyboards import event_actions
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
    goals = list(
        await session.scalars(
            select(Goal).where(Goal.user_id == user.id, Goal.done.is_(False)).order_by(Goal.id)
        )
    )
    if goals:
        lines = []
        for g in goals:
            line = f"• {g.title}"
            if g.target_date:
                line += f" (до {g.target_date.strftime('%d.%m.%Y')})"
            lines.append(line)
        text += "\n\n🎯 <b>Твои цели (не забывай о них):</b>\n" + "\n".join(lines)
    return "☀️ Доброе утро!\n\n" + text


def _digest_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📅 Обновить", callback_data="view:today")]]
    )


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=timezone.utc)
    scheduler.add_job(check_reminders, "interval", seconds=60, args=[bot], id="reminders")
    scheduler.add_job(check_digests, "interval", seconds=60, args=[bot], id="digests")
    scheduler.start()
    log.info("Scheduler started")
    return scheduler
