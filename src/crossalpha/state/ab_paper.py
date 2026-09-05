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
from crossalpha.core.free_paper import (
    PAPER_PROTOCOL,
    _load_freeze as _load_core_freeze,
    _load_marks as _load_core_marks,
    _load_snapshots as _load_core_snapshots,
    _mark_path as _core_mark_path,
    _parse_date,
    _read_required_json,
    _snapshot_path as _core_snapshot_path,
    _verify_sealed as _verify_core_sealed,
)
from crossalpha.state import shadow


AB_PROTOCOL = "CROSSALPHA_STATE_AB_V0_1"
AB_SCHEMA_VERSION = 1
STATE_PROTOCOL = shadow.PROTOCOL
MODE = "PROSPECTIVE_SHADOW_AB"
ALLOWED_MULTIPLIERS = (1.0, 0.75, 0.50)
MIN_COMPARABLE_DAYS = 365
MIN_INTERVENTION_DAYS = 20
MAX_CUMULATIVE_RETURN_SACRIFICE = 0.05


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not _verify_sealed(existing):
            raise ValueError(f"immutable State A/B record failed integrity check: {path}")
        return existing
    sealed = _seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if path.exists():
        tmp.unlink(missing_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not _verify_sealed(existing):
            raise ValueError(f"immutable State A/B record failed integrity check: {path}")
        return existing
    tmp.replace(path)
    return sealed


def _ab_root(data_root: Path) -> Path:
    return data_root / "research" / "free_v01" / "state_ab_v01"


def _freeze_path(data_root: Path) -> Path:
    return _ab_root(data_root) / "freeze.json"


def _decision_path(data_root: Path, effective: date) -> Path:
    return (
        _ab_root(data_root)
        / "decisions"
        / f"year={effective.year:04d}"
        / f"month={effective.month:02d}"
        / f"effective_date={effective.isoformat()}.json"
    )


def _snapshot_path(data_root: Path, effective: date) -> Path:
    return (
        _ab_root(data_root)
        / "snapshots"
        / f"year={effective.year:04d}"
        / f"month={effective.month:02d}"
        / f"effective_date={effective.isoformat()}.json"
    )


def _mark_path(data_root: Path, mark_date: date) -> Path:
    return (
        _ab_root(data_root)
        / "marks"
        / f"year={mark_date.year:04d}"
        / f"month={mark_date.month:02d}"
        / f"date={mark_date.isoformat()}.json"
    )


def _module_path() -> Path:
    return Path(__file__)


def _state_impl_path() -> Path:
    module_file = getattr(shadow, "__file__", None)
    if not module_file:
        raise RuntimeError("cannot locate State Shadow implementation")
    return Path(module_file)


def _repo_root() -> Path:
    return _module_path().resolve().parents[3]


def _state_config_path() -> Path:
    return _repo_root() / "config" / "state_shadow_v01.yaml"


def _ab_config_path() -> Path:
    return _repo_root() / "config" / "state_ab_v01.yaml"


def freeze_state_ab_protocol(
    data_root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = _freeze_path(data_root)
    if path.exists():
        existing = _read_ab_required(path)
        _verify_runtime_hashes(existing)
        return {**existing, "status": "already_frozen"}

    core_freeze = _load_core_freeze(data_root)
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    first_eligible = _parse_date(core_freeze["first_eligible_effective_date"])
    if current.date() > first_eligible:
        raise RuntimeError(
            "State A/B protocol was not frozen before its first eligible live date; "
            "refusing retrospective experiment creation. Create a new protocol version."
        )

    state_config = _state_config_path()
    ab_config = _ab_config_path()
    for required in (state_config, ab_config):
        if not required.exists():
            raise FileNotFoundError(required)

    payload = {
        "schema_version": AB_SCHEMA_VERSION,
        "protocol": AB_PROTOCOL,
        "mode": MODE,
        "frozen_at": current.isoformat(),
        "first_eligible_effective_date": first_eligible.isoformat(),
        "core_paper_protocol": PAPER_PROTOCOL,
        "core_freeze_record_sha256": core_freeze["record_sha256"],
        "core_strategy": frozen_b3_v01.STRATEGY,
        "state_protocol": STATE_PROTOCOL,
        "allowed_multipliers": list(ALLOWED_MULTIPLIERS),
        "parameter_optimization_allowed": False,
        "retrospective_backfill_allowed": False,
        "a_data_source": "immutable Frozen B3 paper marks",
        "b_return_rule": "reuse_A_mark_asset_returns_with_state_adjusted_weights",
        "b_weight_rule": "uniformly_scale_Frozen_B3_risk_weights_and_release_to_CASH",
        "ab_implementation_sha256": _sha256_file(_module_path()),
        "state_implementation_sha256": _sha256_file(_state_impl_path()),
        "state_config_sha256": _sha256_file(state_config),
        "ab_config_sha256": _sha256_file(ab_config),
        "promotion_gate": {
            "minimum_comparable_days": MIN_COMPARABLE_DAYS,
            "minimum_intervention_days": MIN_INTERVENTION_DAYS,
            "max_cumulative_return_sacrifice": MAX_CUMULATIVE_RETURN_SACRIFICE,
            "max_drawdown_must_not_be_worse": True,
            "downside_volatility_must_not_be_worse": True,
            "ledger_integrity_required": True,
            "note": (
                "Promotion is based only on genuinely prospective A/B evidence. "
                "Historical reconstruction cannot satisfy this gate."
            ),
        },
    }
    written = _write_immutable_json(path, payload)
    return {**written, "status": "frozen"}


def _read_ab_required(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not _verify_sealed(value):
        raise ValueError(f"State A/B integrity failure: {path}")
    return value


def _verify_runtime_hashes(freeze: dict[str, Any]) -> None:
    paths = {
        "ab_implementation_sha256": _module_path(),
        "state_implementation_sha256": _state_impl_path(),
        "state_config_sha256": _state_config_path(),
        "ab_config_sha256": _ab_config_path(),
    }
    for key, path in paths.items():
        current = _sha256_file(path)
        if freeze.get(key) != current:
            raise RuntimeError(
                f"State A/B frozen hash changed for {path.name}; V0.1 is locked. "
                "Create a new protocol version instead of mutating the live experiment."
            )


def _load_freeze(data_root: Path) -> dict[str, Any]:
    freeze = _read_ab_required(_freeze_path(data_root))
    _verify_runtime_hashes(freeze)
    core_freeze = _load_core_freeze(data_root)
    if freeze.get("core_freeze_record_sha256") != core_freeze.get("record_sha256"):
        raise RuntimeError("State A/B freeze no longer matches Frozen B3 paper freeze")
    return freeze


def _load_records(root: Path, pattern: str) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob(pattern)):
        value = _read_ab_required(path)
        records.append({**value, "_path": str(path)})
    return records


def _load_decisions(data_root: Path) -> list[dict[str, Any]]:
    rows = _load_records(_ab_root(data_root) / "decisions", "year=*/month=*/effective_date=*.json")
    return sorted(rows, key=lambda row: row["effective_date"])


def _load_snapshots(data_root: Path) -> list[dict[str, Any]]:
    rows = _load_records(_ab_root(data_root) / "snapshots", "year=*/month=*/effective_date=*.json")
    return sorted(rows, key=lambda row: row["effective_date"])


def _load_marks(data_root: Path) -> list[dict[str, Any]]:
    rows = _load_records(_ab_root(data_root) / "marks", "year=*/month=*/date=*.json")
    return sorted(rows, key=lambda row: row["date"])


def _normalize_no_inputs_state(generated_at: datetime) -> dict[str, Any]:
    return {
        "protocol": STATE_PROTOCOL,
        "mode": shadow.MODE,
        "shadow_only": True,
        "core_protocol_mutated": False,
        "as_of": None,
        "generated_at": generated_at.isoformat(),
        "state_band": "NO_MODIFIER_DATA_INSUFFICIENT",
        "shadow_risk_multiplier": 1.0,
        "data_confidence": "NONE",
        "state_pressure": None,
        "leverage_pressure": None,
        "stablecoin_pressure": None,
        "valid_source_components": 0,
        "expected_source_components": 3,
        "status": "no_inputs",
    }


def create_state_ab_snapshot(
    data_root: Path,
    *,
    effective_date: str | date,
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
            "State A/B snapshots cannot be backfilled or future-dated; "
            "effective_date must equal current UTC date"
        )
    if effective.weekday() != frozen_b3_v01.REBALANCE_WEEKDAY:
        raise ValueError("State A/B snapshots are allowed only on Monday UTC")
    first_eligible = _parse_date(freeze["first_eligible_effective_date"])
    if effective < first_eligible:
        raise ValueError("State A/B snapshot predates the prospective experiment")

    b_path = _snapshot_path(data_root, effective)
    if b_path.exists():
        existing = _read_ab_required(b_path)
        return {**existing, "status": "already_exists"}

    a_path = _core_snapshot_path(data_root, effective)
    if not a_path.exists():
        raise FileNotFoundError(
            f"State A/B snapshot requires same-date immutable Frozen B3 snapshot: {a_path}"
        )
    a_snapshot = _read_required_json(a_path)
    if not _verify_core_sealed(a_snapshot):
        raise ValueError("Frozen B3 snapshot failed integrity verification")

    decision_path = _decision_path(data_root, effective)
    if decision_path.exists():
        decision = _read_ab_required(decision_path)
    else:
        state = shadow.build_latest_shadow_state(
            data_root,
            generated_at=current,
            write=False,
        )
        if state.get("status") == "no_inputs":
            state = _normalize_no_inputs_state(current)
        multiplier = float(state.get("shadow_risk_multiplier", 1.0))
        if multiplier not in ALLOWED_MULTIPLIERS:
            raise ValueError(f"State multiplier outside frozen set: {multiplier}")
        if state.get("shadow_only") is not True or state.get("core_protocol_mutated") is not False:
            raise ValueError("State decision violates Frozen Core isolation")
        decision_payload = {
            "schema_version": AB_SCHEMA_VERSION,
            "protocol": AB_PROTOCOL,
            "state_protocol": STATE_PROTOCOL,
            "effective_date": effective.isoformat(),
            "known_at": current.isoformat(),
            "ab_freeze_record_sha256": freeze["record_sha256"],
            "a_snapshot_record_sha256": a_snapshot["record_sha256"],
            "state_band": state.get("state_band"),
            "shadow_risk_multiplier": multiplier,
            "data_confidence": state.get("data_confidence"),
            "state_as_of": state.get("as_of"),
            "state_generated_at": state.get("generated_at"),
            "state_payload": state,
            "retrospective_backfill_allowed": False,
        }
        decision = _write_immutable_json(decision_path, decision_payload)

    multiplier = float(decision["shadow_risk_multiplier"])
    a_weights = pd.Series(
        {asset: float(a_snapshot["weights"].get(asset, 0.0)) for asset in frozen_b3_v01.ALL_ASSETS},
        dtype=float,
    )
    b_weights = shadow.apply_shadow_multiplier(a_weights, multiplier)

    payload = {
        "schema_version": AB_SCHEMA_VERSION,
        "protocol": AB_PROTOCOL,
        "mode": MODE,
        "effective_date": effective.isoformat(),
        "known_at": current.isoformat(),
        "ab_freeze_record_sha256": freeze["record_sha256"],
        "a_snapshot_effective_date": a_snapshot["effective_date"],
        "a_snapshot_record_sha256": a_snapshot["record_sha256"],
        "state_decision_record_sha256": decision["record_sha256"],
        "state_band": decision["state_band"],
        "shadow_risk_multiplier": multiplier,
        "data_confidence": decision["data_confidence"],
        "a_weights": {asset: float(a_weights[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
        "b_weights": {asset: float(b_weights[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
        "a_risk_gross": float(a_weights.loc[list(frozen_b3_v01.RISK_ASSETS)].sum()),
        "b_risk_gross": float(b_weights.loc[list(frozen_b3_v01.RISK_ASSETS)].sum()),
        "b_cash_weight": float(b_weights["CASH"]),
        "relative_core_weights_changed": False,
        "risk_increased_above_A": False,
    }
    written = _write_immutable_json(b_path, payload)
    return {**written, "status": "created"}


def _weights_from_b_snapshot(snapshot: dict[str, Any]) -> pd.Series:
    return pd.Series(
        {asset: float(snapshot["b_weights"].get(asset, 0.0)) for asset in frozen_b3_v01.ALL_ASSETS},
        dtype=float,
    )


def strict_mark_state_ab(
    data_root: Path,
    *,
    end: str | date,
) -> dict[str, Any]:
    freeze = _load_freeze(data_root)
    end_date = _parse_date(end)
    target = end_date - timedelta(days=1)
    snapshots = _load_snapshots(data_root)
    if not snapshots:
        return {
            "protocol": AB_PROTOCOL,
            "status": "no_snapshots",
            "created_marks": 0,
            "end_exclusive": end_date.isoformat(),
        }

    first_effective = _parse_date(snapshots[0]["effective_date"])
    if target < first_effective:
        return {
            "protocol": AB_PROTOCOL,
            "status": "before_first_snapshot",
            "created_marks": 0,
            "target_mark_date": target.isoformat(),
            "end_exclusive": end_date.isoformat(),
        }

    path = _mark_path(data_root, target)
    if path.exists():
        existing = _read_ab_required(path)
        return {**existing, "status": "already_marked", "created_marks": 0}

    marks = _load_marks(data_root)
    if marks:
        previous_date = _parse_date(marks[-1]["date"])
        expected = previous_date + timedelta(days=1)
        if target != expected:
            raise RuntimeError(
                "STATE_AB_LEDGER_GAP: refusing retrospective backfill. "
                f"last mark={previous_date.isoformat()}, next required={expected.isoformat()}, "
                f"requested={target.isoformat()}"
            )
    elif target != first_effective:
        raise RuntimeError(
            "STATE_AB_LEDGER_GAP: first A/B mark was missed; refusing backfill. "
            f"first snapshot={first_effective.isoformat()}, requested={target.isoformat()}"
        )

    a_mark_path = _core_mark_path(data_root, target)
    if not a_mark_path.exists():
        raise FileNotFoundError(
            f"State A/B mark requires same-date immutable Frozen B3 mark: {a_mark_path}"
        )
    a_mark = _read_required_json(a_mark_path)
    if not _verify_core_sealed(a_mark):
        raise ValueError("Frozen B3 mark failed integrity verification")

    eligible = [row for row in snapshots if _parse_date(row["effective_date"]) <= target]
    if not eligible:
        raise RuntimeError("no eligible State A/B snapshot for mark")
    active = eligible[-1]
    if active["a_snapshot_effective_date"] != a_mark["active_snapshot_effective_date"]:
        raise RuntimeError(
            "STATE_AB_SNAPSHOT_GAP: Frozen B3 changed snapshot but State A/B has no "
            "matching immutable Monday decision; refusing to carry stale B weights."
        )
    if active["a_snapshot_record_sha256"] != a_mark["active_snapshot_record_sha256"]:
        raise RuntimeError("State A/B snapshot link does not match Frozen B3 mark snapshot hash")

    weights = _weights_from_b_snapshot(active)
    if marks:
        previous_weights = pd.Series(
            {asset: float(marks[-1]["weights"].get(asset, 0.0)) for asset in frozen_b3_v01.ALL_ASSETS},
            dtype=float,
        )
        previous_equity = float(marks[-1]["equity_after"])
    else:
        previous_weights = pd.Series(0.0, index=frozen_b3_v01.ALL_ASSETS, dtype=float)
        previous_weights["CASH"] = 1.0
        previous_equity = 1.0

    turnover = float((weights - previous_weights).abs().sum() * 0.5)
    asset_returns = pd.Series(
        {asset: float(a_mark["asset_returns"].get(asset, 0.0)) for asset in frozen_b3_v01.ALL_ASSETS},
        dtype=float,
    )
    gross_return = float(weights.to_numpy(dtype=float) @ asset_returns.to_numpy(dtype=float))
    cost = turnover * (frozen_b3_v01.ONE_WAY_COST_BPS / 10_000.0)
    net_return = gross_return - cost
    cash_return = float(asset_returns["CASH"])
    equity_after = previous_equity * (1.0 + net_return)

    payload = {
        "schema_version": AB_SCHEMA_VERSION,
        "protocol": AB_PROTOCOL,
        "mode": MODE,
        "known_at": _utc_now().isoformat(),
        "date": target.isoformat(),
        "ab_freeze_record_sha256": freeze["record_sha256"],
        "a_mark_record_sha256": a_mark["record_sha256"],
        "a_snapshot_effective_date": a_mark["active_snapshot_effective_date"],
        "a_snapshot_record_sha256": a_mark["active_snapshot_record_sha256"],
        "b_snapshot_effective_date": active["effective_date"],
        "b_snapshot_record_sha256": active["record_sha256"],
        "state_decision_record_sha256": active["state_decision_record_sha256"],
        "state_band": active["state_band"],
        "shadow_risk_multiplier": float(active["shadow_risk_multiplier"]),
        "data_confidence": active["data_confidence"],
        "weights": {asset: float(weights[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
        "asset_returns": {asset: float(asset_returns[asset]) for asset in frozen_b3_v01.ALL_ASSETS},
        "turnover": turnover,
        "cost": cost,
        "gross_return": gross_return,
        "net_return": net_return,
        "cash_return": cash_return,
        "equity_after": equity_after,
        "a_net_return": float(a_mark["net_return"]),
        "a_equity_after": float(a_mark["equity_after"]),
        "return_delta_B_minus_A": net_return - float(a_mark["net_return"]),
        "retrospective_backfill_allowed": False,
    }
    written = _write_immutable_json(path, payload)
    return {**written, "status": "marked", "created_marks": 1}


def _annualized_sharpe_excess(returns: pd.Series, cash: pd.Series) -> float | None:
    if len(returns) < 2:
        return None
    excess = returns - cash
    std = float(excess.std(ddof=1))
    if not math.isfinite(std) or std <= 0:
        return None
    return float(excess.mean() / std * math.sqrt(365.0))


def _max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return 0.0
    equity = (1.0 + returns).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return float(drawdown.min())


def _downside_volatility(returns: pd.Series) -> float | None:
    negative = returns.loc[returns < 0]
    if len(negative) < 2:
        return None
    value = float(negative.std(ddof=1) * math.sqrt(365.0))
    return value if math.isfinite(value) else None


def state_ab_integrity_report(data_root: Path) -> dict[str, Any]:
    try:
        freeze = _load_freeze(data_root)
    except FileNotFoundError:
        return {"protocol": AB_PROTOCOL, "frozen": False, "ok": False, "error": "not frozen"}

    decisions = _load_decisions(data_root)
    snapshots = _load_snapshots(data_root)
    marks = _load_marks(data_root)
    a_snapshots = {
        row["effective_date"]: row for row in _load_core_snapshots(data_root)
    }
    a_marks = {row["date"]: row for row in _load_core_marks(data_root)}
    decisions_by_date = {row["effective_date"]: row for row in decisions}

    snapshot_dates = [_parse_date(row["effective_date"]) for row in snapshots]
    mark_dates = [_parse_date(row["date"]) for row in marks]
    checks: dict[str, bool] = {
        "decision_dates_unique": len(decisions) == len(decisions_by_date),
        "snapshot_dates_unique": len(snapshot_dates) == len(set(snapshot_dates)),
        "snapshots_are_mondays": all(value.weekday() == 0 for value in snapshot_dates),
        "mark_dates_unique": len(mark_dates) == len(set(mark_dates)),
        "mark_dates_contiguous": all(
            right - left == timedelta(days=1) for left, right in zip(mark_dates, mark_dates[1:])
        ),
        "snapshot_links_to_A": True,
        "snapshot_links_to_state_decision": True,
        "mark_links_to_A": True,
        "mark_links_to_B_snapshot": True,
        "multipliers_frozen": True,
        "B_never_increases_risk": True,
    }

    snapshot_by_hash = {row["record_sha256"]: row for row in snapshots}
    for row in snapshots:
        a = a_snapshots.get(row["effective_date"])
        decision = decisions_by_date.get(row["effective_date"])
        if a is None or row.get("a_snapshot_record_sha256") != a.get("record_sha256"):
            checks["snapshot_links_to_A"] = False
        if decision is None or row.get("state_decision_record_sha256") != decision.get("record_sha256"):
            checks["snapshot_links_to_state_decision"] = False
        multiplier = float(row.get("shadow_risk_multiplier", -1))
        if multiplier not in ALLOWED_MULTIPLIERS:
            checks["multipliers_frozen"] = False
        if float(row.get("b_risk_gross", 2.0)) > float(row.get("a_risk_gross", 1.0)) + 1e-12:
            checks["B_never_increases_risk"] = False

    for row in marks:
        a = a_marks.get(row["date"])
        if a is None or row.get("a_mark_record_sha256") != a.get("record_sha256"):
            checks["mark_links_to_A"] = False
        b = snapshot_by_hash.get(row.get("b_snapshot_record_sha256"))
        if b is None:
            checks["mark_links_to_B_snapshot"] = False
        if float(row.get("shadow_risk_multiplier", -1)) not in ALLOWED_MULTIPLIERS:
            checks["multipliers_frozen"] = False

    return {
        "protocol": AB_PROTOCOL,
        "frozen": True,
        "ok": all(checks.values()),
        "first_eligible_effective_date": freeze["first_eligible_effective_date"],
        "decision_count": len(decisions),
        "snapshot_count": len(snapshots),
        "mark_count": len(marks),
        "checks": checks,
        "policy": "NO_RETROSPECTIVE_AB_BACKFILL",
    }


def state_ab_status(data_root: Path) -> dict[str, Any]:
    path = _freeze_path(data_root)
    if not path.exists():
        return {"protocol": AB_PROTOCOL, "state": "NOT_FROZEN", "frozen": False}
    freeze = _load_freeze(data_root)
    decisions = _load_decisions(data_root)
    snapshots = _load_snapshots(data_root)
    marks = _load_marks(data_root)
    integrity = state_ab_integrity_report(data_root)

    if marks:
        b = pd.Series([float(row["net_return"]) for row in marks], dtype=float)
        a = pd.Series([float(row["a_net_return"]) for row in marks], dtype=float)
        cash = pd.Series([float(row["cash_return"]) for row in marks], dtype=float)
        b_cum = float(np.prod(1.0 + b.to_numpy()) - 1.0)
        a_cum = float(np.prod(1.0 + a.to_numpy()) - 1.0)
        cash_cum = float(np.prod(1.0 + cash.to_numpy()) - 1.0)
        b_dd = _max_drawdown(b)
        a_dd = _max_drawdown(a)
        b_downside = _downside_volatility(b)
        a_downside = _downside_volatility(a)
        b_sharpe = _annualized_sharpe_excess(b, cash)
        a_sharpe = _annualized_sharpe_excess(a, cash)
        comparable_days = len(marks)
        intervention_days = sum(float(row["shadow_risk_multiplier"]) < 1.0 for row in marks)
        avg_multiplier = float(np.mean([float(row["shadow_risk_multiplier"]) for row in marks]))
    else:
        b_cum = a_cum = cash_cum = 0.0
        b_dd = a_dd = 0.0
        b_downside = a_downside = None
        b_sharpe = a_sharpe = None
        comparable_days = intervention_days = 0
        avg_multiplier = None

    return_sacrifice = a_cum - b_cum
    checks = {
        "minimum_comparable_days": comparable_days >= MIN_COMPARABLE_DAYS,
        "minimum_intervention_days": intervention_days >= MIN_INTERVENTION_DAYS,
        "max_drawdown_not_worse": b_dd >= a_dd - 1e-12,
        "downside_volatility_not_worse": (
            b_downside is not None and a_downside is not None and b_downside <= a_downside + 1e-12
        ),
        "return_sacrifice_within_limit": return_sacrifice <= MAX_CUMULATIVE_RETURN_SACRIFICE,
        "ledger_integrity": bool(integrity.get("ok")),
    }
    promoted = all(checks.values())
    if not snapshots:
        state = "FROZEN_AWAITING_FIRST_LIVE_AB_SNAPSHOT"
    elif not marks:
        state = "LIVE_AWAITING_FIRST_COMPARABLE_MARK"
    elif promoted:
        state = "O2_PROSPECTIVE_RISK_MODIFIER_SUPPORTED"
    else:
        state = "O2_SHADOW_EVIDENCE_ACCUMULATING"

    return {
        "protocol": AB_PROTOCOL,
        "mode": MODE,
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "first_eligible_effective_date": freeze["first_eligible_effective_date"],
        "decision_count": len(decisions),
        "snapshot_count": len(snapshots),
        "mark_count": len(marks),
        "comparable_days": comparable_days,
        "intervention_days": intervention_days,
        "average_multiplier": avg_multiplier,
        "A_cumulative_return": a_cum,
        "B_cumulative_return": b_cum,
        "cash_cumulative_return": cash_cum,
        "B_minus_A_cumulative_return": b_cum - a_cum,
        "A_sharpe_excess_cash": a_sharpe,
        "B_sharpe_excess_cash": b_sharpe,
        "A_max_drawdown": a_dd,
        "B_max_drawdown": b_dd,
        "A_downside_volatility": a_downside,
        "B_downside_volatility": b_downside,
        "promotion_gate": freeze["promotion_gate"],
        "promotion_gate_checks": checks,
        "integrity_ok": bool(integrity.get("ok")),
        "parameter_optimization_allowed": False,
        "retrospective_backfill_allowed": False,
    }
