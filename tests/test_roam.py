"""Roam strip persistence (docs/WALK_MEMORY.md W): x survives restarts."""
from riko import roam


def test_save_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("RIKO_ROAM_STATE", str(tmp_path / "nested" / "roam_x.json"))
    assert roam.load_x() is None            # nothing saved yet
    assert roam.save_x(-0.42) is True
    assert roam.load_x() == -0.42
    assert roam.save_x(1.5) is True         # last write wins
    assert roam.load_x() == 1.5


def test_bad_values_rejected_or_ignored(monkeypatch, tmp_path):
    p = tmp_path / "roam_x.json"
    monkeypatch.setenv("RIKO_ROAM_STATE", str(p))
    for bad in ("0.5", None, True, float("nan"), float("inf")):
        assert roam.save_x(bad) is False, bad
    p.write_text("{ not json", encoding="utf-8")
    assert roam.load_x() is None
    p.write_text('{"x": "nope"}', encoding="utf-8")
    assert roam.load_x() is None
    p.write_text('{"x": 2.25}', encoding="utf-8")
    assert roam.load_x() == 2.25
