from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "target" / "debug" / "crossalpha-raw-envelope-fixture-rs"


def canonical_v2_bytes(value: dict[str, object]) -> bytes:
    if value.get("schema_version") != 2:
        raise ValueError("fixture must use raw envelope schema v2")
    # V2 contract: UTF-8 compact JSON with recursive lexicographic object-key order.
    # Datetime fields are already represented in the contract's fixed six-microsecond UTC-Z form.
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def main() -> int:
    if not BINARY.exists():
        raise SystemExit(f"missing Rust fixture binary: {BINARY}")

    fixture = {
        "schema_version": 2,
        "event_time": "2026-09-06T12:34:55.654321Z",
        "observed_at": "2026-09-06T12:34:56.123456Z",
        "known_at": "2026-09-06T12:34:56.223456Z",
        "source_type": "EXCHANGE",
        "source_id": "fixture:canonical",
        "observation_type": "raw_envelope_v2_golden",
        "payload": {
            "z_outer": {"z": 2, "a": 1},
            "a_array": [{"β": "值", "b": True, "a": None}, 1.25],
        },
        "metadata": {
            "z_endpoint": "https://example.invalid/v2",
            "a_flags": {"z": False, "a": True},
        },
    }
    # Deliberately construct an insertion-order variant. The V2 bytes must be identical.
    variant = {
        "metadata": {
            "a_flags": {"a": True, "z": False},
            "z_endpoint": "https://example.invalid/v2",
        },
        "payload": {
            "a_array": [{"a": None, "b": True, "β": "值"}, 1.25],
            "z_outer": {"a": 1, "z": 2},
        },
        "observation_type": "raw_envelope_v2_golden",
        "source_id": "fixture:canonical",
        "source_type": "EXCHANGE",
        "known_at": "2026-09-06T12:34:56.223456Z",
        "observed_at": "2026-09-06T12:34:56.123456Z",
        "event_time": "2026-09-06T12:34:55.654321Z",
        "schema_version": 2,
    }

    expected = canonical_v2_bytes(fixture)
    if canonical_v2_bytes(variant) != expected:
        raise AssertionError("Python canonical serializer is insertion-order dependent")

    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="crossalpha-raw-v2-") as tmp:
        root = Path(tmp)
        for index, value in enumerate((fixture, variant), start=1):
            path = root / f"fixture-{index}.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            completed = subprocess.run(
                [str(BINARY), str(path)],
                cwd=REPO,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout)
            output = json.loads(completed.stdout)
            actual = output["canonical_utf8"].encode("utf-8")
            if actual != expected:
                raise AssertionError(
                    "raw envelope v2 byte mismatch\n"
                    f"python={expected!r}\n"
                    f"rust={actual!r}"
                )
            if int(output["bytes"]) != len(expected):
                raise AssertionError("Rust raw envelope byte count mismatch")
            results.append(output)

    digest = hashlib.sha256(expected).hexdigest()
    print(
        "ok=true schema_version=2 bytes={} sha256={} variants={} canonical_order=recursive_lexicographic".format(
            len(expected), digest, len(results)
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
