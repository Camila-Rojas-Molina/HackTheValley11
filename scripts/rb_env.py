"""Per-worktree names (PLAN 3.4 isolation), so parallel streams and a second runner never share a database or a port.

    .venv/bin/python scripts/rb_env.py            # print this worktree's names as shell assignments
    .venv/bin/python scripts/rb_env.py --port     # print one free TCP port on 127.0.0.1

- worktree hash: first 8 hex characters of sha256(<resolved worktree root path>)
- test database: rb_test_<hash>
- PGDATA: <worktree>/.pgdata/ (gitignored; never a shared parent directory)
- TimescaleDB container: rb-tsdb-<hash>, published on a free port
Standard library only.
"""
from __future__ import annotations

import hashlib
import socket
import subprocess
import sys
from pathlib import Path


def worktree_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    r = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else start


def worktree_hash(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:8]


def test_db_name(root: Path) -> str:
    return f"rb_test_{worktree_hash(root)}"


def pgdata_dir(root: Path) -> Path:
    return Path(root).resolve() / ".pgdata"


def tsdb_container(root: Path) -> str:
    return f"rb-tsdb-{worktree_hash(root)}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main(argv: list[str]) -> int:
    if argv[:1] == ["--port"]:
        print(free_port())
        return 0
    root = worktree_root()
    print(f"RB_WORKTREE={root}")
    print(f"RB_WORKTREE_HASH={worktree_hash(root)}")
    print(f"RB_TEST_DB={test_db_name(root)}")
    print(f"RB_PGDATA={pgdata_dir(root)}")
    print(f"RB_TSDB_CONTAINER={tsdb_container(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
