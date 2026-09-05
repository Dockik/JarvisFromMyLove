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


MAX_ROWS = 10  # строк в прогнозе, чтобы не спамить в чат


def _fmt_line(hhmm: str, temp: float, desc: str, prob: int | None) -> str:
    line = f"{hhmm}: {temp:+.0f}°"
    if desc:
        line += f", {desc}"
    if prob is not None and prob >= 30:
        line += f", осадки {prob}%"
    return line


def _finish(lines: list[str], umbrella: bool) -> str:
    if umbrella:
        lines.append("☔️ Вероятны осадки — лучше взять зонт.")
    return "\n".join(lines)


async def _geocode(city: str) -> tuple[str, float, float, int] | None:
    """Геокодинг через Open-Meteo: (название, lat, lon, utc_offset_seconds)."""
    async with httpx.AsyncClient(timeout=10) as c:
        geo = await c.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "ru", "format": "json"},
        )
        geo.raise_for_status()
        results = geo.json().get("results")
    if not results:
        return None
    r = results[0]
    return r.get("name", city), r["latitude"], r["longitude"], r.get("utc_offset_seconds", 0)


async def get_weather(city: str, hours: int = 3) -> str:
    """Погода: Open-Meteo → met.no → wttr.in (все бесплатные, без ключей)."""
    try:
        hours = max(1, min(int(hours or 3), 24))
        geo = await _geocode(city)
        if geo is None:
            return f"Не нашёл город «{city}» 🤷 Попробуйте уточнить название."
        name, lat, lon, utc_off = geo
        try:
            return await _weather_open_meteo(name, lat, lon, utc_off, hours)
        except Exception:
            log.warning("Open-Meteo failed, trying met.no", exc_info=True)
        try:
            return await _weather_metno(name, lat, lon, utc_off, hours)
        except Exception:
            log.warning("met.no failed, trying wttr.in", exc_info=True)
        return await _weather_wttr(name, hours)
    except Exception:
        log.exception("All weather sources failed")
        return "Не смог получить погоду, попробуйте ещё раз 🙏"


async def _weather_open_meteo(name: str, lat: float, lon: float, utc_off: int, hours: int) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
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

    now_local = (datetime.now(timezone.utc) + timedelta(seconds=utc_off)).replace(tzinfo=None)
    deadline = now_local + timedelta(hours=hours)

    lines = [f"🌤 <b>{name}</b>, ближайшие {hours} ч:"]
    umbrella = False
    for i, t in enumerate(times):
        dt = datetime.fromisoformat(t)
        if dt < now_local.replace(minute=0, second=0, microsecond=0):
            continue
        if dt > deadline or len(lines) > MAX_ROWS:
            break
        prob = probs[i] if i < len(probs) else None
        code = codes[i] if i < len(codes) else None
        if prob is not None and prob >= 60:
            umbrella = True
        lines.append(_fmt_line(dt.strftime("%H:%M"), temps[i], WEATHER_CODES.get(code, ""), prob))
    return _finish(lines, umbrella)


def _symbol_ru(code: str) -> str:
    c = code or ""
    if "thunder" in c:
        return "Гроза ⛈"
    if "sleet" in c:
        return "Мокрый снег 🌨"
    if "snow" in c:
        return "Снег 🌨"
    if "rainshowers" in c:
        return "Ливни 🌧"
    if "rain" in c:
        return "Дождь 🌧"
    if "drizzle" in c:
        return "Морось 🌦"
    if "fog" in c:
        return "Туман 🌫"
    if "partlycloudy" in c:
        return "Переменная облачность ⛅"
    if "cloudy" in c:
        return "Пасмурно ☁️"
    if "fair" in c:
        return "Малооблачно 🌤"
    if "clearsky" in c:
        return "Ясно ☀️"
    return ""


async def _weather_metno(name: str, lat: float, lon: float, utc_off: int, hours: int) -> str:
    headers = {"User-Agent": "JarvisTelegramAssistant/1.0 (github.com/Dockik)"}
    async with httpx.AsyncClient(timeout=15, headers=headers) as c:
        resp = await c.get(
            "https://api.met.no/weatherapi/locationforecast/2.0/compact",
            params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
        )
        resp.raise_for_status()
    data = resp.json()

    now_local = (datetime.now(timezone.utc) + timedelta(seconds=utc_off)).replace(tzinfo=None)
    deadline = now_local + timedelta(hours=hours)

    lines = [f"🌤 <b>{name}</b>, ближайшие {hours} ч:"]
    umbrella = False
    for entry in data["properties"]["timeseries"]:
        dt = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
        dt = (dt + timedelta(seconds=utc_off)).replace(tzinfo=None)
        if dt < now_local.replace(minute=0, second=0, microsecond=0):
            continue
        if dt > deadline or len(lines) > MAX_ROWS:
            break
        details = entry["data"]["instant"]["details"]
        temp = details.get("air_temperature", 0)
        n1 = entry["data"].get("next_1_hours") or {}
        sym = (n1.get("summary") or {}).get("symbol_code", "")
        precip = (n1.get("details") or {}).get("precipitation_amount") or 0
        if precip >= 0.5:
            umbrella = True
        lines.append(_fmt_line(dt.strftime("%H:%M"), temp, _symbol_ru(sym), None))
    return _finish(lines, umbrella)


def _desc_ru(desc: str) -> str:
    d = desc.lower()
    if "thunder" in d:
        return "Гроза ⛈"
    if "snow" in d or "sleet" in d or "ice" in d:
        return "Снег 🌨"
    if "heavy rain" in d or "heavy shower" in d:
        return "Сильный дождь 🌧"
    if "shower" in d or "rain" in d:
        return "Дождь 🌧"
    if "drizzle" in d:
        return "Морось 🌦"
    if "fog" in d or "mist" in d or "haze" in d:
        return "Туман 🌫"
    if "overcast" in d or "cloud" in d:
        return "Облачно ☁️"
    if "sunny" in d or "clear" in d:
        return "Ясно ☀️"
    return desc


async def _weather_wttr(city: str, hours: int) -> str:
    hours = max(1, min(int(hours or 3), 24))
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "curl/8.0"}) as c:
        resp = await c.get(f"https://wttr.in/{city}", params={"format": "j1"})
        resp.raise_for_status()
    data = resp.json()

    areas = data.get("nearest_area") or []
    name = city
    if areas:
        name = areas[0].get("areaName", [{}])[0].get("value") or city

    # Локальное время места берём из времени наблюдения
    obs = data.get("current_condition", [{}])[0].get("localObsDateTime")
    try:
        local_now = datetime.strptime(obs, "%Y-%m-%d %I:%M %p")
    except (ValueError, TypeError):
        local_now = datetime.utcnow()
    deadline = local_now + timedelta(hours=hours)

    lines = [f"🌤 <b>{name}</b>, ближайшие {hours} ч:"]
    umbrella = False
    for day in data.get("weather", []):
        day_date = datetime.strptime(day["date"], "%Y-%m-%d")
        for slot in day.get("hourly", []):
            hhmm_raw = slot.get("time", "0").zfill(4)
            dt = day_date.replace(hour=int(hhmm_raw[:2]), minute=int(hhmm_raw[2:]))
            if dt < local_now.replace(minute=0, second=0, microsecond=0):
                continue
            if dt > deadline or len(lines) > MAX_ROWS:
                return _finish(lines, umbrella)
            prob = int(slot.get("chanceofrain") or 0)
            if prob >= 60:
                umbrella = True
            desc = _desc_ru(slot.get("weatherDesc", [{}])[0].get("value", ""))
            lines.append(_fmt_line(dt.strftime("%H:%M"), int(slot.get("tempC", 0)), desc, prob))
    return _finish(lines, umbrella)


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
