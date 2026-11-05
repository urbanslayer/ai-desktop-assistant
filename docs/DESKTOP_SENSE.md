# Desktop awareness contract ("she can see the desktop")

Status: owner request (2026-11-05), planned. Children follow this literally and report problems instead
of editing it. All of it is **desktop-client tools** on the existing client-tool channel (docs/API.md;
the hub offers them only while the desktop is online, with the same permission and approval machinery
as the browser tools).

## What she gets

| tool | risk / default permission | what it returns / does |
|---|---|---|
| `desktop_windows()` | read / always | Open windows: app id/class, title, workspace, monitor, focused, fullscreen/floating, pid. Titles are capped at 120 chars. |
| `desktop_focused()` | read / always | The focused window (same shape), plus the **focused browser tab** (title + URL) when Firefox is focused and the Riko Helper is linked. |
| `browser_tabs()` | read / always | All Firefox tabs (title, URL, active, window, pinned, audible) via the Riko Helper extension, which already has the `tabs` permission. |
| `desktop_monitors()` | read / always | Monitors: name, description/make/model, size, position, scale, focused, active workspace. On Hyprland, also the **configured** layout from the Lua config (see below). |
| `desktop_apps(query?)` | read / always | Installed launchable apps from XDG `.desktop` files (Name, GenericName, Keywords, Exec id, NoDisplay/Hidden respected), fuzzy-searched. |
| `open_app(app, monitor?, workspace?)` | act / **ask** (owner can set always) | Launch an installed app (by desktop id or fuzzy name). It can go to a given monitor (by name, description or position words like "left/middle/right", "the Dell on the right", "the portable screen") or workspace where the compositor allows it. |
| `focus_window(window)` | act / ask | Bring an open window to the front (where the compositor allows it). |

Not in scope: keystrokes or mouse control of other apps, closing or killing windows, reading window
*contents*, and screenshots. Screenshots ("look at my screen") are an optional later step, below.

## Privacy rules

- **Master toggle:** `desktop.awareness: on | off` in `character_config.yaml`, and a tray checkbox
  **Let her see my desktop** (default **off** until the owner turns it on). When it's off, the tools
  aren't registered.
- **Denylist:** `desktop.awareness.hide` for app ids/title regexes that are never reported (default:
  password managers such as keepassxc/bitwarden/1password, plus private/incognito browser windows).
  Matching windows appear as `{"app": "<hidden>"}` or are dropped.
- **Nothing is stored:** results flow into the turn only (tool log keeps the call, not the window list
  beyond the existing result-char cap).

## Backends (detect at runtime; first one that works)

1. **Hyprland** (`HYPRLAND_INSTANCE_SIGNATURE` set):
   - windows/focus/monitors/workspaces from `hyprctl -j clients|activewindow|monitors|workspaces` (or the
     IPC socket directly);
   - launch with `hyprctl dispatch exec "[workspace N silent] <cmd>"` (or `[monitor <name>]` rules where
     supported); focus with `hyprctl dispatch focuswindow address:<addr>`.
   - **Config awareness (owner instruction):** read `~/.config/hypr/hyprland.lua` FIRST, then follow its
     `require("…")` / `dofile` / `loadfile` lines. Module names resolve relative to `~/.config/hypr/`,
     e.g. `require("monitors")` → `monitors.lua`, and likewise `keybinds.lua` and `windowrules.lua`.
     - Parse **read-only**:
       - `hl.monitor({output=…, mode=…, position=…, scale=…})` blocks, including `desc:` outputs and
         commented-out ones (ignored);
       - workspace↔monitor rules;
       - keybinds that launch apps (e.g. `exec` binds), so "open my terminal" means the owner's
         configured terminal.
     - Use these to map names like "the left/middle/right screen", "the Dell", "the portable/ARZOPA one" to
       the live monitor names from `hyprctl monitors`. The live state wins; the config adds the owner's
       intent and naming.
   - **Never write** to anything under `~/.config/hypr`, and never reload Hyprland.
2. **KDE Plasma 6 (KWin):**
   - windows via a small **KWin script** loaded at runtime over D-Bus (`org.kde.KWin /Scripting`
     `loadScript` + `run`). It reports `workspace.windowList()`, captions, resourceClass, output and
     desktop back through a D-Bus call or a temp file, then unloads.
   - focus via the same script (`workspace.activeWindow = w`); monitors via the script's `workspace.screens`.
   - launch via `kioclient exec <desktop file>` / `gtk-launch`. A specific screen is best-effort: KWin rule
     or script move after it maps.
3. **GNOME (Mutter):**
   - Wayland GNOME exposes no window list to normal apps (`Shell.Eval` is disabled since 41).
   - Support it via a tiny optional **GNOME Shell extension** we ship (`riko-windows@riko`). It exposes a
     D-Bus `List()`/`Activate(id)` reading `global.get_window_actors()`, and Riko's README gives
     one-command install instructions.
   - Without the extension: only `desktop_apps`/`open_app` (via `gio launch`/`gtk-launch`) and the browser
     tools work, and the tools say why.
4. **X11 / other wlroots** (Sway: `swaymsg -t get_tree`; X11: EWMH via `xprop`/python-xlib):
   best-effort, implement Sway since it's cheap, and X11 if time allows.
5. **Generic:**
   - the app list from XDG data dirs;
   - launch via `gtk-launch <desktop id>`, falling back to `gio launch <path>`;
   - always detach (setsid, no inherited stdio), and use the owner's environment;
   - never run arbitrary shell strings from the model: only desktop ids/Exec lines from installed
     .desktop files, with field codes (%u %f …) stripped.

## Browser tabs

The Riko Helper extension already has `tabs`. It adds `listTabs` / `activeTab` methods on its existing
link (title, URL, windowId, active, pinned, audible, incognito → dropped unless the owner opts in). The
desktop answers `browser_tabs`/the focused-tab part from it. Firefox extension changes need a version bump
and re-signing (`web-ext sign --channel unlisted`, keys in browser-ext/.env, never printed).

## Prompt

When the tools are offered, the hub's tools block gets one line: "You can see the owner's open windows,
browser tabs and screens, and open apps on a given screen, with the desktop_* and browser_tabs tools; look
before guessing, and say which screen you used."

## Optional later: "look at my screen"

- `desktop_screenshot(monitor?)` via xdg-desktop-portal Screenshot. On GNOME and KDE this asks
  interactively each time unless the owner grants permission. On Hyprland it's grim plus the focused
  monitor name.
- Only if the LLM is vision-capable (check `/v1/models` capabilities, or a setting).
- Off by default, permission ask.
- Never used by assistants/children for testing on the owner's display.

## Testing rules (children)

- Never query or screenshot the owner's live session in tests. Unit tests use fake backends: recorded
  JSON fixtures of hyprctl/KWin/GNOME output, and fake .desktop dirs.
- For a REAL check, use a **nested headless compositor**:
  - `sway --headless` plus a nested Hyprland if possible (`Hyprland` with
    `WLR_BACKENDS=headless`, private XDG_RUNTIME_DIR);
  - KDE via `kwin_wayland --virtual` if installed.
  - Launch test apps there and list, focus and open on a monitor. Report what was verified per backend.
- Parse a COPY of the owner's `~/.config/hypr/*.lua` (read-only) in a test to prove the monitor-mapping
  parser handles the real file: the Dell U2518D ×2 and the ARZOPA portable, `desc:` outputs and commented
  blocks.

## Ownership

| path | owner |
|---|---|
| `riko/desktop_sense/**` (new package), `riko/client_tools.py`, `desktop.py`, `character_config.yaml`, README section, non-hub tests, `desktop/gnome-extension/**` | D |
| `browser-ext/**` (tabs methods, version bump, re-sign) | B |
| `hub/tools/registry.py` prompt line + `docs/API.md` client-tool list | D (one-liners) |
