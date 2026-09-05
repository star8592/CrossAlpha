#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone

from crossalpha.outcomes.integrity import (
    outcome_linkage_status,
    strict_outcome_linkage_integrity_report,
)
from crossalpha.outcomes.linkage import materialize_outcome_links
from crossalpha.research_catalog import build_research_catalog
from crossalpha.settings import Settings


def main() -> None:
    settings = Settings()
    settings.ensure_dirs()
    materialized = materialize_outcome_links(settings.crossalpha_data_dir)
    integrity = strict_outcome_linkage_integrity_report(settings.crossalpha_data_dir)
    if not integrity.get("ok"):
        raise RuntimeError(f"Outcome Linkage integrity failed after materialization: {integrity}")
    status = outcome_linkage_status(settings.crossalpha_data_dir)
    catalog = build_research_catalog(settings.crossalpha_data_dir)
    health = {
        "written_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "CROSSALPHA_OUTCOME_LINKAGE_V0_1_CYCLE_HEALTH",
        "materialized": materialized,
        "state": status.get("state"),
        "daily_anchor_count": status.get("daily_anchor_count"),
        "link_count": status.get("link_count"),
        "matured_expected_link_count": status.get("matured_expected_link_count"),
        "integrity_ok": integrity.get("ok"),
        "catalog_views": catalog.get("views", []),
    }
    path = settings.crossalpha_data_dir / "manifests" / "outcome_linkage_cycle_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    print(json.dumps({**health, "health_file": str(path)}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
