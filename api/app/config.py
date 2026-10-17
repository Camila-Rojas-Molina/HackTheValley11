"""Settings from the environment (the RB_ prefix; api/.env.example lists every name).

Every key is optional for local work. RB_DATABASE_URL names the database (DATABASE_URL is read when it is
unset, for tools that only know that name). With RB_ADMIN_KEY unset the admin routes answer 503. The AI
layer reads its own unprefixed names (AI_CHAIN, GEMINI_API_KEY, ...; contracts/ai-interface.md).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

log = logging.getLogger("rainbowbone.config")

# .env.example is public: its "change-me-..." values must never become live credentials.
PLACEHOLDER_PREFIXES = ("change-me", "change_me", "changeme")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_URL = "postgresql:///rainbowbone"
DEFAULT_CORS_ORIGINS = ("http://localhost:3000",)   # the Next.js dev server only


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _opt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _int_in(value: Optional[str], default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _float_in(value: Optional[str], default: float, lo: float, hi: float) -> float:
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _secret(name: str, value: Optional[str]) -> Optional[str]:
    """A key from the environment; a placeholder from .env.example counts as unset."""
    v = _opt(value)
    if v is not None and v.lower().startswith(PLACEHOLDER_PREFIXES):
        log.warning("%s is still the .env.example placeholder; treating it as unset", name)
        return None
    return v


LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_local_database(url: str, env: Optional[dict] = None) -> bool:
    """True when every host the connection string names is this machine (a name above or a unix socket)."""
    from psycopg.conninfo import conninfo_to_dict
    e = os.environ if env is None else env
    try:
        params = conninfo_to_dict(url)
    except Exception:
        return False
    raw = [str(params.get(k) or "") for k in ("host", "hostaddr")]
    if not any(raw):   # libpq falls back to PGHOST / PGHOSTADDR, then the local socket
        raw = [str(e.get("PGHOST") or ""), str(e.get("PGHOSTADDR") or "")]
    hosts = [h.strip() for r in raw for h in r.split(",") if h.strip()]
    return all(h in LOCAL_HOSTS or h.startswith("/") for h in hosts)


def schema_writes_allowed(url: str, env: Optional[dict] = None) -> bool:
    """Migrations and the demo seed only touch a database on this machine, unless RB_ALLOW_REMOTE_SCHEMA=1.

    Tiger Cloud gets the schema only after G1: only the deploy environment sets RB_ALLOW_REMOTE_SCHEMA.
    """
    e = os.environ if env is None else env
    return _bool(e.get("RB_ALLOW_REMOTE_SCHEMA"), False) or is_local_database(url, e)


def _origins(value: Optional[str]) -> tuple[str, ...]:
    v = _opt(value)
    if v is None:
        return DEFAULT_CORS_ORIGINS
    return tuple(o.strip().rstrip("/") for o in v.split(",") if o.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    admin_key: Optional[str] = None
    # Analysis outputs the API serves (<storage_dir>/<scan_id>/<name>); the committed demo bundle
    # (<repo>/demo/<scan_id>/<name>) is read as well.
    storage_dir: Path = REPO_ROOT / "data" / "storage"
    demo_dir: Path = REPO_ROOT / "demo"
    # Shared large data outside the repo (full scans, weights); read by the worker from M5.
    data_dir: Optional[Path] = None
    max_upload_mb: int = 600          # a full Myel_018 CT is about 260 MB as .nii.gz
    solve_timeout_s: int = 120        # C5 physics
    segment_timeout_s: int = 900      # C5 segment
    load_n: float = 800.0             # axial load on the vertebral body (PLAN 6.2)
    cors_origins: tuple[str, ...] = field(default=DEFAULT_CORS_ORIGINS)
    auto_migrate: bool = True
    demo_seed: bool = True
    # Off: auto_migrate and demo_seed refuse a database that is not on this machine (see schema_writes_allowed).
    allow_remote_schema: bool = False
    # The API runs no workers: jobs run in the separate worker process (C5). Kept so a caller can say so.
    run_workers: bool = False
    # Database pool per API process. A free Tiger service has 25 connections and no pooler.
    db_pool_max: int = 8

    @property
    def schema_writes_allowed(self) -> bool:
        return self.allow_remote_schema or is_local_database(self.database_url)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @classmethod
    def from_env(cls, env: Optional[dict] = None, **overrides) -> "Settings":
        e = os.environ if env is None else env
        data_dir = _opt(e.get("RB_DATA_DIR"))
        storage = _opt(e.get("RB_STORAGE_DIR"))
        values = dict(
            database_url=_opt(e.get("RB_DATABASE_URL")) or _opt(e.get("DATABASE_URL")) or DEFAULT_DATABASE_URL,
            admin_key=_secret("RB_ADMIN_KEY", e.get("RB_ADMIN_KEY")),
            storage_dir=Path(storage).expanduser() if storage else REPO_ROOT / "data" / "storage",
            data_dir=Path(data_dir).expanduser() if data_dir else None,
            max_upload_mb=_int_in(e.get("RB_MAX_UPLOAD_MB"), 600, 1, 4096),
            solve_timeout_s=_int_in(e.get("RB_SOLVE_TIMEOUT_S"), 120, 10, 3600),
            segment_timeout_s=_int_in(e.get("RB_SEGMENT_TIMEOUT_S"), 900, 30, 7200),
            load_n=_float_in(e.get("RB_LOAD_N"), 800.0, 1.0, 10000.0),
            cors_origins=_origins(e.get("RB_CORS_ORIGINS")),
            auto_migrate=_bool(e.get("RB_AUTO_MIGRATE"), True),
            demo_seed=_bool(e.get("RB_DEMO_SEED"), True),
            allow_remote_schema=_bool(e.get("RB_ALLOW_REMOTE_SCHEMA"), False),
            db_pool_max=_int_in(e.get("RB_DB_POOL_MAX"), 8, 1, 20),
        )
        values.update(overrides)
        return cls(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
