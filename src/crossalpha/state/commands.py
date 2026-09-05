from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from crossalpha.catalog import build_catalog
from crossalpha.core.free_paper import paper_status
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
from crossalpha.state.v02 import build_latest_state_v02
from crossalpha.state.v02_config import strict_v02_config_consistency_report
from crossalpha.state.v02_cycle import run_state_v02_cycle, write_cycle_health
from crossalpha.state.v02_integrity import (
    strict_state_v02_integrity_report,
    strict_state_v02_status,
)
from crossalpha.state.v02_prospective import freeze_state_v02
from crossalpha.state.v03_config import strict_v03_config_report
from crossalpha.state.v03_cycle import FINALITY_LAG_BLOCKS, run_state_v03_cycle
from crossalpha.state.v03_integrity import (
    strict_state_v03_integrity_report,
    strict_state_v03_status,
)
from crossalpha.state.v03_prospective import freeze_state_v03
from crossalpha.state.v03_rpc import (
    AAVE_V3_ETHEREUM_CORE_POOL,
    AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    AaveBorrowerRpc,
    RpcPolicy,
    resolve_rpc_url,
)


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
    report = build_latest_shadow_state(settings.crossalpha_data_dir, write=not args.no_write)
    if not args.no_write:
        report = {**report, "catalog": build_catalog(settings.crossalpha_data_dir)}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def ab_freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-freeze")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    current_ab = strict_state_ab_status(settings.crossalpha_data_dir)
    if not current_ab.get("frozen"):
        core = paper_status(settings.crossalpha_data_dir)
        first = core.get("first_eligible_effective_date")
        if not first:
            raise SystemExit("Frozen B3 paper protocol must exist before State A/B freeze")
        today = datetime.now(timezone.utc).date()
        first_date = datetime.fromisoformat(str(first)).date()
        if today >= first_date:
            raise SystemExit(
                "STATE A/B FREEZE TOO LATE: a new prospective V0.1 experiment must be "
                f"frozen before {first_date.isoformat()}, not on/after it. "
                "Create a new protocol version instead."
            )
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
    print(json.dumps(strict_state_ab_status(settings.crossalpha_data_dir), ensure_ascii=False, indent=2, default=str))


def ab_integrity_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-ab-integrity")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_state_ab_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE A/B INTEGRITY FAILED")


def v02_describe_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v02-describe")
    parser.add_argument("--write", action="store_true", help="Materialize a derived V0.2 parquet row")
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = build_latest_state_v02(settings.crossalpha_data_dir, write=args.write)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def v02_config_check_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v02-config-check")
    parser.parse_args()
    report = strict_v02_config_consistency_report(Path("config/state_v02.yaml"))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE V0.2 STRICT CONFIG CONSISTENCY FAILED")


def v02_freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v02-freeze")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    consistency = strict_v02_config_consistency_report(Path("config/state_v02.yaml"))
    if not consistency.get("ok"):
        raise SystemExit("STATE V0.2 FREEZE REFUSED: strict config consistency failed")
    report = freeze_state_v02(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def v02_cycle_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v02-cycle")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(run_state_v02_cycle(settings))
    health = write_cycle_health(settings.crossalpha_data_dir, report)
    print(json.dumps({**report, "cycle_health_file": str(health)}, ensure_ascii=False, indent=2, default=str))


def v02_integrity_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v02-integrity")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_state_v02_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE V0.2 PROSPECTIVE INTEGRITY FAILED")


def v02_status_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v02-status")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    print(json.dumps(strict_state_v02_status(settings.crossalpha_data_dir), ensure_ascii=False, indent=2, default=str))


def v03_config_check_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v03-config-check")
    parser.parse_args()
    report = strict_v03_config_report(Path("config/state_v03.yaml"))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE V0.3 STRICT CONFIG CONSISTENCY FAILED")


async def _v03_preflight(settings: Settings) -> dict[str, object]:
    rpc_url, rpc_source = resolve_rpc_url(settings.evm_rpc_url)
    rpc = AaveBorrowerRpc(
        rpc_url,
        policy=RpcPolicy(batch_size=100, timeout_seconds=settings.crossalpha_http_timeout),
    )
    latest = await rpc.latest_block()
    finalized = max(latest - FINALITY_LAG_BLOCKS, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK)
    recent_from = max(finalized - 127, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK)
    recent_logs = await rpc.borrow_logs(recent_from, finalized)
    historical_from = AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK
    historical_to = historical_from + 255
    historical_logs = await rpc.borrow_logs(historical_from, historical_to)
    probe = await rpc.account_data([AAVE_V3_ETHEREUM_CORE_POOL], block_number=finalized)
    if probe.empty or not bool(probe.iloc[0]["success"]):
        raise RuntimeError("getUserAccountData fixed-block probe failed")
    return {
        "protocol": "CROSSALPHA_STATE_V0_3_PREFLIGHT",
        "data_cost_usd": 0,
        "rpc_source": rpc_source,
        "latest_block": latest,
        "finalized_block": finalized,
        "finality_lag_blocks": FINALITY_LAG_BLOCKS,
        "recent_borrow_scan_from_block": recent_from,
        "recent_borrow_scan_to_block": finalized,
        "recent_borrow_log_count": len(recent_logs),
        "historical_log_scan_from_block": historical_from,
        "historical_log_scan_to_block": historical_to,
        "historical_log_scan_ok": True,
        "historical_borrow_log_count": len(historical_logs),
        "fixed_block_account_call_ok": True,
        "actionability": "DESCRIPTIVE_ONLY",
        "risk_multiplier": None,
    }


def v03_preflight_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v03-preflight")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(_v03_preflight(settings))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def v03_freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v03-freeze")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    config = strict_v03_config_report(Path("config/state_v03.yaml"))
    if not config.get("ok"):
        raise SystemExit("STATE V0.3 FREEZE REFUSED: strict config consistency failed")
    v02 = strict_state_v02_integrity_report(settings.crossalpha_data_dir)
    if not v02.get("frozen") or not v02.get("ok"):
        raise SystemExit("STATE V0.3 FREEZE REFUSED: State V0.2 must be frozen and healthy")
    preflight = asyncio.run(_v03_preflight(settings))
    report = freeze_state_v03(
        settings.crossalpha_data_dir,
        minimum_eligible_block=int(preflight["finalized_block"]),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def v03_cycle_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v03-cycle")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(run_state_v03_cycle(settings))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def v03_integrity_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v03-integrity")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_state_v03_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE V0.3 PROSPECTIVE INTEGRITY FAILED")


def v03_status_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v03-status")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    print(json.dumps(strict_state_v03_status(settings.crossalpha_data_dir), ensure_ascii=False, indent=2, default=str))
