"""The avatar page's module script must stay syntactically valid (node --check).

The script uses imports and top-level await, so it is checked as an ES module
(.mjs). Skipped when node is not installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "client" / "index.html"


def test_client_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = PAGE.read_text(encoding="utf-8")
    scripts = re.findall(r'<script type="module">(.*?)</script>', html, re.S)
    assert len(scripts) == 1
    script = tmp_path / "page.mjs"
    script.write_text(scripts[0], encoding="utf-8")
    check = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
