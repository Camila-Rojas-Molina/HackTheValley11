"""The pages' JavaScript, run in Node (skipped when Node is not installed):
every inline script parses, and hr_bridge.html's parseHRM decodes 0x2A37 notifications."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parent.parent / "app" / "pages"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


@pytest.mark.parametrize("page", ["c.html", "hr_bridge.html"])
def test_inline_scripts_parse(page, tmp_path):
    scripts = _scripts((PAGES / page).read_text(encoding="utf-8"))
    assert scripts, page
    for i, src in enumerate(scripts):
        f = tmp_path / f"{page}.{i}.js"
        f.write_text(src, encoding="utf-8")
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def _parse(vectors: list[list[int]]) -> list[dict]:
    html = (PAGES / "hr_bridge.html").read_text(encoding="utf-8")
    fn = html.split("// BEGIN parseHRM")[1].split("// END parseHRM")[0]
    prog = fn + """
const vectors = JSON.parse(process.argv[1]);
console.log(JSON.stringify(vectors.map(v => parseHRM(new DataView(Uint8Array.from(v).buffer)))));
"""
    r = subprocess.run([NODE, "-e", prog, json.dumps(vectors)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def _rr_bytes(ms: float) -> list[int]:
    v = round(ms * 1024 / 1000)
    return [v & 0xFF, v >> 8]


def test_parse_hrm_formats():
    out = _parse([
        [0x00, 72],                                   # uint8 bpm, nothing else
        [0x01, 0x48, 0x00],                           # uint16 bpm
        [0x06, 64],                                   # sensor-contact bits are ignored
        [0x10, 60, *_rr_bytes(1000)],                 # RR present, equal to 60000/bpm -> synthetic
        [0x10, 60, *_rr_bytes(930), *_rr_bytes(1012)],  # measured RR -> not synthetic
        [0x18, 75, 0x34, 0x12, *_rr_bytes(820)],      # Energy Expended skipped before RR
        [0x11, 0x50, 0x00, *_rr_bytes(750)],          # uint16 bpm + RR = 60000/80 -> synthetic
        [0x10, 0, *_rr_bytes(800)],                   # 0 bpm (no reading): never synthetic, no division by 0
    ])
    assert [o["bpm"] for o in out] == [72, 72, 64, 60, 60, 75, 80, 0]
    assert out[0]["rr_ms"] == [] and out[0]["rr_synthetic"] is False
    assert out[3]["rr_ms"] == [1000] and out[3]["rr_synthetic"] is True
    assert [round(x) for x in out[4]["rr_ms"]] == [930, 1012] and out[4]["rr_synthetic"] is False
    assert [round(x) for x in out[5]["rr_ms"]] == [820] and out[5]["rr_synthetic"] is False
    assert out[6]["rr_synthetic"] is True
    assert out[7]["rr_synthetic"] is False
