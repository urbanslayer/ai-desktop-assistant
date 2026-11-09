"""find_file + write-safety helpers: tmp trees only, never $HOME, no shell."""
import pytest

from riko.appedit import files
from riko.client_tools import ToolError


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    tree = {
        "Documents/notes.md": "n",
        "Documents/sub/notes_extra.txt": "n",
        "Desktop/Notes.MD": "n",
        "Development/riko_project/README.md": "n",
        "Development/node_modules/notes.md": "skip-me",
        "Development/.git/hooks/notes.md": "skip-me",
        "Development/cache/notes.md": "skip-me",
        "Cloud/photo.png": "n",
        ".hidden/notes.md": "skip-me",
        "Documents/.dotfile.md": "skip-me",
    }
    for rel, content in tree.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def _paths(out):
    return [r["path"] for r in out["results"]]


def test_ranking_exact_caseless_prefix_substring(home):
    out = files.find_file("notes", home=home)
    paths = _paths(out)
    assert paths[0] == str(home / "Documents" / "notes.md")       # exact stem
    assert paths[1] == str(home / "Desktop" / "Notes.MD")         # caseless exact
    assert paths[2] == str(home / "Documents" / "sub" / "notes_extra.txt")
    skipped = {"node_modules", ".git", "cache", ".hidden"}
    rel = [p[len(str(home)) + 1:].split("/") for p in paths]
    assert all(not skipped.intersection(parts) for parts in rel)
    assert not any(p.endswith(".dotfile.md") for p in paths)      # hidden file


def test_path_match_only_fills_leftovers(home):
    out = files.find_file("photo", home=home)
    assert _paths(out) == [str(home / "Cloud" / "photo.png")]


def test_under_narrows_and_stays_in_home(home):
    out = files.find_file("notes", under="Development", home=home)
    assert out["results"] == []          # everything under it is skipped-dir
    out2 = files.find_file("README", under="Development/riko_project",
                           home=home)
    assert _paths(out2) == [str(home / "Development" / "riko_project" / "README.md")]
    with pytest.raises(ToolError, match="outside your home"):
        files.find_file("x", under="/etc", home=home)
    with pytest.raises(ToolError, match="no such directory"):
        files.find_file("x", under="nowhere", home=home)


def test_limit_max_30(home):
    big = home / "Documents" / "big"
    big.mkdir(parents=True)
    for i in range(40):
        (big / f"hit_{i}.txt").write_text("x")
    out = files.find_file("hit_3", home=home)   # matches hit_3, hit_30..39
    assert len(out["results"]) <= 30
    assert out["results"][0]["path"].endswith("hit_3.txt")
    out_all = files.find_file("hit", home=home)  # substring of all 40 names
    assert len(out_all["results"]) == 30


def test_empty_query(home):
    with pytest.raises(ToolError, match="name or phrase"):
        files.find_file("  ", home=home)


# ── safe_target / writes ───────────────────────────────────────
def test_safe_target_relative_under_home(home):
    t = files.safe_target("Documents/x.md", home=home)
    assert t == home / "Documents" / "x.md"


def test_safe_target_dot_traversal_stays_home(home):
    t = files.safe_target("Documents/../Documents/x.md", home=home)
    assert t == home / "Documents" / "x.md"


def test_safe_target_outside_home_and_symlink_escape(home, tmp_path):
    with pytest.raises(ToolError, match="outside your home"):
        files.safe_target("/etc/hosts", home=home)
    outside = tmp_path.parent / "outside-target.txt"
    outside.write_text("x")
    link = home / "escape"
    link.symlink_to(outside)
    with pytest.raises(ToolError, match="resolves outside"):
        files.safe_target("escape", home=home)


def test_safe_target_dotdir_rule(home):
    (home / ".config").mkdir()
    with pytest.raises(ToolError, match="hidden/config directory"):
        files.safe_target(".config/app/conf", home=home)
    # a dot FILE in a normal dir is allowed (only dot dirs are config dirs)
    t = files.safe_target(".profile", home=home)
    assert t == home / ".profile"


def test_write_with_backup_single_slot(home):
    f = home / "a.txt"
    assert files.write_with_backup(f, "one") is None
    bak = files.write_with_backup(f, "two")
    assert bak.read_text() == "one"
    bak2 = files.write_with_backup(f, "three")
    assert bak2 == bak and bak.read_text() == "two"
    assert f.read_text() == "three"
    assert list(home.glob("*.riko-*")) == [bak]   # no second backup file


def test_apply_edit_once_counts(home):
    assert files.apply_edit_once("a b a", "b", "c") == "a c a"
    with pytest.raises(ToolError, match="2 matches"):
        files.apply_edit_once("a b a", "a", "c")
    with pytest.raises(ToolError, match="0 matches"):
        files.apply_edit_once("a", "z", "c")
    with pytest.raises(ToolError, match="empty"):
        files.apply_edit_once("a", "", "c")


def test_compact_diff_bounded():
    old = "\n".join(f"line {i}" for i in range(100))
    new = old.replace("line 50", "CHANGED")
    d = files.compact_diff(old, new)
    assert "-line 50" in d and "+CHANGED" in d
    assert files.compact_diff("same", "same") == "(no text change)"
