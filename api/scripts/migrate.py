"""Tiny migration runner: applies api/migrations/*.sql in name order, once each.

Usage (from the repo root):  RB_DATABASE_URL=postgresql://... .venv/bin/python api/scripts/migrate.py
Each file runs in its own transaction and is recorded in schema_migrations with its sha256, so a second
run applies nothing. A Postgres advisory lock (LOCK_ID, new for RainbowBone so it never waits on a StillHere
runner sharing a server) keeps two runners from racing.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
LOCK_ID = 0x5242_4D31  # "RBM1": arbitrary, constant, RainbowBone only


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
                    if done[path.name] != digest:   # never silent: change an applied schema in a new file
                        print(f"warning: {path.name} changed after it was applied; the change is NOT applied "
                              "(add a new numbered migration)", file=sys.stderr)
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
    url = os.environ.get("RB_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("RB_DATABASE_URL is not set", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.config import schema_writes_allowed
    if not schema_writes_allowed(url):
        print("refusing to migrate a database that is not on this machine (Tiger Cloud only after G1, "
              "with RB_ALLOW_REMOTE_SCHEMA=1)", file=sys.stderr)
        return 2
    names = apply_migrations(url, verbose=True)
    if not names:
        print("schema up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
