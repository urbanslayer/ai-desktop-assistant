"""Custom animation hub tools (docs/SELF_ANIMATION.md).

create_animation / keep_animation / delete_animation / list_animations /
adapt_animation for one chat turn. All names address the persona's KEPT or
DRAFT animations on the model the clients are currently wearing (the
persona's stored VRM sha256, 'default' without an upload) — except
list_animations (every model of the persona) and adapt_animation (which
copies a named animation from another model onto this one, re-clamped).

Validation + clamping live in hub/animations.py; a rejected animation comes
back as {"error": "<what to fix>"} so the model can repair and retry in the
same turn.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Mapping

try:  # package import (repo root)
    from .. import animations as _animations
except (ImportError, SystemError):  # standalone: hub/ is on sys.path (Docker)
    import animations as _animations  # type: ignore[no-redef]


def _animation_doc_description() -> str:
    example = ('{"name":"victory_dance","description":"fists up, hop",'
               '"duration_s":2.4,"loop":false,"ease":"smooth","keyframes":['
               '{"t":0,"bones":{},"expression":{}},'
               '{"t":0.6,"bones":{"leftUpperArm":[0,0,70],'
               '"rightUpperArm":[0,0,-70],"leftLowerArm":[0,-90,0]},'
               '"expression":{"happy":0.8},"hips_y":0.04},'
               '{"t":1.2,"bones":{"leftUpperArm":[0,0,55],'
               '"rightUpperArm":[0,0,-85],"leftLowerArm":[0,-70,0],'
               '"spine":[4,0,0]},"expression":{"happy":1}},'
               '{"t":2.4,"bones":{},"expression":{}}]}')
    return (
        "Doc: {name: lowercase snake_case 2-32, unused on this model, never "
        "a built-in gesture name; description: short; duration_s: 0.4..8; "
        "loop: bool; ease: smooth|linear; keyframes: 2..24x {t: seconds, "
        "first 0, last = duration_s; bones: {bone: [x,y,z]}; expression: "
        "{face: 0..1}; hips_y: -0.25..0.15}}. Bone rotations are LOCAL Euler "
        "DEGREES vs the rest pose, mirrored sign left/right; start and end "
        "at rest (empty bones). Bones: hips, spine, chest, upperChest, head, "
        "then Shoulder, UpperArm, LowerArm, Hand, UpperLeg, LowerLeg, Foot "
        "with left/right prefixes; neck is FORBIDDEN; finger bones like "
        "leftIndexProximal (Thumb has no Intermediate). Limits: spine/chest/"
        "upperChest ±15, head ±40, shoulders ±30, hips ±45 (±60 y), arms/"
        "legs to ±137 — larger values clamp, reported in `clamped`. "
        "Expressions: happy angry sad relaxed surprised + visemes aa ih ou "
        "ee oh blink. Example: " + example)


TOOL_DESCRIPTIONS = {
    "create_animation": "Invent a new move: write keyframes, the hub "
                        "validates+clamps them, save a draft, preview it, "
                        "ask the owner to keep it.",
    "keep_animation": "Keep a drafted animation as a permanent move of this "
                      "model.",
    "delete_animation": "Delete an animation of this model.",
    "list_animations": "List the persona's custom animations across models.",
    "adapt_animation": "Copy an animation from another VRM model onto the "
                       "current one, re-clamped.",
}


def build_animation_specs(store: Any, persona_id: str,
                          anim_api: Mapping[str, Callable[..., Any]],
                          capture: dict) -> list:
    """The five animation tools for one turn. `anim_api` binds 'sha'
    (current model sha256) and 'auto_keep' (bool). `capture` is the turn's
    mutable dict: create_animation stores the reply-carried preview play
    action under 'preview_action' (docs/SELF_ANIMATION.md)."""
    from .registry import ToolSpec

    sha = anim_api["sha"]
    auto_keep = anim_api["auto_keep"]

    def find(raw: Any):
        n = _animations.normalize_name(raw)
        if not n:
            return None, "give the animation's name"
        row = store.get_animation(persona_id, n, sha())
        if row is None:
            known = [r["name"] for r in
                     store.list_animations(persona_id, model_sha256=sha())]
            hint = ("known: " + ", ".join(known)) if known else \
                "this model has no animations yet"
            return None, f"no animation '{n}' on the current model ({hint})"
        return row, ""

    def run_create(args: dict) -> str:
        try:
            clean, notes = _animations.validate_animation(args.get("animation"))
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        status = "kept" if auto_keep() else "draft"
        row = store.add_animation(persona_id, sha(), clean, status=status)
        if row is None:
            return json.dumps({
                "error": f"'{clean['name']}' already exists on this model — "
                         "rename it or delete_animation first"},
                ensure_ascii=False)
        capture["preview_action"] = {"name": clean["name"], "at": 0.0,
                                     "type": "gesture", "custom": True,
                                     "preview": True}
        out = {"ok": True, "name": row["name"], "status": row["status"],
               "duration_s": row["duration_s"]}
        if notes:
            out["clamped"] = notes
        out["note"] = (
            "You are performing it now as a preview. " +
            ("It is saved: tell the owner, no need to ask." if status == "kept"
             else "It is saved as a draft: describe it, and ask the owner "
                  "whether to keep it (keep_animation) or delete it "
                  "(delete_animation). Only kept moves can be tagged "
                  "[act:" + clean["name"] + "] later."))
        return json.dumps(out, ensure_ascii=False)

    def run_keep(args: dict) -> str:
        row, msg = find(args.get("name"))
        if row is None:
            return json.dumps({"error": msg}, ensure_ascii=False)
        if row["status"] == "kept":
            return json.dumps({"ok": True, "name": row["name"],
                               "note": "it was already kept"},
                              ensure_ascii=False)
        upd, cerr = store.update_animation(persona_id, row["name"], sha(),
                                           status="kept")
        if upd is None:
            return json.dumps({"error": msg or "keep failed"},
                              ensure_ascii=False)
        return json.dumps({"ok": True, "name": upd["name"],
                           "status": "kept",
                           "note": "the owner keeps it: tag it [act:" +
                                   upd["name"] + "] whenever it fits"},
                          ensure_ascii=False)

    def run_delete(args: dict) -> str:
        row, msg = find(args.get("name"))
        if row is None:
            return json.dumps({"error": msg}, ensure_ascii=False)
        if store.delete_animation(persona_id, row["name"], sha()) is None:
            return json.dumps({"error": msg}, ensure_ascii=False)
        return json.dumps({"ok": True, "deleted": row["name"]},
                          ensure_ascii=False)

    def run_list(args: dict) -> str:
        current = sha()
        rows = store.list_animations(persona_id)
        return json.dumps({
            "model_sha256": current,
            "animations": [{
                "name": r["name"], "description": r["description"],
                "duration_s": r["duration_s"], "status": r["status"],
                "loop": r["loop"], "on_current_model":
                    r["model_sha256"] == current,
                "model_sha256": r["model_sha256"]} for r in rows],
            "note": "only animations on the current model can be performed "
                    "or previewed; adapt_animation brings another model's "
                    "move over"}, ensure_ascii=False)

    def run_adapt(args: dict) -> str:
        n = _animations.normalize_name(args.get("name"))
        if not n:
            return json.dumps({"error": "give the animation's name"},
                              ensure_ascii=False)
        current = sha()
        rows = [r for r in store.list_animations(persona_id)
                if r["name"] == n and r["model_sha256"] != current]
        src_model = _animations.normalize_name(args.get("from_model"))
        if src_model:
            rows = [r for r in rows if r["model_sha256"] == src_model]
        if not rows:
            return json.dumps({"error": f"no animation '{n}' on another "
                             "model to copy from (list_animations shows all)"},
                              ensure_ascii=False)
        if len(rows) > 1:
            return json.dumps({"error": f"'{n}' exists on "
                              f"{len(rows)} other models — pass from_model "
                              "(one of: " +
                              ", ".join(r["model_sha256"][:12]
                                        for r in rows) + ")"},
                              ensure_ascii=False)
        src = rows[0]
        if store.get_animation(persona_id, n, current) is not None:
            return json.dumps({"error": f"'{n}' already exists on the "
                              "current model"}, ensure_ascii=False)
        doc = {"name": src["name"], "description": src["description"],
               "duration_s": src["duration_s"], "loop": src["loop"],
               "ease": src["ease"], "keyframes": src["keyframes"]}
        try:
            clean, notes = _animations.validate_animation(doc)
        except ValueError as exc:   # source predates today's validator
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        status = "kept" if auto_keep() else "draft"
        row = store.add_animation(persona_id, current, clean, status=status)
        if row is None:
            return json.dumps({"error": f"'{n}' already exists on the "
                              "current model"}, ensure_ascii=False)
        out = {"ok": True, "name": row["name"], "status": row["status"],
               "from_model": src["model_sha256"],
               "note": "copied onto this model and re-checked; preview it "
                       "with the Animations page or after keep, tag it [act:"
                       + row["name"] + "]"}
        if notes:
            out["clamped"] = notes
        return json.dumps(out, ensure_ascii=False)

    def label_of(verb):
        return lambda args: f"{verb} {(str(args.get('name') or '')).strip()}"

    doc_desc = _animation_doc_description()
    name_prop = {"type": "string",
                 "description": "the animation's name (snake_case)"}
    specs = [
        ToolSpec(name="create_animation", display="create_animation",
                 description="Invent a new move or dance: hand-write "
                             "keyframes; the hub validates, clamps, saves; "
                             "you perform it now as a preview, then ask to "
                             "keep it. " + doc_desc,
                 parameters={"type": "object", "properties": {
                     "animation": {"type": "object",
                                   "description": "the animation doc"}},
                     "required": ["animation"]},
                 source="hub", risk="act", run=run_create,
                 label=lambda args: "invent a move"),
        ToolSpec(name="keep_animation", display="keep_animation",
                 description="Keep a drafted animation (the owner said yes) "
                             "so it becomes a permanent move of this model.",
                 parameters={"type": "object", "properties":
                             {"name": name_prop}, "required": ["name"]},
                 source="hub", risk="act", run=run_keep,
                 label=label_of("keep")),
        ToolSpec(name="delete_animation", display="delete_animation",
                 description="Delete an animation of this model (the owner "
                             "said no, or wants it gone).",
                 parameters={"type": "object", "properties":
                             {"name": name_prop}, "required": ["name"]},
                 source="hub", risk="danger", run=run_delete,
                 label=label_of("delete"),
                 permission_default="ask"),
        ToolSpec(name="list_animations", display="list_animations",
                 description="List the persona's custom animations across "
                             "models: name, status (kept|draft), duration, "
                             "whether each is on the model you are wearing.",
                 parameters={"type": "object", "properties": {}},
                 source="hub", risk="read", run=run_list,
                 label=lambda args: "list animations"),
        ToolSpec(name="adapt_animation", display="adapt_animation",
                 description="Bring an animation made for another VRM model "
                             "onto the current one, re-clamped to this "
                             "body's limits. from_model is optional (needed "
                             "only when the name exists on several models).",
                 parameters={"type": "object", "properties": {
                     "name": name_prop,
                     "from_model": {"type": "string",
                                    "description": "source model_sha256 "
                                    "(or 'default')"}},
                     "required": ["name"]},
                 source="hub", risk="act", run=run_adapt,
                 label=label_of("adapt")),
    ]
    return specs
