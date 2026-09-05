from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from crossalpha.state import v02


T0 = pd.Timestamp("2026-09-01T00:00:00Z")
T1 = pd.Timestamp("2026-09-08T00:00:00Z")
GEN = pd.Timestamp("2026-09-08T00:05:00Z")


def _aave_snapshot(timestamp: pd.Timestamp, *, liquidity: float = 20_000_000.0, apy: float = 5.0) -> list[dict]:
    rows = []
    for symbol in ("WETH", "WBTC", "USDC"):
        rows.append(
            {
                "observed_at": timestamp,
                "known_at": timestamp,
                "market_name": "Aave V3 Ethereum Core",
                "market_address": "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
                "symbol": symbol,
                "borrow_apy_pct": apy,
                "available_liquidity_usd": liquidity,
                "borrow_cap_reached": False,
                "is_frozen": False,
                "is_paused": False,
            }
        )
    return rows


def _stable_system(t0_total: float = 100.0, t1_total: float = 100.0, coverage: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "observed_at": T0,
                "known_at": T0,
                "usd_market_value_usd": t0_total,
                "chain_coverage_ratio": coverage,
                "chain_abs_residual_ratio": 0.0,
            },
            {
                "observed_at": T1,
                "known_at": T1,
                "usd_market_value_usd": t1_total,
                "chain_coverage_ratio": coverage,
                "chain_abs_residual_ratio": 0.0,
            },
        ]
    )


def _chain_state(t0: tuple[float, float], t1: tuple[float, float]) -> pd.DataFrame:
    rows = []
    for ts, values in ((T0, t0), (T1, t1)):
        for chain, value in zip(("Ethereum", "Tron"), values, strict=True):
            rows.append(
                {
                    "observed_at": ts,
                    "known_at": ts,
                    "chain": chain,
                    "market_value_usd": value,
                }
            )
    return pd.DataFrame(rows)


def _hl_state(btc: float = 0.0, eth: float = 0.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"observed_at": T1, "known_at": T1, "asset": "BTC", "basis_z_24h": btc},
            {"observed_at": T1, "known_at": T1, "asset": "ETH", "basis_z_24h": eth},
        ]
    )


def _composition(similar: bool = True) -> pd.DataFrame:
    if similar:
        values = {
            ("Ethereum", "usdt"): 60_000_000.0,
            ("Ethereum", "usdc"): 40_000_000.0,
            ("Tron", "usdt"): 120_000_000.0,
            ("Tron", "usdc"): 80_000_000.0,
        }
    else:
        values = {
            ("Ethereum", "usdt"): 100_000_000.0,
            ("Ethereum", "usdc"): 0.0,
            ("Tron", "usdt"): 0.0,
            ("Tron", "usdc"): 100_000_000.0,
        }
    return pd.DataFrame(
        [
            {
                "observed_at": T1,
                "known_at": T1,
                "chain": chain,
                "stablecoin_id": stable,
                "market_value_usd": value,
            }
            for (chain, stable), value in values.items()
        ]
    )


def test_future_aave_rows_cannot_change_asof_pressure() -> None:
    rows = _aave_snapshot(T1, liquidity=5_000_000.0, apy=10.0)
    rows += _aave_snapshot(T1 + pd.Timedelta(hours=1), liquidity=1.0, apy=100.0)
    component = v02._aave_market_component(
        pd.DataFrame(rows),
        as_of=T1,
        generated_at=GEN,
        cfg=v02.StateV02Config(),
    )
    assert component["valid"] is True
    assert component["pressure"] == pytest.approx(0.5)
    assert component["observed_at"] == T1.isoformat()


def test_chain_migration_is_not_misclassified_as_system_liquidity_stress() -> None:
    component = v02._stablecoin_flow_component(
        _stable_system(100.0, 100.0),
        _chain_state((60.0, 40.0), (40.0, 60.0)),
        as_of=T1,
        generated_at=GEN,
        cfg=v02.StateV02Config(),
    )
    assert component["valid"] is True
    assert component["net_system_change_usd"] == pytest.approx(0.0)
    assert component["offsetting_chain_migration_proxy_usd"] == pytest.approx(20.0)
    assert component["migration_ratio"] == pytest.approx(0.20)
    assert component["pressure"] == pytest.approx(0.0)
    assert "migration_is_not_stress" in component["pressure_semantics"]


def test_two_percent_system_contraction_reaches_full_stablecoin_stress() -> None:
    component = v02._stablecoin_flow_component(
        _stable_system(100.0, 98.0),
        _chain_state((60.0, 40.0), (58.0, 40.0)),
        as_of=T1,
        generated_at=GEN,
        cfg=v02.StateV02Config(),
    )
    assert component["valid"] is True
    assert component["net_system_change_ratio"] == pytest.approx(-0.02)
    assert component["pressure"] == pytest.approx(1.0)


def test_stablecoin_accounting_gate_fails_closed() -> None:
    component = v02._stablecoin_flow_component(
        _stable_system(100.0, 100.0, coverage=0.90),
        _chain_state((60.0, 40.0), (60.0, 40.0)),
        as_of=T1,
        generated_at=GEN,
        cfg=v02.StateV02Config(),
    )
    assert component["valid"] is False
    assert component["reason"] == "stablecoin_accounting_gate_failed"


def test_basis_dispersion_is_cross_asset_hyperliquid_only() -> None:
    component = v02._basis_dispersion_component(
        _hl_state(btc=2.0, eth=-1.0),
        as_of=T1,
        generated_at=GEN,
        cfg=v02.StateV02Config(),
    )
    assert component["valid"] is True
    assert component["basis_z_dispersion"] == pytest.approx(3.0)
    assert component["pressure"] == pytest.approx(1.0)
    assert component["scope"] == "CROSS_ASSET_HYPERLIQUID_ONLY"
    assert component["multi_venue_claim_allowed"] is False


def test_contagion_overlap_distinguishes_shared_from_orthogonal_composition() -> None:
    cfg = v02.StateV02Config()
    shared = v02._contagion_component(
        _composition(similar=True), as_of=T1, generated_at=GEN, cfg=cfg
    )
    orthogonal = v02._contagion_component(
        _composition(similar=False), as_of=T1, generated_at=GEN, cfg=cfg
    )
    assert shared["valid"] is True
    assert orthogonal["valid"] is True
    assert shared["weighted_chain_composition_cosine_overlap"] == pytest.approx(1.0)
    assert orthogonal["weighted_chain_composition_cosine_overlap"] == pytest.approx(0.0)
    assert shared["pressure"] > orthogonal["pressure"]
    assert "not_causal_contagion_proof" in shared["interpretation"]


def test_v02_never_becomes_a_risk_multiplier_and_never_substitutes_market_for_HF() -> None:
    snapshot = v02.compute_state_v02(
        pd.DataFrame(_aave_snapshot(T1)),
        _stable_system(100.0, 100.0),
        _chain_state((60.0, 40.0), (60.0, 40.0)),
        _hl_state(btc=0.5, eth=-0.5),
        _composition(similar=True),
        pd.DataFrame(),
        as_of=T1,
        generated_at=GEN,
    )
    assert snapshot["actionability"] == "DESCRIPTIVE_ONLY"
    assert snapshot["risk_multiplier"] is None
    assert snapshot["mutates_frozen_core"] is False
    assert snapshot["mutates_state_v01"] is False
    assert snapshot["mutates_state_ab_v01"] is False
    health = snapshot["borrower_health_factor_distribution"]
    assert health["valid"] is False
    assert health["status"] == "REQUIRES_AUDITABLE_BORROWER_UNIVERSE"
    assert health["market_level_substitution_allowed"] is False


def test_state_v02_yaml_locks_all_material_thresholds() -> None:
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config" / "state_v02.yaml").read_text(encoding="utf-8"))
    cfg = v02.StateV02Config()
    stable = raw["components"]["stablecoin_flow_decomposition"]
    contagion = raw["components"]["contagion_graph"]
    aggregation = raw["aggregation"]
    assert stable["contraction_full_stress_ratio"] == cfg.stablecoin_contraction_full_stress_ratio
    assert stable["migration_full_reference_ratio"] == cfg.stablecoin_migration_full_reference_ratio
    assert stable["pressure_semantics"] == "system_contraction_only"
    assert contagion["minimum_stablecoin_market_value_usd"] == cfg.contagion_min_stablecoin_market_value_usd
    assert contagion["minimum_chain_market_value_usd"] == cfg.contagion_min_chain_market_value_usd
    assert aggregation["minimum_valid_components"] == cfg.minimum_valid_components
    assert aggregation["full_confidence_components"] == cfg.full_confidence_components
    assert raw["research_policy"]["actionability"] == "DESCRIPTIVE_ONLY"
    assert raw["research_policy"]["risk_multiplier"] is None
