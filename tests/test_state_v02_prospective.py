from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from crossalpha.state import v02_prospective as prospective
from crossalpha.state.v02_integrity import (
    strict_state_v02_integrity_report,
    strict_state_v02_status,
)


FREEZE_TIME = pd.Timestamp("2026-09-05T01:00:00Z")


def _reference_freezes(root: Path) -> None:
    paths = prospective._reference_freeze_paths(root)
    for name, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"protocol": name, "immutable": True}), encoding="utf-8")


def _snapshot(root: Path, generated: pd.Timestamp, *, score: float = 0.4) -> dict:
    derived = (
        root
        / "derived"
        / "state"
        / "v02"
        / f"year={generated:%Y}"
        / f"month={generated:%m}"
        / f"day={generated:%d}"
        / f"state_at={generated:%H%M%S%f}.parquet"
    )
    derived.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"generated_at": generated, "descriptive_stress_score": score}]).to_parquet(
        derived, index=False
    )
    return {
        "protocol": "CROSSALPHA_STATE_V0_2",
        "mode": "PROSPECTIVE_DESCRIPTIVE_SHADOW",
        "actionability": "DESCRIPTIVE_ONLY",
        "risk_multiplier": None,
        "mutates_frozen_core": False,
        "mutates_state_v01": False,
        "mutates_state_ab_v01": False,
        "as_of": generated.isoformat(),
        "generated_at": generated.isoformat(),
        "data_confidence": "FULL",
        "descriptive_stress_score": score,
        "valid_pressure_component_count": 4,
        "valid_pressure_components": [
            "aave_market_stress",
            "stablecoin_flow_decomposition",
            "basis_dispersion",
            "contagion_graph",
        ],
        "components": {
            "aave_market_stress": {"pressure": 0.2},
            "stablecoin_flow_decomposition": {
                "pressure": 0.1,
                "net_system_change_ratio": 0.01,
                "migration_ratio": 0.02,
            },
            "basis_dispersion": {"pressure": 0.3},
            "contagion_graph": {"pressure": 0.4},
        },
        "borrower_health_factor_distribution": {"valid": False},
        "aave_liquidation_activity": {"events_24h": 0, "events_7d": 0},
        "deployment_activation": {"coincident_activation_proxy": False},
        "output": str(derived),
    }


def test_state_v02_freeze_is_immutable_and_references_v01(tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    first = prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    second = prospective.freeze_state_v02(
        tmp_path, now=FREEZE_TIME + pd.Timedelta(hours=1)
    )
    assert first["status"] == "frozen"
    assert second["status"] == "already_frozen"
    assert first["record_sha256"] == second["record_sha256"]
    assert first["actionability"] == "DESCRIPTIVE_ONLY"
    assert first["risk_multiplier"] is None
    assert first["retrospective_backfill_allowed"] is False
    assert set(first["reference_freezes"]) == {"frozen_b3", "state_ab_v01"}


def test_state_v02_rejects_retrospective_observation(tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    generated = FREEZE_TIME - pd.Timedelta(minutes=1)
    with pytest.raises(ValueError, match="cannot predate the freeze"):
        prospective.write_live_state_v02_observation(
            tmp_path,
            _snapshot(tmp_path, generated),
            now=FREEZE_TIME,
        )


def test_state_v02_rejects_non_descriptive_snapshot(tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    generated = FREEZE_TIME + pd.Timedelta(minutes=1)
    snap = _snapshot(tmp_path, generated)
    snap["risk_multiplier"] = 0.75
    with pytest.raises(ValueError, match="descriptive-only"):
        prospective.write_live_state_v02_observation(
            tmp_path, snap, now=generated + pd.Timedelta(minutes=1)
        )


def test_live_observation_is_sealed_linked_and_idempotent(tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    freeze = prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    generated = FREEZE_TIME + pd.Timedelta(minutes=1)
    snap = _snapshot(tmp_path, generated, score=0.7)
    first = prospective.write_live_state_v02_observation(
        tmp_path, snap, now=generated + pd.Timedelta(minutes=1)
    )
    second = prospective.write_live_state_v02_observation(
        tmp_path, snap, now=generated + pd.Timedelta(minutes=2)
    )
    assert first["record_sha256"] == second["record_sha256"]
    assert first["freeze_record_sha256"] == freeze["record_sha256"]
    report = strict_state_v02_integrity_report(tmp_path)
    assert report["ok"] is True
    assert report["audit_level"] == "STRICT_NON_MUTATING_HASH_GRAPH"
    assert report["observation_count"] == 1
    status = strict_state_v02_status(tmp_path)
    assert status["state"] == "O1_PROSPECTIVE_EVIDENCE_ACCUMULATING"
    assert status["automatic_promotion_to_actionable_modifier_allowed"] is False


def test_implementation_hash_drift_locks_prospective_writer(monkeypatch, tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    freeze = prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    changed = dict(freeze["implementation_file_sha256"])
    changed["state_v02"] = "0" * 64
    monkeypatch.setattr(prospective, "_file_hashes", lambda: changed)
    generated = FREEZE_TIME + pd.Timedelta(minutes=1)
    with pytest.raises(RuntimeError, match="IMPLEMENTATION_MUTATED"):
        prospective.write_live_state_v02_observation(
            tmp_path,
            _snapshot(tmp_path, generated),
            now=generated + pd.Timedelta(minutes=1),
        )


def test_derived_state_tamper_is_detected(tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    generated = FREEZE_TIME + pd.Timedelta(minutes=1)
    snap = _snapshot(tmp_path, generated)
    prospective.write_live_state_v02_observation(
        tmp_path, snap, now=generated + pd.Timedelta(minutes=1)
    )
    Path(snap["output"]).write_bytes(b"tampered")
    report = strict_state_v02_integrity_report(tmp_path)
    assert report["ok"] is False
    assert report["checks"]["derived_state_hash_links"] is False


def test_missing_15m_cycles_are_reported_but_never_backfilled(tmp_path: Path) -> None:
    _reference_freezes(tmp_path)
    prospective.freeze_state_v02(tmp_path, now=FREEZE_TIME)
    for offset in (1, 61):
        generated = FREEZE_TIME + pd.Timedelta(minutes=offset)
        prospective.write_live_state_v02_observation(
            tmp_path,
            _snapshot(tmp_path, generated),
            now=generated + pd.Timedelta(minutes=1),
        )
    report = strict_state_v02_integrity_report(tmp_path)
    assert report["ok"] is True
    assert report["gap_count"] == 1
    assert report["gaps_are_visible_not_backfilled"] is True
    assert report["observation_count"] == 2
