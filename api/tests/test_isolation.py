"""Test isolation (PLAN M1 "Tests: Isolation", 3.4): every worktree gets its own database and PGDATA."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from tests.conftest import PGDATA, TEST_DB, WORKTREE, db_name_for, pgdata_for


def test_two_worktree_paths_give_two_databases_and_two_pgdata_dirs(tmp_path):
    a, b = tmp_path / "rb-wt" / "s4-api", tmp_path / "rb-wt" / "s5-ai"
    assert db_name_for(a) != db_name_for(b)
    assert pgdata_for(a) != pgdata_for(b)
    for wt in (a, b):
        assert re.fullmatch(r"rb_test_[0-9a-f]{8}", db_name_for(wt))
        assert pgdata_for(wt) == wt.resolve() / ".pgdata"


def test_names_are_stable_for_one_path(tmp_path):
    wt = tmp_path / "rb-wt" / "s4-api"
    assert db_name_for(wt) == db_name_for(Path(str(wt) + "/"))
    assert db_name_for(wt) == db_name_for(wt / ".." / "s4-api")


def test_this_checkout_uses_its_own_names():
    assert WORKTREE == Path(__file__).resolve().parents[2]
    assert (WORKTREE / "api" / "tests" / "conftest.py").is_file()
    assert TEST_DB == db_name_for(WORKTREE)
    assert PGDATA == WORKTREE / ".pgdata"
    assert PGDATA.parent == WORKTREE, "PGDATA must sit inside this worktree, never in a shared parent"


def test_the_session_database_is_this_worktrees(db_url):
    import psycopg
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT current_database()").fetchone()[0] == TEST_DB


def test_names_match_the_scripts_helper():
    """scripts/rb_env.py (S0) prints the same names for the dev and integration scripts."""
    helper = WORKTREE / "scripts" / "rb_env.py"
    if not helper.is_file():
        pytest.skip("scripts/rb_env.py (S0) is not in this checkout")
    spec = importlib.util.spec_from_file_location("rb_env_for_test", helper)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.test_db_name(WORKTREE) == TEST_DB
    assert mod.pgdata_dir(WORKTREE) == PGDATA
