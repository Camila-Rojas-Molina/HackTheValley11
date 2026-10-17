"""scripts/dev.sh and the M2/M3 DONE CHECK scripts, without Docker or a server (the live run is the `dev` suite,
tests/repo/live/). Every dev.sh call here uses its own state dir and a Docker stand-in, so it never touches this
worktree's real dev servers or containers."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "scripts" / "dev.sh"
sys.path.insert(0, str(REPO / "scripts"))
import rb_env  # noqa: E402


@pytest.fixture
def env(tmp_path):
    """A private state dir and a `docker` that is never running."""
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("RB_API_PORT", "RB_WEB_PORT"))}
    e.update(RB_DEV_STATE=str(tmp_path / "state"), DOCKER=str(fake))
    return e


def dev(env, *args, timeout=60):
    return subprocess.run(["bash", str(DEV), *args], cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=timeout)


def state(env) -> Path:
    return Path(env["RB_DEV_STATE"])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --------------------------------------------------------------------------------------------- usage, idle
def test_help_lists_every_mode(env):
    r = dev(env, "--help")
    assert r.returncode == 0
    for mode in ("[all]", " api ", " web ", " status ", " url ", " stop ", " stop-api ", " stop-db "):
        assert mode in r.stdout, mode


@pytest.mark.parametrize("args", [("nonsense",), ("url",), ("url", "db"), ("all", "x", "y")])
def test_bad_arguments_exit_2(env, args):
    assert dev(env, *args).returncode == 2


def test_status_and_stop_with_nothing_running(env):
    r = dev(env, "status")
    assert r.returncode == 0
    assert [line.split()[0] for line in r.stdout.splitlines()] == ["db", "api", "web"]
    assert r.stdout.count("not running") == 3
    r = dev(env, "stop")
    assert r.returncode == 0, r.stderr
    assert "web: not running" in r.stdout and "api: not running" in r.stdout and "db: not running" in r.stdout
    assert dev(env, "url", "api").returncode == 1
    assert dev(env, "url", "web").returncode == 1


def test_no_docker_is_a_clear_error_for_the_db(env):
    r = dev(env, "api")
    assert r.returncode == 1
    assert "Docker is not running" in r.stderr


def test_a_taken_port_override_fails_before_any_container(env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        r = dev({**env, "RB_API_PORT": str(port)}, "api")
    assert r.returncode == 1
    assert f"api port {port} is taken" in r.stderr
    assert "Docker" not in r.stderr  # the port is checked before the DB is created
    r = dev({**env, "RB_API_PORT": "80a"}, "api")
    assert r.returncode == 1 and "is not a number" in r.stderr


# --------------------------------------------------------------------------------------------- pid safety
def test_a_stale_pid_file_never_signals_an_unrelated_process(env):
    """A pid file left behind after a reboot can name someone else's process: dev.sh only signals a pid whose
    command line is this worktree's own entry point."""
    other = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        state(env).mkdir(parents=True, exist_ok=True)
        (state(env) / "api.pid").write_text(f"{other.pid}\n")
        (state(env) / "api.port").write_text("8000\n")
        r = dev(env, "stop-api")
        assert r.returncode == 0, r.stderr
        assert "stale pid file removed" in r.stdout
        time.sleep(0.3)
        assert alive(other.pid)
        assert not (state(env) / "api.pid").exists() and not (state(env) / "api.port").exists()
        assert "api  not running" in dev(env, "status").stdout
    finally:
        other.kill()
        other.wait()


def test_stop_api_ends_this_worktrees_api_and_its_process_group(env):
    """A stand-in whose command line carries api/scripts/run.py, with a child in its process group: stop-api
    ends both (uvicorn or next dev children must not outlive dev.sh stop)."""
    code = ("import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
            " time.sleep(60)")
    proc = subprocess.Popen([sys.executable, "-c", code, str(REPO / "api" / "scripts" / "run.py")],
                            start_new_session=True)
    try:
        time.sleep(0.5)
        children = subprocess.run(["pgrep", "-g", str(proc.pid)], capture_output=True, text=True).stdout.split()
        assert len(children) >= 2, children
        state(env).mkdir(parents=True, exist_ok=True)
        (state(env) / "api.pid").write_text(f"{proc.pid}\n")
        (state(env) / "api.port").write_text("8000\n")
        r = dev(env, "status")
        assert f"pid {proc.pid} port 8000" in r.stdout
        assert dev(env, "url", "api").stdout.strip() == "http://localhost:8000"
        r = dev(env, "stop-api")
        assert r.returncode == 0, r.stderr
        assert f"api: stopped (pid {proc.pid})" in r.stdout
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(alive(int(c)) for c in children):
            time.sleep(0.1)
        assert not any(alive(int(c)) for c in children)
        assert not (state(env) / "api.pid").exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)


def test_two_state_dirs_get_two_db_containers(env, tmp_path):
    names = set()
    for d in ("a", "b"):
        r = dev({**env, "RB_DEV_STATE": str(tmp_path / d)}, "status")
        assert r.returncode == 0
        names.add(r.stdout.splitlines()[0].split("container ")[1].rstrip(")"))
    assert len(names) == 2 and all(n.startswith("rb-dev-") and len(n) == len("rb-dev-") + 8 for n in names)


# --------------------------------------------------------------------------------------------- ports
def test_pick_port_keeps_a_free_preferred_port():
    port = rb_env.free_port()
    assert not rb_env.port_in_use(port)
    assert rb_env.pick_port(port) == port


def test_pick_port_moves_off_a_taken_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert rb_env.port_in_use(port)
        other = rb_env.pick_port(port)
        assert other != port and not rb_env.port_in_use(other)


def test_rb_env_port_flags():
    run = lambda *a: subprocess.run([sys.executable, str(REPO / "scripts" / "rb_env.py"), *a],  # noqa: E731
                                    capture_output=True, text=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert run("--port-in-use", str(port)).returncode == 0
        assert int(run("--pick-port", str(port)).stdout) != port
    assert run("--port-in-use", str(port)).returncode == 1


# --------------------------------------------------------------------------------------------- the gate
def test_dev_api_is_pinned_to_the_template_chain_and_a_local_db():
    """Galit's gate: before G1 no live AI call and no schema on Tiger Cloud. dev.sh hands the API an explicit
    local URL under both spellings, pins AI_CHAIN, and checks after start-up that this DB was migrated."""
    text = DEV.read_text()
    assert '"AI_CHAIN=template"' in text
    assert '"RB_DATABASE_URL=$url" "DATABASE_URL=$url" "RB_AUTO_MIGRATE=1"' in text
    assert "postgresql://postgres@127.0.0.1:%s/%s" in text
    assert '-p "127.0.0.1:$port:5432"' in text
    assert "table_schema = 'public'" in text and "api/migrations/*.sql" in text
    assert "-H 127.0.0.1" in text and '"HOST=127.0.0.1"' in text


# --------------------------------------------------------------------------------------------- DONE CHECKs
def test_m2_donecheck_runs_the_plan_lines():
    text = (REPO / "scripts" / "donecheck" / "m2.sh").read_text()
    for line in ("scripts/test_all.sh unit contract db tsdb", "scripts/dev.sh api", '"$API/api/v1/health"',
                 '"$API/api/v1/tiger/info" | "$PY" -m json.tool', "grep -E '\"name\"|db_size'",
                 '"$API/api/v1/scans/myel018-L1-d273/summary" | grep -o \'"source": *"[a-z]*"\'',
                 "scripts/dev.sh stop-db", "scripts/dev.sh stop >/dev/null", "scripts/dev.sh url api"):
        assert line in text, line
    for name in ("scan_metrics", "fe_element_results", "ai_generation", "scan_metrics_monthly", "fe_level_daily"):
        assert name in text, name


def test_m3_donecheck_follows_the_click_path_and_the_labels(labels):
    text = (REPO / "scripts" / "donecheck" / "m3.sh").read_text()
    for line in ("scripts/dev.sh 2>&1", "/scan/myel018-L1-d273", "/scan/myel018-L1-d0", "/scan/phantom",
                 'node web/tests/smoke.mjs "$WEB"', "scripts/dev.sh stop-api", "scripts/dev.sh url web"):
        assert line in text, line
    flat = json.dumps(labels, ensure_ascii=False)
    for s in ("offline · fixtures", "not yet registered (M5)", "Compressive strain (µε)", "original scan grid"):
        assert s in flat, f"{s!r} is no longer in contracts/labels.json"
        assert s in text, s


@pytest.mark.parametrize("script", ["donecheck/m2.sh", "donecheck/m3.sh"])
def test_donechecks_never_fall_back_to_a_fixed_port(script):
    """If dev.sh fails, :8000 or :3000 may be another worktree's server: a fallback there is a false PASS."""
    text = (REPO / "scripts" / script).read_text()
    assert "localhost:8000\")" not in text and "localhost:3000\")" not in text
    assert 'url api 2>/dev/null || true)"' in text


@pytest.mark.parametrize("script", ["donecheck/m2.sh", "donecheck/m3.sh"])
def test_donechecks_stop_their_servers(script):
    text = (REPO / "scripts" / script).read_text()
    assert "scripts/dev.sh stop" in text
    assert "trap 'scripts/dev.sh stop" in text
