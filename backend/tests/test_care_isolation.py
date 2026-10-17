"""Senior mode is fully separate from the KAZE check-in and escalation path.

Three checks: the static import closure of the care code, a fresh interpreter that imports the
care modules and lists what got loaded, and the external modules care.py may use (no network
client, no model SDK). test_care_db.py adds the runtime check: seeding and reading the care
endpoints leaves every KAZE table unchanged.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parent.parent
CARE_MODULES = ("app.care", "app.routes.care")
# Everything on the check-in / escalation path, and the helpers that pull it in.
KAZE_MODULES = {
    "app.engine", "app.states", "app.timers", "app.outbox", "app.telegram", "app.rules", "app.sensors",
    "app.audit", "app.messages", "app.deps", "app.main", "app.config", "app.db",
    "app.routes.sessions", "app.routes.device", "app.routes.contacts", "app.routes.demo", "app.routes.hr",
    "app.routes.health",
}


def module_path(name: str) -> Path:
    parts = name.split(".")
    file = BACKEND.joinpath(*parts).with_suffix(".py")
    return file if file.exists() else BACKEND.joinpath(*parts, "__init__.py")


def imports_of(name: str) -> set[str]:
    """Absolute names imported by module `name` (relative imports resolved)."""
    path = module_path(name)
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            out.add(prefix)
            for a in node.names:   # `from .. import care` names a module
                if module_path(f"{prefix}.{a.name}").exists():
                    out.add(f"{prefix}.{a.name}")
    return out


def app_closure(roots) -> set[str]:
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo += [m for m in imports_of(name) if m == "app" or m.startswith("app.")]
    return seen


def test_care_import_closure_never_reaches_the_escalation_path():
    closure = app_closure(CARE_MODULES)
    assert closure <= {"app", "app.routes", "app.care", "app.routes.care", "app.security"}, closure
    assert not closure & KAZE_MODULES


def test_fresh_interpreter_loads_no_kaze_module():
    code = ("import json, sys; import app.care, app.routes.care; "
            "print(json.dumps(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.'))))")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    loaded = set(json.loads(out.stdout.strip().splitlines()[-1]))
    assert loaded == {"app", "app.care", "app.routes", "app.routes.care", "app.security"}, loaded
    assert not loaded & KAZE_MODULES


def test_care_uses_no_network_client_or_model():
    externals = {m.split(".")[0] for m in imports_of("app.care") if not m.startswith("app")}
    assert externals <= {"__future__", "random", "statistics", "dataclasses", "datetime", "typing", "psycopg"}, \
        externals
    route_externals = {m.split(".")[0] for m in imports_of("app.routes.care") if not m.startswith("app")}
    assert route_externals <= {"__future__", "datetime", "typing", "fastapi", "pydantic"}, route_externals


def test_kaze_code_does_not_call_into_care():
    """Only main.py registers the router; no KAZE module imports the care code."""
    for name in KAZE_MODULES - {"app.main"}:
        assert not imports_of(name) & set(CARE_MODULES), name
    assert "app.routes.care" in imports_of("app.main")


def _request(admin_key, header=None):
    settings = SimpleNamespace(admin_key=admin_key)
    app = SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(settings=settings)))
    return SimpleNamespace(app=app, headers={"x-admin-key": header} if header else {})


def test_seed_admin_check():
    from app.routes.care import _require_admin
    with pytest.raises(HTTPException) as e:
        _require_admin(_request(None, "anything"))
    assert e.value.status_code == 503
    with pytest.raises(HTTPException) as e:
        _require_admin(_request("k", "wrong"))
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        _require_admin(_request("k"))
    _require_admin(_request("k", "k"))
