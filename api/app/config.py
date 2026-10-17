"""Settings from the environment.

Every key is optional except DATABASE_URL in production. With no Telegram token
the outbox uses a 'log' channel, so the app runs and the tests pass without keys.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

log = logging.getLogger("stillhere.config")

# .env.example is public: its "change-me-..." values must never become live credentials.
PLACEHOLDER_PREFIXES = ("change-me", "change_me", "changeme")

# Session presets (BUILD_PLAN §7.1). A preset with an end time asks "Home safe?" then. interval_s is the
# real profile's cadence; the demo profile keeps its compressed timing. walking_home has one check-in: the
# "Home safe?" prompt one interval after arming (then every interval until her PIN or the "I'm home" tap).
PRESETS: dict[str, dict] = {
    "night_out": {"interval_s": 30 * 60, "duration_s": 6 * 3600},
    "date": {"interval_s": 45 * 60, "duration_s": None},
    "walking_home": {"interval_s": 20 * 60, "duration_s": None, "single_checkin": True},
    "demo": {"interval_s": None, "duration_s": None},
}


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


def _secret(name: str, value: Optional[str]) -> Optional[str]:
    """A key from the environment; a placeholder from .env.example counts as unset."""
    v = _opt(value)
    if v is not None and v.lower().startswith(PLACEHOLDER_PREFIXES):
        log.warning("%s is still the .env.example placeholder; treating it as unset", name)
        return None
    return v


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql:///stillhere"
    telegram_bot_token: Optional[str] = None
    telegram_bot_username: Optional[str] = None
    telegram_api_base: str = "https://api.telegram.org"
    public_base_url: str = "http://localhost:8000"
    admin_key: Optional[str] = None
    device_key: Optional[str] = None
    wearer_key: Optional[str] = None
    app_secret: Optional[str] = None
    demo_device_id: str = "scrunchie-1"
    demo_wearer_name: str = "Maya"
    demo_pin: Optional[str] = None
    demo_duress_pin: Optional[str] = None
    demo_lat: float = 43.7844  # UTSC campus, used as a SIMULATED last-known location
    demo_lon: float = -79.1866
    demo_hr_baseline: float = 72.0
    default_profile: str = "demo"
    display_tz: str = "America/Toronto"
    run_workers: bool = True
    auto_migrate: bool = True
    demo_seed: bool = True
    # Database pool per API process (research 1.16). A free Tiger service has 25 connections and no
    # pooler: 8 + the Telegram poller's lock connection + the startup migration stays well under it.
    db_pool_max: int = 8

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    @classmethod
    def from_env(cls, env: Optional[dict] = None, **overrides) -> "Settings":
        e = os.environ if env is None else env
        values = dict(
            database_url=e.get("DATABASE_URL") or "postgresql:///stillhere",
            telegram_bot_token=_opt(e.get("TELEGRAM_BOT_TOKEN")),
            telegram_bot_username=_opt(e.get("TELEGRAM_BOT_USERNAME")),
            telegram_api_base=(e.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/"),
            public_base_url=(e.get("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/"),
            admin_key=_secret("ADMIN_KEY", e.get("ADMIN_KEY")),
            device_key=_secret("DEVICE_KEY", e.get("DEVICE_KEY")),
            wearer_key=_secret("WEARER_KEY", e.get("WEARER_KEY")),
            app_secret=_secret("APP_SECRET", e.get("APP_SECRET")),
            demo_device_id=_opt(e.get("DEMO_DEVICE_ID")) or "scrunchie-1",
            demo_wearer_name=_opt(e.get("DEMO_WEARER_NAME")) or "Maya",
            demo_pin=_opt(e.get("DEMO_PIN")),
            demo_duress_pin=_opt(e.get("DEMO_DURESS_PIN")),
            demo_lat=float(e.get("DEMO_LAT") or 43.7844),
            demo_lon=float(e.get("DEMO_LON") or -79.1866),
            demo_hr_baseline=float(e.get("DEMO_HR_BASELINE") or 72.0),
            display_tz=_opt(e.get("DISPLAY_TZ")) or "America/Toronto",
            run_workers=_bool(e.get("RUN_WORKERS"), True),
            auto_migrate=_bool(e.get("AUTO_MIGRATE"), True),
            demo_seed=_bool(e.get("DEMO_SEED"), True),
            db_pool_max=_int_in(e.get("DB_POOL_MAX"), 8, 1, 20),
        )
        values.update(overrides)
        return cls(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
