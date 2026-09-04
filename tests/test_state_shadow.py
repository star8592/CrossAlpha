from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from crossalpha.core import frozen_b3_v01
from crossalpha.state.shadow import (
    StateShadowConfig,
    apply_shadow_multiplier,
    compute_shadow_state,
)


def _hl_rows(*, observed: str, z: float, rolling: int = 30) -> pd.DataFrame:
    rows = []
    for asset in ("BTC", "ETH"):
        rows.append(
            {
                "observed_at": observed,
                "known_at": observed,
                "asset": asset,
                "funding_z_24h": z,
                "basis_z_24h": z,
                "oi_change_z_24h": z,
                "spread_z_24h": z,
                "rolling_observations_24h": rolling,
            }
        )
    return pd.DataFrame(rows)


def _stable_row(
    *,
    observed: str,
    delta_ratio: float = 0.01,
    delta_coverage: float = 0.95,
    chain_coverage: float = 1.0,
    residual_ratio: float = 0.001,
    peg_bps: float = 5.0,
) -> pd.DataFrame:
    supply = 100_000_000_000.0
    return pd.DataFrame(
        [
            {
                "observed_at": observed,
                "known_at": observed,
                "usd_supply_native": supply,
                "usd_delta_7d_native": supply * delta_ratio,
                "delta_7d_market_value_coverage": delta_coverage,
                "chain_coverage_ratio": chain_coverage,
                "chain_abs_residual_ratio": residual_ratio,
                "weighted_abs_peg_deviation_bps": peg_bps,
            }
        ]
    )


def test_shadow_normal_state_never_increases_risk() -> None:
    observed = "2026-09-05T00:00:00Z"
    result = compute_shadow_state(
        _hl_rows(observed=observed, z=0.0),
        _stable_row(observed=observed),
        as_of=observed,
        generated_at="2026-09-05T00:05:00Z",
    )
    assert result["state_band"] == "NORMAL"
    assert result["shadow_risk_multiplier"] == 1.0
    assert result["data_confidence"] == "FULL"
    assert result["core_protocol_mutated"] is False


def test_shadow_severe_leverage_pressure_only_derisks() -> None:
    observed = "2026-09-05T00:00:00Z"
    result = compute_shadow_state(
        _hl_rows(observed=observed, z=3.0),
        _stable_row(observed=observed),
        as_of=observed,
        generated_at="2026-09-05T00:05:00Z",
    )
    assert result["leverage_pressure"] == pytest.approx(1.0)
    assert result["state_band"] == "SEVERE"
    assert result["shadow_risk_multiplier"] == pytest.approx(0.50)


def test_invalid_stablecoin_accounting_cannot_trigger_modifier() -> None:
    observed = "2026-09-05T00:00:00Z"
    result = compute_shadow_state(
        _hl_rows(observed=observed, z=3.0, rolling=10),
        _stable_row(
            observed=observed,
            delta_ratio=-0.10,
            delta_coverage=0.20,
            chain_coverage=0.50,
            residual_ratio=0.50,
            peg_bps=500.0,
        ),
        as_of=observed,
        generated_at="2026-09-05T00:05:00Z",
    )
    assert result["data_confidence"] == "NONE"
    assert result["state_pressure"] is None
    assert result["state_band"] == "NO_MODIFIER_DATA_INSUFFICIENT"
    assert result["shadow_risk_multiplier"] == 1.0


def test_future_state_rows_cannot_change_past_snapshot() -> None:
    past = "2026-09-05T00:00:00Z"
    future = "2026-09-05T01:00:00Z"
    hl = pd.concat(
        [_hl_rows(observed=past, z=0.0), _hl_rows(observed=future, z=3.0)],
        ignore_index=True,
    )
    stable = pd.concat(
        [
            _stable_row(observed=past, delta_ratio=0.01),
            _stable_row(observed=future, delta_ratio=-0.03),
        ],
        ignore_index=True,
    )
    result = compute_shadow_state(
        hl,
        stable,
        as_of=past,
        generated_at="2026-09-05T00:05:00Z",
    )
    assert result["state_band"] == "NORMAL"
    assert result["shadow_risk_multiplier"] == 1.0


def test_stablecoin_contraction_can_trigger_severe_shadow_derisk() -> None:
    observed = "2026-09-05T00:00:00Z"
    result = compute_shadow_state(
        _hl_rows(observed=observed, z=0.0),
        _stable_row(observed=observed, delta_ratio=-0.02),
        as_of=observed,
        generated_at="2026-09-05T00:05:00Z",
    )
    assert result["stablecoin"]["supply_contraction_pressure"] == pytest.approx(1.0)
    assert result["state_band"] == "SEVERE"
    assert result["shadow_risk_multiplier"] == pytest.approx(0.50)


def test_shadow_multiplier_preserves_relative_core_risk_weights() -> None:
    weights = pd.Series(0.0, index=frozen_b3_v01.ALL_ASSETS, dtype=float)
    weights["US_EQUITY"] = 0.20
    weights["GOLD"] = 0.10
    weights["BTC"] = 0.10
    weights["CASH"] = 0.60

    scaled = apply_shadow_multiplier(weights, 0.50)

    assert scaled.sum() == pytest.approx(1.0)
    assert scaled["US_EQUITY"] == pytest.approx(0.10)
    assert scaled["GOLD"] == pytest.approx(0.05)
    assert scaled["BTC"] == pytest.approx(0.05)
    assert scaled["CASH"] == pytest.approx(0.80)
    assert scaled["US_EQUITY"] / scaled["GOLD"] == pytest.approx(
        weights["US_EQUITY"] / weights["GOLD"]
    )
    assert scaled["BTC"] / scaled["GOLD"] == pytest.approx(
        weights["BTC"] / weights["GOLD"]
    )


def test_preregistered_state_yaml_matches_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = yaml.safe_load((root / "config" / "state_shadow_v01.yaml").read_text(encoding="utf-8"))
    cfg = StateShadowConfig()

    assert spec["protocol"] == "CROSSALPHA_STATE_SHADOW_V0_1"
    assert spec["mode"] == "SHADOW_ONLY"
    assert spec["core_mutation_allowed"] is False
    assert spec["risk_increase_allowed"] is False
    assert spec["point_in_time"]["max_source_age_minutes"] == cfg.max_source_age_minutes
    assert (
        spec["hyperliquid"]["minimum_rolling_observations_24h"]
        == cfg.min_hyperliquid_rolling_observations
    )
    assert spec["hyperliquid"]["full_stress_z"] == cfg.z_full_stress
    assert (
        spec["stablecoins"]["minimum_delta_7d_market_value_coverage"]
        == cfg.stablecoin_min_delta_7d_coverage
    )
    assert spec["stablecoins"]["chain_coverage_range"] == [
        cfg.stablecoin_min_chain_coverage,
        cfg.stablecoin_max_chain_coverage,
    ]
    assert (
        spec["stablecoins"]["maximum_chain_abs_residual_ratio"]
        == cfg.stablecoin_max_chain_abs_residual_ratio
    )
    assert (
        spec["stablecoins"]["supply_contraction_full_stress_ratio"]
        == cfg.stablecoin_contraction_full_stress
    )
    assert spec["stablecoins"]["peg_full_stress_bps"] == cfg.peg_full_stress_bps
    assert spec["risk_multiplier"]["moderate"]["multiplier"] == cfg.moderate_risk_multiplier
    assert spec["risk_multiplier"]["severe"]["multiplier"] == cfg.severe_risk_multiplier
