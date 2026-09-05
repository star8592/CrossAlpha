from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import v03


PROSPECTIVE_PROTOCOL = "CROSSALPHA_STATE_V0_3_PROSPECTIVE"
FREEZE_SCHEMA_VERSION = 1


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _research_root(data_root: Path) -> Path:
    return data_root / "research" / "state_v03"


def freeze_path(data_root: Path) -> Path:
    return _research_root(data_root) / "freeze.json"


def _record_path(data_root: Path, block_number: int) -> Path:
    return _research_root(data_root) / "prospective" / f"block={int(block_number)}.json"


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


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


def verify_seal(value: dict[str, Any]) -> bool:
    expected = value.get("record_sha256")
    return isinstance(expected, str) and expected == _payload_hash(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_files() -> dict[str, Path]:
    root = _repo_root()
    return {
        "state_v03": root / "src" / "crossalpha" / "state" / "v03.py",
        "state_v03_rpc": root / "src" / "crossalpha" / "state" / "v03_rpc.py",
        "state_v03_cycle": root / "src" / "crossalpha" / "state" / "v03_cycle.py",
        "state_v03_watchlist": root / "src" / "crossalpha" / "state" / "v03_watchlist.py",
        "state_v03_prospective": root / "src" / "crossalpha" / "state" / "v03_prospective.py",
        "state_v03_config": root / "src" / "crossalpha" / "state" / "v03_config.py",
        "config": root / "config" / "state_v03.yaml",
    }


def implementation_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in _implementation_files().items():
        if not path.exists():
            raise FileNotFoundError(path)
        result[name] = sha256_file(path)
    return result


def _reference_paths(data_root: Path) -> dict[str, Path]:
    return {
        "frozen_b3": data_root / "research" / "free_v01" / "paper" / "freeze.json",
        "state_ab_v01": data_root / "research" / "free_v01" / "state_ab_v01" / "freeze.json",
        "state_v02": data_root / "research" / "state_v02" / "freeze.json",
    }


def _write_immutable(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError(f"immutable State V0.3 record failed seal verification: {path}")
        return existing
    sealed = _seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return sealed


def freeze_state_v03(
    data_root: Path,
    *,
    minimum_eligible_block: int,
    now: Any | None = None,
) -> dict[str, Any]:
    path = freeze_path(data_root)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError("existing State V0.3 freeze failed seal verification")
        return {**existing, "status": "already_frozen"}
    if int(minimum_eligible_block) < 0:
        raise ValueError("minimum_eligible_block must be non-negative")

    from crossalpha.state.v03_config import strict_v03_config_report

    config_report = strict_v03_config_report(_repo_root() / "config" / "state_v03.yaml")
    if not config_report.get("ok"):
        raise ValueError(f"State V0.3 config/implementation mismatch: {config_report}")

    references: dict[str, dict[str, str]] = {}
    for name, reference in _reference_paths(data_root).items():
        if not reference.exists():
            raise FileNotFoundError(
                f"State V0.3 requires frozen predecessor protocol before freeze: {reference}"
            )
        references[name] = {"path": str(reference), "file_sha256": sha256_file(reference)}

    frozen_at = _utc(now or datetime.now(timezone.utc))
    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v03.PROTOCOL,
        "mode": v03.MODE,
        "maturity": "O0_DATA_TO_O1_DESCRIPTION",
        "actionability": v03.ACTIONABILITY,
        "risk_multiplier": None,
        "frozen_at": frozen_at.isoformat(),
        "minimum_eligible_block": int(minimum_eligible_block),
        "historical_bootstrap_is_evidence": False,
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
        "automatic_actionability_allowed": False,
        "implementation_file_sha256": implementation_hashes(),
        "reference_freezes": references,
        "promotion_gate": {
            "minimum_calendar_days_before_O2_candidate": 180,
            "minimum_valid_full_censuses": 120,
            "minimum_distinct_cliff_stress_episodes": 5,
            "cliff_episode_critical_debt_share_threshold": 0.05,
            "cliff_episode_cooldown_hours": 24,
            "requires_complete_borrower_bootstrap": True,
            "requires_outcome_linkage_test": True,
            "requires_predeclared_O2_rule": True,
            "automatic_promotion_to_actionable_modifier_allowed": False,
        },
    }
    written = _write_immutable(path, payload)
    return {**written, "status": "frozen"}


def load_freeze(data_root: Path) -> dict[str, Any]:
    path = freeze_path(data_root)
    if not path.exists():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not verify_seal(value):
        raise ValueError("State V0.3 freeze failed seal verification")
    return value


def hashes_unchanged(data_root: Path, freeze: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    current_impl = implementation_hashes()
    expected_impl = freeze.get("implementation_file_sha256", {})
    impl = {
        name: {
            "expected": expected_impl.get(name),
            "current": current_impl.get(name),
            "unchanged": expected_impl.get(name) == current_impl.get(name),
        }
        for name in sorted(set(expected_impl) | set(current_impl))
    }
    refs: dict[str, Any] = {}
    for name, path in _reference_paths(data_root).items():
        current = sha256_file(path) if path.exists() else None
        expected = freeze.get("reference_freezes", {}).get(name, {}).get("file_sha256")
        refs[name] = {"expected": expected, "current": current, "unchanged": expected == current}
    ok = all(row["unchanged"] for row in impl.values()) and all(
        row["unchanged"] for row in refs.values()
    )
    return ok, {"implementation": impl, "references": refs}


def write_full_census_observation(
    data_root: Path,
    *,
    summary_path: Path,
    detail_path: Path,
    known_at: Any | None = None,
) -> dict[str, Any]:
    freeze = load_freeze(data_root)
    hashes_ok, details = hashes_unchanged(data_root, freeze)
    if not hashes_ok:
        raise RuntimeError(f"STATE_V03_HASH_GRAPH_MUTATED: {details}")
    if not summary_path.exists() or not detail_path.exists():
        raise FileNotFoundError("full census summary/detail artifact missing")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("protocol") != v03.PROTOCOL:
        raise ValueError("full census artifact is not State V0.3")
    if summary.get("actionability") != v03.ACTIONABILITY or summary.get("risk_multiplier") is not None:
        raise ValueError("State V0.3 prospective ledger accepts descriptive-only censuses")
    if not summary.get("bootstrap_complete") or not summary.get("valid_full_census"):
        raise ValueError("prospective State V0.3 requires a valid complete full census")
    if Path(str(summary.get("detail_path", ""))) != detail_path:
        raise ValueError("full census summary points to a different detail artifact")
    detail_hash = sha256_file(detail_path)
    if summary.get("detail_sha256") != detail_hash:
        raise ValueError("full census detail hash does not match its summary artifact")

    current = _utc(known_at or datetime.now(timezone.utc))
    frozen_at = _utc(freeze["frozen_at"])
    captured = _utc(summary["captured_at"])
    if current < frozen_at:
        raise ValueError("prospective census known_at cannot predate State V0.3 freeze")
    if captured < frozen_at:
        raise ValueError("prospective census captured_at cannot predate State V0.3 freeze")
    if current < captured:
        raise ValueError("prospective census known_at cannot precede captured_at")

    block_number = int(summary["block_number"])
    minimum_block = int(freeze["minimum_eligible_block"])
    if block_number < minimum_block:
        raise ValueError(
            "prospective census block predates the frozen minimum eligible block; backfill refused"
        )

    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v03.PROTOCOL,
        "freeze_record_sha256": freeze["record_sha256"],
        "known_at": current.isoformat(),
        "captured_at": captured.isoformat(),
        "block_number": block_number,
        "minimum_eligible_block": minimum_block,
        "summary_path": str(summary_path),
        "summary_sha256": sha256_file(summary_path),
        "detail_path": str(detail_path),
        "detail_sha256": detail_hash,
        "actionability": v03.ACTIONABILITY,
        "risk_multiplier": None,
        "candidate_address_count": summary.get("candidate_address_count"),
        "account_call_coverage_ratio": summary.get("account_call_coverage_ratio"),
        "active_borrower_count": summary.get("active_borrower_count"),
        "total_active_debt_usd": summary.get("total_active_debt_usd"),
        "debt_weighted_hf_p10": summary.get("debt_weighted_hf_p10"),
        "debt_weighted_hf_p25": summary.get("debt_weighted_hf_p25"),
        "debt_weighted_hf_p50": summary.get("debt_weighted_hf_p50"),
        "liquidatable_debt_share": summary.get("liquidatable_debt_share"),
        "critical_hf_le_1_05_debt_share": summary.get("critical_hf_le_1_05_debt_share"),
        "near_cliff_hf_le_1_20_debt_share": summary.get("near_cliff_hf_le_1_20_debt_share"),
        "watchlist_count": summary.get("watchlist_count"),
    }
    path = _record_path(data_root, block_number)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError(f"existing block record failed seal verification: {path}")
        if (
            existing.get("summary_sha256") != payload["summary_sha256"]
            or existing.get("detail_sha256") != payload["detail_sha256"]
            or existing.get("freeze_record_sha256") != payload["freeze_record_sha256"]
        ):
            raise RuntimeError(
                "STATE_V03_BLOCK_COLLISION: same finalized block cannot be relabeled with different census artifacts"
            )
        return {**existing, "status": "already_exists", "output": str(path)}

    written = _write_immutable(path, payload)
    return {**written, "status": "written", "output": str(path)}
