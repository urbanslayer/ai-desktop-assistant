"""The tray's Persona submenu, headless (no display, no tray, no audio).

Owner bug these pin down: clicking a persona in the tray did nothing, with
`[persona] POST /api/active -> 404: no persona matches 'True'` in the terminal.
QAction.triggered hands the action's `checked` bool to its slot, and the old
`def pick(pid=p["id"], _=False)` took it as the FIRST positional argument, so
the id became True. The actions were also not in an exclusive QActionGroup,
so several personas could be ticked at once.

Qt runs on the offscreen platform with DISPLAY / WAYLAND_DISPLAY cleared, so
this never touches the owner's desktop even with a tray-less bar.
"""
import inspect
import os
import sys

import pytest

# Qt must never reach for the owner's real display. Set before desktop.py is
# imported (PySide6 reads QT_QPA_PLATFORM when QApplication is built), and the
# autouse fixture re-asserts it per test.
os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _display_var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
    os.environ.pop(_display_var, None)

from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

import desktop  # noqa: E402
from conftest import persona  # noqa: E402
from riko import hub_client  # noqa: E402

IDS = ("riko", "asuka", "senpai")


@pytest.fixture(autouse=True)
def _offscreen():
    """A test that restores the owner's DISPLAY must not leak it to the next."""
    for _display_var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE"):
        os.environ.pop(_display_var, None)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    yield


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([sys.argv[0]])


class FakeSession:
    """Only what the tray menu touches: switch_persona(id). Records the id it
    was given and drives the fake hub exactly like riko/session.py does."""

    def __init__(self, client, hub):
        self.client = client   # what Session.hub is: a hub_client.HubClient
        self.hub = hub         # the FakeHub server, for asserting its state
        self.switched = []

    def switch_persona(self, pid):
        self.switched.append(pid)
        state = self.client.set_active(pid)  # HubError on 401/404, like the real hub
        hub_client.announce_persona(state)
        return state["active"]


@pytest.fixture
def fake_hub3(fake_hub):
    fake_hub.personas["senpai"] = persona("senpai", "Senpai", "senpai", "Hai?")
    return fake_hub


# PySide6 deletes a Python-orphaned parent QMenu, taking its children with it.
_KEEPALIVE = []


def make_tray_menu(session):
    root = QMenu()
    _KEEPALIVE.append(root)  # main() keeps the tray menu alive; tests mimic that
    return desktop.build_persona_menu(root, session)


@pytest.fixture
def tray(hub_connected, fake_hub3, qapp):
    session = FakeSession(hub_connected, fake_hub3)
    persona_menu = make_tray_menu(session)
    return persona_menu, session, fake_hub3


def actions_of(persona_menu):
    return {a.data(): a for a in persona_menu.actions() if a.isCheckable()}


def checked_ids(persona_menu):
    return {a.data() for a in persona_menu.actions() if a.isCheckable() and a.isChecked()}


def test_submenu_lists_every_persona_and_ticks_only_the_active_one(tray):
    persona_menu, _, _ = tray
    assert list(actions_of(persona_menu)) == list(IDS)
    assert checked_ids(persona_menu) == {"riko"}


def test_clicking_another_persona_switches_by_id_not_by_bool(tray):
    # the owner bug: the checked bool landed in pid, the hub 404'd on 'True'
    persona_menu, session, hub = tray
    actions_of(persona_menu)["asuka"].trigger()
    assert session.switched == ["asuka"]
    assert all(isinstance(pid, str) for pid in session.switched)
    assert hub.active == "asuka"


def test_exactly_one_persona_stays_ticked(tray):
    persona_menu, _, hub = tray
    actions_of(persona_menu)["senpai"].trigger()
    assert checked_ids(persona_menu) == {"senpai"}
    assert hub.active == "senpai"
    actions_of(persona_menu)["riko"].trigger()
    assert checked_ids(persona_menu) == {"riko"}


def test_clicking_the_active_persona_does_nothing(tray):
    persona_menu, session, hub = tray
    actions_of(persona_menu)["riko"].trigger()
    assert session.switched == []
    assert checked_ids(persona_menu) == {"riko"}
    assert hub.active == "riko"


def test_failed_switch_re_syncs_the_ticks_from_the_hub(tray):
    # a dead hub means no switch: Qt's own click tick must snap back
    persona_menu, session, hub = tray
    hub.api_key = "wrong-key"
    actions_of(persona_menu)["asuka"].trigger()
    assert session.switched == ["asuka"]
    assert hub.active == "riko"
    assert checked_ids(persona_menu) == {"riko"}


def test_menu_rebuilds_from_the_hub_when_it_opens(qapp, hub_connected, fake_hub3):
    # the submenu tracks the hub (web page, another PC) at aboutToShow
    session = FakeSession(hub_connected, fake_hub3)
    persona_menu = make_tray_menu(session)
    fake_hub3.active = "senpai"
    hub_client.note_active(fake_hub3.state())
    persona_menu.aboutToShow.emit()
    assert checked_ids(persona_menu) == {"senpai"}
    assert list(actions_of(persona_menu)) == list(IDS)


def test_unreachable_hub_leaves_a_disabled_line(qapp, hub_connected, fake_hub3):
    persona_menu = make_tray_menu(FakeSession(hub_connected, fake_hub3))
    fake_hub3.api_key = "wrong-key"
    persona_menu.aboutToShow.emit()
    lines = persona_menu.actions()
    assert len(lines) == 1
    assert not lines[0].isEnabled()
    assert "hub unreachable" in lines[0].text()


def test_no_connected_tray_slot_receives_a_bool_for_an_id():
    """The bug class, closed at the source: persona-menu slots connected to
    triggered() must take the checked bool as their FIRST positional
    parameter, so the id can only ever arrive bound behind it. Pulled straight
    out of desktop.py's source and called with True, so a future rewrite to a
    default-arg closure (the old `def pick(pid=p['id'], _=False)`) fails here."""
    src = inspect.getsource(desktop.build_persona_menu)
    line = next(l for l in src.splitlines() if ".triggered.connect(" in l)
    connect_arg = line.split(".triggered.connect(", 1)[1].strip()
    assert connect_arg.startswith("lambda") and connect_arg.endswith(")")
    calls = []
    scope = {"p": {"id": "probe-id"}, "pick": calls.append}
    slot = eval(connect_arg[:-1], scope)  # drop connect's closing paren
    slot(True)  # exactly what QAction.triggered emits
    assert calls == ["probe-id"], f"slot got {calls!r}: the checked bool leaked into the id"
    assert list(inspect.signature(slot).parameters) == ["_checked", "pid"]
