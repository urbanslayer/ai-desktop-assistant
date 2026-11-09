# Typing, dictation and app editing (contract)

Owner request: Riko can send keystrokes (terminal, dictation into Obsidian/Kate/a word processor), create or
open Obsidian notes and add or fix text, and work with Kate tabs and files. This replaces the "no keystrokes"
line in docs/DESKTOP_SENSE.md "Not in scope" for the cases below. Mouse control, screenshots and reading
window contents stay out of scope.

## Owner decisions

- **Backend: ydotool** (works on every desktop; needs the `ydotoold` daemon with `/dev/uinput` access). The
  owner installs and enables it; Riko never installs packages or starts daemons. Missing or unreachable
  daemon → a clear tool error with the setup steps (see "Owner setup").
- **Dictation mode**: the owner turns it on (tray item "Dictation", `riko-ctl dictation on|off`, or saying
  "start dictating" → the `set_dictation` tool with the ASK approval bubble). While on, she types freely into
  **the window that was focused when dictation started** (its compositor address/id is pinned). If focus
  moves to another window, typing pauses and she says so; it never types into a different window. Any
  typing outside dictation mode asks first (approval bubble showing the target window and the text).
- **Terminals**: she may type a command, but **Enter is never sent without an explicit approval** for that
  exact command, even in dictation mode. Text typed into a terminal is single-line: newlines are stripped,
  and any control/escape sequences are refused.
- **Obsidian**: through the owner-installed community plugin **Local REST API** (coddingtonbear,
  `https://127.0.0.1:27124`, self-signed cert, `Authorization: Bearer <key>`). The key lives in the desktop
  config (`~/.config/riko/obsidian.json`, mode 0600), never in the hub or chat.

## Safety rules (all backends)

1. **Target check before every keystroke batch**: re-read the focused window from DesktopSense; it must
   match the intended target (dictation pin, or the window named in the approved request). Mismatch →
   stop, no keys sent.
2. **Never type into**: password managers (the existing DesktopSense private/denylist classes: KeePassXC,
   Bitwarden, 1Password, pinentry, polkit/auth agents, SDDM/lock screens) or private browser windows.
3. **No compositor shortcuts**: refuse any combination with Super/Meta/Hyper, and the session/VT combos
   (Ctrl+Alt+Del, Ctrl+Alt+F1–F12, Ctrl+Alt+Backspace). Allowed modifiers: Ctrl, Shift, Alt, AltGr.
4. **Rate and size limits**: at most 4000 characters per call; default key delay 8 ms; one batch at a time
   (a lock); the tray's "Stop typing" item and Escape-from-hub (`stop_typing` tool) abort mid-batch.
5. **Keyboard layout**: ydotool sends evdev keycodes, i.e. it assumes a US layout. The owner uses `gb`
   (hyprland.lua `kb_layout = "gb"`). Character → keycode mapping must follow the active layout (read from
   the compositor: Hyprland `hyprctl -j devices` keyboards' active keymap / `getoption input:kb_layout`,
   KDE kxkbrc, GNOME `org.gnome.desktop.input-sources`), with gb and us tables at least (`"`↔`@`, `#`/`~`,
   `£`, `\`/`|`). Characters the layout can't produce are typed via a clipboard paste fallback
   (save clipboard → `wl-copy` → Ctrl+V, or Ctrl+Shift+V in terminals → restore clipboard).
6. **Long text** (> 200 chars, or with characters outside the layout) uses the paste fallback in
   non-terminal apps. In terminals paste is allowed only for single-line text (bracketed paste keeps it from
   running).
7. **Audit**: every typing call logs one line in the desktop terminal (`[typing] 42 chars → kate "notes.md"
   (dictation) 0.4s`) and goes to the hub tools log like other client tools. The text itself is not logged
   to the terminal.

## Tools (client tools, registered only when desktop awareness is on)

| tool | risk / default | what |
|---|---|---|
| `type_text(text, target?)` | act / ASK (ALWAYS inside dictation for the pinned window) | types at the cursor of the focused window (or `target` window ref, focused first via `focus_window`) |
| `press_keys(keys, target?)` | act / ASK | key combos like `ctrl+s`, `ctrl+shift+t`, `enter`, `backspace*3`; Enter into a terminal always ASKs with the command shown |
| `set_dictation(on, target?)` | act / ASK | start or stop dictation mode; pins the focused (or named) window |
| `dictation_status()` | read / ALWAYS | `{on, target:{app,title}, paused_reason}` |
| `stop_typing()` | act / ALWAYS | aborts the current batch |
| `obsidian_*` | see below | Local REST API |
| `kate_*` | see below | Kate |

**Dictation flow**: in dictation mode, the owner's recognised speech is typed rather than answered
(desktop-side: ASR text → `type_text` into the pinned window, without a hub turn), except for control
phrases: "stop dictating", "new line", "new paragraph", "delete that" (undo the last typed chunk with
backspaces of its length), "scratch that", "full stop/period/comma/question mark" spoken punctuation, and
"Furina, …" (the persona name as vocative) which sends the rest to her as a normal chat turn (e.g. "Furina,
fix the spelling in the last paragraph"). Speech is not spoken back; a small "dictating" pill shows by her.

### Obsidian (Local REST API)

- `obsidian_status()` read/ALWAYS: plugin reachable, vault name, active file.
- `obsidian_open(path, new_leaf?)` read/ALWAYS: `POST /open/{path}`.
- `obsidian_create(path, text)` act/ASK: `PUT /vault/{path}` (refuses to overwrite an existing note unless
  `overwrite=true`, which always ASKs).
- `obsidian_read(path?)` read/ALWAYS: `GET /vault/{path}` or `GET /active/`; markdown text, capped.
- `obsidian_append(text, path?)` act/ASK (ALWAYS in dictation targeting Obsidian): `POST /vault/{path}` or
  `POST /active/`.
- `obsidian_edit(path?, find, replace)` act/ASK: read → exact-match replace (must match exactly once, else
  error with the count) → `PUT`; the approval shows a small diff. Heading/block insertion via `PATCH` with
  `Operation`/`Target-Type` headers when asked to add under a heading.
- `obsidian_search(query)` read/ALWAYS: `POST /search/simple/?query=`.

### Kate

Kate has no document API over D-Bus, so:

- `kate_open(path, line?)` act/ALWAYS: `kate -b [-l line] path` (reuses the running instance); creating a
  new file: `kate_new(path?, text?)` act/ASK writes the file (or opens an untitled tab with `kate -b
  --new`... check the real flags with `kate --help`) and opens it.
- `kate_tabs()` read/ALWAYS: Kate windows from DesktopSense (title = active document) plus Kate's session
  file list when readable (`~/.local/share/kate/sessions/*.katesession` / `anonymous.katesession`, read-only)
  — report what's known honestly.
- `kate_edit(path, find, replace)` act/ASK: edits the file on disk (exact-match, diff in the approval);
  Kate reloads it (warn the owner if the tab has unsaved changes: the window title has `*`/"modified").
- `find_file(query, under?)` read/ALWAYS: search under the home directory (default roots: ~/Documents,
  ~/Development, ~/Cloud, ~/Desktop; skip hidden dirs, .git, node_modules, caches), name match first then
  `fd`/`find` fallback, max 30 results, never outside $HOME.
- Typing into an open Kate tab uses `type_text`/`press_keys` (e.g. Ctrl+End then type).

File writes (Kate and Obsidian create/overwrite) are limited to $HOME, refuse dotfiles/config dirs
(`~/.config`, `~/.ssh`, `~/.local`, …) unless the owner names the exact path in the approval, and keep a
`.riko-bak` copy of the previous content for one edit (most recent only).

## Owner setup (shown by the tools when ydotool is missing)

```
sudo pacman -S ydotool
# uinput access for the daemon (one-time):
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/80-uinput.rules
sudo udevadm control --reload && sudo udevadm trigger
sudo usermod -aG input $USER   # log out and in again
systemctl --user enable --now ydotool.service
```

Note for the owner: membership of `input` lets any program you run read every input device (e.g. a
keylogger). The alternative is a system (root) `ydotoold` with its socket chowned to you; Riko supports a
custom `YDOTOOL_SOCKET` path either way.

Obsidian: install community plugin "Local REST API", enable it, copy its API key into Riko's tray
"Obsidian…" dialog (or `riko-ctl obsidian-key`), which writes `~/.config/riko/obsidian.json` (0600).

## Testing rules

Tests never send real keystrokes, never contact a real ydotoold socket, Obsidian or Kate, and never touch
the owner's session (the tests/conftest.py guard must also block `ydotool`, `ydotoold`, `wl-copy`,
`wl-paste`, `kate` and connections to 127.0.0.1:27124/27123). Fake runners and a fake HTTP server only.
