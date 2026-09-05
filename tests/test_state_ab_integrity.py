from __future__ import annotations

import pytest

from crossalpha.core import frozen_b3_v01
from crossalpha.state import ab_integrity


FREEZE_HASH = "ab-freeze"


def _weights(multiplier: float = 1.0) -> dict[str, float]:
    values = {asset: 0.0 for asset in frozen_b3_v01.ALL_ASSETS}
    values["US_EQUITY"] = 0.20 * multiplier
    values["GOLD"] = 0.10 * multiplier
    values["BTC"] = 0.10 * multiplier
    values["CASH"] = 1.0 - sum(values[a] for a in frozen_b3_v01.RISK_ASSETS)
    return values


def _decision() -> dict:
    return {
        "effective_date": "2026-09-07",
        "record_sha256": "decision",
        "ab_freeze_record_sha256": FREEZE_HASH,
        "a_snapshot_record_sha256": "a-snapshot",
        "shadow_risk_multiplier": 0.5,
        "state_payload": {
            "shadow_only": True,
            "core_protocol_mutated": False,
        },
    }


def _a_snapshot() -> dict:
    return {
        "effective_date": "2026-09-07",
        "record_sha256": "a-snapshot",
    }


def _b_snapshot() -> dict:
    return {
        "effective_date": "2026-09-07",
        "record_sha256": "b-snapshot",
        "ab_freeze_record_sha256": FREEZE_HASH,
        "a_snapshot_record_sha256": "a-snapshot",
        "state_decision_record_sha256": "decision",
        "shadow_risk_multiplier": 0.5,
        "a_weights": _weights(1.0),
        "b_weights": _weights(0.5),
        "a_risk_gross": 0.4,
        "b_risk_gross": 0.2,
    }


def _a_mark() -> dict:
    return {
        "date": "2026-09-07",
        "record_sha256": "a-mark",
        "active_snapshot_effective_date": "2026-09-07",
        "active_snapshot_record_sha256": "a-snapshot",
        "asset_returns": {asset: 0.001 for asset in frozen_b3_v01.ALL_ASSETS},
        "net_return": 0.001,
    }


def _b_mark() -> dict:
    a = _a_mark()
    return {
        "date": "2026-09-07",
        "record_sha256": "b-mark",
        "ab_freeze_record_sha256": FREEZE_HASH,
        "a_mark_record_sha256": "a-mark",
        "a_snapshot_effective_date": "2026-09-07",
        "a_snapshot_record_sha256": "a-snapshot",
        "b_snapshot_record_sha256": "b-snapshot",
        "state_decision_record_sha256": "decision",
        "asset_returns": dict(a["asset_returns"]),
        "a_net_return": 0.001,
        "weights": _weights(0.5),
        "shadow_risk_multiplier": 0.5,
    }


def _patch_loaders(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decisions: list[dict],
    b_snapshots: list[dict],
    b_marks: list[dict],
    a_snapshots: list[dict],
    a_marks: list[dict],
) -> None:
    monkeypatch.setattr(
        ab_integrity.ab_paper,
        "state_ab_integrity_report",
        lambda _root: {
            "protocol": "CROSSALPHA_STATE_AB_V0_1",
            "frozen": True,
            "ok": True,
            "checks": {},
        },
    )
    monkeypatch.setattr(
        ab_integrity.ab_paper,
        "_load_freeze",
        lambda _root: {
            "first_eligible_effective_date": "2026-09-07",
            "record_sha256": FREEZE_HASH,
        },
    )
    monkeypatch.setattr(ab_integrity.ab_paper, "_load_decisions", lambda _root: decisions)
    monkeypatch.setattr(ab_integrity.ab_paper, "_load_snapshots", lambda _root: b_snapshots)
    monkeypatch.setattr(ab_integrity.ab_paper, "_load_marks", lambda _root: b_marks)
    monkeypatch.setattr(ab_integrity, "_load_a_snapshots", lambda _root: a_snapshots)
    monkeypatch.setattr(ab_integrity, "_load_a_marks", lambda _root: a_marks)


def test_strict_audit_rejects_A_snapshot_without_B(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_loaders(
        monkeypatch,
        decisions=[],
        b_snapshots=[],
        b_marks=[],
        a_snapshots=[_a_snapshot()],
        a_marks=[],
    )
    report = ab_integrity.strict_state_ab_integrity_report(tmp_path)
    assert report["ok"] is False
    assert report["checks"]["eligible_A_snapshots_have_B"] is False
    assert report["checks"]["A_marks_have_exactly_one_B_mark"] is False


def test_strict_audit_rejects_A_mark_without_B(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_loaders(
        monkeypatch,
        decisions=[_decision()],
        b_snapshots=[_b_snapshot()],
        b_marks=[],
        a_snapshots=[_a_snapshot()],
        a_marks=[_a_mark()],
    )
    report = ab_integrity.strict_state_ab_integrity_report(tmp_path)
    assert report["ok"] is False
    assert report["checks"]["A_marks_have_exactly_one_B_mark"] is False


def test_strict_audit_rejects_different_B_asset_returns(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    b_mark = _b_mark()
    b_mark["asset_returns"] = dict(b_mark["asset_returns"])
    b_mark["asset_returns"]["BTC"] = 0.002
    _patch_loaders(
        monkeypatch,
        decisions=[_decision()],
        b_snapshots=[_b_snapshot()],
        b_marks=[b_mark],
        a_snapshots=[_a_snapshot()],
        a_marks=[_a_mark()],
    )
    report = ab_integrity.strict_state_ab_integrity_report(tmp_path)
    assert report["ok"] is False
    assert report["checks"]["B_marks_reuse_exact_A_asset_returns"] is False


def test_strict_audit_accepts_complete_hash_linked_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _patch_loaders(
        monkeypatch,
        decisions=[_decision()],
        b_snapshots=[_b_snapshot()],
        b_marks=[_b_mark()],
        a_snapshots=[_a_snapshot()],
        a_marks=[_a_mark()],
    )
    report = ab_integrity.strict_state_ab_integrity_report(tmp_path)
    assert report["ok"] is True
    assert report["audit_level"] == "STRICT_BIDIRECTIONAL_HASH_GRAPH"
    assert all(report["checks"].values())
