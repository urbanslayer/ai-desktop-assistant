"""Tests for riko.shortcuts: defaults, overrides, validation, the no-Super rule."""
from riko import shortcuts

EXPECTED_DEFAULTS = {
    "type": ["Enter", "/"],
    "talk": ["Space"],
    "persona_next": ["P"],
    "hide": ["H"],
    "wave": ["W"],
    "forget": ["Ctrl+Shift+Backspace"],
    "quit": ["Ctrl+Q"],
}


def resolve(raw=None):
    resolved, warnings = shortcuts.resolve(raw, warn=False)
    return resolved, warnings


def test_defaults_when_nothing_configured():
    resolved, warnings = resolve()
    assert resolved == EXPECTED_DEFAULTS
    assert warnings == []
    # the shipped config keeps the documented defaults too
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "character_config.yaml")
                         .read_text(encoding="utf-8"))
    assert resolve(cfg["desktop"]["shortcuts"])[0] == EXPECTED_DEFAULTS


def test_defaults_are_already_normalized():
    for bindings in shortcuts.DEFAULTS.values():
        for b in bindings:
            assert shortcuts.normalize(b) == (b, None)


def test_missing_action_keeps_default():
    resolved, _ = resolve({"hide": "Ctrl+H"})
    assert resolved["hide"] == ["Ctrl+H"]
    assert resolved["talk"] == ["Space"] and resolved["quit"] == ["Ctrl+Q"]


def test_disable_with_empty_string_or_list():
    assert resolve({"talk": ""})[0]["talk"] == []
    assert resolve({"talk": []})[0]["talk"] == []
    assert resolve({"type": None})[0]["type"] == []


def test_list_of_keys():
    resolved, warnings = resolve({"type": ["F2", "Ctrl+T"]})
    assert resolved["type"] == ["F2", "Ctrl+T"]
    assert warnings == []
    assert resolve({"talk": " "})[0]["talk"] == ["Space"]  # a literal space bar


def test_super_meta_win_hyper_rejected():
    for bad in ["Super+Q", "Meta+H", "WIN+d", "Hyper+P", "Super+Space"]:
        resolved, warnings = resolve({"hide": bad})
        assert resolved["hide"] == [], bad
        assert any("window manager" in w for w in warnings), (bad, warnings)


def test_unknown_modifier_rejected():
    resolved, warnings = resolve({"quit": "Cmd+Q"})  # mac Cmd is Meta too
    assert resolved["quit"] == []
    assert any("Cmd" in w or "Meta" in w for w in warnings)
    resolved, warnings = resolve({"quit": "Foo+Q"})
    assert resolved["quit"] == []
    assert any("unknown modifier" in w for w in warnings)


def test_unknown_key_rejected():
    resolved, warnings = resolve({"hide": "Ctrl+NotAKey"})
    assert resolved["hide"] == []
    assert any("unknown key" in w for w in warnings)


def test_duplicate_key_warned_and_dropped():
    # persona_next already owns P by default; hide asking for it too is a mistake
    resolved, warnings = resolve({"hide": "P"})
    assert resolved["persona_next"] == ["P"]
    assert resolved["hide"] == []
    assert any("already bound" in w for w in warnings)


def test_normalization():
    cases = {
        "shift+ctrl+q": "Ctrl+Shift+Q",       # modifier order + letter case
        "Control + q": "Ctrl+Q",              # modifier alias and spaces
        " ": "Space", "space": "Space",
        "esc": "Escape", "return": "Enter", "f5": "F5", "F12": "F12",
        "alt+/": "Alt+/", "P": "P", "up": "ArrowUp",
    }
    for raw, want in cases.items():
        assert shortcuts.normalize(raw) == (want, None), raw


def test_unknown_action_warned():
    resolved, warnings = resolve({"sit_on_lap": "X", "talk": "Space"})
    assert any("sit_on_lap" in w for w in warnings)
    assert "sit_on_lap" not in resolved
    assert resolved["talk"] == ["Space"]


def test_non_mapping_config_warns_and_uses_defaults():
    resolved, warnings = resolve(["Space", "P"])
    assert resolved == EXPECTED_DEFAULTS
    assert any("mapping" in w for w in warnings)
