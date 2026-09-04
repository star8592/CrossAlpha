from __future__ import annotations

import argparse
import json

from crossalpha.core.free_dataset import audit_free_core, build_free_core_returns
from crossalpha.core.free_provider import FreeCoreRange
from crossalpha.settings import Settings


def _range_parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--start", default="2010-06-01")
    parser.add_argument("--end", required=True)
    return parser


def audit_main() -> None:
    parser = _range_parser("crossalpha-free-audit")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = audit_free_core(
        settings.crossalpha_data_dir,
        FreeCoreRange(start=args.start, end=args.end),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("FREE CORE QUALITY GATE FAILED")


def returns_main() -> None:
    parser = _range_parser("crossalpha-free-returns")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = build_free_core_returns(
        settings.crossalpha_data_dir,
        FreeCoreRange(start=args.start, end=args.end),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
