"""scripts/dev.sh for real: a TimescaleDB container and the API from this tree, in a private state dir on free
ports, so a run never touches this worktree's own dev servers (suite `dev` in suites/s0.toml; it needs Docker).

The same path as the M2 DONE CHECK: start -> health 200 with the DB -> stop-db -> 503 -> stop leaves nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEV = REPO / "scripts" / "dev.sh"
DOCKER = os.environ.get("DOCKER") or str(Path.home() / ".docker" / "bin" / "docker")
VENV_PY = REPO / ".venv" / "bin" / "python"


def _docker_up() -> bool:
    try:
        return subprocess.run([DOCKER, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _api_deps() -> bool:
    return VENV_PY.exists() and subprocess.run(
        [str(VENV_PY), "-c", "import fastapi, uvicorn, psycopg, psycopg_pool"], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.skipif(not _docker_up(), reason="Docker is not running"),
    pytest.mark.skipif(not _api_deps(), reason="the API's packages are not in .venv (scripts/bootstrap.sh)"),
]


def get(url: str, timeout: float = 30) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def env(tmp_path):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    e = {**os.environ, "RB_DEV_STATE": str(tmp_path / "state"), "RB_API_PORT": str(port), "DOCKER": DOCKER}
    yield e
    subprocess.run(["bash", str(DEV), "stop"], cwd=REPO, env=e, capture_output=True, timeout=120)


def dev(env, *args):
    return subprocess.run(["bash", str(DEV), *args], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=300)


def test_api_on_a_throwaway_timescaledb_then_stop_db_then_stop(env):
    r = dev(env, "api")
    assert r.returncode == 0, r.stdout + r.stderr
    port = env["RB_API_PORT"]
    lines = r.stdout.splitlines()
    assert any(line.startswith("api ") and f"port {port}" in line and " pid " in f" {line} " for line in lines), r.stdout
    container = next(line.split()[1] for line in lines if line.startswith("db ") and "rb-dev-" in line)
    assert dev(env, "url", "api").stdout.strip() == f"http://localhost:{port}"

    code, body = get(f"http://127.0.0.1:{port}/api/v1/health")
    assert code == 200 and body["status"] == "ok" and body["db"] == "ok", body
    assert body["timescaledb"] is True
    assert body["ai_chain"] == ["template"]  # Galit's gate: no live AI before G1

    # The DB is published on 127.0.0.1 only.
    ports = subprocess.run([DOCKER, "port", container, "5432"], capture_output=True, text=True).stdout
    assert ports.strip().startswith("127.0.0.1:"), ports

    r = dev(env, "stop-db")
    assert r.returncode == 0 and f"db: {container} removed" in r.stdout
    deadline = time.monotonic() + 30
    code = 0
    while time.monotonic() < deadline:
        code, body = get(f"http://127.0.0.1:{port}/api/v1/health")
        if code == 503:
            break
        time.sleep(0.5)
    assert code == 503 and body["db"] == "down", body

    r = dev(env, "stop")
    assert r.returncode == 0, r.stderr
    status = dev(env, "status").stdout
    assert status.count("not running") == 3, status
    assert subprocess.run([DOCKER, "inspect", container], capture_output=True).returncode != 0
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=3)


def test_a_second_start_restarts_the_api_and_keeps_the_db(env):
    assert dev(env, "api").returncode == 0
    pid1 = (Path(env["RB_DEV_STATE"]) / "api.pid").read_text().strip()
    r = dev(env, "api")
    assert r.returncode == 0, r.stderr
    assert "db: reusing rb-dev-" in r.stdout
    pid2 = (Path(env["RB_DEV_STATE"]) / "api.pid").read_text().strip()
    assert pid1 != pid2
    assert get(f"http://127.0.0.1:{env['RB_API_PORT']}/api/v1/health")[0] == 200
