"""The director / gesture modules must stay syntactically valid and their
behaviour tests must pass under `node --test client/js/` (pure logic, no DOM).
motion.test.js additionally imports real three (devDependency in
client/package.json, same version as the page's importmap) and drives 10
simulated minutes through the animation loop to catch pose accumulation.
Skipped when node is not installed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent.parent / "client" / "js"


@pytest.mark.parametrize("script", sorted(JS.glob("*.js")), ids=lambda p: p.name)
def test_module_parses(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    check = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_director_and_gesture_node_tests_pass():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    run = subprocess.run([node, "--test", str(JS)], capture_output=True, text=True, cwd=JS.parent.parent)
    tail = "\n".join(run.stdout.splitlines()[-25:])
    assert run.returncode == 0, f"node --test failed:\n{tail}\n{run.stderr[-2000:]}"
    assert "fail 0" in run.stdout, tail
