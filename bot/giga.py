from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

import httpx

from .config import settings

log = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
TRANSCRIBE_URL = "https://gigachat.devices.sberbank.ru/api/v1/audio/transcriptions"

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
    """Один запрос к GigaChat с повторами при 429 (лимит RPM у Сбера). messages: [{"role": ..., "content": ...}]."""
    token = await get_token()
    last_exc: Exception | None = None
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2 * attempt)
        try:
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
                if r.status_code == 429:
                    raise RuntimeError("giga 429")
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last_exc = e
            log.warning("GigaChat chat attempt %s failed: %s", attempt + 1, e)
    assert last_exc is not None
    raise last_exc


def _strip_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    return t.strip()


def ogg_to_wav(ogg: bytes) -> bytes:
    """OGG/Opus из Telegram → WAV 16 kHz mono (формат распознавания GigaChat). PyAV несёт FFmpeg внутри."""
    import io

    import av

    inp = av.open(io.BytesIO(ogg))
    out_buf = io.BytesIO()
    out = av.open(out_buf, "w", format="wav")
    stream = out.add_stream("pcm_s16le", rate=16000, layout="mono")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    for frame in inp.decode(audio=0):
        for rf in resampler.resample(frame):
            for packet in stream.encode(rf):
                out.mux(packet)
    out.close()
    return out_buf.getvalue()


async def transcribe(audio: bytes, filename: str = "voice.wav") -> str:
    """Распознавание речи GigaChat. Возвращает текст или пустую строку."""
    token = await get_token()
    last_exc: Exception | None = None
    for model in ("GigaChat-Audio", "whisper"):
        try:
            async with httpx.AsyncClient(timeout=120, verify=False) as c:
                r = await c.post(
                    TRANSCRIBE_URL,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    files={"file": (filename, audio, "audio/wav")},
                    data={"model": model},
                )
                r.raise_for_status()
                return (r.json().get("text") or "").strip()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            log.warning("GigaChat transcribe with model %s failed: %s", model, e)
    assert last_exc is not None
    raise last_exc


async def chat_json(system_prompt: str, user_text: str, max_tokens: int = 900) -> dict:
    """Запрос, ожидающий JSON в ответе. При невалидном JSON — один повтор с указанием на ошибку."""
    last_raw = ""
    for attempt in range(2):
        sys = system_prompt + "\nВерни ТОЛЬКО валидный JSON без пояснений."
        if attempt:
            sys += (
                "\nОШИБКА: твой прошлый ответ — не валидный JSON (фрагмент: "
                f"{last_raw[:150]!r}). Верни ответ ЗАНОВО — строго один JSON-объект, без текста вокруг."
            )
        raw = await chat(
            [
                {"role": "system", "content": sys},
                {"role": "user", "content": user_text},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        try:
            return json.loads(_strip_json(raw))
        except json.JSONDecodeError:
            last_raw = raw
    raise ValueError(f"GigaChat returned non-JSON twice: {last_raw[:200]!r}")
