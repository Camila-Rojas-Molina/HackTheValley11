"""Settings from the environment, plus the demo and real timing profiles.

Every key is optional except DATABASE_URL in production. With no Telegram token
the outbox uses a 'log' channel, so the app runs and the tests pass without keys.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from .states import Timings

log = logging.getLogger("stillhere.config")

# .env.example is public: its "change-me-..." values must never become live credentials.
PLACEHOLDER_PREFIXES = ("change-me", "change_me", "changeme")

# Demo compresses minutes into seconds (BUILD_PLAN §6.2 "Deadline demo / real").
# Thresholds marked "assumption" are untested engineering assumptions.
PROFILES: dict[str, Timings] = {
    "demo": Timings(
        name="demo",
        interval_s=60,
        prompt_window_s=12,
        delivery_grace_s=30,
        ack_s=15,
        duress_ack_s=10,
        renotify_s=20,
        renotify_cap=10,
        followup_s=20,
        link_expiry_s=15,
        heartbeat_s=2,
        lease_s=10,
        lost_contact_grace_s=10,
        hr_sustain_s=15,
        hr_eval_s=2,
        sensor_cooldown_s=60,
        hr_lost_s=120,
    ),
    "real": Timings(
        name="real",
        interval_s=1800,
        prompt_window_s=120,
        delivery_grace_s=30,
        ack_s=300,
        duress_ack_s=120,
        renotify_s=60,
        renotify_cap=10,
        followup_s=600,
        link_expiry_s=1800,
        heartbeat_s=15,
        lease_s=60,
        lost_contact_grace_s=300,
        hr_sustain_s=90,
        hr_eval_s=10,
        sensor_cooldown_s=900,
        hr_lost_s=120,
    ),
}

# Session presets. Each ends with a "Home safe?" prompt at end time.
PRESETS: dict[str, dict] = {
    "night_out": {"duration_s": 6 * 3600},
    "walking_home": {"duration_s": 45 * 60},
    "demo": {"duration_s": None},
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
    profiles: dict = field(default_factory=lambda: PROFILES)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    def timings(self, profile: Optional[str]) -> Timings:
        return self.profiles.get(profile or self.default_profile) or self.profiles["demo"]

    @classmethod
    def from_env(cls, env: Optional[dict] = None, **overrides) -> "Settings":
        e = os.environ if env is None else env
        profile = (e.get("STILLHERE_PROFILE") or "demo").strip()
        if profile not in PROFILES:
            profile = "demo"
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
            default_profile=profile,
            display_tz=_opt(e.get("DISPLAY_TZ")) or "America/Toronto",
            run_workers=_bool(e.get("RUN_WORKERS"), True),
            auto_migrate=_bool(e.get("AUTO_MIGRATE"), True),
            demo_seed=_bool(e.get("DEMO_SEED"), True),
        )
        values.update(overrides)
        return cls(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
