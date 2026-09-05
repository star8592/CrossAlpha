#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from crossalpha.settings import Settings
from crossalpha.state.v03_cycle import run_state_v03_cycle


def main() -> None:
    settings = Settings()
    settings.ensure_dirs()
    report = asyncio.run(run_state_v03_cycle(settings))
    state_path = settings.crossalpha_data_dir / "research" / "state_v03" / "bootstrap_state.json"
    bootstrap = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {}
    )
    health = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "CROSSALPHA_STATE_V0_3_CYCLE_HEALTH",
        "status": report.get("status"),
        "rpc_source": report.get("rpc_source"),
        "data_cost_usd": report.get("data_cost_usd"),
        "latest_block": report.get("latest_block"),
        "finalized_block": report.get("finalized_block"),
        "next_block": report.get("next_block", bootstrap.get("next_block")),
        "last_scanned_block": bootstrap.get("last_scanned_block"),
        "bootstrap_complete": bool(bootstrap.get("bootstrap_complete")),
        "candidate_address_count": report.get(
            "candidate_address_count", bootstrap.get("candidate_address_count")
        ),
        "pending_new_borrower_count": report.get(
            "pending_new_borrower_count",
            len(bootstrap.get("pending_new_borrowers_since_full", [])),
        ),
        "last_valid_full_census_at": bootstrap.get("last_valid_full_census_at"),
        "last_valid_full_census_block": bootstrap.get("last_valid_full_census_block"),
        "historical_bootstrap_is_evidence": report.get(
            "historical_bootstrap_is_evidence", False
        ),
        "mutates_v01_or_v02": report.get("mutates_v01_or_v02"),
    }
    path = settings.crossalpha_data_dir / "manifests" / "state_v03_cycle_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    print(
        json.dumps(
            {**report, "cycle_health_file": str(path), "cycle_health": health},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
