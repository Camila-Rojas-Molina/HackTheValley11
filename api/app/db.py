"""psycopg 3 async connection pool.

Connections are autocommit; every multi-statement change runs inside an explicit
`async with conn.transaction()`. idle_in_transaction_session_timeout is set per connection, so a client
lost mid-transaction cannot keep job rows locked (SKIP LOCKED would skip them forever otherwise), and no
long work may ever run inside a transaction (jobs are claimed, committed, then run: app/jobs.py).
"""
from __future__ import annotations

import logging

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("rainbowbone.db")

IDLE_IN_TRANSACTION_TIMEOUT = "10s"


async def _configure(conn) -> None:
    await conn.execute(f"SET idle_in_transaction_session_timeout = '{IDLE_IN_TRANSACTION_TIMEOUT}'")
    await conn.execute("SET TIME ZONE 'UTC'")


FREE_TIER_MAX_CONNECTIONS = 25   # a free Tiger Cloud service: fixed, and no connection pooler


def connection_budget(settings) -> dict:
    """The most connections one API process holds: its pool plus the migration runner at start-up."""
    return {"pool_max": settings.db_pool_max, "migrations_at_start": 1,
            "total_max": settings.db_pool_max + 1, "free_tier_max_connections": FREE_TIER_MAX_CONNECTIONS}


class Database:
    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 10, timeout: float = 5.0,
                 name: str = "rainbowbone"):
        self.conninfo = conninfo
        self.pool = AsyncConnectionPool(
            conninfo,
            open=False,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            kwargs={"autocommit": True, "row_factory": dict_row},
            configure=_configure,
            check=AsyncConnectionPool.check_connection,
            name=name,
        )

    async def open(self, wait: bool = False, timeout: float = 10.0) -> None:
        await self.pool.open(wait=wait, timeout=timeout)

    async def close(self) -> None:
        await self.pool.close()

    def connection(self):
        return self.pool.connection()
