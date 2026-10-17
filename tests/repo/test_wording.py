"""Wording lint (GOAL Constraints, science.md 6): no claim the product cannot back.

The banned patterns live in contracts/labels.json `wording.banned_claims` (the single source the UI, this lint
and the AI checker share). The lint runs over README.md, ARCHITECTURE.md, labels.json (minus the lists that
quote banned words on purpose) and the visible text of web/: Markdown in full, JSON string values, and in
code only string literals and JSX text, so a field name such as `verified` from the API schema is not text.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LABELS = json.loads((REPO / "contracts" / "labels.json").read_text(encoding="utf-8"))
BANNED = [re.compile(p, re.I) for p in LABELS["wording"]["banned_claims"]]
WEB_SKIP_DIRS = {"node_modules", ".next", "out", "build", "dist", "coverage", ".turbo"}
WEB_SKIP_FILES = {"package-lock.json", "next-env.d.ts", "tsconfig.json", "components.json", "package.json"}
CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}


def schema_field_names() -> set[str]:
    """Every field name declared in api/app/schemas.py: a string literal equal to one is a key, not text."""
    tree = ast.parse((REPO / "api" / "app" / "schemas.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names.add(stmt.target.id)
    return names


FIELD_NAMES = schema_field_names()


def hits(text: str) -> list[str]:
    return [m.group(0) for rx in BANNED for m in rx.finditer(text)]


def labels_text() -> list[str]:
    data = json.loads(json.dumps(LABELS))
    data.pop("wording")
    data["ai"].pop("blocked_output_patterns")
    data["ai"].pop("number_words")
    out: list[str] = []

    def walk(v):
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                out.append(str(k))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data)
    return out


STRING_RX = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`", re.S)
JSX_TEXT_RX = re.compile(r">([^<>{}]*[A-Za-z][^<>{}]*)<")
COMMENT_RX = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def code_text(src: str) -> list[str]:
    """String literals and JSX text of a TS/JS file; comments are text too (they ship in the public repo)."""
    out = [m.group(0) for m in COMMENT_RX.finditer(src)]
    for m in STRING_RX.finditer(src):
        s = m.group(0)[1:-1]
        if s in FIELD_NAMES:
            continue
        out.append(s)
    out += [m.group(1) for m in JSX_TEXT_RX.finditer(src)]
    return out


def json_values(v) -> list[str]:
    if isinstance(v, str):
        return [] if v in FIELD_NAMES else [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in json_values(x)]
    if isinstance(v, list):
        return [s for x in v for s in json_values(x)]
    return []


def web_texts() -> list[tuple[str, str]]:
    web = REPO / "web"
    if not web.is_dir():
        return []
    out = []
    for p in sorted(web.rglob("*")):
        rel = p.relative_to(REPO)
        if not p.is_file() or WEB_SKIP_DIRS & set(rel.parts) or p.name in WEB_SKIP_FILES:
            continue
        if p.suffix in CODE_EXT:
            out += [(str(rel), t) for t in code_text(p.read_text(encoding="utf-8", errors="replace"))]
        elif p.suffix in {".md", ".mdx", ".html", ".txt"}:
            out.append((str(rel), p.read_text(encoding="utf-8", errors="replace")))
        elif p.suffix == ".json":
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            out += [(str(rel), t) for t in json_values(data)]
    return out


@pytest.mark.parametrize("doc", ["README.md", "ARCHITECTURE.md"])
def test_docs_make_no_banned_claim(doc):
    assert not hits((REPO / doc).read_text(encoding="utf-8"))


def test_labels_make_no_banned_claim():
    bad = [(t, h) for t in labels_text() for h in hits(t)]
    assert not bad, bad


def test_web_text_makes_no_banned_claim():
    bad = [(f, h, t[:80]) for f, t in web_texts() for h in hits(t)]
    assert not bad, bad


@pytest.mark.parametrize("claim", [
    "RainbowBone predicts fractures", "it predicts pathological fracture", "Fracture Risk Index",
    "a 40% chance of collapse", "12 % probability", "the model diagnoses myeloma", "clinically validated",
    "results verified by the solver", "FDA-ready", "Health Canada ready", "replaces SINS", "replaces Mirels",
    "GPU-accelerated physics", "AI predicts", "the spine is unsafe", "needs surgery",
    "indicating a positive response to the current therapy", "risk of fracture", "accurate to 2%",
])
def test_lint_catches_each_banned_claim(claim):
    assert hits(claim), claim


@pytest.mark.parametrize("ok", LABELS["wording"]["say"] + [
    LABELS["status_line"], "not verified", "Not for clinical use or diagnosis.", "numbers match computed values",
])
def test_lint_allows_the_approved_wording(ok):
    assert not hits(ok), hits(ok)


def test_code_text_ignores_schema_field_names_but_reads_visible_text():
    src = 'const v = g.verified; const k = "verified"; const t = "Results verified"; <p>AI predicts it</p>'
    texts = code_text(src)
    assert "verified" not in texts
    assert hits(" ".join(texts)) == ["verified", "AI predicts"]
