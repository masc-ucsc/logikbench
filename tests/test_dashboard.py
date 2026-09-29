"""Dashboard annotations for historical and frozen refresh publications."""
from dashboard.build_db import build_section, refresh_pending, settings_label


def test_frozen_run_identity():
    refresh = {"run": "new", "binary_sha256": "abc"}
    assert not refresh_pending(refresh, {"run": "new", "job": "/jobs/basic/mux"})
    assert refresh_pending(refresh, {"run": "old", "binary_sha256": "abc"})
    assert refresh_pending(refresh, {"run": "new", "binary_sha256": "different"})
    assert refresh_pending(refresh, {})


def test_legacy_binary_identity():
    assert not refresh_pending({"binary_sha256": "abc"}, {"binary_sha256": "abc"})
    assert refresh_pending({"binary_sha256": "abc"}, {"binary_sha256": "old"})
    assert refresh_pending({}, {})


def test_policy_settings_visible():
    label = settings_label({"synthesis_policy": {"synth_alg": "cones", "ctrl_cones": True,
                           "forward": "all", "stop_mux": False}, "options": "--stats"})
    assert "synth_alg=cones ctrl_cones=true forward=all stop_mux=false" in label
    assert "--stats" in label
    assert settings_label({"options": "old options"}) == "old options"


def test_new_results_are_current_without_inventing_missing_rows(monkeypatch):
    monkeypatch.setattr("dashboard.build_db.benchmark_order", lambda: [("basic", "mux"), ("basic", "band")])
    payload = {"meta": {"target": "lhd_asap7", "refresh": {"run": "new", "binary_sha256": "abc"},
                       "row_provenance": {"basic": {"mux": {"run": "new"}}}},
               "metrics": {"cells": {"basic": {"mux": 10}}},
               "status": {"basic": {"mux": {"synthesis": "success", "timing": "skipped"}}}}
    section = build_section(["lhd_asap7"], ["cells"], {"lhd_asap7": payload})
    assert set(section["data"]) == {"basic/mux"}
    row = section["data"]["basic/mux"]["lhd_asap7"]
    assert row["cells"] == 10 and not row["_refresh_pending"]
    assert row["_status"]["timing"] == "skipped"
