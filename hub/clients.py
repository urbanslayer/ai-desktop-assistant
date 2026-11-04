"""The client-tool channel (server side): register / heartbeat / long-poll /
results — docs/ROUNDS.md R1-B.

A desktop (later Android) app registers the tools it can run; when the model
calls one the hub pushes a request down the client's long-poll, waits for the
result, and feeds it back to the model. Clients re-register (or heartbeat)
every 30 s and count as offline 75 s after the last contact. Approval-gated
requests wait up to 120 s, then the outcome is "not approved".

The model sees client tools as `<kind>__<name>` (e.g. `desktop__open_url`).
A client may also register a `body` (body.actions / body.expressions,
docs/BODY.md); the union over online clients is the body set (`body_actions()`).
"""
import json
import re
import threading
import time
import uuid

try:
    from .tools.registry import TOOL_NAME_RE, ToolSpec, full_tool_name
except (ImportError, SystemError):  # standalone: hub/ is on sys.path
    from tools.registry import TOOL_NAME_RE, ToolSpec, full_tool_name  # type: ignore

HEARTBEAT_S = 30          # suggested client cadence
OFFLINE_AFTER_S = 75      # no contact for this long -> offline
APPROVAL_TIMEOUT_S = 120  # an approval nobody answers is a "not approved"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KIND_RE = re.compile(r"^[a-z0-9]{1,16}$")
MAX_REQUESTS_PER_POLL = 20


class _Pending:
    __slots__ = ("request", "done", "result", "approval", "client_id")

    def __init__(self, client_id: str, request: dict):
        self.client_id = client_id
        self.request = request
        self.done = threading.Event()
        self.result: dict | None = None   # {ok, result?, error?}
        self.approval = "pending"         # approved | denied | not approved | timeout


class ClientHub:
    """All state for registered clients. Blocking methods run on threadpool
    threads (the chat loop dispatches from one of them)."""

    def __init__(self, events=None):
        self.events = events
        self._lock = threading.Lock()
        self._clients: dict[str, dict] = {}   # client_id -> record
        self._pending: dict[str, _Pending] = {}
        self._cond = threading.Condition(self._lock)  # signals queued requests

    # ── registration ───────────────────────────────────────────────────
    def register(self, client_id: str, name: str, kind: str, tools: list,
                 body: dict | None = None) -> dict:
        client_id = str(client_id or "").strip()
        kind = str(kind or "").strip().lower()
        if not CLIENT_ID_RE.match(client_id):
            raise ValueError("client_id must match ^[A-Za-z0-9_-]{1,64}$")
        if not KIND_RE.match(kind):
            raise ValueError("kind must match ^[a-z0-9]{1,16}$ (desktop, android, …)")
        clean_body = _clean_body(body)
        clean_tools: dict[str, dict] = {}
        for raw in tools or []:
            if not isinstance(raw, dict):
                raise ValueError("each tool must be an object")
            tname = str(raw.get("name") or "")
            if not TOOL_NAME_RE.match(tname):
                raise ValueError(f"tool name '{tname}' must match ^[A-Za-z0-9_-]{{1,64}}$")
            model_name = full_tool_name(kind, tname)
            risk = str(raw.get("risk") or "act")
            if risk not in ("read", "act", "danger"):
                raise ValueError(f"tool '{tname}' risk must be read, act or danger")
            params = raw.get("parameters")
            clean_tools[tname] = {
                "name": tname,
                "model_name": model_name,
                "description": str(raw.get("description") or "")[:1000],
                "parameters": params if isinstance(params, dict) else {
                    "type": "object", "properties": {}},
                "risk": risk,
            }
        record = {"client_id": client_id, "name": str(name or client_id)[:100],
                  "kind": kind, "tools": clean_tools, "last_seen": time.monotonic(),
                  "body": clean_body, "queued": []}
        with self._lock:
            old = self._clients.get(client_id)
            # a re-register must not lose requests queued since the last poll
            record["queued"] = list(old["queued"]) if old else []
            self._clients[client_id] = record
            if old:  # a re-register with fewer tools: fail its stranded waits
                for tname in set(old["tools"]) - set(clean_tools):
                    self._fail_client_requests_locked(client_id, f"tool '{tname}' unregistered")
            self._cond.notify_all()
        return {"ok": True, "heartbeat_s": HEARTBEAT_S}

    def heartbeat(self, client_id: str) -> bool:
        with self._lock:
            rec = self._clients.get(client_id)
            if not rec:
                return False
            rec["last_seen"] = time.monotonic()
            return True

    def _is_online(self, rec: dict, now: float) -> bool:
        return now - rec["last_seen"] < OFFLINE_AFTER_S

    def online_clients(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            return [self._public_locked(c) for c in self._clients.values()
                    if self._is_online(c, now)]

    def _public_locked(self, rec: dict) -> dict:
        return {"client_id": rec["client_id"], "name": rec["name"], "kind": rec["kind"],
                "seconds_since_seen": round(now_elapsed(rec["last_seen"]), 1),
                "body": dict(rec["body"]),
                "tools": [dict(t) for t in rec["tools"].values()]}

    def body_actions(self) -> list[str]:
        """The BODY SET (docs/BODY.md §1): union of every ONLINE client's
        body.actions, first-seen order, empty when nobody embodied is online."""
        now = time.monotonic()
        out: list[str] = []
        with self._lock:
            for rec in self._clients.values():
                if not self._is_online(rec, now):
                    continue
                for name in rec["body"]["actions"]:
                    if name not in out:
                        out.append(name)
        return out

    # ── tool visibility + dispatch ─────────────────────────────────────
    def online_tool_specs(self) -> list[ToolSpec]:
        """ToolSpecs for every tool of every online client (last client to
        register a name wins)."""
        now = time.monotonic()
        found: dict[str, tuple[dict, dict]] = {}
        with self._lock:
            for rec in self._clients.values():
                if not self._is_online(rec, now):
                    continue
                for t in rec["tools"].values():
                    found[t["model_name"]] = (rec, t)
        return [ToolSpec(name=t["model_name"], display=t["name"],
                         description=t["description"] or f"client tool {t['name']}",
                         parameters=t["parameters"], source="client", risk=t["risk"],
                         client_id=rec["client_id"], run=None,
                         label=(lambda nm: lambda args: summarize(nm, args))(t["name"]))
                for name, (rec, t) in found.items()]

    def dispatch(self, spec: ToolSpec, arguments: dict, needs_approval: bool,
                 summary: str, wait_seconds: float) -> dict:
        """Send one request to the owning client and block for its result.
        Returns {ok, result?, error?, approval}. `approval` is 'not needed'
        when the policy didn't gate the call."""
        now = time.monotonic()
        with self._lock:
            rec = self._clients.get(spec.client_id or "")
            if not rec or not self._is_online(rec, now):
                return {"ok": False, "error": "client is offline", "approval": "not needed"}
            request_id = uuid.uuid4().hex
            req = {"request_id": request_id, "tool": spec.display,
                   "arguments": arguments, "needs_approval": bool(needs_approval),
                   "summary": summary}
            pend = _Pending(spec.client_id, req)
            pend.approval = "pending" if needs_approval else "not needed"
            self._pending[request_id] = pend
            rec["queued"].append(req)
            self._cond.notify_all()
        if needs_approval and self.events is not None:
            self.events.publish("approval_needed", request_id=request_id,
                                client_id=spec.client_id, tool=spec.name,
                                summary=summary)
        # approvals wait APPROVAL_TIMEOUT_S; plain calls the remaining budget
        timeout = APPROVAL_TIMEOUT_S if needs_approval else max(1.0, wait_seconds)
        answered = pend.done.wait(timeout)
        with self._lock:
            self._pending.pop(request_id, None)
        if not answered:
            if needs_approval:
                return {"ok": False, "error": f"not approved within {APPROVAL_TIMEOUT_S} s",
                        "approval": "not approved"}
            return {"ok": False, "error": f"client did not answer within {int(timeout)} s",
                    "approval": "not needed"}
        result = dict(pend.result or {"ok": False, "error": "no result"})
        result["approval"] = pend.approval
        return result

    def request_approval(self, summary: str, wait_seconds: float = 60.0) -> dict | None:
        """Approval gate for a HUB tool set to `ask`: push an approval-only
        request to the first online client. Returns None when no client is
        online (the caller decides the fallback); otherwise the same
        {ok, error, approval} shape as dispatch()."""
        with self._lock:
            now = time.monotonic()
            rec = next((r for r in self._clients.values() if self._is_online(r, now)), None)
            if rec is None:
                return None
            request_id = uuid.uuid4().hex
            req = {"request_id": request_id, "tool": "__approval__",
                   "arguments": {"summary": summary}, "needs_approval": False,
                   "summary": summary}
            pend = _Pending(rec["client_id"], req)
            pend.approval = "pending"
            self._pending[request_id] = pend
            rec["queued"].append(req)
            self._cond.notify_all()
        if self.events is not None:
            self.events.publish("approval_needed", request_id=request_id,
                                client_id=rec["client_id"], tool="__approval__",
                                summary=summary)
        answered = pend.done.wait(max(1.0, min(float(wait_seconds), APPROVAL_TIMEOUT_S)))
        with self._lock:
            self._pending.pop(request_id, None)
        if not answered:
            return {"ok": False, "error": "no approval within the time limit",
                    "approval": "not approved"}
        result = dict(pend.result or {"ok": False, "error": "no result"})
        result["approval"] = pend.approval
        return result

    def take_requests(self, client_id: str, timeout: float) -> list[dict]:
        """Long-poll: hand the client everything queued for it (immediately
        if something is already waiting, else block up to `timeout`)."""
        deadline = time.monotonic() + max(0.0, min(float(timeout), 60.0))
        with self._cond:
            rec = self._clients.get(client_id)
            if not rec:
                raise KeyError(client_id)
            while not rec["queued"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(remaining)
            out, rec["queued"] = rec["queued"][:MAX_REQUESTS_PER_POLL], \
                rec["queued"][MAX_REQUESTS_PER_POLL:]
            return out

    def submit_result(self, client_id: str, request_id: str, ok, result=None,
                      error=None, approved=None) -> dict | None:
        """Resolve one pending request; returns its {approval} or None when
        the request is unknown (already timed out, wrong client)."""
        with self._lock:
            pend = self._pending.get(request_id)
            if not pend or pend.client_id != client_id:
                return None
            if self._clients.get(client_id) is None:
                return None
        if approved is False:
            pend.approval = "denied"
            pend.result = {"ok": False, "error": "the user denied this action"}
        else:
            if pend.approval == "pending":
                pend.approval = "approved" if approved else "not needed"
            if ok:
                pend.result = {"ok": True, "result": result}
            else:
                pend.result = {"ok": False, "error": str(error or "tool failed")}
        pend.done.set()
        return {"approval": pend.approval}

    def _fail_client_requests_locked(self, client_id: str, reason: str) -> None:
        """A tool vanished at re-registration: nobody will ever answer its
        in-flight requests, so release the chat loop instead of letting it
        wait out the timeout."""
        for pend in list(self._pending.values()):
            if pend.client_id == client_id and not pend.done.is_set():
                pend.result = {"ok": False, "error": reason}
                pend.approval = "not approved"
                pend.done.set()

    def forget(self, client_id: str) -> None:
        with self._cond:
            self._clients.pop(client_id, None)
            self._cond.notify_all()


def now_elapsed(mono: float) -> float:
    return time.monotonic() - mono


BODY_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _clean_body(raw) -> dict:
    """Normalise a registration `body` object (docs/BODY.md §1) into
    {actions: [...], expressions: [...]} of bare lowercase names.
    None/absent = no body (a text-only client)."""
    if raw is None:
        return {"actions": [], "expressions": []}
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    out: dict[str, list[str]] = {}
    for key in ("actions", "expressions"):
        items = raw.get(key) or []
        if not isinstance(items, list):
            raise ValueError(f"body.{key} must be a list of names")
        names: list[str] = []
        for item in items:
            name = str(item or "").strip().lower().replace(" ", "_")
            if not BODY_NAME_RE.match(name):
                raise ValueError(f"body.{key} name '{item}' must match "
                                 "^[A-Za-z0-9_-]{1,64}$")
            if name not in names:
                names.append(name)
        out[key] = names
    return out


def summarize(tool_display: str, arguments: dict) -> str:
    """Short human phrase for approval prompts and logs."""
    try:
        args = json.dumps(arguments, ensure_ascii=False)[:160]
    except (TypeError, ValueError):
        args = str(arguments)[:160]
    return f"{tool_display} {args}"
