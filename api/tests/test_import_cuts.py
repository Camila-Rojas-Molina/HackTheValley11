"""Push 1 import cuts: the moved StillHere modules import cleanly and name no module Push 1 deleted."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

API = Path(__file__).resolve().parents[1]

# backend/app/{states,engine,rules,sensors,telegram,messages,timers,care}.py and
# backend/app/routes/{device,hr,sessions,contacts,demo,care,telemetry}.py (PLAN M1 "Deleted").
DELETED_APP = {"states", "engine", "rules", "sensors", "telegram", "messages", "timers", "care"}
DELETED_ROUTES = {"device", "hr", "sessions", "contacts", "demo", "care", "telemetry"}

MOVED = ["app.main", "app.config", "app.db", "app.deps", "app.security", "app.audit", "app.tiger", "app.jobs",
         "app.routes.health", "scripts.migrate", "scripts.run", "tests.contract_routes",
         "tests.livesrv"]


@pytest.mark.parametrize("module", MOVED)
def test_moved_module_imports(module):
    importlib.import_module(module)


def _s4_python_files() -> list[Path]:
    """Every .py file S4 owns under api/ (api/app/ai and api/tests/ai are S5's)."""
    out = []
    for p in sorted(API.rglob("*.py")):
        rel = p.relative_to(API).as_posix()
        if rel.startswith(("app/ai/", "tests/ai/", ".venv/")) or "/site-packages/" in rel:
            continue
        out.append(p)
    return out


def _imported(tree: ast.AST, module_path: str) -> set[str]:
    """Fully qualified names of the modules a file imports, relative imports resolved against `module_path`."""
    pkg = module_path.rsplit(".", 1)[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                parent = ".".join(parts[: len(parts) - (node.level - 1)])
                base = f"{parent}.{base}" if base else parent
            names.add(base)
            names |= {f"{base}.{a.name}" for a in node.names}
    return names


def test_no_file_imports_a_deleted_module():
    deleted = {f"app.{m}" for m in DELETED_APP} | {f"app.routes.{m}" for m in DELETED_ROUTES}
    offenders = {}
    files = _s4_python_files()
    assert any(p.name == "main.py" for p in files)
    for p in files:
        rel = p.relative_to(API).with_suffix("")
        module_path = ".".join(rel.parts)
        if p.name == "__init__.py":
            module_path += ".__init__"
        hits = _imported(ast.parse(p.read_text(encoding="utf-8")), module_path) & deleted
        if hits:
            offenders[str(rel)] = sorted(hits)
    assert offenders == {}


def test_deleted_modules_are_gone():
    for m in DELETED_APP:
        assert not (API / "app" / f"{m}.py").exists()
    for m in DELETED_ROUTES:
        assert not (API / "app" / "routes" / f"{m}.py").exists()
