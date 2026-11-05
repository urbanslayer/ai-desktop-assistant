"""The timer fire loop (docs/TIMERS.md H).

One daemon thread, boring on purpose: every `poll_s` seconds (floor 1 s)
claim up to 10 due timers and run each as a chat turn — through
`lock_for(persona_id)`, so a timer NEVER interrupts a running chat turn
(it waits; late is better than never). After taking the lock it re-reads
the row: a timer cancelled while it waited (cancel_on hooks cancel
pending AND claimed-not-yet-run rows) is simply skipped.

Everything time-related is injectable (`clock`) so tests drive
the 30 s retry and late-fire paths by calling `tick()` with a stepped
clock, without wall-clock sleeps.

The `fire` callback is the hub's chat-turn path; raising from it counts as
an LLM error: one retry after `retry_delay_s`, then failed. `TimerPermanent`
( persona vanished) skips the retry.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

BATCH = 10          # contract: at most 10 timers per poll cycle
MIN_POLL_S = 1.0    # contract: poll floor
RETRY_DELAY_S = 30.0  # contract: one retry after 30 s on an LLM error


class TimerPermanent(Exception):
    """fire() raises this when retrying cannot possibly help (persona gone)."""


def iso_utc(dt: datetime) -> str:
    """Same string format as store.now_iso — the claim query compares these."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TimerScheduler:
    def __init__(self, store, events, fire: Callable[[dict], dict],
                 lock_for: Callable[[str], threading.Lock], *,
                 clock: Callable[[], datetime] = _utc_now,
                 poll_s: float = MIN_POLL_S,
                 retry_delay_s: float = RETRY_DELAY_S,
                 batch: int = BATCH,
                 on_change: Callable[[], None] | None = None):
        self.store = store
        self.events = events
        self.fire = fire
        self.lock_for = lock_for
        self.clock = clock
        self.poll_s = max(MIN_POLL_S, float(poll_s))
        self.retry_delay_s = float(retry_delay_s)
        self.batch = int(batch)
        self.on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        """Restart resume: rows a dead scheduler claimed fire late instead of
        rotting in 'firing' forever."""
        self.store.reset_firing_timers()
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop,
                                            name="timer-scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # a broken poll must not kill the loop
                pass
            self._stop.wait(self.poll_s)

    # ── one poll cycle (public so tests can drive it with an injected clock)
    def tick(self) -> int:
        due = self.store.claim_due_timers(iso_utc(self.clock()), limit=self.batch)
        for timer in due:
            self._fire_one(timer)
        return len(due)

    def _fire_one(self, timer: dict) -> None:
        with self.lock_for(timer["persona_id"]):
            row = self.store.get_timer(timer["id"])
            if row is None or row["status"] != "firing":
                return  # cancelled (or deleted) while we waited for the lock
            try:
                result = self.fire(row)
            except TimerPermanent as exc:
                self._fail(row, str(exc), retry=False)
                return
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                if row["retry_count"] < 1:
                    retry_at = iso_utc(self.clock() + timedelta(seconds=self.retry_delay_s))
                    self.store.retry_timer(row["id"], retry_at, err)
                    self.events.publish(
                        "timer_retry", id=row["id"], label=row["label"],
                        persona_id=row["persona_id"], error=err[:300],
                        retry_at=retry_at)
                else:
                    self._fail(row, err, retry=True)
                return
            self.store.finish_timer(row["id"], "fired",
                                    result_message_id=result.get("message_id"))
            # docs/TIMERS.md: the payload must carry the FULL speak data —
            # clients (child D) play this turn like any live reply.
            self.events.publish(
                "timer_fired",
                id=row["id"], label=row["label"], persona_id=row["persona_id"],
                origin="timer",
                message_id=result.get("message_id"),
                reply=result.get("reply", ""),
                speech=result.get("speech", ""),
                emotion=result.get("emotion"),
                actions=result.get("actions") or [],
                audio_url=result.get("audio_url"))
            if self.on_change:
                self.on_change()

    def _fail(self, row: dict, error: str, *, retry: bool) -> None:
        self.store.finish_timer(row["id"], "failed", error=error)
        self.events.publish("timer_failed", id=row["id"], label=row["label"],
                            persona_id=row["persona_id"], error=error[:300],
                            retries_used=1 if retry else 0)
        if self.on_change:
            self.on_change()
