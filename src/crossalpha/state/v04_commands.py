from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pandas as pd

from crossalpha.settings import Settings
from crossalpha.state import v04
from crossalpha.state.v03_integrity import strict_state_v03_integrity_report
from crossalpha.state.v04_config import strict_v04_config_report
from crossalpha.state.v04_cycle import run_state_v04_cycle
from crossalpha.state.v04_integrity import (
    strict_state_v04_integrity_report,
    strict_state_v04_status,
)
from crossalpha.state.v04_prospective import freeze_state_v04
from crossalpha.state.v04_provider import MultiVenueCollector, parse_venue_snapshot


def config_check_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v04-config-check")
    parser.parse_args()
    report = strict_v04_config_report(Path("config/state_v04.yaml"))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE V0.4 STRICT CONFIG CONSISTENCY FAILED")


async def _preflight(settings: Settings) -> dict[str, object]:
    collector = MultiVenueCollector(timeout=settings.crossalpha_http_timeout)
    payloads = await collector.collect()
    if len(payloads) != 6:
        raise RuntimeError(f"State V0.4 preflight expected 6 payloads, got {len(payloads)}")
    known = pd.Timestamp.now(tz="UTC")
    rows = pd.DataFrame(
        [parse_venue_snapshot(payload, known_at=known) for payload in payloads]
    )
    report = v04.compute_market_mechanics(rows, generated_at=known)
    if report.get("data_confidence") == "INSUFFICIENT":
        raise RuntimeError(f"State V0.4 live preflight insufficient: {report}")
    funding_semantics_ok = rows["funding_semantics"].astype(str).eq(v04.FUNDING_SEMANTICS).all()
    comparable = pd.to_numeric(rows["funding_rate_8h"], errors="coerce").notna()
    if not funding_semantics_ok:
        raise RuntimeError("State V0.4 preflight funding semantics mismatch")
    if int(comparable.sum()) < 4:
        raise RuntimeError(
            "State V0.4 preflight requires at least four comparable settled-funding rows"
        )
    return {
        "protocol": "CROSSALPHA_STATE_V0_4_PREFLIGHT",
        "data_cost_usd": 0,
        "authentication_required": False,
        "payload_count": len(payloads),
        "venue_row_count": len(rows),
        "data_confidence": report.get("data_confidence"),
        "funding_semantics": v04.FUNDING_SEMANTICS,
        "funding_comparable_row_count": int(comparable.sum()),
        "assets": report.get("assets"),
        "actionability": v04.ACTIONABILITY,
        "risk_multiplier": None,
        "no_composite_stress_score": True,
    }


def preflight_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v04-preflight")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(_preflight(settings))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v04-freeze")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    config = strict_v04_config_report(Path("config/state_v04.yaml"))
    if not config.get("ok"):
        raise SystemExit("STATE V0.4 FREEZE REFUSED: strict config consistency failed")
    v03 = strict_state_v03_integrity_report(settings.crossalpha_data_dir)
    if not v03.get("frozen") or not v03.get("ok"):
        raise SystemExit("STATE V0.4 FREEZE REFUSED: State V0.3 must be frozen and healthy")
    asyncio.run(_preflight(settings))
    report = freeze_state_v04(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def cycle_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v04-cycle")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(run_state_v04_cycle(settings, write=True))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def integrity_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v04-integrity")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_state_v04_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE V0.4 PROSPECTIVE INTEGRITY FAILED")


def status_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-state-v04-status")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_state_v04_status(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
