"""Request helpers: the database handle and the admin key check."""
from __future__ import annotations

from fastapi import HTTPException, Request

from .config import Settings
from .security import same


class AdminDisabled(Exception):
    """RB_ADMIN_KEY is unset: admin routes answer 503 with the Unavailable body (main.py handler)."""


class DatabaseNotReady(Exception):
    """Migrations or the seed have not run yet: database routes answer 503 (main.py handler)."""


def get_db(request: Request):
    return request.app.state.db


def ready_db(request: Request):
    """The database, once start-up migrations and the seed have run; 503 before that."""
    if not getattr(request.app.state, "db_ready", False):
        raise DatabaseNotReady()
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def is_admin(request: Request) -> bool:
    expected = request.app.state.settings.admin_key
    return bool(expected) and same(request.headers.get("x-admin-key"), expected)


def require_admin(request: Request) -> None:
    """Admin routes: 503 when RB_ADMIN_KEY is unset, 401 when X-Admin-Key is missing or wrong."""
    if not request.app.state.settings.admin_key:
        raise AdminDisabled()
    if not is_admin(request):
        raise HTTPException(401, "X-Admin-Key missing or wrong")
