from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from crossalpha.state.v03 import compute_borrower_census
from crossalpha.state.v03_integrity import (
    strict_state_v03_integrity_report,
    strict_state_v03_status,
)
from crossalpha.state import v03_prospective as prospective


FREEZE_TIME = pd.Timestamp("2026-09-05T02:00:00Z")
MIN_BLOCK = 23_000_000


def _references(root: Path) -> None:
    for name, path in prospective._reference_paths(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"protocol": name, "frozen": True}), encoding="utf-8")


def _account_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "address": "0x" + "1" * 40,
                "success": True,
                "error": None,
                "total_collateral_usd": 2_000_000.0,
                "total_debt_usd": 1_000_000.0,
                "available_borrows_usd": 0.0,
                "current_liquidation_threshold_pct": 82.5,
                "ltv_pct": 75.0,
                "health_factor": 1.04,
            }
        ]
    )


def _artifacts(root: Path, *, block: int, captured: pd.Timestamp) -> tuple[Path, Path]:
    directory = root / "derived" / "state" / "v03" / "full_census" / "fixture"
    directory.mkdir(parents=True, exist_ok=True)
    detail = directory / f"accounts_{block}.parquet"
    _account_rows().to_parquet(detail, index=False)
    report = compute_borrower_census(
        _account_rows(),
        total_candidate_addresses=1,
        bootstrap_complete=True,
        block_number=block,
        captured_at=captured,
    )
    summary = directory / f"summary_{block}.json"
    payload = {
        **report,
        "detail_path": str(detail),
        "detail_sha256": prospective.sha256_file(detail),
    }
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary, detail


def test_freeze_is_immutable_and_has_block_floor(tmp_path: Path) -> None:
    _references(tmp_path)
    first = prospective.freeze_state_v03(
        tmp_path,
        minimum_eligible_block=MIN_BLOCK,
        now=FREEZE_TIME,
    )
    second = prospective.freeze_state_v03(
        tmp_path,
        minimum_eligible_block=MIN_BLOCK + 100,
        now=FREEZE_TIME + pd.Timedelta(hours=1),
    )
    assert first["status"] == "frozen"
    assert second["status"] == "already_frozen"
    assert first["record_sha256"] == second["record_sha256"]
    assert first["minimum_eligible_block"] == MIN_BLOCK
    assert first["historical_bootstrap_is_evidence"] is False
    assert first["retrospective_backfill_allowed"] is False


def test_writer_rejects_old_block_even_if_written_after_freeze(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_state_v03(tmp_path, minimum_eligible_block=MIN_BLOCK, now=FREEZE_TIME)
    summary, detail = _artifacts(
        tmp_path,
        block=MIN_BLOCK - 1,
        captured=FREEZE_TIME + pd.Timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="block predates"):
        prospective.write_full_census_observation(
            tmp_path,
            summary_path=summary,
            detail_path=detail,
            known_at=FREEZE_TIME + pd.Timedelta(minutes=6),
        )


def test_writer_rejects_prefreeze_captured_census(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_state_v03(tmp_path, minimum_eligible_block=MIN_BLOCK, now=FREEZE_TIME)
    summary, detail = _artifacts(
        tmp_path,
        block=MIN_BLOCK,
        captured=FREEZE_TIME - pd.Timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="captured_at cannot predate"):
        prospective.write_full_census_observation(
            tmp_path,
            summary_path=summary,
            detail_path=detail,
            known_at=FREEZE_TIME + pd.Timedelta(minutes=1),
        )


def test_valid_record_is_sealed_and_strictly_audited(tmp_path: Path) -> None:
    _references(tmp_path)
    freeze = prospective.freeze_state_v03(
        tmp_path, minimum_eligible_block=MIN_BLOCK, now=FREEZE_TIME
    )
    captured = FREEZE_TIME + pd.Timedelta(minutes=5)
    summary, detail = _artifacts(tmp_path, block=MIN_BLOCK + 10, captured=captured)
    record = prospective.write_full_census_observation(
        tmp_path,
        summary_path=summary,
        detail_path=detail,
        known_at=captured + pd.Timedelta(minutes=1),
    )
    assert record["status"] == "written"
    assert record["freeze_record_sha256"] == freeze["record_sha256"]
    assert record["risk_multiplier"] is None
    integrity = strict_state_v03_integrity_report(tmp_path)
    assert integrity["ok"] is True, integrity
    assert integrity["record_count"] == 1
    status = strict_state_v03_status(tmp_path)
    assert status["automatic_promotion_to_actionable_modifier_allowed"] is False
    assert status["state"] == "FROZEN_BORROWER_UNIVERSE_BOOTSTRAPPING"


def test_artifact_tamper_is_detected(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_state_v03(tmp_path, minimum_eligible_block=MIN_BLOCK, now=FREEZE_TIME)
    captured = FREEZE_TIME + pd.Timedelta(minutes=5)
    summary, detail = _artifacts(tmp_path, block=MIN_BLOCK + 10, captured=captured)
    prospective.write_full_census_observation(
        tmp_path,
        summary_path=summary,
        detail_path=detail,
        known_at=captured + pd.Timedelta(minutes=1),
    )
    detail.write_bytes(b"tampered")
    integrity = strict_state_v03_integrity_report(tmp_path)
    assert integrity["ok"] is False
    assert integrity["checks"]["artifact_hash_links"] is False


def test_same_block_cannot_be_relabelled_with_new_artifacts(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_state_v03(tmp_path, minimum_eligible_block=MIN_BLOCK, now=FREEZE_TIME)
    captured = FREEZE_TIME + pd.Timedelta(minutes=5)
    summary, detail = _artifacts(tmp_path, block=MIN_BLOCK + 10, captured=captured)
    prospective.write_full_census_observation(
        tmp_path,
        summary_path=summary,
        detail_path=detail,
        known_at=captured + pd.Timedelta(minutes=1),
    )
    # Rebuild a different valid artifact while keeping the same finalized block.
    changed = _account_rows().copy()
    changed.loc[0, "total_debt_usd"] = 900_000.0
    detail2 = detail.with_name("accounts_changed.parquet")
    changed.to_parquet(detail2, index=False)
    report2 = compute_borrower_census(
        changed,
        total_candidate_addresses=1,
        bootstrap_complete=True,
        block_number=MIN_BLOCK + 10,
        captured_at=captured + pd.Timedelta(minutes=2),
    )
    summary2 = summary.with_name("summary_changed.json")
    summary2.write_text(
        json.dumps(
            {
                **report2,
                "detail_path": str(detail2),
                "detail_sha256": prospective.sha256_file(detail2),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="BLOCK_COLLISION"):
        prospective.write_full_census_observation(
            tmp_path,
            summary_path=summary2,
            detail_path=detail2,
            known_at=captured + pd.Timedelta(minutes=3),
        )
