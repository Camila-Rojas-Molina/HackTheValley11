"""Start the StillHere server: one process, no reloader, so the listener is this process.

    python scripts/run.py            (from backend/; serve.sh runs it in a restart loop)

Env: HOST (default 0.0.0.0), PORT (default 8000), LOG_LEVEL (default info), plus
everything the app reads (backend/.env is loaded by app.main).

Windows: uvicorn's default loop there is ProactorEventLoop, which psycopg's async
mode does not support. We set the selector policy and pass loop="none" so
uvicorn uses it (uvicorn >= 0.36 otherwise builds its own Proactor loop).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def main() -> None:
    os.chdir(BACKEND)
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
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
