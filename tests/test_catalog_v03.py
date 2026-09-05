from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from crossalpha.catalog import build_catalog


def test_catalog_exposes_state_v03_borrower_views(tmp_path: Path) -> None:
    universe = tmp_path / "derived/state/v03/borrower_universe.parquet"
    universe.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"address": "0x" + "1" * 40}]).to_parquet(universe, index=False)

    full = tmp_path / "derived/state/v03/full_census/year=2026/month=09/day=05"
    full.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "address": "0x" + "1" * 40,
                "success": True,
                "total_debt_usd": 1_000_000.0,
                "health_factor": 1.1,
            }
        ]
    ).to_parquet(full / "accounts_at=010000.parquet", index=False)
    (full / "summary_at=010000.json").write_text(
        json.dumps(
            {
                "protocol": "CROSSALPHA_STATE_V0_3",
                "block_number": 23_000_000,
                "active_borrower_count": 1,
                "critical_hf_le_1_05_debt_share": 0.0,
            }
        ),
        encoding="utf-8",
    )

    watch = tmp_path / "derived/state/v03/watchlist/year=2026/month=09/day=05"
    watch.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [{"address": "0x" + "1" * 40, "success": True, "health_factor": 1.1}]
    ).to_parquet(watch / "accounts_at=011500.parquet", index=False)
    (watch / "summary_at=011500.json").write_text(
        json.dumps(
            {
                "protocol": "CROSSALPHA_STATE_V0_3",
                "scope": "WATCHLIST_ONLY",
                "block_number": 23_000_050,
            }
        ),
        encoding="utf-8",
    )

    prospect = tmp_path / "research/state_v03/prospective/block=23000000.json"
    prospect.parent.mkdir(parents=True, exist_ok=True)
    prospect.write_text(
        json.dumps(
            {
                "protocol": "CROSSALPHA_STATE_V0_3_PROSPECTIVE",
                "block_number": 23_000_000,
                "actionability": "DESCRIPTIVE_ONLY",
                "risk_multiplier": None,
            }
        ),
        encoding="utf-8",
    )

    result = build_catalog(tmp_path)
    expected = {
        "state_engine.v03_borrower_universe",
        "state_engine.v03_full_census",
        "state_engine.v03_full_census_accounts",
        "state_engine.v03_watchlist",
        "state_engine.v03_watchlist_accounts",
        "state_engine.v03_prospective",
    }
    assert expected.issubset(set(result["views"]))

    con = duckdb.connect(result["database"], read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM state_engine.v03_borrower_universe").fetchone()[0] == 1
        assert con.execute("SELECT active_borrower_count FROM state_engine.v03_full_census").fetchone()[0] == 1
        assert con.execute("SELECT total_debt_usd FROM state_engine.v03_full_census_accounts").fetchone()[0] == 1_000_000.0
        assert con.execute("SELECT scope FROM state_engine.v03_watchlist").fetchone()[0] == "WATCHLIST_ONLY"
        assert con.execute("SELECT risk_multiplier FROM state_engine.v03_prospective").fetchone()[0] is None
    finally:
        con.close()
