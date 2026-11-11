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
