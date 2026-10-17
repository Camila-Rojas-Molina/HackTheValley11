"""Tiny migration runner: applies backend/migrations/*.sql in name order, once each.

Usage (from backend/):  DATABASE_URL=postgresql://... ../.venv/bin/python scripts/migrate.py
Each file runs in its own transaction and is recorded in schema_migrations with
its sha256. A Postgres advisory lock keeps two runners from racing.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 7_202_611  # arbitrary, constant


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(p for p in directory.glob("*.sql") if p.is_file())


def apply_migrations(conninfo: str, directory: Path = MIGRATIONS_DIR, verbose: bool = False) -> list[str]:
    """Apply pending migrations. Returns the names applied in this run."""
    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True, connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       name text PRIMARY KEY,
                       sha256 text NOT NULL,
                       applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            done = {r[0]: r[1] for r in conn.execute("SELECT name, sha256 FROM schema_migrations")}
            for path in migration_files(directory):
                sql = path.read_text(encoding="utf-8")
                digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if path.name in done:
                    if done[path.name] != digest and verbose:
                        print(f"warning: {path.name} changed after it was applied", file=sys.stderr)
                    continue
                with conn.transaction():
                    conn.execute(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)", (path.name, digest)
                    )
                applied.append(path.name)
                if verbose:
                    print(f"applied {path.name}")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    names = apply_migrations(url, verbose=True)
    if not names:
        print("schema up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
