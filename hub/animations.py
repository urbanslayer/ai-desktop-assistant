"""Custom animations: validation, clamping and reserved names (docs/SELF_ANIMATION.md).

The LLM authors keyframes in the literal contract format (hub/animation_limits.json
owns the per-bone limits, mirroring client/js/pose.js grab/joint limits where they
exist). This module is pure: `validate_animation` either returns a normalised
document plus a list of human-readable clamp notes, or raises ValueError with
precise, model-actionable messages (bad name, reserved name, keyframe count /
timing, unknown bone, forbidden `neck`, bad expression…). Storage lives in
hub/store.py (animations table, schema v10); routes/tools wire it in server.py.

`model_sha256` (the avatar identity an animation is scoped to) is the sha256 of
the persona's stored VRM bytes as the hub serves them; personas without an
uploaded avatar use MODEL_DEFAULT ('default'). The resolver caches by
(mtime, size) so the hash is computed at most once per avatar version.
"""
import hashlib
import json
import re
import threading
from pathlib import Path

try:
    from . import body as _body
except (ImportError, SystemError):  # standalone: hub/ is on sys.path
    import body as _body  # type: ignore[no-redef]

_HERE = Path(__file__).resolve().parent
LIMITS_PATH = _HERE / "animation_limits.json"

MODEL_DEFAULT = "default"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
EASES = ("smooth", "linear")
EXPRESSIONS = ("happy", "angry", "sad", "relaxed", "surprised",
               "aa", "ih", "ou", "ee", "oh", "blink")
MIN_KEYFRAMES, MAX_KEYFRAMES = 2, 24
MIN_DURATION, MAX_DURATION = 0.4, 8.0
MAX_DESCRIPTION_CHARS = 200
HIPS_Y_MIN, HIPS_Y_MAX = -0.25, 0.15
MAX_ERRORS = 8

CORE_BONES = ("hips", "spine", "chest", "upperChest", "head",
              "leftShoulder", "rightShoulder",
              "leftUpperArm", "rightUpperArm",
              "leftLowerArm", "rightLowerArm",
              "leftHand", "rightHand",
              "leftUpperLeg", "rightUpperLeg",
              "leftLowerLeg", "rightLowerLeg",
              "leftFoot", "rightFoot")
# three-vrm normalized humanoid finger bones (Thumb has no Intermediate)
FINGERS = ("Thumb", "Index", "Middle", "Ring", "Little")
FINGER_SEGS = {"Thumb": ("Proximal", "Distal"),
               "Index": ("Proximal", "Intermediate", "Distal"),
               "Middle": ("Proximal", "Intermediate", "Distal"),
               "Ring": ("Proximal", "Intermediate", "Distal"),
               "Little": ("Proximal", "Intermediate", "Distal")}
FINGER_BONES = tuple(f"{side}{finger}{seg}"
                     for side in ("left", "right")
                     for finger in FINGERS
                     for seg in FINGER_SEGS[finger])
ALLOWED_BONES = frozenset(CORE_BONES) | frozenset(FINGER_BONES)
# garment skinning (docs/BODY.md): neck may never rotate
FORBIDDEN_BONES = frozenset({"neck"})
LEG_BONES = frozenset(("leftUpperLeg", "rightUpperLeg",
                       "leftLowerLeg", "rightLowerLeg",
                       "leftFoot", "rightFoot"))

# VRMA clip names the hub itself knows (it does not import .vrma files — none
# are listed in the hub today; the client-side library owns that list).
VRMA_CLIPS: tuple = ()


# The desktop's built-in gesture library (client/js/gestures.js GESTURES keys,
# including the talk_1..3 gestures body.GESTURE_VOCAB does not tag).
# tests/test_hub_animations.py re-parses gestures.js and fails on drift.
GESTURE_NAMES = ["wave", "think", "listen", "talk_1", "talk_2", "talk_3",
                 "stretch", "look_around", "fidget", "hair_touch", "nod",
                 "shake_head", "laugh", "pout"]


def gesture_names() -> list[str]:
    """The built-in gesture names (client/js/gestures.js == GESTURE_NAMES;
    the test re-parses that file so drift fails the suite)."""
    return list(GESTURE_NAMES)


def normalize_name(raw) -> str:
    """`Victory Dance` / `Victory-Dance` -> `victory_dance` ('' when empty)."""
    return _body.normalize_action_name(raw)


def reserved_names() -> frozenset:
    """Names a custom animation may never take: built-in gesture names, every
    action-tag target (ACTION_VERBS values) and VRMA clips known to the hub."""
    return (frozenset(gesture_names())
            | frozenset(_body.ACTION_VERBS.values())
            | frozenset(VRMA_CLIPS))


# ── limits file ──────────────────────────────────────────────────────────────

_limits_cache: dict | None = None
_limits_lock = threading.Lock()


def load_limits() -> dict:
    """bone name -> {x: [min,max], y: […], z: […]} (degrees). Finger bones all
    share `finger_default`. Cached; a corrupt/missing file raises (a hub
    without its limits file must not silently accept anything)."""
    global _limits_cache
    with _limits_lock:
        if _limits_cache is None:
            raw = json.loads(LIMITS_PATH.read_text(encoding="utf-8"))
            bones = dict(raw.get("bones") or {})
            default = raw.get("finger_default")
            if not bones or not default:
                raise RuntimeError(f"animation_limits.json is malformed")
            for bone in FINGER_BONES:
                bones.setdefault(bone, dict(default))
            _limits_cache = bones
        return _limits_cache


# ── validation / clamping ────────────────────────────────────────────────────

def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_f(v) -> float:
    """float() for values already checked with _num (typing helper)."""
    return float(v)  # type: ignore[arg-type]


# ── hold semantics (docs/SELF_ANIMATION.md, 2026-11 revision) ──────────────
#
# A bone / expression / hips_y value MISSING from a keyframe no longer means
# "rest at that key". It interpolates between the nearest keys where the
# track IS specified, and the FIRST/LAST keyframes act as implicit rest/0
# anchors when the track is absent there — so animations still start and end
# at rest. validate_animation expands every mentioned track into explicit
# per-key values at save time, so STORED docs are unambiguous (even an
# old page player reading "missing = rest" renders exactly the same move).
# client/js/keyframes.js implements the same fill defensively (desktop
# caches and fixtures can hold pre-expansion docs); client/js/fixtures/
# animation_hold_golden.json is the parity fixture both sides test against.

def _track_fill(spec: dict, ts: list[float], zero) -> list:
    """spec: keyIndex -> authored value (absent = missing). Returns one value
    per key, linear in TIME between the nearest specified anchors; the first
    and last keys are implicit `zero` anchors when the track is missing
    there. spec/zero are lists (bones) or scalars (expression, hips_y)."""
    n = len(ts)
    out: list = [None] * n
    for i in range(n):
        if i in spec:
            out[i] = spec[i]
            continue
        prev = max((k for k in spec if k < i), default=None)
        nxt = min((k for k in spec if k > i), default=None)
        if prev is None and nxt is None:      # no anchor at all: rest
            out[i] = zero
        elif prev is None:                    # implicit rest anchor at key 0
            assert nxt is not None
            va, vb = zero, spec[nxt]
            u = (ts[i] - ts[0]) / (ts[nxt] - ts[0]) if ts[nxt] > ts[0] else 0.0
            out[i] = _lerp(va, vb, u)
        elif nxt is None:                     # implicit rest anchor at last key
            va, vb = spec[prev], zero
            u = (ts[i] - ts[prev]) / (ts[n - 1] - ts[prev]) \
                if ts[n - 1] > ts[prev] else 0.0
            out[i] = _lerp(va, vb, u)
        else:
            va, vb = spec[prev], spec[nxt]
            u = (ts[i] - ts[prev]) / (ts[nxt] - ts[prev]) \
                if ts[nxt] > ts[prev] else 0.0
            out[i] = _lerp(va, vb, u)
    return out


def _lerp(a, b, u):
    if isinstance(a, list):
        return [round(a[j] + (b[j] - a[j]) * u, 3) for j in range(len(a))]
    return round(a + (b - a) * u, 3)


def fill_keyframe_holds(keyframes: list[dict]) -> list[dict]:
    """Expand sparse keyframes to explicit per-key values (hold semantics).
    Idempotent: an already-explicit list comes back value-identical."""
    n = len(keyframes)
    if n < 2:
        return [dict(k) for k in keyframes]
    ts = [float(k["t"]) for k in keyframes]
    out = [{**k, "bones": dict(k.get("bones") or {}),
            "expression": dict(k.get("expression") or {})} for k in keyframes]
    bone_track: dict[str, dict] = {}
    expr_track: dict[str, dict] = {}
    hips_track: dict[int, float] = {}
    for i, k in enumerate(keyframes):
        for bone, v in (k.get("bones") or {}).items():
            bone_track.setdefault(bone, {})[i] = v
        for ex, w in (k.get("expression") or {}).items():
            expr_track.setdefault(ex, {})[i] = w
        if k.get("hips_y") is not None:
            hips_track[i] = float(k["hips_y"])
    for bone, spec in bone_track.items():
        vals = _track_fill(spec, ts, [0.0, 0.0, 0.0])
        for i, v in enumerate(vals):
            out[i]["bones"][bone] = v
    for ex, spec in expr_track.items():
        vals = _track_fill(spec, ts, 0.0)
        for i, v in enumerate(vals):
            out[i]["expression"][ex] = v
    if hips_track:
        vals = _track_fill(hips_track, ts, 0.0)
        for i, v in enumerate(vals):
            out[i]["hips_y"] = v
    return out


# ── descriptive lint (non-fatal hints, docs/SELF_ANIMATION.md) ──────────────
# Cheap words-vs-numbers checks on the CREATED doc: when the description
# promises a move the numbers cannot show, the model gets a hint it can act
# on in the same turn. Hints never reject and never block the save.

_LINT_SWAY_RE = re.compile(
    r"sway|swaying|lean|tilt|side[- ]to[- ]side|rock(?:ing)?", re.IGNORECASE)
_LINT_HOP_RE = re.compile(
    r"hop|jump|bounce|leap|spring", re.IGNORECASE)
_LINT_RAISE_RE = re.compile(
    r"\bv\b|raise|arms? up|arms? high|overhead|reach up|arms? rise",
    re.IGNORECASE)

# measured on the sample avatar + Furina (docs/SELF_ANIMATION.md convention
# table): UpperArm ±Z swings the arm sideways — ~30° clears the shoulder
# line, ~80°+ reads as a raised/V pose; a hop needs hips_y >= ~0.08 m to
# read against the ground line.
LINT_ARM_RAISE_MIN_DEG = 30.0
LINT_HOP_MIN_M = 0.08   # measured: 0.03-0.05 reads as nothing; 0.12+ is a clear hop
_TORSO_BONES = ("spine", "chest", "upperChest", "head")


def lint_animation(doc: dict, clean: dict) -> list[str]:
    """Words-vs-numbers hints for one created animation (see above)."""
    notes: list[str] = []
    text = f"{clean.get('name', '')} {clean.get('description', '')}"
    kfs = clean.get("keyframes") or []

    def _zmax(bones):
        vals = [abs((k.get("bones") or {}).get(b, [0, 0, 0])[2])
                for k in kfs for b in bones]
        return max(vals, default=0.0)

    if _LINT_SWAY_RE.search(text) and _zmax(_TORSO_BONES) < 3.0:
        notes.append("hint: description says sway/lean/tilt but no Z-axis "
                     "keys on spine/chest/head — sideways motion is Z "
                     "(Y twists the torso, it barely registers sideways)")
    hips_vals = [k.get("hips_y") or 0.0 for k in kfs]
    if _LINT_HOP_RE.search(text) and max(hips_vals, default=0.0) < LINT_HOP_MIN_M:
        notes.append(f"hint: description says hop/jump but hips_y never "
                     f"reaches {LINT_HOP_MIN_M} m — a visible hop needs a "
                     "dip (~-0.03) then a rise (>=0.05); feet leave the "
                     "ground with the hips automatically")
    if _LINT_RAISE_RE.search(text) and _zmax(
            ("leftUpperArm", "rightUpperArm")) < LINT_ARM_RAISE_MIN_DEG:
        notes.append("hint: description raises the arms but |UpperArm.z| "
                     "stays under 30° — Z swings the arm sideways/up "
                     "(see the cookbook raise pair for the CURRENT rig: "
                     "leftUpperArm -Z / rightUpperArm +Z raise on the live "
                     "model); clear the shoulder at ~30°, a raised/V pose "
                     "needs |z| >= 75-105")
    return notes


def migrate_hold_semantics(store) -> int:
    """Hub-start migration (idempotent): rewrite every stored animation's
    keyframes with explicit hold-filled values so stored docs are
    unambiguous for any player. Returns the number of rows rewritten."""
    fixed = 0
    for row in store.iter_animations():
        kfs = row.get("keyframes") or []
        if len(kfs) < 2:
            continue
        try:
            filled = fill_keyframe_holds(kfs)
        except (KeyError, TypeError, ValueError):
            continue            # a row the current validator never produced
        if filled != kfs:
            if store.set_animation_keyframes(row["id"], filled):
                fixed += 1
    return fixed


def validate_animation(doc, reserved=None) -> tuple[dict, list[str]]:
    """Check + clamp one keyframe document (docs/SELF_ANIMATION.md format).

    Returns (normalised_doc, clamp_notes); raises ValueError('animation_error:
    …; …') listing up to MAX_ERRORS precise problems the model can fix.
    normalised_doc: {name, description, duration_s, loop, ease, keyframes}
    with keyframes = [{t, bones: {name: [x,y,z]}, expression: {…}, hips_y?}].
    """
    if not isinstance(doc, dict):
        raise ValueError("animation_error: animation must be a JSON object")
    limits = load_limits()
    reserved = reserved_names() if reserved is None else frozenset(reserved)
    errors: list[str] = []
    notes: list[str] = []
    err = errors.append

    unknown = [k for k in doc
               if k not in ("name", "description", "duration_s", "loop",
                            "ease", "keyframes")]
    if unknown:
        err(f"unknown field(s) {', '.join(sorted(map(str, unknown)))}; "
            "allowed: name, description, duration_s, loop, ease, keyframes")

    name = doc.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        err("name must match ^[a-z][a-z0-9_]{1,31}$ (lowercase letters, "
            "digits, underscores, starting with a letter)")
        name = ""
    elif name in reserved:
        err(f"name '{name}' is reserved (built-in gesture/action name); "
            "pick a different one")

    desc = doc.get("description", "")
    if not isinstance(desc, str):
        err("description must be a string")
        desc = ""
    desc = desc.strip()
    if len(desc) > MAX_DESCRIPTION_CHARS:
        desc = desc[:MAX_DESCRIPTION_CHARS].rstrip()
        notes.append(f"description trimmed to {MAX_DESCRIPTION_CHARS} chars")

    dur = doc.get("duration_s")
    if not _num(dur) or not (MIN_DURATION <= _as_f(dur) <= MAX_DURATION):
        err(f"duration_s must be a number between {MIN_DURATION} and "
            f"{MAX_DURATION} seconds")
        dur = None  # type: ignore[assignment]

    loop = doc.get("loop", False)
    if not isinstance(loop, bool):
        err("loop must be a boolean")
        loop = False

    ease = doc.get("ease", "smooth")
    if ease not in EASES:
        err(f"ease must be one of {', '.join(EASES)}")
        ease = "smooth"

    kfs = doc.get("keyframes")
    clean_kfs: list[dict] = []
    if not isinstance(kfs, list):
        err("keyframes must be a list")
    elif not (MIN_KEYFRAMES <= len(kfs) <= MAX_KEYFRAMES):
        err(f"keyframes must hold {MIN_KEYFRAMES}..{MAX_KEYFRAMES} entries "
            f"(got {len(kfs)})")
    else:
        prev_t = None
        for i, kf in enumerate(kfs):
            if not isinstance(kf, dict):
                err(f"keyframe {i} must be an object")
                continue
            bad_keys = [k for k in kf
                        if k not in ("t", "bones", "expression", "hips_y")]
            if bad_keys:
                err(f"keyframe {i} has unknown field(s) "
                    f"{', '.join(sorted(map(str, bad_keys)))}; "
                    "allowed: t, bones, expression, hips_y")
            t = kf.get("t")
            if not _num(t) or _as_f(t) < 0:
                err(f"keyframe {i}: t must be a number >= 0 (seconds)")
                continue
            t = _as_f(t)
            if dur is not None and t > _as_f(dur) + 1e-6:
                err(f"keyframe {i}: t={t} is past duration_s={dur}")
                continue
            if prev_t is not None and not t > prev_t:
                err(f"keyframe {i}: t={t} must be strictly greater than the "
                    f"previous t={prev_t}")
                continue
            if i == 0 and t != 0.0:
                err(f"the first keyframe must have t=0 (got {t})")
                continue
            if i == len(kfs) - 1 and dur is not None \
                    and abs(t - _as_f(dur)) > 0.05:
                err(f"the last keyframe must have t=duration_s={dur} "
                    f"(got {t})")
                continue
            prev_t = t

            bones = kf.get("bones", {})
            if bones is None:
                bones = {}
            if not isinstance(bones, dict):
                err(f"keyframe {i}: bones must be an object")
                bones = {}
            clean_bones: dict[str, list[float]] = {}
            for bone, euler in bones.items():
                if bone in FORBIDDEN_BONES:
                    err(f"keyframe {i}: bone '{bone}' is not allowed "
                        "(garment skinning, docs/BODY.md)")
                    continue
                if bone not in ALLOWED_BONES:
                    err(f"keyframe {i}: unknown bone '{bone}'; use humanoid "
                        "normalized names (hips, spine, chest, upperChest, "
                        "head, left/rightShoulder, UpperArm, LowerArm, Hand, "
                        "UpperLeg, LowerLeg, Foot, plus finger bones like "
                        "leftIndexProximal)")
                    continue
                if not isinstance(euler, (list, tuple)) or len(euler) != 3 \
                        or not all(_num(v) for v in euler):
                    err(f"keyframe {i}: {bone} must be [x, y, z] Euler "
                        "degrees (local, vs rest pose)")
                    continue
                vals = []
                for axis, v in zip("xyz", euler):
                    lo, hi = limits[bone][axis]
                    v = _as_f(v)
                    if v < lo or v > hi:
                        clamped = min(hi, max(lo, v))
                        notes.append(f"keyframe {i}: {bone}.{axis} {v:g} "
                                     f"clamped to {clamped:g}")
                        v = clamped
                    vals.append(round(v, 3))
                clean_bones[bone] = vals

            expr = kf.get("expression", {})
            if expr is None:
                expr = {}
            if not isinstance(expr, dict):
                err(f"keyframe {i}: expression must be an object")
                expr = {}
            clean_expr: dict[str, float] = {}
            for ex_name, w in expr.items():
                if ex_name not in EXPRESSIONS:
                    err(f"keyframe {i}: unknown expression '{ex_name}'; use "
                        f"one of {', '.join(EXPRESSIONS)}")
                    continue
                if not _num(w):
                    err(f"keyframe {i}: expression {ex_name} must be a "
                        "number 0..1")
                    continue
                w = _as_f(w)
                if w < 0.0 or w > 1.0:
                    clamped = min(1.0, max(0.0, w))
                    notes.append(f"keyframe {i}: expression {ex_name} "
                                 f"{w:g} clamped to {clamped:g}")
                    w = clamped
                clean_expr[ex_name] = round(w, 3)

            entry: dict = {"t": round(t, 3), "bones": clean_bones,
                           "expression": clean_expr}
            if "hips_y" in kf and kf["hips_y"] is not None:
                hy = kf["hips_y"]
                if not _num(hy):
                    err(f"keyframe {i}: hips_y must be a number "
                        f"({HIPS_Y_MIN}..{HIPS_Y_MAX} metres)")
                else:
                    hy = _as_f(hy)
                    if hy < HIPS_Y_MIN or hy > HIPS_Y_MAX:
                        clamped = min(HIPS_Y_MAX, max(HIPS_Y_MIN, hy))
                        notes.append(f"keyframe {i}: hips_y {hy:g} clamped "
                                     f"to {clamped:g}")
                        hy = clamped
                    entry["hips_y"] = round(hy, 4)
            clean_kfs.append(entry)
    
    if errors:
        raise ValueError("animation_error: " + "; ".join(errors[:MAX_ERRORS])
                         + (f"; (+{len(errors) - MAX_ERRORS} more)"
                            if len(errors) > MAX_ERRORS else ""))
    # hold semantics: store every mentioned track EXPLICITLY per key
    # (docs/SELF_ANIMATION.md) so stored docs are unambiguous for any player
    clean_kfs = fill_keyframe_holds(clean_kfs)
    return ({"name": name, "description": desc,
             "duration_s": round(_as_f(dur), 3), "loop": loop, "ease": ease,
             "keyframes": clean_kfs}, notes)


def doc_from_row(row: dict) -> dict:
    """Rebuild a keyframe document from a stored animation row (adapt re-clamp)."""
    return {"name": row["name"], "description": row.get("description", ""),
            "duration_s": row["duration_s"], "loop": bool(row.get("loop")),
            "ease": row.get("ease", "smooth"),
            "keyframes": row.get("keyframes") or []}


# ── invent-a-move detector (intents fallback, docs/SELF_ANIMATION.md) ───────

_INVENT_RE = re.compile(
    r"\b(?:make\s+up|invent|create|improvise|improvize|dream\s+up|cook\s+up|"
    r"design|choreograph)\b"
    r"(?:\s+for\s+me|\s+me)?"
    r"(?:\s+(?:a|an|some|the|my|one))?"
    r"(?:\s+[a-z]+){0,2}"
    r"\s+(?:dance|dances|move|moves|motion|motions|animation|animations|"
    r"gesture|gestures|curtsy|curtsies)\b", re.IGNORECASE)

INVENT_NUDGE = ("(system) The user asked you to invent a move: call "
                "create_animation NOW with keyframes in the documented format "
                "(do not just describe or claim it), then ask whether to keep it.")


def detect_invent_request(text: str) -> bool:
    """The owner asks her to invent/make up a dance/move/gesture/animation.
    Conservative phrase list only — 'what is a dance?' or 'I danced' never fire
    (the verbs are all creation imperatives/requests)."""
    return bool(_INVENT_RE.search(str(text or "")))


# ── model identity (avatar sha256, cached by mtime+size) ─────────────────────

class ModelShaResolver:
    """model_sha256 for a persona: sha256 of its stored avatar VRM bytes (the
    exact file GET /api/personas/{id}/avatar serves), MODEL_DEFAULT when the
    persona has no uploaded avatar. Hashed lazily, cached by (mtime, size) so
    an unchanged avatar is hashed once per process."""

    def __init__(self, avatars_dir):
        self.dir = Path(avatars_dir)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple, str]] = {}

    def __call__(self, persona_id: str) -> str:
        path = self.dir / f"{persona_id}.vrm"
        try:
            st = path.stat()
        except OSError:
            return MODEL_DEFAULT
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(persona_id)
            if hit and hit[0] == stamp:
                return hit[1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._lock:
            self._cache[persona_id] = (stamp, digest)
        return digest

    def forget(self, persona_id: str) -> None:
        with self._lock:
            self._cache.pop(persona_id, None)
