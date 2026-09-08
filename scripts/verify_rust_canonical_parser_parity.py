from __future__ import annotations

import argparse
import gzip
import json
import math
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crossalpha.domain.models import RawSnapshotManifest  # noqa: E402
from crossalpha.observatory.canonical.hyperliquid import (  # noqa: E402
    parse_meta_and_asset_contexts,
)
from crossalpha.observatory.canonical.stablecoins import (  # noqa: E402
    CANONICAL_SCHEMA_VERSION,
    parse_stablecoin_snapshot,
)
from crossalpha.storage.indexes import manifest_lock  # noqa: E402


def _load_audit_under_lock(data_root: Path) -> list[RawSnapshotManifest]:
    audit = data_root / "manifests" / "raw_snapshots.jsonl"
    records: list[RawSnapshotManifest] = []
    with manifest_lock(data_root):
        for line in audit.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(RawSnapshotManifest.model_validate_json(line))
    return records


def _freeze_latest(data_root: Path, frozen_root: Path) -> dict[str, RawSnapshotManifest]:
    records = _load_audit_under_lock(data_root)
    wanted = {
        "hyperliquid": ("hyperliquid", "metaAndAssetCtxs"),
        "stablecoins": ("defillama", "stablecoins_snapshot"),
    }
    selected: dict[str, RawSnapshotManifest] = {}
    for label, (source_id, observation_type) in wanted.items():
        matches = [
            record
            for record in records
            if record.source_id == source_id and record.observation_type == observation_type
        ]
        if not matches:
            raise RuntimeError(f"no raw records for {source_id}/{observation_type}")
        selected[label] = max(matches, key=lambda record: record.observed_at)

    frozen_raw = frozen_root / "frozen_raw"
    frozen_raw.mkdir(parents=True, exist_ok=True)
    frozen_manifests = frozen_root / "manifests"
    frozen_manifests.mkdir(parents=True, exist_ok=True)
    frozen_records: dict[str, RawSnapshotManifest] = {}
    lines: list[str] = []
    for label, record in selected.items():
        source = Path(record.path)
        destination = frozen_raw / f"{label}_{source.name}"
        shutil.copy2(source, destination)
        frozen = record.model_copy(update={"path": str(destination)})
        frozen_records[label] = frozen
        lines.append(frozen.model_dump_json())
    (frozen_manifests / "raw_snapshots.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return frozen_records


def _load_envelope(record: RawSnapshotManifest) -> dict[str, Any]:
    with gzip.open(record.path, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _normalize_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        if isinstance(value, str) and (value.endswith("Z") or value.endswith("+00:00")):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
            except ValueError:
                pass
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    if hasattr(value, "item"):
        return _normalize_scalar(value.item())
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return _normalize_scalar(value)


def _frame_records(frame: Any) -> list[dict[str, Any]]:
    return [_normalize(record) for record in frame.to_dict(orient="records")]


def _python_expected(
    frozen: dict[str, RawSnapshotManifest],
) -> dict[str, dict[str, Any]]:
    hyper_record = frozen["hyperliquid"]
    hyper_frame = parse_meta_and_asset_contexts(_load_envelope(hyper_record), hyper_record)
    hyper_rows = sorted(_frame_records(hyper_frame), key=lambda row: str(row.get("asset")))

    stable_record = frozen["stablecoins"]
    asset_frame, chain_frame = parse_stablecoin_snapshot(
        _load_envelope(stable_record), stable_record
    )
    asset_rows = sorted(
        _frame_records(asset_frame), key=lambda row: str(row.get("stablecoin_id"))
    )
    chain_rows = sorted(
        _frame_records(chain_frame),
        key=lambda row: (str(row.get("stablecoin_id")), str(row.get("chain"))),
    )
    return {
        "hyperliquid": {
            "source": "hyperliquid",
            "raw_sha256": hyper_record.sha256,
            "rows": hyper_rows,
        },
        "stablecoins": {
            "source": "stablecoins",
            "raw_sha256": stable_record.sha256,
            "canonical_schema_version": CANONICAL_SCHEMA_VERSION,
            "assets": asset_rows,
            "chains": chain_rows,
        },
    }


def _run_rust(binary: Path, frozen_root: Path, source: str) -> dict[str, Any]:
    completed = subprocess.run(
        [str(binary), "canonical-preview", str(frozen_root), "--source", source],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Rust canonical-preview failed for {source}: {completed.stderr.strip()}"
        )
    try:
        return _normalize(json.loads(completed.stdout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid Rust canonical JSON for {source}: {exc}; stdout={completed.stdout[:500]!r}"
        ) from exc


def _sort_rust_rows(report: dict[str, Any], source: str) -> None:
    if source == "hyperliquid":
        report["rows"] = sorted(
            report.get("rows", []), key=lambda row: str(row.get("asset"))
        )
    else:
        report["assets"] = sorted(
            report.get("assets", []), key=lambda row: str(row.get("stablecoin_id"))
        )
        report["chains"] = sorted(
            report.get("chains", []),
            key=lambda row: (str(row.get("stablecoin_id")), str(row.get("chain"))),
        )


def _diff(expected: Any, actual: Any, path: str = "$", limit: int = 50) -> list[str]:
    mismatches: list[str] = []

    def walk(left: Any, right: Any, current: str) -> None:
        if len(mismatches) >= limit:
            return
        if isinstance(left, bool) or isinstance(right, bool):
            if left != right or type(left) is not type(right):
                mismatches.append(f"{current}: python={left!r} rust={right!r}")
            return
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            left_value = float(left)
            right_value = float(right)
            tolerance = 1e-9 * max(1.0, abs(left_value), abs(right_value))
            if abs(left_value - right_value) > tolerance:
                mismatches.append(f"{current}: python={left!r} rust={right!r}")
            return
        if isinstance(left, dict) and isinstance(right, dict):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys):
                mismatches.append(f"{current}.{key}: missing from Rust")
                if len(mismatches) >= limit:
                    return
            for key in sorted(right_keys - left_keys):
                mismatches.append(f"{current}.{key}: unexpected in Rust")
                if len(mismatches) >= limit:
                    return
            for key in sorted(left_keys & right_keys):
                walk(left[key], right[key], f"{current}.{key}")
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                mismatches.append(
                    f"{current}: list length python={len(left)} rust={len(right)}"
                )
                return
            for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
                walk(left_item, right_item, f"{current}[{index}]")
            return
        if left != right or type(left) is not type(right):
            mismatches.append(f"{current}: python={left!r} rust={right!r}")

    walk(expected, actual, path)
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Python/Rust canonical parsers on frozen real raw snapshots."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    args = parser.parse_args()

    if not args.rust_binary.exists():
        print(f"ok=false mismatches=1 error=Rust binary missing: {args.rust_binary}")
        return 1

    help_result = subprocess.run(
        [str(args.rust_binary), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if "canonical-preview" not in help_result.stdout:
        print(
            "ok=false mismatches=1 error=Rust binary is stale and lacks canonical-preview; "
            "run cargo build -p crossalpha-cli"
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="crossalpha-canonical-parity-") as tmp:
        frozen_root = Path(tmp)
        frozen = _freeze_latest(args.data_root, frozen_root)
        expected = _python_expected(frozen)
        actual = {
            source: _run_rust(args.rust_binary, frozen_root, source)
            for source in ("hyperliquid", "stablecoins")
        }
        for source, report in actual.items():
            _sort_rust_rows(report, source)

        mismatches: list[str] = []
        for source in ("hyperliquid", "stablecoins"):
            mismatches.extend(
                _diff(expected[source], actual[source], f"$.{source}", limit=50)
            )
            if len(mismatches) >= 50:
                break

        print(
            "ok={} mismatches={} hyperliquid_rows={} stablecoin_assets={} stablecoin_chains={}".format(
                str(not mismatches).lower(),
                len(mismatches),
                len(expected["hyperliquid"]["rows"]),
                len(expected["stablecoins"]["assets"]),
                len(expected["stablecoins"]["chains"]),
            )
        )
        for mismatch in mismatches:
            print(f"mismatch={mismatch}")
        return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
