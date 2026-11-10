"""Policy tests for run_command (docs/COMMANDS.md 'Execution rules' +
'Auto-run allowlist'). Pure decide() calls — nothing is executed, ever."""
import os

import pytest

from riko.commands import policy


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "notes").mkdir(parents=True)
    (h / "notes" / "a.txt").write_text("hello\n")
    (h / ".env").write_text("X=1\n")
    (h / ".ssh").mkdir()
    (h / ".ssh" / "id_ed25519").write_text("KEY\n")
    big = h / "notes" / "big.bin"
    big.write_bytes(b"x" * (policy._READ_FILE_MAX + 1))
    return str(h)


def D(cmd, home, cwd=None, **kw):
    return policy.decide(cmd, cwd=cwd, home=home, **kw)


# ── allowlist hits ─────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "df -h", "free -m", "uptime", "whoami", "id", "pwd", "date",
    "uname -a", "hostname", "nproc", "lscpu", "lsblk", "lsusb", "lspci",
    "sensors", "which ls", "file notes/a.txt", "stat notes/a.txt",
    "ps aux", "ls -la",
    "ip addr", "ip -s link", "ip route",
    "ss -tuln", "ss -tulpn",
    "du -s notes", "du -sh ~/notes", "du -d 2 notes",
    "tree -L 2 notes",
    "cat notes/a.txt", "head -n 5 notes/a.txt", "tail -c 10 notes/a.txt",
    "wc -l notes/a.txt", "grep hello notes/a.txt", "grep 'a|b' notes/a.txt",
    "find notes -name '*.txt'", "find notes -name x -maxdepth 1",
    "journalctl --user -n 20", "journalctl -n 500", "journalctl -n5",
    "systemctl --user status", "systemctl --user list-units",
    "systemctl --user is-enabled foo", "systemctl status sshd",
    "systemctl list-units", "systemctl is-active foo",
    "git status", "git log --oneline -5", "git diff", "git show HEAD",
    "git branch -v", "git remote -v", "git --no-pager log",
    "pacman -Q", "pacman -Qe", "pacman -Ss firefox", "pacman -Si vim",
    "flatpak list --app",
    "nvidia-smi", "rocm-smi", "radeontop -d - -l 1",
    "uv --version", "python --version", "python3 --version",
    "node --version", "docker ps", "docker images", "docker compose ls",
])
def test_auto(cmd, home):
    d = D(cmd, home)
    assert d.mode == "auto", f"{cmd}: {d.mode} ({d.reason})"
    assert not d.shell
    assert d.argv[0] in cmd


# ── allowlist misses → ask ─────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "touch new", "echo hi", "rm file.txt", "cp a b", "mkdir x",
    "ip neigh", "ss -a", "tree -L 9", "tree notes",
    "du -x /", "find notes -exec rm {} ;", "find notes -delete",
    "find notes -ok cat {} ;", "find notes -fprint out",
    "journalctl", "journalctl --vacuum-size=1M", "journalctl --flush",
    "journalctl -n 5000", "systemctl restart foo", "systemctl enable foo",
    "systemctl --user stop foo",
    "git -c user.name=x status", "git checkout main", "git push",
    "git status --output=out", "git -C /elsewhere status",
    "pacman -S firefox", "flatpak install x", "docker run ubuntu",
    "uv sync", "python script.py", "cat",            # stdin
    "cat nope.txt", "cat notes",                     # missing / directory
    "cat notes/big.bin",                             # > 1 MB
    "nvidia-smi -r", "radeontop -d -",
    "top -b -n 1",                      # batch mode: not refused, not listed
    "mkswap /dev/sdb2",                  # writes a signature: ask (not auto)
])
def test_ask(cmd, home):
    assert D(cmd, home).mode == "ask"


# ── shell syntax forces approval + bash -c ────────────────────
@pytest.mark.parametrize("cmd", [
    "ls | grep txt", "ls ; pwd", "true && ls", "false || ls",
    "echo hi > out.txt", "sort < in.txt", "echo $(pwd)", "echo `date`",
    "ls notes/*.txt", "wc -l notes/*.txt",
])
def test_shell_asks_and_uses_bash(cmd, home):
    d = D(cmd, home)
    assert d.mode == "ask"
    assert d.shell and d.argv == ["bash", "-c", cmd]


def test_quoted_metachars_are_not_shell(home):
    d = D("grep 'a|b>$' notes/a.txt", home)
    assert not d.shell and d.mode == "auto"


# ── hard refusals (even with approval) ─────────────────────────
@pytest.mark.parametrize("cmd,frag", [
    ("sudo ls", "sudo"), ("su -", "su"), ("doas ls", "doas"),
    ("pkexec whoami", "pkexec"), ("run0 ls", "run0"),
    ("machinectl shell", "elevated"),
    ("ls | sudo rm -rf x", "sudo"),               # inside a pipeline too
])
def test_never_elevated(cmd, frag, home):
    d = D(cmd, home)
    assert d.mode == "refuse" and frag in d.reason


@pytest.mark.parametrize("cmd", [
    "hyprctl monitors", "swaymsg -t get_tree", "loginctl unlock-session",
    "systemctl poweroff", "systemctl --user suspend", "systemctl reboot",
    "systemctl kexec", "shutdown -h now", "reboot", "poweroff", "halt",
    "Hyprland", "sway", "Xwayland", "gnome-shell",
    "killall Hyprland", "pkill sway", "kill -9 hyprland",
    "kill $(pgrep plasmashell)",
])
def test_session_power_refused(cmd, home):
    d = D(cmd, home)
    assert d.mode == "refuse", f"{cmd}: {d.mode}"


@pytest.mark.parametrize("cmd", [
    "mkfs.ext4 /dev/sda1", "wipefs /dev/sda",
    "dd if=/dev/zero of=/dev/sda bs=1M", "shred /dev/sda",
    "rm -rf /etc/nginx", "rm -rf /var", "rm -rf ~", "rm -rf $HOME",
    "rm -fr /", "rm -r /home",
])
def test_destroyers_refused(cmd, home):
    d = D(cmd, home)
    assert d.mode == "refuse", f"{cmd}: {d.mode}"


def test_rm_recursive_inside_home_only_asks(home):
    assert D("rm -rf ~/notes/tmp", home).mode == "ask"
    assert D("rm -rf /tmp/scratch", home).mode == "ask"
    assert D("rm notes/a.txt", home).mode == "ask"     # plain rm: ask


# ── refusals see THROUGH wrapper programs and shell strings ─────
# (rule 1: the refusals hold EVEN WITH approval, so a wrapper or a
#  shell string must never downgrade them to ask)
@pytest.mark.parametrize("cmd", [
    # wrapper programs: the real program is judged
    "env sudo ls", "env VAR=x sudo ls", "env -i PATH=/bin sudo ls",
    "env -u DISPLAY sudo ls", "env -S 'sudo x'", "nice sudo ls",
    "nice -n 5 sudo ls", "nice -5 sudo ls", "ionice -c3 sudo ls",
    "nohup sudo ls", "timeout 5 sudo ls", "timeout -k 2 5 sudo ls",
    "time sudo ls", "stdbuf -oL sudo ls", "chrt -f 1 sudo ls",
    "taskset -c 0 sudo ls", "setsid sudo ls", "flock /tmp/l sudo ls",
    "flock -c 'sudo ls' /tmp/l", "unbuffer sudo ls",
    "watch -n 5 sudo ls", "script -c 'sudo x'", "busybox sudo ls",
    "command sudo ls", "command -v sudo", "exec sudo ls",
    "builtin sudo ls", "env nice sudo ls", "sudo sudo ls",
    "doas -n sudo ls",
    # find -exec/-ok judge the EXECUTED program
    "find notes -exec sudo ls ;", "find notes -exec rm -rf / ;",
    "find notes -execdir rm -rf /etc ;", "find notes -ok rm -rf / ;",
    # absolute / relative paths to refused binaries
    "/usr/bin/sudo ls", "./sudo ls", "../bin/su root",
    # wrappers hiding destroyers / power / session kills
    "timeout 5 rm -rf /etc", "xargs -a l.txt sudo rm -rf /",
    "nice rm -rf /var", "env FOO=1 systemctl poweroff",
    # shell strings: every segment is scanned, recursively
    "ls; rm -rf ~", "ls && rm -rf ~", "ls || rm -rf ~",
    "ls ; sudo rm -rf x", "bash -c 'sudo ls'", 'bash -c "sudo ls"',
    "sh -c 'ls; rm -rf ~'", "bash -lc 'sudo x'", "zsh -c 'x && sudo y'",
    "bash -c 'ls | sudo rm -rf /'", "bash -c 'kill $(pgrep sway)'",
    "echo hi && sudo ls", "true; pkill sway", "ls || kill -9 Hyprland",
    "(sudo ls)", "echo $(sudo ls)", "echo `sudo ls`",
    "diff <(sudo ls) <(ls)", "ls && sudo reboot", "ls; loginctl lock",
    "ls | mkfs.ext4 /dev/sda", "true && env sudo ls",
    # wrappers with flags we cannot interpret: word-scan safety net
    "timeout --weird sudo ls", "xargs --mystery sudo ls",
])
def test_refusals_see_through_wrappers_and_shell(cmd, home):
    d = D(cmd, home)
    assert d.mode == "refuse", f"{cmd}: {d.mode} ({d.reason})"


@pytest.mark.parametrize("cmd", [
    # clean payloads behind wrappers/shell strings: still ask, not refuse
    "env FOO=1 ls", "bash -c 'ls | grep x'", "sh -c 'ls; pwd'",
    "timeout 5 make build", "xargs -a list.txt ls", "watch ls",
    "nohup make", "find notes -exec cat {} ;", "busybox ls",
    "script -c 'make'", "ls; rm -rf ~/notes/tmp", "nice -n 5 df",
    # repo-configured external programs: ask, never auto
    "git diff --ext-diff", "git diff --textconv", "git show --ext-diff",
    "git log -p --textconv",
    # structures the best-effort lexer cannot check safely: refuse
    "cat <<EOF", "echo $(unbalanced", "echo `unbalanced", "(( i=0 ))",
])
def test_wrapper_and_shell_ask_and_uncertain_home(cmd, home):
    mode = D(cmd, home).mode
    if cmd in ("cat <<EOF", "echo $(unbalanced", "echo `unbalanced",
               "(( i=0 ))"):
        assert mode == "refuse", f"{cmd}: {mode}"
        assert "too complex to check safely" in D(cmd, home).reason
    else:
        assert mode == "ask", f"{cmd}: {mode} " \
            f"({D(cmd, home).reason})"


# ── auto-run git never executes repo-configured programs ───────
def test_git_autorun_is_hardened(home):
    cfg = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    safe = ["--no-ext-diff", "--no-textconv"]
    d = D("git diff", home)
    assert d.mode == "auto"
    assert d.argv == ["git"] + cfg + ["diff"] + safe
    d = D("git show HEAD", home)
    assert d.mode == "auto"
    assert d.argv == ["git"] + cfg + ["show", "HEAD"] + safe
    d = D("git log -p --oneline", home)
    assert d.mode == "auto"
    assert d.argv == ["git"] + cfg + ["log", "-p", "--oneline"] + safe
    # no patch output -> no external diff runs, but fsmonitor/hooks stay off
    for cmd, sub in (("git log --oneline -5", ["log", "--oneline", "-5"]),
                     ("git status", ["status"]),
                     ("git --no-pager log", ["--no-pager", "log"]),
                     ("git branch -v", ["branch", "-v"])):
        d = D(cmd, home)
        assert d.mode == "auto", f"{cmd}: {d.reason}"
        assert d.argv == ["git"] + cfg + sub, d.argv
    # owner-widened git entries get the same hardening
    d = D("git stash list", home, extra_auto=["git stash"])
    assert d.mode == "auto" and d.argv[:2] == ["git", "-c"]


@pytest.mark.parametrize("cmd,hint", [
    ("vim notes/a.txt", "cat"), ("nano x", "cat"), ("less x", "cat"),
    ("top", "top -b -n1"), ("htop", "top"), ("man ls", ""),
    ("ssh", "interactive"), ("python", "REPL"), ("node", "REPL"),
])
def test_interactive_refused_with_hint(cmd, hint, home):
    d = D(cmd, home)
    assert d.mode == "refuse", f"{cmd}: {d.mode}"
    assert hint in d.reason


def test_batch_modes_not_interactive(home):
    """Batch flags mean a real one-shot: asked at worst, never refused."""
    assert D("top -b -n1", home).mode == "ask"
    assert D("python --version", home).mode == "auto"
    assert D("python -c 'print(1)'", home).mode == "ask"
    assert D("ssh box uptime", home).mode == "ask"


# ── secret paths: ask, never auto ──────────────────────────────
@pytest.mark.parametrize("cmd", [
    "cat ~/.ssh/id_ed25519", "cat $HOME/.ssh/id_ed25519", "ls ~/.ssh",
    "cat ~/.env", "cat .env", "cat ~/.env.local", "grep x ~/.aws/config",
    "cat ~/.config/riko/client_id", "ls ~/.gnupg", "cat /etc/shadow",
    "cat ~/.docker/config.json", "cat ~/.netrc", "ls ~/.mozilla",
])
def test_secret_paths_ask(cmd, home):
    d = D(cmd, home)
    assert d.mode == "ask", f"{cmd}: {d.mode} ({d.reason})"
    assert d.secret_touch is True


def test_secret_cwd_asks(home):
    d = D("ls", home, cwd=os.path.join(home, ".ssh"))
    assert d.mode == "ask" and d.secret_touch


# ── cwd rules ──────────────────────────────────────────────────
def test_cwd_rules(home):
    assert D("ls", home).cwd == os.path.expanduser(home)     # default $HOME
    assert D("ls", home, cwd="/etc").mode == "ask"           # outside
    assert D("ls", home, cwd="/tmp").mode == "auto"
    assert D("ls", home, cwd="/var/tmp").mode == "ask"
    assert D("ls", home, cwd="/nonexistent-dir-riko").mode == "ask"
    assert D("ls", home, cwd="/nonexistent-dir-riko").exists is False


# ── timeouts ───────────────────────────────────────────────────
def test_timeout_rules(home):
    assert D("df", home).timeout == policy.DEFAULT_TIMEOUT
    assert D("df", home, timeout=5).timeout == 5.0
    assert D("df", home, timeout=9999).timeout == policy.MAX_TIMEOUT
    assert D("df", home, timeout="x").mode == "refuse"


# ── owner-widened auto_allow (character_config commands.auto_allow) ──
def test_extra_auto(home):
    assert D("cowsay hi", home).mode == "ask"
    assert D("cowsay hi", home, extra_auto=["cowsay"]).mode == "auto"
    assert D("git stash list", home,
             extra_auto=["git stash"]).mode == "auto"
    # still guarded: secrets + shell + refusals outrank the list
    assert D("cowsay ~/.ssh/x", home,
             extra_auto=["cowsay"]).mode == "ask"
    assert D("cowsay hi | cat", home, extra_auto=["cowsay"]).mode == "ask"
    assert D("sudo cowsay", home, extra_auto=["cowsay"]).mode == "refuse"


def test_garbage_refused(home):
    assert D("", home).mode == "refuse"
    assert D("echo 'unbalanced", home).mode == "refuse"
