from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..config import get_tz
from ..db import Event, Goal, ReminderLog, SessionLocal, Task, User, get_or_create_user
from ..gemini import ParsedIntent
from ..keyboards import MAIN_MENU, TIMEZONES, settings_menu
from ..pending import pop_pending
from ..views import goals_view, tasks_view, today_view

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("view:"))
async def on_view(cb: CallbackQuery) -> None:
    action = cb.data.split(":", 1)[1]
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        if action == "today":
            text = await today_view(session, user)
        elif action == "tasks":
            text = await tasks_view(session, user)
        elif action == "goals":
            text = await goals_view(session, user)
        elif action == "settings":
            await cb.message.edit_reply_markup(reply_markup=settings_menu(user.tz))
            await cb.answer()
            return
        else:  # menu
            await cb.message.edit_reply_markup(reply_markup=MAIN_MENU)
            await cb.answer()
            return
    await cb.message.edit_text(text, reply_markup=MAIN_MENU)
    await cb.answer()


# ---------- Подтверждение новых записей ----------

async def _save_pending(cb: CallbackQuery, prefix: str, key: str) -> None:
    pending = pop_pending(key)
    if pending is None:
        await cb.answer("Запись устарела, отправьте заново", show_alert=True)
        return
    intent: ParsedIntent = pending.intent
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        if prefix == "evadd":
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
                await cb.answer("Не распознал время события", show_alert=True)
                return
            ev = Event(
                user_id=user.id,
                title=intent.title or "Событие",
                starts_at=dt.astimezone(timezone.utc),
                remind_before_min=intent.remind_before_minutes or 60,
                notes=intent.answer,
            )
            session.add(ev)
            await session.commit()
            text = f"📌 Событие сохранено: <b>{ev.title}</b>"
        elif prefix == "tkadd":
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
            text = f"✅ Задача добавлена: <b>{tk.title}</b>"
        else:  # gladd
            from datetime import date

            td = None
            if intent.target_date:
                try:
                    td = date.fromisoformat(intent.target_date)
                except ValueError:
                    td = None
            gl = Goal(user_id=user.id, title=intent.title or "Цель", target_date=td)
            session.add(gl)
            await session.commit()
            text = f"🎯 Цель записана: <b>{gl.title}</b>"
    await cb.message.edit_text(text)
    await cb.answer("Сохранено ✅")


@router.callback_query(F.data.regexp(r"^(evadd|tkadd|gladd):(save|edit|cancel):(\w+)$"))
async def on_confirm(cb: CallbackQuery) -> None:
    prefix, action, key = cb.data.split(":")
    if action == "save":
        await _save_pending(cb, prefix, key)
    elif action == "cancel":
        pop_pending(key)
        await cb.message.edit_text("Отменено.")
        await cb.answer()
    else:  # edit
        pop_pending(key)
        await cb.message.edit_text("Отменено. Напишите исправленный вариант обычным сообщением.")
        await cb.answer()


# ---------- Действия над существующими ----------

@router.callback_query(F.data.regexp(r"^(ev|tk|gl):(done|del):(\d+)$"))
async def on_object_action(cb: CallbackQuery) -> None:
    kind, action, obj_id = cb.data.split(":")
    obj_id = int(obj_id)
    model = {"ev": Event, "tk": Task, "gl": Goal}[kind]
    async with SessionLocal() as session:
        obj = await session.get(model, obj_id)
        if obj is None:
            await cb.answer("Уже удалено", show_alert=True)
            return
        if action == "done":
            obj.done = True
            text = "Выполнено ✔️"
        else:
            if kind == "ev":
                await session.execute(
                    ReminderLog.__table__.delete().where(ReminderLog.event_id == obj_id)
                )
            await session.delete(obj)
            text = "Удалено 🗑"
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer(text)


# ---------- Настройки ----------

@router.callback_query(F.data.startswith("st:"))
async def on_settings(cb: CallbackQuery) -> None:
    _, field, value = cb.data.split(":")
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        if field == "digest":
            user.digest_time = value
            await session.commit()
            await cb.answer(f"Дайджест в {value} ⏰", show_alert=True)
        elif field == "tz":
            keys = list(TIMEZONES)
            idx = keys.index(user.tz) if user.tz in keys else -1
            user.tz = keys[(idx + 1) % len(keys)]
            await session.commit()
        await session.refresh(user)
        await cb.message.edit_reply_markup(reply_markup=settings_menu(user.tz))
    await cb.answer()
