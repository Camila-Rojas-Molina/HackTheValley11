"""Static checks on the ops scripts (no server needed)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("name", ["serve.sh", "chaos.sh"])
def test_shell_scripts_parse(name):
    r = subprocess.run(["bash", "-n", str(SCRIPTS / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_restart_loop_names_never_contain_uvicorn():
    # pkill -f uvicorn matches full command lines; the loop must not be one of them.
    for name in ("serve.sh", "run.py"):
        assert "uvicorn" not in name


def test_chaos_kills_only_the_listener():
    src = (SCRIPTS / "chaos.sh").read_text(encoding="utf-8")
    assert 'lsof -tiTCP:"$PORT" -sTCP:LISTEN' in src
    kills = [line for line in src.splitlines() if line.strip().startswith("kill ")]
    assert kills == ["  kill -9 $pids"], kills
    assert "pkill" not in src.replace("`pkill -f`", "")


def test_run_py_fixes_the_windows_event_loop():
    src = (SCRIPTS / "run.py").read_text(encoding="utf-8")
    assert "WindowsSelectorEventLoopPolicy()" in src and 'loop = "none"' in src
    assert "reload=False" in src
