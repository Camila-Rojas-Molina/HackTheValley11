"""A real RainbowBone API process for the live tests: scripts/run.py on a free port, its own database, and no
AI key (the chain answers with the template).
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

from tests.conftest import fresh_database

API_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = API_DIR / "scripts"
KEYS = {"RB_ADMIN_KEY": "live-admin"}
ADMIN = {"X-Admin-Key": KEYS["RB_ADMIN_KEY"]}
# Unset for every live server: the code must run without any of them.
NO_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "VULTR_API_KEY", "VULTR_INFERENCE_API_KEY", "LOCAL_LLM_BASE_URL",
           "DATABASE_URL")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout: float, what: str, every: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}")


class LiveServer:
    def __init__(self, pg_base_uri: str, name: str, log_dir: Path, *, database_url: Optional[str] = None,
                 log_level: str = "info", extra_env: Optional[dict] = None):
        self.db_url = fresh_database(pg_base_uri, name)
        self.server_db_url = database_url or self.db_url
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = log_dir / f"{name}.log"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("RB_", "AI_", "PYTEST")) and k not in NO_KEYS}
        env.update(KEYS)
        env.update({"RB_DATABASE_URL": self.server_db_url, "HOST": "127.0.0.1", "PORT": str(self.port),
                    "RB_AUTO_MIGRATE": "1", "RB_DEMO_SEED": "1", "AI_CHAIN": "template", "LOG_LEVEL": log_level,
                    "PYTHON": sys.executable})
        for k in NO_KEYS:
            env[k] = ""
        env.update(extra_env or {})
        self.env = env
        self.proc: Optional[subprocess.Popen] = None

    def start(self, wait: bool = True) -> "LiveServer":
        fh = open(self.log, "ab")
        self.proc = subprocess.Popen([sys.executable, str(SCRIPTS / "run.py")], cwd=API_DIR, env=self.env,
                                     stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        if wait:
            self.wait_healthy()
        return self

    def wait_healthy(self, timeout: float = 40) -> None:
        def ok():
            if self.proc is not None and self.proc.poll() is not None:
                raise AssertionError(f"server exited with {self.proc.returncode}:\n{self.log_text()[-3000:]}")
            try:
                return httpx.get(f"{self.base}/api/v1/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                return False
        wait_for(ok, timeout, f"{self.base} to answer /api/v1/health")

    def kill9(self) -> None:
        assert self.proc is not None
        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=5)

    def log_text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, timeout=15)
