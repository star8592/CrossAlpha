from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from crossalpha.catalog import build_catalog
from crossalpha.observatory.canonical.aave import canonicalize_aave
from crossalpha.observatory.providers.aave import (
    AaveV3LiquidationRpcProvider,
    AaveV3MarketProvider,
)
from crossalpha.settings import Settings
from crossalpha.state.v02 import build_latest_state_v02
from crossalpha.state.v02_prospective import (
    _freeze_path,
    state_v02_integrity_report,
    state_v02_status,
    write_live_state_v02_observation,
)
from crossalpha.storage.raw import RawSnapshotStore


async def run_state_v02_cycle(settings: Settings) -> dict[str, Any]:
    """Run one isolated State V0.2 O0/O1 cycle.

    The existing V0.1 collector/timers are deliberately not called or modified.
    Aave market data is required for a successful V0.2 collection cycle. The
    liquidation RPC stream is optional and can degrade independently.
    """
    settings.ensure_dirs()
    data_root = settings.crossalpha_data_dir
    store = RawSnapshotStore(data_root)
    started_at = datetime.now(timezone.utc)

    market_provider = AaveV3MarketProvider(timeout=settings.crossalpha_http_timeout)
    market_manifests: list[dict[str, Any]] = []
    for envelope in await market_provider.collect():
        manifest = store.write(envelope)
        market_manifests.append(manifest.model_dump(mode="json"))

    liquidation_status: dict[str, Any]
    if settings.evm_rpc_url:
        try:
            liquidation_provider = AaveV3LiquidationRpcProvider(
                settings.evm_rpc_url,
                timeout=settings.crossalpha_http_timeout,
            )
            liquidation_manifests: list[dict[str, Any]] = []
            for envelope in await liquidation_provider.collect():
                manifest = store.write(envelope)
                liquidation_manifests.append(manifest.model_dump(mode="json"))
            liquidation_status = {
                "configured": True,
                "ok": True,
                "manifests": liquidation_manifests,
                "error": None,
            }
        except Exception as exc:  # optional feed: report degradation without hiding it
            liquidation_status = {
                "configured": True,
                "ok": False,
                "manifests": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        liquidation_status = {
            "configured": False,
            "ok": None,
            "manifests": [],
            "error": None,
            "reason": "EVM_RPC_URL optional and not configured",
        }

    canonical = canonicalize_aave(data_root, recent_days=2)
    state = build_latest_state_v02(data_root, write=True)
    if state.get("status") == "no_inputs":
        raise RuntimeError("State V0.2 has no inputs after successful Aave market collection")

    if _freeze_path(data_root).exists():
        prospective = write_live_state_v02_observation(data_root, state, strict_live=True)
        integrity = state_v02_integrity_report(data_root)
        status = state_v02_status(data_root)
    else:
        prospective = {
            "protocol": "CROSSALPHA_STATE_V0_2_PROSPECTIVE",
            "status": "not_frozen_no_prospective_write",
        }
        integrity = {"frozen": False, "ok": False, "reason": "not_frozen"}
        status = {"state": "NOT_FROZEN", "frozen": False}

    catalog = build_catalog(data_root)
    completed_at = datetime.now(timezone.utc)
    return {
        "protocol": "CROSSALPHA_STATE_V0_2_CYCLE",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "data_cost_usd": 0,
        "v01_collector_or_paper_mutated": False,
        "aave_market": {
            "required": True,
            "ok": True,
            "manifest_count": len(market_manifests),
            "manifests": market_manifests,
        },
        "aave_liquidations_rpc": liquidation_status,
        "canonical": canonical,
        "state_v02": state,
        "prospective": prospective,
        "prospective_integrity": integrity,
        "prospective_status": status,
        "catalog": catalog,
    }


def write_cycle_health(data_root: Path, report: dict[str, Any]) -> Path:
    path = data_root / "manifests" / "state_v02_cycle_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "protocol": report.get("protocol"),
        "market_ok": bool(report.get("aave_market", {}).get("ok")),
        "optional_liquidation_rpc": report.get("aave_liquidations_rpc"),
        "state_status": report.get("state_v02", {}).get("status"),
        "state_confidence": report.get("state_v02", {}).get("data_confidence"),
        "prospective_status": report.get("prospective_status", {}).get("state"),
        "prospective_integrity_ok": report.get("prospective_integrity", {}).get("ok"),
        "v01_collector_or_paper_mutated": False,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path
