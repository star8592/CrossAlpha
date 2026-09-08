from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str], *, timeout: int = 600) -> None:
    print(f"\n===== {label} =====", flush=True)
    print("$ " + " ".join(command), flush=True)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        env={**os.environ, "CROSSALPHA_CLOUD_CI": "1"},
        timeout=timeout,
        check=False,
    )
    elapsed = time.monotonic() - started
    print(f"===== {label} rc={completed.returncode} elapsed={elapsed:.2f}s =====", flush=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def main() -> int:
    python = sys.executable
    checks: list[tuple[str, list[str]]] = [
        (
            "raw-envelope-v2",
            [python, "scripts/verify_rust_raw_envelope_v2.py"],
        ),
        (
            "free-core",
            [python, "scripts/verify_rust_free_core_parity.py"],
        ),
        (
            "research-kernel",
            [python, "scripts/verify_rust_research_kernel_parity.py"],
        ),
        (
            "state-kernel",
            [python, "scripts/verify_rust_state_kernel_parity.py"],
        ),
        (
            "state-v02",
            [python, "scripts/verify_rust_state_v02_parity.py"],
        ),
        (
            "state-v03-config",
            [python, "scripts/verify_rust_state_v03_config_parity.py"],
        ),
        (
            "state-v04",
            [
                python,
                "scripts/verify_rust_state_v04_parity.py",
                "--config-binary",
                "target/debug/crossalpha-state-v04-config-check-rs",
                "--parse-binary",
                "target/debug/crossalpha-state-v04-parse-rs",
                "--mechanics-binary",
                "target/debug/crossalpha-state-v04-mechanics-rs",
            ],
        ),
        (
            "paper-ab",
            [python, "scripts/verify_rust_paper_ab_parity.py"],
        ),
    ]

    print("CrossAlpha deterministic cloud parity", flush=True)
    print("===================================", flush=True)
    print("network_credentials=false", flush=True)
    print("production_data=false", flush=True)
    print("production_writes=false", flush=True)
    print(f"checks={len(checks)}", flush=True)

    for label, command in checks:
        run(label, command)

    print(f"\nok=true checks={len(checks)} cloud_safe=true", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
