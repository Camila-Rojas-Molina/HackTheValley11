"""The two headless heart-rate bridges (research 1.14): scripts/hr_bleak.py (HeartCast over BLE,
source heartcast-ble) and scripts/hr_pulsoid.py (Pulsoid WebSocket, source pulsoid-ws).

No phone, Watch or Pulsoid account here: the BLE and WebSocket loops are not run. What is tested is
everything around them: the 0x2A37 parser, the Pulsoid message parser, the POST into the real API
with the wearer key, the refusal to start while another source is posting, and that neither the
wearer key nor the Pulsoid token ever reaches a log line.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from scripts import hr_bleak, hr_common, hr_pulsoid
from tests.conftest import make_world


def rr_bytes(ms: float) -> list[int]:
    v = round(ms * 1024 / 1000)
    return [v & 0xFF, v >> 8]


# --- HeartCast: Heart Rate Measurement (0x2A37), same layout as hr_bridge.html's parseHRM ---------
def test_parse_hrm_matches_the_bridge_page():
    vectors = [
        ([0x00, 72], 72, [], False),
        ([0x01, 0x48, 0x00], 72, [], False),
        ([0x06, 64], 64, [], False),                         # sensor-contact bits are ignored
        ([0x10, 60, *rr_bytes(1000)], 60, [1000], True),     # RR == 60000/bpm: computed by the relay
        ([0x10, 60, *rr_bytes(930), *rr_bytes(1012)], 60, [930, 1012], False),
        ([0x18, 75, 0x34, 0x12, *rr_bytes(820)], 75, [820], False),   # Energy Expended skipped
        ([0x11, 0x50, 0x00, *rr_bytes(750)], 80, [750], True),
        ([0x10, 0, *rr_bytes(800)], 0, [800], False),        # 0 bpm: never synthetic, no division by 0
    ]
    for raw, bpm, rr, synthetic in vectors:
        out = hr_bleak.parse_hrm(bytes(raw))
        assert out.bpm == bpm and out.rr_synthetic is synthetic, raw
        assert out.rr_ms == pytest.approx(rr, abs=0.6), raw     # 1/1024 s units round to within 0.5 ms


def test_bleak_sample_is_tagged_heartcast_ble_and_skips_no_reading():
    s = hr_bleak.sample(hr_bleak.parse_hrm(bytes([0x00, 72])))
    assert s["source"] == "heartcast-ble" and s["simulated"] is False and s["bpm"] == 72 and s["ts"]
    assert hr_bleak.sample(hr_bleak.parse_hrm(bytes([0x00, 0]))) is None          # watch off the wrist
    assert hr_bleak.HRS.startswith("0000180d") and hr_bleak.HRM.startswith("00002a37")


# --- Pulsoid: {"measured_at": <unix ms>, "data": {"heart_rate": N}} ---------------------------------
def test_pulsoid_message_parser():
    s = hr_pulsoid.parse_message('{"measured_at": 1625310655000, "data": {"heart_rate": 78}}')
    assert s == {"ts": "2021-07-03T11:10:55+00:00", "bpm": 78, "rr_ms": [], "source": "pulsoid-ws",
                 "simulated": False}
    for bad in ("not json", "{}", '{"data": {"heart_rate": 0}, "measured_at": 1}', '{"measured_at": 1}',
                '{"data": {"heart_rate": "x"}, "measured_at": 1625310655000}'):
        assert hr_pulsoid.parse_message(bad) is None, bad


def test_pulsoid_token_goes_in_a_header_never_the_url():
    assert hr_pulsoid.WS_URL == "wss://dev.pulsoid.net/api/v1/data/real_time"
    assert hr_pulsoid.ws_headers({"PULSOID_TOKEN": "tok-abc"}) == {"Authorization": "Bearer tok-abc"}
    with pytest.raises(SystemExit) as e:
        hr_pulsoid.ws_headers({})
    assert "PULSOID_TOKEN" in str(e.value)


# --- shared: auth, posting, the duplicate guard, and no secrets in logs ----------------------------
def test_bridges_need_the_wearer_key():
    with pytest.raises(SystemExit) as e:
        hr_common.Config.from_env({})
    assert "WEARER_KEY" in str(e.value)
    cfg = hr_common.Config.from_env({"WEARER_KEY": "wk", "STILLHERE_URL": "http://host:9000/"})
    assert cfg.base_url == "http://host:9000" and cfg.headers() == {"Authorization": "Bearer wk"}


async def test_a_bridge_sample_lands_in_hr_samples_through_the_real_api(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    transport = httpx.ASGITransport(app=client._transport.app)
    cfg = hr_common.Config(base_url="http://test", wearer_key=w.wearer_key)
    async with hr_common.Poster(cfg, transport=transport) as poster:
        assert await poster.post(hr_bleak.sample(hr_bleak.parse_hrm(bytes([0x00, 71])))) is True
        assert await poster.post(hr_pulsoid.parse_message(
            '{"measured_at": %d, "data": {"heart_rate": 73}}' % int(__import__("time").time() * 1000))) is True
    async with db.connection() as conn:
        rows = await (await conn.execute(
            "SELECT source, bpm, simulated FROM hr_samples WHERE user_id = %s ORDER BY time", (w.user_id,))).fetchall()
    assert {(r["source"], r["bpm"], r["simulated"]) for r in rows} == {("heartcast-ble", 71, False),
                                                                     ("pulsoid-ws", 73, False)}


async def test_a_bridge_refuses_to_start_while_another_source_is_posting(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    transport = httpx.ASGITransport(app=client._transport.app)
    cfg = hr_common.Config(base_url="http://test", wearer_key=w.wearer_key)
    async with hr_common.Poster(cfg, transport=transport) as poster:
        assert await poster.others_live("heartcast-ble") == []
        await client.post("/v1/hr", headers=w.wearer, json={"source": "heartcast-webbt", "bpm": 70})
        assert await poster.others_live("heartcast-ble") == ["heartcast-webbt"]
        assert await poster.others_live("heartcast-webbt") == []
        with pytest.raises(SystemExit) as e:
            await poster.guard("heartcast-ble", force=False)
        assert "heartcast-webbt" in str(e.value) and "only one bridge" in str(e.value)
        await poster.guard("heartcast-ble", force=True)                     # --force skips the check


async def test_failed_posts_never_log_the_key(caplog):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-wearer-key-123 in a message")
    cfg = hr_common.Config(base_url="http://test", wearer_key="secret-wearer-key-123")
    with caplog.at_level(logging.INFO):
        async with hr_common.Poster(cfg, transport=httpx.MockTransport(boom)) as poster:
            assert await poster.post({"ts": "x", "bpm": 70, "source": "heartcast-ble", "simulated": False}) is False
    assert "ConnectError" in caplog.text and "secret-wearer-key-123" not in caplog.text


def test_pulsoid_reconnect_log_names_the_error_type_only(caplog):
    with caplog.at_level(logging.INFO):
        hr_pulsoid.log_reconnect(RuntimeError("token tok-xyz rejected"), 4)
    assert "RuntimeError" in caplog.text and "tok-xyz" not in caplog.text and "4 s" in caplog.text


def test_bleak_and_websockets_are_imported_only_when_a_bridge_runs():
    """The scripts import (and these tests run) without Bluetooth or a network connection."""
    for mod, lib in ((hr_bleak, "bleak"), (hr_pulsoid, "websockets")):
        src = open(mod.__file__, encoding="utf-8").read()
        top = [line for line in src.splitlines() if line.startswith(("import ", "from "))]
        assert not [line for line in top if lib in line], (mod.__name__, top)
