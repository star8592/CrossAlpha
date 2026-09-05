from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.state import v04


PROSPECTIVE_PROTOCOL = "CROSSALPHA_STATE_V0_4_PROSPECTIVE"
FREEZE_SCHEMA_VERSION = 1
MAX_LIVE_WRITE_AGE_SECONDS = 180


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _research_root(data_root: Path) -> Path:
    return data_root / "research" / "state_v04"


def freeze_path(data_root: Path) -> Path:
    return _research_root(data_root) / "freeze.json"


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


def sha256_gzip_payload(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_files() -> dict[str, Path]:
    root = _repo_root()
    return {
        "state_v04": root / "src" / "crossalpha" / "state" / "v04.py",
        "state_v04_provider": root / "src" / "crossalpha" / "state" / "v04_provider.py",
        "state_v04_safe_provider": root
        / "src"
        / "crossalpha"
        / "state"
        / "v04_safe_provider.py",
        "state_v04_cycle": root / "src" / "crossalpha" / "state" / "v04_cycle.py",
        "state_v04_prospective": root / "src" / "crossalpha" / "state" / "v04_prospective.py",
        "state_v04_config": root / "src" / "crossalpha" / "state" / "v04_config.py",
        "config": root / "config" / "state_v04.yaml",
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
        "state_v03": data_root / "research" / "state_v03" / "freeze.json",
    }


def _write_immutable(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError(f"State V0.4 immutable record failed seal verification: {path}")
        return existing
    sealed = _seal(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sealed, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return sealed


def _record_path(data_root: Path, generated: pd.Timestamp) -> Path:
    return (
        _research_root(data_root)
        / "prospective"
        / f"year={generated:%Y}"
        / f"month={generated:%m}"
        / f"day={generated:%d}"
        / f"state_at={generated:%H%M%S%f}.json"
    )


def freeze_state_v04(data_root: Path, *, now: Any | None = None) -> dict[str, Any]:
    path = freeze_path(data_root)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError("existing State V0.4 freeze failed seal verification")
        return {**existing, "status": "already_frozen"}

    from crossalpha.state.v04_config import strict_v04_config_report

    config = strict_v04_config_report(_repo_root() / "config" / "state_v04.yaml")
    if not config.get("ok"):
        raise ValueError(f"State V0.4 config/implementation mismatch: {config}")
    references: dict[str, dict[str, str]] = {}
    for name, reference in _reference_paths(data_root).items():
        if not reference.exists():
            raise FileNotFoundError(
                f"State V0.4 requires frozen predecessor protocol before freeze: {reference}"
            )
        references[name] = {"path": str(reference), "file_sha256": sha256_file(reference)}

    frozen_at = _utc(now or datetime.now(timezone.utc))
    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v04.PROTOCOL,
        "mode": v04.MODE,
        "maturity": "O0_DATA_TO_O1_DESCRIPTION",
        "actionability": v04.ACTIONABILITY,
        "risk_multiplier": None,
        "frozen_at": frozen_at.isoformat(),
        "first_eligible_generated_at": frozen_at.isoformat(),
        "retrospective_backfill_allowed": False,
        "parameter_optimization_allowed": False,
        "automatic_actionability_allowed": False,
        "no_composite_stress_score": True,
        "implementation_file_sha256": implementation_hashes(),
        "reference_freezes": references,
        "promotion_gate": {
            "minimum_calendar_days_before_O2_candidate": 180,
            "minimum_observations": 500,
            "minimum_valid_venue_share": 0.95,
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
        raise ValueError("State V0.4 freeze failed seal verification")
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


def write_live_observation(
    data_root: Path,
    *,
    mechanics_path: Path,
    venue_path: Path,
    now: Any | None = None,
) -> dict[str, Any]:
    freeze = load_freeze(data_root)
    hashes_ok, details = hashes_unchanged(data_root, freeze)
    if not hashes_ok:
        raise RuntimeError(f"STATE_V04_HASH_GRAPH_MUTATED: {details}")
    if not mechanics_path.exists() or not venue_path.exists():
        raise FileNotFoundError("State V0.4 mechanics/venue artifact missing")

    mechanics = json.loads(mechanics_path.read_text(encoding="utf-8"))
    if mechanics.get("protocol") != v04.PROTOCOL:
        raise ValueError("mechanics artifact is not State V0.4")
    if mechanics.get("actionability") != v04.ACTIONABILITY or mechanics.get("risk_multiplier") is not None:
        raise ValueError("State V0.4 prospective ledger is descriptive only")
    if mechanics.get("no_composite_stress_score") is not True:
        raise ValueError("State V0.4 composite stress score is forbidden")
    if Path(str(mechanics.get("venue_snapshot_path", ""))) != venue_path:
        raise ValueError("mechanics artifact points to another venue snapshot")

    venues = pd.read_parquet(venue_path)
    if len(venues) != 6 or set(venues.get("asset", [])) != {"BTC", "ETH"}:
        raise ValueError("State V0.4 prospective venue snapshot must contain six BTC/ETH rows")
    if set(venues.get("venue", [])) != {"binance", "okx", "bybit"}:
        raise ValueError("State V0.4 prospective venue snapshot has incomplete venue universe")
    if venues.duplicated(["asset", "venue"]).any():
        raise ValueError("State V0.4 venue snapshot contains duplicate asset/venue rows")

    generated = _utc(mechanics["generated_at"])
    current = _utc(now or datetime.now(timezone.utc))
    frozen_at = _utc(freeze["frozen_at"])
    if generated < frozen_at:
        raise ValueError("State V0.4 prospective observation predates freeze")
    age = current - generated
    if age < pd.Timedelta(0) or age > pd.Timedelta(seconds=MAX_LIVE_WRITE_AGE_SECONDS):
        raise ValueError("State V0.4 live observation is stale; retrospective backfill refused")

    known_times = pd.to_datetime(venues["known_at"], utc=True, errors="coerce")
    observed_times = pd.to_datetime(venues["observed_at"], utc=True, errors="coerce")
    if known_times.isna().any() or observed_times.isna().any():
        raise ValueError("State V0.4 venue snapshot has invalid PTI timestamps")
    if (observed_times > known_times).any() or (known_times > generated).any():
        raise ValueError("State V0.4 PTI ordering violated")

    raw_links: list[dict[str, str]] = []
    raw_records = mechanics.get("raw_records")
    if not isinstance(raw_records, list) or len(raw_records) != 6:
        raise ValueError("State V0.4 mechanics artifact must link six raw records")
    for raw in raw_records:
        path = Path(str(raw.get("raw_path", "")))
        payload_hash = str(raw.get("raw_sha256", ""))
        compressed_hash = str(raw.get("raw_compressed_file_sha256", ""))
        if not path.exists():
            raise ValueError("State V0.4 raw snapshot file missing")
        if sha256_gzip_payload(path) != payload_hash:
            raise ValueError("State V0.4 raw uncompressed payload hash link failed")
        if sha256_file(path) != compressed_hash:
            raise ValueError("State V0.4 raw compressed-file hash link failed")
        raw_links.append(
            {
                "venue": str(raw.get("venue")),
                "asset": str(raw.get("asset")),
                "raw_path": str(path),
                "raw_sha256": payload_hash,
                "raw_compressed_file_sha256": compressed_hash,
            }
        )

    payload = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": v04.PROTOCOL,
        "freeze_record_sha256": freeze["record_sha256"],
        "known_at": current.isoformat(),
        "generated_at": generated.isoformat(),
        "mechanics_path": str(mechanics_path),
        "mechanics_sha256": sha256_file(mechanics_path),
        "venue_snapshot_path": str(venue_path),
        "venue_snapshot_sha256": sha256_file(venue_path),
        "raw_links": raw_links,
        "actionability": v04.ACTIONABILITY,
        "risk_multiplier": None,
        "no_composite_stress_score": True,
        "data_confidence": mechanics.get("data_confidence"),
        "funding_semantics": mechanics.get("funding_semantics"),
        "assets": mechanics.get("assets"),
    }
    path = _record_path(data_root, generated)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not verify_seal(existing):
            raise ValueError("existing State V0.4 record failed seal verification")
        if (
            existing.get("mechanics_sha256") != payload["mechanics_sha256"]
            or existing.get("venue_snapshot_sha256") != payload["venue_snapshot_sha256"]
        ):
            raise RuntimeError("STATE_V04_TIMESTAMP_COLLISION")
        return {**existing, "status": "already_exists", "output": str(path)}
    written = _write_immutable(path, payload)
    return {**written, "status": "written", "output": str(path)}
