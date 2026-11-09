"""The hub's event ring buffer (docs/ROUNDS.md R1-B: GET /api/events).

Last 500 events, monotonic `seq`, thread-safe long-poll. Every producer
(chat turns, tool loop, client-tool channel, state bumps) publishes; every
client consumes via the long-poll route — no websockets, so any HTTP client
(Android included) can follow along.

Event types: turn_started {persona, source}, tool_started {call_id, tool,
label}, tool_finished {call_id, tool, ok, ms}, approval_needed {request_id,
client_id, tool, summary}, turn_finished {persona, message_id, emotion,
actions}, state_changed {version}, memory_changed {id, op} (docs/WALK_MEMORY.md M:
op added | updated | deleted, fired by the memory tools and the API routes),
history_changed {persona, ids, op} (op deleted — one or many chat-history
rows removed via the API; open clients refresh their transcript).
"""
import threading
import time

try:
    from .store import now_iso
except ImportError:  # standalone: modules sit next to this file (Docker: /app)
    from store import now_iso  # type: ignore[no-redef]

EVENT_BUFFER = 500
MAX_WAIT = 30.0     # /api/events timeout clamp (docs/ROUNDS.md)
DEFAULT_WAIT = 25.0


class EventBus:
    def __init__(self, cap: int = EVENT_BUFFER):
        self.cap = cap
        self._events: list[dict] = []
        self._seq = 0
        self._cond = threading.Condition()

    def publish(self, event_type: str, **fields) -> dict:
        event = {"seq": None, "time": now_iso(), "type": event_type}
        event.update(fields)
        with self._cond:
            self._seq += 1
            event["seq"] = self._seq
            self._events.append(event)
            if len(self._events) > self.cap:
                del self._events[:len(self._events) - self.cap]
            self._cond.notify_all()
        return event

    def wait(self, after: int = 0, timeout: float = DEFAULT_WAIT) -> dict:
        """Block until an event with seq > `after` exists or `timeout` passes.
        Returns {events: [...], last: <highest published seq>} — the caller
        passes `last` back as the next `after`."""
        timeout = max(0.0, min(float(timeout), MAX_WAIT))
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                fresh = [e for e in self._events if e["seq"] > after]
                remaining = deadline - time.monotonic()
                if fresh or remaining <= 0:
                    return {"events": fresh, "last": self._seq}
                self._cond.wait(remaining)
