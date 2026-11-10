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
   These refusals are applied by a **recursive scan that sees through wrappers and shell strings**:
   - wrapper programs are unwrapped before judging the real program: `env` (incl. `VAR=x` assignments,
     `-i`/`-u`/`-S`, and `env -S 'sudo x'`), `nice`, `ionice`, `nohup`, `timeout`, `time`, `stdbuf`,
     `chrt`, `taskset`, `setsid`, `flock`, `unbuffer`, `script -c`, `watch`, `xargs`, `busybox
     <applet>`, `command`, `exec`, `builtin`, and `find -exec/-execdir/-ok` (the *executed* program);
   - shell strings — `bash`/`sh`/`zsh`/`dash -c '...'` and the tool's own shell-syntax path — are split
     into segments on `;` `&&` `||` `|` `&` newlines plus `$( )`, backticks and `(...)` (best-effort
     lexer; when the structure cannot be parsed safely the command is **refused** as "too complex to
     check safely — run it yourself", never waved through), and the full refusal set applies to every
     segment, recursively: `ls; rm -rf ~` and `xargs sudo ls` refuse even though an approval follows;
   - absolute/relative paths to refused binaries count too (`/usr/bin/sudo ls`, `./sudo ls`).
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
  `python --version`, `node --version`, `docker ps|images|compose ls` (read verbs only),
  `findmnt` (show mounts), `checkupdates` (read-only), `systemctl --failed` /
  `systemctl --user --failed` (a failed-unit listing is read-only).

Everything else asks. Riko can't widen the allowlist herself.

Auto-run `git` must never execute **repo-configured** programs (external diff drivers and textconv
filters come from the clone's own `.git/config`, and `git status` may poll an fsmonitor hook), so the
decision injects them into the exec argv: every auto-run git gets `-c core.fsmonitor=false
-c core.hooksPath=/dev/null`, and the verbs that print patches (`diff`, `show`, `log -p`) additionally
get `--no-ext-diff --no-textconv`. An explicit `--ext-diff`/`--textconv` in the command means the owner
asked for the external driver: that asks instead of auto-running.

## Recipes

Natural-language shortcuts so "how much space is free on the root volume?" becomes `df -h /`
without her guessing flags. Defaults live in `riko/commands/recipes.py` (`DEFAULT_RECIPES`:
~30 entries covering disk/memory/CPU/GPU/processes/services/logs/network/packages/git/docker —
the owner's GPU is an AMD RX 9060 XT, so the GPU defaults are `rocm-smi` and `radeontop -d - -l 1`).
The owner overrides/extends them in `character_config.yaml` `commands.recipes`: a list of
`{ask, command, note?}` where an entry with the same `ask` replaces a default, new asks are
appended, and `recipes: false` clears the catalogue.

Honesty rule (enforced by `recipes.validate()` + a test): every recipe command either auto-runs
under the allowlist exactly as written, or carries `asks: true` (shell pipes, `<placeholder>`
templates — those pop the normal approval bubble; `templated: true` recipes must set `asks: true`).

Two surfaces: (1) the `run_command` tool description carries a compact "common asks → command"
block (≤ 900 chars, `compact_desc`); (2) a read-only client tool `command_recipes(query?)`
returns ranked matches (simple keyword scoring, `recipes.lookup`) so she can look up the right
command before running it. The hub prompt line tells her to look up recipes for any
this-PC question and explain the result in plain words. A recipe never bypasses the policy:
the looked-up command goes through `decide()` like any other command (secret paths still ask,
refusals still refuse).

## Tests

Never execute real commands in tests: the runner is injectable and `tests/conftest.py` makes the real
`subprocess` path raise for this package unless a test opts into a harmless fixture binary (e.g. a tmp
script). Cover: allowlist hits/misses (shell syntax, secret path, write flags, subcommands), refusals
(sudo, power, compositor, mkfs/dd/rm — including hidden behind wrapper programs or inside shell
strings, and via paths to the refused binary), the too-complex-to-check refusal, auto-run git argv
hardening (fsmonitor/hooks off, `--no-ext-diff --no-textconv`, `--ext-diff`/`--textconv` ask), approval
flow (exact command shown, deny → not run, hub approval alone not enough), env scrubbing, timeout kill,
output cap, redaction, cwd rules, audit line.
Recipes: lookup finds the owner's exact phrasings (root/home volume space, memory users, GPU heat),
every default recipe auto-runs or is explicitly `asks: true`, and config override/extend behaves.
