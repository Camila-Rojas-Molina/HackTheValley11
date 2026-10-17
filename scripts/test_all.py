"""Suite runner behind scripts/test_all.sh (standard library only).

Suite registration: one TOML file per stream, suites/<stream>.toml (s0.toml, s1.toml, ...), owned by that
stream (contracts/ownership.json). Canonical form:

    stream = "S4"                      # optional; default from the file name (s4.toml -> S4)

    [suites.unit]                      # the suite name test_all.sh is called with
    what = "API units, no database"    # one line for the table
    run = ".venv/bin/python -m pytest -q api/tests -m unit"   # shell command, run from the repo root
    cwd = "."                          # optional, relative to the repo root
    timeout = 900                      # optional, seconds (default 1800)
    kind = "unit"                      # optional: `test_all.sh unit` also runs suites of this kind
    setup = "npm ci"                   # optional: shell command(s) run first, in cwd; a failure is a FAIL
    requires = ["web/package.json"]    # optional: each entry is a path that exists or a command on PATH, else SKIP
    requires_cmd = ["npm"]             # optional: commands that must be on PATH, else SKIP
    requires_env = ["RB_DATA_DIR"]     # optional: env vars that must be set, else SKIP
    requires_docker = false            # optional: true -> SKIP unless "${DOCKER:-$HOME/.docker/bin/docker}" info works
    skip_reason = ""                   # optional: non-empty -> always SKIP with this reason
    env = { AI_CHAIN = "template" }    # optional extra environment

`run` may also be a list of shell commands, run in order (the first non-zero exit is a FAIL). Also accepted:
`[[suite]]` / `[[suites]]` arrays with a `name` key (and an optional per-suite `stream`), `cmd` / `command`
for `run` (a list there is ONE argv, not a sequence), `dir` for `cwd`, `description` for `what`.

Several streams may register the same suite name (e.g. `unit` from S4 and S5): each registration is its own
row; `--stream S<N>` first keeps only that stream's registrations. A pytest exit code 5 (no tests collected) is a FAIL. The last run is saved to
<git-dir>/rb-test-state.json for scripts/watchdog.sh.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITES_DIR = REPO / "suites"
DEFAULT_TIMEOUT = 1800


@dataclass
class Suite:
    name: str
    stream: str
    source: str
    run: str | list[str] | None             # as written, for --list
    steps: list[str | list[str]] = field(default_factory=list)   # str = shell command, list = one argv
    kind: str = ""
    what: str = ""
    cwd: str = "."
    timeout: int = DEFAULT_TIMEOUT
    requires: list[str] = field(default_factory=list)
    requires_cmd: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)
    requires_docker: bool = False
    skip_reason: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Row:
    suite: Suite
    status: str
    seconds: float
    detail: str
    log: Path | None = None


def _steps(d: dict) -> list[str | list[str]]:
    steps: list[str | list[str]] = []
    setup = d.get("setup")
    if isinstance(setup, str) and setup:
        steps.append(setup)
    elif isinstance(setup, list):
        steps += [str(x) for x in setup]
    if "run" in d:
        run = d["run"]
        steps += [run] if isinstance(run, str) and run else [str(x) for x in run] if isinstance(run, list) else []
    else:
        cmd = d.get("cmd", d.get("command"))
        if isinstance(cmd, str) and cmd:
            steps.append(cmd)
        elif isinstance(cmd, list) and cmd:
            steps.append([str(x) for x in cmd])
    return steps


def _entry(name: str, d: dict, stream: str, source: str) -> Suite:
    run = d.get("run", d.get("cmd", d.get("command")))
    return Suite(
        name=str(name), stream=str(d.get("stream", stream)).upper(), source=source, run=run, steps=_steps(d),
        kind=str(d.get("kind", "")),
        what=str(d.get("what", d.get("description", ""))),
        cwd=str(d.get("cwd", d.get("dir", "."))),
        timeout=int(d.get("timeout", DEFAULT_TIMEOUT)),
        requires=list(d.get("requires", [])),
        requires_cmd=list(d.get("requires_cmd", [])),
        requires_env=list(d.get("requires_env", [])),
        requires_docker=bool(d.get("requires_docker", False)),
        skip_reason=str(d.get("skip_reason", "") or ""),
        env={str(k): str(v) for k, v in dict(d.get("env", {})).items()},
    )


def load_suites(suites_dir: Path = SUITES_DIR) -> tuple[list[Suite], list[str]]:
    """Every registration in suites/*.toml, in file order; plus parse errors (each one is a FAIL row)."""
    suites: list[Suite] = []
    errors: list[str] = []
    for path in sorted(suites_dir.glob("*.toml")):
        rel = str(path.relative_to(suites_dir.parent)) if suites_dir.parent in path.parents else path.name
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            errors.append(f"{rel}: {e}")
            continue
        stream = str(data.get("stream", path.stem)).upper()
        found = 0
        for key in ("suite", "suites"):
            block = data.get(key)
            if isinstance(block, list):
                for d in block:
                    if isinstance(d, dict) and "name" in d:
                        suites.append(_entry(d["name"], d, stream, rel))
                        found += 1
            elif isinstance(block, dict):
                for name, d in block.items():
                    if isinstance(d, dict):
                        suites.append(_entry(name, d, stream, rel))
                        found += 1
        for name, d in data.items():   # bare [name] tables with a command
            if name not in ("suite", "suites") and isinstance(d, dict) and \
                    any(k in d for k in ("run", "cmd", "command")):
                suites.append(_entry(name, d, stream, rel))
                found += 1
        if not found:
            errors.append(f"{rel}: no suites found")
    return suites, errors


def docker_ok() -> bool:
    docker = os.environ.get("DOCKER") or str(Path.home() / ".docker" / "bin" / "docker")
    if not Path(docker).exists() and not shutil.which(docker):
        return False
    try:
        return subprocess.run([docker, "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def skip_reason(s: Suite, repo: Path) -> str | None:
    if s.skip_reason:
        return s.skip_reason
    if not s.steps:
        return None
    for p in s.requires:
        if not (repo / p).exists() and not ("/" not in p and shutil.which(p)):
            return f"{p} not present (no such path or command)"
    for c in s.requires_cmd:
        if not shutil.which(c):
            return f"{c} not on PATH"
    for e in s.requires_env:
        if not os.environ.get(e):
            return f"{e} not set"
    if s.requires_docker and not docker_ok():
        return "Docker not available (\"${DOCKER:-$HOME/.docker/bin/docker}\" info failed)"
    return None


def last_summary(text: str) -> str:
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("=").strip()
        if line:
            return line[:110]
    return ""


def run_suite(s: Suite, repo: Path, logs: Path) -> Row:
    reason = skip_reason(s, repo)
    if reason is not None:
        return Row(s, "SKIP", 0.0, reason)
    if not s.steps:
        return Row(s, "FAIL", 0.0, f"{s.source}: suite '{s.name}' has no run command")
    log = logs / f"{s.stream}-{s.name}.log"
    env = {**os.environ, "RB_REPO": str(repo), **s.env}
    start = time.monotonic()
    rc, where = 0, ""
    with log.open("w") as fh:
        for i, step in enumerate(s.steps, 1):
            cmd = step if isinstance(step, list) else ["bash", "-c", step]
            fh.write(f"$ {step if isinstance(step, str) else shlex.join(step)}\n")
            fh.flush()
            left = s.timeout - (time.monotonic() - start)
            try:
                rc = subprocess.run(cmd, cwd=repo / s.cwd, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                    timeout=max(left, 1)).returncode
            except subprocess.TimeoutExpired:
                return Row(s, "FAIL", time.monotonic() - start, f"timed out after {s.timeout} s", log)
            except OSError as e:
                return Row(s, "FAIL", time.monotonic() - start, f"could not start: {e}", log)
            if rc != 0:
                where = f"step {i}/{len(s.steps)} " if len(s.steps) > 1 else ""
                break
    secs = time.monotonic() - start
    out = log.read_text(errors="replace")
    if rc == 0:
        return Row(s, "PASS", secs, last_summary(out), log)
    if rc == 5 and "no tests ran" in out:
        return Row(s, "FAIL", secs, f"{where}no tests collected (pytest exit 5)", log)
    return Row(s, "FAIL", secs, f"{where}exit {rc}: {last_summary(out)}", log)


def git_dir(repo: Path) -> Path | None:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def save_state(repo: Path, args: list[str], rows: list[Row]) -> None:
    gd = git_dir(repo)
    if gd is None:
        return
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    state = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "head": head, "args": args,
             "rows": [{"suite": r.suite.name, "stream": r.suite.stream, "status": r.status,
                       "seconds": round(r.seconds, 1), "detail": r.detail} for r in rows]}
    try:
        (gd / "rb-test-state.json").write_text(json.dumps(state, indent=1) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    suites, errors = load_suites()
    stream_args: list[str] = []
    if argv[:1] == ["--stream"]:   # --stream S0: only that stream's registrations (ci-core runs S0's alone)
        if len(argv) < 2 or argv[1].startswith("-"):
            print("usage: scripts/test_all.sh [--stream S<N>] all | <suite> [<suite> ...] | --list", file=sys.stderr)
            return 2
        stream_args = argv[:2]
        suites = [s for s in suites if s.stream.upper() == argv[1].upper()]
        argv = argv[2:]
    if argv[:1] == ["--list"]:
        for s in suites:
            cmd = shlex.join(s.run) if isinstance(s.run, list) else s.run
            kind = f" (kind {s.kind})" if s.kind and s.kind != s.name else ""
            print(f"{s.name:<12} {s.stream:<4} {s.source:<16} {s.what or cmd}{kind}")
        for e in errors:
            print(f"ERROR {e}")
        return 1 if errors else 0
    if any(a.startswith("-") for a in argv):
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        print("usage: scripts/test_all.sh [--stream S<N>] all | <suite> [<suite> ...] | --list", file=sys.stderr)
        return 2
    names = argv or ["all"]
    if names == ["all"]:
        chosen = list(suites)
        missing: list[str] = []
    else:
        chosen, missing = [], []
        for n in names:
            match = [s for s in suites if s.name == n or s.kind == n]
            if not match:
                missing.append(n)
            chosen.extend(x for x in match if all(x is not c for c in chosen))
    logs = Path(tempfile.mkdtemp(prefix="rb-tests-"))
    rows: list[Row] = []
    for s in chosen:
        print(f"test_all: {s.name} ({s.stream}) ...", flush=True)
        rows.append(run_suite(s, REPO, logs))
        r = rows[-1]
        print(f"test_all: {s.name} ({s.stream}) {r.status} {r.seconds:.1f}s", flush=True)
    for n in missing:
        rows.append(Row(Suite(n, "-", "-", None), "FAIL", 0.0, "no suite with this name in suites/*.toml"))
    for e in errors:
        rows.append(Row(Suite("(parse)", "-", "-", None), "FAIL", 0.0, e))

    print()
    print(f"{'suite':<12} {'stream':<6} {'status':<6} {'secs':>7}  detail")
    print("-" * 100)
    for r in rows:
        print(f"{r.suite.name:<12} {r.suite.stream:<6} {r.status:<6} {r.seconds:>7.1f}  {r.detail}")
    counts = {k: sum(r.status == k for r in rows) for k in ("PASS", "FAIL", "SKIP")}
    print("-" * 100)
    print(f"PASS {counts['PASS']}  FAIL {counts['FAIL']}  SKIP {counts['SKIP']}   logs: {logs}")
    for r in rows:
        if r.status == "FAIL" and r.log and r.log.exists():
            tail = r.log.read_text(errors="replace").splitlines()[-30:]
            print(f"\n--- {r.suite.name} ({r.suite.stream}) last {len(tail)} lines of {r.log}")
            print("\n".join(tail))
    save_state(REPO, stream_args + names, rows)
    if not rows:
        print("test_all: no suites registered in suites/*.toml")
        return 1
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
