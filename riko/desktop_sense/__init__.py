"""Desktop awareness (docs/DESKTOP_SENSE.md): windows, focus, monitors, apps.

Everything the desktop client offers the model through the `desktop_*` tools
lives here. The package is Qt-free and talks to the live session only when a
tool actually runs — unit tests inject recorded JSON and a fake runner, and
never touch the owner's compositor.

Layout:
  awareness.py  the privacy master toggle (`desktop.awareness`, tray
                checkbox "Let her see my desktop", persisted like listen
                mode) + the denylist (password managers, private windows)
  apps.py       generic XDG .desktop index + the safe launcher (desktop ids
                and sanitized Exec lines only — never a shell string from
                the model; always detached)
  monitors.py   monitor phrase resolver: "left/middle/right", "the Dell on
                the right", "the portable screen", exact output names
  hyprlua.py    read-only reader for the owner's Hyprland Lua config
                (hyprland.lua -> require/dofile/loadfile -> monitors,
                window rules, exec keybinds). Never writes, never reloads.
  hyprland.py   Hyprland backend (hyprctl -j + dispatch)
  sway.py       Sway backend (swaymsg -t get_tree + commands)
  kde.py        KDE Plasma backend (KWin script over D-Bus)
  gnome.py      GNOME backend: the D-Bus client for the shipped Shell
                extension desktop/gnome-extension/riko-windows@riko;
                without the extension only apps/open_app work (contract §3)
  __init__.py   DesktopSense: backend detection + the tool surface
"""
from .awareness import Awareness
from .sense import DesktopSense, detect_backend, sense_tools

__all__ = ["Awareness", "DesktopSense", "detect_backend", "sense_tools"]
