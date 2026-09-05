from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..db import Event, Goal, ReminderLog, SessionLocal, Task, User, get_or_create_user
from ..keyboards import MAIN_MENU, TIMEZONES, settings_menu
from ..pending import pop_group
from ..views import goals_view, save_intent, tasks_view, today_view

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

@router.callback_query(F.data.regexp(r"^grp:(save|edit|cancel):(\w+)$"))
async def on_confirm(cb: CallbackQuery) -> None:
    _, action, key = cb.data.split(":")
    group = pop_group(key)
    if group is None:
        await cb.answer("Запись устарела, отправьте заново", show_alert=True)
        return
    if action == "cancel":
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return
    if action == "edit":
        await cb.message.edit_text("Отменено. Напишите исправленный вариант обычным сообщением.")
        await cb.answer()
        return
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        lines = [await save_intent(session, user, intent) for intent in group.intents]
    await cb.message.edit_text("\n".join(lines))
    await cb.answer("Сохранено ✅")


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
