from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import v02


FREEZE_SCHEMA_VERSION = 1
PROSPECTIVE_PROTOCOL = "CROSSALPHA_STATE_V0_2_PROSPECTIVE"
EXPECTED_CADENCE_MINUTES = 15
MAX_LIVE_WRITE_AGE_MINUTES = 10


def _utc(value: Any) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _research_root(data_root: Path) -> Path:
    return data_root / "research" / "state_v02"


def _freeze_path(data_root: Path) -> Path:
    return _research_root(data_root) / "freeze.json"


def _observation_path(data_root: Path, generated_at: pd.Timestamp) -> Path:
    return (
        _research_root(data_root)
        / "prospective"
        / f"year={generated_at:%Y}"
        / f"month={generated_at:%m}"
        / f"day={generated_at:%d}"
        / f"state_at={generated_at:%H%M%S%f}.json"
    )


def _write_immutable(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not _verify_sealed(existing):
            raise ValueError(f"State V0.2 immutable record failed seal verification: {path}")
        return existing
    sealed = _seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return sealed


def _implementation_files() -> dict[str, Path]:
    root = _repo_root()
    return {
        "state_v02": root / "src" / "crossalpha" / "state" / "v02.py",
        "prospective_v02": root / "src" / "crossalpha" / "state" / "v02_prospective.py",
        "aave_provider": root / "src" / "crossalpha" / "observatory" / "providers" / "aave.py",
        "aave_canonical": root / "src" / "crossalpha" / "observatory" / "canonical" / "aave.py",
        "config": root / "config" / "state_v02.yaml",
    }


def _file_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in _implementation_files().items():
        if not path.exists():
            raise FileNotFoundError(path)
        result[name] = _sha256_file(path)
    return result


def _reference_freeze_paths(data_root: Path) -> dict[str, Path]:
    return {
        "frozen_b3": data_root / "research" / "free_v01" / "paper" / "freeze.json",
        "state_ab_v01": data_root / "research" / "free_v01" / "state_ab_v01" / "freeze.json",
    }


def freeze_state_v02(
    data_root: Path,
    *,
    now: Any | None = None,
) -> dict[str, Any]:
    path = _freeze_path(data_root)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not _verify_sealed(existing):
            raise ValueError("existing State V0.2 freeze failed seal verification")
        return {**existing, "status": "already_frozen"}

    protocol_path = _repo_root() / "config" / "state_v02.yaml"
    consistency = v02.config_consistency_report(protocol_path)
    if not consistency.get("ok"):
        raise ValueError(f"State V0.2 config/implementation mismatch: {consistency}")

    references: dict[str, dict[str, str]] = {}
    for name, reference in _reference_freeze_paths(data_root).items():
        if not reference.exists():
            raise FileNotFoundError(
                f"State V0.2 requires the already-frozen V0.1 reference: {reference}"
            )
        references[name] = {
            "path": str(reference),
            "file_sha256": _sha256_file(reference),
        }

    frozen_at = _utc(now or datetime.now(timezone.utc))
    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v02.PROTOCOL,
        "mode": v02.MODE,
        "maturity": "O0_DATA_TO_O1_DESCRIPTION",
        "actionability": v02.ACTIONABILITY,
        "risk_multiplier": None,
        "frozen_at": frozen_at.isoformat(),
        "first_eligible_observed_at": frozen_at.isoformat(),
        "expected_cadence_minutes": EXPECTED_CADENCE_MINUTES,
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
        "historical_data_can_promote_to_O2": False,
        "implementation_file_sha256": _file_hashes(),
        "reference_freezes": references,
        "promotion_gate": {
            "minimum_calendar_days_before_O2_candidate": 180,
            "minimum_observations": 500,
            "minimum_distinct_stress_episodes": 5,
            "stress_episode_score_threshold": 0.67,
            "stress_episode_cooldown_hours": 24,
            "requires_outcome_linkage_test": True,
            "requires_predeclared_O2_rule": True,
            "maximum_automatic_state": "ELIGIBLE_FOR_O2_PROTOCOL_DESIGN",
        },
    }
    written = _write_immutable(path, payload)
    return {**written, "status": "frozen"}


def _load_freeze(data_root: Path) -> dict[str, Any]:
    path = _freeze_path(data_root)
    if not path.exists():
        raise FileNotFoundError(path)
    freeze = json.loads(path.read_text(encoding="utf-8"))
    if not _verify_sealed(freeze):
        raise ValueError("State V0.2 freeze failed seal verification")
    return freeze


def _hashes_unchanged(freeze: dict[str, Any]) -> tuple[bool, dict[str, dict[str, Any]]]:
    expected = freeze.get("implementation_file_sha256", {})
    current = _file_hashes()
    details: dict[str, dict[str, Any]] = {}
    ok = True
    for name in sorted(set(expected) | set(current)):
        same = expected.get(name) == current.get(name)
        details[name] = {
            "expected": expected.get(name),
            "current": current.get(name),
            "unchanged": same,
        }
        ok = ok and same
    return ok, details


def _reference_hashes_unchanged(
    data_root: Path, freeze: dict[str, Any]
) -> tuple[bool, dict[str, dict[str, Any]]]:
    details: dict[str, dict[str, Any]] = {}
    ok = True
    frozen = freeze.get("reference_freezes", {})
    for name, path in _reference_freeze_paths(data_root).items():
        expected = frozen.get(name, {}).get("file_sha256")
        current = _sha256_file(path) if path.exists() else None
        same = expected == current and current is not None
        details[name] = {"expected": expected, "current": current, "unchanged": same}
        ok = ok and same
    return ok, details


def write_live_state_v02_observation(
    data_root: Path,
    snapshot: dict[str, Any],
    *,
    now: Any | None = None,
    strict_live: bool = True,
) -> dict[str, Any]:
    freeze = _load_freeze(data_root)
    impl_ok, _ = _hashes_unchanged(freeze)
    refs_ok, _ = _reference_hashes_unchanged(data_root, freeze)
    if not impl_ok:
        raise RuntimeError(
            "STATE_V02_IMPLEMENTATION_MUTATED: create a new protocol version instead of "
            "continuing the frozen prospective ledger"
        )
    if not refs_ok:
        raise RuntimeError("STATE_V02_REFERENCE_MUTATED: Frozen V0.1 reference hash changed")
    if snapshot.get("protocol") != v02.PROTOCOL:
        raise ValueError("snapshot is not CROSSALPHA_STATE_V0_2")
    if snapshot.get("actionability") != v02.ACTIONABILITY or snapshot.get("risk_multiplier") is not None:
        raise ValueError("State V0.2 prospective ledger accepts descriptive-only snapshots")
    if any(
        snapshot.get(field) is not False
        for field in ("mutates_frozen_core", "mutates_state_v01", "mutates_state_ab_v01")
    ):
        raise ValueError("State V0.2 snapshot claims mutation of a frozen V0.1 protocol")

    generated = _utc(snapshot["generated_at"])
    current = _utc(now or datetime.now(timezone.utc))
    first_eligible = _utc(freeze["first_eligible_observed_at"])
    if generated < first_eligible:
        raise ValueError("prospective State V0.2 observations cannot predate the freeze")
    if strict_live:
        age = current - generated
        if age < pd.Timedelta(0) or age > pd.Timedelta(minutes=MAX_LIVE_WRITE_AGE_MINUTES):
            raise ValueError(
                "prospective State V0.2 observation is not live; retrospective backfill refused"
            )

    derived_output = snapshot.get("output")
    if not isinstance(derived_output, str):
        raise ValueError("prospective State V0.2 observation requires a written derived state")
    derived_path = Path(derived_output)
    if not derived_path.exists():
        raise FileNotFoundError(derived_path)

    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v02.PROTOCOL,
        "freeze_record_sha256": freeze["record_sha256"],
        "known_at": current.isoformat(),
        "as_of": snapshot["as_of"],
        "generated_at": generated.isoformat(),
        "derived_state_path": str(derived_path),
        "derived_state_sha256": _sha256_file(derived_path),
        "actionability": v02.ACTIONABILITY,
        "risk_multiplier": None,
        "data_confidence": snapshot.get("data_confidence"),
        "descriptive_stress_score": snapshot.get("descriptive_stress_score"),
        "valid_pressure_component_count": snapshot.get("valid_pressure_component_count"),
        "valid_pressure_components": snapshot.get("valid_pressure_components", []),
        "aave_market_pressure": snapshot.get("components", {}).get("aave_market_stress", {}).get("pressure"),
        "stablecoin_flow_pressure": snapshot.get("components", {}).get("stablecoin_flow_decomposition", {}).get("pressure"),
        "stablecoin_net_change_ratio": snapshot.get("components", {}).get("stablecoin_flow_decomposition", {}).get("net_system_change_ratio"),
        "stablecoin_migration_ratio": snapshot.get("components", {}).get("stablecoin_flow_decomposition", {}).get("migration_ratio"),
        "basis_dispersion_pressure": snapshot.get("components", {}).get("basis_dispersion", {}).get("pressure"),
        "contagion_pressure": snapshot.get("components", {}).get("contagion_graph", {}).get("pressure"),
        "borrower_health_factor_distribution_valid": snapshot.get("borrower_health_factor_distribution", {}).get("valid", False),
        "aave_liquidation_events_24h": snapshot.get("aave_liquidation_activity", {}).get("events_24h"),
        "aave_liquidation_events_7d": snapshot.get("aave_liquidation_activity", {}).get("events_7d"),
        "deployment_activation_proxy": snapshot.get("deployment_activation", {}).get("coincident_activation_proxy"),
    }
    path = _observation_path(data_root, generated)
    written = _write_immutable(path, payload)
    return {**written, "status": "written" if written.get("known_at") == payload["known_at"] else "already_exists", "output": str(path)}


def _load_observations(data_root: Path) -> list[dict[str, Any]]:
    root = _research_root(data_root) / "prospective"
    if not root.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("year=*/month=*/day=*/state_at=*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        row["_path"] = str(path)
        result.append(row)
    return result


def _count_stress_episodes(
    observations: list[dict[str, Any]], threshold: float, cooldown_hours: int
) -> int:
    qualifying = sorted(
        (
            _utc(row["generated_at"])
            for row in observations
            if row.get("descriptive_stress_score") is not None
            and float(row["descriptive_stress_score"]) >= threshold
        )
    )
    count = 0
    last_start: pd.Timestamp | None = None
    cooldown = pd.Timedelta(hours=cooldown_hours)
    for timestamp in qualifying:
        if last_start is None or timestamp - last_start >= cooldown:
            count += 1
            last_start = timestamp
    return count


def state_v02_integrity_report(data_root: Path) -> dict[str, Any]:
    try:
        freeze = _load_freeze(data_root)
    except FileNotFoundError:
        return {"protocol": PROSPECTIVE_PROTOCOL, "frozen": False, "ok": False, "error": "not_frozen"}
    observations = _load_observations(data_root)
    impl_ok, impl_details = _hashes_unchanged(freeze)
    refs_ok, ref_details = _reference_hashes_unchanged(data_root, freeze)
    first_eligible = _utc(freeze["first_eligible_observed_at"])

    checks = {
        "freeze_seal": _verify_sealed(freeze),
        "implementation_hashes_unchanged": impl_ok,
        "v01_reference_hashes_unchanged": refs_ok,
        "observation_seals": True,
        "freeze_links": True,
        "no_pre_freeze_observations": True,
        "descriptive_only": True,
        "derived_state_hash_links": True,
        "generated_at_unique": True,
        "generated_at_monotonic": True,
    }
    generated: list[pd.Timestamp] = []
    seen: set[str] = set()
    for row in observations:
        if not _verify_sealed(row):
            checks["observation_seals"] = False
        if row.get("freeze_record_sha256") != freeze.get("record_sha256"):
            checks["freeze_links"] = False
        ts = _utc(row["generated_at"])
        generated.append(ts)
        key = ts.isoformat()
        if key in seen:
            checks["generated_at_unique"] = False
        seen.add(key)
        if ts < first_eligible:
            checks["no_pre_freeze_observations"] = False
        if row.get("actionability") != v02.ACTIONABILITY or row.get("risk_multiplier") is not None:
            checks["descriptive_only"] = False
        derived = Path(str(row.get("derived_state_path", "")))
        expected = row.get("derived_state_sha256")
        if not derived.exists() or expected != _sha256_file(derived):
            checks["derived_state_hash_links"] = False
    if generated != sorted(generated):
        checks["generated_at_monotonic"] = False

    gap_count = 0
    max_gap_minutes = 0.0
    for left, right in zip(sorted(generated), sorted(generated)[1:]):
        gap = float((right - left) / pd.Timedelta(minutes=1))
        max_gap_minutes = max(max_gap_minutes, gap)
        if gap > EXPECTED_CADENCE_MINUTES * 2.25:
            gap_count += 1

    return {
        "protocol": PROSPECTIVE_PROTOCOL,
        "frozen": True,
        "ok": all(checks.values()),
        "observation_count": len(observations),
        "first_observation_at": generated[0].isoformat() if generated else None,
        "last_observation_at": generated[-1].isoformat() if generated else None,
        "gap_count": gap_count,
        "max_gap_minutes": max_gap_minutes,
        "gaps_are_visible_not_backfilled": True,
        "checks": checks,
        "implementation_hashes": impl_details,
        "reference_hashes": ref_details,
    }


def state_v02_status(data_root: Path) -> dict[str, Any]:
    integrity = state_v02_integrity_report(data_root)
    if not integrity.get("frozen"):
        return {
            "protocol": PROSPECTIVE_PROTOCOL,
            "state": "NOT_FROZEN",
            "frozen": False,
            "integrity_ok": False,
        }
    freeze = _load_freeze(data_root)
    observations = _load_observations(data_root)
    gate = freeze["promotion_gate"]
    if observations:
        times = sorted(_utc(row["generated_at"]) for row in observations)
        calendar_days = int((times[-1] - times[0]) / pd.Timedelta(days=1)) + 1
    else:
        calendar_days = 0
    episodes = _count_stress_episodes(
        observations,
        float(gate["stress_episode_score_threshold"]),
        int(gate["stress_episode_cooldown_hours"]),
    )
    volume_checks = {
        "minimum_calendar_days": calendar_days >= int(gate["minimum_calendar_days_before_O2_candidate"]),
        "minimum_observations": len(observations) >= int(gate["minimum_observations"]),
        "minimum_distinct_stress_episodes": episodes >= int(gate["minimum_distinct_stress_episodes"]),
        "ledger_integrity": bool(integrity.get("ok")),
    }
    data_ready = all(volume_checks.values())
    if not observations:
        state = "FROZEN_AWAITING_FIRST_LIVE_OBSERVATION"
    elif data_ready:
        state = "ELIGIBLE_FOR_O2_PROTOCOL_DESIGN"
    else:
        state = "O1_PROSPECTIVE_EVIDENCE_ACCUMULATING"
    latest = observations[-1] if observations else None
    return {
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v02.PROTOCOL,
        "state": state,
        "frozen": True,
        "frozen_at": freeze["frozen_at"],
        "actionability": v02.ACTIONABILITY,
        "risk_multiplier": None,
        "observation_count": len(observations),
        "prospective_calendar_days": calendar_days,
        "stress_episode_count": episodes,
        "latest_generated_at": latest.get("generated_at") if latest else None,
        "latest_descriptive_stress_score": latest.get("descriptive_stress_score") if latest else None,
        "latest_data_confidence": latest.get("data_confidence") if latest else None,
        "gap_count": integrity.get("gap_count", 0),
        "promotion_gate": gate,
        "promotion_volume_checks": volume_checks,
        "outcome_linkage_test_completed": False,
        "predeclared_O2_rule_exists": False,
        "automatic_promotion_to_actionable_modifier_allowed": False,
        "integrity_ok": bool(integrity.get("ok")),
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
    }
