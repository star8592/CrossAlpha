#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from crossalpha.settings import Settings
from crossalpha.state.v04_cycle import run_state_v04_cycle


def main() -> None:
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(run_state_v04_cycle(settings, write=True))
    state = report.get("state", {})
    health = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "CROSSALPHA_STATE_V0_4_CYCLE_HEALTH",
        "generated_at": report.get("generated_at"),
        "data_confidence": state.get("data_confidence"),
        "funding_semantics": state.get("funding_semantics"),
        "data_cost_usd": report.get("data_cost_usd"),
        "authentication_required": report.get("authentication_required"),
        "mutates_predecessors": report.get("mutates_predecessors"),
        "prospective_status": report.get("prospective", {}).get("status"),
        "BTC_valid_venues": state.get("assets", {}).get("BTC", {}).get("valid_venue_count"),
        "ETH_valid_venues": state.get("assets", {}).get("ETH", {}).get("valid_venue_count"),
        "BTC_funding_comparable_venues": state.get("assets", {}).get("BTC", {}).get(
            "funding_comparable_venue_count"
        ),
        "ETH_funding_comparable_venues": state.get("assets", {}).get("ETH", {}).get(
            "funding_comparable_venue_count"
        ),
    }
    path = settings.crossalpha_data_dir / "manifests" / "state_v04_cycle_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    print(json.dumps({**report, "cycle_health_file": str(path)}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
