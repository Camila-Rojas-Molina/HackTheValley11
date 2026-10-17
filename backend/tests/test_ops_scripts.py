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


def test_simulate_runs_the_pcc_seed_after_the_kaze_scenarios():
    """APPROVED_PLAN: simulate.py runs all 7 KAZE scenarios plus a PCC seed."""
    import httpx

    from scripts import simulate
    calls = []

    def server(flagged_cards: int, scripted_flagged: int):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path, request.headers.get("x-admin-key")))
            if request.url.path == "/v1/demo/care-seed":
                return httpx.Response(200, json={
                    "ok": True, "synthetic": True, "residents": 20,
                    "scripted_change": {"residents": 7, "flagged": scripted_flagged},
                    "no_change_scripts": {"residents": 13, "flagged": 0}})
            if request.url.path == "/v1/care/alerts":
                levels = ["check today"] * flagged_cards
                return httpx.Response(200, json={"synthetic": True, "alerts": [
                    {"resident_id": f"R-{i:02d}", "level": lv, "score": 50, "synthetic": True}
                    for i, lv in enumerate(levels)]})
            return httpx.Response(404)
        return handler

    srv = simulate.Server("http://sim.test", "adm", "wk")
    srv.c = httpx.Client(base_url="http://sim.test", transport=httpx.MockTransport(server(7, 7)))
    note = simulate.pcc_seed(srv)
    assert ("POST", "/v1/demo/care-seed", "adm") in calls and ("GET", "/v1/care/alerts", None) in calls
    assert "7" in note
    srv.c = httpx.Client(base_url="http://sim.test", transport=httpx.MockTransport(server(6, 6)))
    with pytest.raises(simulate.Fail):
        simulate.pcc_seed(srv)
    assert "pcc" in [s[0] for s in simulate.STEPS]
