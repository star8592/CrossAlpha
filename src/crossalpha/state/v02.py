from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


PROTOCOL = "CROSSALPHA_STATE_V0_2"
MODE = "PROSPECTIVE_DESCRIPTIVE_SHADOW"
ACTIONABILITY = "DESCRIPTIVE_ONLY"


@dataclass(frozen=True)
class StateV02Config:
    max_source_age_minutes: int = 30
    aave_minimum_reserves: int = 3
    aave_borrow_apy_full_stress_pct: float = 20.0
    aave_low_available_liquidity_full_stress_usd: float = 10_000_000.0
    stablecoin_lookback_hours: int = 168
    stablecoin_lag_tolerance_hours: int = 24
    stablecoin_min_chain_coverage: float = 0.98
    stablecoin_max_chain_abs_residual_ratio: float = 0.02
    stablecoin_contraction_full_stress_ratio: float = 0.02
    stablecoin_migration_full_reference_ratio: float = 0.10
    basis_full_stress_z_dispersion: float = 3.0
    contagion_min_stablecoin_market_value_usd: float = 10_000_000.0
    contagion_min_chain_market_value_usd: float = 50_000_000.0
    aave_weight: float = 0.30
    stablecoin_weight: float = 0.30
    basis_weight: float = 0.20
    contagion_weight: float = 0.20
    minimum_valid_components: int = 2
    full_confidence_components: int = 4


FOCUS_AAVE_SYMBOLS = ("WETH", "ETH", "WBTC", "USDC", "USDT", "GHO", "DAI")


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


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _prepare_pti(frame: pd.DataFrame, *, as_of: pd.Timestamp, known_at: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    data = frame.copy()
    if "observed_at" not in data.columns or "known_at" not in data.columns:
        return pd.DataFrame(columns=data.columns)
    data["observed_at"] = pd.to_datetime(data["observed_at"], utc=True, errors="coerce")
    data["known_at"] = pd.to_datetime(data["known_at"], utc=True, errors="coerce")
    return data.loc[(data["observed_at"] <= as_of) & (data["known_at"] <= known_at)].copy()


def _fresh(observed_at: Any, generated_at: pd.Timestamp, max_age_minutes: int) -> bool:
    if observed_at is None or pd.isna(observed_at):
        return False
    age = generated_at - _utc(observed_at)
    return pd.Timedelta(0) <= age <= pd.Timedelta(minutes=max_age_minutes)


def _closest_timestamp(
    values: pd.Series,
    target: pd.Timestamp,
    tolerance: pd.Timedelta,
) -> pd.Timestamp | None:
    timestamps = pd.to_datetime(values, utc=True, errors="coerce").dropna().drop_duplicates()
    if timestamps.empty:
        return None
    diffs = (timestamps - target).abs()
    idx = diffs.argmin()
    chosen = timestamps.iloc[int(idx)]
    return chosen if abs(chosen - target) <= tolerance else None


def _aave_market_component(
    aave: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    generated_at: pd.Timestamp,
    cfg: StateV02Config,
) -> dict[str, Any]:
    data = _prepare_pti(aave, as_of=as_of, known_at=generated_at)
    if data.empty:
        return {"valid": False, "reason": "missing_aave_market_snapshots"}
    latest_ts = data["observed_at"].max()
    if not _fresh(latest_ts, generated_at, cfg.max_source_age_minutes):
        return {"valid": False, "reason": "stale_aave_market_snapshot", "observed_at": latest_ts.isoformat()}
    latest = data.loc[data["observed_at"] == latest_ts].copy()
    latest["symbol"] = latest["symbol"].astype(str).str.upper()
    focus = latest.loc[latest["symbol"].isin(FOCUS_AAVE_SYMBOLS)].copy()
    if len(focus) < cfg.aave_minimum_reserves:
        return {
            "valid": False,
            "reason": "insufficient_focus_reserves",
            "observed_at": latest_ts.isoformat(),
            "focus_reserve_count": int(len(focus)),
        }

    rows: list[dict[str, Any]] = []
    reserve_pressures: list[float] = []
    for row in focus.itertuples(index=False):
        apy = _number(getattr(row, "borrow_apy_pct", None))
        liquidity = _number(getattr(row, "available_liquidity_usd", None))
        apy_pressure = _clip01((apy or 0.0) / cfg.aave_borrow_apy_full_stress_pct) if apy is not None else None
        liquidity_pressure = None
        if liquidity is not None:
            liquidity_pressure = _clip01(
                max(0.0, cfg.aave_low_available_liquidity_full_stress_usd - liquidity)
                / cfg.aave_low_available_liquidity_full_stress_usd
            )
        flag_pressure = 1.0 if bool(getattr(row, "borrow_cap_reached", False)) or bool(getattr(row, "is_frozen", False)) or bool(getattr(row, "is_paused", False)) else 0.0
        candidates = [value for value in (apy_pressure, liquidity_pressure, flag_pressure) if value is not None]
        pressure = max(candidates) if candidates else None
        if pressure is not None:
            reserve_pressures.append(float(pressure))
        rows.append(
            {
                "symbol": str(getattr(row, "symbol")),
                "borrow_apy_pct": apy,
                "available_liquidity_usd": liquidity,
                "borrow_cap_reached": bool(getattr(row, "borrow_cap_reached", False)),
                "is_frozen": bool(getattr(row, "is_frozen", False)),
                "is_paused": bool(getattr(row, "is_paused", False)),
                "pressure": pressure,
            }
        )

    pressure = max(reserve_pressures) if reserve_pressures else None

    # A 24h change is descriptive only and is never used in the current stress score.
    total_latest = pd.to_numeric(focus["available_liquidity_usd"], errors="coerce").sum(min_count=1)
    target = latest_ts - pd.Timedelta(hours=24)
    prior_ts = _closest_timestamp(data["observed_at"], target, pd.Timedelta(hours=6))
    liquidity_delta_ratio_24h = None
    if prior_ts is not None:
        prior = data.loc[data["observed_at"] == prior_ts].copy()
        prior["symbol"] = prior["symbol"].astype(str).str.upper()
        prior = prior.loc[prior["symbol"].isin(FOCUS_AAVE_SYMBOLS)]
        total_prior = pd.to_numeric(prior["available_liquidity_usd"], errors="coerce").sum(min_count=1)
        if pd.notna(total_latest) and pd.notna(total_prior) and float(total_prior) > 0:
            liquidity_delta_ratio_24h = float(total_latest / total_prior - 1.0)

    return {
        "valid": pressure is not None,
        "reason": "ok" if pressure is not None else "no_numeric_market_stress_fields",
        "observed_at": latest_ts.isoformat(),
        "market_name": focus["market_name"].dropna().astype(str).iloc[0] if "market_name" in focus and focus["market_name"].notna().any() else None,
        "focus_reserve_count": int(len(focus)),
        "focus_available_liquidity_usd": float(total_latest) if pd.notna(total_latest) else None,
        "available_liquidity_delta_ratio_24h": liquidity_delta_ratio_24h,
        "pressure": float(pressure) if pressure is not None else None,
        "aggregation": "max_across_focus_reserves",
        "reserves": rows,
    }


def _stablecoin_flow_component(
    system: pd.DataFrame,
    chains: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    generated_at: pd.Timestamp,
    cfg: StateV02Config,
) -> dict[str, Any]:
    sys = _prepare_pti(system, as_of=as_of, known_at=generated_at)
    chn = _prepare_pti(chains, as_of=as_of, known_at=generated_at)
    if sys.empty or chn.empty:
        return {"valid": False, "reason": "missing_stablecoin_history"}
    latest_ts = sys["observed_at"].max()
    latest_rows = sys.loc[sys["observed_at"] == latest_ts]
    if latest_rows.empty:
        return {"valid": False, "reason": "missing_latest_stablecoin_system_row"}
    latest = latest_rows.iloc[-1]
    chain_coverage = _number(latest.get("chain_coverage_ratio"))
    residual_ratio = _number(latest.get("chain_abs_residual_ratio"))
    if (
        chain_coverage is None
        or chain_coverage < cfg.stablecoin_min_chain_coverage
        or residual_ratio is None
        or residual_ratio > cfg.stablecoin_max_chain_abs_residual_ratio
    ):
        return {
            "valid": False,
            "reason": "stablecoin_accounting_gate_failed",
            "observed_at": latest_ts.isoformat(),
            "chain_coverage_ratio": chain_coverage,
            "chain_abs_residual_ratio": residual_ratio,
        }

    target = latest_ts - pd.Timedelta(hours=cfg.stablecoin_lookback_hours)
    lag_ts = _closest_timestamp(
        sys["observed_at"], target, pd.Timedelta(hours=cfg.stablecoin_lag_tolerance_hours)
    )
    if lag_ts is None:
        return {
            "valid": False,
            "reason": "insufficient_lookback_history",
            "observed_at": latest_ts.isoformat(),
            "required_lookback_hours": cfg.stablecoin_lookback_hours,
        }
    lag = sys.loc[sys["observed_at"] == lag_ts].iloc[-1]
    latest_total = _number(latest.get("usd_market_value_usd"))
    lag_total = _number(lag.get("usd_market_value_usd"))
    if latest_total is None or lag_total is None or lag_total <= 0:
        return {"valid": False, "reason": "invalid_system_market_value"}

    latest_chain = chn.loc[chn["observed_at"] == latest_ts, ["chain", "market_value_usd"]].copy()
    lag_chain = chn.loc[chn["observed_at"] == lag_ts, ["chain", "market_value_usd"]].copy()
    if latest_chain.empty or lag_chain.empty:
        return {"valid": False, "reason": "missing_chain_rows_at_matched_timestamps"}
    latest_map = latest_chain.groupby("chain")["market_value_usd"].sum(min_count=1)
    lag_map = lag_chain.groupby("chain")["market_value_usd"].sum(min_count=1)
    aligned = pd.concat([lag_map.rename("lag"), latest_map.rename("latest")], axis=1).fillna(0.0)
    aligned["delta"] = aligned["latest"] - aligned["lag"]
    positive = float(aligned.loc[aligned["delta"] > 0, "delta"].sum())
    negative_abs = float(-aligned.loc[aligned["delta"] < 0, "delta"].sum())
    migration_proxy = float(min(positive, negative_abs))
    net_system_change = float(latest_total - lag_total)
    net_system_change_ratio = net_system_change / lag_total
    migration_ratio = migration_proxy / lag_total
    contraction_ratio = max(0.0, -net_system_change_ratio)
    contraction_pressure = _clip01(
        contraction_ratio / cfg.stablecoin_contraction_full_stress_ratio
    )
    migration_reference = _clip01(
        migration_ratio / cfg.stablecoin_migration_full_reference_ratio
    )

    top_moves = aligned.reindex(aligned["delta"].abs().sort_values(ascending=False).index).head(10)
    moves = [
        {"chain": str(index), "delta_market_value_usd": float(row.delta)}
        for index, row in top_moves.iterrows()
    ]
    return {
        "valid": contraction_pressure is not None,
        "reason": "ok",
        "observed_at": latest_ts.isoformat(),
        "lag_observed_at": lag_ts.isoformat(),
        "lookback_hours_actual": float((latest_ts - lag_ts) / pd.Timedelta(hours=1)),
        "system_market_value_usd": latest_total,
        "net_system_change_usd": net_system_change,
        "net_system_change_ratio": net_system_change_ratio,
        "issuance_or_redemption_proxy_usd": net_system_change,
        "offsetting_chain_migration_proxy_usd": migration_proxy,
        "migration_ratio": migration_ratio,
        "migration_reference_intensity": migration_reference,
        "pressure": contraction_pressure,
        "pressure_semantics": "system_contraction_only_migration_is_not_stress",
        "chain_coverage_ratio": chain_coverage,
        "chain_abs_residual_ratio": residual_ratio,
        "top_chain_moves": moves,
    }


def _basis_dispersion_component(
    market_state: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    generated_at: pd.Timestamp,
    cfg: StateV02Config,
) -> dict[str, Any]:
    data = _prepare_pti(market_state, as_of=as_of, known_at=generated_at)
    if data.empty or "asset" not in data.columns:
        return {"valid": False, "reason": "missing_hyperliquid_market_state"}
    data["asset"] = data["asset"].astype(str).str.upper()
    rows: dict[str, pd.Series] = {}
    for asset in ("BTC", "ETH"):
        part = data.loc[data["asset"] == asset].sort_values(["observed_at", "known_at"])
        if part.empty:
            return {"valid": False, "reason": f"missing_{asset.lower()}_market_state"}
        rows[asset] = part.iloc[-1]
    values = {asset: _number(row.get("basis_z_24h")) for asset, row in rows.items()}
    if any(value is None for value in values.values()):
        return {"valid": False, "reason": "missing_basis_z_24h", "basis_z_24h": values}
    latest_observed = max(_utc(row["observed_at"]) for row in rows.values())
    if not _fresh(latest_observed, generated_at, cfg.max_source_age_minutes):
        return {"valid": False, "reason": "stale_hyperliquid_market_state"}
    dispersion = abs(float(values["BTC"]) - float(values["ETH"]))
    pressure = _clip01(dispersion / cfg.basis_full_stress_z_dispersion)
    return {
        "valid": pressure is not None,
        "reason": "ok",
        "observed_at": latest_observed.isoformat(),
        "basis_z_24h": values,
        "basis_z_dispersion": dispersion,
        "pressure": pressure,
        "scope": "CROSS_ASSET_HYPERLIQUID_ONLY",
        "multi_venue_claim_allowed": False,
    }


def _contagion_component(
    canonical_chain_supply: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    generated_at: pd.Timestamp,
    cfg: StateV02Config,
) -> dict[str, Any]:
    data = _prepare_pti(canonical_chain_supply, as_of=as_of, known_at=generated_at)
    required = {"stablecoin_id", "chain", "market_value_usd", "observed_at"}
    if data.empty or not required.issubset(data.columns):
        return {"valid": False, "reason": "missing_stablecoin_chain_composition"}
    latest_ts = data["observed_at"].max()
    latest = data.loc[data["observed_at"] == latest_ts].copy()
    latest["market_value_usd"] = pd.to_numeric(latest["market_value_usd"], errors="coerce").fillna(0.0)
    stable_totals = latest.groupby("stablecoin_id")["market_value_usd"].sum()
    chain_totals = latest.groupby("chain")["market_value_usd"].sum()
    valid_stables = stable_totals[stable_totals >= cfg.contagion_min_stablecoin_market_value_usd].index
    valid_chains = chain_totals[chain_totals >= cfg.contagion_min_chain_market_value_usd].index
    latest = latest.loc[
        latest["stablecoin_id"].isin(valid_stables) & latest["chain"].isin(valid_chains)
    ]
    if latest.empty or latest["chain"].nunique() < 2 or latest["stablecoin_id"].nunique() < 1:
        return {"valid": False, "reason": "insufficient_graph_after_filters"}

    matrix = latest.pivot_table(
        index="chain", columns="stablecoin_id", values="market_value_usd", aggfunc="sum", fill_value=0.0
    ).astype(float)
    totals = matrix.sum(axis=1)
    norms = np.linalg.norm(matrix.to_numpy(dtype=float), axis=1)
    values = matrix.to_numpy(dtype=float)
    weighted_overlap = 0.0
    weight_sum = 0.0
    pair_count = 0
    top_pairs: list[dict[str, Any]] = []
    chains = list(matrix.index.astype(str))
    for i in range(len(chains)):
        for j in range(i + 1, len(chains)):
            if norms[i] <= 0 or norms[j] <= 0:
                continue
            cosine = float(np.dot(values[i], values[j]) / (norms[i] * norms[j]))
            pair_weight = float(math.sqrt(float(totals.iloc[i]) * float(totals.iloc[j])))
            weighted_overlap += cosine * pair_weight
            weight_sum += pair_weight
            pair_count += 1
            top_pairs.append({"chain_a": chains[i], "chain_b": chains[j], "cosine_overlap": cosine, "pair_weight": pair_weight})
    if weight_sum <= 0 or pair_count == 0:
        return {"valid": False, "reason": "no_valid_chain_pairs"}
    connectivity = float(weighted_overlap / weight_sum)
    top_pairs = sorted(top_pairs, key=lambda row: row["cosine_overlap"] * row["pair_weight"], reverse=True)[:10]
    return {
        "valid": True,
        "reason": "ok",
        "observed_at": latest_ts.isoformat(),
        "chain_count": int(matrix.shape[0]),
        "stablecoin_count": int(matrix.shape[1]),
        "edge_count": int((matrix.to_numpy() > 0).sum()),
        "chain_pair_count": pair_count,
        "weighted_chain_composition_cosine_overlap": connectivity,
        "pressure": _clip01(connectivity),
        "top_overlapping_chain_pairs": top_pairs,
        "interpretation": "potential_common_stablecoin_collateral_channel_not_causal_contagion_proof",
    }


def _liquidation_activity_component(
    liquidations: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    generated_at: pd.Timestamp,
) -> dict[str, Any]:
    if liquidations.empty:
        return {"valid": False, "reason": "optional_rpc_not_observed_or_no_canonical_files"}
    data = liquidations.copy()
    for column in ("observed_at", "known_at", "event_time"):
        if column in data.columns:
            data[column] = pd.to_datetime(data[column], utc=True, errors="coerce")
    if "known_at" in data:
        data = data.loc[data["known_at"] <= generated_at]
    if "event_time" in data:
        data = data.loc[data["event_time"].isna() | (data["event_time"] <= as_of)]
    if data.empty:
        return {"valid": True, "reason": "observed_no_events_by_as_of", "events_24h": 0, "events_7d": 0}
    if {"transaction_hash", "log_index"}.issubset(data.columns):
        data = data.sort_values("known_at").drop_duplicates(["transaction_hash", "log_index"], keep="last")
    times = data["event_time"].dropna() if "event_time" in data.columns else pd.Series(dtype="datetime64[ns, UTC]")
    events_24h = int((times >= as_of - pd.Timedelta(hours=24)).sum()) if not times.empty else 0
    events_7d = int((times >= as_of - pd.Timedelta(days=7)).sum()) if not times.empty else 0
    return {
        "valid": True,
        "reason": "ok",
        "events_total_observed": int(len(data)),
        "events_24h": events_24h,
        "events_7d": events_7d,
        "role": "O0_EVENT_CONFIRMATION_ONLY",
        "pressure": None,
    }


def _borrower_health_component() -> dict[str, Any]:
    return {
        "valid": False,
        "reason": "auditable_borrower_universe_not_yet_available",
        "status": "REQUIRES_AUDITABLE_BORROWER_UNIVERSE",
        "liquidation_threshold_health_factor": 1.0,
        "market_level_substitution_allowed": False,
        "pressure": None,
    }


def _deployment_activation_descriptor(
    aave: dict[str, Any], stable: dict[str, Any]
) -> dict[str, Any]:
    issuance = _number(stable.get("net_system_change_ratio")) if stable.get("valid") else None
    aave_delta = _number(aave.get("available_liquidity_delta_ratio_24h")) if aave.get("valid") else None
    valid = issuance is not None and aave_delta is not None
    return {
        "valid": valid,
        "external_liquidity_expanding": bool(issuance > 0) if issuance is not None else None,
        "aave_available_liquidity_falling": bool(aave_delta < 0) if aave_delta is not None else None,
        "coincident_activation_proxy": bool(issuance > 0 and aave_delta < 0) if valid else None,
        "stablecoin_net_change_ratio": issuance,
        "aave_available_liquidity_delta_ratio_24h": aave_delta,
        "actionability": "DESCRIPTION_ONLY_NO_CAUSAL_CLAIM",
    }


def compute_state_v02(
    aave_markets: pd.DataFrame,
    stablecoin_system: pd.DataFrame,
    stablecoin_chain_state: pd.DataFrame,
    hyperliquid_market_state: pd.DataFrame,
    stablecoin_chain_composition: pd.DataFrame,
    aave_liquidations: pd.DataFrame,
    *,
    as_of: Any,
    generated_at: Any | None = None,
    config: StateV02Config | None = None,
) -> dict[str, Any]:
    cfg = config or StateV02Config()
    as_of_ts = _utc(as_of)
    generated_ts = _utc(generated_at or datetime.now(timezone.utc))
    if generated_ts < as_of_ts:
        raise ValueError("generated_at cannot precede as_of")

    aave = _aave_market_component(aave_markets, as_of=as_of_ts, generated_at=generated_ts, cfg=cfg)
    stable = _stablecoin_flow_component(
        stablecoin_system, stablecoin_chain_state, as_of=as_of_ts, generated_at=generated_ts, cfg=cfg
    )
    basis = _basis_dispersion_component(
        hyperliquid_market_state, as_of=as_of_ts, generated_at=generated_ts, cfg=cfg
    )
    contagion = _contagion_component(
        stablecoin_chain_composition, as_of=as_of_ts, generated_at=generated_ts, cfg=cfg
    )
    liquidations = _liquidation_activity_component(
        aave_liquidations, as_of=as_of_ts, generated_at=generated_ts
    )
    borrower_health = _borrower_health_component()
    deployment = _deployment_activation_descriptor(aave, stable)

    components = {
        "aave_market_stress": aave,
        "stablecoin_flow_decomposition": stable,
        "basis_dispersion": basis,
        "contagion_graph": contagion,
    }
    weights = {
        "aave_market_stress": cfg.aave_weight,
        "stablecoin_flow_decomposition": cfg.stablecoin_weight,
        "basis_dispersion": cfg.basis_weight,
        "contagion_graph": cfg.contagion_weight,
    }
    valid_weight = 0.0
    weighted_pressure = 0.0
    valid_names: list[str] = []
    for name, component in components.items():
        pressure = _number(component.get("pressure")) if component.get("valid") else None
        if pressure is None:
            continue
        weight = weights[name]
        valid_weight += weight
        weighted_pressure += weight * pressure
        valid_names.append(name)
    composite = weighted_pressure / valid_weight if valid_weight > 0 else None
    valid_count = len(valid_names)
    if valid_count >= cfg.full_confidence_components:
        confidence = "FULL"
    elif valid_count >= cfg.minimum_valid_components:
        confidence = "PARTIAL"
    else:
        confidence = "INSUFFICIENT"

    return {
        "protocol": PROTOCOL,
        "mode": MODE,
        "actionability": ACTIONABILITY,
        "risk_multiplier": None,
        "mutates_frozen_core": False,
        "mutates_state_v01": False,
        "mutates_state_ab_v01": False,
        "as_of": as_of_ts.isoformat(),
        "generated_at": generated_ts.isoformat(),
        "components": components,
        "aave_liquidation_activity": liquidations,
        "borrower_health_factor_distribution": borrower_health,
        "deployment_activation": deployment,
        "valid_pressure_components": valid_names,
        "valid_pressure_component_count": valid_count,
        "expected_pressure_component_count": 4,
        "data_confidence": confidence,
        "descriptive_stress_score": float(composite) if composite is not None else None,
        "interpretation": (
            "O1 descriptive state only. This score is not a trading signal and cannot alter "
            "Frozen B3, State V0.1, or the prospective A/B V0.1 ledger."
        ),
    }


def _load_parquet_files(files: list[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in files:
        try:
            frames.append(pd.read_parquet(path, columns=columns))
        except (KeyError, ValueError):
            frames.append(pd.read_parquet(path))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _recent_day_files(root: Path, days: int) -> list[Path]:
    if not root.exists():
        return []
    files = sorted(root.glob("year=*/month=*/day=*/*.parquet"))
    if not files:
        return []
    day_dirs = sorted({path.parent for path in files})[-max(days, 1):]
    allowed = set(day_dirs)
    return [path for path in files if path.parent in allowed]


def _latest_snapshot_file(root: Path) -> list[Path]:
    files = sorted(root.glob("year=*/month=*/day=*/*.parquet")) if root.exists() else []
    return [files[-1]] if files else []


def _output_path(data_root: Path, generated_at: pd.Timestamp) -> Path:
    return (
        data_root
        / "derived"
        / "state"
        / "v02"
        / f"year={generated_at:%Y}"
        / f"month={generated_at:%m}"
        / f"day={generated_at:%d}"
        / f"state_at={generated_at:%H%M%S%f}.parquet"
    )


def _flatten(snapshot: dict[str, Any]) -> dict[str, Any]:
    components = snapshot["components"]
    row = {
        "protocol": snapshot["protocol"],
        "mode": snapshot["mode"],
        "actionability": snapshot["actionability"],
        "risk_multiplier": snapshot["risk_multiplier"],
        "mutates_frozen_core": snapshot["mutates_frozen_core"],
        "mutates_state_v01": snapshot["mutates_state_v01"],
        "mutates_state_ab_v01": snapshot["mutates_state_ab_v01"],
        "as_of": snapshot["as_of"],
        "generated_at": snapshot["generated_at"],
        "data_confidence": snapshot["data_confidence"],
        "descriptive_stress_score": snapshot["descriptive_stress_score"],
        "valid_pressure_component_count": snapshot["valid_pressure_component_count"],
        "aave_valid": bool(components["aave_market_stress"].get("valid")),
        "aave_pressure": components["aave_market_stress"].get("pressure"),
        "stablecoin_flow_valid": bool(components["stablecoin_flow_decomposition"].get("valid")),
        "stablecoin_flow_pressure": components["stablecoin_flow_decomposition"].get("pressure"),
        "stablecoin_net_change_ratio": components["stablecoin_flow_decomposition"].get("net_system_change_ratio"),
        "stablecoin_migration_ratio": components["stablecoin_flow_decomposition"].get("migration_ratio"),
        "basis_dispersion_valid": bool(components["basis_dispersion"].get("valid")),
        "basis_dispersion_pressure": components["basis_dispersion"].get("pressure"),
        "basis_z_dispersion": components["basis_dispersion"].get("basis_z_dispersion"),
        "contagion_valid": bool(components["contagion_graph"].get("valid")),
        "contagion_pressure": components["contagion_graph"].get("pressure"),
        "contagion_connectivity": components["contagion_graph"].get("weighted_chain_composition_cosine_overlap"),
        "liquidation_events_24h": snapshot["aave_liquidation_activity"].get("events_24h"),
        "liquidation_events_7d": snapshot["aave_liquidation_activity"].get("events_7d"),
        "borrower_health_factor_distribution_valid": snapshot["borrower_health_factor_distribution"].get("valid"),
        "deployment_activation_proxy": snapshot["deployment_activation"].get("coincident_activation_proxy"),
        "details_json": json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str),
    }
    return row


def build_latest_state_v02(
    data_root: Path,
    *,
    generated_at: Any | None = None,
    write: bool = True,
    config: StateV02Config | None = None,
) -> dict[str, Any]:
    generated_ts = _utc(generated_at or datetime.now(timezone.utc))
    aave = _load_parquet_files(_recent_day_files(data_root / "canonical" / "aave" / "markets", 2))
    stable_system = _load_parquet_files(_recent_day_files(data_root / "derived" / "stablecoins" / "system_state", 10))
    stable_chain = _load_parquet_files(_recent_day_files(data_root / "derived" / "stablecoins" / "chain_state", 10))
    hl = _load_parquet_files(_recent_day_files(data_root / "derived" / "hyperliquid" / "market_state", 2))
    chain_composition = _load_parquet_files(_latest_snapshot_file(data_root / "canonical" / "defillama" / "stablecoin_chain_supply"))
    liquidations = _load_parquet_files(_recent_day_files(data_root / "canonical" / "aave" / "liquidations", 8))

    observed_candidates: list[pd.Timestamp] = []
    for frame in (aave, stable_system, hl, chain_composition):
        if not frame.empty and "observed_at" in frame.columns:
            values = pd.to_datetime(frame["observed_at"], utc=True, errors="coerce").dropna()
            if not values.empty:
                observed_candidates.append(values.max())
    if not observed_candidates:
        return {
            "protocol": PROTOCOL,
            "mode": MODE,
            "status": "no_inputs",
            "actionability": ACTIONABILITY,
            "risk_multiplier": None,
            "written": False,
        }
    as_of = max(observed_candidates)
    snapshot = compute_state_v02(
        aave,
        stable_system,
        stable_chain,
        hl,
        chain_composition,
        liquidations,
        as_of=as_of,
        generated_at=generated_ts,
        config=config,
    )
    if not write:
        return {**snapshot, "status": "computed", "written": False}
    path = _output_path(data_root, generated_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pd.DataFrame([_flatten(snapshot)]).to_parquet(tmp, index=False)
    tmp.replace(path)
    return {**snapshot, "status": "written", "written": True, "output": str(path)}


def load_yaml_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def config_consistency_report(path: Path) -> dict[str, Any]:
    raw = load_yaml_config(path)
    cfg = StateV02Config()
    checks = {
        "protocol": raw.get("protocol") == PROTOCOL,
        "mode": raw.get("mode") == MODE,
        "actionability": raw.get("research_policy", {}).get("actionability") == ACTIONABILITY,
        "aave_max_age": raw.get("components", {}).get("aave_market_stress", {}).get("max_source_age_minutes") == cfg.max_source_age_minutes,
        "aave_min_reserves": raw.get("components", {}).get("aave_market_stress", {}).get("minimum_reserves") == cfg.aave_minimum_reserves,
        "aave_apy_threshold": raw.get("components", {}).get("aave_market_stress", {}).get("borrow_apy_full_stress_pct") == cfg.aave_borrow_apy_full_stress_pct,
        "aave_liquidity_threshold": raw.get("components", {}).get("aave_market_stress", {}).get("low_available_liquidity_full_stress_usd") == cfg.aave_low_available_liquidity_full_stress_usd,
        "stable_lookback": raw.get("components", {}).get("stablecoin_flow_decomposition", {}).get("lookback_hours") == cfg.stablecoin_lookback_hours,
        "stable_tolerance": raw.get("components", {}).get("stablecoin_flow_decomposition", {}).get("lag_tolerance_hours") == cfg.stablecoin_lag_tolerance_hours,
        "stable_chain_coverage": raw.get("components", {}).get("stablecoin_flow_decomposition", {}).get("minimum_chain_coverage") == cfg.stablecoin_min_chain_coverage,
        "stable_residual": raw.get("components", {}).get("stablecoin_flow_decomposition", {}).get("maximum_chain_abs_residual_ratio") == cfg.stablecoin_max_chain_abs_residual_ratio,
        "basis_threshold": raw.get("components", {}).get("basis_dispersion", {}).get("full_stress_z_dispersion") == cfg.basis_full_stress_z_dispersion,
        "weights": raw.get("aggregation", {}).get("component_weights") == {
            "aave_market_stress": cfg.aave_weight,
            "stablecoin_flow_stress": cfg.stablecoin_weight,
            "basis_dispersion_stress": cfg.basis_weight,
            "contagion_connectivity_stress": cfg.contagion_weight,
        },
    }
    return {"protocol": PROTOCOL, "ok": all(checks.values()), "checks": checks}
