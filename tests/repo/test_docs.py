"""The docs teammates build on: a short README whose commands run on the team's machines, and an
ARCHITECTURE.md that carries the PLAN 2.1 diagram, the rule under it and the contracts index."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
ARCH = REPO / "ARCHITECTURE.md"
LABELS = json.loads((REPO / "contracts" / "labels.json").read_text(encoding="utf-8"))
RULE = "AI never computes the number. Physics does; AI finds the bone and explains the result."

# A command that starts with a bare interpreter or pip: macOS here has no bare `python`, and the system
# python3 lacks the project's packages (and must never be installed into).
BARE = re.compile(r"(?:^|(?<=[\s`'\"(;&|]))(?<!-m )(python3?(\.\d+)?|pip3?)\s")


def _tracked() -> list[str]:
    return subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                          check=True).stdout.split()


def test_readme_is_100_lines_or_fewer():
    n = len(README.read_text(encoding="utf-8").splitlines())
    assert n <= 100, f"README.md has {n} lines"


def test_readme_commands_use_the_project_venv_python():
    found = [f"README.md:{i}: {line.strip()}"
             for i, line in enumerate(README.read_text(encoding="utf-8").splitlines(), 1) if BARE.search(line)]
    assert not found, "\n".join(found)


def test_readme_runs_python_only_through_the_venv():
    text = README.read_text(encoding="utf-8")
    assert ".venv/bin/python" in text
    assert "scripts/bootstrap.sh" in text


@pytest.mark.parametrize("doc", ["README.md", "ARCHITECTURE.md"])
def test_doc_carries_the_status_line_and_the_attribution(doc):
    text = (REPO / doc).read_text(encoding="utf-8")
    assert LABELS["status_line"] in text
    assert LABELS["attribution"]["short"] in text


def test_architecture_has_the_diagram_and_the_rule():
    text = ARCH.read_text(encoding="utf-8")
    for marker in ("Browser (web/", "FastAPI (api/)", "AI layer (api/app/ai/)", "worker/ (separate process)",
                   "Bone Finder", "physics (NOT AI)", "Tiger Data", "Hosting: Vultr"):
        assert marker in text, marker
    assert RULE in text


def test_architecture_indexes_every_contract_file():
    text = ARCH.read_text(encoding="utf-8")
    contracts = sorted(p.name for p in (REPO / "contracts").iterdir() if p.is_file())
    missing = [c for c in contracts if f"contracts/{c}" not in text]
    assert not missing, missing
    for c in ("C1", "C2", "C3", "C4", "C5", "C6", "C7"):
        assert f"| {c} |" in text, c


def test_markdown_links_in_the_docs_resolve():
    bad = []
    for doc in (README, ARCH):
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("mailto:"):
                continue
            if not (doc.parent / target).exists():
                bad.append(f"{doc.name}: {target}")
    assert not bad, bad


def test_no_planning_docs_are_tracked():
    """PLAN.md, GOAL.md, NAMES.md and the research reports never go into this public repo."""
    bad = [f for f in _tracked()
           if Path(f).name in {"PLAN.md", "GOAL.md", "NAMES.md"} or re.search(r"(^|/)(research|planning|m1-prep)/", f)]
    assert not bad, bad
