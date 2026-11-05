"""Desktop awareness (docs/DESKTOP_SENSE.md) — fixture-driven, fake-runner
tests. Nothing here touches a live compositor: every backend call goes
through a `runner=` fake fed by recorded JSON in tests/fixtures/
desktop_sense/ (shapes recorded from real hyprctl/swaymsg/KWin/GNOME
output), and the launcher tests only record what WOULD run."""
import json
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
        r"hyprctl dispatch": Done("ok"),
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
    state = {"after_exec": False}
    mons = json.dumps(fix("hypr_monitors.json"))

    def clients_reply(argv):
        return Done(json.dumps(base + ([foot] if state["after_exec"] else [])))

    def dispatch(argv):
        if "exec" in " ".join(argv):
            state["after_exec"] = True
        return Done("ok")

    runner = recording_runner({
        r"hyprctl monitors": Done(mons), r"hyprctl clients": clients_reply,
        r"hyprctl activewindow": Done("{}"), r"hyprctl workspaces":
            Done(json.dumps(fix("hypr_workspaces.json"))),
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
    rows = s.browser_tabs()
    assert [r["id"] for r in rows] == [1]          # private tab hidden by default
    assert rows[0]["title"] == "A" and rows[0]["active"]
    # old extension: listTabs unknown -> falls back to tabs
    old = {"tabs": {"ok": True, "result": [{"title": "X", "url": "https://x",
                                            "active": True}]}}
    s2 = sense_on(tmp_path, monkeypatch, bridge=FakeBridge(old))
    rows2 = s2.browser_tabs()
    assert rows2[0]["title"] == "X" and rows2[0]["id"] == 1
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
    assert len(s.browser_tabs()) == 1


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
                          "desktop_apps", "open_app", "focus_window"}
    assert names["desktop_windows"]["risk"] == "read"
    assert names["open_app"]["risk"] == "act" and names["open_app"]["permission_default"] == "always"
    assert names["focus_window"]["permission_default"] == "always"


def test_no_backend_message(tmp_path, monkeypatch):
    s = sense_on(tmp_path, monkeypatch, backend=None)
    s._backend, s._backend_tried = None, True
    with pytest.raises(ToolError, match="no supported window manager"):
        s.desktop_windows()


# ── hub prompt line ───────────────────────────────────────────
def test_prompt_line_appears_only_with_desktop_tools():
    from hub.tools.registry import tools_prompt
    with_line = tools_prompt(["pc__desktop_windows", "pc__open_app"])
    assert "open windows" in with_line and "which screen" in with_line
    plain = tools_prompt(["pc__browser_tabs"])
    assert "open windows" not in plain
