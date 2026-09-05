from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from crossalpha.research_catalog import build_research_catalog


def test_research_catalog_adds_v04_and_outcome_views(tmp_path: Path) -> None:
    v04 = tmp_path / "derived" / "state" / "v04" / "year=2026" / "month=09" / "day=05"
    v04.mkdir(parents=True)
    pd.DataFrame(
        [{"asset": "BTC", "venue": "binance", "basis_bps": 1.0}]
    ).to_parquet(v04 / "venues_at=120000.parquet", index=False)
    (v04 / "mechanics_at=120000.json").write_text(
        json.dumps({"protocol": "CROSSALPHA_STATE_V0_4", "generated_at": "2026-09-05T12:00:00Z"}),
        encoding="utf-8",
    )
    v04p = (
        tmp_path
        / "research"
        / "state_v04"
        / "prospective"
        / "year=2026"
        / "month=09"
        / "day=05"
    )
    v04p.mkdir(parents=True)
    (v04p / "state_at=120000.json").write_text(
        json.dumps({"protocol": "CROSSALPHA_STATE_V0_4_PROSPECTIVE", "generated_at": "2026-09-05T12:00:00Z"}),
        encoding="utf-8",
    )
    outcome = (
        tmp_path
        / "research"
        / "outcome_linkage_v01"
        / "links"
        / "source=STATE_V04"
        / "year=2026"
        / "month=09"
        / "anchor_date=2026-09-05"
    )
    outcome.mkdir(parents=True)
    (outcome / "horizon=01d.json").write_text(
        json.dumps({"protocol": "CROSSALPHA_OUTCOME_LINKAGE_V0_1", "horizon_days": 1}),
        encoding="utf-8",
    )

    report = build_research_catalog(tmp_path)
    views = set(report["views"])
    assert "state_engine.v04_venues" in views
    assert "state_engine.v04_mechanics" in views
    assert "state_engine.v04_prospective" in views
    assert "research.outcome_links" in views
