from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from crossalpha.state import v03, v03_cycle, v03_prospective, v03_rpc


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def strict_v03_config_report(path: Path) -> dict[str, Any]:
    raw = _load(path)
    source = raw.get("source", {})
    universe = raw.get("borrower_universe", {})
    census = raw.get("census", {})
    cliff = raw.get("liquidation_cliff", {})
    gate = raw.get("prospective_gate", {})
    policy = v03.CensusPolicy()
    frozen_gate = {
        "minimum_calendar_days_before_O2_candidate": 180,
        "minimum_valid_full_censuses": 120,
        "minimum_distinct_cliff_stress_episodes": 5,
        "cliff_episode_critical_debt_share_threshold": 0.05,
        "cliff_episode_cooldown_hours": 24,
        "requires_complete_borrower_bootstrap": True,
        "requires_outcome_linkage_test": True,
        "requires_predeclared_O2_rule": True,
        "automatic_promotion_to_actionable_modifier_allowed": False,
    }
    checks = {
        "protocol": raw.get("protocol") == v03.PROTOCOL,
        "mode": raw.get("mode") == v03.MODE,
        "actionability": raw.get("actionability") == v03.ACTIONABILITY,
        "risk_multiplier_null": raw.get("risk_multiplier") is None,
        "no_core_mutation": raw.get("research_policy", {}).get("mutates_frozen_core") is False,
        "no_v01_mutation": raw.get("research_policy", {}).get("mutates_state_v01") is False,
        "no_ab_mutation": raw.get("research_policy", {}).get("mutates_state_ab_v01") is False,
        "no_v02_mutation": raw.get("research_policy", {}).get("mutates_state_v02") is False,
        "no_parameter_optimization": raw.get("research_policy", {}).get("parameter_optimization_allowed") is False,
        "no_backfill": raw.get("research_policy", {}).get("retrospective_prospective_backfill_allowed") is False,
        "historical_bootstrap_not_evidence": raw.get("research_policy", {}).get("historical_bootstrap_is_evidence") is False,
        "pool_address": str(source.get("aave_v3_core_pool", "")).lower()
        == v03_rpc.AAVE_V3_ETHEREUM_CORE_POOL.lower(),
        "deployment_block": source.get("deployment_block") == v03_rpc.AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
        "borrow_topic": str(source.get("borrow_event_topic0", "")).lower()
        == v03_rpc.BORROW_EVENT_TOPIC0,
        "account_selector": str(source.get("get_user_account_data_selector", "")).lower()
        == v03_rpc.GET_USER_ACCOUNT_DATA_SELECTOR,
        "base_decimals": source.get("ethereum_core_base_currency_decimals") == 8,
        "bootstrap_start": universe.get("bootstrap_start_block") == v03_rpc.AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
        "bootstrap_chunk": universe.get("bootstrap_chunk_blocks") == v03_cycle.BOOTSTRAP_CHUNK_BLOCKS,
        "bootstrap_chunks_per_cycle": universe.get("max_bootstrap_chunks_per_cycle")
        == v03_cycle.MAX_BOOTSTRAP_CHUNKS_PER_CYCLE,
        "adaptive_minimum_span": universe.get("adaptive_minimum_span_blocks") == 256,
        "finality_lag": universe.get("finality_lag_blocks") == v03_cycle.FINALITY_LAG_BLOCKS,
        "batch_size": census.get("rpc_batch_size") == v03_rpc.RpcPolicy().batch_size,
        "failed_call_ratio": census.get("maximum_failed_call_ratio_for_valid_census")
        == policy.maximum_failed_call_ratio,
        "full_census_cadence": census.get("full_census_cadence_minutes")
        == v03_cycle.FULL_CENSUS_CADENCE_MINUTES,
        "watchlist_hf": census.get("watchlist_health_factor_max")
        == policy.watchlist_health_factor_max,
        "watchlist_debt": census.get("watchlist_debt_usd_min") == policy.watchlist_debt_usd_min,
        "hf_scale_decimals": census.get("health_factor_scale_decimals") == 18,
        "hf_thresholds": tuple(float(value) for value in census.get("thresholds", []))
        == v03.HF_THRESHOLDS,
        "hf_bands": tuple(
            (float(pair[0]), None if pair[1] is None else float(pair[1]))
            for pair in cliff.get("hf_bands", [])
        )
        == v03.HF_BANDS,
        "prospective_gate": gate == frozen_gate,
        "prospective_protocol_name": v03_prospective.PROSPECTIVE_PROTOCOL
        == "CROSSALPHA_STATE_V0_3_PROSPECTIVE",
    }
    return {
        "protocol": v03.PROTOCOL,
        "audit_level": "STRICT_CONFIG_IMPLEMENTATION_MATCH",
        "ok": all(checks.values()),
        "checks": checks,
    }
