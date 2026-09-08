from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from crossalpha.state.v03_prospective import freeze_state_v03, verify_seal  # noqa: E402
from verify_rust_canonical_parser_parity import _diff, _normalize  # noqa: E402

FIXED_NOW = "2026-09-06T00:00:00.123456+00:00"
MINIMUM_BLOCK = 23_000_000


def _seed_reference_freezes(data_root: Path) -> None:
    payloads = {
        data_root / "research/free_v01/paper/freeze.json": b'{"fixture":"b3"}\n',
        data_root / "research/free_v01/state_ab_v01/freeze.json": b'{"fixture":"ab"}\n',
        data_root / "research/state_v02/freeze.json": b'{"fixture":"v02"}\n',
    }
    for path, payload in payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _run_rust(binary: Path, data_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(binary),
            str(data_root),
            "--minimum-eligible-block",
            str(MINIMUM_BLOCK),
            "--now",
            FIXED_NOW,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Rust State V0.3 freeze preview failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid Rust freeze JSON: {exc}; stdout={completed.stdout[:500]!r}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare Python State V0.3 legacy V1 freeze payload/seal with Rust preview "
            "using isolated deterministic predecessor fixtures."
        )
    )
    parser.add_argument(
        "--rust-binary",
        type=Path,
        default=(
            REPO_ROOT
            / "target"
            / "debug"
            / "crossalpha-state-v03-freeze-preview-rs"
        ),
    )
    args = parser.parse_args()

    if not args.rust_binary.exists():
        print(f"ok=false mismatches=1 error=Rust binary missing: {args.rust_binary}")
        return 1

    with tempfile.TemporaryDirectory(prefix="crossalpha-state-v03-freeze-") as tmp:
        data_root = Path(tmp)
        _seed_reference_freezes(data_root)
        python = freeze_state_v03(
            data_root,
            minimum_eligible_block=MINIMUM_BLOCK,
            now=FIXED_NOW,
        )
        expected = dict(python)
        expected.pop("status", None)
        if not verify_seal(expected):
            raise RuntimeError("Python fixture freeze seal failed")
        actual = _run_rust(args.rust_binary, data_root)
        mismatches = _diff(
            _normalize(expected),
            _normalize(actual),
            "$.state_v03_freeze",
            limit=100,
        )
        print(
            "ok={} mismatches={} implementation_hashes={} references={} seal_match={}".format(
                str(not mismatches).lower(),
                len(mismatches),
                len(expected.get("implementation_file_sha256", {})),
                len(expected.get("reference_freezes", {})),
                str(
                    expected.get("record_sha256") == actual.get("record_sha256")
                ).lower(),
            )
        )
        for mismatch in mismatches:
            print(f"mismatch={mismatch}")
        return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
