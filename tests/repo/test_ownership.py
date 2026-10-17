"""contracts/ownership.json (PLAN 3.1): every tracked file has exactly one owning stream, and a stream branch
touches only its own globs (scripts/check_ownership.py)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import check_ownership as co  # noqa: E402

OWN = co.Ownership.load(REPO)

# PLAN M1 "Files touched: New", plus the paths M2-M4 name, so the map is right before anyone writes them.
PLANNED = """README.md ARCHITECTURE.md contracts/openapi.json contracts/formats.md contracts/db.md
contracts/ai-interface.md contracts/jobs.md contracts/labels.json contracts/pipeline.md contracts/ownership.json
contracts/fixtures/index.json contracts/fixtures/scans/myel018-L1-d273.json api/app/schemas.py
api/app/routes/patients.py api/app/routes/scans.py api/app/routes/series.py api/app/routes/jobs.py
api/app/routes/summary.py api/app/routes/copilot.py api/app/routes/tiger.py api/app/routes/audit.py
api/app/routes/alerts.py api/app/ai/__init__.py api/app/ai/facts.py api/app/ai/contract.py api/app/ai/chain.py
api/app/ai/template.py api/app/ai/provenance.py api/app/ai/copilot.py api/app/ai/tools.py
api/app/ai/providers/__init__.py api/app/ai/providers/base.py api/app/ai/providers/gemini.py
api/app/ai/providers/openai_compat.py api/app/ai/providers/vultr.py api/tests/conftest.py api/tests/ai/test_chain.py
api/requirements-ai.txt api/requirements.txt pipeline/rb_pipeline/io/__init__.py pipeline/rb_pipeline/segment/__init__.py
pipeline/rb_pipeline/longitudinal/__init__.py pipeline/rb_pipeline/physics/__init__.py
pipeline/rb_pipeline/export/__init__.py pipeline/pyproject.toml pipeline/rb_pipeline/__init__.py
pipeline/tests/conftest.py worker/rb_worker/__init__.py web/package.json web/lib/utils.ts web/app/page.tsx
scripts/test_all.sh scripts/check_ownership.py scripts/bootstrap.sh scripts/integrate.sh scripts/watchdog.sh
scripts/donecheck/m1.sh suites/s0.toml suites/s1.toml suites/s4.toml suites/s5.toml
.github/workflows/ci-core.yml .github/workflows/s1-web.yml .github/workflows/s4-api.yml
.github/workflows/s5-ai.yml tests/repo/test_docs.py tests/repo/test_wording.py
demo/ATTRIBUTION.md demo/PROVENANCE.json demo/myel018-L1-d273/ct.nii.gz pipeline/tests/test_demo_bundle.py
deploy/Caddyfile deploy/RUNBOOK.md deploy/api.Dockerfile .dockerignore api/migrations/rb_001_core.sql
api/scripts/seed_demo.py pipeline/rb_pipeline/physics/fe.py pipeline/tests/physics/test_patch.py
pipeline/requirements-physics.txt pipeline/requirements-io.txt pipeline/requirements-seg.txt
api/tests/ai/test_filter.py""".split()


def test_every_tracked_file_has_exactly_one_owner():
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "check_ownership.py"), "--all", "--repo", str(REPO)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK: 0 unowned, 0 overlapping paths" in r.stdout


@pytest.mark.parametrize("path", PLANNED)
def test_every_planned_path_has_exactly_one_owner(path):
    assert len(OWN.owners(path)) == 1, (path, OWN.owners(path))


@pytest.mark.parametrize("path,owner", [
    ("api/app/schemas.py", "S0"), ("api/app/main.py", "S4"), ("api/app/ai/chain.py", "S5"),
    ("api/tests/ai/test_chain.py", "S5"), ("api/tests/test_contract.py", "S4"), ("api/requirements-ai.txt", "S5"),
    ("pipeline/rb_pipeline/physics/fe.py", "S3"), ("pipeline/rb_pipeline/io/crop.py", "S2"),
    ("pipeline/rb_pipeline/__init__.py", "S0"), ("web/lib/utils.ts", "S1"), ("deploy/Caddyfile", "S6"),
    ("worker/rb_worker/run.py", "S4"), ("tests/repo/contracts/test_openapi.py", "S0"),
])
def test_owner(path, owner):
    assert OWN.owners(path) == [owner]


@pytest.mark.parametrize("glob,path,match", [
    ("contracts/**", "contracts/a.json", True), ("contracts/**", "contracts/fixtures/x/y.json", True),
    ("contracts/**", "contractsx/a", False), ("suites/s0.toml", "suites/s0.toml", True),
    ("pipeline/tests/physics/**", "pipeline/tests/physics/test_a.py", True), ("a/*.py", "a/b/c.py", False),
    ("a/**/c.py", "a/c.py", True), ("a/**/c.py", "a/x/y/c.py", True), ("a/?.py", "a/bb.py", False),
])
def test_glob_semantics(glob, path, match):
    assert bool(co.glob_to_regex(glob).match(path)) is match


@pytest.mark.parametrize("branch,expected", [
    ("rb/s0/m1", ("S0", "M1")), ("rb/s1-web/m1", ("S1", "M1")), ("rb/s3-physics/m5", ("S3", "M5")),
    ("rb/s5-ai/m9-1", ("S5", "M9-1")),
])
def test_branch_names(branch, expected):
    assert co.parse_branch(branch) == expected


@pytest.mark.parametrize("branch", ["rb/integrate/m1", "main", "rb/s1", "feature/x", "rb/land/m1"])
def test_non_stream_branches_are_refused(branch):
    with pytest.raises(ValueError):
        co.parse_branch(branch)


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
                          env=env).stdout


@pytest.fixture()
def toy(tmp_path: Path) -> Path:
    """A repo with ownership.json, a base branch rb/integrate/m1 and nothing else."""
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "contracts").mkdir()
    shutil.copy(REPO / "contracts" / "ownership.json", tmp_path / "contracts" / "ownership.json")
    (tmp_path / "api" / "app").mkdir(parents=True)
    (tmp_path / "api" / "app" / "main.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _git(tmp_path, "branch", "rb/integrate/m1")
    return tmp_path


def _branch(repo: Path, name: str, files: dict[str, str]) -> None:
    _git(repo, "switch", "-q", "-c", name, "rb/integrate/m1")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    _git(repo, "switch", "-q", "main")


def _check(repo: Path, branch: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "scripts" / "check_ownership.py"), "--repo", str(repo),
                           "--branch", branch], capture_output=True, text=True)


def test_branch_touching_only_its_own_globs_passes(toy):
    _branch(toy, "rb/s4-api/m1", {"api/app/main.py": "x = 2\n", "api/app/routes/patients.py": ""})
    r = _check(toy, "rb/s4-api/m1")
    assert r.returncode == 0, r.stdout
    assert "touched 2 files" in r.stdout


def test_branch_touching_another_streams_file_fails(toy):
    _branch(toy, "rb/s4-api/m1", {"api/app/schemas.py": "# S0's file\n", "api/app/ai/chain.py": ""})
    r = _check(toy, "rb/s4-api/m1")
    assert r.returncode == 1
    assert "NOT S4: api/app/ai/chain.py (owner: S5)" in r.stdout
    assert "NOT S4: api/app/schemas.py (owner: S0)" in r.stdout


def test_bootstrap_exceptions_apply_to_their_stream_and_milestone_only(toy):
    files = {"pipeline/rb_pipeline/physics/__init__.py": "", "worker/rb_worker/__init__.py": ""}
    _branch(toy, "rb/s0/m1", files)
    assert _check(toy, "rb/s0/m1").returncode == 0
    _git(toy, "branch", "rb/integrate/m2", "rb/integrate/m1")
    _git(toy, "switch", "-q", "-c", "rb/s0/m2", "rb/s0/m1")
    _git(toy, "switch", "-q", "main")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "check_ownership.py"), "--repo", str(toy),
                        "--branch", "rb/s0/m2", "--base", "rb/integrate/m1"], capture_output=True, text=True)
    assert r.returncode == 1 and "NOT S0: pipeline/rb_pipeline/physics/__init__.py (owner: S3)" in r.stdout


def test_a_deleted_file_counts_as_touched(toy):
    _git(toy, "switch", "-q", "-c", "rb/s5-ai/m1", "rb/integrate/m1")
    _git(toy, "rm", "-q", "api/app/main.py")
    _git(toy, "commit", "-q", "-m", "delete S4 file")
    _git(toy, "switch", "-q", "main")
    r = _check(toy, "rb/s5-ai/m1")
    assert r.returncode == 1 and "NOT S5: api/app/main.py (owner: S4)" in r.stdout
