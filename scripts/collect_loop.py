#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--source", action="append", default=[])
    args = parser.parse_args()
    command = [sys.executable, "-m", "crossalpha.cli", "collect-observatory"]
    for source in args.source:
        command += ["--source", source]
    while True:
        print(f"[{datetime.now(timezone.utc).isoformat()}] collecting...")
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            print(f"collector failed rc={result.returncode}", file=sys.stderr)
        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    main()
