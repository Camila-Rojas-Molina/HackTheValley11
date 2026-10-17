"""Every route the app mounts, read from the app itself (routers included), for the contract and wiring tests."""
from __future__ import annotations


def mounted_routes() -> dict[tuple[str, str], str]:
    """{(METHOD, path): endpoint module} for every route create_app() mounts (HEAD left out)."""
    from fastapi.routing import _IncludedRouter

    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(database_url="postgresql:///unused"), start_workers=False)
    out: dict[tuple[str, str], str] = {}

    def walk(routes, prefix=""):
        for r in routes:
            if isinstance(r, _IncludedRouter):
                walk(r.original_router.routes, prefix + (r.include_context.prefix or ""))
            else:
                module = getattr(getattr(r, "endpoint", None), "__module__", "")
                for m in getattr(r, "methods", None) or ():
                    if m != "HEAD":
                        out[(m, prefix + r.path)] = module
    walk(app.router.routes)
    return out


def served_routes() -> set[tuple[str, str]]:
    return set(mounted_routes())
