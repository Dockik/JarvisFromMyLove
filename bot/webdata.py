from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

WEATHER_CODES = {
    0: "Ясно ☀️", 1: "Преимущественно ясно 🌤", 2: "Переменная облачность ⛅",
    3: "Пасмурно ☁️", 45: "Туман 🌫", 48: "Изморозь 🌫",
    51: "Слабая морось 🌦", 53: "Морось 🌦", 55: "Сильная морось 🌧",
    61: "Небольшой дождь 🌦", 63: "Дождь 🌧", 65: "Ливень 🌧",
    71: "Небольшой снег 🌨", 73: "Снег 🌨", 75: "Сильный снег ❄️", 77: "Снежные зёрна 🌨",
    80: "Ливни 🌧", 81: "Ливни 🌧", 82: "Сильные ливни ⛈",
    85: "Снегопад 🌨", 86: "Сильный снегопад ❄️",
    95: "Гроза ⛈", 96: "Гроза с градом ⛈", 99: "Сильная гроза с градом ⛈",
}


async def get_weather(city: str, hours: int = 3) -> str:
    """Погода через Open-Meteo — бесплатно, без ключей и квот."""
    hours = max(1, min(int(hours or 3), 24))
    async with httpx.AsyncClient(timeout=10) as c:
        geo = await c.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru", "format": "json"},
        )
        geo.raise_for_status()
        results = geo.json().get("results")
        if not results:
            return f"Не нашёл город «{city}» 🤷 Попробуйте уточнить название."
        r = results[0]
        name, lat, lon = r.get("name", city), r["latitude"], r["longitude"]
        fc = await c.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "forecast_days": 2,
                "timezone": "auto",
            },
        )
        fc.raise_for_status()

    data = fc.json()
    hourly = data["hourly"]
    times: list[str] = hourly["time"]
    temps = hourly["temperature_2m"]
    probs = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []

    utc_off = data.get("utc_offset_seconds", 0)
    now_local = (datetime.now(timezone.utc) + timedelta(seconds=utc_off)).replace(tzinfo=None)

    lines = [f"🌤 <b>{name}</b>, ближайшие {hours} ч:"]
    umbrella = False
    shown = 0
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t)
        if dt < now_local.replace(minute=0, second=0, microsecond=0):
            continue
        if shown >= hours:
            break
        prob = probs[i] if i < len(probs) else None
        code = codes[i] if i < len(codes) else None
        desc = WEATHER_CODES.get(code, "")
        line = f"{dt.strftime('%H:%M')}: {temps[i]:+.0f}°"
        if desc:
            line += f", {desc}"
        if prob is not None and prob >= 30:
            line += f", осадки {prob}%"
        if prob is not None and prob >= 60:
            umbrella = True
        lines.append(line)
        shown += 1
    if umbrella:
        lines.append("☔️ Вероятны осадки — лучше взять зонт.")
    return "\n".join(lines)


async def get_rate(currency: str, base: str = "RUB") -> str:
    """Курс валют через open.er-api.com — бесплатно, без ключей."""
    currency, base = currency.upper().strip(), base.upper().strip()
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(f"https://open.er-api.com/v6/latest/{currency}")
        resp.raise_for_status()
        data = resp.json()
    if data.get("result") != "success":
        return "Не смог узнать курс, попробуйте позже 🙏"
    rate = data.get("rates", {}).get(base)
    if rate is None:
        return f"Не знаю валюту {currency} или {base} 🤷"
    updated = (data.get("time_last_update_utc") or "")[:16]
    return f"💱 1 {currency} = <b>{rate:.2f}</b> {base}\n<i>Данные от {updated}</i>"
