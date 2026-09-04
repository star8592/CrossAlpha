from __future__ import annotations

import argparse
import json

from crossalpha.core.free_baselines import FreeBaselineConfig, run_free_baselines
from crossalpha.core.free_dataset import audit_free_core, build_free_core_returns
from crossalpha.core.free_final_evaluation import run_free_final_evaluation
from crossalpha.core.free_paper import (
    HISTORICAL_END,
    HISTORICAL_START,
    create_paper_snapshot,
    freeze_paper_protocol,
    paper_status,
    refresh_paper_core,
)
from crossalpha.core.free_paper_guard import strict_mark_paper_forward
from crossalpha.core.free_provider import FreeCoreRange
from crossalpha.core.free_robustness import run_free_robustness_stage1
from crossalpha.core.free_robustness_stage2 import run_free_robustness_stage2
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


def baselines_main() -> None:
    parser = _range_parser("crossalpha-free-baselines")
    parser.add_argument(
        "--cost-bps",
        type=float,
        default=5.0,
        help="One-way turnover cost in basis points; frozen V0.1 baseline is 5 bps.",
    )
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = run_free_baselines(
        settings.crossalpha_data_dir,
        start=args.start,
        end=args.end,
        config=FreeBaselineConfig(cost_bps=args.cost_bps),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def robustness_main() -> None:
    parser = _range_parser("crossalpha-free-robustness")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = run_free_robustness_stage1(
        settings.crossalpha_data_dir,
        start=args.start,
        end=args.end,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def robustness2_main() -> None:
    parser = _range_parser("crossalpha-free-robustness2")
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=2000,
        help="Circular block-bootstrap replications; minimum 100.",
    )
    parser.add_argument("--seed", type=int, default=8592)
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = run_free_robustness_stage2(
        settings.crossalpha_data_dir,
        start=args.start,
        end=args.end,
        bootstrap_replications=args.bootstrap,
        seed=args.seed,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def final_evaluation_main() -> None:
    parser = _range_parser("crossalpha-free-finalize")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = run_free_final_evaluation(
        settings.crossalpha_data_dir,
        start=args.start,
        end=args.end,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def paper_freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-free-paper-freeze")
    parser.add_argument("--historical-start", default=HISTORICAL_START)
    parser.add_argument("--historical-end", default=HISTORICAL_END)
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = freeze_paper_protocol(
        settings.crossalpha_data_dir,
        historical_start=args.historical_start,
        historical_end=args.historical_end,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def paper_refresh_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-free-paper-refresh")
    parser.add_argument("--start", default=HISTORICAL_START)
    parser.add_argument("--end", required=True, help="UTC date, exclusive")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = refresh_paper_core(settings, start=args.start, end=args.end)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def paper_snapshot_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-free-paper-snapshot")
    parser.add_argument("--effective-date", required=True, help="Current Monday UTC date")
    parser.add_argument("--research-start", default=HISTORICAL_START)
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = create_paper_snapshot(
        settings.crossalpha_data_dir,
        effective_date=args.effective_date,
        research_start=args.research_start,
        strict_live=True,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def paper_mark_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-free-paper-mark")
    parser.add_argument("--end", required=True, help="UTC date, exclusive")
    parser.add_argument("--research-start", default=HISTORICAL_START)
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_mark_paper_forward(
        settings.crossalpha_data_dir,
        end=args.end,
        research_start=args.research_start,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def paper_status_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-free-paper-status")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = paper_status(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
