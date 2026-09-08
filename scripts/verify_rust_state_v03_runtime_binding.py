from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crossalpha.state.v03_prospective import freeze_state_v03  # noqa: E402
from verify_rust_state_v03_freeze_parity import (  # noqa: E402
    FIXED_NOW,
    MINIMUM_BLOCK,
    _seed_reference_freezes,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _payload_hash(value: dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("record_sha256", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_rust(binary: Path, data_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(binary), str(data_root), "--now", FIXED_NOW],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Rust runtime-binding preview failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    return json.loads(completed.stdout)


def _cargo_lock_tracked() -> bool:
    completed = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "Cargo.lock"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate native State V0.3 runtime binding and source-lock safety."
    )
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=(
            REPO_ROOT
            / "target"
            / "debug"
            / "crossalpha-state-v03-runtime-binding-preview-rs"
        ),
    )
    args = parser.parse_args()
    if not args.rust_binary.exists():
        print(f"ok=false errors=1 error=Rust binary missing: {args.rust_binary}")
        return 1

    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="crossalpha-state-v03-binding-") as tmp:
        data_root = Path(tmp)
        _seed_reference_freezes(data_root)
        freeze_state_v03(
            data_root,
            minimum_eligible_block=MINIMUM_BLOCK,
            now=FIXED_NOW,
        )
        legacy_path = data_root / "research/state_v03/freeze.json"
        binding = _run_rust(args.rust_binary, data_root)

        if binding.get("protocol") != "CROSSALPHA_STATE_V0_3_RUST_RUNTIME_BINDING":
            errors.append("runtime binding protocol mismatch")
        if binding.get("runtime") != "RUST_TOKIO":
            errors.append("runtime binding is not RUST_TOKIO")
        if binding.get("python_runtime_required") is not False:
            errors.append("runtime binding unexpectedly requires Python")
        if binding.get("record_sha256") != _payload_hash(binding):
            errors.append("runtime binding seal mismatch")
        legacy = binding.get("legacy_freeze", {})
        if legacy.get("file_sha256") != _sha256(legacy_path):
            errors.append("legacy freeze file hash mismatch")
        if len(binding.get("native_source_sha256", {})) < 10:
            errors.append("native source hash graph is incomplete")

        lock_present = (REPO_ROOT / "Cargo.lock").exists()
        lock_tracked = _cargo_lock_tracked()
        if bool(binding.get("cargo_lock_present")) != lock_present:
            errors.append("cargo_lock_present mismatch")
        if bool(binding.get("cargo_lock_tracked")) != lock_tracked:
            errors.append("cargo_lock_tracked mismatch")
        expected_eligible = lock_present and lock_tracked
        if bool(binding.get("production_binding_eligible")) != expected_eligible:
            errors.append("production_binding_eligible mismatch")

        print(
            "ok={} errors={} source_hashes={} cargo_lock_present={} "
            "cargo_lock_tracked={} production_binding_eligible={}".format(
                str(not errors).lower(),
                len(errors),
                len(binding.get("native_source_sha256", {})),
                str(lock_present).lower(),
                str(lock_tracked).lower(),
                str(expected_eligible).lower(),
            )
        )
        for error in errors:
            print(f"error={error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
