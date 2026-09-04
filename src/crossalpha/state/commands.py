from __future__ import annotations

import argparse
import json

from crossalpha.catalog import build_catalog
from crossalpha.settings import Settings
from crossalpha.state.shadow import build_latest_shadow_state


def shadow_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-shadow")
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Compute the latest shadow state without materializing parquet.",
    )
    args = parser.parse_args()

    settings = Settings()
    settings.ensure_dirs()
    report = build_latest_shadow_state(
        settings.crossalpha_data_dir,
        write=not args.no_write,
    )
    if not args.no_write:
        report = {**report, "catalog": build_catalog(settings.crossalpha_data_dir)}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
