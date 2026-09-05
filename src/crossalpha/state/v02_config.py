from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from crossalpha.state.v02 import ACTIONABILITY, MODE, PROTOCOL, StateV02Config


def strict_v02_config_consistency_report(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = StateV02Config()
    components = raw.get("components", {})
    aave = components.get("aave_market_stress", {})
    stable = components.get("stablecoin_flow_decomposition", {})
    basis = components.get("basis_dispersion", {})
    contagion = components.get("contagion_graph", {})
    aggregation = raw.get("aggregation", {})
    research = raw.get("research_policy", {})
    borrower = components.get("borrower_health_factor_distribution", {})

    checks = {
        "protocol": raw.get("protocol") == PROTOCOL,
        "mode": raw.get("mode") == MODE,
        "actionability": research.get("actionability") == ACTIONABILITY,
        "risk_multiplier_null": research.get("risk_multiplier") is None,
        "mutates_frozen_core_false": research.get("mutates_frozen_core") is False,
        "mutates_state_v01_false": research.get("mutates_state_v01") is False,
        "mutates_state_ab_v01_false": research.get("mutates_state_ab_v01") is False,
        "parameter_optimization_disabled": research.get("parameter_optimization_allowed") is False,
        "retrospective_backfill_disabled": research.get("retrospective_prospective_backfill_allowed") is False,
        "historical_promotion_disabled": research.get("historical_data_can_promote_to_O2") is False,
        "aave_max_age": aave.get("max_source_age_minutes") == cfg.max_source_age_minutes,
        "aave_minimum_reserves": aave.get("minimum_reserves") == cfg.aave_minimum_reserves,
        "aave_apy_threshold": aave.get("borrow_apy_full_stress_pct") == cfg.aave_borrow_apy_full_stress_pct,
        "aave_liquidity_threshold": aave.get("low_available_liquidity_full_stress_usd") == cfg.aave_low_available_liquidity_full_stress_usd,
        "stable_lookback": stable.get("lookback_hours") == cfg.stablecoin_lookback_hours,
        "stable_lag_tolerance": stable.get("lag_tolerance_hours") == cfg.stablecoin_lag_tolerance_hours,
        "stable_chain_coverage": stable.get("minimum_chain_coverage") == cfg.stablecoin_min_chain_coverage,
        "stable_residual_ratio": stable.get("maximum_chain_abs_residual_ratio") == cfg.stablecoin_max_chain_abs_residual_ratio,
        "stable_contraction_threshold": stable.get("contraction_full_stress_ratio") == cfg.stablecoin_contraction_full_stress_ratio,
        "stable_migration_reference": stable.get("migration_full_reference_ratio") == cfg.stablecoin_migration_full_reference_ratio,
        "basis_threshold": basis.get("full_stress_z_dispersion") == cfg.basis_full_stress_z_dispersion,
        "contagion_min_stablecoin": contagion.get("minimum_stablecoin_market_value_usd") == cfg.contagion_min_stablecoin_market_value_usd,
        "contagion_min_chain": contagion.get("minimum_chain_market_value_usd") == cfg.contagion_min_chain_market_value_usd,
        "minimum_valid_components": aggregation.get("minimum_valid_components") == cfg.minimum_valid_components,
        "full_confidence_components": aggregation.get("full_confidence_components") == cfg.full_confidence_components,
        "weights": aggregation.get("component_weights") == {
            "aave_market_stress": cfg.aave_weight,
            "stablecoin_flow_stress": cfg.stablecoin_weight,
            "basis_dispersion_stress": cfg.basis_weight,
            "contagion_connectivity_stress": cfg.contagion_weight,
        },
        "borrower_health_not_substituted": borrower.get("no_market_level_substitution_allowed") is True,
        "borrower_liquidation_threshold": borrower.get("liquidation_threshold_health_factor") == 1.0,
    }
    return {
        "protocol": PROTOCOL,
        "audit_level": "STRICT_CONFIG_IMPLEMENTATION_CONSISTENCY",
        "ok": all(checks.values()),
        "checks": checks,
    }
