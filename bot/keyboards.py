from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MAIN_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="view:today")],
        [
            InlineKeyboardButton(text="✅ Задачи", callback_data="view:tasks"),
            InlineKeyboardButton(text="🎯 Цели", callback_data="view:goals"),
        ],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="view:settings")],
    ]
)


def confirm_card(prefix: str, key: str | int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data=f"{prefix}:save:{key}"),
                InlineKeyboardButton(text="✏️ Текстом", callback_data=f"{prefix}:edit:{key}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"{prefix}:cancel:{key}"),
            ]
        ]
    )


def event_actions(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✔️ Выполнено", callback_data=f"ev:done:{event_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ev:del:{event_id}"),
            ]
        ]
    )


def task_actions(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✔️ Готово", callback_data=f"tk:done:{task_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"tk:del:{task_id}"),
            ]
        ]
    )


def goal_actions(goal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✔️ Достигнута", callback_data=f"gl:done:{goal_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gl:del:{goal_id}"),
            ]
        ]
    )


def goal_folder(goal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Новый план", callback_data=f"gplan:{goal_id}")],
            [
                InlineKeyboardButton(text="✅ Достигнута", callback_data=f"gl:done:{goal_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"gl:del:{goal_id}"),
            ],
            [InlineKeyboardButton(text="◀️ К целям", callback_data="view:goals")],
        ]
    )


def goals_kb(openable: list[tuple[int, str]]) -> InlineKeyboardMarkup | None:
    """Кнопки-«папки» для каждой цели."""
    if not openable:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📂 {title[:28]}", callback_data=f"gopen:{gid}")]
            for gid, title in openable
        ]
    )


def subtask_done_kb(sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✔️ Сделано", callback_data=f"sb:done:{sub_id}")]]
    )


DIGEST_TIMES = ["06:00", "07:00", "08:00", "09:00"]
TIMEZONES = {
    "Europe/Moscow": "Москва (UTC+3)",
    "Europe/Samara": "Самара (UTC+4)",
    "Asia/Yekaterinburg": "Екатеринбург (UTC+5)",
    "Asia/Novosibirsk": "Новосибирск (UTC+7)",
    "Asia/Vladivostok": "Владивосток (UTC+10)",
}


def settings_menu(tz: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⏰ Дайджест сейчас: {t}", callback_data=f"st:digest:{t}")]
        for t in DIGEST_TIMES
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=f"🌍 {TIMEZONES.get(tz, tz)}",
                callback_data="st:tz:next",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="◀️ Меню", callback_data="view:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Меню", callback_data="view:menu")]]
    )
