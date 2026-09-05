from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


PROTOCOL = "CROSSALPHA_OUTCOME_LINKAGE_V0_1"
MODE = "PROSPECTIVE_STATE_TO_REALIZED_OUTCOME_LINKAGE"
SCHEMA_VERSION = 1
HORIZONS_DAYS = (1, 3, 7, 14, 28)
SOURCE_LAYERS = ("STATE_V02", "STATE_V03", "STATE_V04")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _root(data_root: Path) -> Path:
    return data_root / "research" / "outcome_linkage_v01"


def freeze_path(data_root: Path) -> Path:
    return _root(data_root) / "freeze.json"


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


def seal(value: dict[str, Any]) -> dict[str, Any]:
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
        "outcome_prospective": root / "src" / "crossalpha" / "outcomes" / "prospective.py",
        "outcome_linkage": root / "src" / "crossalpha" / "outcomes" / "linkage.py",
        "outcome_config": root / "config" / "outcome_linkage_v01.yaml",
    }


def implementation_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, path in _implementation_files().items():
        if not path.exists():
            raise FileNotFoundError(path)
        result[name] = sha256_file(path)
    return result


def reference_paths(data_root: Path) -> dict[str, Path]:
    return {
        "frozen_b3": data_root / "research" / "free_v01" / "paper" / "freeze.json",
        "state_ab_v01": data_root / "research" / "free_v01" / "state_ab_v01" / "freeze.json",
        "state_v02": data_root / "research" / "state_v02" / "freeze.json",
        "state_v03": data_root / "research" / "state_v03" / "freeze.json",
        "state_v04": data_root / "research" / "state_v04" / "freeze.json",
    }


def _load_config() -> dict[str, Any]:
    path = _repo_root() / "config" / "outcome_linkage_v01.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def config_consistency_report() -> dict[str, Any]:
    raw = _load_config()
    anchors = raw.get("anchors", {})
    policy = raw.get("research_policy", {})
    completeness = raw.get("completeness", {})
    checks = {
        "protocol": raw.get("protocol") == PROTOCOL,
        "mode": raw.get("mode") == MODE,
        "actionability_none": raw.get("actionability") == "NONE",
        "risk_multiplier_null": raw.get("risk_multiplier") is None,
        "no_predecessor_mutation": policy.get("mutates_any_predecessor") is False,
        "no_parameter_optimization": policy.get("parameter_optimization_allowed") is False,
        "no_selective_linking": policy.get("selective_linking_allowed") is False,
        "deterministic_late_materialization": policy.get(
            "deterministic_late_materialization_allowed"
        ) is True,
        "source_must_be_prospective": policy.get(
            "source_observations_must_already_be_prospective"
        ) is True,
        "outcome_marks_immutable": policy.get("outcome_marks_must_already_be_immutable") is True,
        "same_day_forbidden": policy.get("same_day_outcome_allowed") is False,
        "no_auto_actionability": policy.get("automatic_actionability_allowed") is False,
        "timezone": anchors.get("timezone") == "UTC",
        "daily_sampling_unit": anchors.get("granularity") == "one_per_source_per_utc_day",
        "anchor_selection": anchors.get("selection") == "latest_known_at_within_source_utc_day",
        "source_layers": tuple(anchors.get("source_layers", [])) == SOURCE_LAYERS,
        "outcome_start": anchors.get("outcome_start")
        == "next_full_utc_day_after_anchor_known_at_date",
        "horizons": tuple(int(x) for x in raw.get("horizons_days", [])) == HORIZONS_DAYS,
        "require_A": completeness.get("require_every_daily_A_mark") is True,
        "require_B": completeness.get("require_every_daily_B_mark") is True,
        "same_date": completeness.get("require_A_B_same_date") is True,
        "B_links_A": completeness.get("require_B_mark_links_to_same_date_A_mark") is True,
        "incomplete_waits": completeness.get("incomplete_horizon_policy")
        == "do_not_materialize_yet",
    }
    return {
        "protocol": PROTOCOL,
        "audit_level": "STRICT_CONFIG_IMPLEMENTATION_MATCH",
        "ok": all(checks.values()),
        "checks": checks,
    }


def _write_immutable(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError(f"Outcome linkage immutable record failed seal verification: {path}")
        return existing
    sealed = seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return sealed


def freeze_outcome_linkage(data_root: Path, *, now: Any | None = None) -> dict[str, Any]:
    path = freeze_path(data_root)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError("existing Outcome Linkage freeze failed seal verification")
        return {**existing, "status": "already_frozen"}
    config = config_consistency_report()
    if not config["ok"]:
        raise ValueError(f"Outcome Linkage config mismatch: {config}")
    references: dict[str, dict[str, str]] = {}
    for name, ref in reference_paths(data_root).items():
        if not ref.exists():
            raise FileNotFoundError(f"Outcome Linkage requires predecessor freeze: {ref}")
        references[name] = {"path": str(ref), "file_sha256": sha256_file(ref)}
    frozen_at = _utc(now or datetime.now(timezone.utc))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL,
        "mode": MODE,
        "maturity": "DATA_LINKAGE_ONLY",
        "actionability": "NONE",
        "risk_multiplier": None,
        "frozen_at": frozen_at.isoformat(),
        "source_layers": list(SOURCE_LAYERS),
        "horizons_days": list(HORIZONS_DAYS),
        "anchor_granularity": "one_per_source_per_utc_day",
        "anchor_selection": "latest_known_at_within_source_utc_day",
        "outcome_start": "next_full_utc_day_after_anchor_known_at_date",
        "same_day_outcome_allowed": False,
        "selective_linking_allowed": False,
        "deterministic_late_materialization_allowed": True,
        "parameter_optimization_allowed": False,
        "automatic_actionability_allowed": False,
        "implementation_file_sha256": implementation_hashes(),
        "reference_freezes": references,
    }
    written = _write_immutable(path, payload)
    return {**written, "status": "frozen"}


def load_freeze(data_root: Path) -> dict[str, Any]:
    path = freeze_path(data_root)
    if not path.exists():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not verify_seal(value):
        raise ValueError("Outcome Linkage freeze failed seal verification")
    return value


def hashes_unchanged(data_root: Path, freeze: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    expected_impl = freeze.get("implementation_file_sha256", {})
    current_impl = implementation_hashes()
    impl = {
        name: {
            "expected": expected_impl.get(name),
            "current": current_impl.get(name),
            "unchanged": expected_impl.get(name) == current_impl.get(name),
        }
        for name in sorted(set(expected_impl) | set(current_impl))
    }
    refs: dict[str, Any] = {}
    for name, path in reference_paths(data_root).items():
        current = sha256_file(path) if path.exists() else None
        expected = freeze.get("reference_freezes", {}).get(name, {}).get("file_sha256")
        refs[name] = {"expected": expected, "current": current, "unchanged": expected == current}
    ok = all(row["unchanged"] for row in impl.values()) and all(
        row["unchanged"] for row in refs.values()
    )
    return ok, {"implementation": impl, "references": refs}
