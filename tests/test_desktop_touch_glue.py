"""desktop.py touch glue, offscreen (docs/TOUCH.md): the SitEdgeMonitor
sits on touch_desk.SitWatcher and reads the monitor edge from injected
hyprctl fixture rows (fake runner — the owner's live session is NEVER
queried here) or from Qt geometry. No compositor, no display, no tray.
"""
import json
import os
import pathlib
import time

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
for _var in ("DISPLAY", "WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
             "SWAYSOCK", "DBUS_SESSION_BUS_ADDRESS"):
    os.environ.pop(_var, None)

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import desktop  # noqa: E402
from riko import touch_desk  # noqa: E402

FX = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class FakeWin:
    """Duck-typed AvatarView: only last_bbox + windowHandle are used."""

    def __init__(self, hips_y=None):
        self.last_bbox = {} if hips_y is None else {"hips_y": hips_y}

    def windowHandle(self):
        return None


def _rows():
    return (json.loads((FX / "hypr_real_clients_riko.json").read_text()),
            json.loads((FX / "hypr_real_monitors.json").read_text()))


def _watcher(qapp, monkeypatch, hips_y, runner=None):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "fixture-only")
    se = desktop.SitEdgeMonitor(FakeWin(hips_y), {},
                                runner=runner or (lambda: _rows()))
    cmds = []
    se.on_cmd = cmds.append
    se._until = time.monotonic() + 30
    return se, cmds


def test_sit_edge_fixture_sits_and_stands(qapp, monkeypatch):
    clients, monitors = _rows()
    row = next(c for c in clients if c.get("initialClass") == "riko")
    edge = touch_desk.edge_y_from_hypr(clients, monitors,
                                       lambda c: c.get("initialClass") == "riko")
    assert edge is not None
    se, cmds = _watcher(qapp, monkeypatch, hips_y=edge - 20)  # hips above the 8 px band
    se._tick()
    assert cmds == []
    se.win.last_bbox = {"hips_y": edge}                        # hips on the line
    se._tick()
    assert cmds == [f"sit:{round(edge)}"]
    se.win.last_bbox = {"hips_y": edge - 100}                  # dragged up
    se._tick()
    assert cmds == [f"sit:{round(edge)}", "stand"]


def test_sit_edge_none_until_known(qapp, monkeypatch):
    se, cmds = _watcher(qapp, monkeypatch, hips_y=10_000,
                        runner=lambda: ([], []))               # no client found
    se._tick()
    assert cmds == []                                          # edge unknown


def test_sit_edge_timer_stops_after_window(qapp, monkeypatch):
    se, cmds = _watcher(qapp, monkeypatch, hips_y=10_000)
    se._until = time.monotonic() - 1                           # window over
    se._timer.start()
    se._tick()
    assert not se._timer.isActive()                            # never continuous


def test_sit_edge_disabled_never_arms(qapp, monkeypatch):
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "fixture-only")
    se = desktop.SitEdgeMonitor(FakeWin(10_000), {"sit_detection": False},
                                runner=lambda: _rows())
    se.arm()
    assert not se._timer.isActive()


def test_qt_geometry_fallback_without_hypr(qapp, monkeypatch):
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    se = desktop.SitEdgeMonitor(FakeWin(10_000), {}, runner=lambda: _rows())
    assert se.edge_y_css() is None        # windowHandle None -> unknown, no crash


def test_touch_kinds_match_contract():
    assert desktop.TOUCH_KINDS == {
        "pat", "poke", "tickle", "grab", "hold", "release", "action",
        "sit", "stand"}
