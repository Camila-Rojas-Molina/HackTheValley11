"""psycopg 3 async connection pool.

Connections are autocommit; every multi-statement change runs inside an
explicit `async with conn.transaction()`. idle_in_transaction_session_timeout
is set per connection so a client lost mid-transaction cannot keep timer rows
locked (SKIP LOCKED would skip them forever otherwise).
"""
from __future__ import annotations

import logging

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("stillhere.db")


async def _configure(conn) -> None:
    await conn.execute("SET idle_in_transaction_session_timeout = '10s'")
    await conn.execute("SET TIME ZONE 'UTC'")


class Database:
    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 10, timeout: float = 5.0):
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
            name="stillhere",
        )

    async def open(self, wait: bool = False, timeout: float = 10.0) -> None:
        await self.pool.open(wait=wait, timeout=timeout)

    async def close(self) -> None:
        await self.pool.close()

    def connection(self):
        return self.pool.connection()
