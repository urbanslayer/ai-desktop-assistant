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
