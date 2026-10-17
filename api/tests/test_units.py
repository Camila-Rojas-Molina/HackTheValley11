"""Units without a database: settings (RB_ prefix), asset names, file kinds, the flag rule and bucket ranges."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import storage, tiger, views
from app.config import Settings


def test_settings_read_the_rb_prefix_and_refuse_placeholders():
    s = Settings.from_env({"RB_DATABASE_URL": "postgresql://h/db", "RB_ADMIN_KEY": "change-me-admin-key",
                           "RB_MAX_UPLOAD_MB": "5", "RB_CORS_ORIGINS": "http://a:3000/, http://b",
                           "RB_DATA_DIR": "/tmp/rb", "RB_LOAD_N": "nan"})
    assert s.database_url == "postgresql://h/db" and s.admin_key is None
    assert s.max_upload_bytes == 5 * 1024 * 1024 and s.cors_origins == ("http://a:3000", "http://b")
    assert str(s.data_dir) == "/tmp/rb" and s.load_n == 800.0
    assert Settings.from_env({"DATABASE_URL": "postgresql://x/y"}).database_url == "postgresql://x/y"
    assert Settings.from_env({}).cors_origins == ("http://localhost:3000",)


@pytest.mark.parametrize("name,ok", [("ct.nii.gz", True), ("mask_body.ai.nii.gz", True), ("../ct.nii.gz", False),
                                     ("..", False), ("a/b.nii.gz", False), (".env", False), ("x.vtu", False),
                                     ("ct.nii.gz\x00", False), ("context_T11-L5.mz3", True)])
def test_asset_names(name, ok):
    assert storage.valid_name(name) is ok


def test_asset_path_never_leaves_its_root(tmp_path):
    (tmp_path / "s1").mkdir()
    (tmp_path / "s1" / "ct.nii.gz").write_bytes(b"x")
    assert storage.asset_path([tmp_path], "s1", "ct.nii.gz") == (tmp_path / "s1" / "ct.nii.gz").resolve()
    assert storage.asset_path([tmp_path], "..", "ct.nii.gz") is None
    assert storage.asset_path([tmp_path], "s1", "../s1/ct.nii.gz") is None


@pytest.mark.parametrize("name,expect", [
    ("ct.nii.gz", ("ct", None, None)),
    ("mask_lesion.expert.nii.gz", ("mask_lesion", "expert", None)),
    ("strain_min_principal_ue.ai.mz3", ("strain_surface", "ai", "strain_min_principal_ue")),
    ("vonmises_mpa.expert.nii.gz", ("strain_volume", "expert", "vonmises_mpa")),
    ("surface.expert.vtp", ("surface_vtp", "expert", None)),
    ("fields.ai.vti", ("volume_vti", "ai", None)),
    ("vertebra.ai.stl", ("stl", "ai", None)),
    ("context_T11-L5.mz3", ("context_mesh", None, None)),
    ("metrics.json", ("metrics_json", None, None)),
    ("mesh.vtu", (None, None, None)),
])
def test_asset_kinds_follow_formats_md(name, expect):
    assert views.describe_asset(name) == expect


def _scan(sid, day):
    return {"scan_id": sid, "level": "L1", "time": views.time_of(day), "provenance": "real_scan"}


def _row(sid, day, lesion_mm3, **kw):
    base = {"scan_id": sid, "site": "L1", "time": views.time_of(day), "created_at": views.time_of(day),
            "mask_source": "expert", "lesion_model": None, "statistic": None, "lesion_grid": "original",
            "lesion_volume_mm3": lesion_mm3, "vertebra_volume_mm3": 68000.0, "body_volume_mm3": None}
    base.update(kw)
    return base


def test_flag_fires_only_with_every_condition_met(monkeypatch):
    lab = views.labels()
    monkeypatch.setitem(lab["flag"]["rule"], "lesion_share_pts_min", 2.0)
    a, b = _scan("a", 0), _scan("b", 273)
    rows = [_row("a", 0, 0.0), _row("b", 273, 3240.0),
            _row("b", 273, None, lesion_grid=None, statistic="central80_mean", lesion_share_pts=5.0,
                 calib_low_pct=-1.0, calib_high_pct=1.5)]
    flag = views.evaluate_flag("L1", a, b, rows)
    assert flag["status"] == "flagged" and flag["alert_id"] == "al-b"
    assert flag["line"].startswith("Flagged because: new lesion at L1 (0.00 → 3.24 cm³)")
    rows[-1]["lesion_share_pts"] = 2.0          # 2.0 points is inside the 2.5-point calibration range
    flag = views.evaluate_flag("L1", a, b, rows)
    assert flag["status"] == "not_flagged" and flag["alert_id"] is None


def test_flag_is_pending_without_the_share_and_never_fires_on_shrinkage():
    a, b = _scan("a", 0), _scan("b", 273)
    flag = views.evaluate_flag("L1", a, b, [_row("a", 0, 0.0), _row("b", 273, 3240.0)])
    assert flag["status"] == "pending" and flag["line"].endswith("pending FE (M5).")
    flag = views.evaluate_flag("L1", a, b, [_row("a", 0, 900.0), _row("b", 273, 200.0)])
    assert flag["status"] == "not_flagged"


def test_monthly_bucket_range_covers_whole_months():
    lo, hi = tiger.bucket_range("scan_metrics_monthly", datetime(2026, 1, 17, tzinfo=timezone.utc),
                                datetime(2026, 12, 3, tzinfo=timezone.utc))
    assert (lo, hi) == (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))
    lo, hi = tiger.bucket_range("fe_level_daily", datetime(2026, 1, 17, 5, tzinfo=timezone.utc),
                                datetime(2026, 1, 17, 6, tzinfo=timezone.utc))
    assert (hi - lo).days == 1


@pytest.mark.parametrize("url,env,local", [
    ("postgresql:///rainbowbone", {}, True), ("postgresql://postgres@127.0.0.1:5544/rb", {}, True),
    ("postgresql://u@localhost/rb", {}, True), ("postgresql://u@[::1]:5432/rb", {}, True),
    ("host=/tmp/pg dbname=rb", {}, True), ("postgresql:///rb", {"PGHOST": "db.tsdb.cloud.timescale.com"}, False),
    ("postgresql://u@tiger-cloud.invalid:5432/tsdb?sslmode=require", {}, False),
    ("postgresql://u@localhost,tiger-cloud.invalid/rb", {}, False), ("not a url", {}, False)])
def test_only_a_database_on_this_machine_counts_as_local(url, env, local):
    from app.config import is_local_database, schema_writes_allowed
    assert is_local_database(url, env) is local
    assert schema_writes_allowed(url, env) is local
    assert schema_writes_allowed(url, {**env, "RB_ALLOW_REMOTE_SCHEMA": "1"}) is True


def test_start_up_never_migrates_or_seeds_a_remote_database(monkeypatch):
    from app import main
    import scripts.migrate as migrate_mod
    import scripts.seed_demo as seed_mod
    calls = []
    monkeypatch.setattr(migrate_mod, "apply_migrations", lambda url: calls.append(("migrate", url)) or [])
    monkeypatch.setattr(seed_mod, "seed", lambda url: calls.append(("seed", url)) or {})
    remote = "postgresql://u@tiger-cloud.invalid:5432/tsdb?sslmode=require"
    with pytest.raises(PermissionError):
        main._apply(Settings.from_env({"DATABASE_URL": remote}))
    with pytest.raises(PermissionError):
        main._apply(Settings.from_env({"RB_DATABASE_URL": remote, "RB_DEMO_SEED": "0"}))
    assert calls == []
    assert main._apply(Settings.from_env({"RB_DATABASE_URL": remote, "RB_AUTO_MIGRATE": "0",
                                          "RB_DEMO_SEED": "0"})) == []
    main._apply(Settings.from_env({"RB_DATABASE_URL": remote, "RB_ALLOW_REMOTE_SCHEMA": "1"}))
    local = "postgresql://postgres@127.0.0.1:5544/rb"
    main._apply(Settings.from_env({"RB_DATABASE_URL": local}))
    assert calls == [("migrate", remote), ("seed", remote), ("migrate", local), ("seed", local)]
