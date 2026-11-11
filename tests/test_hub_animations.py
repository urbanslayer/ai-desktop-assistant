"""Custom animations (docs/SELF_ANIMATION.md, BACKLOG item 6): validation and
clamping in hub/animations.py, the animations store (schema v10, model
scoping), the routes + animations_changed event + preview, the five tools
through the real chat loop, [act:<name>] tags in hub/body.py and the
animations.auto_keep setting.

tests/fixtures/animation_victory.json is the SHARED fixture (A6-page keeps its
own copy under client/js/fixtures/); it must validate with zero clamp notes.
"""
import copy
import json
import re
from pathlib import Path

import pytest

from hub import animations as A
from hub import body as _body
from hub.server import create_app
from hub.store import Store

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "animation_victory.json"


def fixture_doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def simple_doc(**over) -> dict:
    doc = {"name": "wiggle", "description": "a small wiggle",
           "duration_s": 1.0, "loop": False, "ease": "smooth",
           "keyframes": [{"t": 0.0, "bones": {}, "expression": {}},
                         {"t": 1.0, "bones": {}, "expression": {}}]}
    doc.update(over)
    return doc


# ── shared fixture + reserved-name drift guards ─────────────────────────────

def test_shared_fixture_validates_clean():
    clean, notes = A.validate_animation(fixture_doc())
    assert notes == []
    assert clean["name"] == "victory_dance"
    assert clean["keyframes"][0]["t"] == 0.0
    assert clean["keyframes"][-1]["t"] == clean["duration_s"]


def test_gesture_names_match_gestures_js():
    """The reserved built-in gesture list is parsed from client/js/gestures.js
    at test time: if the page ever renames/adds a gesture, this fails and the
    hub constant must follow (docs/SELF_ANIMATION.md: built-ins are reserved)."""
    src = (ROOT / "client" / "js" / "gestures.js").read_text(encoding="utf-8")
    m = re.search(r"export const GESTURES = \{(.*?)\n\};", src, re.S)
    assert m, "GESTURES library not found in gestures.js"
    names = re.findall(r"^  ([a-z][a-z0-9_]*): \{", m.group(1), re.M)
    assert names, "no gestures parsed"
    assert sorted(names) == sorted(A.gesture_names())
    # body.py's vocab (taggable gestures) is a subset (talk_* are not tags)
    assert set(_body.GESTURE_VOCAB) <= set(names)


def test_reserved_names_cover_gestures_and_action_targets():
    reserved = A.reserved_names()
    assert set(A.gesture_names()) <= reserved
    assert set(_body.ACTION_VERBS.values()) <= reserved
    for clash in ("wave", "think", "laugh", "shake_head"):
        with pytest.raises(ValueError, match="reserved"):
            A.validate_animation(simple_doc(name=clash))


# ── structural errors (each precise enough for the model to fix) ─────────────

@pytest.mark.parametrize("bad, msg", [
    ("nope", "JSON object"),                                  # not an object
    (simple_doc(name="Uppercase"), r"name must match"),
    (simple_doc(name="1lead"), r"name must match"),
    (simple_doc(name="x"), r"name must match"),               # too short
    (simple_doc(name="a" * 33), r"name must match"),          # too long
    (simple_doc(name="has-dash"), r"name must match"),
    (simple_doc(duration_s=0.2), "duration_s"),
    (simple_doc(duration_s=9.5), "duration_s"),
    (simple_doc(duration_s="1.0"), "duration_s"),
    (simple_doc(loop="yes"), "loop must be a boolean"),
    (simple_doc(ease="bouncy"), "ease must be one of smooth, linear"),
    (simple_doc(bogus=1), "unknown field"),
    (simple_doc(keyframes=[{"t": 0, "bones": {}, "expression": {}}]),
     "2..24 entries"),
    (simple_doc(keyframes=[]), "2..24 entries"),
    (simple_doc(keyframes="x"), "keyframes must be a list"),
    (simple_doc(keyframes=[{"bones": {}, "expression": {}},
                           {"t": 1.0}]), "t must be a number"),
    (simple_doc(keyframes=[{"t": 0.5, "bones": {}, "expression": {}},
                           {"t": 1.0, "bones": {}, "expression": {}}]),
     "first keyframe must have t=0"),
    (simple_doc(keyframes=[{"t": 0.0, "bones": {}, "expression": {}},
                           {"t": 0.5, "bones": {}, "expression": {}}]),
     "last keyframe must have t=duration_s"),
    (simple_doc(keyframes=[{"t": 0.0, "bones": {}, "expression": {}},
                           {"t": 0.4, "bones": {}, "expression": {}},
                           {"t": 0.4, "bones": {}, "expression": {}},
                           {"t": 1.0, "bones": {}, "expression": {}}]),
     "strictly greater"),
    (simple_doc(keyframes=[{"t": 0.0, "bones": {}, "expression": {}},
                           {"t": 2.0, "bones": {}, "expression": {}}]),
     "past duration_s"),
])
def test_structural_errors(bad, msg):
    with pytest.raises(ValueError, match=msg):
        A.validate_animation(bad)


def test_error_carries_animation_error_prefix_and_is_model_actionable():
    with pytest.raises(ValueError) as ei:
        A.validate_animation(simple_doc(name="Bad Name"))
    assert str(ei.value).startswith("animation_error:")
    assert "^[a-z][a-z0-9_]{1,31}$" in str(ei.value)


def test_unknown_bone_rejected_with_bone_list():
    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {}, "expression": {}},
        {"t": 0.5, "bones": {"wigglyBone": [1, 2, 3]}, "expression": {}},
        {"t": 1.0, "bones": {}, "expression": {}}])
    with pytest.raises(ValueError, match="unknown bone 'wigglyBone'"):
        A.validate_animation(doc)


def test_neck_rejected_not_allowed():
    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {}, "expression": {}},
        {"t": 0.5, "bones": {"neck": [10, 0, 0]}, "expression": {}},
        {"t": 1.0, "bones": {}, "expression": {}}])
    with pytest.raises(ValueError, match="neck.*not allowed"):
        A.validate_animation(doc)
    assert "neck" not in A.ALLOWED_BONES
    assert "neck" not in A.load_limits()


def test_bad_euler_shape_and_expression():
    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {"head": [10, 20]}, "expression": {}},
        {"t": 0.5, "bones": {}, "expression": {"joy": 1}},
        {"t": 1.0, "bones": {}, "expression": {"happy": "much"}}])
    with pytest.raises(ValueError) as ei:
        A.validate_animation(doc)
    err = str(ei.value)
    assert "head must be [x, y, z]" in err
    assert "unknown expression 'joy'" in err
    assert "expression happy must be a number" in err


def test_keyframe_unknown_field():
    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {}, "expression": {}},
        {"t": 0.5, "bone": {}, "expression": {}},
        {"t": 1.0, "bones": {}, "expression": {}}])
    with pytest.raises(ValueError, match="unknown field"):
        A.validate_animation(doc)


# ── clamping (notes out, values in range) ────────────────────────────────────

def test_clamps_with_notes():
    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {}, "expression": {}, "hips_y": -0.25},
        {"t": 0.5,
         "bones": {"leftUpperArm": [0, 0, 200],     # > 137 (pose.js 2.4 rad)
                   "spine": [40, 0, 0],             # > 15 (docs)
                   "head": [-50, 0, 0]},            # < -40 (docs)
         "expression": {"happy": 1.4},
         "hips_y": 2.0},
        {"t": 1.0, "bones": {}, "expression": {}}])
    clean, notes = A.validate_animation(doc)
    assert clean["keyframes"][1]["bones"]["leftUpperArm"] == [0, 0, 137]
    assert clean["keyframes"][1]["bones"]["spine"] == [15, 0, 0]
    assert clean["keyframes"][1]["bones"]["head"] == [-40, 0, 0]
    assert clean["keyframes"][1]["expression"]["happy"] == 1.0
    assert clean["keyframes"][1]["hips_y"] == 0.15
    joined = "; ".join(notes)
    assert "leftUpperArm.z 200 clamped to 137" in joined
    assert "spine.x 40 clamped to 15" in joined
    assert "head.x -50 clamped to -40" in joined
    assert "expression happy 1.4 clamped to 1" in joined
    assert "hips_y 2 clamped to 0.15" in joined


def test_limits_mirror_pose_js_grab_limits():
    """GRAB_LIMITS in client/js/pose.js (rad) must match animation_limits.json
    (deg) — drift fails this test."""
    src = (ROOT / "client" / "js" / "pose.js").read_text(encoding="utf-8")
    m = re.search(r"export const GRAB_LIMITS = \{(.*?)\};", src, re.S)
    assert m
    limits = A.load_limits()
    for bone, rad in re.findall(r"(\w+):\s*([\d.]+)", m.group(1)):
        deg = float(rad) * 180 / 3.141592653589793
        lo, hi = limits[bone]["x"]
        # floor: the hub limit must sit strictly inside the pose.js cap
        assert lo == -hi and int(deg) == hi, bone


def test_docs_fixed_limits():
    limits = A.load_limits()
    for bone in ("spine", "chest", "upperChest"):
        for axis in "xyz":
            assert limits[bone][axis] == [-15, 15], bone
    for axis in "xyz":
        assert limits["head"][axis] == [-40, 40]


def test_allowed_bone_set_exact():
    # every core bone + 28 finger bones, no neck
    assert len(A.FINGER_BONES) == 28
    assert "leftThumbProximal" in A.ALLOWED_BONES
    assert "rightLittleDistal" in A.ALLOWED_BONES
    assert "leftThumbIntermediate" not in A.ALLOWED_BONES
    for bone in A.CORE_BONES:
        assert bone in A.load_limits()


def test_invent_detector():
    yes = ["make up a little victory dance", "Invent a curtsy",
           "create a new move for me", "can you improvise a gesture?",
           "dream up a dance", "make up an animation"]
    no = ["what is a dance?", "I danced yesterday", "do your wave",
          "create a reminder", "I need a new dress", "dance with me"]
    for t in yes:
        assert A.detect_invent_request(t), t
    for t in no:
        assert not A.detect_invent_request(t), t


# ── store: round-trip + model scoping (schema v10) ──────────────────────────

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "hub.db")
    s.add_persona("riko", "Riko", [], "sys", "hi", "")
    s.add_persona("asuka", "Asuka", [], "sys", "hi", "")
    yield s
    s.close()


def _clean_doc(name="victory_dance"):
    clean, notes = A.validate_animation(fixture_doc())
    assert notes == []
    clean["name"] = name
    return clean


def test_schema_version_is_10():
    from hub.store import SCHEMA_VERSION
    assert SCHEMA_VERSION == 10


def test_store_round_trip_and_model_scoping(store):
    doc = _clean_doc()
    row = store.add_animation("riko", "shaAAA", doc)
    assert row and row["status"] == "draft" and row["model_sha256"] == "shaAAA"
    assert row["keyframes"] == doc["keyframes"]          # exact round-trip
    assert row["loop"] is False and row["duration_s"] == 2.4
    got = store.get_animation("riko", "victory_dance", "shaAAA")
    assert got == row

    # duplicate (persona, model, name) refused; same name on another model fine
    assert store.add_animation("riko", "shaAAA", doc) is None
    other = store.add_animation("riko", "shaBBB", doc)
    assert other and other["id"] != row["id"]
    # another persona too
    assert store.add_animation("asuka", "shaAAA", doc) is not None

    assert [a["model_sha256"] for a in
            store.list_animations("riko", model_sha256="shaAAA")] == ["shaAAA"]
    assert len(store.list_animations("riko")) == 2       # across models
    assert store.animation_models("riko") == ["shaAAA", "shaBBB"]

    # status filter + kept-first ordering
    upd, err = store.update_animation("riko", "victory_dance", "shaAAA",
                                      status="kept")
    assert err == "" and upd["status"] == "kept"
    store.add_animation("riko", "shaAAA", _clean_doc("newer_draft"))
    names = [(a["name"], a["status"]) for a in
             store.list_animations("riko", "shaAAA")]
    assert names[0] == ("victory_dance", "kept")
    assert ("newer_draft", "draft") in names
    assert store.list_animations("riko", "shaAAA", status="draft")[0][
        "name"] == "newer_draft"

    # rename inside the same model; conflict on an existing name
    upd, err = store.update_animation("riko", "newer_draft", "shaAAA",
                                      new_name="curtsy")
    assert err == "" and upd["name"] == "curtsy"
    upd, err = store.update_animation("riko", "curtsy", "shaAAA",
                                      new_name="victory_dance")
    assert upd is None and err == "conflict"
    upd, err = store.update_animation("riko", "ghost", "shaAAA",
                                      status="kept")
    assert upd is None and err == "missing"

    # description patch
    upd, err = store.update_animation("riko", "curtsy", "shaAAA",
                                      description="small bow")
    assert err == "" and upd["description"] == "small bow"

    # delete is model-scoped
    gone = store.delete_animation("riko", "victory_dance", "shaAAA")
    assert gone and gone["name"] == "victory_dance"
    assert store.get_animation("riko", "victory_dance", "shaAAA") is None
    assert store.get_animation("riko", "victory_dance", "shaBBB") is not None
    assert store.delete_animation("riko", "victory_dance", "shaAAA") is None

    # persona deletion cascades
    store.delete_persona("riko")
    assert store.list_animations("riko") == []
    assert store.list_animations("asuka") != []


def test_store_migrates_old_db(tmp_path):
    """A v9 hub DB (fresh Store then downgraded pragma) gains the animations
    table on reopen — mirrors the user_version migration path."""
    db = tmp_path / "hub.db"
    s = Store(db)
    s.close()
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("DROP TABLE animations")
    con.execute("PRAGMA user_version=9")
    con.commit()
    con.close()
    s2 = Store(db)
    assert s2.get_animation("riko", "x", "sha") is None   # table exists
    s2.close()


def test_model_sha_resolver(tmp_path):
    import hashlib
    r = A.ModelShaResolver(tmp_path)
    assert r("nobody") == A.MODEL_DEFAULT
    (tmp_path / "riko.vrm").write_bytes(b"glTF-fake-vrm")
    assert r("riko") == hashlib.sha256(b"glTF-fake-vrm").hexdigest()
    # cached across calls, refreshed when the bytes change
    assert r("riko") == hashlib.sha256(b"glTF-fake-vrm").hexdigest()
    (tmp_path / "riko.vrm").write_bytes(b"glTF-fake-vrm-v2")
    assert r("riko") == hashlib.sha256(b"glTF-fake-vrm-v2").hexdigest()


# ── routes + animations_changed event + preview + auto_keep ─────────────────

from fastapi.testclient import TestClient  # noqa: E402

VRM_BYTES = b"glTF" + b"-test-vrm-" + b"\0" * 16


@pytest.fixture
def hub(tmp_path):
    client = TestClient(create_app(tmp_path / "hubdata", api_key=None))
    ids = {p["id"] for p in client.get("/api/personas").json()}
    if "riko" not in ids:
        assert client.post("/api/personas",
                           json={"name": "Riko"}).status_code == 201
    client._ring = 0   # last seen event seq
    return client


def new_events(client, types=("animations_changed", "action")):
    out = client.get("/api/events", params={"after": client._ring,
                                            "wait": 0}).json()
    client._ring = out["last"]
    return [e for e in out["events"] if e["type"] in types]


def test_animations_routes_crud_event_and_preview(hub):
    doc = fixture_doc()
    r = hub.post("/api/personas/riko/animations", json=doc)
    assert r.status_code == 201, r.text
    row = r.json()["animation"]
    assert r.json()["clamped"] == []
    assert row["status"] == "draft" and row["model_sha256"] == "default"
    assert row["keyframes"][-1]["t"] == 2.4
    evs = new_events(hub)
    assert evs and evs[-1]["type"] == "animations_changed"
    assert evs[-1]["persona_id"] == "riko"
    assert evs[-1]["model_sha256"] == "default"

    g = hub.get("/api/personas/riko/animations").json()
    assert g["model_sha256"] == "default"
    assert [a["name"] for a in g["animations"]] == ["victory_dance"]
    assert hub.get("/api/personas/riko/animations?status=kept"
                   ).json()["animations"] == []
    assert len(hub.get("/api/personas/riko/animations?status=draft"
                       ).json()["animations"]) == 1
    assert hub.get("/api/personas/nobody/animations").status_code == 404
    assert hub.get("/api/personas/riko/animations?status=zz"
                   ).status_code == 422

    # keep + rename + description through PUT
    r = hub.put("/api/personas/riko/animations/victory_dance",
                json={"status": "kept"})
    assert r.status_code == 200 and r.json()["animation"]["status"] == "kept"
    r = hub.put("/api/personas/riko/animations/victory_dance",
                json={"name": "big_win", "description": "fists + hop"})
    assert r.json()["animation"]["name"] == "big_win"
    assert r.json()["animation"]["description"] == "fists + hop"
    assert hub.put("/api/personas/riko/animations/ghost",
                   json={"status": "kept"}).status_code == 404
    assert hub.put("/api/personas/riko/animations/big_win",
                   json={"status": "frozen"}).status_code == 422
    assert hub.put("/api/personas/riko/animations/big_win",
                   json={"name": "wave"}).status_code == 422   # reserved
    assert hub.put("/api/personas/riko/animations/big_win",
                   json={"dance": 1}).status_code == 422       # unknown key

    # preview rides the same action channel replies/actions use
    hub.get("/api/events", params={"after": hub._ring, "wait": 0})  # drain
    r = hub.post("/api/personas/riko/animations/big_win/preview")
    assert r.status_code == 200 and r.json()["ok"] is True
    acts = [e for e in new_events(hub, types=("action",))][-1]
    assert acts["persona_id"] == "riko"
    assert acts["actions"] == [{"name": "big_win", "at": 0.0,
                                "type": "gesture", "custom": True,
                                "preview": True}]
    assert hub.post("/api/personas/riko/animations/ghost/preview"
                    ).status_code == 404

    # delete (204) then 404
    assert hub.delete("/api/personas/riko/animations/big_win").status_code == 204
    assert hub.get("/api/personas/riko/animations").json()["animations"] == []
    assert hub.delete("/api/personas/riko/animations/big_win").status_code == 404
    assert len(new_events(hub)) == 1   # the delete published one


def test_animations_route_validation_and_clamp_notes(hub):
    bad = simple_doc(name="Bad Name")
    r = hub.post("/api/personas/riko/animations", json=bad)
    assert r.status_code == 422
    assert "animation_error" in r.json()["detail"]

    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {}, "expression": {}},
        {"t": 0.5, "bones": {"spine": [90, 0, 0]}, "expression": {}},
        {"t": 1.0, "bones": {}, "expression": {}}])
    r = hub.post("/api/personas/riko/animations", json=doc)
    assert r.status_code == 201
    body = r.json()
    assert any("spine.x 90 clamped to 15" in n for n in body["clamped"])
    assert body["animation"]["keyframes"][1]["bones"]["spine"] == [15, 0, 0]

    # duplicate name on the same model → 409; rename collision too
    assert hub.post("/api/personas/riko/animations",
                    json=simple_doc()).status_code == 409


def test_animations_are_scoped_to_the_current_avatar_model(hub):
    hub.post("/api/personas/riko/animations", json=fixture_doc())
    # upload an avatar: the persona's current model changes
    r = hub.put("/api/personas/riko/avatar",
                files={"file": ("furina.vrm", VRM_BYTES, "model/gltf-binary")})
    assert r.status_code == 200
    import hashlib
    sha = hashlib.sha256(VRM_BYTES).hexdigest()

    g = hub.get("/api/personas/riko/animations").json()
    assert g["model_sha256"] == sha
    assert g["animations"] == []                       # old rows are other-model
    g_all = hub.get("/api/personas/riko/animations?model=all").json()
    assert [a["model_sha256"] for a in g_all["animations"]] == ["default"]
    g_lit = hub.get(f"/api/personas/riko/animations?model={sha}").json()
    assert g_lit["animations"] == []
    g_lit2 = hub.get("/api/personas/riko/animations?model=default").json()
    assert len(g_lit2["animations"]) == 1

    # same name allowed on the new model
    r = hub.post("/api/personas/riko/animations", json=fixture_doc())
    assert r.status_code == 201 and r.json()["animation"]["model_sha256"] == sha
    # PUT/DELETE address ONE model each: the row on 'default' stays untouched
    # when the current model is addressed
    assert hub.put(f"/api/personas/riko/animations/victory_dance?model={sha}",
                   json={"status": "kept"}).status_code == 200
    assert hub.delete("/api/personas/riko/animations/victory_dance"
                      ).status_code == 204          # current model
    assert hub.get(f"/api/personas/riko/animations?model={sha}"
                   ).json()["animations"] == []
    assert len(hub.get("/api/personas/riko/animations?model=default"
                       ).json()["animations"]) == 1  # default row survived


def test_animations_auto_keep_setting(hub):
    s = hub.get("/api/settings").json()
    assert s["animations"] == {"auto_keep": False}
    assert s["defaults"]["animations"] == {"auto_keep": False}
    assert s["animations_source"] == {"auto_keep": "default"}

    r = hub.put("/api/settings", json={"animations": {"auto_keep": True}})
    assert r.status_code == 200
    assert r.json()["animations"] == {"auto_keep": True}
    assert r.json()["animations_source"] == {"auto_keep": "saved"}

    r = hub.post("/api/personas/riko/animations", json=fixture_doc())
    assert r.json()["animation"]["status"] == "kept"

    assert hub.put("/api/settings",
                   json={"animations": {"auto_keep": "yes"}}).status_code == 422
    assert hub.put("/api/settings",
                   json={"animations": []}).status_code == 422

    r = hub.post("/api/settings/reset", json={"animations": True})
    assert r.status_code == 200
    assert r.json()["animations"] == {"auto_keep": False}


# ── [act:<name>] action tags (docs/BODY.md + SELF_ANIMATION.md) ─────────────

def test_act_tag_only_resolves_for_kept_custom_names():
    kept = {"victory_dance"}
    clean, actions, ignored = _body.parse_actions(
        "Look! [act:victory_dance] wasn't that cool?", [], kept)
    assert clean == "Look! wasn't that cool?"
    assert actions[0]["name"] == "victory_dance"
    assert actions[0]["type"] == "gesture" and actions[0]["custom"] is True
    assert abs(actions[0]["at"] - 6 / len(clean)) < 0.05
    assert ignored == []

    # draft / unknown names: stripped + ignored, never played
    _, actions, ignored = _body.parse_actions("[act:somedance] hi", [], kept)
    assert actions == [] and ignored == ["somedance"]

    # bare tag naming a kept animation works too; built-ins keep their shape
    _, actions, _ = _body.parse_actions("hi [victory_dance] [wave]",
                                        ["wave"], kept)
    assert actions[1] == {"name": "wave", "at": 1.0}
    assert actions[0]["custom"] is True

    # [action:x] also reaches the custom set
    _, actions, _ = _body.parse_actions("[action:victory_dance]", [], kept)
    assert actions and actions[0]["custom"] is True


def test_body_prompt_lists_custom_moves():
    p = _body.body_prompt(["wave"], custom_actions=["victory_dance"])
    assert "victory_dance" in p and "[act:<name>]" in p
    assert "[act:" not in _body.body_prompt(["wave"])


def test_chat_turn_custom_tag_only_when_kept_on_current_model(tmp_path):
    from test_hub_tools import ScriptedLLM
    reply = "Ta-da! [act:victory_dance]"
    llm = ScriptedLLM([{"content": reply}])
    client = TestClient(create_app(
        tmp_path / "hubdata", api_key=None,
        llm_defaults={"base_url": llm.base_url, "model": "auto"},
        tools_defaults={"searxng_url": ""}))
    assert client.post("/api/personas",
                       json={"name": "Riko"}).status_code in (200, 201, 409)
    r = client.post("/api/personas/riko/animations", json=fixture_doc())
    assert r.status_code == 201
    res = client.post("/api/chat", json={"text": "show me something"}).json()
    assert "victory_dance" not in res["reply"]
    assert res["actions"] == []                 # draft: not taggable yet

    assert client.put("/api/personas/riko/animations/victory_dance",
                      json={"status": "kept"}).status_code == 200
    res = client.post("/api/chat", json={"text": "again!"}).json()
    assert any(a["name"] == "victory_dance" and a.get("custom") is True
               and a.get("type") == "gesture" for a in res["actions"])

    # upload a different avatar → the kept animation is on another model:
    # the tag no longer resolves
    client.put("/api/personas/riko/avatar",
               files={"file": ("x.vrm", VRM_BYTES, "model/gltf-binary")})
    res = client.post("/api/chat", json={"text": "again!"}).json()
    assert res["actions"] == []


# ── tools: create/keep/delete/list/adapt (docs/SELF_ANIMATION.md) ───────────

from hub.tools import animation_tools as _AT  # noqa: E402
from hub.tools.registry import permission_for  # noqa: E402


def _specs(store_obj, pid="riko", sha="shaAAA", auto_keep=False):
    capture: dict = {}
    specs = _AT.build_animation_specs(
        store_obj, pid,
        {"sha": lambda: sha, "auto_keep": lambda: auto_keep}, capture)
    return {s.name: s for s in specs}, capture


def _payload(spec, args):
    import json as _json
    return _json.loads(spec.run(args))


def test_create_tool_drafts_clamps_and_prepares_preview_action(store):
    specs, capture = _specs(store)
    doc = simple_doc(keyframes=[
        {"t": 0.0, "bones": {}, "expression": {}},
        {"t": 0.5, "bones": {"neck": [10, 0, 0]}, "expression": {}},
        {"t": 1.0, "bones": {}, "expression": {}}])
    out = _payload(specs["create_animation"], {"animation": doc})
    assert "error" in out and "neck" in out["error"]      # structural error
    doc["keyframes"][1]["bones"] = {"spine": [90, 0, 0]}
    out = _payload(specs["create_animation"], {"animation": doc})
    assert out["ok"] is True and out["status"] == "draft"
    assert any("spine.x" in n for n in out["clamped"])
    assert "draft" in out["note"] and "keep" in out["note"]
    assert capture["preview_action"] == {
        "name": "wiggle", "at": 0.0, "type": "gesture", "custom": True,
        "preview": True}
    row = store.get_animation("riko", "wiggle", "shaAAA")
    assert row["status"] == "draft"
    assert row["keyframes"][1]["bones"]["spine"] == [15, 0, 0]
    # duplicate → fixable error, auto_keep flips the saved status
    out = _payload(specs["create_animation"], {"animation": doc})
    assert "already exists" in out["error"]
    specs2, _ = _specs(store, sha="shaBBB", auto_keep=True)
    out = _payload(specs2["create_animation"], {"animation": fixture_doc()})
    assert out["status"] == "kept"


def test_keep_delete_list_tools(store):
    specs, _ = _specs(store)
    store.add_animation("riko", "shaAAA", _clean_doc(), status="draft")
    store.add_animation("riko", "shaZZZ", _clean_doc(
        name="old_move"), status="kept")
    out = _payload(specs["keep_animation"], {"name": "victory_dance"})
    assert out["ok"] and store.get_animation(
        "riko", "victory_dance", "shaAAA")["status"] == "kept"
    out = _payload(specs["keep_animation"], {"name": "ghost"})
    assert "no animation 'ghost'" in out["error"]
    assert "victory_dance" in out["error"]        # lists what exists
    out = _payload(specs["delete_animation"], {"name": "ghost"})
    assert "error" in out
    out = _payload(specs["delete_animation"], {"name": "victory_dance"})
    assert out["ok"] and store.get_animation("riko", "victory_dance",
                                             "shaAAA") is None
    out = _payload(specs["list_animations"], {})
    assert out["model_sha256"] == "shaAAA"
    flags = {a["name"]: a["on_current_model"] for a in out["animations"]}
    assert flags == {"old_move": False}


def test_adapt_tool_reclamps_and_scopes(store):
    specs, _ = _specs(store)
    doc = _clean_doc()
    doc["keyframes"][1]["bones"]["spine"] = [90, 0, 0]
    store.add_animation("riko", "shaZZZ", doc, status="kept")
    out = _payload(specs["adapt_animation"], {"name": "victory_dance"})
    assert out["ok"] and out["from_model"] == "shaZZZ"
    assert out["status"] == "draft"
    assert any("spine.x" in n for n in out["clamped"])
    row = store.get_animation("riko", "victory_dance", "shaAAA")
    assert row["keyframes"][1]["bones"]["spine"] == [15, 0, 0]
    assert "already exists" in _payload(
        specs["adapt_animation"], {"name": "victory_dance"})["error"]
    # ambiguous source models need from_model
    store.add_animation("riko", "shayyy", _clean_doc(name="bow"))
    store.add_animation("riko", "shazzz", _clean_doc(name="bow"))
    out = _payload(specs["adapt_animation"], {"name": "bow"})
    assert "from_model" in out["error"]
    out = _payload(specs["adapt_animation"],
                   {"name": "bow", "from_model": "shayyy"})
    assert out["ok"] and out["from_model"] == "shayyy"


def test_animation_tool_permissions_and_descriptions(store):
    specs, _ = _specs(store)
    assert specs["delete_animation"].permission_default == "ask"
    assert (permission_for("delete_animation", "hub", "danger", {},
                           default="ask") == "ask")
    assert (permission_for("delete_animation", "hub", "danger",
                           {"delete_animation": "always"}) == "always")
    assert permission_for("create_animation", "hub", "act", {}) == "always"
    for name, spec in specs.items():
        assert len(spec.description) <= 1500, (name, len(spec.description))
    assert "victory_dance" in specs["create_animation"].description


def test_chat_turn_create_animation_reply_carries_preview(tmp_path):
    from test_hub_tools import ScriptedLLM, call_step
    llm = ScriptedLLM([
        call_step("create_animation", {"animation": fixture_doc()}),
        {"content": "Ta-da! A little victory dance!"},
    ])
    client = TestClient(create_app(
        tmp_path / "hubdata", api_key=None,
        llm_defaults={"base_url": llm.base_url, "model": "auto"},
        tools_defaults={"searxng_url": ""}))
    assert client.post("/api/personas",
                       json={"name": "Riko"}).status_code in (200, 201, 409)
    res = client.post("/api/chat",
                      json={"text": "make up a victory dance"}).json()
    assert res["actions"] == [{"name": "victory_dance", "at": 0.0,
                               "type": "gesture", "custom": True,
                               "preview": True}]
    g = client.get("/api/personas/riko/animations").json()
    assert [(a["name"], a["status"]) for a in g["animations"]] == \
        [("victory_dance", "draft")]


def test_invent_guard_retries_once_without_tool_call(tmp_path):
    from test_hub_tools import ScriptedLLM
    llm = ScriptedLLM([{"content": "Dancing is not really for me."}])
    client = TestClient(create_app(
        tmp_path / "hubdata", api_key=None,
        llm_defaults={"base_url": llm.base_url, "model": "auto"},
        tools_defaults={"searxng_url": ""}))
    assert client.post("/api/personas",
                       json={"name": "Riko"}).status_code in (200, 201, 409)
    res = client.post("/api/chat",
                      json={"text": "make up a little dance for me"}).json()
    assert res["reply"]
    assert len(llm.chat_requests) >= 2       # the nudge retry happened
    assert "create_animation" in str(llm.chat_requests[1])
