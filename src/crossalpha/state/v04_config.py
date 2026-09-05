from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from crossalpha.state import v04
from crossalpha.state import v04_provider


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_v04_config_report(path: Path) -> dict[str, Any]:
    raw = _load(path)
    universe = raw.get("universe", {})
    cadence = raw.get("cadence", {})
    normalization = raw.get("normalization", {})
    policy = raw.get("research_policy", {})
    venues = raw.get("venues", {})
    gate = raw.get("prospective_gate", {})
    safe_provider = _repo_root() / "src" / "crossalpha" / "state" / "v04_safe_provider.py"
    frozen_gate = {
        "minimum_calendar_days_before_O2_candidate": 180,
        "minimum_observations": 500,
        "minimum_valid_venue_share": 0.95,
        "requires_outcome_linkage_test": True,
        "requires_predeclared_O2_rule": True,
        "automatic_promotion_to_actionable_modifier_allowed": False,
    }
    checks = {
        "protocol": raw.get("protocol") == v04.PROTOCOL,
        "mode": raw.get("mode") == v04.MODE,
        "actionability": raw.get("actionability") == v04.ACTIONABILITY,
        "risk_multiplier_null": raw.get("risk_multiplier") is None,
        "no_predecessor_mutation": policy.get("mutates_predecessors") is False,
        "no_parameter_optimization": policy.get("parameter_optimization_allowed") is False,
        "no_backfill": policy.get("retrospective_prospective_backfill_allowed") is False,
        "no_auto_actionability": policy.get("automatic_actionability_allowed") is False,
        "historical_not_evidence": policy.get("historical_backfill_is_evidence") is False,
        "zero_cost": policy.get("required_data_cost_usd") == 0,
        "fault_isolation_module_hash": safe_provider.exists()
        and policy.get("fault_isolation_module_sha256") == _sha256(safe_provider),
        "assets": tuple(universe.get("assets", [])) == v04.ASSETS == v04_provider.ASSETS,
        "venues": tuple(universe.get("venues", [])) == v04.VENUES == v04_provider.VENUES,
        "minimum_venues": universe.get("minimum_valid_venues") == v04.MINIMUM_VALID_VENUES,
        "full_venues": universe.get("full_confidence_venues") == v04.FULL_CONFIDENCE_VENUES,
        "cadence_minutes": cadence.get("observation_minutes") == 5,
        "max_age": cadence.get("maximum_snapshot_age_seconds") == v04.MAXIMUM_SNAPSHOT_AGE_SECONDS,
        "funding_semantics": normalization.get("funding_semantics")
        == v04_provider.FUNDING_SEMANTICS
        == "LATEST_SETTLED_NORMALIZED_TO_8H",
        "funding_period": normalization.get("funding_comparison_period_hours") == 8,
        "funding_interval_source": normalization.get("funding_interval_source")
        == "difference_between_latest_two_settlement_timestamps",
        "funding_unknown_policy": normalization.get("funding_unknown_interval_policy")
        == "exclude_from_cross_venue_funding_dispersion",
        "oi_unit": normalization.get("open_interest_unit") == "USD_NOTIONAL",
        "binance_funding_endpoint": venues.get("binance", {}).get("settled_funding_history")
        == "/fapi/v1/fundingRate",
        "binance_funding_fields": (
            venues.get("binance", {}).get("settled_rate_field") == "fundingRate"
            and venues.get("binance", {}).get("settlement_time_field") == "fundingTime"
        ),
        "okx_funding_endpoint": venues.get("okx", {}).get("settled_funding_history")
        == "/api/v5/public/funding-rate-history",
        "okx_funding_fields": (
            venues.get("okx", {}).get("settled_rate_field") == "realizedRate"
            and venues.get("okx", {}).get("settlement_time_field") == "fundingTime"
        ),
        "bybit_funding_endpoint": venues.get("bybit", {}).get("settled_funding_history")
        == "/v5/market/funding/history",
        "bybit_funding_fields": (
            venues.get("bybit", {}).get("settled_rate_field") == "fundingRate"
            and venues.get("bybit", {}).get("settlement_time_field")
            == "fundingRateTimestamp"
        ),
        "no_composite": raw.get("features", {}).get("no_composite_stress_score") is True,
        "prospective_gate": gate == frozen_gate,
    }
    return {
        "protocol": v04.PROTOCOL,
        "audit_level": "STRICT_CONFIG_IMPLEMENTATION_MATCH_WITH_FAULT_ISOLATION_HASH",
        "ok": all(checks.values()),
        "checks": checks,
    }
