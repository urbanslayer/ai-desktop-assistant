"""Built-in weather: Open-Meteo geocoding + forecast, no key, no subprocess.

Why built-in instead of an MCP server (owner ask "identify the simplest
weather MCP that is reliable"): the most-used Open-Meteo MCP
(isdaniel/mcp_weather_server) currently crashes on startup against the mcp
SDK 2.x and pins nothing, the IBM Open-Meteo MCP is archived, and the rest
are tiny/unmaintained — while Open-Meteo's own HTTP APIs are free, keyless
and answer in ~0.3 s. Same shape as web_search/fetch_page: one stdlib +
requests module the hub calls directly, no SDK coupling.

Endpoints (docs/API.md "Weather"):
  geocoding https://geocoding-api.open-meteo.com/v1/search?name=..&count=5
  forecast  https://api.open-meteo.com/v1/forecast?..&timezone=auto

Timeouts: 5 s geocode, 6 s forecast with ONE retry (Open-Meteo is fast; a
timeout is transient). Results are cached in memory for 10 minutes — geocode
per query, forecast per (lat, lon, days, hourly, units) — so repeated
questions in one conversation are instant.

`TRANSPORT` is the test seam: set it to fn(url, params, timeout) -> dict
(raising WeatherTimeout / WeatherError) and no real network is touched.
"""
import threading
import time

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "riko-hub/1 weather"
GEOCODE_TIMEOUT = 5.0
WEATHER_TIMEOUT = 6.0
CACHE_TTL = 600.0          # 10 min — the forecast is 15-min-resolution anyway
MIN_DAYS, MAX_DAYS = 1, 7

# test seam (see module docstring); None = the real requests.get
TRANSPORT = None

_cache_lock = threading.Lock()
_geo_cache: dict[str, tuple[float, list]] = {}
_fc_cache: dict[tuple, tuple[float, dict]] = {}


class WeatherError(RuntimeError):
    """Anything Open-Meteo-side that the model should be told about."""


class WeatherTimeout(WeatherError):
    """Request timed out — retryable once before giving up."""


def clear_cache() -> None:
    with _cache_lock:
        _geo_cache.clear()
        _fc_cache.clear()


def _http_json(url: str, params: dict, timeout: float) -> dict:
    if TRANSPORT is not None:
        return TRANSPORT(url, params, timeout)
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={"User-Agent": USER_AGENT})
    except requests.Timeout as e:
        raise WeatherTimeout(f"Open-Meteo did not answer within {timeout:.0f} s") from e
    except requests.RequestException as e:
        raise WeatherError(f"Open-Meteo request failed ({e})") from e
    if r.status_code != 200:
        raise WeatherError(f"Open-Meteo answered HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise WeatherError("Open-Meteo returned non-JSON") from e


# ── geocoding ───────────────────────────────────────────────────────────────
def geocode(query: str, limit: int = 5) -> list[dict]:
    """Up to `limit` matches [{name, latitude, longitude, country,
    country_code, admin1, timezone}] (best first). Empty list = no match."""
    q = str(query or "").strip()
    if not q:
        raise WeatherError("empty place name")
    key = q.lower()
    now = time.monotonic()
    with _cache_lock:
        hit = _geo_cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return [dict(r) for r in hit[1]]
    data = _http_json(GEOCODE_URL, {"name": q, "count": 5, "language": "en"},
                      GEOCODE_TIMEOUT)
    out = []
    for row in (data.get("results") or [])[:max(1, min(5, int(limit)))]:
        out.append({"name": str(row.get("name") or "").strip(),
                    "latitude": row.get("latitude"),
                    "longitude": row.get("longitude"),
                    "country": str(row.get("country") or "").strip(),
                    "country_code": str(row.get("country_code") or "").strip(),
                    "admin1": str(row.get("admin1") or "").strip(),
                    "timezone": str(row.get("timezone") or "").strip()})
    with _cache_lock:
        if out:
            _geo_cache[key] = (now, [dict(r) for r in out])
    return out


def geocode_first(query: str) -> dict:
    hits = geocode(query)
    if not hits:
        raise WeatherError(f"no place found matching “{query}”")
    return hits[0]


# ── unit presets (docs/API.md "Weather") ────────────────────────────────────
# Settings `tools.units` is a PRESET; per-quantity overrides
# (`tools.units_temperature` etc.) win over it. Open-Meteo takes these unit
# names as-is (temperature_unit / wind_speed_unit / precipitation_unit), so
# no client-side conversion anywhere.
UNIT_PRESETS = {
    "metric": {"temperature": "celsius", "wind": "kmh", "precipitation": "mm"},
    "uk":     {"temperature": "celsius", "wind": "mph", "precipitation": "mm"},
    "us":     {"temperature": "fahrenheit", "wind": "mph", "precipitation": "inch"},
}
UNIT_PRESETS["imperial"] = UNIT_PRESETS["us"]   # legacy alias = us
VALID_QUANTITY_UNITS = {"temperature": ("celsius", "fahrenheit"),
                        "wind": ("kmh", "mph", "ms", "kn"),
                        "precipitation": ("mm", "inch")}
UNIT_SYMBOLS = {"celsius": "°C", "fahrenheit": "°F",
                "kmh": "km/h", "mph": "mph", "ms": "m/s", "kn": "kn",
                "mm": "mm", "inch": "in"}
# `auto` country mapping: GB only — Ireland drives in km/h, so it stays
# metric. Liberia + Myanmar are the customary US-units holdouts.
UK_COUNTRIES = {"GB"}
US_COUNTRIES = {"US", "LR", "MM"}
# country-name fallback for places saved before country_code was stored
_COUNTRY_NAME_TO_CC = {"united kingdom": "GB", "great britain": "GB",
                       "united states": "US", "united states of america": "US",
                       "liberia": "LR", "myanmar": "MM"}


def normalize_preset(value) -> str:
    """Saved/env preset -> auto|metric|uk|us (legacy imperial -> us; anything
    unknown -> auto, the default)."""
    v = str(value or "").strip().lower()
    if v == "imperial":
        return "us"
    return v if v in ("auto", "metric", "uk", "us") else "auto"


def country_code_of(loc: dict | None) -> str:
    loc = loc if isinstance(loc, dict) else {}
    cc = str(loc.get("country_code") or "").strip().upper()
    if len(cc) == 2:
        return cc
    return _COUNTRY_NAME_TO_CC.get(
        str(loc.get("country") or "").strip().lower(), "")


def preset_for_country(cc: str) -> str:
    if str(cc or "").strip().upper() in UK_COUNTRIES:
        return "uk"
    if str(cc or "").strip().upper() in US_COUNTRIES:
        return "us"
    return "metric"


def resolve_units(preset, overrides: dict | None = None,
                  loc: dict | None = None) -> dict:
    """{preset, temperature, wind, precipitation} — the concrete units the
    forecast call uses. `auto` picks the preset from the location's country
    (no location -> metric); a per-quantity override (blank/invalid = from
    the preset) replaces just that quantity."""
    p = normalize_preset(preset)
    if p == "auto":
        p = preset_for_country(country_code_of(loc))
    base = UNIT_PRESETS[p]
    ov = overrides if isinstance(overrides, dict) else {}
    out = {"preset": p}
    for qty, valid in VALID_QUANTITY_UNITS.items():
        v = str(ov.get("units_" + qty) or ov.get(qty) or "").strip().lower()
        out[qty] = v if v in valid else base[qty]
    return out


def unit_symbols(resolved: dict) -> dict:
    """Payload-facing units block: which preset answered + unit symbols."""
    return {"preset": str(resolved.get("preset") or "metric"),
            "temperature": UNIT_SYMBOLS.get(str(resolved.get("temperature") or ""), "?"),
            "wind": UNIT_SYMBOLS.get(str(resolved.get("wind") or ""), "?"),
            "precipitation": UNIT_SYMBOLS.get(str(resolved.get("precipitation") or ""), "?")}


# ── forecast ────────────────────────────────────────────────────────────────
def forecast(lat, lon, days: int = 1, hourly: bool = False,
             units: dict | str = "metric") -> dict:
    """Raw Open-Meteo response (current + daily [+ hourly], local time).
    `units` is a resolved dict from resolve_units() (or a preset string —
    normalized here). Cached for CACHE_TTL per (lat, lon, days, hourly,
    temperature, wind, precipitation); one retry on timeout."""
    if not isinstance(units, dict):
        units = resolve_units(units)
    days = max(MIN_DAYS, min(MAX_DAYS, int(days)))
    key = (round(float(lat), 4), round(float(lon), 4), days, bool(hourly),
           units.get("temperature", "celsius"), units.get("wind", "kmh"),
           units.get("precipitation", "mm"))
    now = time.monotonic()
    with _cache_lock:
        hit = _fc_cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    params = {
        "latitude": key[0], "longitude": key[1],
        "current": ("temperature_2m,apparent_temperature,relative_humidity_2m,"
                    "weather_code,wind_speed_10m,precipitation,is_day"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max,sunrise,sunset"),
        "timezone": "auto",
        # an hourly summary asked late in the day needs the NEXT day's rows
        # too, or "the next 12 hours" runs out of data; weather_tool trims
        # the daily block back to `days`
        "forecast_days": min(MAX_DAYS, days + 1) if hourly else days,
    }
    if hourly:
        params["hourly"] = "temperature_2m,precipitation_probability,weather_code"
    # Open-Meteo's own unit params (no client-side conversion): its names
    # are exactly our unit keys (celsius|fahrenheit, kmh|mph|ms|kn, mm|inch)
    params.update({"temperature_unit": key[4],
                   "wind_speed_unit": key[5],
                   "precipitation_unit": key[6]})
    try:
        data = _http_json(FORECAST_URL, params, WEATHER_TIMEOUT)
    except WeatherTimeout:
        data = _http_json(FORECAST_URL, params, WEATHER_TIMEOUT)  # one retry
    with _cache_lock:
        _fc_cache[key] = (now, data)
    return data


# ── WMO weather codes → plain words ─────────────────────────────────────────
WMO_WORDS = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}


def wmo_words(code) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "unknown conditions"
    return WMO_WORDS.get(c, f"weather code {c}")


# ── schemas + the tool ──────────────────────────────────────────────────────
WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "place": {"type": "string",
                  "description": "place name to ask about (e.g. 'Tokyo'); "
                                 "omit to use the location saved in Settings"},
        "days": {"type": "integer",
                 "description": f"days to include, {MIN_DAYS}-{MAX_DAYS} "
                                "(default 1 = today; 2 includes tomorrow)"},
        "hourly": {"type": "boolean",
                   "description": "true adds a next-12-hours summary"},
    },
}

NO_LOCATION_MSG = ("no location set: ask the user where they are and pass it "
                   "as place, or have the owner save a home place on the "
                   "hub web page (Settings → Location)")


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else round(f, 1)


def weather_tool(args: dict, location_ctx: dict | None = None) -> dict:
    """Compact weather payload (docs/API.md "Weather"). Raises
    WeatherError with a model-actionable message.

    location_ctx (from the hub server): {resolve: () -> saved-or-env
    location dict | None, units: preset (auto|metric|uk|us),
    units_temperature/units_wind/units_precipitation: per-quantity
    overrides, blank = from the preset}.
    """
    ctx = location_ctx or {}
    args = args if isinstance(args, dict) else {}
    place = str(args.get("place") or "").strip()
    try:
        days = int(args.get("days") or 1)
    except (TypeError, ValueError):
        days = 1
    days = max(MIN_DAYS, min(MAX_DAYS, days))
    hourly = bool(args.get("hourly"))

    if place:
        loc = geocode_first(place)
    else:
        resolve = ctx.get("resolve")
        loc = None
        if resolve is not None:
            try:
                loc = resolve()
            except WeatherError:
                loc = None
        if not loc or loc.get("latitude") is None or loc.get("longitude") is None:
            raise WeatherError(NO_LOCATION_MSG)

    # `auto` follows the country of the place actually being answered for
    # (an explicit `place` overrides the saved one, so it decides too)
    units = resolve_units(ctx.get("units"), {
        "units_temperature": ctx.get("units_temperature"),
        "units_wind": ctx.get("units_wind"),
        "units_precipitation": ctx.get("units_precipitation"),
    }, loc)
    data = forecast(loc["latitude"], loc["longitude"], days, hourly, units)
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    t_suffix = {"celsius": "c", "fahrenheit": "f"}[units["temperature"]]
    p_suffix = {"mm": "mm", "inch": "in"}[units["precipitation"]]
    w_suffix = units["wind"]           # kmh | mph | ms | kn
    out: dict = {
        "place": {"name": loc.get("name") or "",
                  "admin1": loc.get("admin1") or "",
                  "country": loc.get("country") or "",
                  "latitude": loc.get("latitude"),
                  "longitude": loc.get("longitude"),
                  "timezone": data.get("timezone") or loc.get("timezone") or ""},
        "local_time": cur.get("time") or "",
        "units": unit_symbols(units),
        "current": {
            f"temp_{t_suffix}": _num(cur.get("temperature_2m")),
            f"feels_like_{t_suffix}": _num(cur.get("apparent_temperature")),
            "humidity_pct": _num(cur.get("relative_humidity_2m")),
            f"wind_{w_suffix}": _num(cur.get("wind_speed_10m")),
            f"precipitation_{p_suffix}": _num(cur.get("precipitation")),
            "condition": wmo_words(cur.get("weather_code")),
            "is_day": bool(cur.get("is_day")),
        },
        "days": [],
    }
    times = daily.get("time") or []
    for i in range(min(len(times), days)):   # an hourly fetch may carry one extra day
        out["days"].append({
            "date": times[i],
            f"min_{t_suffix}": _num((daily.get("temperature_2m_min") or [None] * (i + 1))[i]),
            f"max_{t_suffix}": _num((daily.get("temperature_2m_max") or [None] * (i + 1))[i]),
            "rain_chance_pct": _num((daily.get("precipitation_probability_max")
                                     or [None] * (i + 1))[i]),
            "condition": wmo_words((daily.get("weather_code") or [None] * (i + 1))[i]),
            "sunrise": (daily.get("sunrise") or [None] * (i + 1))[i],
            "sunset": (daily.get("sunset") or [None] * (i + 1))[i],
        })
    if hourly:
        h = data.get("hourly") or {}
        htimes = h.get("time") or []
        start = str(cur.get("time") or "")[:13]   # "YYYY-MM-DDTHH" local
        idx = next((j for j, t in enumerate(htimes) if str(t)[:13] >= start), 0)
        out["hourly"] = []
        for j in range(idx, min(idx + 12, len(htimes))):
            out["hourly"].append({
                "time": htimes[j],
                f"temp_{t_suffix}": _num((h.get("temperature_2m") or [None] * (j + 1))[j]),
                "rain_chance_pct": _num((h.get("precipitation_probability")
                                         or [None] * (j + 1))[j]),
                "condition": wmo_words((h.get("weather_code") or [None] * (j + 1))[j]),
            })
    return out


def location_prompt(loc: dict) -> str:
    """One system-prompt line: the owner's home place (docs/API.md Weather)."""
    name = str(loc.get("name") or "").strip()
    bits = ", ".join(b for b in (str(loc.get("admin1") or "").strip(),
                                 str(loc.get("country") or "").strip()) if b)
    where = f"{name} ({bits})" if bits else name
    return (f"The user lives in {where}; use it for weather, time and local "
            "questions unless they name another place.")
