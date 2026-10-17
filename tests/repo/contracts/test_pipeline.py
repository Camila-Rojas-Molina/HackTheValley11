"""C7: the pipeline signatures in pipeline/rb_pipeline/*/__init__.py match contracts/pipeline.md, and until
Galit's gate (G1) every stage is a stub: no segmentation, FE or physics compute exists in the repo."""
from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import re
from pathlib import Path

import pytest

import rb_pipeline as rp

REPO = Path(__file__).resolve().parents[3]
DOC = (REPO / "contracts" / "pipeline.md").read_text(encoding="utf-8")
STAGES = ("io", "segment", "longitudinal", "physics", "export")
G1_PASSED = False   # S0 flips this (a contract commit) when Galit confirms Step 1; then the stage owners add compute.


def doc_signatures() -> dict[tuple[str, str], str]:
    """(module, function) -> signature text from the Functions table."""
    out = {}
    for line in DOC.splitlines():
        m = re.match(r"^\| `(\w+)` \(S\d\) \| `([^`]+)` \|", line)
        if m:
            sig = m.group(2).replace("\\|", "|")
            out[(m.group(1), sig.split("(", 1)[0])] = sig
    return out


def render(fn) -> str:
    sig = inspect.signature(fn)
    parts = []
    for p in sig.parameters.values():
        s = f"{p.name}: {p.annotation}"
        if p.default is not inspect.Parameter.empty:
            d = p.default
            if dataclasses.is_dataclass(d) and d == type(d)():
                s += f" = {type(d).__name__}()"
            else:
                s += f" = {d!r}".replace("'", '"')
        parts.append(s)
    return f"{fn.__name__}({', '.join(parts)}) -> {sig.return_annotation}"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("'", '"')).strip()


SIGS = doc_signatures()


def test_the_doc_lists_every_c7_function():
    assert set(SIGS) == {("io", "ingest"), ("physics", "calibrate"), ("segment", "segment"), ("io", "crop"),
                         ("longitudinal", "register"), ("physics", "refill_lesion"), ("physics", "run_physics"),
                         ("export", "export"), ("longitudinal", "synth_series")}


@pytest.mark.parametrize("module,function", sorted(SIGS))
def test_signature_matches_the_contract(module, function):
    fn = getattr(importlib.import_module(f"rb_pipeline.{module}"), function)
    assert norm(render(fn)) == norm(SIGS[(module, function)])


@pytest.mark.parametrize("stage", STAGES)
def test_every_public_function_is_in_the_contract(stage):
    mod = importlib.import_module(f"rb_pipeline.{stage}")
    assert set(mod.__all__) == {f for (m, f) in SIGS if m == stage}


def doc_types() -> dict[str, list[str]]:
    block = DOC.split("```python", 1)[1].split("```", 1)[0]
    out, current = {}, None
    for line in block.splitlines():
        m = re.match(r"^class (\w+)", line)
        if m:
            current = m.group(1)
            out[current] = []
            continue
        if current and line.startswith("    "):
            code = line.split("#", 1)[0]
            out[current] += re.findall(r"(?:^|;)\s*(\w+):", code)
    return out


@pytest.mark.parametrize("name,fields", sorted(doc_types().items()))
def test_types_match_the_contract(name, fields):
    cls = getattr(rp, name)
    if dataclasses.is_dataclass(cls):
        assert [f.name for f in dataclasses.fields(cls)] == fields
    else:
        assert issubclass(cls, ValueError) and fields == []


def _crop(calibration):
    masks = rp.Masks(vertebra=Path("m.nii.gz"), body=None, source="expert", body_split_method=None)
    return rp.Crop(scan_id="phantom", level="L1", ct=Path("ct.nii.gz"), masks=masks, lesion=None,
                   calibration=calibration, affine=((1.0, 0.0, 0.0, 0.0),) * 4, shape=(1, 1, 1))


def test_run_physics_refuses_a_crop_without_calibration():
    from rb_pipeline.physics import run_physics
    with pytest.raises(rp.CalibrationMissingError):
        run_physics(_crop(None))


@pytest.mark.skipif(G1_PASSED, reason="G1 passed: the stages hold real code now")
@pytest.mark.parametrize("module,function", sorted(SIGS))
def test_before_g1_every_stage_is_a_stub(module, function):
    fn = getattr(importlib.import_module(f"rb_pipeline.{module}"), function)
    cal = rp.Calibration(method="phantomless_fat_air", slope=1.0, intercept=0.0, slope_sd=0.0, peaks_hu={},
                         reference={}, reference_unit="mg/cm3", roi="test", fit_r2=None, label="test")
    args = {name: (_crop(cal) if "crop" in name or name in ("moving", "fixed", "scan_a", "scan_b") else None)
            for name, p in inspect.signature(fn).parameters.items() if p.default is inspect.Parameter.empty}
    with pytest.raises(NotImplementedError, match="lands after G1"):
        fn(**args)


@pytest.mark.skipif(G1_PASSED, reason="G1 passed: the stages hold real code now")
def test_before_g1_the_pipeline_imports_only_the_standard_library():
    allowed = {"__future__", "dataclasses", "pathlib", "typing", "rb_pipeline"}
    for p in sorted((REPO / "pipeline" / "rb_pipeline").rglob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {"rb_pipeline"} if node.level else {(node.module or "").split(".")[0]}
            else:
                continue
            assert roots <= allowed, (p.relative_to(REPO), roots)
