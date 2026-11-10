# run_command: Riko runs commands on the owner's PC (contract)

Owner request: a safe way for her to run terminal commands on the PC and read their output, instead of a
third-party MCP terminal server (reviewed and rejected: `dillip285/mcp-terminal` runs any model string
with `shell: true`, no path checks despite its README, and would execute on eve, not on the PC). The
existing "type into a terminal + approved Enter" path (docs/TYPING.md) stays; this tool adds commands whose
**output comes back to her**.

## Shape

Desktop client tool, registered with the other desktop tools (only while desktop awareness is on):

`run_command(command: str, cwd?: str, timeout?: int)` → `{exit_code, stdout, stderr, truncated,
duration_ms, cwd, auto: bool}`

- Hub permission default **always**; the desktop decides per call: **auto-run** for allowlisted read-only
  commands, otherwise a client-side `NeedConfirmation` (the existing approval bubble) showing the **exact
  command and working directory**. A generic hub approval never substitutes for that ask, the same way
  the terminal run-token works in docs/TYPING.md.
- Lives in `riko/commands/` (new package), registered through `client_tools.register_extra`.

## Execution rules

1. **As the owner's user, never elevated.** Refuse outright, even with approval: `sudo`, `su`, `doas`,
   `pkexec`, `run0`, `machinectl shell`. Refuse anything that starts, stops or controls a
   compositor/session/power state: the `open_app` denylist (riko/desktop_sense/apps.py `block_reason`)
   plus `hyprctl`, `swaymsg`, `loginctl`, `systemctl poweroff|reboot|suspend|hibernate|halt|kexec`,
   `shutdown`, `reboot`, `poweroff`, `kill`/`pkill`/`killall` targeting compositor/session processes.
   Refuse disk destroyers: `mkfs*`, `dd` writing to `/dev/*`, `wipefs`, `shred` on devices, `rm` with
   `-r`/`-f` on `/`, `~`, or any path outside `$HOME` and `/tmp`.
2. **No shell by default.** Parse with `shlex`, run with an argv (`subprocess.run(argv, shell=False)`).
   Commands that need a shell (pipes `|`, `;`, `&&`, `||`, redirects `> >> <`, `$(...)`, backticks,
   globs) are allowed **only with approval** and then run via `bash -c` with the exact string shown in the
   approval. Auto-run never uses a shell.
3. **Environment scrubbed.** Child env = the user's env minus secrets: anything matching
   `*_KEY`, `*_TOKEN`, `*_SECRET`, `*PASSWORD*`, `RIKO_*`, `ASR_*`, `LLM_*`, `FISH_*`, `GROQ_*`,
   `OPENAI_*`, `ANTHROPIC_*`, `HF_*`, `GITHUB_TOKEN`, `GH_TOKEN`, `AWS_*`, `SSH_AUTH_SOCK`,
   `GPG_AGENT_INFO`, `DBUS_SESSION_BUS_ADDRESS` is removed (the desktop process loads `.env` and the
   hub key lives in it). `LANG=C.UTF-8`, `TERM=dumb`, `NO_COLOR=1`, `PAGER=cat`, `GIT_PAGER=cat`,
   `SYSTEMD_PAGER=`.
4. **No interaction.** stdin is `/dev/null`, no TTY. Known interactive/full-screen programs (`vim`, `nano`,
   `less`, `more`, `top`/`htop`/`btop` without batch flags, `ssh` without a command, `python`/`node` REPL
   with no script, `man`) are refused with a hint (`top -b -n1`, `cat`).
5. **Limits.** Timeout default 20 s, max 120 s (process group killed on expiry; result says it timed out).
   Output capped at 8,000 chars per stream (head + tail with `…[truncated]`), and `truncated: true`.
6. **Working directory.** Default `$HOME`. Must exist. Outside `$HOME`/`/tmp` → approval required.
7. **Secret paths.** Never auto-run anything whose arguments touch: `~/.ssh`, `~/.gnupg`,
   `~/.password-store`, `~/.local/share/keyrings`, `~/.mozilla` and other browser profiles, `~/.config/riko`,
   any `.env` file, `~/.netrc`, `~/.git-credentials`, `~/.aws`, `~/.kube`, `~/.docker/config.json`,
   `/etc/shadow`. With approval the bubble warns "this may expose secrets to the model".
8. **Output redaction.** Before returning, mask long secret-looking tokens (`sk-…`, `ghp_…`, `hf_…`,
   `xox[bp]-…`, JWT `eyJ…\.…\.…`, `-----BEGIN … PRIVATE KEY-----` blocks, `Authorization: Bearer …`)
   as `[redacted]`.
9. **Audit.** One desktop terminal line per call: `[cmd] auto|approved|denied|refused <command> (cwd)
   exit=N 0.3s`. The command text IS logged (unlike typing), the output is not. The hub tools log records
   it like any client tool.
10. **One at a time** (a lock); the tray's "Stop typing" item also cancels a running command
    (renamed "Stop typing / command").

## Auto-run allowlist (read-only; no approval)

Configurable in `character_config.yaml` `commands.auto_allow` (and `commands.enabled`, default true).
A command auto-runs only if it is a single argv (no shell syntax), its program and subcommand match a
rule, no argument hits a secret path, and no argument is an output/write flag listed for that program.
Defaults:

- `df`, `free`, `uptime`, `whoami`, `id`, `pwd`, `date`, `uname`, `hostname`, `nproc`, `lscpu`, `lsblk`,
  `lsusb`, `lspci`, `sensors`, `ip addr|link|route` (show only), `ss -tuln[p]`, `ps` (any read flags),
  `which`, `type`, `file`, `stat`, `du` (with `-s`/`-h`/`-d N`, path under `$HOME`), `ls`, `tree -L ≤3`,
  `cat`/`head`/`tail`/`wc`/`grep`/`rg` on regular files ≤ 1 MB under `$HOME` or `/etc` (not secret
  paths), `find` without `-exec`/`-delete`/`-fprint*`/`-ok`, `journalctl` with `--user` or `-n N`
  (N ≤ 500) and no `--vacuum*`/`--rotate`/`--flush`, `systemctl --user status|list-units|list-timers|
  is-active|is-enabled`, `systemctl status|list-units|is-active` (system, read-only verbs),
  `git status|log|diff|show|branch|remote -v` (no `-c` config overrides, no `--output`), `pacman -Q*` and
  `pacman -Ss|-Si`, `flatpak list`, `nvidia-smi` / `rocm-smi` / `radeontop -d - -l 1`, `uv --version`,
  `python --version`, `node --version`, `docker ps|images|compose ls` (read verbs only).

Everything else asks. Riko can't widen the allowlist herself.

## Tests

Never execute real commands in tests: the runner is injectable and `tests/conftest.py` makes the real
`subprocess` path raise for this package unless a test opts into a harmless fixture binary (e.g. a tmp
script). Cover: allowlist hits/misses (shell syntax, secret path, write flags, subcommands), refusals
(sudo, power, compositor, mkfs/dd/rm), approval flow (exact command shown, deny → not run, hub
approval alone not enough), env scrubbing, timeout kill, output cap, redaction, cwd rules, audit line.
