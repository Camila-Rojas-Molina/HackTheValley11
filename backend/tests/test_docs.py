"""The docs teammates build on must not state untested thresholds as facts, and must give
commands that run on the team's machines."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "docs" / "API.md"


def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def test_api_md_labels_the_sensor_thresholds_as_untested_assumptions():
    text = API.read_text(encoding="utf-8")
    hr = [p for p in _paragraphs(text) if "30 bpm" in p]
    still = [line for line in text.splitlines() if "45 mg" in line]
    assert hr and still
    for chunk in hr + still:
        assert "untested" in chunk and "assumption" in chunk, chunk


def test_readme_commands_use_the_project_venv_python():
    """macOS here has no bare `python`, and the system python3 lacks the project's packages."""
    bare = re.compile(r"(^|[\s`'\"(])python3?\s")
    found = []
    for path in (REPO / "README.md", REPO / "backend" / "README.md"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if bare.search(line):
                found.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert not found, "\n".join(found)
    usage = (REPO / "backend" / "scripts" / "migrate.py").read_text(encoding="utf-8").split('"""')[1]
    assert not bare.search(usage), usage
