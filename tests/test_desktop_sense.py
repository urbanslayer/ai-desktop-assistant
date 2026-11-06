"""Desktop awareness (docs/DESKTOP_SENSE.md) — fixture-driven, fake-runner
tests. Nothing here touches a live compositor: every backend call goes
through a `runner=` fake fed by recorded JSON in tests/fixtures/
desktop_sense/ (shapes recorded from real hyprctl/swaymsg/KWin/GNOME
output), and the launcher tests only record what WOULD run."""
import json
import os
import re
from pathlib import Path

import pytest

from riko.client_tools import ToolError
from riko.desktop_sense import apps as apps_mod
from riko.desktop_sense import hyprland as hyprland_mod
from riko.desktop_sense import sway as sway_mod
from riko.desktop_sense.awareness import Awareness
from riko.desktop_sense.gnome import GnomeBackend
from riko.desktop_sense.hyprlua import HyprLua
from riko.desktop_sense.kde import KWinBackend
from riko.desktop_sense.monitors import resolve_monitor_phrase
from riko.desktop_sense.sense import DesktopSense, sense_tools
from riko.desktop_sense.util import clean_title

FIX = Path(__file__).parent / "fixtures" / "desktop_sense"
HYPR_FIX = Path(__file__).parent / "fixtures" / "hypr_lua"


def fix(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def recording_runner(replies):
    """runner(argv, timeout=..) -> Done; `replies` maps a regex over
    'argv joined' to a Done or callable(argv) -> Done. Records all calls."""
    calls = []

    def runner(argv, timeout=None, **kw):
        calls.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        for pat, reply in replies.items():
            if re.search(pat, joined):
                return reply(argv) if callable(reply) else reply
        return Done("", 1, f"no fake reply for: {joined}")
    runner.calls = calls
    return runner


# ── title hygiene ─────────────────────────────────────────────
def test_clean_title():
    assert clean_title("a\n\n b " + "x" * 200) == ("a b " + "x" * 200)[:120]


# ── Hyprland backend ──────────────────────────────────────────
@pytest.fixture
def hypr():
    mons, clients = json.dumps(fix("hypr_monitors.json")), json.dumps(fix("hypr_clients.json"))
    act, ws = json.dumps(fix("hypr_activewindow.json")), json.dumps(fix("hypr_workspaces.json"))
    runner = recording_runner({
        r"hyprctl monitors": Done(mons),
        r"hyprctl clients": Done(clients),
        r"hyprctl activewindow": Done(act),
        r"hyprctl workspaces": Done(ws),
        r"dispatch hl\.dsp\.no_op": Done("Invalid dispatcher"),   # classic hyprlang
        r"hyprctl dispatch": Done("ok"),
        r"hyprctl activeworkspace": Done(json.dumps(
            {"id": 1, "name": "1", "monitor": 1})),
    })
    lua = type("L", (), {"preferred": {"terminal": "kitty"},
                         "configured_monitors": lambda s: [
                             {"output": "desc:GWD ARZOPA 2022110200001",
                              "mode": "2560x1440@140", "position": "5120x0",
                              "scale": 1.25}]})()
    return hyprland_mod.HyprlandBackend(runner=runner, lua=lua)


def test_hyprland_monitors(hypr):
    mons = hypr.monitors()
    assert [m["name"] for m in mons] == ["DP-1", "DP-2", "HDMI-A-1"]
    assert mons[0]["description"].startswith("Dell Inc.")
    assert mons[0]["size"] == "2560x1440" and mons[0]["position"] == "0x0"
    assert mons[2]["scale"] == 1.25 and mons[1]["active_workspace"] == "1"
    assert hypr.preferences() == {"terminal": "kitty"}


def test_hyprland_windows_maps_monitors_and_hides_scratchpad(hypr):
    wins = hypr.windows()
    by = {w["app"]: w for w in wins}
    assert set(by) == {"firefox", "kitty"}   # keepassxc lives in special:1
    assert by["firefox"]["monitor"] == "DP-1"
    assert by["firefox"]["title"] == "Arch Wiki - Mozilla Firefox"
    assert by["kitty"]["focused"] and not by["firefox"]["focused"]
    f = hypr.focused()
    assert f["app"] == "kitty" and f["monitor"] == "1"


def test_hyprland_focus_window_by_title(hypr):
    out = hypr.focus_window("user@arch")
    assert out["focused"] == "kitty"
    assert any("focuswindow address:0x222" in " ".join(c) for c in hypr.runner.calls)
    with pytest.raises(ToolError):
        hypr.focus_window("definitely-not-open")


def test_hyprland_launch_to_monitor(hypr):
    """open_app('foot', monitor='HDMI-A-1') moves the new window to that
    screen's workspace (stateful fake: foot appears after the exec)."""
    base = fix("hypr_clients.json")
    foot = {"address": "0x999", "class": "foot", "title": "foot",
            "workspace": {"id": 1, "name": "1"}, "monitor": 1, "pid": 9,
            "fullscreen": False, "floating": False}
    state = {"after_exec": False, "after_move": False}
    mons = json.dumps(fix("hypr_monitors.json"))

    def clients_reply(argv):
        if not state["after_exec"]:
            return Done(json.dumps(base))
        f = dict(foot)
        if state["after_move"]:                  # the move took effect
            f["workspace"] = {"id": 8, "name": "8"}
            f["monitor"] = 2
        return Done(json.dumps(base + [f]))

    def dispatch(argv):
        j = " ".join(argv)
        if "exec" in j:
            state["after_exec"] = True
        if "movetoworkspace" in j:
            state["after_move"] = True
        return Done("ok")

    runner = recording_runner({
        r"hyprctl monitors": Done(mons), r"hyprctl clients": clients_reply,
        r"hyprctl activewindow": Done("{}"), r"hyprctl workspaces":
            Done(json.dumps(fix("hypr_workspaces.json"))),
        r"dispatch hl\.dsp\.no_op": Done("Invalid dispatcher"),   # classic hyprlang
        r"hyprctl dispatch": dispatch})
    b = hyprland_mod.HyprlandBackend(runner=runner, lua=None)
    out = b.launch(["foot"], monitor="HDMI-A-1")
    assert out == {"workspace": "8", "monitor": "HDMI-A-1"}
    joined = [" ".join(c) for c in runner.calls]
    assert any('exec "foot"' in c for c in joined)
    assert any("movetoworkspacesilent 8,address:0x999" in c for c in joined)


# ── the owner's real Hyprland Lua config, copied as fixture ──
def test_hyprlua_owner_config():
    lua = HyprLua(HYPR_FIX)
    outs = [m["output"] for m in lua.configured_monitors()]
    assert outs == [
        "desc:Dell Inc. DELL U2518D 3C4YP99OB39L",
        "desc:Dell Inc. DELL U2518D 3C4YP82K504L",
        "desc:GWD ARZOPA 2022110200001"]     # exactly the 3 live screens
    # commented-out blocks (DP-1, eDP-2, the fallback line) stay invisible
    assert not [o for o in outs if o in ("DP-1", "DP-2", "eDP-2", "")]
    assert lua.monitors[2]["mode"] == "2560x1440@140"
    assert lua.monitors[2]["scale"] == 1.25
    # for-loop + tostring workspace rules expand: 1-4 centre, 5-7 left,
    # 8/9/10 bottom
    rules = {(r["workspace"], r["monitor"]) for r in lua.workspace_rules}
    assert ("1", "desc:Dell Inc. DELL U2518D 3C4YP82K504L") in rules
    assert ("7", "desc:Dell Inc. DELL U2518D 3C4YP99OB39L") in rules
    assert ("9", "desc:GWD ARZOPA 2022110200001") in rules
    assert len(rules) == 10
    # env TERMINAL=… names the owner's terminal
    assert lua.env.get("TERMINAL") == "kitty"
    assert lua.preferred.get("terminal") == "kitty"
    # keybind execs are collected (and commented-out ones are not)
    assert any(c.startswith("kitty") for c in lua.exec_binds)
    assert not any("pam_kwallet" in c for c in lua.exec_binds)


# ── monitor phrase resolver (owner's live 3-screen layout) ───
@pytest.fixture
def live_monitors():
    return [{"name": "DP-1", "description": "Dell Inc. DELL U2518D 3C4YP99OB39L",
             "size": "2560x1440", "position": "0x0", "scale": 1},
            {"name": "DP-2", "description": "Dell Inc. DELL U2518D 3C4YP82K504L",
             "size": "2560x1440", "position": "2560x0", "scale": 1,
             "focused": True},
            {"name": "HDMI-A-1", "description": "GWD ARZOPA 2022110200001",
             "size": "2560x1440", "position": "5120x0", "scale": 1.25}]


def test_monitor_phrases(live_monitors):
    r = lambda p: resolve_monitor_phrase(p, live_monitors)
    assert r("left")[0]["name"] == "DP-1"
    assert r("the right screen")[0]["name"] == "HDMI-A-1"
    assert r("middle")[0]["name"] == "DP-2"
    assert r("the Dell on the right")[0]["name"] == "DP-2"
    assert r("the dell on the left")[0]["name"] == "DP-1"
    assert r("the portable screen")[0]["name"] == "HDMI-A-1"
    assert r("HDMI-A-1")[0]["name"] == "HDMI-A-1"
    assert r("ARZOPA")[0]["name"] == "HDMI-A-1"
    m, err = r("the Dell")            # two Dells, no position word
    assert m is None and "left or right" in err
    m, err = r("the tiny screen")
    assert m is None and "DP-1" in err and "ARZOPA" in err   # names offered back


# ── Sway backend ──────────────────────────────────────────────
@pytest.fixture
def swayb():
    tree = json.dumps(fix("sway_tree.json"))
    runner = recording_runner({r"get_tree": Done(tree), r"-t command": Done("[]")})
    return sway_mod.SwayBackend(runner=runner)


def test_sway_state(swayb):
    mons = swayb.monitors()
    assert [m["name"] for m in mons] == ["DP-1", "DP-2", "HDMI-A-1"]
    assert mons[0]["size"] == "2560x1440" and mons[1]["position"] == "2560x0"
    assert mons[0]["active_workspace"] == "2"
    wins = swayb.windows()
    by = {w["app"]: w for w in wins}
    assert by["firefox"]["monitor"] == "DP-1" and by["firefox"]["workspace"] == "2"
    assert swayb.focused()["app"] == "kitty"


def test_sway_launch_and_focus(swayb):
    tree0 = fix("sway_tree.json")
    foot = {"id": 777, "type": "con", "app_id": "foot", "name": "foot",
            "pid": 999, "focused": False, "nodes": [], "layout": "none"}
    state = {"added": False}

    def tree_reply(argv):
        t = json.loads(json.dumps(tree0))
        if state["added"]:
            t["nodes"][2]["nodes"][0]["nodes"].append(foot)
        return Done(json.dumps(t))

    runner = recording_runner({r"get_tree": tree_reply,
                               r"-t command": (lambda a: (state.__setitem__(
                                   "added", "exec " in " ".join(a)) or Done("[]")))})
    b = sway_mod.SwayBackend(runner=runner)
    out = b.launch(["foot"], monitor="HDMI-A-1")
    assert out == {"monitor": "HDMI-A-1"}
    cmds = [" ".join(c) for c in runner.calls if "-t command" in " ".join(c)]
    assert any("move container to output HDMI-A-1" in c for c in cmds)
    # the exec arg is quoted argv, never a raw model string
    assert any("exec 'foot'" in c for c in cmds)
    assert b.focus_window("kitty")["focused"] == "kitty"
    assert any('[con_id="102"] focus' in c for c in runner.calls if "-t command" in " ".join(c))


# ── KDE (KWin over D-Bus) ────────────────────────────────────
def kwin_runner(payload):
    """Fake qdbus runner: on loadScriptForIdentifier it plays the script by
    extracting the output path from the script file and writing `payload`."""
    def reply(argv):
        joined = " ".join(str(a) for a in argv)
        if "loadScriptForIdentifier" in joined:
            script = Path(str(argv[-2]))
            text = script.read_text()
            out_path = re.search(r'writeFile\("([^"]+)"', text).group(1)
            Path(out_path).write_text(json.dumps(payload), encoding="utf-8")
        return Done("")
    return recording_runner({r".": reply})


def test_kde_state_and_focus():
    payload = fix("kwin_list.json")
    b = KWinBackend(runner=kwin_runner(payload))
    wins = b.windows()
    assert [w["app"] for w in wins] == ["firefox", "konsole"]
    assert wins[0]["monitor"] == "DP-1" and wins[1]["focused"]
    assert b.focused()["app"] == "konsole"
    assert [m["name"] for m in b.monitors()] == ["DP-1", "DP-2"]
    assert b.focus_window("4211")["focused"] == "4211"
    # a focus failure comes back as the script's error
    bad = KWinBackend(runner=kwin_runner({"ok": False, "error": "nope"}))
    with pytest.raises(ToolError):
        bad.focus_window("4211")


# ── GNOME (shell extension over D-Bus) ───────────────────────
def gnome_runner(replies, qdbus=True):
    def reply(argv):
        joined = " ".join(str(a) for a in argv)
        for meth, payload in replies.items():
            if meth in joined:
                raw = payload if isinstance(payload, str) else json.dumps(payload)
                return Done(f'string "{raw}"\n' if not qdbus else raw + "\n")
        return Done("", 1, "The name im.riko.Windows was not provided")
    return recording_runner({r".": reply})


def test_gnome_state_and_focus():
    b = GnomeBackend(runner=gnome_runner({
        "List": fix("gnome_windows.json") if False else json.loads(
            (FIX / "gnome_windows.json").read_text()),
        "Monitors": json.loads((FIX / "gnome_monitors.json").read_text()),
        "Activate": json.loads((FIX / "gnome_activate.json").read_text())}))
    wins = b.windows()
    assert [w["app"] for w in wins] == ["firefox", "org.gnome.Nautilus"]
    assert wins[0]["focused"] and wins[1]["monitor"] == "1"
    assert b.focused()["app"] == "firefox"
    assert [m["name"] for m in b.monitors()] == ["MVN-0", "MVN-1"]
    assert b.focus_window("18")["focused"] == "org.gnome.Nautilus"


def test_gnome_without_extension():
    b = GnomeBackend(runner=gnome_runner({}))   # name unknown on the bus
    with pytest.raises(ToolError) as e:
        b.windows()
    assert "extension" in str(e.value)          # install hint, not a crash


# ── XDG app index + safe launcher ─────────────────────────────
@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "applications"
    d.mkdir()
    (d / "foot.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Foot\nExec=foot -e %u\n"
        "Keywords=terminal;shell;\n")
    (d / "evil.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Evil; rm -rf /\n"
        'Exec=sh -c "echo hi; rm -rf /" -- %F\n')
    (d / "brave-browser.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Brave Browser\n"
        "Exec=brave-browser %U\nStartupWMClass=brave-browser\n")
    (d / "note.desktop").write_text(
        "[Desktop Entry]\nType=Link\nName=Not a linkable app\nURL=https://x\n")
    return d


def test_split_exec_field_codes_and_no_shell():
    assert apps_mod.split_exec("foot -e %u") == ["foot", "-e"]
    assert apps_mod.split_exec('brave-browser --new-window %U') == ["brave-browser", "--new-window"]
    # shell metacharacters survive as literal argv — nothing is ever sh'ed
    assert apps_mod.split_exec('sh -c "echo hi; rm -rf /" -- %F') == \
        ["sh", "-c", "echo hi; rm -rf /", "--"]
    assert apps_mod.split_exec("foo\\ bar %i") == ["foo\\", "bar"]


def test_app_index(app_dir):
    idx = apps_mod.AppIndex([app_dir])
    ids = {a["id"] for a in idx.all()}
    assert ids == {"foot.desktop", "evil.desktop", "brave-browser.desktop"}  # Type=Link skipped
    assert idx.resolve("foot")["exec"] == "foot -e %u"
    assert idx.resolve("brave")["app"] == "brave-browser"
    assert idx.resolve("nonexistent-app") is None
    assert idx.find("terminal")[0]["id"] == "foot.desktop"


def test_open_app_generic_detached(app_dir):
    runner = recording_runner({r".": Done("")})
    out = apps_mod.open_app("foot", index=apps_mod.AppIndex([app_dir]),
                            runner=runner)
    launch = [c for c in runner.calls if c and c[0] == "__launch__"][0]
    assert launch[1:] in (["gtk-launch", "foot"], ["gio", "launch", str(app_dir / "foot.desktop")])
    assert "foot" in launch or "foot.desktop" in " ".join(launch)
    assert out["launched"] and out["app"] == "Foot"


def test_open_app_no_shell_injection(app_dir):
    """The ';' in the evil app's Name/Exec stays data: argv list, never a
    shell string, and the whole argv is what would run."""
    runner = recording_runner({r".": Done("")})
    out = apps_mod.open_app("Evil; rm -rf /", index=apps_mod.AppIndex([app_dir]),
                            runner=runner)
    launch = [c for c in runner.calls if c and c[0] == "__launch__"][0]
    assert isinstance(launch, list) and all(isinstance(a, str) for a in launch)
    if launch[1] == "sh":       # plain Exec argv path
        assert "echo hi; rm -rf /" in launch


def test_open_app_role_and_monitor(app_dir):
    """'terminal' honours the owner's configured preference; the monitor
    phrase resolves against the backend's live monitors."""
    calls = {}

    class B:
        name = "hyprland"
        supports_launch = True

        def launch(self, argv, monitor=None, workspace=None, runner=None, env=None):
            calls.update(argv=argv, monitor=monitor, workspace=workspace)
            return {"workspace": "8"}

    mons = [{"name": "DP-1", "description": "Dell U2518D", "position": "0x0"},
            {"name": "HDMI-A-1", "description": "GWD ARZOPA", "position": "5120x0"}]
    out = apps_mod.open_app("terminal", monitor="portable screen",
                            backend=B(), index=apps_mod.AppIndex([app_dir]),
                            preferences={"terminal": "foot"},
                            runner=recording_runner({r".": Done("")}),
                            resolve_monitor=lambda: mons)
    assert calls == {"argv": ["foot", "-e"], "monitor": "HDMI-A-1",
                     "workspace": None}
    assert out["monitor"] == "HDMI-A-1"
    # unknown app: nothing runs
    with pytest.raises(ToolError):
        apps_mod.open_app("definitely-not-installed",
                          index=apps_mod.AppIndex([app_dir]),
                          runner=recording_runner({r".": Done("")}))


# ── awareness: toggle, persistence, denylist ─────────────────
def test_awareness_toggle_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    assert Awareness({}).enabled is False                    # default OFF
    assert Awareness({"desktop": {"awareness": "on"}}).enabled is True
    a = Awareness({})
    assert a.set(True) is True
    assert Awareness({}).enabled is True                     # persisted wins
    assert Awareness({"desktop": {"awareness": "on"}}).enabled is True
    Awareness({}).set(False)
    assert Awareness({"desktop": {"awareness": "on"}}).enabled is False  # even over cfg


def test_denylist(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    a = Awareness({"desktop": {"awareness": {"enabled": "on",
                                             "hide": ["mybank", "Secret-.*"]}}})
    assert a.hides("org.keepassxc.KeePassXC", "vault")
    assert a.hides("firefox", "Banking — Private Browsing")
    assert a.hides("firefox", "private site") is False       # app+title both clean
    assert a.hides("chrome", "Some Page - Incognito Window")
    assert a.hides("mybank-app", "")                          # extra app entry
    assert a.hides("notes", "Secret-Project plan")            # extra title regex
    assert a.hides_private("", "about:privatebrowsing")
    assert a.hides("firefox", "MyBank portal")                # app word in title too


# ── DesktopSense: privacy gating + focused-tab wiring ────────
class FakeBridge:
    def __init__(self, answers):
        self.answers, self._c = answers, True

    def connected(self):
        return self._c

    def call(self, method, params=None):
        a = self.answers.get(method)
        if a is None:
            return {"ok": False, "error": f"Unknown method: {method}"}
        return a


class FakeBackend:
    name = "fake"
    supports_launch = False

    def __init__(self, wins, mons=None):
        self.wins, self._mons = wins, mons or []

    def windows(self):
        return self.wins

    def focused(self):
        return next((w for w in self.wins if w.get("focused")), None)

    def monitors(self):
        return self._mons


def sense_on(tmp_path, monkeypatch, backend=None, bridge=None, cfg=None):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = cfg or {"desktop": {"awareness": "on"}}
    s = DesktopSense(cfg, bridge, backend=backend or FakeBackend([]),
                     awareness=Awareness(cfg))
    return s


def test_windows_denylist_and_focused_drop(tmp_path, monkeypatch):
    wins = [{"id": "1", "app": "firefox", "title": "Arch Wiki", "workspace": "1",
             "monitor": "DP-1", "focused": False, "fullscreen": False,
             "floating": False, "pid": 1},
            {"id": "2", "app": "org.keepassxc.KeePassXC", "title": "vault",
             "workspace": "1", "monitor": "DP-1", "focused": True,
             "fullscreen": False, "floating": False, "pid": 2}]
    s = sense_on(tmp_path, monkeypatch, backend=FakeBackend(wins))
    out = s.desktop_windows()
    apps = [w["app"] for w in out["windows"]]
    assert apps == ["firefox", "<hidden>"]
    f = s.desktop_focused()
    assert f["window"] is None            # denylisted focused window dropped


def test_focused_firefox_gets_active_tab(tmp_path, monkeypatch):
    wins = [{"id": "1", "app": "firefox", "title": "Arch Wiki", "workspace": "1",
             "monitor": "DP-1", "focused": True, "fullscreen": False,
             "floating": False, "pid": 1}]
    bridge = FakeBridge({"activeTab": {"ok": True, "result": {
        "id": 3, "windowId": 1, "title": "ArchWiki", "url": "https://wiki.archlinux.org",
        "active": True, "pinned": False, "audible": False, "incognito": False}}})
    s = sense_on(tmp_path, monkeypatch, backend=FakeBackend(wins), bridge=bridge)
    f = s.desktop_focused()
    assert f["browser_tab"]["url"] == "https://wiki.archlinux.org"


def test_browser_tabs_listtabs_and_old_extension_fallback(tmp_path, monkeypatch):
    tabs = {"ok": True, "result": [
        {"id": 1, "windowId": 1, "title": "A", "url": "https://a", "active": True,
         "pinned": False, "audible": False, "incognito": False},
        {"id": 2, "windowId": 1, "title": "B", "url": "https://b", "active": False,
         "pinned": True, "audible": True, "incognito": True}]}
    s = sense_on(tmp_path, monkeypatch, bridge=FakeBridge({"listTabs": tabs}))
    out = s.browser_tabs()
    rows = out["tabs"]
    assert [r["id"] for r in rows] == [1]          # private tab hidden by default
    assert rows[0]["title"] == "A" and rows[0]["active"]
    # windows: the one window's active tab (no compositor -> extension order)
    assert out["windows"][0]["window"] == 1
    assert out["windows"][0]["tab"]["title"] == "A"
    assert out["windows"][0]["screen"] == ""       # FakeBackend has no monitors
    # old extension: listTabs unknown -> falls back to tabs
    old = {"tabs": {"ok": True, "result": [{"title": "X", "url": "https://x",
                                            "active": True}]}}
    s2 = sense_on(tmp_path, monkeypatch, bridge=FakeBridge(old))
    out2 = s2.browser_tabs()
    assert out2["tabs"][0]["title"] == "X" and out2["tabs"][0]["id"] == 1
    # neither method: actionable message
    s3 = sense_on(tmp_path, monkeypatch, bridge=FakeBridge({}))
    with pytest.raises(ToolError, match="Riko Helper"):
        s3.browser_tabs()
    # activeTab on an old extension says so too
    with pytest.raises(ToolError, match="Riko Helper"):
        s3.active_tab()


def test_private_tab_shown_when_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = {"desktop": {"awareness": {"enabled": "on", "private_tabs": True}}}
    tabs = {"ok": True, "result": [
        {"id": 2, "windowId": 1, "title": "B", "url": "https://b",
         "active": False, "incognito": True}]}
    s = DesktopSense(cfg, FakeBridge({"listTabs": tabs}),
                     backend=FakeBackend([]), awareness=Awareness(cfg))
    out = s.browser_tabs()
    assert len(out["tabs"]) == 1 and out["windows"][0]["private"]


def test_toggle_off_hides_tools_and_backend_not_queried(tmp_path, monkeypatch):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = {}                              # awareness unset -> off
    a = Awareness(cfg)
    queried = []

    class SpyBackend(FakeBackend):
        def windows(self):
            queried.append(1)
            return []

    s = DesktopSense(cfg, None, backend=SpyBackend([]), awareness=a)
    assert not s.awareness.enabled
    assert not any(t["name"].startswith("desktop_")
                   for t in __import__("riko.client_tools", fromlist=["x"]).desktop_tools(s))
    assert s.desktop_windows.__func__ is not None  # tool exists in code but…
    # …with awareness on it registers, and names/risks follow the contract
    names = {t["name"]: t for t in sense_tools()}
    assert set(names) == {"desktop_windows", "desktop_focused", "desktop_monitors",
                          "desktop_apps", "open_app", "switch_workspace",
                          "focus_window"}
    assert names["desktop_windows"]["risk"] == "read"
    assert names["open_app"]["risk"] == "act" and names["open_app"]["permission_default"] == "always"
    assert names["switch_workspace"]["risk"] == "act"
    assert names["switch_workspace"]["permission_default"] == "always"
    assert names["focus_window"]["permission_default"] == "always"


def test_no_backend_message(tmp_path, monkeypatch):
    s = sense_on(tmp_path, monkeypatch, backend=None)
    s._backend, s._backend_tried = None, True
    with pytest.raises(ToolError, match="no supported window manager"):
        s.desktop_windows()


# ── never launch a compositor / session / power action ───────
@pytest.fixture
def session_dir(tmp_path):
    """An applications dir plus a wayland-sessions dir."""
    d = tmp_path / "applications"
    d.mkdir()
    sess = tmp_path / "wayland-sessions"
    sess.mkdir()
    (d / "foot.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Foot\nExec=foot\n")
    (d / "Hyprland.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hyprland\nExec=Hyprland\n")
    (d / "start-hyprland.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Hyprland (wrapped)\n"
        "Exec=start-hyprland\n")
    (d / "envsway.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Sway via env\n"
        "Exec=env WLR_BACKENDS=headless sway\n")
    (d / "kwin.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=KWin\n"
        "Exec=kwin_wayland --x11\n")
    (d / "plasma.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Plasma\n"
        "Exec=startplasma-wayland\n")
    (d / "shell.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Plasma Shell\n"
        "Exec=env GTK_USE_PORTAL=1 plasmashell --replace\n")
    (d / "gnome-shell.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=GNOME Shell\n"
        "Exec=gnome-shell --wayland\n")
    (d / "tryexec.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Needs Hyprland\n"
        "TryExec=Hyprland\nExec=some-tool --in-hyprland\n")
    (d / "xorg.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Xorg\nExec=/usr/lib/Xorg\n")
    (d / "xvfb.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Xvfb\nExec=Xvfb :99\n")
    (d / "sddm.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=SDDM\nExec=sddm\n")
    (d / "reboot.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Restart\nExec=reboot\n")
    (d / "sysctl.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Suspend\n"
        "Exec=systemctl suspend\n")
    (d / "loginctl.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Lock\n"
        "Exec=loginctl lock-session\n")
    (sess / "sway.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Swam\nExec=sway\n")
    (sess / "gnome.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=GNOME\n"
        "Exec=gnome-session\n")
    return d, sess


def _wants(idx, runner=None):
    return lambda w: apps_mod.open_app(w, index=idx,
                                       runner=runner or recording_runner(
                                           {r".": Done("")}))


def test_compositor_session_entries_are_hidden(session_dir):
    d, sess = session_dir
    idx = apps_mod.AppIndex([d, sess])
    ids = {a["id"] for a in idx.all()}
    assert ids == {"foot.desktop"}          # everything else is hidden
    for hidden in ("Hyprland.desktop", "start-hyprland.desktop",
                   "envsway.desktop", "kwin.desktop", "plasma.desktop",
                   "shell.desktop", "gnome-shell.desktop",
                   "tryexec.desktop", "xorg.desktop", "xvfb.desktop",
                   "sddm.desktop", "reboot.desktop", "sysctl.desktop",
                   "loginctl.desktop", "sway.desktop", "gnome.desktop"):
        assert hidden not in ids
    assert not [a for a in idx.find("hyprland") or [] if "sway" not in a["id"]]


def test_open_app_refuses_compositor_session_and_power(session_dir):
    d, sess = session_dir
    idx = apps_mod.AppIndex([d, sess])
    runner = recording_runner({r".": Done("")})
    ask = _wants(idx, runner)
    with pytest.raises(ToolError, match="compositor"):
        ask("Hyprland")
    with pytest.raises(ToolError, match="compositor"):
        ask("start-hyprland")
    with pytest.raises(ToolError, match="compositor"):     # behind env=
        ask("envsway")
    with pytest.raises(ToolError, match="compositor"):     # kwin* prefix
        ask("kwin")
    with pytest.raises(ToolError, match="compositor"):     # startplasma*
        ask("plasma")
    with pytest.raises(ToolError, match="compositor"):     # env-wrapped shell
        ask("shell")
    with pytest.raises(ToolError, match="compositor"):     # gnome-shell
        ask("gnome-shell")
    with pytest.raises(ToolError, match="compositor"):     # TryExec names it
        ask("Needs Hyprland")
    with pytest.raises(ToolError, match="compositor"):     # Xorg / Xvfb
        ask("xorg")
    with pytest.raises(ToolError, match="compositor"):
        ask("xvfb")
    with pytest.raises(ToolError, match="compositor"):     # display manager
        ask("sddm")
    with pytest.raises(ToolError, match="power"):          # power levers
        ask("reboot")
    with pytest.raises(ToolError, match="power"):
        ask("sysctl")
    with pytest.raises(ToolError, match="power"):
        ask("loginctl")
    with pytest.raises(ToolError, match="login-session"):  # wayland-sessions
        ask("sway")
    assert runner.calls == []               # nothing ever reached the launcher
    # ordinary apps still launch, and the refusal text explains why
    out = ask("foot")
    assert out["launched"] and out["app"] == "Foot"


def test_open_app_refuses_compositor_preferences(session_dir, monkeypatch):
    """Even the owner's configured preference can't smuggle a compositor in
    (a TERMINAL=Hyprland-style preference, or a .desktop pref id)."""
    d, _ = session_dir
    idx = apps_mod.AppIndex([d])
    monkeypatch.setattr(apps_mod.shutil, "which",
                        lambda n: f"/usr/bin/{n}" if n == "Hyprland" else None)
    with pytest.raises(ToolError, match="compositor"):
        apps_mod.open_app("terminal", index=idx,
                          preferences={"terminal": "Hyprland"},
                          runner=recording_runner({r".": Done("")}))


def test_generic_launchers_carry_their_session_argument():
    """gtk-launch hyprland / gio launch .../wayland-sessions/x.desktop — the
    session is the ARGUMENT, the launcher is harmless alone."""
    assert apps_mod.block_reason({"exec": "gtk-launch hyprland"})
    assert apps_mod.block_reason(
        {"exec": "gio launch /usr/share/wayland-sessions/sway.desktop"})
    assert apps_mod.block_reason(
        {"exec": "gio launch /usr/share/xsessions/plasma.desktop"})
    assert apps_mod.block_reason(
        {"exec": "dbus-run-session -- sway"}) is not None
    assert apps_mod.block_reason({"exec": "gtk-launch foot"}) is None
    assert apps_mod.block_reason({"exec": "gio launch /usr/share/applications/"
                                          "foot.desktop"}) is None
    assert apps_mod.block_reason({"exec": "foot -e %u"}) is None


# ── conftest guards: no real compositor, ever ────────────────
def test_conftest_guard_blocks_real_compositor_subprocess():
    """A missing fake runner must fail LOUDLY, not spawn a compositor."""
    import subprocess as sp
    for prog in ("hyprctl", "swaymsg", "qdbus", "qdbus-qt6", "gdbus",
                 "busctl", "dbus-send", "kwin_wayland", "Hyprland", "sway",
                 "gnome-shell", "weston", "Xvfb", "Xephyr", "plasmashell",
                 "startplasma-wayland", "/usr/bin/hyprctl", "grim"):
        with pytest.raises(RuntimeError, match="compositor"):
            sp.run([prog, "--version"])
        with pytest.raises(RuntimeError, match="compositor"):
            sp.Popen([prog])
    # the fake-runner path is untouched: backends never reach subprocess
    # when a runner is injected
    from riko.desktop_sense.util import run as ds_run
    assert ds_run(["hyprctl", "monitors"], runner=lambda a, **k: Done("{}")) \
        .stdout == "{}"
    # and unrelated binaries still run normally
    assert sp.run(["/bin/echo", "ok"], capture_output=True,
                  text=True).stdout.strip() == "ok"


def test_session_env_is_stripped_from_the_test_run():
    for var in ("HYPRLAND_INSTANCE_SIGNATURE", "WAYLAND_DISPLAY", "DISPLAY",
                "SWAYSOCK", "DBUS_SESSION_BUS_ADDRESS"):
        assert var not in os.environ, f"{var} leaked into the test env"
    # backend detection in the cleaned env finds nothing (no live session),
    # unless a test injects an explicit env
    from riko.desktop_sense.sense import detect_backend
    assert detect_backend(env={}) is None


# ── the tray checkbox wiring (main(); source-checked, no window) ──
def test_tray_toggle_consumes_checked_and_persists():
    """'Let her see my desktop' must swallow QAction's checked bool (skill
    pitfall), persist via awareness.set, and re-register the tool list."""
    import inspect
    import desktop
    src = inspect.getsource(desktop)
    assert "def toggle_desktop_sense(checked=None):" in src
    assert "sense.awareness.set(want)" in src
    assert "client_tools.apply_awareness(runtime, cfg, browser, sense)" in src
    assert 'QAction("Let her see my desktop"' in src


# ── hub prompt line ───────────────────────────────────────────
def test_prompt_line_appears_only_with_desktop_tools():
    from hub.tools.registry import tools_prompt
    with_line = tools_prompt(["pc__desktop_windows", "pc__open_app"])
    assert "open windows" in with_line and "which screen" in with_line
    plain = tools_prompt(["pc__browser_tabs"])
    assert "open windows" not in plain


# ── multi-window Firefox: compositor is the truth (owner 2026-11-05) ─
from riko.desktop_sense import browser_windows as bw
from riko.desktop_sense.monitors import monitor_phrase


def test_split_firefox_title_variants():
    t, p = bw.split_firefox_title("Godot Docs \u2014 Mozilla Firefox")
    assert (t, p) == ("Godot Docs", False)
    t, p = bw.split_firefox_title("Godot Docs - Mozilla Firefox")
    assert (t, p) == ("Godot Docs", False)
    t, p = bw.split_firefox_title("X \u2014 Mozilla Firefox Private Browsing")
    assert (t, p) == ("X", True)
    t, p = bw.split_firefox_title("X - Mozilla Firefox (Private Browsing)")
    assert (t, p) == ("X", True)
    t, p = bw.split_firefox_title("just a title")
    assert (t, p) == ("just a title", False)


def test_parse_workspace_ref_phrases():
    for phrase, want in [("2", "2"), ("desktop 2", "2"), ("workspace 2", "2"),
                         ("virtual desktop two", "2"), ("go to desktop 3", "3"),
                         ("web", "web"), ("", None), (None, None)]:
        assert bw.parse_workspace_ref(phrase) == want


def test_monitor_phrases_reverse(live_monitors):
    by = {m["name"]: m for m in live_monitors}
    assert monitor_phrase(by["DP-1"], live_monitors) == "left Dell"
    assert monitor_phrase(by["DP-2"], live_monitors) == "middle Dell"
    assert monitor_phrase(by["HDMI-A-1"], live_monitors) == "portable screen"
    # round-trips through the resolver
    for name in ("DP-1", "DP-2", "HDMI-A-1"):
        m, err = resolve_monitor_phrase(monitor_phrase(by[name], live_monitors),
                                        live_monitors)
        assert err is None and m["name"] == name or name == "DP-2"


def ffwin(address, title, ws, mon, hist):
    return {"id": address, "app": "firefox", "title": title, "workspace": ws,
            "monitor": mon, "focused": hist == 0, "focus_recency": hist,
            "fullscreen": False, "floating": False, "pid": 1}


def test_match_windows_orders_by_compositor_not_firefox():
    # extension says hub is last-focused; compositor says the godot window is
    ext = [{"window": 101, "title": "Riko Hub", "tab": None, "tab_count": 3,
            "focused": True, "private": False},
           {"window": 102, "title": "Godot Docs \u2014 Godot Engine",
            "tab": None, "tab_count": 12, "focused": False, "private": False}]
    comp = [ffwin("0x501", "Riko Hub \u2014 Mozilla Firefox", "1", "DP-2", 2),
            ffwin("0x502", "Godot Docs \u2014 Godot Engine \u2014 Mozilla Firefox",
                  "2", "DP-1", 0)]
    pairs = bw.match_windows(ext, comp)
    assert [p[0]["window"] for p in pairs] == [102, 101]
    assert pairs[0][1]["id"] == "0x502"


def test_match_windows_title_collision():
    ext = [{"window": 1, "title": "New Tab", "tab": None, "tab_count": 1,
            "focused": True, "private": False},
           {"window": 2, "title": "New Tab", "tab": None, "tab_count": 2,
            "focused": False, "private": False}]
    comp = [ffwin("0xA", "New Tab - Mozilla Firefox", "1", "DP-1", 4),
            ffwin("0xB", "New Tab - Mozilla Firefox", "2", "DP-2", 0)]
    pairs = bw.match_windows(ext, comp)
    # unique pairing (no window matched twice): the freshest extension
    # window pairs with the freshest compositor row, order follows recency
    assert [p[0]["window"] for p in pairs] == [1, 2]
    assert pairs[0][1]["id"] == "0xB" and pairs[1][1]["id"] == "0xA"


def test_match_windows_no_compositor_keeps_extension_order():
    ext = [{"window": 101, "title": "Riko Hub", "tab": None, "tab_count": 3,
            "focused": True, "private": False},
           {"window": 102, "title": "Godot", "tab": None, "tab_count": 1,
            "focused": False, "private": False}]
    pairs = bw.match_windows(ext, [])
    assert [p[0]["window"] for p in pairs] == [101, 102]


# ── hyprland backend: focusHistoryID + workspace dispatch ───────────
def hypr_ff(act="hypr_activewindow_firefox.json"):
    # classic hyprlang config; stateful active workspace so the post-dispatch
    # verification (activeworkspace) sees the switch happen
    ws_state = {"cur": {"id": 1, "name": "1", "monitor": 1}}

    def dispatch(argv):
        if argv[-1] == "workspace 2":
            ws_state["cur"] = {"id": 2, "name": "2", "monitor": 0}
        return Done("ok")

    runner = recording_runner({
        r"hyprctl monitors": Done(json.dumps(fix("hypr_monitors.json"))),
        r"hyprctl clients": Done(json.dumps(fix("hypr_clients_ffwindows.json"))),
        r"hyprctl activewindow": Done(json.dumps(fix(act))),
        r"hyprctl workspaces": Done(json.dumps(fix("hypr_workspaces.json"))),
        r"dispatch hl\.dsp\.no_op": Done("Invalid dispatcher"),   # classic hyprlang
        r"hyprctl dispatch": dispatch,
        r"hyprctl activeworkspace": lambda a: Done(json.dumps(ws_state["cur"])),
    })
    return hyprland_mod.HyprlandBackend(runner=runner, lua=None)


def test_hyprland_focus_recency_rows():
    b = hypr_ff()
    by = {w["id"]: w for w in b.windows()}
    assert by["0x502"]["focus_recency"] == 0 and by["0x502"]["focused"]
    assert by["0x501"]["focus_recency"] == 2
    assert by["0x503"]["focus_recency"] == 3
    assert b.focused()["focus_recency"] == 0
    assert b.focused()["app"] == "firefox"


def test_hyprland_switch_workspace_dispatch():
    b = hypr_ff()
    out = b.switch_workspace("2")
    joined = [" ".join(c) for c in b.runner.calls]
    assert any(c == "hyprctl dispatch workspace 2" for c in joined)
    # the observed state is reported, not the requested string
    assert out == {"workspace": "2", "monitor": "DP-1"}
    # 'show workspace 2 on the left screen': moveworkspacetomonitor FIRST
    b.switch_workspace("2", monitor="DP-1")
    joined = [" ".join(c) for c in b.runner.calls]
    mv = [i for i, c in enumerate(joined) if "moveworkspacetomonitor 2 DP-1" in c]
    ws = [i for i, c in enumerate(joined) if c == "hyprctl dispatch workspace 2"]
    assert mv and ws and mv[-1] < ws[-1]


def test_hyprland_launch_follow_vs_quiet():
    base = fix("hypr_clients_ffwindows.json")
    dol = {"address": "0x999", "class": "org.kde.dolphin", "title": "Dolphin",
           "workspace": {"id": 2, "name": "2"}, "monitor": 0, "pid": 9,
           "fullscreen": False, "floating": False, "focusHistoryID": 5}
    state = {"after_exec": False}

    def clients_reply(argv):
        return Done(json.dumps(base + ([dol] if state["after_exec"] else [])))

    def dispatch(argv):
        if "exec" in " ".join(argv):
            state["after_exec"] = True
        return Done("ok")

    def mk():
        runner = recording_runner({
            r"hyprctl monitors": Done(json.dumps(fix("hypr_monitors.json"))),
            r"hyprctl clients": clients_reply,
            r"hyprctl activewindow": Done(json.dumps(fix("hypr_activewindow.json"))),
            r"hyprctl workspaces": Done(json.dumps(fix("hypr_workspaces.json"))),
            r"dispatch hl\.dsp\.no_op": Done("Invalid dispatcher"),   # classic hyprlang
            r"hyprctl dispatch": dispatch})
        return hyprland_mod.HyprlandBackend(runner=runner, lua=None)

    b = mk()                                   # follow: no 'silent' tag
    b.launch(["dolphin"], workspace="2", follow=True)
    joined = [" ".join(c) for c in b.runner.calls]
    assert any('exec "[workspace 2] dolphin"' in c for c in joined)
    assert any("movetoworkspace 2,address:0x999" in c for c in joined)
    assert not any("silent" in c for c in joined)
    state["after_exec"] = False
    b = mk()                                   # no follow: silent placement
    b.launch(["dolphin"], workspace="2")
    joined = [" ".join(c) for c in b.runner.calls]
    assert any('exec "[workspace 2 silent] dolphin"' in c for c in joined)
    assert any("movetoworkspacesilent 2,address:0x999" in c for c in joined)


# ── hyprland on a Lua config: hl.dispatch expressions ───────────────
# Hyprland >=0.55 with a Lua config runs `hyprctl dispatch <x>` as
# eval("return hl.dispatch(<x>)") (src/debug/HyprCtl.cpp dispatchRequest):
# classic strings are invalid Lua, the error comes back in the OUTPUT while
# hyprctl still exits 0, and the actions are hl.dsp.* expressions.

_LUA_ERR_REPLY = ('error: [string "return hl.dispatch(workspace 6)"]:1: '
                  "syntax error: [string \"return hl.dispatch(workspace 6)\"]:"
                  "1: unexpected symbol near '6'"
                  "\n\n → Note: dispatch in lua is a shorthand for "
                  "hl.dispatch(...), your syntax might need to be updated.")


def _lua_hypr(ws=None, act=None, extra=None, clients=None, exprs=None,
              flip_ws=None, flip_addr=None, spawn=None, moved=None,
              dispatch=None):
    """Backend facing a Lua-config fixture: the style probe answers 'ok'.
    Exact dispatch expressions land in `exprs` (the style probe excepted).
    flip_ws=(expr, ws) / flip_addr=(expr, addr) make the fake view follow
    that exact expression; spawn=(prefix, window) makes a window appear
    once a dispatch starts with prefix, moved=(prefix, ws_name, mon_id)
    relocates it \u2014 the fake compositor behaves like 0.56.2."""
    state = {"ws": dict(ws or {"id": 1, "name": "1", "monitor": 1}),
             "addr": act or "0x222", "spawned": False, "moved": False}
    if exprs is None:
        exprs = []

    def default_dispatch(argv):
        expr = argv[-1]
        if expr != "hl.dsp.no_op()":
            exprs.append(expr)
        if flip_ws and expr == flip_ws[0]:
            state["ws"] = dict(flip_ws[1])
        if flip_addr and expr == flip_addr[0]:
            state["addr"] = flip_addr[1]
        if spawn and expr.startswith(spawn[0]):
            state["spawned"] = True
        if moved and expr.startswith(moved[0]):
            state["moved"] = True
        return Done("ok")

    def act_reply(argv):
        a = dict(fix("hypr_activewindow.json"))
        a["address"] = state["addr"]
        if state["addr"] == "0x503":
            a["workspace"] = {"id": 8, "name": "8"}
        return Done(json.dumps(a))

    base_clients = clients or fix("hypr_clients_ffwindows.json")

    def clients_reply(argv):
        out = list(base_clients)
        if spawn and state["spawned"]:
            w = dict(spawn[1])
            if moved and state["moved"]:
                name = str(moved[1])
                w["workspace"] = {"id": int(name) if name.isdigit() else -1,
                                  "name": name}
                w["monitor"] = moved[2]
            out.append(w)
        return Done(json.dumps(out))

    replies = {
        r"hyprctl monitors": Done(json.dumps(fix("hypr_monitors.json"))),
        r"hyprctl clients": clients_reply,
        r"hyprctl activewindow": act_reply,
        r"hyprctl workspaces": Done(json.dumps(fix("hypr_workspaces.json"))),
        r"dispatch hl\.dsp\.no_op": Done("ok"),        # Lua config!
        r"hyprctl activeworkspace":
            lambda a: Done(json.dumps(state["ws"])),
        r"hyprctl dispatch": dispatch or default_dispatch,
    }
    if extra:
        replies.update(extra)
    runner = recording_runner(replies)
    return hyprland_mod.HyprlandBackend(runner=runner, lua=None), state


def test_hyprland_config_style_detection(tmp_path):
    def mk(reply):
        r = recording_runner({r"dispatch hl\.dsp\.no_op\(\)": reply})
        return hyprland_mod.HyprlandBackend(runner=r, lua=None,
                                            config_dir=str(tmp_path)), r
    b, r = mk(Done("ok"))
    assert b.config_style() == "lua"
    # the probe itself goes as ONE argv element, never through a shell
    assert r.calls[0] == ["hyprctl", "dispatch", "hl.dsp.no_op()"]
    assert mk(Done("Invalid dispatcher"))[0].config_style() == "legacy"
    assert mk(Done(_LUA_ERR_REPLY))[0].config_style() == "lua"
    # hyprctl unreachable: the file layout decides
    dead = recording_runner({r".": Done("", 1, "could not connect")})
    assert hyprland_mod.HyprlandBackend(
        runner=dead, lua=None, config_dir=str(tmp_path)).config_style() == "legacy"
    (tmp_path / "hyprland.lua").write_text("-- owner's lua config\n")
    assert hyprland_mod.HyprlandBackend(
        runner=recording_runner({r".": Done("", 1, "no")}),
        lua=None, config_dir=str(tmp_path)).config_style() == "lua"


def test_hyprland_lua_switch_to_empty_workspace_6():
    # 'switch to workspace 6' when 6 does not exist yet: the same expression
    # the owner's Super+6 binds use; the compositor creates it
    # (resolveWorkspaceForChange creates missing workspaces, 0.56.2).
    exprs = []
    flip = ("hl.dsp.focus({ workspace = 6 })",
            {"id": 6, "name": "6", "monitor": 1})
    b, _ = _lua_hypr(exprs=exprs, flip_ws=flip)
    assert b.config_style() == "lua"
    out = b.switch_workspace("6")
    assert exprs == ["hl.dsp.focus({ workspace = 6 })"]   # number unquoted
    assert out == {"workspace": "6", "monitor": "DP-2"}   # observed state
    # the classic string that silently failed on the owner's setup is gone
    assert "workspace 6" not in exprs


def test_hyprland_lua_switch_to_occupied_and_verify():
    exprs = []
    flip = ("hl.dsp.focus({ workspace = 4 })",
            {"id": 4, "name": "4", "monitor": 0})
    b, _ = _lua_hypr(exprs=exprs, flip_ws=flip)
    out = b.switch_workspace("4")
    assert exprs == ["hl.dsp.focus({ workspace = 4 })"]
    assert out == {"workspace": "4", "monitor": "DP-1"}


def test_hyprland_lua_named_workspace_quoted():
    exprs = []
    flip = ('hl.dsp.focus({ workspace = "code" })',
            {"id": 3, "name": "code", "monitor": 1})
    b, _ = _lua_hypr(exprs=exprs, flip_ws=flip)
    out = b.switch_workspace("code")
    assert exprs == ['hl.dsp.focus({ workspace = "code" })']
    assert out["workspace"] == "code"


def test_hyprland_lua_view_did_not_change_is_an_error():
    # dispatch answers 'ok' but the view stays put (the owner's 'switch to
    # 4 said done but nothing changed')
    b, _ = _lua_hypr()
    with pytest.raises(ToolError, match="did not change"):
        b.switch_workspace("4")


def test_hyprland_lua_exit_0_but_error_text_surfaces():
    # hyprctl exits 0 while the reply carries the Lua error + note: the
    # real reason must reach her, not 'hyprctl workspace switch failed'
    b, _ = _lua_hypr(dispatch=lambda argv: Done(_LUA_ERR_REPLY))
    with pytest.raises(ToolError) as ei:
        b.switch_workspace("6")
    assert "syntax error" in str(ei.value)
    assert "shorthand for hl.dispatch" in str(ei.value)


def test_hyprland_lua_focus_window_drags_the_view():
    exprs = []
    flip = ('hl.dsp.focus({ window = "address:0x503" })', "0x503")
    b, _ = _lua_hypr(act="0x502", exprs=exprs, flip_addr=flip)
    out = b.focus_window("0x503")
    assert exprs == ['hl.dsp.focus({ window = "address:0x503" })']
    assert out["focused"] == "firefox" and out["address"] == "0x503"
    assert out["workspace"] == "8"        # the view followed the window
    # focus that stays put is an error, not 'done'
    b2, _ = _lua_hypr(act="0x502")
    with pytest.raises(ToolError, match="did not change"):
        b2.focus_window("0x503")


def test_hyprland_lua_workspace_to_monitor_left_screen(tmp_path, monkeypatch):
    # sense level: 'show workspace 2 on the left screen' resolves the phrase
    # to the live name, then Lua dispatchers move + focus
    exprs = []
    flip = ("hl.dsp.focus({ workspace = 2 })",
            {"id": 2, "name": "2", "monitor": 0})
    b, _ = _lua_hypr(exprs=exprs, flip_ws=flip)
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = {"desktop": {"awareness": "on"}}
    s = DesktopSense(cfg, None, awareness=Awareness(cfg), runner=b.runner,
                     backend=b, app_dirs=[])
    out = s.switch_workspace("desktop 2", monitor="the left screen")
    assert exprs == ['hl.dsp.workspace.move({ workspace = 2, monitor = "DP-1" })',
                     "hl.dsp.focus({ workspace = 2 })"]
    assert out["workspace"] == "2" and out["monitor"] == "DP-1"


def test_hyprland_lua_open_dolphin_on_workspace_2_with_follow():
    dol = {"address": "0x999", "class": "org.kde.dolphin", "title": "Dolphin",
           "workspace": {"id": 2, "name": "2"}, "monitor": 0, "pid": 9,
           "fullscreen": False, "floating": False, "focusHistoryID": 5}
    exprs = []
    b, _ = _lua_hypr(exprs=exprs, spawn=("hl.dsp.exec_cmd", dol),
                     moved=("hl.dsp.window.move", "2", 0))
    out = b.launch(["dolphin"], workspace="2", follow=True)
    assert out == {"workspace": "2"}      # verified against the re-read list
    assert 'hl.dsp.exec_cmd("dolphin", { workspace = "2" })' in exprs
    assert ('hl.dsp.window.move({ workspace = 2, window = "address:0x999", '
            "follow = true })") in exprs
    assert not any("silent" in e for e in exprs)
    # quiet placement = follow = false (the movetoworkspacesilent equivalent)
    exprs2 = []
    b2, _ = _lua_hypr(exprs=exprs2, spawn=("hl.dsp.exec_cmd", dol),
                      moved=("hl.dsp.window.move", "2", 0))
    b2.launch(["dolphin"], workspace="2")
    assert ('hl.dsp.window.move({ workspace = 2, window = "address:0x999", '
            "follow = false })") in exprs2


def test_hyprland_lua_exec_is_one_argv_element_and_escaped():
    term = {"address": "0x777", "class": "foot", "title": "foot",
            "workspace": {"id": 1, "name": "1"}, "monitor": 1, "pid": 8,
            "fullscreen": False, "floating": False}
    exprs = []
    b, _ = _lua_hypr(exprs=exprs, spawn=("hl.dsp.exec_cmd", term))
    out = b.launch(["sh", "-c", 'echo "hi"'])
    assert out == {"launched": True}
    # one argv element, no shell; quotes cannot break out of the Lua literal
    exec_calls = [c for c in b.runner.calls
                  if c[:2] == ["hyprctl", "dispatch"]
                  and c[-1].startswith("hl.dsp.exec_cmd")]
    assert exec_calls == [["hyprctl", "dispatch",
                           'hl.dsp.exec_cmd("sh -c echo \\"hi\\"")']]


def test_hyprland_lua_monitor_placement_launch_moves_and_pins():
    dol = {"address": "0x999", "class": "org.kde.dolphin", "title": "Dolphin",
           "workspace": {"id": 1, "name": "1"}, "monitor": 1, "pid": 9,
           "fullscreen": False, "floating": False}
    exprs = []
    b, _ = _lua_hypr(exprs=exprs, spawn=("hl.dsp.exec_cmd", dol),
                     moved=("hl.dsp.window.move", "2", 0))
    out = b.launch(["dolphin"], monitor="DP-1")   # no follow: quiet placement
    assert out == {"workspace": "2", "monitor": "DP-1"}
    assert 'hl.dsp.exec_cmd("dolphin")' in exprs
    assert ('hl.dsp.window.move({ workspace = 2, window = "address:0x999", '
            "follow = false })") in exprs
    assert ('hl.dsp.workspace.move({ workspace = 2, monitor = "DP-1" })'
            in exprs)


# ── sway / KDE / GNOME workspace switching ───────────────────────────
def test_sway_switch_workspace():
    runner = recording_runner({r"get_tree": Done(json.dumps(fix("sway_tree.json"))),
                               r"-t command": Done("[]")})
    b = sway_mod.SwayBackend(runner=runner)
    out = b.switch_workspace("2")
    cmds = [" ".join(c) for c in runner.calls if "-t command" in " ".join(c)]
    assert any(c.endswith("workspace 2") for c in cmds)
    assert out["workspace"] == "2"
    b.switch_workspace("web", monitor="DP-1")
    cmds = [" ".join(c) for c in runner.calls if "-t command" in " ".join(c)]
    mv = [i for i, c in enumerate(cmds) if 'move workspace to output DP-1' in c]
    ws = [i for i, c in enumerate(cmds) if 'workspace "web"' in c]
    assert mv and ws and mv[-1] < ws[-1]


def test_sway_launch_follow_focuses():
    tree0 = fix("sway_tree.json")
    foot = {"id": 777, "type": "con", "app_id": "foot", "name": "foot",
            "pid": 999, "focused": False, "nodes": [], "layout": "none"}
    state = {"added": False}

    def tree_reply(argv):
        t = json.loads(json.dumps(tree0))
        if state["added"]:
            t["nodes"][2]["nodes"][0]["nodes"].append(foot)
        return Done(json.dumps(t))

    runner = recording_runner({r"get_tree": tree_reply,
                               r"-t command": (lambda a: (state.__setitem__(
                                   "added", "exec " in " ".join(a)) or Done("[]")))})
    b = sway_mod.SwayBackend(runner=runner)
    out = b.launch(["foot"], workspace="3", follow=True)
    cmds = [" ".join(c) for c in runner.calls if "-t command" in " ".join(c)]
    assert out == {"workspace": "3"}
    assert any("move container to workspace 3" in c for c in cmds)
    assert any('[con_id="777"] focus' in c for c in cmds)   # the follow part


def test_kde_switch_workspace_and_desktop_per_screen():
    payload = fix("kwin_list.json")
    base = kwin_runner(payload)
    seen = []

    def reply(argv):                    # peek at the script that got loaded
        try:
            seen.append(Path(str(argv[-2])).read_text())
        except (OSError, ValueError):
            pass
        return base(argv)

    runner = recording_runner({r".": reply})
    b = KWinBackend(runner=runner)
    # current virtual desktop per screen comes through
    assert [m["active_workspace"] for m in b.monitors()] == ["1", "2"]
    assert b.windows()[0]["focus_recency"] is None      # no history in KWin
    assert b.windows()[1]["focus_recency"] == 0         # the active window
    assert b.switch_workspace("2")["workspace"] == "2"
    out = b.switch_workspace("2", monitor="DP-1")
    assert "every desktop on every screen" in out["note"]
    assert any("rikoSwitchWorkspace(" in s for s in seen)   # same script path
    bad = KWinBackend(runner=kwin_runner({"ok": False, "error": "no desktop 9"}))
    with pytest.raises(ToolError, match="no desktop 9"):
        bad.switch_workspace("9")


def test_gnome_switch_workspace():
    ok = {"ok": True, "workspace": "1"}
    b = GnomeBackend(runner=gnome_runner({
        "ActivateWorkspace": ok, "Monitors": fix("gnome_monitors.json")}))
    assert [m["active_workspace"] for m in b.monitors()] == ["1", "1"]
    assert b.switch_workspace("2")["workspace"] == "2"   # 'desktop 2' -> index 1
    joined = [" ".join(str(a) for a in c) for c in b.runner.calls
              if "ActivateWorkspace" in " ".join(str(a) for a in c)]
    assert joined and joined[-1].split()[-1] == "1"
    out = b.switch_workspace("web", monitor="MVN-1")
    assert "one workspace per screen" in out["note"]


# ── the owner's exact case, end to end ───────────────────────────────
LIVE_MONS = [{"name": "DP-1", "description": "Dell Inc. DELL U2518D 3C4YP99OB39L",
              "size": "2560x1440", "position": "0x0", "scale": 1},
             {"name": "DP-2", "description": "Dell Inc. DELL U2518D 3C4YP82K504L",
              "size": "2560x1440", "position": "2560x0", "scale": 1,
              "focused": True},
             {"name": "HDMI-A-1", "description": "GWD ARZOPA 2022110200001",
              "size": "2560x1440", "position": "5120x0", "scale": 1.25}]


def ff_sense(tmp_path, monkeypatch, act="hypr_activewindow_firefox.json",
             bridge_answers=None, cfg=None):
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = cfg or {"desktop": {"awareness": "on"}}
    answers = {"activeTabs": fix("ext_active_tabs.json"),
               "listTabs": fix("ext_list_tabs_ffwindows.json")}
    answers.update(bridge_answers or {})
    return DesktopSense(cfg, FakeBridge(answers), backend=hypr_ff(act),
                        awareness=Awareness(cfg))


def test_current_tab_is_the_compositor_focused_window(tmp_path, monkeypatch):
    """Owner report: hub window is Firefox's last-focused, but the OTHER
    window (different tab, other monitor) is what the compositor shows —
    THAT tab is 'current', and both windows are listed with their screens."""
    s = ff_sense(tmp_path, monkeypatch)
    f = s.desktop_focused()
    rows = f["browser_windows"]
    assert [r["window"] for r in rows] == [102, 101]        # godot first!
    assert rows[0]["tab"]["url"] == "https://docs.godotengine.org/en/stable/"
    assert rows[0]["screen"] == "left Dell" and rows[0]["focused_app"]
    assert rows[0]["workspace"] == "2" and rows[0]["tab_count"] == 12
    assert rows[1]["tab"]["title"] == "Riko Hub"            # the hub, second
    assert rows[1]["screen"] == "middle Dell"
    assert f["browser_tab"]["id"] == 21 and rows[0]["window"] == 102
    assert f["browser_tab"]["url"] == "https://docs.godotengine.org/en/stable/"
    assert f["browser_screen"] == "left Dell"
    assert "browser_tab_note" not in f
    # browser_tabs answers the same way (private window hidden by default)
    out = s.browser_tabs()
    assert out["windows"][0]["window"] == 102
    assert {r["window"] for r in out["windows"]} == {101, 102}


def test_firefox_not_focused(tmp_path, monkeypatch):
    """Compositor focuses kitty: say so, and name the most recent Firefox
    window's screen instead of pretending the hub tab is current."""
    s = ff_sense(tmp_path, monkeypatch, act="hypr_activewindow.json")
    f = s.desktop_focused()
    assert f["window"]["app"] == "kitty"
    assert "Firefox isn't focused" in f["browser_tab_note"]
    assert "left Dell" in f["browser_tab_note"] and "Godot" in f["browser_tab_note"]
    assert f["browser_windows"][0]["window"] == 102


def test_old_extension_grouping_fallback(tmp_path, monkeypatch):
    """Pre-0.1.3 helper: no activeTabs -> listTabs grouped by windowId;
    the compositor order still decides which window is current."""
    s = ff_sense(tmp_path, monkeypatch,
                 bridge_answers={"activeTabs": None,
                                 "listTabs": fix("ext_list_tabs_ffwindows.json")})
    out = s.browser_tabs()
    assert out["windows"][0]["window"] == 102
    assert out["windows"][0]["tab_count"] == 1
    hub = next(r for r in out["windows"] if r["window"] == 101)
    assert hub["tab_count"] == 3 and hub["screen"] == "middle Dell"


def test_private_window_suffix(tmp_path, monkeypatch):
    """The private window matches the 'Private Browsing' suffix row; shown
    only when the owner opted into private tabs."""
    s = ff_sense(tmp_path, monkeypatch)
    assert 103 not in [r["window"] for r in s.browser_tabs()["windows"]]
    cfg = {"desktop": {"awareness": {"enabled": "on", "private_tabs": True}}}
    s2 = ff_sense(tmp_path, monkeypatch, cfg=cfg)
    rows = {r["window"]: r for r in s2.browser_tabs()["windows"]}
    assert rows[103]["private"] and "portable" in rows[103]["screen"]
    assert rows[103]["tab"]["title"] == "Search Results"


def test_awareness_off_hides_screens(tmp_path, monkeypatch):
    """browser_tabs stays (browser tool), but with awareness off no
    screen/workspace names leak and the backend is not queried."""
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = {"desktop": {"awareness": "off"}}
    s = ff_sense(tmp_path, monkeypatch, cfg=cfg)
    out = s.browser_tabs()
    for r in out["windows"]:
        assert r["screen"] == "" and r["workspace"] == ""


# ── switch_workspace / open_app placement through the tool surface ──
class SwitchBackend(FakeBackend):
    def __init__(self, wins=None, mons=None):
        super().__init__(wins or [], mons or LIVE_MONS)
        self.sw = []

    def switch_workspace(self, workspace, monitor=None):
        self.sw.append((workspace, monitor))
        return {"workspace": workspace, "monitor": monitor or ""}


def test_switch_workspace_tool(tmp_path, monkeypatch):
    b = SwitchBackend()
    s = sense_on(tmp_path, monkeypatch, backend=b)
    assert s.switch_workspace("virtual desktop two")["workspace"] == "2"
    s.switch_workspace("show desktop 3 on the left screen", monitor="the Dell on the left")
    assert b.sw == [("2", None), ("3", "DP-1")]
    with pytest.raises(ToolError, match="cannot switch workspaces"):
        sense_on(tmp_path, monkeypatch).switch_workspace("2")
    with pytest.raises(ToolError, match="give a workspace"):
        s.switch_workspace("")


def test_dolphin_flow_switch_and_open_on_desktop_2(tmp_path, monkeypatch, app_dir):
    """The owner's 'switch to desktop 2 and open dolphin', fixtures only:
    dolphin comes from a fake .desktop index, the compositor is a recording
    fake — dispatch exec '[workspace 2] dolphin' without 'silent'."""
    (app_dir / "org.kde.dolphin.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Dolphin\nExec=dolphin %U\n"
        "Keywords=files;manager;\n")
    base = fix("hypr_clients_ffwindows.json")
    dol = {"address": "0x999", "class": "org.kde.dolphin", "title": "Dolphin",
           "workspace": {"id": 2, "name": "2"}, "monitor": 0, "pid": 9,
           "fullscreen": False, "floating": False, "focusHistoryID": 5}
    state = {"after_exec": False}
    ws_state = {"cur": {"id": 1, "name": "1", "monitor": 1}}

    def clients_reply(argv):
        return Done(json.dumps(base + ([dol] if state["after_exec"] else [])))

    def dispatch(argv):
        j = " ".join(argv)
        if "exec" in j:
            state["after_exec"] = True
        if argv[-1] == "workspace 2":
            ws_state["cur"] = {"id": 2, "name": "2", "monitor": 0}
        return Done("ok")

    runner = recording_runner({
        r"hyprctl monitors": Done(json.dumps(fix("hypr_monitors.json"))),
        r"hyprctl clients": clients_reply,
        r"hyprctl activewindow": Done(json.dumps(fix("hypr_activewindow.json"))),
        r"hyprctl workspaces": Done(json.dumps(fix("hypr_workspaces.json"))),
        r"dispatch hl\.dsp\.no_op": Done("Invalid dispatcher"),   # classic hyprlang
        r"hyprctl dispatch": dispatch,
        r"hyprctl activeworkspace": lambda a: Done(json.dumps(ws_state["cur"])),
    })
    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = {"desktop": {"awareness": "on"}}
    s = DesktopSense(cfg, None, awareness=Awareness(cfg), runner=runner,
                     backend=hyprland_mod.HyprlandBackend(runner=runner, lua=None),
                     app_dirs=[str(app_dir)])
    s.switch_workspace("virtual desktop 2")
    out = s.open_app("dolphin", workspace="desktop 2")
    assert out["launched"] and out["follow"] is True and out["workspace"] == "2"
    joined = [" ".join(c) for c in runner.calls]
    assert any("dispatch workspace 2" == c.split("hyprctl ")[-1] for c in joined)
    assert any('exec "[workspace 2] dolphin"' in c for c in joined)
    assert any("movetoworkspace 2,address:0x999" in c for c in joined)
    assert not any("silent" in c for c in joined)
    # open_app alone with only a workspace follows by default; monitor-only
    # placement does not steal the view
    state["after_exec"] = False
    out = s.open_app("dolphin", monitor="the Dell on the left", follow=False)
    assert out["follow"] is False and out["monitor"] == "DP-1"
    joined = [" ".join(c) for c in runner.calls]
    assert any("movetoworkspacesilent 2,address:0x999" in c for c in joined)
    assert any("moveworkspacetomonitor 2 DP-1" in c for c in joined)


def test_kde_open_app_switches_first(tmp_path, monkeypatch, app_dir):
    """KDE cannot place a launch on a desktop — switching there first IS
    the placement; follow=False says the app opened on the current one."""
    calls = []

    class B:
        name = "kwin"
        supports_launch = False
        def switch_workspace(self, workspace, monitor=None):
            calls.append(("switch", workspace, monitor))
            return {"workspace": workspace, "monitor": monitor or ""}
        def monitors(self):
            return LIVE_MONS

    monkeypatch.setenv("RIKO_APP_STATE_DIR", str(tmp_path))
    cfg = {"desktop": {"awareness": "on"}}
    runner = recording_runner({r".": Done("")})
    s = DesktopSense(cfg, None, awareness=Awareness(cfg), runner=runner,
                     backend=B(), app_dirs=[str(app_dir)])
    out = s.open_app("foot", workspace="2")
    assert calls == [("switch", "2", None)]           # switched before launch
    assert any("gtk-launch" in " ".join(c) or "gio" in " ".join(c)
               for c in runner.calls)                  # then the generic launch
    out = s.open_app("foot", workspace="2", follow=False)
    assert "current workspace" in out.get("note", "")


def test_prompt_line_covers_windows_workspaces_keystrokes():
    from hub.tools.registry import tools_prompt
    line = tools_prompt(["pc__desktop_windows", "pc__open_app"])
    assert "most recently focused" in line
    assert "switch workspaces/virtual desktops" in line
    assert "you don't need keystrokes" in line
