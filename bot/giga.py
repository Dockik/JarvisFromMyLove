from __future__ import annotations

import json
import logging
import time
import uuid

import httpx

from .config import settings

log = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

_token: str | None = None
_expires: float = 0.0


def enabled() -> bool:
    return bool(settings.giga_key)


async def get_token() -> str:
    global _token, _expires
    if _token and time.time() < _expires - 60:
        return _token
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as c:
                r = await c.post(
                    OAUTH_URL,
                    headers={
                        "Authorization": f"Basic {settings.giga_key}",
                        "RqUID": str(uuid.uuid4()),
                        "Accept": "application/json",
                    },
                    data={"scope": settings.giga_scope},
                )
                r.raise_for_status()
                data = r.json()
            break
        except Exception as e:  # noqa: BLE001
            last_exc = e
            log.warning("GigaChat oauth attempt %s failed", attempt + 1)
    else:
        assert last_exc is not None
        raise last_exc
    _token = data["access_token"]
    exp = data.get("expires_at")
    # expires_at приходит в миллисекундах epoch
    _expires = (exp / 1000) if exp and exp > 1e12 else float(exp or (time.time() + 1800))
    log.info("GigaChat token refreshed, ttl=%ss", int(_expires - time.time()))
    return _token


async def chat(messages: list[dict], temperature: float = 0.4, max_tokens: int = 900) -> str:
    """Один запрос к GigaChat. messages: [{"role": ..., "content": ...}]."""
    token = await get_token()
    async with httpx.AsyncClient(timeout=90, verify=False) as c:
        r = await c.post(
            CHAT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "model": "GigaChat",
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _strip_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    return t.strip()


async def chat_json(system_prompt: str, user_text: str) -> dict:
    """Запрос, ожидающий JSON в ответе."""
    raw = await chat(
        [
            {"role": "system", "content": system_prompt + "\nВерни ТОЛЬКО валидный JSON без пояснений."},
            {"role": "user", "content": user_text},
        ],
        temperature=0.2,
    )
    return json.loads(_strip_json(raw))
