from __future__ import annotations

import uuid
from dataclasses import dataclass

from .gemini import ParsedIntent


@dataclass
class PendingGroup:
    """Одна карточка подтверждения: одно или несколько действий сразу."""

    intents: list[ParsedIntent]
    chat_id: int
    text: str = ""


GROUPS: dict[str, PendingGroup] = {}
BY_CHAT: dict[int, list[str]] = {}

MAX_PENDING = 200


def put_group(g: PendingGroup) -> str:
    key = uuid.uuid4().hex[:12]
    GROUPS[key] = g
    BY_CHAT.setdefault(g.chat_id, []).append(key)
    if len(GROUPS) > MAX_PENDING:
        for k in list(GROUPS)[: -MAX_PENDING // 2]:
            pop_group(k)
    return key


def pop_group(key: str) -> PendingGroup | None:
    g = GROUPS.pop(key, None)
    if g is not None:
        keys = BY_CHAT.get(g.chat_id)
        if keys and key in keys:
            keys.remove(key)
        if keys is not None and not keys:
            BY_CHAT.pop(g.chat_id, None)
    return g


def chat_groups(chat_id: int) -> list[tuple[str, PendingGroup]]:
    """Живые карточки, ожидающие подтверждения в этом чате."""
    return [(k, GROUPS[k]) for k in BY_CHAT.get(chat_id, []) if k in GROUPS]
