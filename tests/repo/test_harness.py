"""The build harness (PLAN M1 "Harness" and "Isolation"): test_all.sh finds every registered suite and reports
PASS/FAIL/SKIP with reasons, integrate.sh --dry-run lands a toy branch in a temporary clone, and two worktree
paths never share a database, PGDATA or container name."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import rb_env  # noqa: E402
import test_all  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def run(cmd: list[str], cwd: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV, "PYTHON": sys.executable, **env}, timeout=300)


@pytest.fixture(autouse=True)
def _own_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run clones and suite logs go under this test's tmp_path, not the shared TMPDIR."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))


def git(cwd: Path, *args: str) -> str:
    r = run(["git", *args], cwd)
    assert r.returncode == 0, r.stderr
    return r.stdout


# ------------------------------------------------------------------ test_all.sh
def test_this_repo_registers_the_s0_suites():
    suites, errors = test_all.load_suites(REPO / "suites")
    assert not errors, errors
    names = {(s.stream, s.name) for s in suites}
    assert {("S0", "repo"), ("S0", "contract")} <= names


def test_every_registered_suite_has_a_command_or_a_skip_reason():
    suites, _ = test_all.load_suites(REPO / "suites")
    for s in suites:
        assert s.run or s.skip_reason, (s.source, s.name)


@pytest.fixture()
def toy_runner(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    for f in ("test_all.sh", "test_all.py"):
        shutil.copy(REPO / "scripts" / f, tmp_path / "scripts" / f)
    (tmp_path / "suites").mkdir()
    (tmp_path / "suites" / "s0.toml").write_text(
        'stream = "S0"\n'
        '[suites.ok]\nwhat = "passes"\nrun = "echo 3 passed"\n'
        '[suites.later]\nrun = "false"\nskip_reason = "lands in M5"\n'
        '[suites.needs]\nrun = "false"\nrequires = ["web/package.json"]\n')
    (tmp_path / "suites" / "s4.toml").write_text(   # the tolerated draft form
        '[[suite]]\nname = "ok"\ncmd = ["true"]\n'
        '[[suite]]\nname = "bad"\ncommand = "exit 3"\n')
    (tmp_path / "suites" / "s1.toml").write_text(   # S1's form: setup, a list of shell commands, commands required
        '[[suite]]\nname = "web"\nstream = "S1"\ncwd = "sub"\nrequires = ["sh"]\n'
        'setup = "echo setup > ../setup.txt"\nrun = ["true", "echo 7 checks passed"]\n'
        '[[suite]]\nname = "webfail"\nrun = ["true", "exit 4", "touch ../never"]\n'
        '[[suite]]\nname = "nocmd"\nrun = "true"\nrequires = ["no-such-command-rb"]\n')
    (tmp_path / "suites" / "s5.toml").write_text(   # S5's form: argv list, selected by kind
        'stream = "S5"\n[[suite]]\nname = "ai"\nkind = "unit"\ncmd = ["sh", "-c", "echo 5 passed"]\n')
    (tmp_path / "sub").mkdir()
    git(tmp_path, "init", "-q")
    return tmp_path


def test_test_all_finds_every_registration_and_skips_with_reasons(toy_runner):
    r = run(["scripts/test_all.sh", "ok", "later", "needs"], toy_runner)
    assert r.returncode == 0, r.stdout + r.stderr
    table = r.stdout
    assert re.search(r"^ok +S0 +PASS .*3 passed", table, re.M)
    assert re.search(r"^ok +S4 +PASS", table, re.M)
    assert re.search(r"^later +S0 +SKIP .*lands in M5", table, re.M)
    assert re.search(r"^needs +S0 +SKIP .*web/package.json not present", table, re.M)
    assert "PASS 2  FAIL 0  SKIP 2" in table
    assert (toy_runner / ".git" / "rb-test-state.json").is_file()


def test_test_all_runs_setup_and_command_lists_and_selects_by_kind(toy_runner):
    r = run(["scripts/test_all.sh", "web", "unit", "ai"], toy_runner)
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^web +S1 +PASS .*7 checks passed", r.stdout, re.M)
    assert len(re.findall(r"^ai +S5 +PASS .*5 passed", r.stdout, re.M)) == 1, "a suite runs once per call"
    assert (toy_runner / "setup.txt").read_text() == "setup\n"


def test_a_command_list_stops_at_the_first_failure(toy_runner):
    r = run(["scripts/test_all.sh", "webfail", "nocmd"], toy_runner)
    assert r.returncode == 1
    assert re.search(r"^webfail +S1 +FAIL .*step 2/3 exit 4", r.stdout, re.M)
    assert not (toy_runner / "never").exists()
    assert re.search(r"^nocmd +S1 +SKIP .*no-such-command-rb not present", r.stdout, re.M)


def test_test_all_fails_on_a_failing_or_unregistered_suite(toy_runner):
    r = run(["scripts/test_all.sh", "bad", "e2e"], toy_runner)
    assert r.returncode == 1
    assert re.search(r"^bad +S4 +FAIL +\S+ +exit 3", r.stdout, re.M)
    assert re.search(r"^e2e +- +FAIL .*no suite with this name", r.stdout, re.M)


def test_stream_filter_keeps_one_streams_registrations(toy_runner):
    r = run(["scripts/test_all.sh", "--stream", "S0", "ok", "later"], toy_runner)
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"^ok +S0 +PASS", r.stdout, re.M)
    assert not re.search(r"^ok +S4 ", r.stdout, re.M)
    assert "PASS 1  FAIL 0  SKIP 1" in r.stdout


def test_ci_core_runs_only_s0_suites():
    """ci-core installs only scripts/requirements-repo.txt, so it must not pick up S4's `contract` suite."""
    wf = (REPO / ".github" / "workflows" / "ci-core.yml").read_text()
    runs = re.findall(r"run: (scripts/test_all\.sh .*)", wf)
    assert runs and all(cmd.split()[1:3] == ["--stream", "S0"] for cmd in runs), runs


def test_test_all_runs_from_any_directory(toy_runner):
    r = run([str(toy_runner / "scripts" / "test_all.sh"), "ok"], toy_runner / "suites")
    assert r.returncode == 0, r.stdout


def test_no_tests_collected_is_a_failure(tmp_path):
    (tmp_path / "scripts").mkdir()
    for f in ("test_all.sh", "test_all.py"):
        shutil.copy(REPO / "scripts" / f, tmp_path / "scripts" / f)
    (tmp_path / "suites").mkdir()
    (tmp_path / "empty").mkdir()
    (tmp_path / "suites" / "s9.toml").write_text(
        f'[suites.empty]\nrun = "{sys.executable} -m pytest -q -p no:cacheprovider empty"\n')
    r = run(["scripts/test_all.sh", "all"], tmp_path)
    assert r.returncode == 1 and "no tests collected" in r.stdout


# ------------------------------------------------------------------ integrate.sh --dry-run
@pytest.fixture()
def toy_repo(tmp_path: Path) -> Path:
    """main with the S0 tooling; rb/integrate/m1 = main + a mechanical commit; three stream branches."""
    repo = tmp_path / "toy"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "scripts").mkdir()
    for f in ("integrate.sh", "check_ownership.py", "test_all.sh", "test_all.py"):
        shutil.copy(REPO / "scripts" / f, repo / "scripts" / f)
    (repo / "contracts").mkdir()
    shutil.copy(REPO / "contracts" / "ownership.json", repo / "contracts" / "ownership.json")
    (repo / "suites").mkdir()
    # The suite also checks integrate.sh leaks no RB_INTEGRATE_* variable: a nested run must start fresh.
    (repo / "suites" / "s0.toml").write_text(
        "[suites.toy]\nrun = 'test -z \"${RB_INTEGRATE_SRC:-}${RB_INTEGRATE_COPY:-}\" "
        "&& test -f web/page.tsx && test -f api/app/x.py'\n")
    (repo / "old.txt").write_text("StillHere\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "switch", "-q", "-c", "rb/integrate/m1")
    git(repo, "rm", "-q", "old.txt")
    git(repo, "commit", "-q", "-m", "mechanical")
    for branch, path in (("rb/s1-web/m1", "web/page.tsx"), ("rb/s4-api/m1", "api/app/x.py"),
                         ("rb/s0/m1", "README.md")):
        git(repo, "switch", "-q", "-c", branch, "rb/integrate/m1")
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(f"{branch}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", branch)
    git(repo, "switch", "-q", "main")
    return repo


def test_integrate_dry_run_lands_the_toy_branches_in_a_temporary_clone(toy_repo):
    before = {ref: git(toy_repo, "rev-parse", ref) for ref in ("main", "rb/integrate/m1")}
    tags_before = git(toy_repo, "tag")
    r = run(["scripts/integrate.sh", "m1", "--dry-run"], toy_repo)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    order = [out.index(f"{b}: integrated") for b in ("rb/s0/m1", "rb/s4-api/m1", "rb/s1-web/m1")]
    assert order == sorted(order), "landing order is S0, S4, S5, S2, S3, S1, S6"
    assert "DRY RUN: not pushed" in out
    assert re.search(r"next deadline|every deadline has passed", out)
    for tag in ("landed/S0/m1", "landed/S1/m1", "landed/S4/m1"):
        assert f"tagged {tag}" in out
    clone = Path(re.search(r"DRY RUN OK: \w+ on rb/land/m1 in (\S+)", out).group(1))
    log = git(clone, "log", "--format=%s", "main..rb/land/m1").splitlines()
    assert log == ["RainbowBone: remove StillHere code; add skeleton, contracts and architecture"]
    files = set(git(clone, "ls-files").split())
    assert {"web/page.tsx", "api/app/x.py", "README.md"} <= files and "old.txt" not in files
    assert {ref: git(toy_repo, "rev-parse", ref) for ref in before} == before, "the source repo was changed"
    assert git(toy_repo, "tag") == tags_before


def test_integrate_stops_when_a_branch_leaves_its_globs(toy_repo):
    git(toy_repo, "switch", "-q", "rb/s1-web/m1")
    (toy_repo / "api" / "app").mkdir(parents=True)
    (toy_repo / "api" / "app" / "main.py").write_text("# S4's file\n")
    git(toy_repo, "add", "-A")
    git(toy_repo, "commit", "-q", "-m", "S1 edits an S4 file")
    git(toy_repo, "switch", "-q", "main")
    r = run(["scripts/integrate.sh", "m1", "--dry-run"], toy_repo)
    assert r.returncode == 1
    assert "NOT S1: api/app/main.py (owner: S4)" in r.stdout
    assert "STOP" in r.stderr


def test_integrate_stops_when_the_suite_fails(toy_repo):
    r = run(["scripts/integrate.sh", "m1", "--dry-run", "--branches", "rb/s1-web/m1"], toy_repo)
    assert r.returncode == 1
    assert "suites failed" in r.stderr


# ------------------------------------------------------------------ isolation
def test_two_worktrees_get_different_names(tmp_path):
    a, b = tmp_path / "rb-wt" / "s1-web", tmp_path / "rb-wt" / "s4-api"
    assert rb_env.test_db_name(a) != rb_env.test_db_name(b)
    assert rb_env.pgdata_dir(a) != rb_env.pgdata_dir(b)
    assert rb_env.tsdb_container(a) != rb_env.tsdb_container(b)
    assert re.fullmatch(r"rb_test_[0-9a-f]{8}", rb_env.test_db_name(a))
    assert rb_env.pgdata_dir(a) == a.resolve() / ".pgdata"
    assert rb_env.test_db_name(a) == rb_env.test_db_name(a / "x" / "..")


def test_free_port_is_bindable():
    import socket
    port = rb_env.free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


# ------------------------------------------------------------------ bootstrap, watchdog, donecheck
def test_bootstrap_refuses_a_python_that_is_not_3_12(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "bootstrap.sh", tmp_path / "scripts" / "bootstrap.sh")
    fake = tmp_path / "fakepy"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    r = run(["bash", "scripts/bootstrap.sh", "--only", "repo"], tmp_path, RB_PYTHON=str(fake))
    assert r.returncode == 2 and "not Python 3.12" in r.stderr
    assert not (tmp_path / ".venv").exists()


@pytest.mark.parametrize("script", ["bootstrap.sh", "test_all.sh", "integrate.sh", "watchdog.sh", "donecheck/m1.sh",
                                    "dev.sh", "donecheck/m2.sh", "donecheck/m3.sh"])
def test_scripts_are_executable_bash(script):
    p = REPO / "scripts" / script
    assert os.access(p, os.X_OK), p
    assert p.read_text().startswith("#!/usr/bin/env bash\n")
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0


def test_watchdog_reports_this_worktree():
    r = run(["scripts/watchdog.sh"], REPO)
    assert r.returncode == 0
    assert re.search(r"last commit \d+ min ago", r.stdout), r.stdout


def test_integrate_real_mode_against_a_throwaway_origin(toy_repo, tmp_path):
    """Steps 1-3, a fix on a stream branch integrated as a delta, then --land: one fast-forward commit on the
    (temporary, bare) origin's main, and each landed/S<N>/m1 tag at the tip that was integrated and tested."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(toy_repo, "remote", "add", "origin", str(origin))
    git(toy_repo, "push", "-q", "origin", "main")
    r = run(["scripts/integrate.sh", "m1"], toy_repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "4. review (not mechanical)" in r.stdout and "--land" in r.stdout

    git(toy_repo, "switch", "-q", "rb/s1-web/m1")
    (toy_repo / "web" / "page.tsx").write_text("fixed after review\n")
    git(toy_repo, "commit", "-q", "-am", "S1 review fix")
    integrated_tip = git(toy_repo, "rev-parse", "HEAD").strip()
    git(toy_repo, "switch", "-q", "rb/integrate/m1")
    r = run(["scripts/integrate.sh", "m1"], toy_repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rb/s1-web/m1: integrated" in r.stdout and "delta since" in r.stdout
    assert "rb/s4-api/m1: already integrated" in r.stdout

    git(toy_repo, "switch", "-q", "rb/s1-web/m1")      # work continues after the tested integration
    (toy_repo / "web" / "next.tsx").write_text("M3 work\n")
    git(toy_repo, "add", "-A")
    git(toy_repo, "commit", "-q", "-m", "S1 M3 work")
    git(toy_repo, "switch", "-q", "rb/integrate/m1")
    r = run(["scripts/integrate.sh", "m1", "--land", "--no-ci"], toy_repo)
    assert r.returncode == 0, r.stdout + r.stderr
    log = git(origin, "log", "--format=%s", "main").splitlines()
    assert log == ["RainbowBone: remove StillHere code; add skeleton, contracts and architecture", "base"]
    assert git(toy_repo, "rev-parse", "landed/S1/m1^{commit}").strip() == integrated_tip
    assert "git rebase --onto origin/main landed/S1/m1 rb/s1-web/m1" in r.stdout
    files = git(origin, "ls-tree", "-r", "--name-only", "main").split()
    assert "web/next.tsx" not in files and "web/page.tsx" in files
