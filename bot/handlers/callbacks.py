from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from ..db import Event, Goal, ReminderLog, SessionLocal, Subtask, Task, User, get_or_create_user
from ..goalplan import create_goal_plan, goal_card_text
from ..keyboards import MAIN_MENU, TIMEZONES, goal_folder, settings_menu
from ..pending import pop_group
from ..views import goals_view, save_intent, tasks_view, today_view

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("view:"))
async def on_view(cb: CallbackQuery) -> None:
    action = cb.data.split(":", 1)[1]
    kb = MAIN_MENU
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        if action == "today":
            text = await today_view(session, user)
        elif action == "tasks":
            text = await tasks_view(session, user)
        elif action == "goals":
            text, kb = await goals_view(session, user)
        elif action == "settings":
            await cb.message.edit_reply_markup(reply_markup=settings_menu(user.tz))
            await cb.answer()
            return
        else:  # menu
            await cb.message.edit_reply_markup(reply_markup=MAIN_MENU)
            await cb.answer()
            return
    await cb.message.edit_text(text, reply_markup=kb or MAIN_MENU)
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
        created: list = []
        lines = [
            await save_intent(session, user, intent, created_goals=created)
            for intent in group.intents
        ]
    await cb.message.edit_text("\n".join(lines))
    await cb.answer("Сохранено ✅")
    await _spawn_goal_plans(cb.message, created, group.text)


async def _spawn_goal_plans(message, goals: list, context: str) -> None:
    """После подтверждения цели спрашивает срок плана и ждёт ответ пользователя."""
    from .. import pending

    if not goals:
        return
    pending.PLAN_ASK[message.chat.id] = [(g.id, context) for g in goals]
    names = " и ".join(f"«{g.title}»" for g in goals)
    await message.answer(
        f"🎯 Цель {names} записана!\n\n"
        "📅 На какой срок составить план? Напиши, например:\n"
        "• «на неделю»\n"
        "• «на 2 недели, пн-пт»\n"
        "• «на месяц, в 20:00»\n\n"
        "Можно указать дни недели и время напоминаний."
    )


async def _generate_plans_for(message, items: list[tuple[int, str]], days: int, weekdays, sub_time: str | None) -> None:
    """Строит планы по целям после ответа пользователя о сроке."""
    from ..keyboards import goal_folder

    for goal_id, context in items:
        async with SessionLocal() as session:
            user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
            goal = await session.get(Goal, goal_id)
            if goal is None or goal.done:
                continue
            await message.answer(f"⏳ Составляю план на {days} дн. по цели «{goal.title}»…")
            try:
                text = await create_goal_plan(
                    session, user, goal, context, days=days, weekdays=weekdays, sub_time=sub_time
                )
            except Exception:
                log.exception("Goal plan generation failed")
                await message.answer("Не смог составить план — нажмите «🔄 Новый план» позже.")
                continue
        await message.answer(text, reply_markup=goal_folder(goal.id))


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
            elif kind == "gl":
                # Сначала подзадачи — иначе FK не даст удалить цель
                await session.execute(
                    Subtask.__table__.delete().where(Subtask.goal_id == obj_id)
                )
            await session.delete(obj)
            text = "Удалено 🗑"
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer(text)


# ---------- Папка цели, план, подзадачи ----------

@router.callback_query(F.data.regexp(r"^gopen:(\d+)$"))
async def on_goal_open(cb: CallbackQuery) -> None:
    goal_id = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        goal = await session.get(Goal, goal_id)
        if goal is None or goal.user_id != user.id:
            await cb.answer("Цель не найдена", show_alert=True)
            return
        text = await goal_card_text(session, goal, user)
    try:
        await cb.message.edit_text(text, reply_markup=goal_folder(goal_id))
    except Exception:
        await cb.message.answer(text, reply_markup=goal_folder(goal_id))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^gplan:(\d+)$"))
async def on_goal_replan(cb: CallbackQuery) -> None:
    goal_id = int(cb.data.split(":")[1])
    await cb.answer("Формирую новый план… ⏳")
    async with SessionLocal() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        goal = await session.get(Goal, goal_id)
        if goal is None or goal.user_id != user.id:
            return
        try:
            text = await create_goal_plan(session, user, goal)
        except Exception:
            log.exception("Goal replan failed")
            await cb.message.answer("Не смог составить план, попробуйте ещё раз 🙏")
            return
    try:
        await cb.message.edit_text(text, reply_markup=goal_folder(goal_id))
    except Exception:
        await cb.message.answer(text, reply_markup=goal_folder(goal_id))


@router.callback_query(F.data.regexp(r"^sb:done:(\d+)$"))
async def on_subtask_done(cb: CallbackQuery) -> None:
    sub_id = int(cb.data.split(":")[1])
    async with SessionLocal() as session:
        sub = await session.get(Subtask, sub_id)
        if sub is None:
            await cb.answer("Уже неактуально", show_alert=True)
            return
        sub.done = True
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer("Сделано! 💪")


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
