"""Check contracts/ownership.json (PLAN 3.1): who may edit which path.

    .venv/bin/python scripts/check_ownership.py --all
        every tracked file has exactly one owning stream
    .venv/bin/python scripts/check_ownership.py --branch rb/s4-api/m1 [--base <ref>]
        every file the branch changed since its merge base with <ref> belongs to the branch's stream
        (default base: rb/integrate/<milestone>, else origin/main, else main)
    .venv/bin/python scripts/check_ownership.py --owner <path> [<path> ...]
        print the owning stream(s) of each path

Standard library only. Exit 0 = OK, 1 = a violation, 2 = usage or git error.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRANCH_RE = re.compile(r"^rb/(s\d+)(?:-[A-Za-z0-9._-]+)?/([A-Za-z0-9._-]+)$")


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """ownership.json glob semantics: '**' = zero or more whole segments, '*' and '?' never cross '/'."""
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            out.append(r"(?:/.*)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(r".*")
            i += 2
        elif glob[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Ownership:
    def __init__(self, data: dict):
        self.data = data
        self.rx = {s: ([glob_to_regex(g) for g in d["include"]], [glob_to_regex(g) for g in d.get("exclude", [])])
                   for s, d in data["streams"].items()}

    @classmethod
    def load(cls, repo: Path = REPO) -> "Ownership":
        return cls(json.loads((repo / "contracts" / "ownership.json").read_text(encoding="utf-8")))

    def owners(self, path: str) -> list[str]:
        return [s for s, (inc, exc) in self.rx.items()
                if any(r.match(path) for r in inc) and not any(r.match(path) for r in exc)]

    def exceptions(self, stream: str, milestone: str) -> set[str]:
        return set(self.data.get("bootstrap_exceptions", {}).get(milestone, {}).get(stream, []))


def parse_branch(branch: str) -> tuple[str, str]:
    """rb/s1-web/m1 -> ("S1", "M1"). Raises ValueError for anything that is not a stream branch."""
    m = BRANCH_RE.match(branch)
    if not m or branch.startswith(("rb/integrate/", "rb/land/")):
        raise ValueError(f"not a stream branch: {branch!r} (expected rb/s<N>[-topic]/<milestone>)")
    return m.group(1).upper(), m.group(2).upper()


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                          capture_output=True).returncode == 0


def check_all(own: Ownership, repo: Path) -> int:
    files = git(repo, "ls-files", "-z").split("\0")
    unowned, overlap = [], []
    for f in sorted(x for x in files if x):
        o = own.owners(f)
        if not o:
            unowned.append(f)
        elif len(o) > 1:
            overlap.append((f, o))
    for f in unowned:
        print(f"UNOWNED: {f}")
    for f, o in overlap:
        print(f"OVERLAP {','.join(o)}: {f}")
    status = "OK" if not (unowned or overlap) else "FAIL"
    print(f"{status}: {len(unowned)} unowned, {len(overlap)} overlapping paths "
          f"({len([x for x in files if x])} tracked files)")
    return 0 if status == "OK" else 1


def default_base(repo: Path, milestone: str) -> str:
    for ref in (f"rb/integrate/{milestone.lower()}", "origin/main", "main"):
        if ref_exists(repo, ref):
            return ref
    raise RuntimeError("no base ref found; pass --base")


def check_branch(own: Ownership, repo: Path, branch: str, base: str | None, stream: str | None) -> int:
    s, milestone = parse_branch(branch)
    stream = (stream or s).upper()
    if stream not in own.data["streams"]:
        print(f"FAIL: unknown stream {stream} for {branch}")
        return 1
    base = base or default_base(repo, milestone)
    changed = [f for f in git(repo, "diff", "--name-only", "--no-renames", "-z", f"{base}...{branch}").split("\0")
               if f]
    allowed_extra = own.exceptions(stream, milestone)
    bad = []
    for f in sorted(changed):
        o = own.owners(f)
        if o == [stream] or f in allowed_extra:
            continue
        bad.append((f, o))
    for f, o in bad:
        print(f"NOT {stream}: {f} (owner: {','.join(o) or 'none'})")
    if bad:
        print(f"FAIL: {branch} ({stream}) touched {len(bad)} of {len(changed)} files outside its globs (base {base})")
        return 1
    extra = sorted(set(changed) & allowed_extra)
    note = f"; bootstrap exceptions {milestone}: {', '.join(extra)}" if extra else ""
    print(f"OK: {branch} ({stream}) touched {len(changed)} files, all its own (base {base}){note}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="every tracked file has exactly one owner")
    g.add_argument("--branch", help="a stream branch touched only its own globs")
    g.add_argument("--owner", nargs="+", metavar="PATH", help="print the owner(s) of each path")
    ap.add_argument("--base", help="base ref for --branch")
    ap.add_argument("--stream", help="override the stream parsed from the branch name (S0..S6)")
    ap.add_argument("--repo", type=Path, default=None, help="repository root (default: the current checkout)")
    ap.add_argument("--ownership", type=Path, default=None,
                    help="ownership map to use (default: <repo>/contracts/ownership.json)")
    a = ap.parse_args(argv)
    repo = a.repo or Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    own = Ownership(json.loads(a.ownership.read_text(encoding="utf-8"))) if a.ownership else Ownership.load(repo)
    try:
        if a.all:
            return check_all(own, repo)
        if a.branch:
            return check_branch(own, repo, a.branch, a.base, a.stream)
        rc = 0
        for p in a.owner:
            o = own.owners(p)
            print(f"{p}: {','.join(o) or 'UNOWNED'}")
            rc |= int(len(o) != 1)
        return rc
    except (RuntimeError, ValueError) as e:
        print(f"check_ownership: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
