"""Recipe-catalogue tests (docs/COMMANDS.md 'Recipes'): lookup for the
owner's exact phrasings, the honesty rule (every default auto-runs or is
marked asks), config override/extend, and the tool surfaces. No command
is executed — lookup/validate are pure; validate calls decide() only."""
import pytest

from riko import client_tools as ct
from riko import commands as cp
from riko.commands import policy, recipes
from riko.commands.recipes import (DEFAULT_RECIPES, compact_desc, lookup,
                                   merge_recipes, score, validate)


def test_catalogue_size_sane():
    assert 25 <= len(DEFAULT_RECIPES) <= 35
    asks = [r["ask"] for r in DEFAULT_RECIPES]
    assert len(asks) == len(set(asks)), "duplicate asks"


# ── honesty rule ───────────────────────────────────────────────
def test_every_default_autoruns_or_is_marked_asks():
    assert validate() == []


def test_validate_flags_a_lying_recipe():
    bad = [{"ask": "a", "command": "rm -rf /srv"},        # asks-worthy, no flag
           {"ask": "b", "command": "sudo df"},            # refused outright
           {"ask": "c", "command": "pacman -Qi"},         # honest: auto
           {"ask": "d", "command": "cowsay <x>"},         # templated, no flag
           {"ask": "", "command": "df"}]
    problems = validate(bad)
    assert len(problems) == 4


# ── the owner's exact phrasings land on the right command ──────
@pytest.mark.parametrize("phrasing,command", [
    ("how much space is free on the root volume?", "df -h /"),
    ("what size is the home volume", "df -h /home"),
    ("how much is my home folder using?", "du -sh ~"),
    ("what's using my memory", "ps -eo pid,comm,%mem,%cpu --sort=-%mem"),
    ("is my GPU hot", "rocm-smi"),
    ("what ports are listening?", "ss -tuln"),
    ("any failed services?", "systemctl --failed"),
    ("any pending updates", "checkupdates"),
    ("temperatures please", "sensors"),
])
def test_owner_phrasings(phrasing, command):
    hits = lookup(phrasing, [dict(r) for r in DEFAULT_RECIPES])
    assert hits, phrasing
    assert hits[0]["command"] == command, \
        f"{phrasing!r} -> {hits[0]['command']!r} (not {command!r})"


def test_lookup_no_query_lists_all():
    out = lookup("", [dict(r) for r in DEFAULT_RECIPES])
    assert len(out) == len(DEFAULT_RECIPES)
    assert lookup(None, [dict(r) for r in DEFAULT_RECIPES])


def test_lookup_ranks_and_limits():
    cat = [dict(r) for r in DEFAULT_RECIPES]
    hits = lookup("disk space", cat, limit=3)
    assert 0 < len(hits) <= 3
    assert all(score("disk space", h) > 0 for h in hits)
    assert any(h["command"].startswith("df") for h in hits)


def test_lookup_returns_copies():
    cat = [dict(r) for r in DEFAULT_RECIPES]
    hits = lookup("uptime", cat)
    hits[0]["command"] = "tampered"
    assert any(r["command"] == "uptime" for r in DEFAULT_RECIPES)


# ── config override / extend (character_config commands.recipes) ─
def test_merge_override_and_extend():
    out = merge_recipes({"recipes": [
        {"ask": "is my GPU hot", "command": "radeontop -d - -l 1",
         "asks": True},                        # override the AMD default
        {"ask": "battery health", "command": "upower -i /dev/battery",
         "asks": True, "note": "custom"},      # brand-new ask
        {"ask": "", "command": "x"},           # ignored (no ask)
        "junk", "", None,                      # ignored
    ]})
    gpu = next(r for r in out if r["ask"] == "is my GPU hot")
    assert gpu["command"] == "radeontop -d - -l 1" and gpu["asks"] is True
    batt = next(r for r in out if r["ask"] == "battery health")
    assert batt["note"] == "custom"
    assert len(out) == len(DEFAULT_RECIPES) + 1
    # one entry per ask (override replaces, never duplicates)
    names = [r["ask"] for r in out]
    assert len(names) == len(set(names))


def test_merge_false_clears():
    assert merge_recipes({"recipes": False}) == []
    assert merge_recipes({}) == [dict(r) for r in DEFAULT_RECIPES]


def test_custom_recipes_reach_the_lookup_tool(tmp_path):
    """Config-extended catalogue is what the installed tool searches."""
    log = []
    pack = cp.install(
        sense=_FakeSense(), runtime=None, runner_fn=lambda *a, **k: {},
        env={}, printer=log.append,
        cfg={"commands": {"enabled": True, "recipes": [
            {"ask": "how is the wifi", "command": "nmcli -t -f ACTIVE wifi"}]}})
    try:
        _, ex = pack.provider(None)
        hits = ex["command_recipes"](query="how is the wifi")["recipes"]
        assert hits and hits[0]["command"].startswith("nmcli")
    finally:
        ct.unregister_extra(pack.provider)


class _FakeAwareness:
    enabled = True


class _FakeSense:
    awareness = _FakeAwareness()


# ── honesty at run time: recipe commands really auto-run ───────
@pytest.mark.parametrize("command", sorted(
    {r["command"] for r in DEFAULT_RECIPES
     if not r.get("asks") and not r.get("templated")}))
def test_recipe_commands_auto_run(command):
    """What the catalogue promises unattended, the policy grants —
    over the REAL home (df/du/journalctl paths are absolute or ~)."""
    d = policy.decide(command)
    assert d.mode == "auto", f"{command}: {d.mode} ({d.reason})"


def test_templated_recipe_placeholders():
    for r in DEFAULT_RECIPES:
        if r.get("templated"):
            assert r.get("asks") is True
            assert "<" in r["command"] and ">" in r["command"]


# ── surfaces ───────────────────────────────────────────────────
def test_compact_desc_budget_and_shape():
    block = compact_desc([dict(r) for r in DEFAULT_RECIPES])
    assert len(block) <= recipes.DESC_BUDGET
    assert "->" in block and "df -h /" in block
    assert "<pkg>" not in block              # templated recipes excluded


def test_tool_description_carries_the_block():
    specs = cp.command_tools()
    run_cmd = next(s for s in specs if s["name"] == "run_command")
    assert "Common asks -> command:" in run_cmd["description"]
    assert "df -h /" in run_cmd["description"]
    rec = next(s for s in specs if s["name"] == "command_recipes")
    assert rec["risk"] == "read"
    assert "query" in rec["parameters"]["properties"]


def test_recipes_tool_results(tmp_path):
    svc = cp.CommandService(cfg={}, home=str(tmp_path),
                            runner_fn=lambda *a, **k: {}, env={})
    ex = cp.command_executors(svc)
    out = ex["command_recipes"](query="what's using my CPU")
    assert out["count"] >= 1
    assert out["recipes"][0]["command"] == \
        "ps -eo pid,comm,%cpu,%mem --sort=-%cpu"
    assert "run_command" in out["note"]
    miss = ex["command_recipes"](query="qwertyuiop zxcvbnm")
    assert miss["count"] == 0 and "no recipe matched" in miss["note"]
    full = ex["command_recipes"]()
    assert full["count"] == len(DEFAULT_RECIPES)


def test_service_carries_default_catalogue(tmp_path):
    svc = cp.CommandService(cfg={}, home=str(tmp_path),
                            runner_fn=lambda *a, **k: {}, env={})
    assert svc.recipes == [dict(r) for r in DEFAULT_RECIPES]


# ── new allowlist entries the recipes lean on ──────────────────
def test_new_allowlist_additions():
    assert policy.decide("findmnt -D").mode == "auto"
    assert policy.decide("checkupdates").mode == "auto"
    assert policy.decide("systemctl --failed").mode == "auto"
    assert policy.decide("systemctl --user --failed").mode == "auto"
    assert policy.decide("systemctl --failed restart foo").mode == "ask"
    assert policy.decide("journalctl --user -p err -n 50").mode == "auto"
    assert policy.decide("ip -br addr").mode == "auto"
    assert policy.decide("ps -eo pid,comm,%cpu,%mem --sort=-%cpu") \
        .mode == "auto"
    assert policy.decide("du -sh ~/*").mode == "ask"   # glob -> shell mode
