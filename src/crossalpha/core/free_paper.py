from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from crossalpha.core import frozen_b3_v01
from crossalpha.core.free_baselines import _load_daily_panel, _safe_slug
from crossalpha.core.free_dataset import audit_free_core, build_free_core_returns
from crossalpha.core.free_provider import FreeCoreProvider, FreeCoreRange
from crossalpha.settings import Settings


PAPER_PROTOCOL = "CROSSALPHA_FREE_V0_1_PAPER"
PAPER_SCHEMA_VERSION = 1
HISTORICAL_START = "2010-06-01"
HISTORICAL_END = "2026-09-01"
FROZEN_STRATEGY = frozen_b3_v01.STRATEGY


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value, tz="UTC").date()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _payload_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("record_sha256", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _seal(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["record_sha256"] = _payload_hash(payload)
    return payload


def _verify_sealed(value: dict[str, Any]) -> bool:
    expected = value.get("record_sha256")
    return isinstance(expected, str) and expected == _payload_hash(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paper_root(data_root: Path) -> Path:
    return data_root / "research" / "free_v01" / "paper"


def _freeze_path(data_root: Path) -> Path:
    return _paper_root(data_root) / "freeze.json"


def _returns_path(data_root: Path, *, start: str, end: str) -> Path:
    return (
        data_root
        / "derived"
        / "core"
        / "free_v01"
        / f"start={_safe_slug(start)}"
        / f"end={_safe_slug(end)}"
        / "asset_returns.parquet"
    )


def _final_decision_path(data_root: Path, *, start: str, end: str) -> Path:
    return (
        data_root
        / "research"
        / "free_v01"
        / "final_evaluation"
        / f"start={_safe_slug(start)}"
        / f"end={_safe_slug(end)}"
        / "final_decision.json"
    )


def _snapshot_path(data_root: Path, effective_date: date) -> Path:
    return (
        _paper_root(data_root)
        / "snapshots"
        / f"year={effective_date.year:04d}"
        / f"month={effective_date.month:02d}"
        / f"effective_date={effective_date.isoformat()}.json"
    )


def _mark_path(data_root: Path, mark_date: date) -> Path:
    return (
        _paper_root(data_root)
        / "marks"
        / f"year={mark_date.year:04d}"
        / f"month={mark_date.month:02d}"
        / f"date={mark_date.isoformat()}.json"
    )


def _refresh_manifest_path(data_root: Path, end: str) -> Path:
    return _paper_root(data_root) / "refreshes" / f"end={_safe_slug(end)}" / "manifest.json"


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not _verify_sealed(existing):
            raise ValueError(f"immutable paper record failed integrity check: {path}")
        return existing
    sealed = _seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if path.exists():
        tmp.unlink(missing_ok=True)
        return json.loads(path.read_text(encoding="utf-8"))
    tmp.replace(path)
    return sealed


def _read_required_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _next_monday_strict(after: date) -> date:
    days = (7 - after.weekday()) % 7
    if days == 0:
        days = 7
    return after + timedelta(days=days)


def _frozen_implementation_path() -> Path:
    module_file = getattr(frozen_b3_v01, "__file__", None)
    if not module_file:
        raise RuntimeError("cannot locate frozen B3 implementation")
    return Path(module_file)


def _verify_final_decision(final: dict[str, Any]) -> None:
    if final.get("protocol") != frozen_b3_v01.PROTOCOL:
        raise ValueError("final decision protocol does not match frozen B3 V0.1")
    if final.get("core_candidate") != FROZEN_STRATEGY:
        raise ValueError("final decision does not nominate frozen B3")
    if final.get("candidate_config_frozen") is not True:
        raise ValueError("final decision is not frozen")
    if final.get("parameter_optimization_allowed") is not False:
        raise ValueError("paper protocol refuses a tunable candidate")
    decision = final.get("decisions", {}).get(FROZEN_STRATEGY, {})
    if decision.get("state") != "PROMISING_BUT_UNPROVEN":
        raise ValueError("B3 is not in PROMISING_BUT_UNPROVEN state")

    expected = frozen_b3_v01.frozen_parameters()
    observed = final.get("candidate_parameters", {})
    pairs = {
        "trend_window_calendar_days": expected["trend_window_calendar_days"],
        "vol_window_calendar_days": expected["vol_window_calendar_days"],
        "target_vol": expected["target_vol"],
        "rebalance_weekday": expected["rebalance_weekday"],
        "execution_lag_calendar_days": expected["execution_lag_calendar_days"],
        "one_way_cost_bps": expected["one_way_cost_bps"],
        "shorting": expected["shorting"],
        "leverage_cap": expected["leverage_cap"],
    }
    for key, expected_value in pairs.items():
        if observed.get(key) != expected_value:
            raise ValueError(
                f"frozen paper parameter mismatch for {key}: "
                f"final={observed.get(key)!r} frozen={expected_value!r}"
            )


def freeze_paper_protocol(
    data_root: Path,
    *,
    historical_start: str = HISTORICAL_START,
    historical_end: str = HISTORICAL_END,
    now: datetime | None = None,
) -> dict[str, Any]:
    freeze_path = _freeze_path(data_root)
    if freeze_path.exists():
        existing = _read_required_json(freeze_path)
        if not _verify_sealed(existing):
            raise ValueError("existing paper freeze failed integrity check")
        return {**existing, "status": "already_frozen"}

    final_path = _final_decision_path(
        data_root, start=historical_start, end=historical_end
    )
    final = _read_required_json(final_path)
    _verify_final_decision(final)

    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    frozen_impl = _frozen_implementation_path()
    first_eligible = _next_monday_strict(current.date())

    payload = {
        "schema_version": PAPER_SCHEMA_VERSION,
        "paper_protocol": PAPER_PROTOCOL,
        "research_protocol": frozen_b3_v01.PROTOCOL,
        "strategy": FROZEN_STRATEGY,
        "state_at_freeze": "PROMISING_BUT_UNPROVEN",
        "frozen_at": current.isoformat(),
        "historical_start": historical_start,
        "historical_end_exclusive": historical_end,
        "first_eligible_effective_date": first_eligible.isoformat(),
        "parameter_optimization_allowed": False,
        "frozen_parameters": frozen_b3_v01.frozen_parameters(),
        "frozen_implementation_sha256": _sha256_file(frozen_impl),
        "frozen_implementation_file": frozen_impl.name,
        "final_decision_sha256": _sha256_file(final_path),
        "final_decision_path": str(final_path),
        "execution_model": "research_daily_close_to_close_shadow",
        "prospective_promotion_gate": {
            "minimum_calendar_days": 730,
            "minimum_weekly_snapshots": 90,
            "minimum_sharpe_excess_cash": 0.25,
            "cumulative_return_must_exceed_cash": True,
            "ledger_integrity_required": True,
            "protocol_mutation_allowed": False,
            "note": (
                "SUPPORTED is reserved for genuinely prospective evidence. "
                "No historical backfill can satisfy this gate."
            ),
        },
    }
    written = _write_immutable_json(freeze_path, payload)
    return {**written, "status": "frozen"}


def _load_freeze(data_root: Path) -> dict[str, Any]:
    freeze = _read_required_json(_freeze_path(data_root))
    if not _verify_sealed(freeze):
        raise ValueError("paper freeze failed integrity check")
    current_hash = _sha256_file(_frozen_implementation_path())
    if current_hash != freeze.get("frozen_implementation_sha256"):
        raise ValueError(
            "frozen B3 implementation hash changed; V0.1 paper ledger is locked. "
            "Create a new protocol version instead of mutating V0.1."
        )
    return freeze


def refresh_paper_core(
    settings: Settings,
    *,
    start: str = HISTORICAL_START,
    end: str,
) -> dict[str, Any]:
    settings.ensure_dirs()
    value = FreeCoreRange(start=start, end=end)
    returns_path = _returns_path(settings.crossalpha_data_dir, start=start, end=end)

    if returns_path.exists():
        quality = audit_free_core(settings.crossalpha_data_dir, value, write_report=False)
        if not quality.get("ok"):
            raise ValueError("cached paper Core range exists but quality gate is not clean")
        manifest = {
            "schema_version": PAPER_SCHEMA_VERSION,
            "paper_protocol": PAPER_PROTOCOL,
            "generated_at": _utc_now().isoformat(),
            "start": start,
            "end_exclusive": end,
            "status": "cached",
            "data_cost_usd": 0,
            "returns_path": str(returns_path),
            "returns_sha256": _sha256_file(returns_path),
        }
        _write_immutable_json(_refresh_manifest_path(settings.crossalpha_data_dir, end), manifest)
        return manifest

    quality = audit_free_core(settings.crossalpha_data_dir, value, write_report=False)
    fetch_report: dict[str, Any] | None = None
    if not quality.get("ok"):
        if not settings.tiingo_api_token:
            raise ValueError("TIINGO_API_TOKEN is required for free paper refresh")
        if not settings.fred_api_key:
            raise ValueError("FRED_API_KEY is required for free paper refresh")
        provider = FreeCoreProvider(
            tiingo_token=settings.tiingo_api_token,
            fred_api_key=settings.fred_api_key,
            timeout=settings.crossalpha_http_timeout,
        )
        fetch_report = provider.fetch_all(value, settings.crossalpha_data_dir)

    derived = build_free_core_returns(settings.crossalpha_data_dir, value)
    manifest = {
        "schema_version": PAPER_SCHEMA_VERSION,
        "paper_protocol": PAPER_PROTOCOL,
        "generated_at": _utc_now().isoformat(),
        "start": start,
        "end_exclusive": end,
        "status": "fetched" if fetch_report is not None else "built_from_cached_canonical",
        "data_cost_usd": 0,
        "returns_path": derived["output"],
        "returns_sha256": _sha256_file(Path(derived["output"])),
        "coverage": derived["coverage"],
    }
    _write_immutable_json(_refresh_manifest_path(settings.crossalpha_data_dir, end), manifest)
    return manifest


def _asset_last_return_dates(path: Path) -> dict[str, str | None]:
    frame = pd.read_parquet(path)
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame["return"] = pd.to_numeric(frame["return"], errors="coerce")
    result: dict[str, str | None] = {}
    for asset, part in frame.groupby("economic_asset", sort=True):
        known = part.loc[part["return"].notna(), "date"]
        result[str(asset)] = known.max().isoformat() if not known.empty else None
    return result


def create_paper_snapshot(
    data_root: Path,
    *,
    effective_date: str | date,
    research_start: str = HISTORICAL_START,
    now: datetime | None = None,
    strict_live: bool = True,
) -> dict[str, Any]:
    freeze = _load_freeze(data_root)
    effective = _parse_date(effective_date)
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)

    if strict_live and effective != current.date():
        raise ValueError(
            "prospective snapshots cannot be backfilled or future-dated; "
            "effective_date must equal the current UTC date"
        )
    if effective.weekday() != frozen_b3_v01.REBALANCE_WEEKDAY:
        raise ValueError("frozen B3 paper snapshots are allowed only on Monday UTC")
    first_eligible = _parse_date(freeze["first_eligible_effective_date"])
    if effective < first_eligible:
        raise ValueError(
            f"effective_date {effective} predates first prospective eligible date {first_eligible}"
        )

    path = _snapshot_path(data_root, effective)
    if path.exists():
        existing = _read_required_json(path)
        if not _verify_sealed(existing):
            raise ValueError("existing paper snapshot failed integrity check")
        return {**existing, "status": "already_exists"}

    end = effective.isoformat()
    returns_path = _returns_path(data_root, start=research_start, end=end)
    if not returns_path.exists():
        raise FileNotFoundError(
            f"paper snapshot requires exact point-in-time Core range ending {end}: {returns_path}"
        )
    daily, available = _load_daily_panel(data_root, research_start, end)
    signal = pd.Timestamp(effective - timedelta(days=frozen_b3_v01.EXECUTION_LAG_DAYS), tz="UTC")
    if daily.index.max() != signal:
        raise ValueError(
            f"paper snapshot input must end exactly on signal date {signal}; got {daily.index.max()}"
        )
    weights = frozen_b3_v01.compute_target(daily, available, signal_date=signal)
    if abs(float(weights.sum()) - 1.0) > 1e-10:
        raise ValueError("frozen B3 snapshot weights do not sum to one")
    if (weights < -1e-12).any():
        raise ValueError("frozen B3 snapshot contains a negative weight")

    payload = {
        "schema_version": PAPER_SCHEMA_VERSION,
        "paper_protocol": PAPER_PROTOCOL,
        "research_protocol": frozen_b3_v01.PROTOCOL,
        "strategy": FROZEN_STRATEGY,
        "known_at": current.isoformat(),
        "effective_date": effective.isoformat(),
        "signal_date": signal.date().isoformat(),
        "input_end_exclusive": end,
        "input_returns_path": str(returns_path),
        "input_returns_sha256": _sha256_file(returns_path),
        "asset_last_return_dates": _asset_last_return_dates(returns_path),
        "freeze_record_sha256": freeze["record_sha256"],
        "frozen_implementation_sha256": freeze["frozen_implementation_sha256"],
        "weights": {asset: float(weights[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
        "risk_gross": float(weights.loc[list(frozen_b3_v01.RISK_ASSETS)].sum()),
        "cash_weight": float(weights["CASH"]),
        "execution_model": freeze["execution_model"],
    }
    written = _write_immutable_json(path, payload)
    return {**written, "status": "created"}


def _load_snapshots(data_root: Path) -> list[dict[str, Any]]:
    root = _paper_root(data_root) / "snapshots"
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/month=*/effective_date=*.json")):
        record = _read_required_json(path)
        if not _verify_sealed(record):
            raise ValueError(f"paper snapshot integrity failure: {path}")
        record = {**record, "_path": str(path)}
        result.append(record)
    return sorted(result, key=lambda row: row["effective_date"])


def _load_marks(data_root: Path) -> list[dict[str, Any]]:
    root = _paper_root(data_root) / "marks"
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/month=*/date=*.json")):
        record = _read_required_json(path)
        if not _verify_sealed(record):
            raise ValueError(f"paper mark integrity failure: {path}")
        record = {**record, "_path": str(path)}
        result.append(record)
    return sorted(result, key=lambda row: row["date"])


def _weights_from_snapshot(snapshot: dict[str, Any]) -> pd.Series:
    return pd.Series(
        {asset: float(snapshot["weights"].get(asset, 0.0)) for asset in frozen_b3_v01.ALL_ASSETS},
        dtype=float,
    )


def mark_paper_forward(
    data_root: Path,
    *,
    end: str | date,
    research_start: str = HISTORICAL_START,
    now: datetime | None = None,
) -> dict[str, Any]:
    freeze = _load_freeze(data_root)
    end_date = _parse_date(end)
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if end_date > current.date():
        raise ValueError("paper marks cannot use a future end date")

    returns_path = _returns_path(data_root, start=research_start, end=end_date.isoformat())
    if not returns_path.exists():
        raise FileNotFoundError(
            f"paper mark requires Core returns ending {end_date.isoformat()}: {returns_path}"
        )
    daily, _ = _load_daily_panel(data_root, research_start, end_date.isoformat())
    snapshots = _load_snapshots(data_root)
    if not snapshots:
        return {
            "paper_protocol": PAPER_PROTOCOL,
            "status": "no_snapshots",
            "created_marks": 0,
            "skipped_existing_marks": 0,
            "end_exclusive": end_date.isoformat(),
        }

    snapshot_by_date = {_parse_date(row["effective_date"]): row for row in snapshots}
    existing = _load_marks(data_root)
    existing_dates = {_parse_date(row["date"]) for row in existing}
    previous_equity = float(existing[-1]["equity_after"]) if existing else 1.0
    previous_cash_equity = float(existing[-1]["cash_equity_after"]) if existing else 1.0

    first_effective = min(snapshot_by_date)
    created = 0
    skipped = 0
    current_weights = pd.Series(0.0, index=frozen_b3_v01.ALL_ASSETS, dtype=float)
    current_weights["CASH"] = 1.0
    active_snapshot: dict[str, Any] | None = None

    # Reconstruct the active weight state before the first unmarked day.
    if existing:
        last_existing = max(existing_dates)
        for snap_date in sorted(snapshot_by_date):
            if snap_date <= last_existing:
                active_snapshot = snapshot_by_date[snap_date]
                current_weights = _weights_from_snapshot(active_snapshot)

    for timestamp in daily.index:
        mark_date = timestamp.date()
        if mark_date < first_effective or mark_date >= end_date:
            continue
        if mark_date in existing_dates:
            skipped += 1
            continue

        turnover = 0.0
        if mark_date in snapshot_by_date:
            new_snapshot = snapshot_by_date[mark_date]
            new_weights = _weights_from_snapshot(new_snapshot)
            turnover = float((new_weights - current_weights).abs().sum() * 0.5)
            current_weights = new_weights
            active_snapshot = new_snapshot
        elif active_snapshot is None:
            eligible = [
                row for row in snapshots if _parse_date(row["effective_date"]) <= mark_date
            ]
            if not eligible:
                continue
            active_snapshot = eligible[-1]
            current_weights = _weights_from_snapshot(active_snapshot)

        row = daily.loc[timestamp, list(frozen_b3_v01.ALL_ASSETS)].fillna(0.0)
        gross_return = float(
            current_weights.to_numpy(dtype=float) @ row.to_numpy(dtype=float)
        )
        cost = turnover * (frozen_b3_v01.ONE_WAY_COST_BPS / 10_000.0)
        net_return = gross_return - cost
        cash_return = float(row["CASH"])
        previous_equity *= 1.0 + net_return
        previous_cash_equity *= 1.0 + cash_return

        payload = {
            "schema_version": PAPER_SCHEMA_VERSION,
            "paper_protocol": PAPER_PROTOCOL,
            "strategy": FROZEN_STRATEGY,
            "known_at": current.isoformat(),
            "date": mark_date.isoformat(),
            "active_snapshot_effective_date": active_snapshot["effective_date"],
            "active_snapshot_record_sha256": active_snapshot["record_sha256"],
            "weights": {asset: float(current_weights[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
            "asset_returns": {asset: float(row[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
            "turnover": turnover,
            "cost": cost,
            "gross_return": gross_return,
            "net_return": net_return,
            "cash_return": cash_return,
            "equity_after": previous_equity,
            "cash_equity_after": previous_cash_equity,
            "input_end_exclusive": end_date.isoformat(),
            "input_returns_sha256": _sha256_file(returns_path),
            "freeze_record_sha256": freeze["record_sha256"],
        }
        _write_immutable_json(_mark_path(data_root, mark_date), payload)
        created += 1

    return {
        "paper_protocol": PAPER_PROTOCOL,
        "status": "marked",
        "created_marks": created,
        "skipped_existing_marks": skipped,
        "end_exclusive": end_date.isoformat(),
        "latest_equity": previous_equity,
        "latest_cash_equity": previous_cash_equity,
    }


def paper_status(data_root: Path) -> dict[str, Any]:
    freeze_path = _freeze_path(data_root)
    if not freeze_path.exists():
        return {
            "paper_protocol": PAPER_PROTOCOL,
            "state": "NOT_FROZEN",
            "frozen": False,
        }
    freeze = _load_freeze(data_root)
    snapshots = _load_snapshots(data_root)
    marks = _load_marks(data_root)
    integrity_ok = all(_verify_sealed(row) for row in [freeze, *snapshots, *marks])

    if marks:
        returns = pd.Series([float(row["net_return"]) for row in marks], dtype=float)
        cash = pd.Series([float(row["cash_return"]) for row in marks], dtype=float)
        excess = returns - cash
        std = float(excess.std(ddof=1)) if len(excess) > 1 else float("nan")
        sharpe = (
            float(excess.mean() / std * math.sqrt(365.0))
            if math.isfinite(std) and std > 0
            else None
        )
        cumulative = float(np.prod(1.0 + returns.to_numpy()) - 1.0)
        cash_cumulative = float(np.prod(1.0 + cash.to_numpy()) - 1.0)
        equity = pd.Series([float(row["equity_after"]) for row in marks], dtype=float)
        drawdown = equity / equity.cummax() - 1.0
        max_drawdown = float(drawdown.min())
        first_date = _parse_date(marks[0]["date"])
        last_date = _parse_date(marks[-1]["date"])
        calendar_days = (last_date - first_date).days + 1
    else:
        sharpe = None
        cumulative = 0.0
        cash_cumulative = 0.0
        max_drawdown = 0.0
        calendar_days = 0

    gate = freeze["prospective_promotion_gate"]
    gate_checks = {
        "minimum_calendar_days": calendar_days >= int(gate["minimum_calendar_days"]),
        "minimum_weekly_snapshots": len(snapshots) >= int(gate["minimum_weekly_snapshots"]),
        "minimum_sharpe_excess_cash": sharpe is not None
        and sharpe >= float(gate["minimum_sharpe_excess_cash"]),
        "cumulative_return_exceeds_cash": cumulative > cash_cumulative,
        "ledger_integrity": integrity_ok,
        "implementation_hash_unchanged": (
            _sha256_file(_frozen_implementation_path())
            == freeze["frozen_implementation_sha256"]
        ),
    }
    supported = bool(marks) and all(gate_checks.values())
    if supported:
        state = "SUPPORTED"
    elif snapshots:
        state = "FORWARD_OBSERVATION"
    else:
        state = "FROZEN_AWAITING_FIRST_LIVE_SNAPSHOT"

    return {
        "paper_protocol": PAPER_PROTOCOL,
        "research_state_at_freeze": freeze["state_at_freeze"],
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "first_eligible_effective_date": freeze["first_eligible_effective_date"],
        "snapshot_count": len(snapshots),
        "mark_count": len(marks),
        "latest_snapshot_effective_date": snapshots[-1]["effective_date"] if snapshots else None,
        "latest_mark_date": marks[-1]["date"] if marks else None,
        "prospective_calendar_days": calendar_days,
        "cumulative_net_return": cumulative,
        "cumulative_cash_return": cash_cumulative,
        "sharpe_excess_cash": sharpe,
        "max_drawdown": max_drawdown,
        "integrity_ok": integrity_ok,
        "promotion_gate": gate,
        "promotion_gate_checks": gate_checks,
        "parameter_optimization_allowed": False,
    }
