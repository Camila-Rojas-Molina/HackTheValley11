"""Security regression index: every finding of the adversarial reviews (r1 #1-#42, r2 #1-#35) is pinned
to the commit that fixed it and to the tests that would fail if it came back (tests/security_findings.py).

`pytest -m security` runs exactly those tests (the marker is applied in tests/conftest.py).
"""
from __future__ import annotations

import ast
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from tests.security_findings import FINDINGS, UNREFERENCED, all_tests

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent.parent


@lru_cache(maxsize=None)
def _functions(filename: str) -> set[str]:
    tree = ast.parse((TESTS / filename).read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_finding_of_both_review_rounds_has_regression_tests():
    expected = {f"r1#{n}" for n in range(1, 43)} - set(UNREFERENCED) | {f"r2#{n}" for n in range(1, 36)}
    assert sorted(expected - set(FINDINGS)) == []
    assert sorted(set(FINDINGS) - expected) == []
    for finding, (commit, tests) in FINDINGS.items():
        assert tests, finding
        assert len(commit) >= 7, finding


@pytest.mark.parametrize("node", sorted(all_tests()))
def test_each_listed_regression_test_exists(node):
    filename, func = node.split("::")
    assert (TESTS / filename).exists(), node
    assert func in _functions(filename), f"{node} is gone: the finding it pinned is no longer tested"


def test_each_fix_commit_is_in_the_history():
    try:
        shallow = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--is-shallow-repository"],
                                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not available")
    if shallow.returncode != 0 or shallow.stdout.strip() == "true":
        pytest.skip("not a full git checkout")
    missing = [f"{finding} {commit}" for finding, (commit, _) in FINDINGS.items()
               if subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{commit}^{{commit}}"],
                                 capture_output=True, timeout=10).returncode != 0]
    assert not missing, missing
