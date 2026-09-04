from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from crossalpha.core import frozen_b3_v01


PROTOCOL = "CROSSALPHA_STATE_SHADOW_V0_1"
MODE = "SHADOW_ONLY"
FOCUS_ASSETS = ("BTC", "ETH")


@dataclass(frozen=True)
class StateShadowConfig:
    max_source_age_minutes: int = 30
    min_hyperliquid_rolling_observations: int = 24
    stablecoin_min_delta_7d_coverage: float = 0.80
    stablecoin_min_chain_coverage: float = 0.98
    stablecoin_max_chain_coverage: float = 1.02
    stablecoin_max_chain_abs_residual_ratio: float = 0.02
    z_full_stress: float = 3.0
    stablecoin_contraction_full_stress: float = 0.02
    peg_full_stress_bps: float = 100.0
    moderate_pressure_threshold: float = 1.0 / 3.0
    severe_pressure_threshold: float = 2.0 / 3.0
    moderate_risk_multiplier: float = 0.75
    severe_risk_multiplier: float = 0.50


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _clip01(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return float(min(1.0, max(0.0, number)))


def _positive_z_pressure(value: Any, full_stress: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or full_stress <= 0:
        return None
    return _clip01(max(0.0, number) / full_stress)


def _select_latest_known(
    frame: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    generated_at: pd.Timestamp,
    asset: str | None = None,
) -> pd.Series | None:
    if frame.empty:
        return None
    data = frame.copy()
    data["observed_at"] = pd.to_datetime(data["observed_at"], utc=True)
    data["known_at"] = pd.to_datetime(data["known_at"], utc=True)
    mask = (data["observed_at"] <= as_of) & (data["known_at"] <= generated_at)
    if asset is not None:
        mask &= data["asset"].astype(str) == asset
    part = data.loc[mask].sort_values(["observed_at", "known_at"])
    if part.empty:
        return None
    return part.iloc[-1]


def _source_fresh(observed_at: Any, generated_at: pd.Timestamp, max_age_minutes: int) -> bool:
    if observed_at is None or pd.isna(observed_at):
        return False
    age = generated_at - _utc(observed_at)
    return pd.Timedelta(0) <= age <= pd.Timedelta(minutes=max_age_minutes)


def _hyperliquid_component(
    row: pd.Series | None,
    *,
    generated_at: pd.Timestamp,
    config: StateShadowConfig,
) -> dict[str, Any]:
    if row is None:
        return {"valid": False, "reason": "missing"}
    required = ("funding_z_24h", "basis_z_24h", "oi_change_z_24h", "spread_z_24h")
    if int(row.get("rolling_observations_24h", 0) or 0) < config.min_hyperliquid_rolling_observations:
        return {"valid": False, "reason": "insufficient_rolling_observations"}
    if not _source_fresh(row.get("observed_at"), generated_at, config.max_source_age_minutes):
        return {"valid": False, "reason": "stale"}

    pressures = {
        name: _positive_z_pressure(row.get(name), config.z_full_stress)
        for name in required
    }
    if any(value is None for value in pressures.values()):
        return {"valid": False, "reason": "missing_causal_zscore"}
    pressure = float(np.mean(list(pressures.values())))
    return {
        "valid": True,
        "reason": "ok",
        "observed_at": _utc(row["observed_at"]).isoformat(),
        "known_at": _utc(row["known_at"]).isoformat(),
        "rolling_observations_24h": int(row["rolling_observations_24h"]),
        "funding_z_24h": float(row["funding_z_24h"]),
        "basis_z_24h": float(row["basis_z_24h"]),
        "oi_change_z_24h": float(row["oi_change_z_24h"]),
        "spread_z_24h": float(row["spread_z_24h"]),
        "pressure": pressure,
        "component_pressures": pressures,
    }


def _stablecoin_component(
    row: pd.Series | None,
    *,
    generated_at: pd.Timestamp,
    config: StateShadowConfig,
) -> dict[str, Any]:
    if row is None:
        return {"valid": False, "reason": "missing"}
    if not _source_fresh(row.get("observed_at"), generated_at, config.max_source_age_minutes):
        return {"valid": False, "reason": "stale"}

    def number(name: str) -> float | None:
        value = pd.to_numeric(pd.Series([row.get(name)]), errors="coerce").iloc[0]
        return float(value) if pd.notna(value) and math.isfinite(float(value)) else None

    supply = number("usd_supply_native")
    delta_7d = number("usd_delta_7d_native")
    delta_coverage = number("delta_7d_market_value_coverage")
    chain_coverage = number("chain_coverage_ratio")
    residual_ratio = number("chain_abs_residual_ratio")
    peg_bps = number("weighted_abs_peg_deviation_bps")

    accounting_valid = bool(
        supply is not None
        and supply > 0
        and delta_7d is not None
        and delta_coverage is not None
        and delta_coverage >= config.stablecoin_min_delta_7d_coverage
        and chain_coverage is not None
        and config.stablecoin_min_chain_coverage <= chain_coverage <= config.stablecoin_max_chain_coverage
        and residual_ratio is not None
        and residual_ratio <= config.stablecoin_max_chain_abs_residual_ratio
    )
    if not accounting_valid:
        return {
            "valid": False,
            "reason": "accounting_or_coverage_gate_failed",
            "delta_7d_market_value_coverage": delta_coverage,
            "chain_coverage_ratio": chain_coverage,
            "chain_abs_residual_ratio": residual_ratio,
        }

    contraction_ratio = max(0.0, -delta_7d / supply)
    contraction_pressure = _clip01(
        contraction_ratio / config.stablecoin_contraction_full_stress
    )
    peg_pressure = (
        _clip01(peg_bps / config.peg_full_stress_bps) if peg_bps is not None else 0.0
    )
    pressure = float(max(contraction_pressure or 0.0, peg_pressure or 0.0))
    return {
        "valid": True,
        "reason": "ok",
        "observed_at": _utc(row["observed_at"]).isoformat(),
        "known_at": _utc(row["known_at"]).isoformat(),
        "usd_supply_native": supply,
        "usd_delta_7d_native": delta_7d,
        "delta_7d_ratio": delta_7d / supply,
        "delta_7d_market_value_coverage": delta_coverage,
        "chain_coverage_ratio": chain_coverage,
        "chain_abs_residual_ratio": residual_ratio,
        "weighted_abs_peg_deviation_bps": peg_bps,
        "supply_contraction_pressure": contraction_pressure,
        "peg_pressure": peg_pressure,
        "pressure": pressure,
    }


def _multiplier_from_pressure(pressure: float | None, config: StateShadowConfig) -> tuple[str, float]:
    if pressure is None:
        return "NO_MODIFIER_DATA_INSUFFICIENT", 1.0
    if pressure >= config.severe_pressure_threshold:
        return "SEVERE", config.severe_risk_multiplier
    if pressure >= config.moderate_pressure_threshold:
        return "MODERATE", config.moderate_risk_multiplier
    return "NORMAL", 1.0


def compute_shadow_state(
    hyperliquid_market_state: pd.DataFrame,
    stablecoin_system_state: pd.DataFrame,
    *,
    as_of: Any,
    generated_at: Any | None = None,
    config: StateShadowConfig | None = None,
) -> dict[str, Any]:
    """Compute the pre-registered State v0.1 shadow risk overlay.

    The output is deliberately not a trading signal.  It can only reduce the gross
    risk of an already-frozen Core allocation.  Rows later than ``as_of`` or not yet
    known by ``generated_at`` are excluded, preserving point-in-time semantics.
    """
    cfg = config or StateShadowConfig()
    as_of_ts = _utc(as_of)
    generated_ts = _utc(generated_at or datetime.now(timezone.utc))
    if generated_ts < as_of_ts:
        raise ValueError("generated_at cannot precede as_of")

    hl: dict[str, Any] = {}
    valid_hl_pressures: list[float] = []
    for asset in FOCUS_ASSETS:
        selected = _select_latest_known(
            hyperliquid_market_state,
            as_of=as_of_ts,
            generated_at=generated_ts,
            asset=asset,
        )
        component = _hyperliquid_component(selected, generated_at=generated_ts, config=cfg)
        hl[asset] = component
        if component.get("valid"):
            valid_hl_pressures.append(float(component["pressure"]))

    stable_row = _select_latest_known(
        stablecoin_system_state,
        as_of=as_of_ts,
        generated_at=generated_ts,
    )
    stable = _stablecoin_component(stable_row, generated_at=generated_ts, config=cfg)

    leverage_pressure = max(valid_hl_pressures) if valid_hl_pressures else None
    stablecoin_pressure = float(stable["pressure"]) if stable.get("valid") else None
    valid_pressures = [value for value in (leverage_pressure, stablecoin_pressure) if value is not None]
    state_pressure = max(valid_pressures) if valid_pressures else None
    band, multiplier = _multiplier_from_pressure(state_pressure, cfg)

    source_count = len(valid_hl_pressures) + int(bool(stable.get("valid")))
    expected_count = len(FOCUS_ASSETS) + 1
    if source_count == expected_count:
        confidence = "FULL"
    elif source_count > 0:
        confidence = "PARTIAL"
    else:
        confidence = "NONE"

    return {
        "protocol": PROTOCOL,
        "mode": MODE,
        "shadow_only": True,
        "core_protocol_mutated": False,
        "as_of": as_of_ts.isoformat(),
        "generated_at": generated_ts.isoformat(),
        "focus_assets": list(FOCUS_ASSETS),
        "hyperliquid": hl,
        "stablecoin": stable,
        "leverage_pressure": leverage_pressure,
        "stablecoin_pressure": stablecoin_pressure,
        "state_pressure": state_pressure,
        "state_band": band,
        "shadow_risk_multiplier": float(multiplier),
        "data_confidence": confidence,
        "valid_source_components": source_count,
        "expected_source_components": expected_count,
        "interpretation": (
            "Shadow-only de-risking overlay. It never changes relative Core weights, "
            "never increases risk above Frozen B3, and is not part of the B3 paper ledger."
        ),
    }


def apply_shadow_multiplier(
    frozen_weights: pd.Series,
    multiplier: float,
) -> pd.Series:
    """Scale Frozen B3 risky assets uniformly and move released notional to CASH."""
    if not (0.0 <= float(multiplier) <= 1.0):
        raise ValueError("state shadow multiplier must be in [0, 1]")
    weights = pd.to_numeric(
        frozen_weights.reindex(frozen_b3_v01.ALL_ASSETS), errors="coerce"
    ).fillna(0.0)
    if (weights < -1e-12).any():
        raise ValueError("frozen weights contain negative exposure")
    if abs(float(weights.sum()) - 1.0) > 1e-8:
        raise ValueError("frozen weights must sum to one")

    result = weights.copy()
    risk = list(frozen_b3_v01.RISK_ASSETS)
    result.loc[risk] = weights.loc[risk] * float(multiplier)
    result.loc["CASH"] = 1.0 - float(result.loc[risk].sum())
    return result


def _latest_input_frame(root: Path) -> pd.DataFrame:
    files = sorted(root.glob("year=*/month=*/day=*/*.parquet"))
    if not files:
        return pd.DataFrame()
    latest_day = files[-1].parent
    day_files = sorted(latest_day.glob("*.parquet"))
    return pd.concat((pd.read_parquet(path) for path in day_files), ignore_index=True)


def _flatten_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "protocol": snapshot["protocol"],
        "mode": snapshot["mode"],
        "shadow_only": snapshot["shadow_only"],
        "core_protocol_mutated": snapshot["core_protocol_mutated"],
        "as_of": snapshot["as_of"],
        "generated_at": snapshot["generated_at"],
        "leverage_pressure": snapshot["leverage_pressure"],
        "stablecoin_pressure": snapshot["stablecoin_pressure"],
        "state_pressure": snapshot["state_pressure"],
        "state_band": snapshot["state_band"],
        "shadow_risk_multiplier": snapshot["shadow_risk_multiplier"],
        "data_confidence": snapshot["data_confidence"],
        "valid_source_components": snapshot["valid_source_components"],
        "expected_source_components": snapshot["expected_source_components"],
    }
    for asset in FOCUS_ASSETS:
        comp = snapshot["hyperliquid"][asset]
        prefix = asset.lower()
        row[f"{prefix}_valid"] = bool(comp.get("valid", False))
        row[f"{prefix}_pressure"] = comp.get("pressure")
        for field in ("funding_z_24h", "basis_z_24h", "oi_change_z_24h", "spread_z_24h"):
            row[f"{prefix}_{field}"] = comp.get(field)
    stable = snapshot["stablecoin"]
    row["stablecoin_valid"] = bool(stable.get("valid", False))
    for field in (
        "delta_7d_ratio",
        "delta_7d_market_value_coverage",
        "chain_coverage_ratio",
        "chain_abs_residual_ratio",
        "weighted_abs_peg_deviation_bps",
        "supply_contraction_pressure",
        "peg_pressure",
    ):
        row[field] = stable.get(field)
    row["details_json"] = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return row


def build_latest_shadow_state(
    data_root: Path,
    *,
    generated_at: Any | None = None,
    write: bool = True,
    config: StateShadowConfig | None = None,
) -> dict[str, Any]:
    """Build one latest State v0.1 shadow snapshot from existing free Observatory data."""
    hl = _latest_input_frame(data_root / "derived" / "hyperliquid" / "market_state")
    stable = _latest_input_frame(data_root / "derived" / "stablecoins" / "system_state")
    if hl.empty and stable.empty:
        return {
            "protocol": PROTOCOL,
            "mode": MODE,
            "status": "no_inputs",
            "written": False,
        }

    maxima: list[pd.Timestamp] = []
    for frame in (hl, stable):
        if not frame.empty:
            maxima.append(pd.to_datetime(frame["observed_at"], utc=True).max())
    as_of = min(maxima) if len(maxima) > 1 else maxima[0]
    generated_ts = _utc(generated_at or datetime.now(timezone.utc))
    snapshot = compute_shadow_state(
        hl,
        stable,
        as_of=as_of,
        generated_at=generated_ts,
        config=config,
    )
    if not write:
        return {**snapshot, "status": "computed", "written": False}

    row = _flatten_snapshot(snapshot)
    frame = pd.DataFrame([row])
    frame["as_of"] = pd.to_datetime(frame["as_of"], utc=True)
    frame["generated_at"] = pd.to_datetime(frame["generated_at"], utc=True)
    ts = _utc(snapshot["as_of"])
    out_dir = (
        data_root
        / "derived"
        / "state"
        / "shadow_v01"
        / f"year={ts:%Y}"
        / f"month={ts:%m}"
        / f"day={ts:%d}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"state_at={ts:%H%M%S%f}.parquet"
    tmp = out_path.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(out_path)
    return {
        **snapshot,
        "status": "written",
        "written": True,
        "output": str(out_path),
    }
