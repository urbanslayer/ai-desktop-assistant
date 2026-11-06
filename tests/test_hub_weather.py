"""Weather round: the built-in Open-Meteo weather tool + the Location setting.

No real network: hub.tools.weather.TRANSPORT is the injected fake (it sees
url/params/timeout exactly like the real path). A scripted fake LLM drives
the tool loop through /api/chat, like tests/test_hub_tools.py.
"""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from hub.server import create_app
from hub.tools import registry
from hub.tools import weather
from test_hub_tools import ScriptedLLM, call_step

LONDON_UK = {"id": 2643743, "name": "London", "latitude": 51.50853,
             "longitude": -0.12574, "country": "United Kingdom",
             "country_code": "GB", "admin1": "England",
             "timezone": "Europe/London"}
LONDON_CA = {"id": 6058560, "name": "London", "latitude": 42.98339,
             "longitude": -81.23304, "country": "Canada",
             "country_code": "CA", "admin1": "Ontario",
             "timezone": "America/Toronto"}
GLOUCESTER = {"id": 2647582, "name": "Gloucester", "latitude": 51.86772,
              "longitude": -2.24435, "country": "United Kingdom",
              "country_code": "GB", "admin1": "England",
              "timezone": "Europe/London"}
TOKYO = {"id": 1850147, "name": "Tokyo", "latitude": 35.6895,
         "longitude": 139.69171, "country": "Japan", "country_code": "JP",
         "admin1": "Tokyo", "timezone": "Asia/Tokyo"}
PLACES = {"london": [LONDON_UK, LONDON_CA], "gloucester, uk": [GLOUCESTER],
          "gloucester": [GLOUCESTER], "tokyo": [TOKYO]}


def _forecast(lat, temp="°C", wind="km/h", precip="mm", fdays=1):
    dates = [f"2026-11-{5 + i:02d}" for i in range(fdays)]
    hourly_time = [f"{d}T{h:02d}:00" for d in dates for h in range(24)]
    n = len(hourly_time)
    return {
        "timezone": "Europe/London", "utc_offset_seconds": 0,
        "current": {"time": "2026-11-05T15:30", "temperature_2m": 14.2,
                    "apparent_temperature": 12.1, "relative_humidity_2m": 81,
                    "weather_code": 61, "wind_speed_10m": 13.3,
                    "precipitation": 0.1, "is_day": 1},
        "current_units": {"temperature_2m": temp, "wind_speed_10m": wind,
                          "precipitation": precip},
        "daily": {"time": dates,
                  "weather_code": [61, 3] * fdays,
                  "temperature_2m_max": [15.1, 13.0] * fdays,
                  "temperature_2m_min": [9.2, 7.1] * fdays,
                  "precipitation_probability_max": [78, 20] * fdays,
                  "sunrise": [f"{d}T07:01" for d in dates],
                  "sunset": [f"{d}T16:26" for d in dates]},
        "hourly": {"time": hourly_time,
                   "temperature_2m": [14.0 - 0.1 * i for i in range(n)],
                   "precipitation_probability": [max(0, 70 - i) for i in range(n)],
                   "weather_code": ([61] * 6 + [3] * 18) * fdays},
        "elevation": 20.0, "latitude": lat,
    }


class FakeOpenMeteo:
    """Injected TRANSPORT: geocoding canned by query, forecast canned by
    lat. `timeouts` = how many upcoming calls raise a timeout (retry test)."""

    def __init__(self):
        self.calls = []
        self.timeouts = 0
        self.fail_geocode = False

    def __call__(self, url, params, timeout):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        if self.timeouts:
            self.timeouts -= 1
            raise weather.WeatherTimeout("fake timeout")
        if "geocoding" in url:
            if self.fail_geocode:
                raise weather.WeatherError("geocode down")
            key = str(params.get("name") or "").lower().strip()
            return {"results": [dict(r) for r in PLACES.get(key, [])]}
        units = str(params.get("temperature_unit") or "celsius")
        wind = str(params.get("wind_speed_unit") or "kmh")
        precip = str(params.get("precipitation_unit") or "mm")
        return _forecast(params.get("latitude"),
                         temp="°F" if units == "fahrenheit" else "°C",
                         wind={"kmh": "km/h", "mph": "mph", "ms": "m/s",
                               "kn": "kn"}[wind],
                         precip="in" if precip == "inch" else "mm",
                         fdays=max(1, int(params.get("forecast_days") or 1)))

    def forecast_calls(self):
        return [c for c in self.calls if "forecast" in c["url"]]

    def geocode_calls(self):
        return [c for c in self.calls if "geocoding" in c["url"]]


@pytest.fixture
def wx():
    fake = FakeOpenMeteo()
    weather.TRANSPORT = fake
    weather.clear_cache()
    yield fake
    weather.TRANSPORT = None
    weather.clear_cache()


SAVED_LONDON = {"name": "London", "latitude": 51.50853, "longitude": -0.12574,
                "timezone": "Europe/London", "country": "United Kingdom",
                "country_code": "GB", "admin1": "England"}


def make_client(tmp_path, steps=None, location_defaults=None,
                tools_defaults=None):
    llm = ScriptedLLM(steps or [{"content": "ok"}])
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     tools_defaults=tools_defaults or {},
                     location_defaults=location_defaults)
    return TestClient(app), llm


# ── geocode route ───────────────────────────────────────────────────────────

def test_geocode_route_proxies_and_validates(tmp_path, wx):
    client, _ = make_client(tmp_path)
    r = client.get("/api/geocode", params={"q": "London"})
    assert r.status_code == 200
    hits = r.json()["results"]
    assert [h["name"] for h in hits] == ["London", "London"]
    assert hits[0]["country"] == "United Kingdom"
    assert hits[0]["timezone"] == "Europe/London"
    call = wx.geocode_calls()[-1]
    assert call["timeout"] == 5.0 and call["params"]["count"] == 5
    assert client.get("/api/geocode").status_code == 422          # no q
    wx.fail_geocode = True
    weather.clear_cache()
    assert client.get("/api/geocode", params={"q": "Nowhere"}).status_code == 502


# ── location setting: save / clear / reset / env default / units ────────────

def test_location_save_clear_and_reset(tmp_path, wx):
    client, _ = make_client(tmp_path)
    st = client.get("/api/settings").json()
    assert st["location"] == {"name": ""}
    assert st["location_source"]["latitude"] == "default"

    r = client.put("/api/settings", json={"location": SAVED_LONDON})
    assert r.status_code == 200
    got = r.json()["location"]
    assert got["latitude"] == 51.50853 and got["admin1"] == "England"
    assert got["country_code"] == "GB"          # drives the auto unit preset
    assert r.json()["location_source"]["name"] == "saved"
    assert client.get("/api/settings").json()["location"]["timezone"] == "Europe/London"

    # blank name = unset -> no location again
    r = client.put("/api/settings", json={"location": {"name": ""}})
    assert r.json()["location"] == {"name": ""}

    client.put("/api/settings", json={"location": SAVED_LONDON})
    r = client.put("/api/settings", json={"location": None})
    assert r.json()["location"] == {"name": ""}
    r = client.post("/api/settings/reset", json={"location": True})
    assert r.status_code == 200

    for bad in ({"location": 5},
                {"location": {"name": "x"}},                       # no coords
                {"location": {"name": "x", "latitude": 999, "longitude": 0}},
                {"location": {"name": "x", "latitude": "n", "longitude": 1}}):
        assert client.put("/api/settings", json=bad).status_code == 422


def test_location_env_default_shown_but_lazy(tmp_path, wx):
    client, _ = make_client(tmp_path, location_defaults={"place": "Gloucester, UK"})
    st = client.get("/api/settings").json()
    assert st["location"]["name"] == "Gloucester, UK"
    assert st["location_source"]["name"] == "env"
    assert st["defaults"]["location"]["name"] == "Gloucester, UK"
    assert wx.calls == []          # GET /api/settings never geocodes
    # a saved place beats the env default; clearing returns to it
    client.put("/api/settings", json={"location": SAVED_LONDON})
    assert client.get("/api/settings").json()["location_source"]["name"] == "saved"
    client.put("/api/settings", json={"location": {"name": ""}})
    assert client.get("/api/settings").json()["location_source"]["name"] == "env"


def test_weather_units_setting(tmp_path, wx):
    client, _ = make_client(tmp_path)
    st = client.get("/api/settings").json()
    assert st["tools"]["units"] == "auto"                  # preset default
    assert st["tools"]["units_wind"] == ""                 # blank = from preset
    assert st["tools_source"]["units"] == "default"
    assert st["tools_source"]["units_wind"] == "default"
    for good in ("metric", "uk", "us"):
        assert client.put("/api/settings", json={"tools": {"units": good}})\
            .json()["tools"]["units"] == good
    # legacy imperial is accepted and saved as us
    r = client.put("/api/settings", json={"tools": {"units": "imperial"}})
    assert r.json()["tools"]["units"] == "us"
    assert r.json()["tools_source"]["units"] == "saved"
    assert client.put("/api/settings", json={"tools": {"units": "kelvin"}})\
        .status_code == 422
    assert client.put("/api/settings", json={"tools": {"units": ""}})\
        .json()["tools"]["units"] == "auto"    # blank = default again
    # per-quantity overrides: set, blank-back-to-preset, invalid
    r = client.put("/api/settings", json={"tools": {
        "units_temperature": "fahrenheit", "units_wind": "kn",
        "units_precipitation": "inch"}})
    assert r.json()["tools"]["units_temperature"] == "fahrenheit"
    assert r.json()["tools"]["units_wind"] == "kn"
    assert r.json()["tools"]["units_precipitation"] == "inch"
    assert r.json()["tools_source"]["units_wind"] == "saved"
    assert client.put("/api/settings",
                      json={"tools": {"units_wind": "barns"}}).status_code == 422
    assert client.put("/api/settings",
                      json={"tools": {"units_precipitation": "hands"}})\
        .status_code == 422
    assert client.put("/api/settings", json={"tools": {"units_wind": ""}})\
        .json()["tools"]["units_wind"] == ""   # blank = from the preset
    # reset drops every units override back to the defaults
    client.put("/api/settings", json={"tools": {"units": "uk",
                                                "units_wind": "ms"}})
    r = client.post("/api/settings/reset", json={"tools": True})
    assert r.json()["tools"]["units"] == "auto"
    assert r.json()["tools"]["units_wind"] == ""


def test_weather_units_env_default(tmp_path, wx):
    # RIKO_UNITS keeps working through tools_defaults (main() normalizes it)
    client, _ = make_client(tmp_path, tools_defaults={"units": "imperial"})
    assert client.get("/api/settings").json()["tools"]["units"] == "us"
    client, _ = make_client(tmp_path, tools_defaults={"units": "uk"})
    st = client.get("/api/settings").json()
    assert st["tools"]["units"] == "uk"
    assert st["defaults"]["tools"]["units"] == "uk"


def test_units_effective(tmp_path, wx):
    client, _ = make_client(tmp_path)
    # no location + auto -> metric
    assert client.get("/api/settings").json()["units_effective"] == \
        {"preset": "metric", "temperature": "°C", "wind": "km/h",
         "precipitation": "mm"}
    # auto follows the SAVED place's country: GB -> uk (°C + mph)
    client.put("/api/settings", json={"location": SAVED_LONDON})
    assert client.get("/api/settings").json()["units_effective"] == \
        {"preset": "uk", "temperature": "°C", "wind": "mph",
         "precipitation": "mm"}
    # a per-quantity override shows up in the resolved line
    client.put("/api/settings", json={"tools": {"units_wind": "ms"}})
    assert client.get("/api/settings").json()["units_effective"]["wind"] == "m/s"
    # an explicit preset ignores the country
    client.put("/api/settings", json={"tools": {"units": "us", "units_wind": ""}})
    assert client.get("/api/settings").json()["units_effective"]["preset"] == "us"
    # GET still never geocodes (the lazy env default reads as metric)
    client2, _ = make_client(tmp_path / "b", location_defaults={"place": "Gloucester, UK"})
    assert client2.get("/api/settings").json()["units_effective"]["preset"] == "metric"
    assert wx.calls == []


# ── preset resolution (pure) ────────────────────────────────────────────────

def test_unit_preset_tables():
    assert weather.UNIT_PRESETS["metric"] == {"temperature": "celsius",
                                              "wind": "kmh", "precipitation": "mm"}
    assert weather.UNIT_PRESETS["uk"] == {"temperature": "celsius",
                                          "wind": "mph", "precipitation": "mm"}
    assert weather.UNIT_PRESETS["us"] == {"temperature": "fahrenheit",
                                          "wind": "mph", "precipitation": "inch"}


def test_resolve_units_presets_and_auto_by_country():
    assert weather.resolve_units("metric")["wind"] == "kmh"
    assert weather.resolve_units("uk") == {"preset": "uk", "temperature": "celsius",
                                           "wind": "mph", "precipitation": "mm"}
    assert weather.resolve_units("imperial")["preset"] == "us"   # legacy alias
    assert weather.resolve_units("kelvin")["preset"] == "metric"  # junk -> auto, no loc
    gb = {"country_code": "GB"}
    assert weather.resolve_units("auto", loc=gb)["preset"] == "uk"
    for cc in ("US", "LR", "MM"):
        assert weather.resolve_units("auto", loc={"country_code": cc})["preset"] == "us"
    # Ireland drives in km/h -> metric; any other country too
    assert weather.resolve_units("auto", loc={"country_code": "IE"})["preset"] == "metric"
    assert weather.resolve_units("auto", loc={"country_code": "JP"})["preset"] == "metric"
    assert weather.resolve_units("auto", loc=None)["preset"] == "metric"
    # country-name fallback for places saved before country_code existed
    assert weather.resolve_units("auto", loc={"country": "United Kingdom"})["preset"] == "uk"


def test_resolve_units_overrides():
    r = weather.resolve_units("uk", {"units_temperature": "fahrenheit"})
    assert r == {"preset": "uk", "temperature": "fahrenheit", "wind": "mph",
                 "precipitation": "mm"}
    # blank or invalid overrides fall back to the preset quantity
    r = weather.resolve_units("metric", {"units_wind": "", "units_precipitation": "hands"})
    assert r["wind"] == "kmh" and r["precipitation"] == "mm"
    for w in ("kmh", "mph", "ms", "kn"):
        assert weather.resolve_units("metric", {"units_wind": w})["wind"] == w


# ── the tool itself ─────────────────────────────────────────────────────────

def weather_spec(ctx=None):
    specs = registry.build_hub_specs({"enabled": True, "units": "metric"},
                                     location_ctx=ctx or {})
    return next(s for s in specs if s.name == "weather")


def payload_of(tool_msg: dict) -> dict:
    """toolloop wraps results with an untrusted-data preamble line."""
    c = tool_msg["content"]
    return json.loads(c[c.index("\n") + 1:])


def test_wmo_words_mapping():
    assert weather.wmo_words(0) == "clear sky"
    assert weather.wmo_words(3) == "overcast"
    assert weather.wmo_words(61) == "light rain"
    assert weather.wmo_words(95) == "thunderstorm"
    assert weather.wmo_words(42) == "weather code 42"
    assert weather.wmo_words(None) == "unknown conditions"


def test_weather_saved_place_and_shape(tmp_path, wx):
    ctx = {"resolve": lambda: dict(SAVED_LONDON), "units": "metric",
           "label_name": lambda: "London"}
    out = weather_spec(ctx).run({"days": 2})
    assert out["place"]["name"] == "London"
    assert out["place"]["country"] == "United Kingdom"
    assert out["local_time"] == "2026-11-05T15:30"
    cur = out["current"]
    assert cur["temp_c"] == 14.2 and cur["feels_like_c"] == 12.1
    assert cur["wind_kmh"] == 13.3 and cur["humidity_pct"] == 81
    assert cur["condition"] == "light rain" and cur["is_day"] is True
    assert len(out["days"]) == 2
    assert out["days"][0]["rain_chance_pct"] == 78
    assert out["days"][0]["sunrise"] == "2026-11-05T07:01"
    assert out["days"][1]["condition"] == "overcast"
    assert "hourly" not in out
    call = wx.forecast_calls()[-1]
    assert call["params"]["latitude"] == round(51.50853, 4)
    assert call["timeout"] == 6.0 and call["params"]["timezone"] == "auto"
    # progress label + permission policy
    assert weather_spec(ctx).label({}) == "checking the weather in London"
    assert weather_spec(ctx).label({"place": "Tokyo"}) == "checking the weather in Tokyo"
    assert registry.permission_for("weather", "hub", "read", {}) == "always"


def test_weather_explicit_place_resolves_and_names(tmp_path, wx):
    out = weather_spec({"units": "metric"}).run({"place": "Tokyo"})
    assert out["place"]["name"] == "Tokyo" and out["place"]["country"] == "Japan"
    assert wx.forecast_calls()[-1]["params"]["latitude"] == round(35.6895, 4)


def test_weather_env_default_geocoded_lazily(tmp_path, wx):
    ctx = {"resolve": lambda: weather.geocode_first("Gloucester, UK"),
           "units": "metric", "label_name": lambda: "Gloucester, UK"}
    out = weather_spec(ctx).run({})
    assert out["place"]["name"] == "Gloucester"
    assert wx.geocode_calls()[-1]["params"]["name"] == "Gloucester, UK"


def test_weather_no_location_error(tmp_path, wx):
    with pytest.raises(weather.WeatherError) as e:
        weather_spec({}).run({})
    assert "no location" in str(e.value) and "Settings" in str(e.value)
    with pytest.raises(weather.WeatherError):
        weather_spec({}).run({"place": "Atlantis"})   # geocode finds nothing


def test_weather_cache_hit(tmp_path, wx):
    ctx = {"resolve": lambda: dict(SAVED_LONDON), "units": "metric"}
    weather_spec(ctx).run({"days": 2})
    weather_spec(ctx).run({"days": 2})
    assert len(wx.forecast_calls()) == 1
    weather_spec(ctx).run({"days": 2, "hourly": True})   # different key
    assert len(wx.forecast_calls()) == 2


def test_weather_timeout_retries_once_then_succeeds(tmp_path, wx):
    wx.timeouts = 1
    out = weather_spec({"resolve": lambda: dict(SAVED_LONDON),
                        "units": "metric"}).run({})
    assert out["current"]["temp_c"] == 14.2
    assert len(wx.forecast_calls()) == 2
    wx.timeouts = 99
    with pytest.raises(weather.WeatherTimeout):
        weather.clear_cache()
        weather_spec({"resolve": lambda: dict(SAVED_LONDON),
                      "units": "metric"}).run({"days": 3})


def test_weather_unit_presets_call_openmeteo(tmp_path, wx):
    # the resolved units travel as Open-Meteo's own params, no conversion
    out = weather_spec({"resolve": lambda: dict(SAVED_LONDON),
                        "units": "uk"}).run({})
    call = wx.forecast_calls()[-1]
    assert call["params"]["temperature_unit"] == "celsius"
    assert call["params"]["wind_speed_unit"] == "mph"
    assert call["params"]["precipitation_unit"] == "mm"
    assert out["units"] == {"preset": "uk", "temperature": "°C",
                            "wind": "mph", "precipitation": "mm"}
    assert "temp_c" in out["current"] and "wind_mph" in out["current"]
    assert "precipitation_mm" in out["current"] and "max_c" in out["days"][0]

    out = weather_spec({"resolve": lambda: dict(SAVED_LONDON),
                        "units": "us"}).run({})
    call = wx.forecast_calls()[-1]
    assert call["params"]["temperature_unit"] == "fahrenheit"
    assert call["params"]["wind_speed_unit"] == "mph"
    assert call["params"]["precipitation_unit"] == "inch"
    assert out["units"]["wind"] == "mph" and out["units"]["temperature"] == "°F"
    assert "temp_f" in out["current"] and "precipitation_in" in out["current"]

    # legacy "imperial" answers exactly like us
    out = weather_spec({"resolve": lambda: dict(SAVED_LONDON),
                        "units": "imperial"}).run({})
    assert out["units"]["preset"] == "us" and "temp_f" in out["current"]


def test_weather_units_overrides_win_over_preset(tmp_path, wx):
    ctx = {"resolve": lambda: dict(SAVED_LONDON), "units": "metric",
           "units_temperature": "fahrenheit", "units_wind": "kn",
           "units_precipitation": ""}
    out = weather_spec(ctx).run({})
    call = wx.forecast_calls()[-1]
    assert call["params"]["temperature_unit"] == "fahrenheit"
    assert call["params"]["wind_speed_unit"] == "kn"
    assert call["params"]["precipitation_unit"] == "mm"   # blank = from metric
    assert "wind_kn" in out["current"] and "temp_f" in out["current"]
    # m/s through an override
    ctx2 = dict(ctx, units_wind="ms", units_temperature="", units_precipitation="")
    out = weather_spec(ctx2).run({"days": 1, "hourly": False})
    assert "wind_ms" in out["current"]
    assert out["units"] == {"preset": "metric", "temperature": "°C",
                            "wind": "m/s", "precipitation": "mm"}


def test_weather_auto_follows_place_country(tmp_path, wx):
    auto = {"resolve": lambda: dict(SAVED_LONDON), "units": "auto"}
    out = weather_spec(auto).run({})                    # saved GB place -> uk
    assert wx.forecast_calls()[-1]["params"]["wind_speed_unit"] == "mph"
    assert out["units"]["preset"] == "uk"

    tokyo = weather_spec({"units": "auto"}).run({"place": "Tokyo"})
    assert wx.forecast_calls()[-1]["params"]["wind_speed_unit"] == "kmh"
    assert tokyo["units"]["preset"] == "metric"         # explicit place decides

    # a place with no country info at all falls back to metric
    plain = {"resolve": lambda: {"name": "Nowhere", "latitude": 1.0,
                                 "longitude": 2.0}}
    assert weather_spec(dict(plain, units="auto")).run({})["units"]["preset"] == "metric"


def test_weather_units_in_cache_key(tmp_path, wx):
    # metric vs uk differ ONLY in the wind unit — still two cache entries
    loc = {"resolve": lambda: dict(SAVED_LONDON)}
    weather_spec(dict(loc, units="metric")).run({})
    weather_spec(dict(loc, units="metric")).run({})
    assert len(wx.forecast_calls()) == 1
    weather_spec(dict(loc, units="uk")).run({})
    assert len(wx.forecast_calls()) == 2
    # and an override that matches another preset's wind is still keyed apart
    weather_spec(dict(loc, units="metric", units_wind="mph")).run({})
    assert len(wx.forecast_calls()) == 2   # same resolved units as uk -> hit


def test_weather_hourly_next_12h(tmp_path, wx):
    out = weather_spec({"resolve": lambda: dict(SAVED_LONDON),
                        "units": "metric"}).run({"hourly": True})
    assert len(out["hourly"]) == 12
    assert out["hourly"][0]["time"] == "2026-11-05T15:00"
    assert out["hourly"][0]["rain_chance_pct"] == 55   # fake ramp 70-i, i=15


# ── the tool through /api/chat: prompt line, label, log ─────────────────────

def test_chat_weather_roundtrip_and_prompt(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        call_step("weather", {"days": 2}),
        {"content": "Light rain, 14 degrees Celsius, senpai."}])
    assert client.put("/api/settings", json={"location": SAVED_LONDON}).status_code == 200
    r = client.post("/api/chat", json={"text": "what's the weather like?"})
    assert r.status_code == 200
    sysmsg = llm.chat_requests[0]["messages"][0]["content"]
    assert "The user lives in London (England, United Kingdom)" in sysmsg
    # the hub pre-fetched weather BEFORE the model: data + instruction are
    # already in the first request (docs/API.md "Weather pre-fetch")
    assert "Live weather data (fetched just now for London" in sysmsg
    assert "do not call the weather tool again" in sysmsg
    assert "weather" in [t["function"]["name"] for t in llm.chat_requests[0]["tools"]]
    # settings.timezone blank -> the local-time line follows the location zone
    assert "(Europe/London)" in sysmsg
    tool_msgs = [m for m in llm.chat_requests[1]["messages"] if m["role"] == "tool"]
    payload = payload_of(tool_msgs[0])
    assert payload["place"]["name"] == "London"
    assert payload["current"]["condition"] == "light rain"
    out = r.json()
    # entry 0: the hub's own pre-fetch (auto); entry 1: this scripted model
    # calling the tool anyway despite the injected data
    assert out["tools_used"] == [
        {"tool": "weather", "label": "checking the weather in London",
         "source": "hub", "auto": True},
        {"tool": "weather", "label": "checking the weather in London",
         "args": {"days": 2}}]
    log = client.get("/api/tools/log", params={"limit": 5}).json()["entries"]
    assert log[0]["tool"] == "weather" and log[0]["ok"] is True


def test_chat_weather_other_place_and_no_location(tmp_path, wx):
    client, llm = make_client(tmp_path, [
        call_step("weather", {"place": "Tokyo"}), {"content": "sunny"}])
    r = client.post("/api/chat", json={"text": "what's the weather in Tokyo?"})
    assert r.status_code == 200
    payload = payload_of([m for m in llm.chat_requests[1]["messages"]
                          if m["role"] == "tool"][0])
    assert payload["place"]["name"] == "Tokyo"   # she can say which place
    assert "The user lives in" not in llm.chat_requests[0]["messages"][0]["content"]
    assert "no location set" not in json.dumps(r.json())

    # and with no location at all the error reaches the model, not a 500
    client2, llm2 = make_client(tmp_path / "b", [
        call_step("weather", {}), {"content": "where are we?"}])
    r2 = client2.post("/api/chat", json={"text": "what's the weather like?"})
    assert r2.status_code == 200
    err = payload_of([m for m in llm2.chat_requests[1]["messages"]
                      if m["role"] == "tool"][0])
    assert "no location" in err["error"] and "Settings" in err["error"]


def test_chat_env_default_prompt_line(tmp_path, wx):
    client, llm = make_client(tmp_path, [{"content": "ok"}],
                              location_defaults={"place": "Gloucester, UK"})
    assert client.post("/api/chat", json={"text": "hi"}).status_code == 200
    assert "The user lives in Gloucester (England, United Kingdom)" in \
        llm.chat_requests[0]["messages"][0]["content"]
