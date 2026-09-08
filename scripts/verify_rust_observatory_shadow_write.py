#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_KEYS = {
    ("hyperliquid", "metaAndAssetCtxs"),
    ("hyperliquid", "allMids"),
    ("defillama", "stablecoins_snapshot"),
}


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _require_command(binary: Path, command: str) -> str | None:
    if not binary.exists():
        return f"Rust binary missing: {binary}; run cargo build -p crossalpha-cli first"
    help_result = _run([str(binary), "--help"])
    if help_result.returncode != 0:
        return f"Rust binary --help failed: {help_result.stderr.strip()}"
    if command not in help_result.stdout:
        return (
            f"Rust binary is stale and does not contain {command!r}; "
            "run cargo build -p crossalpha-cli first"
        )
    return None


def _parse_json_lines(raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"line {line_no} is not a JSON object")
        records.append(value)
    return records


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return _parse_json_lines(path.read_text(encoding="utf-8"))


def _verify_snapshot(
    record: dict[str, Any],
    data_root: Path,
    errors: list[str],
) -> tuple[str | None, str | None]:
    raw_path = record.get("path")
    if not isinstance(raw_path, str):
        errors.append("manifest record path is missing or not a string")
        return None, None

    path = Path(raw_path)
    try:
        path.relative_to(data_root)
    except ValueError:
        errors.append(f"snapshot escaped shadow root: {path}")
        return None, None

    if not path.exists():
        errors.append(f"snapshot missing: {path}")
        return None, None

    try:
        with gzip.open(path, "rb") as fh:
            payload = fh.read()
    except Exception as exc:  # noqa: BLE001 - verifier must report corruption cleanly.
        errors.append(f"snapshot gzip read failed {path}: {exc}")
        return None, None

    digest = hashlib.sha256(payload).hexdigest()
    expected_digest = record.get("sha256")
    if digest != expected_digest:
        errors.append(f"sha256 mismatch: {path}")

    expected_bytes = record.get("bytes")
    if len(payload) != expected_bytes:
        errors.append(
            f"uncompressed byte mismatch: {path} manifest={expected_bytes} actual={len(payload)}"
        )

    compressed_bytes = path.stat().st_size
    expected_compressed = record.get("compressed_bytes")
    if expected_compressed is not None and compressed_bytes != expected_compressed:
        errors.append(
            f"compressed byte mismatch: {path} manifest={expected_compressed} actual={compressed_bytes}"
        )

    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError as exc:
        errors.append(f"snapshot payload is not JSON {path}: {exc}")
        return None, None

    if not isinstance(envelope, dict):
        errors.append(f"snapshot envelope is not an object: {path}")
        return None, None

    source_id = envelope.get("source_id")
    observation_type = envelope.get("observation_type")
    key = (source_id, observation_type)
    if key not in EXPECTED_KEYS:
        errors.append(f"unexpected snapshot key: {key!r}")

    if record.get("source_id") != source_id:
        errors.append(f"manifest/envelope source mismatch: {path}")
    if record.get("observation_type") != observation_type:
        errors.append(f"manifest/envelope observation mismatch: {path}")

    return source_id if isinstance(source_id, str) else None, (
        observation_type if isinstance(observation_type, str) else None
    )


def _verify_health(
    binary: Path,
    data_root: Path,
    command: str,
    errors: list[str],
) -> dict[str, Any] | None:
    result = _run([str(binary), command, str(data_root), "--no-write-report"])
    if result.returncode != 0:
        errors.append(f"{command} failed: {result.stderr.strip()}")
        return None
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        errors.append(f"{command} returned invalid JSON: {exc}")
        return None
    if not isinstance(report, dict):
        errors.append(f"{command} report is not an object")
        return None
    if report.get("ok") is not True:
        errors.append(f"{command} reported ok=false")
    if report.get("manifest_records") != 3:
        errors.append(
            f"{command} manifest_records={report.get('manifest_records')!r}, expected 3"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Perform one real Rust Observatory write cycle in an isolated temporary data root, "
            "then verify the full storage and health contracts."
        )
    )
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    for command in ("observatory-collect", "observatory-health", "observatory-live-health"):
        error = _require_command(args.rust_binary, command)
        if error:
            print("ok=false manifests=0 snapshots=0 errors=1")
            print(f"error={error}")
            return 1

    errors: list[str] = []
    snapshots_verified = 0

    with tempfile.TemporaryDirectory(prefix="crossalpha-rust-shadow-write-") as tmp:
        data_root = Path(tmp).resolve()
        collect = _run(
            [
                str(args.rust_binary),
                "observatory-collect",
                str(data_root),
                "--source",
                "hyperliquid",
                "--source",
                "defillama",
                "--timeout",
                str(args.timeout),
            ]
        )
        if collect.returncode != 0:
            print("ok=false manifests=0 snapshots=0 errors=1")
            print(f"error=shadow collector failed: {collect.stderr.strip()}")
            return 1

        try:
            stdout_manifests = _parse_json_lines(collect.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            print("ok=false manifests=0 snapshots=0 errors=1")
            print(f"error=invalid shadow collector JSONL: {exc}")
            return 1

        if len(stdout_manifests) != 3:
            errors.append(f"collector emitted {len(stdout_manifests)} manifests, expected 3")

        audit_path = data_root / "manifests" / "raw_snapshots.jsonl"
        if not audit_path.exists():
            errors.append("audit manifest was not created")
            audit_records: list[dict[str, Any]] = []
        else:
            try:
                audit_records = _read_jsonl(audit_path)
            except (json.JSONDecodeError, ValueError) as exc:
                errors.append(f"audit manifest is invalid JSONL: {exc}")
                audit_records = []

        if len(audit_records) != 3:
            errors.append(f"audit manifest has {len(audit_records)} records, expected 3")

        keys: set[tuple[str | None, str | None]] = set()
        for record in audit_records:
            source_id, observation_type = _verify_snapshot(record, data_root, errors)
            keys.add((source_id, observation_type))
            snapshots_verified += 1

        if keys != EXPECTED_KEYS:
            errors.append(f"shadow snapshot key set mismatch: {sorted(keys)!r}")

        daily_files = list((data_root / "manifests" / "daily").rglob("raw_snapshots.jsonl"))
        if len(daily_files) != 1:
            errors.append(f"expected exactly 1 daily manifest file, got {len(daily_files)}")
        elif len(_read_jsonl(daily_files[0])) != 3:
            errors.append("daily manifest does not contain exactly 3 records")

        for source_id, observation_type in EXPECTED_KEYS:
            state_path = (
                data_root
                / "manifests"
                / "series"
                / source_id
                / f"{observation_type}.json"
            )
            if not state_path.exists():
                errors.append(f"series state missing: {source_id}/{observation_type}")
                continue
            state = _read_json(state_path)
            if not isinstance(state, dict):
                errors.append(f"series state is not an object: {source_id}/{observation_type}")
                continue
            if state.get("count") != 1:
                errors.append(
                    f"series state count={state.get('count')!r}, expected 1: "
                    f"{source_id}/{observation_type}"
                )
            latest = state.get("latest_manifest")
            if not isinstance(latest, dict):
                errors.append(
                    f"series latest_manifest missing: {source_id}/{observation_type}"
                )

        full_health = _verify_health(
            args.rust_binary, data_root, "observatory-health", errors
        )
        live_health = _verify_health(
            args.rust_binary, data_root, "observatory-live-health", errors
        )
        if live_health is not None and live_health.get("mode") != "series_state":
            errors.append(
                f"live health mode={live_health.get('mode')!r}, expected 'series_state'"
            )

        if full_health is not None:
            for label, report in (full_health.get("series") or {}).items():
                if isinstance(report, dict) and report.get("count") != 1:
                    errors.append(f"full health count != 1 for {label}")

        if live_health is not None:
            for label, report in (live_health.get("series") or {}).items():
                if isinstance(report, dict) and report.get("count") != 1:
                    errors.append(f"live health count != 1 for {label}")

    print(
        "ok={} manifests={} snapshots={} full_health={} live_health={} errors={}".format(
            str(not errors).lower(),
            len(stdout_manifests),
            snapshots_verified,
            str(bool(full_health and full_health.get("ok"))).lower(),
            str(bool(live_health and live_health.get("ok"))).lower(),
            len(errors),
        )
    )
    for error in errors:
        print(f"error={error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
