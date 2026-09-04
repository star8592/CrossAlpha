#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

from crossalpha.observatory.health import observatory_health, write_health_report
from crossalpha.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--collector-timeout", type=int, default=120)
    parser.add_argument("--max-consecutive-failures", type=int, default=3)
    parser.add_argument("--stale-after", type=int, default=900)
    args = parser.parse_args()

    settings = Settings()
    settings.ensure_dirs()
    command = [sys.executable, "-m", "crossalpha.cli", "collect-observatory"]
    for source in args.source:
        command += ["--source", source]

    consecutive_failures = 0
    while True:
        cycle_started = time.monotonic()
        print(f"[{datetime.now(timezone.utc).isoformat()}] collecting...", flush=True)
        try:
            result = subprocess.run(command, check=False, timeout=max(args.collector_timeout, 1))
            returncode = result.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
            print(
                f"collector timed out after {args.collector_timeout}s",
                file=sys.stderr,
                flush=True,
            )

        if returncode != 0:
            consecutive_failures += 1
            print(
                f"collector failed rc={returncode} consecutive={consecutive_failures}",
                file=sys.stderr,
                flush=True,
            )
        else:
            consecutive_failures = 0

        health = observatory_health(
            settings.crossalpha_data_dir,
            expected_interval_seconds=args.interval,
            stale_after_seconds=max(args.stale_after, args.interval * 2),
            verify_latest=True,
        )
        health["collector_returncode"] = returncode
        health["consecutive_failures"] = consecutive_failures
        health_path = write_health_report(settings.crossalpha_data_dir, health)
        print(
            json.dumps(
                {
                    "health_ok": health["ok"],
                    "health_path": str(health_path),
                    "consecutive_failures": consecutive_failures,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        if consecutive_failures >= max(args.max_consecutive_failures, 1):
            print("too many consecutive collector failures; exiting for systemd restart", file=sys.stderr, flush=True)
            raise SystemExit(1)

        elapsed = time.monotonic() - cycle_started
        time.sleep(max(args.interval - elapsed, 1))


if __name__ == "__main__":
    main()
