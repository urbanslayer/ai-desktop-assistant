"""Kate tools: fake runners only — never a real kate, never the owner's
session (docs/TYPING.md testing rules)."""
import subprocess

import pytest

from riko.appedit import kate
from riko.client_tools import NeedConfirmation, ToolError


class Recorder:
    def __init__(self, rc=0, stderr=""):
        self.argvs = []
        self.rc = rc
        self.stderr = stderr

    def __call__(self, argv):
        self.argvs.append([str(a) for a in argv])
        return subprocess.CompletedProcess(argv, self.rc, stdout="",
                                          stderr=self.stderr)


class FakeSense:
    def __init__(self, windows):
        self._windows = windows

    def desktop_windows(self):
        return {"backend": "fake", "windows": self._windows}


class BoomSense:
    def desktop_windows(self):
        raise ToolError("no supported window manager found")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))   # home boundary -> tmp tree
    (tmp_path / "Documents").mkdir()
    return tmp_path


# ── kate_open: real flags, fake runner ─────────────────────────
def test_open_reuses_instance_with_line_flag_no_blocking(home):
    f = home / "Documents" / "notes.md"
    f.write_text("hi\n")
    r = Recorder()
    out = kate.kate_open(str(f), line=42, runner=r, home=home)
    assert "open" in out
    assert r.argvs == [["kate", "-l", "42", str(f)]]   # no -b: it blocks


def test_open_missing_file_points_at_kate_new(home):
    with pytest.raises(ToolError, match="kate_new"):
        kate.kate_open(str(home / "gone.md"), runner=Recorder(), home=home)


def test_open_outside_home_refused(home):
    with pytest.raises(ToolError, match="outside your home"):
        kate.kate_open("/etc/passwd", runner=Recorder(), home=home)


def test_open_reports_runner_failure(home):
    f = home / "a.txt"
    f.write_text("x")
    with pytest.raises(ToolError, match="kate failed"):
        kate.kate_open(str(f), runner=Recorder(rc=3, stderr="boom"), home=home)


# ── kate_new ───────────────────────────────────────────────────
def test_new_writes_then_opens(home):
    r = Recorder()
    out = kate.kate_new(path="Documents/new.md", text="hello",
                        runner=r, home=home)
    assert (home / "Documents" / "new.md").read_text() == "hello"
    assert r.argvs[-1] == ["kate", str(home / "Documents" / "new.md")]
    assert "wrote" in out


def test_new_refuses_existing_content_without_overwrite(home):
    f = home / "Documents" / "x.md"
    f.write_text("old stuff")
    with pytest.raises(ToolError, match="overwrite=true"):
        kate.kate_new(path="Documents/x.md", text="mine", runner=Recorder(),
                      home=home)
    assert f.read_text() == "old stuff"


def test_new_overwrite_asks_with_diff_then_writes(home):
    f = home / "Documents" / "x.md"
    f.write_text("old stuff\n")
    with pytest.raises(NeedConfirmation) as ei:
        kate.kate_new(path="Documents/x.md", text="mine\n",
                      overwrite=True, runner=Recorder(), home=home)
    assert "-old stuff" in ei.value.reason and "+mine" in ei.value.reason
    kate.kate_new(path="Documents/x.md", text="mine\n", overwrite=True,
                  runner=Recorder(), home=home, confirmed=True)
    assert f.read_text() == "mine\n"


def test_new_untitled_tab_uses_tempfile_flag(home):
    r = Recorder()
    kate.kate_new(runner=r, home=home)
    assert r.argvs == [["kate", "--tempfile"]]
    r2 = Recorder()
    out = kate.kate_new(text="scratch text", runner=r2, home=home)
    assert r2.argvs[0][1] == "--tempfile"
    opened = r2.argvs[0][-1]
    assert open(opened).read() == "scratch text"
    assert "untitled" in out


# ── kate_tabs ──────────────────────────────────────────────────
def test_tabs_combines_windows_and_session_files(home):
    sess_dir = home / ".local" / "share" / "kate" / "sessions"
    sess_dir.mkdir(parents=True)
    (sess_dir / "Main.katesession").write_text(
        "[Document 0]\nURL=file:///home/user/Cloud/notes%20one.md\n\n"
        "[Document 1]\nURL=\n\n"
        "[General]\nOpenOnStartup=false\n")
    sense = FakeSense([
        {"app": "org.kde.kate", "title": "notes.md — Kate", "focused": True},
        {"app": "firefox", "title": "web"},
    ])
    out = kate.kate_tabs(sense=sense, sessions_dir=sess_dir, home=home)
    assert out["windows"] == [{"title": "notes.md — Kate",
                               "unsaved": False, "focused": True}]
    assert out["sessions"] == [{"session": "Main.katesession",
                                "documents": ["/home/user/Cloud/notes one.md"],
                                "untitled_documents": 1}]


def test_tabs_unsaved_marker_and_no_sense_is_honest(home):
    sense = FakeSense([{"app": "org.kde.kate", "title": "*draft.md — Kate"}])
    out = kate.kate_tabs(sense=sense, sessions_dir=home / "none", home=home)
    assert out["windows"][0]["unsaved"] is True
    assert out["sessions_note"].startswith("no session dir")
    out2 = kate.kate_tabs(sense=None, sessions_dir=home / "none", home=home)
    assert "awareness is off" in out2["windows_note"]


def test_tabs_sense_backend_error_is_reported_not_raised(home):
    out = kate.kate_tabs(sense=BoomSense(), sessions_dir=home / "none",
                         home=home)
    assert "window manager" in out["windows_note"]


# ── kate_edit: diff approval, once-only, backup, dot-paths ─────
def test_edit_confirmation_diff_then_backup(home):
    f = home / "Documents" / "a.md"
    old = "alpha\nbeta\ngamma\n"
    f.write_text(old)
    with pytest.raises(NeedConfirmation) as ei:
        kate.kate_edit(path="Documents/a.md", find="beta", replace="BETA",
                       home=home)
    assert "-beta" in ei.value.reason and "+BETA" in ei.value.reason
    assert str(f) in ei.value.reason
    out = kate.kate_edit(path="Documents/a.md", find="beta", replace="BETA",
                         home=home, confirmed=True)
    assert f.read_text() == "alpha\nBETA\ngamma\n"
    assert ".riko-bak" in out
    assert (home / "Documents" / "a.md.riko-bak").read_text() == old


def test_edit_backup_keeps_most_recent_only(home):
    f = home / "Documents" / "a.md"
    f.write_text("one\n")
    kate.kate_edit(path="Documents/a.md", find="one", replace="two",
                   home=home, confirmed=True)
    kate.kate_edit(path="Documents/a.md", find="two", replace="three",
                   home=home, confirmed=True)
    baks = list((home / "Documents").glob("*.riko-bak*"))
    assert [b.name for b in baks] == ["a.md.riko-bak"]
    assert baks[0].read_text() == "two\n"


def test_edit_match_count_errors(home):
    f = home / "a.md"
    f.write_text("dup dup solo")
    with pytest.raises(ToolError, match="2 matches"):
        kate.kate_edit(path="a.md", find="dup", replace="x", home=home)
    with pytest.raises(ToolError, match="0 matches"):
        kate.kate_edit(path="a.md", find="nope", replace="x", home=home)
    assert f.read_text() == "dup dup solo"


def test_edit_requires_existing_file_and_home(home):
    with pytest.raises(ToolError, match="no such file"):
        kate.kate_edit(path="gone.md", find="a", replace="b", home=home)
    with pytest.raises(ToolError, match="outside your home"):
        kate.kate_edit(path="/etc/hosts", find="a", replace="b", home=home)


def test_edit_refuses_dot_dirs_unless_exact_approval(home):
    d = home / ".config" / "riko"
    d.mkdir(parents=True)
    (d / "thing.conf").write_text("old\n")
    with pytest.raises(ToolError, match="hidden/config directory"):
        kate.kate_edit(path=".config/riko/thing.conf", find="old",
                       replace="new", home=home)
    kate.kate_edit(path=".config/riko/thing.conf", find="old", replace="new",
                   home=home, allow_dot=True, confirmed=True)
    assert (d / "thing.conf").read_text() == "new\n"


def test_edit_warns_about_unsaved_tab(home):
    f = home / "draft.md"
    f.write_text("body\n")
    sense = FakeSense([{"app": "org.kde.kate", "title": "*draft.md — Kate"}])
    with pytest.raises(NeedConfirmation) as ei:
        kate.kate_edit(path="draft.md", find="body", replace="Body",
                       home=home, sense=sense)
    assert "UNSAVED" in ei.value.reason
    out = kate.kate_edit(path="draft.md", find="body", replace="Body",
                         home=home, sense=sense, confirmed=True)
    assert "unsaved changes" in out
