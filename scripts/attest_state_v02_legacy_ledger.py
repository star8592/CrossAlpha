from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crossalpha.state.v02_integrity import strict_state_v02_integrity_report

PROTOCOL = "CROSSALPHA_STATE_V0_2_PYTHON_LEGACY_INTEGRITY_ATTESTATION"
SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ledger_snapshot(data_root: Path) -> dict[str, Any]:
    prospective = data_root / "research" / "state_v02" / "prospective"
    files = sorted(prospective.glob("year=*/month=*/day=*/state_at=*.json")) if prospective.exists() else []
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(data_root).as_posix()
        file_hash = sha256_file(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "observation_count": len(files),
        "ledger_root_sha256": digest.hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    data_root = args.data_root.resolve()

    integrity = strict_state_v02_integrity_report(data_root)
    if integrity.get("ok") is not True:
        raise SystemExit(
            "V02 legacy attestation refused: frozen Python strict integrity is not green\n"
            + json.dumps(integrity, ensure_ascii=False, indent=2, default=str)
        )

    freeze = data_root / "research" / "state_v02" / "freeze.json"
    if not freeze.is_file():
        raise SystemExit(f"V02 legacy attestation refused: freeze missing: {freeze}")

    snapshot = ledger_snapshot(data_root)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verifier": "crossalpha.state.v02_integrity.strict_state_v02_integrity_report",
        "python_integrity_ok": True,
        "freeze_file_sha256": sha256_file(freeze),
        **snapshot,
    }
    output = data_root / "research" / "state_v02" / "legacy_python_integrity_attestation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    tmp.replace(output)
    print(json.dumps({"ok": True, "output": str(output), **payload}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
