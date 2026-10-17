"""Start the RainbowBone API: one process, no reloader, so the listener is this process.

    .venv/bin/python api/scripts/run.py          (from the repo root; it changes into api/)

Env: HOST (default 0.0.0.0), PORT (default 8000), LOG_LEVEL (default info), plus
everything the app reads (api/.env is loaded by app.main; api/.env.example lists the names).

Windows: uvicorn's default loop there is ProactorEventLoop, which psycopg's async
mode does not support. We set the selector policy and pass loop="none" so
uvicorn uses it (uvicorn >= 0.36 otherwise builds its own Proactor loop).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    os.chdir(API_DIR)
    if str(API_DIR) not in sys.path:
        sys.path.insert(0, str(API_DIR))
    import uvicorn

    loop = "auto"
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        loop = "none"
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        loop=loop,
        log_level=os.environ.get("LOG_LEVEL", "info"),
        proxy_headers=True,
        reload=False,
        workers=None,
    )


if __name__ == "__main__":
    main()
