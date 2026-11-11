"""One-shot timers (docs/TIMERS.md H): parsing, store/migration, scheduler
with an injected clock, cancel_on hooks, the tools through the real chat
loop, the routes, events payloads and the timezone setting.

No test sleeps longer than 1 s: every clock is injected (scheduler.tick()
driven directly) and the fire loop only runs as a thread where the test
explicitly asks for one.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from hub.events import EventBus
from hub.scheduler import TimerPermanent, TimerScheduler
from hub.server import create_app
from hub.store import Store
from hub.tools import timers as T
from test_hub_tools import ScriptedLLM, call_step  # shared fake LLM

from fastapi.testclient import TestClient

UTC = timezone.utc
NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── parsing (contract §Behaviour/Times + caps) ──────────────────────────────

@pytest.mark.parametrize("raw,secs", [
    ("10s", 10), ("+20s", 20), ("5m", 300), ("1h", 3600), ("2d", 172800),
    ("in 10 seconds", 10), ("5 minutes", 300), ("2 hours", 7200),
    ("30 seconds from now", 30), ("1h30m", 5400), ("in 1 day", 86400),
])
def test_parse_relative_forms(raw, secs):
    assert T.parse_timer_time(raw, NOW) == NOW + timedelta(seconds=secs)


def test_parse_rfc3339_with_explicit_timezone():
    assert T.parse_timer_time("2026-11-05T14:30:00Z", NOW) == \
        datetime(2026, 11, 5, 14, 30, tzinfo=UTC)
    assert T.parse_timer_time("2026-11-05T14:30:00+01:00", NOW) == \
        datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    assert T.parse_timer_time("2026-11-05T14:30:00-0500", NOW) == \
        datetime(2026, 11, 5, 19, 30, tzinfo=UTC)


@pytest.mark.parametrize("raw", [
    "2026-11-05T14:30:00",            # no timezone: refuse, never guess
    "2026-11-05 14:30",
    "2026-11-05T10:00:00Z",           # in the past
    "2027-11-05T12:00:00Z",           # > 30 days ahead
    "sometime after lunch",
    "0", "", None, 42,
])
def test_parse_rejects(raw):
    with pytest.raises(ValueError, match="timer_error"):
        T.parse_timer_time(raw, NOW)


def test_parse_bounds():
    with pytest.raises(ValueError, match="too soon"):
        T.parse_timer_time("+0.5s", NOW)
    # exactly at the 1 s floor and exactly 30 days are fine
    assert T.parse_timer_time("+1s", NOW) == NOW + timedelta(seconds=1)
    assert T.parse_timer_time("30d", NOW) == NOW + timedelta(days=30)
    with pytest.raises(ValueError, match="30 days"):
        T.parse_timer_time("31d", NOW)


def test_labels_and_cancel_on():
    assert T.label_from_prompt("tell the owner the tea is ready now") == \
        "tell the owner the tea"
    assert T.label_from_prompt("") == "timer"
    assert T.validate_cancel_on(None) == []
    assert T.validate_cancel_on(["chat.read", "chat.read"]) == ["chat.read"]
    with pytest.raises(ValueError, match="timer_error"):
        T.validate_cancel_on(["email.sent"])
    with pytest.raises(ValueError, match="timer_error"):
        T.validate_cancel_on("chat.read")


# ── store: persistence, claim/finish, migration ─────────────────────────────

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.add_persona("riko", "Riko", [], "sys", "hi", "")
    yield s
    s.close()


def _add(s, due, **kw):
    return s.add_timer(persona_id=kw.pop("persona", "riko"),
                       label=kw.pop("label", "t"), prompt="p",
                       due_at=iso(due), cancel_on=kw.pop("cancel_on", []),
                       captured=kw.pop("captured", {}))


def test_store_timer_roundtrip_ordering_and_caps(store):
    far = _add(store, NOW + timedelta(days=2), label="far")
    soon = _add(store, NOW + timedelta(minutes=5), label="soon")
    done = _add(store, NOW - timedelta(hours=1), label="old")
    store.finish_timer(done["id"], "fired", result_message_id=7)
    rows = store.list_timers()
    assert [r["id"] for r in rows] == [soon["id"], far["id"], done["id"]]  # open soonest-first, closed after
    assert rows[2]["status"] == "fired" and rows[2]["result_message_id"] == 7
    assert rows[0]["cancel_on"] == [] and rows[0]["captured"] == {}
    assert store.open_timer_count() == 2
    # messages of a fired turn carry the timer id
    u = store.add_message("riko", "user", "p", "timer", timer_id=soon["id"])
    a = store.add_message("riko", "assistant", "ok", "timer", timer_id=soon["id"])
    msgs = store.recent_messages("riko", 10)
    assert [m["timer_id"] for m in msgs] == [soon["id"], soon["id"]]
    assert msgs[0]["id"] == u and msgs[1]["id"] == a


def test_store_claim_finish_retry_cancel(store):
    t = _add(store, NOW + timedelta(seconds=30))
    assert store.claim_due_timers(iso(NOW)) == []          # not due yet
    got = store.claim_due_timers(iso(NOW + timedelta(minutes=1)))
    assert [x["id"] for x in got] == [t["id"]]
    assert store.get_timer(t["id"])["status"] == "firing"
    assert store.claim_due_timers(iso(NOW)) == []          # claimed once only
    back = store.retry_timer(t["id"], iso(NOW + timedelta(seconds=30)), "boom")
    assert back["status"] == "pending" and back["retry_count"] == 1
    assert store.reset_firing_timers() == []               # nothing firing
    fired = store.claim_due_timers(iso(NOW + timedelta(minutes=2)))[0]
    store.finish_timer(fired["id"], "fired", result_message_id=9)
    assert store.get_timer(t["id"])["status"] == "fired"
    assert store.cancel_timer_rows([t["id"]]) == []        # fired cannot cancel


def test_store_restart_resume(store):
    t = _add(store, NOW)
    store.claim_due_timers(iso(NOW + timedelta(seconds=1)))  # "crash" after claim
    assert store.get_timer(t["id"])["status"] == "firing"
    revived = store.reset_firing_timers()
    assert [x["id"] for x in revived] == [t["id"]]
    assert store.get_timer(t["id"])["status"] == "pending"  # fires late, never lost


def test_delete_persona_clears_timers(store):
    t = _add(store, NOW + timedelta(hours=1))
    store.add_persona("ghost", "Ghost", [], "sys", "hi", "")
    _add(store, NOW + timedelta(hours=1), persona="ghost")
    store.delete_persona("ghost")
    assert store.get_timer(t["id"])["status"] == "pending"   # riko's survives
    assert store.open_timer_count(persona_id="ghost") == 0


def test_migration_v7_to_v8_adds_timers(tmp_path):
    path = tmp_path / "hub.db"
    s = Store(path)
    s.add_persona("riko", "Riko", [], "sys", "hi", "")
    mid = s.add_message("riko", "user", "hello", "web")
    # wind the db back to v7 exactly as a pre-timers hub left it
    s.db.execute("DROP TABLE timers")
    s.db.execute("ALTER TABLE messages DROP COLUMN timer_id")
    s.db.execute("PRAGMA user_version=7")
    s.db.commit()
    s.close()
    s2 = Store(path)
    assert s2.db.execute("PRAGMA user_version").fetchone()[0] == 10  # keeps migrating to the current schema
    assert s2.recent_messages("riko", 5)[0]["timer_id"] is None  # old rows survive
    t = s2.add_timer(persona_id="riko", label="tea", prompt="p",
                     due_at=iso(NOW), cancel_on=["chat.read"], captured={})
    assert t["status"] == "pending" and t["cancel_on"] == ["chat.read"]
    s2.close()


# ── scheduler with an injected clock (no real sleeps) ───────────────────────

class FakeClock:
    def __init__(self, start=NOW):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def make_sched(store, fire, clock=None, **kw):
    ev = EventBus()
    locks = {}

    def lock_for(pid):
        return locks.setdefault(pid, threading.Lock())

    s = TimerScheduler(store, ev, fire, lock_for, clock=clock or FakeClock(), **kw)
    return s, ev, lock_for


def ev_list(ev):
    return ev.wait(0, 0)["events"]


def test_scheduler_fires_with_full_payload_and_batch_limit(store):
    clock = FakeClock()
    fired = []
    sched, ev, _ = make_sched(store, lambda t: fired.append(t) or {
        "message_id": 42, "reply": "Tea!", "speech": "Tea!",
        "emotion": "happy", "actions": [], "audio_url": "/api/messages/42/audio"},
        clock=clock)
    for i in range(12):
        _add(store, NOW + timedelta(seconds=10), label=f"t{i}")
    clock.advance(seconds=30)
    assert sched.tick() == 10                          # batch of 10 per poll
    assert sched.tick() == 2
    assert len(fired) == 12
    assert store.open_timer_count() == 0
    evs = [e for e in ev_list(ev) if e["type"] == "timer_fired"]
    assert len(evs) == 12
    e = evs[0]
    assert {"id", "label", "persona_id", "message_id", "reply", "speech",
            "emotion", "actions", "audio_url", "origin"} <= set(e)
    assert e["origin"] == "timer" and e["message_id"] == 42


def test_scheduler_waits_for_busy_chat_and_skips_cancelled(store):
    clock = FakeClock()
    fired = []
    sched, ev, lock_for = make_sched(
        store, lambda t: fired.append(t["id"]) or {"message_id": len(fired)}, clock)
    t = _add(store, NOW + timedelta(seconds=5))
    c = _add(store, NOW + timedelta(seconds=5), label="doomed",
             cancel_on=["chat.user_message"])
    lock = lock_for("riko")
    lock.acquire()                                     # a chat turn is running
    done = threading.Event()

    def poll():
        clock.advance(seconds=30)
        sched.tick()
        done.set()

    th = threading.Thread(target=poll)
    th.start()
    assert not done.wait(0.3)                          # never interrupts
    assert store.get_timer(t["id"])["status"] == "firing"   # claimed, waiting
    store.cancel_timer_rows([c["id"]])                 # cancelled while waiting
    lock.release()
    assert done.wait(5)
    th.join()
    assert fired == [t["id"]]                          # fired once the chat freed
    assert store.get_timer(t["id"])["status"] == "fired"
    assert store.get_timer(c["id"])["status"] == "cancelled"  # skipped, not fired


def test_scheduler_retry_once_then_failed(store):
    clock = FakeClock()

    def boom(timer):
        raise RuntimeError("LLM down")

    sched, ev, _ = make_sched(store, boom, clock)
    t = _add(store, NOW + timedelta(seconds=5))
    clock.advance(seconds=10)
    sched.tick()                                       # first failure
    row = store.get_timer(t["id"])
    assert row["status"] == "pending" and row["retry_count"] == 1
    assert row["due_at"] == iso(NOW + timedelta(seconds=40))  # failure time +30 s
    assert [e["type"] for e in ev_list(ev)] == ["timer_retry"]
    sched.tick()                                       # not due yet: no claim
    assert store.get_timer(t["id"])["status"] == "pending"
    clock.advance(seconds=30)
    sched.tick()                                       # second failure: final
    row = store.get_timer(t["id"])
    assert row["status"] == "failed" and "LLM down" in row["error"]
    assert any(e["type"] == "timer_failed" for e in ev_list(ev))
    assert sched.tick() == 0                           # never retried again


def test_scheduler_permanent_error_no_retry(store):
    clock = FakeClock()

    def gone(timer):
        raise TimerPermanent("persona vanished")

    sched, ev, _ = make_sched(store, gone, clock)
    t = _add(store, NOW)
    clock.advance(seconds=1)
    sched.tick()
    assert store.get_timer(t["id"])["status"] == "failed"
    assert store.get_timer(t["id"])["retry_count"] == 0


# ── hub app: tools, hooks, routes, events (fake LLM) ────────────────────────

def timer_client(tmp_path, steps):
    llm = ScriptedLLM(steps)
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     tools_defaults={"searxng_url": ""})
    return TestClient(app), llm, app


def events_of(client, ev_type):
    return [e for e in client.get("/api/events?after=0").json()["events"]
            if e["type"] == ev_type]


def fire_now(app, ahead=60):
    """Advance the injected test clock past due and run one scheduler poll."""
    sched = app.state.timer_scheduler
    sched.clock = lambda: datetime.now(UTC) + timedelta(seconds=ahead)
    return sched.tick()


def test_timer_tool_sets_lists_and_fires(tmp_path):
    steps = [
        call_step("timer", {"prompt": "tell the owner the tea is ready",
                            "time": "20 seconds", "label": "tea"}),
        {"content": "[happy] OK, I'll remind you at fifteen past noon."},
        {"content": "[happy] Your tea is ready!"},
    ]
    client, llm, app = timer_client(tmp_path, steps)

    res = client.post("/api/chat", json={"text": "tea timer for 20 seconds"}).json()
    assert res["tools_used"] == [
        {"tool": "timer", "label": "setting a timer: tea",
         "args": {"prompt": "tell the owner the tea is ready",
                  "time": "20 seconds", "label": "tea"}}]
    timers = client.get("/api/timers").json()["timers"]
    assert len(timers) == 1 and timers[0]["status"] == "pending"
    row = timers[0]
    assert row["label"] == "tea" and row["persona_id"] == "riko"
    assert "captured" not in row and "timezone" in row and "due_at_local" in row
    # captured: settings + the offered tool ids (the fire turn runs with these)
    cap = app.state.store.get_timer(row["id"])["captured"]
    assert "timer" in cap["tool_ids"] and cap["settings"].get("base_url")
    # the tool result gave her the LOCAL time to read back
    assert any("local_time" in json.dumps(r) for r in llm.chat_requests[1]["messages"])

    # set-turn's turn_finished is normal (no origin)
    tf_normal = [e for e in events_of(client, "turn_finished")]
    assert len(tf_normal) == 1 and "origin" not in tf_normal[0]

    n = fire_now(app)
    assert n == 1
    assert client.get("/api/timers?status=pending").json()["timers"] == []
    done = client.get("/api/timers").json()["timers"][0]
    assert done["status"] == "fired" and done["result_message_id"]

    fired = events_of(client, "timer_fired")[0]
    assert fired["label"] == "tea" and fired["reply"] == "Your tea is ready!"
    tf = events_of(client, "turn_finished")[-1]
    # docs/TIMERS.md: the FULL speak payload rides on turn_finished
    assert tf["origin"] == "timer" and tf["persona_id"] == "riko"
    assert tf["timer"] == {"id": done["id"], "label": "tea"}
    assert tf["reply"] == "Your tea is ready!" and tf["speech"] == "Your tea is ready!"
    assert tf["emotion"] == "happy" and "actions" in tf and "audio_url" in tf
    assert tf["message_id"] == done["result_message_id"]

    hist = client.get("/api/history?persona=riko&limit=50").json()["messages"]
    turn = [m for m in hist if m.get("timer_id") == done["id"]]
    assert [m["role"] for m in turn] == ["user", "assistant"]  # ⏰ on both
    assert turn[0]["content"] == "tell the owner the tea is ready"

    # the fired turn is a user message but must NOT self-cancel timers
    other = app.state.store.add_timer(
        persona_id="riko", label="oven", prompt="check the oven",
        due_at=iso(datetime.now(UTC) + timedelta(hours=6)),
        cancel_on=["chat.user_message"], captured={})
    assert other["status"] == "pending"


def test_cancel_on_user_message_and_fired_turn_not_self_cancelling(tmp_path):
    client, llm, app = timer_client(tmp_path, [{"content": "noted."},
                                               {"content": "timer fired reply"}])
    st = app.state.store
    doomed = st.add_timer(persona_id="riko", label="nap", prompt="wake",
                          due_at=iso(datetime.now(UTC) + timedelta(hours=1)),
                          cancel_on=["chat.user_message"], captured={})
    immune = st.add_timer(persona_id="riko", label="meds", prompt="pills",
                          due_at=iso(datetime.now(UTC) + timedelta(hours=2)),
                          cancel_on=[], captured={})
    client.post("/api/chat", json={"text": "never mind"})
    assert st.get_timer(doomed["id"])["status"] == "cancelled"
    assert st.get_timer(immune["id"])["status"] == "pending"
    reasons = {e.get("reason") for e in events_of(client, "timer_cancelled")}
    assert reasons == {"user_message"}


def test_cancel_on_chat_read_and_route(tmp_path):
    client, llm, app = timer_client(tmp_path, [{"content": "ok"}])
    st = app.state.store
    t = st.add_timer(persona_id="riko", label="showers", prompt="move the car",
                     due_at=iso(datetime.now(UTC) + timedelta(hours=1)),
                     cancel_on=["chat.read"], captured={})
    res = client.post("/api/chat/read", json={"persona_id": "riko"}).json()
    assert res["cancelled"] == [t["id"]]
    assert st.get_timer(t["id"])["status"] == "cancelled"
    assert events_of(client, "timer_cancelled")[0]["reason"] == "chat_read"
    assert client.post("/api/chat/read", json={"persona_id": "nobody"}).status_code == 404
    # default persona = active one, empty body allowed
    t2 = st.add_timer(persona_id="riko", label="laundry", prompt="fold",
                      due_at=iso(datetime.now(UTC) + timedelta(hours=1)),
                      cancel_on=["chat.read"], captured={})
    assert client.post("/api/chat/read", json={}).json()["cancelled"] == [t2["id"]]


def test_list_and_cancel_tools_by_voice(tmp_path):
    client, llm, app = timer_client(
        tmp_path, [call_step("list_timers", {}),
                   call_step("cancel_timer", {"label": "tea"}),
                   {"content": "cancelled the tea timer."}])
    st = app.state.store
    tea = st.add_timer(persona_id="riko", label="tea", prompt="kettle",
                       due_at=iso(datetime.now(UTC) + timedelta(hours=1)),
                       cancel_on=[], captured={})
    st.add_timer(persona_id="riko", label="oven", prompt="oven",
                 due_at=iso(datetime.now(UTC) + timedelta(hours=2)),
                 cancel_on=[], captured={})
    res = client.post("/api/chat", json={"text": "what timers do I have? then kill the tea one"}).json()
    assert [x["tool"] for x in res["tools_used"]] == ["list_timers", "cancel_timer"]
    assert st.get_timer(tea["id"])["status"] == "cancelled"
    seen = json.dumps(llm.chat_requests[1]["messages"])
    # the tool result is JSON-in-JSON: check the fields reached the model
    assert "oven" in seen and "due_at_local" in seen and str(tea["id"]) in seen
    assert events_of(client, "timer_cancelled")[0]["reason"] == "tool"


def test_timer_routes(tmp_path):
    client, llm, app = timer_client(tmp_path, [{"content": "ok"}])
    st = app.state.store
    t = st.add_timer(persona_id="riko", label="a", prompt="p",
                     due_at=iso(datetime.now(UTC) + timedelta(hours=1)),
                     cancel_on=[], captured={"settings": {"api_key": "sk-secret"}})
    out = client.get("/api/timers").json()
    assert "sk-secret" not in json.dumps(out)           # captured never leaks
    assert client.get("/api/timers?status=fired").json()["timers"] == []
    assert client.delete("/api/timers/9999").status_code == 404
    assert client.delete(f"/api/timers/{t['id']}").json() == {"cancelled": t["id"], "label": "a"}
    assert client.delete(f"/api/timers/{t['id']}").status_code == 409  # already cancelled
    assert client.get("/api/timers?status=cancelled").json()["timers"][0]["id"] == t["id"]


def test_pending_cap(tmp_path):
    client, llm, app = timer_client(
        tmp_path, [call_step("timer", {"prompt": "overflow", "time": "1h"}),
                   {"content": "cannot"}])
    st = app.state.store
    for i in range(T.MAX_PENDING_TIMERS):
        st.add_timer(persona_id="riko", label=f"c{i}", prompt="p",
                     due_at=iso(datetime.now(UTC) + timedelta(days=2)),
                     cancel_on=[], captured={})
    client.post("/api/chat", json={"text": "one more timer"})
    result = llm.chat_requests[1]["messages"][-1]["content"]
    assert "at most 50 pending timers" in result
    assert st.open_timer_count() == T.MAX_PENDING_TIMERS   # none added


def test_timezone_setting_roundtrip(tmp_path):
    client, llm, app = timer_client(tmp_path, [{"content": "ok"}])
    s = client.get("/api/settings").json()
    assert s["timezone"] == "" and s["timezone_source"] == "default"
    assert s["defaults"]["timezone"]                       # server zone visible
    r = client.put("/api/settings", json={"timezone": "Europe/London"})
    assert r.status_code == 200
    s = client.get("/api/settings").json()
    assert s["timezone"] == "Europe/London" and s["timezone_source"] == "saved"
    assert client.put("/api/settings", json={"timezone": "Mars/Olympus"}).status_code == 422
    assert client.put("/api/settings", json={"timezone": ""}).status_code == 200
    assert client.get("/api/settings").json()["timezone_source"] == "default"
    client.put("/api/settings", json={"timezone": "America/New_York"})
    s = client.post("/api/settings/reset", json={"timezone": True}).json()
    assert s["timezone"] == "" and s["timezone_source"] == "default"
    # a timer's due_at_local follows the saved zone
    client.put("/api/settings", json={"timezone": "America/New_York"})
    app.state.store.add_timer(persona_id="riko", label="tz", prompt="p",
                              due_at="2026-11-05T17:00:00Z", cancel_on=[],
                              captured={})
    row = client.get("/api/timers").json()["timers"][0]
    assert row["timezone"] == "America/New_York"
    assert row["due_at_local"].endswith("-05:00")          # EDT->EST by date: Nov = -05


def test_local_time_line_in_tools_prompt():
    p = __import__("hub.tools.registry", fromlist=["tools_prompt"]).tools_prompt(
        ["timer"], now_local="2026-11-05 12:00", tz_name="Europe/London")
    assert "2026-11-05 12:00" in p and "Europe/London" in p
    assert "2026-11-05 12:00" not in __import__(
        "hub.tools.registry", fromlist=["tools_prompt"]).tools_prompt(["web_search"])


def test_live_scheduler_thread_fires_end_to_end(tmp_path):
    """The real thread (started via timers_defaults) fires a 1 s timer."""
    steps = [call_step("timer", {"prompt": "say hi", "time": "+1s"}),
             {"content": "ok"}, {"content": "hi!"}]
    llm = ScriptedLLM(steps)
    app = create_app(tmp_path / "hubdata",
                     llm_defaults={"base_url": llm.base_url, "model": "auto"},
                     tools_defaults={"searxng_url": ""},
                     timers_defaults={"poll_s": "1"})
    client = TestClient(app)
    try:
        client.post("/api/chat", json={"text": "ping in one second"})
        import time as _t
        deadline = _t.time() + 10
        fired = []
        while _t.time() < deadline:
            fired = [e for e in client.get("/api/events?after=0").json()["events"]
                     if e["type"] == "timer_fired"]
            if fired:
                break
            _t.sleep(0.25)
        assert fired, "live scheduler never fired"
        assert fired[0]["reply"] == "hi!"
    finally:
        app.state.timer_scheduler.stop()


# ── asked-for fallback + chain guard (docs/TIMERS.md "Reviewer additions") ───

# (text, time passed to parse_timer_time, label, prompt)
POSITIVE_ASKS = [
    ("remind me in 20 seconds to stretch",
     "20s", "stretch", "Reminder: stretch"),
    ("Remind me in 5 minutes to check the oven",
     "300s", "check the oven", "Reminder: check the oven"),
    ("hey, remind me in 5 minutes to check the oven please",
     "300s", "check the oven", "Reminder: check the oven"),
    ("please remind me in 3 days to pay the rent",
     "259200s", "pay the rent", "Reminder: pay the rent"),
    ("could you remind me in 5 minutes to hydrate",
     "300s", "hydrate", "Reminder: hydrate"),
    ("and then remind me in 1 hour to turn the oven off",
     "3600s", "turn the oven off", "Reminder: turn the oven off"),
    ("remind me to stretch in 20 minutes",
     "1200s", "stretch", "Reminder: stretch"),
    ("remind me to call mum in 2 hours",
     "7200s", "call mum", "Reminder: call mum"),
    ("remind me in an hour to check the kettle",
     "3600s", "check the kettle", "Reminder: check the kettle"),
    ("remind me in half an hour to check the kettle",
     "1800s", "check the kettle", "Reminder: check the kettle"),
    ("remind me in 1d to renew the domain",
     "86400s", "renew the domain", "Reminder: renew the domain"),
    ("remind me in twenty minutes to stretch",
     "1200s", "stretch", "Reminder: stretch"),
    ("remind me in ninety seconds to flip the burgers",
     "90s", "flip the burgers", "Reminder: flip the burgers"),
    ("remind me in 1h30m to stretch",
     "5400s", "stretch", "Reminder: stretch"),
    ("in 20 minutes remind me to stretch",
     "1200s", "stretch", "Reminder: stretch"),
    ("in 5 minutes, remind me to check the oven",
     "300s", "check the oven", "Reminder: check the oven"),
    ("in 30 seconds remind me",
     "30s", "check in with the owner", "Reminder: check in with the owner"),
    ("set a timer for 30 seconds",
     "30s", "timer", 'Timer "timer" is up.'),
    ("set a timer for 30 seconds labelled stretch",
     "30s", "stretch", 'Timer "stretch" is up.'),
    ("Please set a timer for 30 seconds labelled stretch, using your timer tool",
     "30s", "stretch, using your timer tool",
     'Timer "stretch, using your timer tool" is up.'),
    ("could you set a timer for 5 minutes?",
     "300s", "timer", 'Timer "timer" is up.'),
    ("set a reminder for 1 hour to take meds",
     "3600s", "take meds", 'Timer "take meds" is up.'),
    ("set an alarm for 6 hours called workout",
     "21600s", "workout", 'Timer "workout" is up.'),
    ("set a 15 second tea timer",
     "15s", "tea", 'Timer "tea" is up.'),
    ("set a 10 minute timer",
     "600s", "timer", 'Timer "timer" is up.'),
    ("set a 2 hour timer for the roast",
     "7200s", "the roast", 'Timer "the roast" is up.'),
    ("10 minute timer for pasta",
     "600s", "pasta", 'Timer "pasta" is up.'),
    ("20 second timer", "20s", "timer", 'Timer "timer" is up.'),
    ("timer for 45 seconds", "45s", "timer", 'Timer "timer" is up.'),
    ("tea timer for 20 seconds", "20s", "tea", 'Timer "tea" is up.'),
    ("I need a 10 minute timer for pasta",
     "600s", "pasta", 'Timer "pasta" is up.'),
    ("wake me in 3 hours",
     "10800s", "wake up", "Reminder: wake the owner up"),
    ("wake me up in 20 minutes",
     "1200s", "wake up", "Reminder: wake the owner up"),
    ("wake me up in 1 hour to start dinner",
     "3600s", "wake up", "Reminder: start dinner"),
    # at-HH:MM resolves in the OWNER's zone, today or tomorrow (now =
    # 12:00 UTC 2026-11-05, London = UTC in November)
    ("remind me at 14:30 to stretch",
     "2026-11-05T14:30:00+00:00", "stretch", "Reminder: stretch"),
    ("remind me at 11:00",
     "2026-11-06T11:00:00+00:00", "check in with the owner",
     "Reminder: check in with the owner"),
    ("remind me at 7:30 pm to lock the door",
     "2026-11-05T19:30:00+00:00", "lock the door", "Reminder: lock the door"),
    ("remind me at 20:00",
     "2026-11-05T20:00:00+00:00", "check in with the owner",
     "Reminder: check in with the owner"),
]

NEGATIVE_ASKS = [
    # questions about timers
    "what timers do I have", "how long is left", "tell me how long is left",
    "how much time is left", "when does my tea timer go off",
    "is my timer still running", "do I have a timer for 5 minutes",
    "tell me what timers I have set",
    # past-tense mentions
    "I set a timer for 20 seconds", "I set a tea timer earlier",
    "did I set a timer?", "you reminded me yesterday",
    "she reminded me in 5 minutes", "the timer went off",
    "you just set a timer for 10 minutes", "my timer for 20 minutes is done",
    # negations
    "don't remind me about that", "dont set a timer", "do not remind me",
    "no reminder needed", "never mind the timer",
    # cancellations
    "cancel the 10 minute timer", "cancel my reminder",
    "delete the timer called tea", "stop the timer",
    # hypotheticals
    "if I asked you to remind me in 10 minutes what would you do",
    "what if I wanted a timer for 5 minutes", "imagine a timer for 10 minutes",
    # no time attached: the hub will not guess
    "remind me", "remind me to stretch", "set a timer", "tea timer",
    "what time is it",
]


@pytest.mark.parametrize("text,time,label,prompt", POSITIVE_ASKS)
def test_detect_timer_request_positive(text, time, label, prompt):
    got = T.detect_timer_request(text, now=NOW, tz_name="Europe/London")
    assert got is not None, text
    assert got["time"] == time
    assert got["label"] == label
    assert got["prompt"] == prompt
    # the time is exactly what parse_timer_time accepts
    T.parse_timer_time(got["time"], NOW)


@pytest.mark.parametrize("text", NEGATIVE_ASKS)
def test_detect_timer_request_negative(text):
    assert T.detect_timer_request(text, now=NOW, tz_name="Europe/London") is None


def test_refusal_detection():
    assert T.reply_refuses("Sorry senpai, I can't set timers right now.")
    assert T.reply_refuses("I won't be doing that today.")
    assert T.reply_refuses("I was unable to reach the scheduler.")
    assert not T.reply_refuses("Twenty seconds, then I'll nudge you!")
    assert not T.reply_refuses("Sure — timer set for 20 seconds.")


def test_fallback_creates_timer_when_model_only_claims(tmp_path):
    """Live finding A: she answers 'Twenty seconds, then…' with only a
    list_timers call — the hub creates the asked timer itself."""
    steps = [call_step("list_timers", {}),
             {"content": "Twenty seconds, then I'll nudge you, senpai!"}]
    client, llm, app = timer_client(tmp_path, steps)

    res = client.post("/api/chat",
                      json={"text": "remind me in 20 seconds to stretch"}).json()
    assert [x["tool"] for x in res["tools_used"]] == ["list_timers"]
    timers = client.get("/api/timers").json()["timers"]
    assert len(timers) == 1
    row = timers[0]
    assert row["status"] == "pending" and row["label"] == "stretch"
    assert row["asked"] is True and row["chain_depth"] == 0
    stored = app.state.store.get_timer(row["id"])
    assert stored["prompt"] == "Reminder: stretch"
    assert stored["captured"]["asked"] is True
    assert res["timers_set"] == [{"id": row["id"], "label": "stretch",
                                  "due_at_local": row["due_at_local"],
                                  "asked": True}]
    due = datetime.strptime(row["due_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc)
    assert 15 <= (due - datetime.now(UTC)).total_seconds() <= 22
    ev = events_of(client, "timer_set")[-1]
    assert ev["id"] == row["id"] and ev["asked"] is True

    # the prompt hint rode along on the asked turn
    assert "The user just asked for a timer" in \
        llm.chat_requests[0]["messages"][0]["content"]
    assert 'time="20s"' in llm.chat_requests[0]["messages"][0]["content"]


def test_no_duplicate_when_model_sets_itself(tmp_path):
    steps = [call_step("timer", {"prompt": "Reminder: stretch",
                                 "time": "20 seconds", "label": "stretch"}),
             {"content": "Done — twenty seconds!"}]
    client, llm, app = timer_client(tmp_path, steps)
    res = client.post("/api/chat",
                      json={"text": "remind me in 20 seconds to stretch"}).json()
    timers = client.get("/api/timers").json()["timers"]
    assert len(timers) == 1                      # no fallback duplicate
    assert res["timers_set"] == [{"id": timers[0]["id"], "label": "stretch",
                                  "due_at_local": timers[0]["due_at_local"],
                                  "asked": False}]


def test_fallback_skips_on_refusal_and_without_tools(tmp_path):
    steps = [{"content": "I can't set timers right now, senpai."}]
    client, llm, app = timer_client(tmp_path, steps)
    res = client.post("/api/chat",
                      json={"text": "remind me in 20 seconds to stretch"}).json()
    assert client.get("/api/timers").json()["timers"] == []
    assert res["timers_set"] == []
    # tools off entirely: no timer tool offered, so no scheduling behind her back
    client.put("/api/settings", json={"tools": {"enabled": False}})
    client.post("/api/chat", json={"text": "remind me in 20 seconds to stretch"})
    assert client.get("/api/timers").json()["timers"] == []


def _seed_timer(app, prompt="tell the owner the tea is ready", **cap_kw):
    captured = {"settings": {}, "memory": {},
                "tool_ids": ["timer", "list_timers", "cancel_timer"]}
    captured.update(cap_kw)
    return app.state.store.add_timer(
        persona_id="riko", label="tea", prompt=prompt,
        due_at=iso(datetime.now(UTC) + timedelta(seconds=5)),
        cancel_on=[], captured=captured)


def test_chain_guard_fired_turn_lacks_timer_tool(tmp_path):
    """The fired turn keeps list_timers/cancel_timer but NOT timer, and the
    asked-for fallback never fires on its prompt."""
    steps = [{"content": "noted."},                      # seeding chat turn
             {"content": "Tea is ready, senpai!"}]        # the fired turn
    client, llm, app = timer_client(tmp_path, steps)
    t = _seed_timer(app, prompt="remind me to stretch in 20 minutes")
    client.post("/api/chat", json={"text": "hello"})      # step 0
    assert fire_now(app) == 1
    fired_req = llm.chat_requests[-1]
    names = [x["function"]["name"] for x in fired_req.get("tools", [])]
    assert "timer" not in names
    assert {"list_timers", "cancel_timer"} <= set(names)   # still available
    # the prompt read like an ask, yet no timer was created and the fallback
    # left it alone (no duplicate, the row itself is fired)
    assert client.get("/api/timers?status=pending").json()["timers"] == []
    assert client.get("/api/timers?status=fired").json()["timers"][0]["id"] == t["id"]
    # the fired turn's system prompt carries no "just asked for a timer" hint
    assert "The user just asked for a timer" not in fired_req["messages"][0]["content"]


def test_chaining_enabled_depth_cap(tmp_path):
    # fire 1 asks for a link (depth 1); fire 2 (depth-3 parent) asks again
    # and must hit the chain limit
    link_call = call_step("timer", {"prompt": "next link", "time": "1h"})
    steps = [link_call, {"content": "queued the next one."},
             link_call, {"content": "the chain ends here."}]
    client, llm, app = timer_client(tmp_path, steps)
    assert client.put("/api/settings",
                      json={"timers": {"allow_chaining": True}}).status_code == 200
    _seed_timer(app)                             # root: no chain_depth
    assert fire_now(app) == 1
    fired_req = llm.chat_requests[0]
    names = [x["function"]["name"] for x in fired_req.get("tools", [])]
    assert "timer" in names                      # chaining on: tool is back
    pending = client.get("/api/timers?status=pending").json()["timers"]
    link = next(r for r in pending if r["prompt"] == "next link")
    # the model-created link inherits the parent depth + 1
    assert app.state.store.get_timer(link["id"])["captured"]["chain_depth"] == 1

    # depth 3 parent: the next link would be 4 — refused, nothing created
    _seed_timer(app, prompt="last link", chain_depth=3)
    assert fire_now(app) == 1
    tool_msg = json.dumps(llm.chat_requests[-1]["messages"])
    assert "chain limit" in tool_msg
    pending = client.get("/api/timers?status=pending").json()["timers"]
    assert [r["id"] for r in pending] == [link["id"]]   # only the depth-1 link
    assert client.get("/api/timers?status=failed").json()["timers"] == []


def test_timer_settings_roundtrip(tmp_path):
    client, llm, app = timer_client(tmp_path, [{"content": "ok"}])
    s = client.get("/api/settings").json()
    assert s["timers"] == {"allow_chaining": False}
    assert s["timers_source"] == {"allow_chaining": "default"}
    r = client.put("/api/settings", json={"timers": {"allow_chaining": True}})
    assert r.status_code == 200
    s = r.json()
    assert s["timers"] == {"allow_chaining": True}
    assert s["timers_source"] == {"allow_chaining": "saved"}
    assert client.put("/api/settings",
                      json={"timers": {"allow_chaining": "yes"}}).status_code == 422
    assert client.put("/api/settings", json={"timers": []}).status_code == 422
    s = client.post("/api/settings/reset", json={"timers": True}).json()
    assert s["timers"] == {"allow_chaining": False}
    assert s["timers_source"] == {"allow_chaining": "default"}


def test_fallback_never_fires_when_permission_never(tmp_path):
    steps = [{"content": "Sure, twenty seconds!"}]
    client, llm, app = timer_client(tmp_path, steps)
    client.put("/api/settings", json={"tools": {"permissions": {"timer": "never"}}})
    client.post("/api/chat",
                json={"text": "remind me in 20 seconds to stretch"})
    assert client.get("/api/timers").json()["timers"] == []
