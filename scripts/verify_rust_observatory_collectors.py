from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    ("hyperliquid", "metaAndAssetCtxs"): {
        "source_type": "EXCHANGE",
        "endpoint": "https://api.hyperliquid.xyz/info",
        "payload_type": list,
    },
    ("hyperliquid", "allMids"): {
        "source_type": "EXCHANGE",
        "endpoint": "https://api.hyperliquid.xyz/info",
        "payload_type": dict,
    },
    ("defillama", "stablecoins_snapshot"): {
        "source_type": "AGGREGATOR",
        "endpoint": "https://stablecoins.llama.fi/stablecoins?includePrices=true",
        "payload_type": dict,
    },
}


def _require_subcommand(binary: Path, subcommand: str) -> str | None:
    if not binary.exists():
        return f"Rust binary missing: {binary}; run cargo build -p crossalpha-cli"
    completed = subprocess.run(
        [str(binary), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return f"Rust binary --help failed: {completed.stderr.strip()}"
    if subcommand not in completed.stdout:
        return (
            f"Rust binary is stale and does not expose {subcommand!r}; "
            "fix cargo build errors and rebuild crossalpha-cli"
        )
    return None


def _validate_envelope(envelope: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    key = (envelope.get("source_id"), envelope.get("observation_type"))
    expected = EXPECTED.get(key)
    if expected is None:
        return [f"unexpected envelope: {key}"]

    if envelope.get("schema_version") != 1:
        errors.append(f"{key}: schema_version != 1")
    if envelope.get("event_time") is not None:
        errors.append(f"{key}: event_time should be null")
    if envelope.get("observed_at") != envelope.get("known_at"):
        errors.append(f"{key}: observed_at != known_at")
    if envelope.get("source_type") != expected["source_type"]:
        errors.append(
            f"{key}: source_type={envelope.get('source_type')!r} "
            f"expected={expected['source_type']!r}"
        )

    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        errors.append(f"{key}: metadata is not an object")
    elif metadata.get("endpoint") != expected["endpoint"]:
        errors.append(f"{key}: endpoint metadata mismatch")

    payload = envelope.get("payload")
    if not isinstance(payload, expected["payload_type"]):
        errors.append(
            f"{key}: payload type={type(payload).__name__} "
            f"expected={expected['payload_type'].__name__}"
        )

    if key[0] == "hyperliquid":
        request = metadata.get("request") if isinstance(metadata, dict) else None
        if request != {"type": key[1]}:
            errors.append(f"{key}: request metadata mismatch")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run real Rust Observatory provider requests without writing data."
    )
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=REPO_ROOT / "target" / "debug" / "crossalpha-rs",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    binary_error = _require_subcommand(args.rust_binary, "observatory-collect")
    if binary_error:
        print("ok=false envelopes=0 errors=1")
        print(f"error={binary_error}")
        return 1

    with tempfile.TemporaryDirectory(prefix="crossalpha-rust-collector-") as tmp:
        data_root = Path(tmp)
        command = [
            str(args.rust_binary),
            "observatory-collect",
            str(data_root),
            "--source",
            "hyperliquid",
            "--source",
            "defillama",
            "--timeout",
            str(args.timeout),
            "--dry-run",
        ]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        if completed.returncode != 0:
            print("ok=false envelopes=0 errors=1")
            print(f"error=collector command failed: {completed.stderr.strip()}")
            return 1

        try:
            envelopes = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            print("ok=false envelopes=0 errors=1")
            print(f"error=invalid collector JSON: {exc}")
            return 1

        errors: list[str] = []
        if not isinstance(envelopes, list):
            errors.append("collector output is not a list")
            envelopes = []
        if len(envelopes) != 3:
            errors.append(f"expected 3 envelopes, got {len(envelopes)}")

        keys = {
            (item.get("source_id"), item.get("observation_type"))
            for item in envelopes
            if isinstance(item, dict)
        }
        if keys != set(EXPECTED):
            errors.append(f"envelope key set mismatch: {sorted(keys)!r}")

        for envelope in envelopes:
            if not isinstance(envelope, dict):
                errors.append("non-object envelope returned")
                continue
            errors.extend(_validate_envelope(envelope))

        hyperliquid_times = {
            item.get("observed_at")
            for item in envelopes
            if isinstance(item, dict) and item.get("source_id") == "hyperliquid"
        }
        if len(hyperliquid_times) != 1:
            errors.append(
                "Hyperliquid metaAndAssetCtxs/allMids do not share one collection timestamp"
            )

        if (data_root / "raw").exists() or (data_root / "manifests").exists():
            errors.append("dry-run unexpectedly wrote raw/manifests data")

        print(
            "ok={} envelopes={} errors={}".format(
                str(not errors).lower(),
                len(envelopes),
                len(errors),
            )
        )
        for error in errors:
            print(f"error={error}")
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
