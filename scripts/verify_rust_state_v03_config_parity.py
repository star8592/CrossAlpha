from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from crossalpha.state.v03_config import strict_v03_config_report  # noqa: E402
from verify_rust_canonical_parser_parity import _diff, _normalize  # noqa: E402


def _run_rust(binary: Path, config: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(binary), "state-v03-config-check", str(config)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in (0, 2):
        raise RuntimeError(
            "Rust state-v03-config-check failed unexpectedly: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"invalid Rust State V0.3 config JSON: {exc}; stdout={completed.stdout[:500]!r}"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare Python/Rust State V0.3 strict config audit reports."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "config" / "state_v03.yaml",
    )
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
    if "state-v03-config-check" not in help_result.stdout:
        print(
            "ok=false mismatches=1 error=Rust binary is stale and lacks "
            "state-v03-config-check; run cargo build -p crossalpha-cli"
        )
        return 1

    expected = _normalize(strict_v03_config_report(args.config))
    actual = _normalize(_run_rust(args.rust_binary, args.config))
    mismatches = _diff(expected, actual, "$.state_v03_config", limit=100)
    checks = expected.get("checks", {})
    passed = sum(bool(value) for value in checks.values())
    print(
        "ok={} mismatches={} checks={} passed={} python_ok={} rust_ok={}".format(
            str(not mismatches).lower(),
            len(mismatches),
            len(checks),
            passed,
            str(bool(expected.get("ok"))).lower(),
            str(bool(actual.get("ok"))).lower(),
        )
    )
    for mismatch in mismatches:
        print(f"mismatch={mismatch}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
