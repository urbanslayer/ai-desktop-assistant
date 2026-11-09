# Desktop awareness contract ("she can see the desktop")

Status: owner request (2026-11-05), planned. Children follow this literally and report problems instead
of editing it. All of it is **desktop-client tools** on the existing client-tool channel (docs/API.md;
the hub offers them only while the desktop is online, with the same permission and approval machinery
as the browser tools).

## What she gets

| tool | risk / default permission | what it returns / does |
|---|---|---|
| `desktop_windows()` | read / always | Open windows: app id/class, title, workspace, monitor, focused, fullscreen/floating, pid. Titles are capped at 120 chars. Also `current_workspaces`: {screen phrase: the workspace that screen shows now} — the model can see a switch land even when the destination workspace is empty (2026-11-06). |

| `desktop_focused()` | read / always | The focused window (same shape), plus the Firefox windows when the Riko Helper is linked: `browser_windows` lists **every Firefox window** (active tab, tab count, screen, workspace, rank — index 0 is the CURRENT tab, ordered by the compositor's focus recency, not Firefox's own last-focused order), `browser_tab`/`browser_screen` describe that window's tab. When Firefox has windows but isn't focused, `browser_tab_note` says so and names the most recent window's screen. |
| `browser_tabs()` | read / always | `{windows, tabs}`: `windows` is the same per-Firefox-window list (most recently focused first — THE current tab is `windows[0]`; screens/workspaces only while desktop awareness is on), `tabs` is every tab (title, URL, active, window, pinned, audible) via the Riko Helper extension, which already has the `tabs` permission. |
| `desktop_monitors()` | read / always | Monitors: name, description/make/model, size, position, scale, focused, active workspace. On Hyprland, also the **configured** layout from the Lua config (see below). |
| `desktop_apps(query?)` | read / always | Installed launchable apps from XDG `.desktop` files (Name, GenericName, Keywords, Exec id, NoDisplay/Hidden respected), fuzzy-searched. |
| `open_app(app, monitor?, workspace?, follow?)` | act / **always** (owner decision 2026-11-05: no confirmation) | Launch an installed app (by desktop id or fuzzy name). It can go to a given monitor (by name, description or position words like "left/middle/right", "the Dell on the right", "the portable screen") or workspace where the compositor allows it. `follow` (default true when a workspace was asked for — the user said "switch to X and open …") ALSO switches the view there; on Hyprland that's `hyprctl dispatch exec "[workspace N] <cmd>"` (no `silent` when following) on hyprlang configs or `hl.dsp.exec_cmd` + `hl.dsp.window.move` on Lua configs (0.55+, see Backends), on Sway move-container + focus, on KDE/GNOME switch-to-the-desktop-then-launch. |
| `switch_workspace(workspace, monitor?)` | act / **always** (owner decision 2026-11-05, same as open_app) | Show a workspace / virtual desktop: accepts `2`, `desktop 2`, `virtual desktop two` or a workspace name. Hyprland `hyprctl dispatch workspace <n|name>` on hyprlang configs or `hl.dsp.focus({ workspace = N })` on Lua configs (0.55+, see Backends; the reply is checked and the view re-read, so "done" means it actually moved) (with a monitor: `moveworkspacetomonitor` first — `hl.dsp.workspace.move` on Lua — that is the dispatch that shows workspace 2 on the left screen), Sway `swaymsg workspace <n>` (`move workspace to output` first with a monitor), KDE via the KWin script (`workspace.currentDesktop = desktops[n-1]`, "virtual desktop" wording), GNOME via the extension's `ActivateWorkspace(index)`. On success the result is unambiguous: `now_showing {workspace, screen, windows_on_it}` plus a message like "Now showing workspace 2 on the middle Dell (empty)." — an EMPTY workspace has no focused window and that IS success. Exactly one dispatch goes to the compositor per switch; the Lua-vs-hyprlang style is decided from the config file layout when unambiguous (never a probe dispatch in front of the user's switch). |
| `focus_window(window)` | act / always | Bring an open window to the front (where the compositor allows it). |

Not in scope: mouse control of other apps, closing or killing windows, reading window
*contents*, and screenshots. Keystrokes entered the scope with docs/TYPING.md (typing +
dictation tools, ydotool backend); everything else about other apps stays out. Workspace
switching is NOT a keystroke: it is a first-class compositor action (`switch_workspace`),
so "switch to desktop 2" never needs key synthesis. Screenshots ("look at my screen") are
an optional later step, below.

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
     supported; drop `silent` when `follow` is set so the view switches with the app); focus with
     `hyprctl dispatch focuswindow address:<addr>`; switch workspaces with
     `hyprctl dispatch workspace <n|name>` (`moveworkspacetomonitor <n> <monitor>` first when the user
     named a screen). These classic strings are for **hyprlang configs only** — see the Lua note below.
     Windows carry `focusHistoryID` (`hyprctl -j clients`) — 0 is the currently focused
     window, larger is less recent — which decides which Firefox window owns "the current tab".
   - **Lua configs (Hyprland 0.55+, the owner's setup): dispatch is a Lua expression, not a command
     string.** `hyprctl dispatch <x>` runs `eval("return hl.dispatch(<x>)")`
     (src/debug/HyprCtl.cpp `dispatchRequest`, v0.56.2), so classic strings are invalid Lua:
     the reply is an error plus "dispatch in lua is a shorthand for hl.dispatch(...)"
     and hyprctl can still exit 0. Actions therefore check the config style once (a probe
     dispatch of `hl.dsp.no_op()` answers `ok` on Lua, `Invalid dispatcher` on hyprlang) and send
     hl.dsp.* expressions as ONE argv element (never a shell): switch workspace (creates an empty
     one, like the Super+N binds) `hl.dsp.focus({ workspace = 6 })` (numbers unquoted, names
     quoted); focus a window `hl.dsp.focus({ window = "address:0x..." })`; move a window
     `hl.dsp.window.move({ workspace = 2, window = "address:0x...", follow = true|false })`
     (`follow = false` = the old `movetoworkspacesilent`); move a workspace to a screen
     `hl.dsp.workspace.move({ workspace = 2, monitor = "DP-1" })`; launch
     `hl.dsp.exec_cmd("dolphin", { workspace = "2" })` — the table is a window rule for the
     spawned process (NOT `exec_raw`, which spawns a raw process instead of dispatching).
     Success is ONLY the exact reply `ok`; any other text is raised as the tool error verbatim
     (so she can repeat the real reason), and every action re-reads
     `hyprctl -j activeworkspace|activewindow` afterwards — an accepted command that left the
     view unchanged errors with "the view did not change" instead of reporting done.
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
   - focus via the same script (`workspace.activeWindow = w`); monitors (with the current virtual
     desktop per screen) via the script's `workspace.screens`; switch virtual desktops via the same
     script (`workspace.currentDesktop = desktops[n-1]`, name-aware on Plasma 6).
   - launch via `kioclient exec <desktop file>` / `gtk-launch`. A specific screen is best-effort: KWin rule
     or script move after it maps.
3. **GNOME (Mutter):**
   - Wayland GNOME exposes no window list to normal apps (`Shell.Eval` is disabled since 41).
   - Support it via a tiny optional **GNOME Shell extension** we ship (`riko-windows@riko`). It exposes a
     D-Bus `List()`/`Activate(id)`/`ActivateWorkspace(index)` reading `global.get_window_actors()`
     (workspace indexes/names come from `workspace_manager`), and Riko's README gives
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
link (title, URL, windowId, active, pinned, audible, incognito → dropped unless the owner opts in) and,
from 0.1.3, `activeTabs`: `{focusedWindowId, windows: [{windowId, focused, state, title, tabCount,
lastFocusedAt, activeTab}]}` ordered most-recently-focused first. Older extensions only know `activeTab`/
`listTabs` — the desktop then groups `listTabs` by windowId (active tab per window, focus order unknown).
Either way the **compositor** (focusHistoryID / the focused window) decides which Firefox window is
"current", matched by the window's active-tab title. The desktop answers
`browser_tabs`/the focused-tab part from it. Firefox extension changes need a version bump
and re-signing (`web-ext sign --channel unlisted`, keys in browser-ext/.env, never printed).

## Prompt

When the tools are offered, the hub's tools block gets one line: "You can see the owner's open windows,
browser tabs and screens, and open apps on a given screen, with the desktop_* and browser_tabs tools; look
before guessing, and say which screen you used. There can be several Firefox windows; when asked for the
current/active tab, give the most recently focused one first and mention the others with their screen if
more than one window is open. You can switch workspaces/virtual desktops and open apps on a given workspace
or screen; you don't need keystrokes for that."

## Optional later: "look at my screen"

- `desktop_screenshot(monitor?)` via xdg-desktop-portal Screenshot. On GNOME and KDE this asks
  interactively each time unless the owner grants permission. On Hyprland it's grim plus the focused
  monitor name.
- Only if the LLM is vision-capable (check `/v1/models` capabilities, or a setting).
- Off by default, permission ask.
- Never used by assistants/children for testing on the owner's display.

## Testing rules (children)

**Owner rule (replaces the old nested-compositor section — REVOKED after an
incident): never START or QUERY a compositor in tests.** A child's "nested
headless Hyprland" attempts aborted and the owner's LIVE Hyprland SIGSEGV'd
at the same second, killing their session. So:

- No compositor, window manager or display server is ever launched by a
  test — not nested, not "headless", not `--virtual`: no Hyprland, sway,
  kwin_wayland, weston, gnome-shell, cage, mutter, labwc, niri, river,
  wayfire, Xorg, Xwayland, Xvfb, Xephyr, sddm/gdm/lightdm.
- Never query the owner's live session either: no `hyprctl`, `swaymsg`,
  `qdbus`, `gdbus`, `busctl`, `dbus-send`, `grim` against it.
- **Verification = recorded fixtures + offscreen Qt only.**
  - every backend answers from recorded JSON fixtures
    (`tests/fixtures/desktop_sense/`: hyprctl clients/activewindow/
    monitors/workspaces, sway tree, KWin script payload, GNOME extension
    replies) injected through the `runner=` fake — a backend call that
    reaches real `subprocess` in a test is a bug;
    - the launcher tests only record what WOULD run (fake runner records
      argv); nothing is ever spawned;
    - tray/UI behaviour is covered with `QT_QPA_PLATFORM=offscreen` tests
      that trigger the real QAction;
    - `tests/conftest.py` pops `HYPRLAND_INSTANCE_SIGNATURE`,
      `WAYLAND_DISPLAY`, `DISPLAY`, `SWAYSOCK` and
      `DBUS_SESSION_BUS_ADDRESS` for the whole run (so detection code sees
      "no compositor" and test-started subprocesses inherit a clean env)
      and raises if any `subprocess.run/Popen` targets a compositor,
      display server or session-bus binary without a fake runner.
- Parse a COPY of the owner's `~/.config/hypr/*.lua` (read-only) in a test
  to prove the monitor-mapping parser handles the real file: the Dell
  U2518D ×2 and the ARZOPA portable, `desc:` outputs and commented blocks
  (`tests/fixtures/hypr_lua/`).
- Anything that needs a live compositor — real hyprctl output, KWin script
  loading, the GNOME extension in place, actual launches and moves — is
  verified by the **owner on their session**. Children report what is
  fixture-verified per backend and what still awaits that live check; they
  never verify it themselves on the owner's display.

## Ownership

| path | owner |
|---|---|
| `riko/desktop_sense/**` (new package), `riko/client_tools.py`, `desktop.py`, `character_config.yaml`, README section, non-hub tests, `desktop/gnome-extension/**` | D |
| `browser-ext/**` (tabs methods, version bump, re-sign) | B |
| `hub/tools/registry.py` prompt line + `docs/API.md` client-tool list | D (one-liners) |
