from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.state.v03 import compute_borrower_census
from crossalpha.state.v03_watchlist import compute_watchlist_snapshot


def _rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "address": "0x" + "1" * 40,
                "success": True,
                "error": None,
                "total_collateral_usd": 2_000_000.0,
                "total_debt_usd": 1_000_000.0,
                "available_borrows_usd": 100_000.0,
                "current_liquidation_threshold_pct": 82.5,
                "ltv_pct": 75.0,
                "health_factor": 1.01,
            },
            {
                "address": "0x" + "2" * 40,
                "success": True,
                "error": None,
                "total_collateral_usd": 10_000_000.0,
                "total_debt_usd": 5_000_000.0,
                "available_borrows_usd": 500_000.0,
                "current_liquidation_threshold_pct": 80.0,
                "ltv_pct": 72.0,
                "health_factor": 1.18,
            },
            {
                "address": "0x" + "3" * 40,
                "success": True,
                "error": None,
                "total_collateral_usd": 1_000_000.0,
                "total_debt_usd": 0.0,
                "available_borrows_usd": 200_000.0,
                "current_liquidation_threshold_pct": 80.0,
                "ltv_pct": 70.0,
                "health_factor": None,
            },
        ]
    )


def test_full_census_reports_debt_weighted_cliff_exposure() -> None:
    report = compute_borrower_census(
        _rows(),
        total_candidate_addresses=3,
        bootstrap_complete=True,
        block_number=23_000_000,
        captured_at="2026-09-05T02:00:00Z",
    )
    assert report["valid_full_census"] is True
    assert report["data_confidence"] == "FULL_CENSUS"
    assert report["active_borrower_count"] == 2
    assert report["total_active_debt_usd"] == pytest.approx(6_000_000.0)
    assert report["critical_hf_le_1_05_debt_usd"] == pytest.approx(1_000_000.0)
    assert report["critical_hf_le_1_05_debt_share"] == pytest.approx(1 / 6)
    assert report["near_cliff_hf_le_1_20_debt_share"] == pytest.approx(1.0)
    assert report["liquidatable_debt_share"] == pytest.approx(0.0)
    assert report["debt_weighted_hf_p50"] == pytest.approx(1.18)
    assert report["risk_multiplier"] is None
    assert report["actionability"] == "DESCRIPTIVE_ONLY"
    assert report["mutates_state_v02"] is False


def test_failed_call_ratio_blocks_full_census_claim() -> None:
    rows = _rows().iloc[:2].copy()
    report = compute_borrower_census(
        rows,
        total_candidate_addresses=100,
        bootstrap_complete=True,
        block_number=23_000_000,
    )
    assert report["valid_full_census"] is False
    assert report["data_confidence"] == "PARTIAL_RPC_COVERAGE"
    assert report["account_call_coverage_ratio"] == pytest.approx(0.02)


def test_bootstrap_incomplete_never_claims_full_census() -> None:
    report = compute_borrower_census(
        _rows(),
        total_candidate_addresses=3,
        bootstrap_complete=False,
        block_number=23_000_000,
    )
    assert report["valid_full_census"] is False
    assert report["data_confidence"] == "BOOTSTRAP_INCOMPLETE"


def test_duplicate_addresses_fail_closed() -> None:
    rows = pd.concat([_rows(), _rows().iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate addresses"):
        compute_borrower_census(
            rows,
            total_candidate_addresses=4,
            bootstrap_complete=True,
            block_number=23_000_000,
        )


def test_watchlist_snapshot_never_claims_whole_market_coverage() -> None:
    report = compute_watchlist_snapshot(
        _rows().iloc[:2],
        expected_addresses=2,
        block_number=23_000_000,
        captured_at="2026-09-05T02:00:00Z",
    )
    assert report["scope"] == "WATCHLIST_ONLY"
    assert report["full_market_census_claim_allowed"] is False
    assert report["watchlist_active_debt_usd"] == pytest.approx(6_000_000.0)
    assert report["minimum_health_factor"] == pytest.approx(1.01)
    assert report["risk_multiplier"] is None
