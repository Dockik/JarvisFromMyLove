from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .gemini import ParsedIntent


@dataclass
class Pending:
    intent: ParsedIntent
    chat_id: int
    text: str = ""
    extra: dict = field(default_factory=dict)


PENDING: dict[str, Pending] = {}


def put_pending(p: Pending) -> str:
    key = uuid.uuid4().hex[:12]
    PENDING[key] = p
    if len(PENDING) > 200:
        for k in list(PENDING)[:-100]:
            PENDING.pop(k, None)
    return key


def pop_pending(key: str) -> Pending | None:
    return PENDING.pop(key, None)
