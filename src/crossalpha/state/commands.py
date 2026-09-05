from __future__ import annotations

import argparse
import json

from crossalpha.catalog import build_catalog
from crossalpha.settings import Settings
from crossalpha.state.ab_integrity import (
    strict_state_ab_integrity_report,
    strict_state_ab_status,
)
from crossalpha.state.ab_paper import (
    create_state_ab_snapshot,
    freeze_state_ab_protocol,
    strict_mark_state_ab,
)
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


def ab_freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-freeze")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = freeze_state_ab_protocol(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def ab_snapshot_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-snapshot")
    parser.add_argument("--effective-date", required=True, help="Current Monday UTC date")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = create_state_ab_snapshot(
        settings.crossalpha_data_dir,
        effective_date=args.effective_date,
        strict_live=True,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def ab_mark_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-mark")
    parser.add_argument("--end", required=True, help="UTC date, exclusive")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_mark_state_ab(settings.crossalpha_data_dir, end=args.end)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def ab_status_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-status")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    print(
        json.dumps(
            strict_state_ab_status(settings.crossalpha_data_dir),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


def ab_integrity_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-integrity")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_state_ab_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE A/B INTEGRITY FAILED")
