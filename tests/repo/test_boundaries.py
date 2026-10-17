"""Import boundaries (PLAN M1, contracts/pipeline.md): the pipeline knows nothing about the web, the database or
the AI layer, and physics never imports AI. Checked statically (AST), so no package needs to be installed."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PIPELINE = REPO / "pipeline"

NO_WEB_DB_AI = {"fastapi", "starlette", "uvicorn", "psycopg", "psycopg2", "psycopg_pool", "pgserver", "app"}
AI_MODULES = {"app", "google", "openai", "groq", "anthropic", "mlx_lm", "transformers", "torch",
              "totalsegmentator", "rb_pipeline.segment", "api"}


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imports(path: Path, root: Path) -> set[str]:
    """Absolute dotted names a file imports; relative imports are resolved against its package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mod = module_name(path, root)
    pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                prefix = ".".join(base)
                target = f"{prefix}.{node.module}" if node.module else prefix
                out.add(target)
                out |= {f"{target}.{a.name}" for a in node.names}
            elif node.module:
                out.add(node.module)
                out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def violations(files: list[Path], root: Path, banned: set[str]) -> list[str]:
    bad = []
    for f in files:
        for name in sorted(imports(f, root)):
            if any(name == b or name.startswith(b + ".") for b in banned):
                bad.append(f"{f.relative_to(root)}: imports {name}")
    return bad


def py_files(d: Path) -> list[Path]:
    return sorted(p for p in d.rglob("*.py") if ".venv" not in p.parts and "__pycache__" not in p.parts)


def test_pipeline_imports_no_web_db_or_ai():
    assert not violations(py_files(PIPELINE), PIPELINE, NO_WEB_DB_AI)


@pytest.mark.parametrize("stage", ["physics", "export"])
def test_physics_and_export_never_import_ai(stage):
    files = py_files(PIPELINE / "rb_pipeline" / stage) + py_files(PIPELINE / "tests" / stage) \
        if (PIPELINE / "tests" / stage).is_dir() else py_files(PIPELINE / "rb_pipeline" / stage)
    assert files
    assert not violations(files, PIPELINE, AI_MODULES)


def test_schemas_import_only_pydantic_and_the_standard_library_at_module_level():
    tree = ast.parse((REPO / "api" / "app" / "schemas.py").read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            top.add(node.module.split(".")[0])
    assert top <= {"__future__", "datetime", "typing", "pydantic"}, top


def test_the_checker_catches_a_forbidden_import(tmp_path):
    pkg = tmp_path / "rb_pipeline" / "physics"
    pkg.mkdir(parents=True)
    (tmp_path / "rb_pipeline" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("from ..segment import segment\nimport app.ai.chain\nfrom google import genai\n")
    bad = violations([pkg / "__init__.py"], tmp_path, AI_MODULES)
    assert len(bad) >= 3, bad
