from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from crossalpha.catalog import build_catalog
from crossalpha.observatory.canonical.aave import LIQUIDATION_COLUMNS


def _parquet(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_parquet(path, index=False)


def test_catalog_exposes_state_v02_and_aave_without_removing_v01_views(tmp_path: Path) -> None:
    _parquet(
        tmp_path / "canonical/aave/markets/year=2026/month=09/day=05/a.parquet",
        [
            {
                "observed_at": "2026-09-05T01:00:00Z",
                "known_at": "2026-09-05T01:00:01Z",
                "market_address": "0xcore",
                "symbol": "USDC",
                "borrow_apy_pct": 4.5,
                "available_liquidity_usd": 100_000_000.0,
            }
        ],
    )
    _parquet(
        tmp_path / "canonical/aave/liquidations/year=2026/month=09/day=05/a.parquet",
        [],
        list(LIQUIDATION_COLUMNS),
    )
    _parquet(
        tmp_path / "derived/state/v02/year=2026/month=09/day=05/state.parquet",
        [
            {
                "protocol": "CROSSALPHA_STATE_V0_2",
                "generated_at": "2026-09-05T01:00:02Z",
                "actionability": "DESCRIPTIVE_ONLY",
                "descriptive_stress_score": 0.2,
            }
        ],
    )
    _parquet(
        tmp_path / "derived/state/shadow_v01/year=2026/month=09/day=05/state.parquet",
        [
            {
                "protocol": "CROSSALPHA_STATE_SHADOW_V0_1",
                "state_band": "NORMAL",
                "shadow_risk_multiplier": 1.0,
            }
        ],
    )
    prospect = tmp_path / "research/state_v02/prospective/year=2026/month=09/day=05/state_at=010002.json"
    prospect.parent.mkdir(parents=True, exist_ok=True)
    prospect.write_text(
        json.dumps(
            {
                "protocol": "CROSSALPHA_STATE_V0_2_PROSPECTIVE",
                "generated_at": "2026-09-05T01:00:02Z",
                "actionability": "DESCRIPTIVE_ONLY",
                "risk_multiplier": None,
                "descriptive_stress_score": 0.2,
            }
        ),
        encoding="utf-8",
    )

    result = build_catalog(tmp_path)
    expected = {
        "observatory.aave_markets",
        "observatory.aave_liquidations",
        "state_engine.shadow_v01",
        "state_engine.v02",
        "state_engine.v02_prospective",
    }
    assert expected.issubset(set(result["views"]))

    con = duckdb.connect(result["database"], read_only=True)
    try:
        assert con.execute("SELECT symbol FROM observatory.aave_markets").fetchone()[0] == "USDC"
        assert con.execute("SELECT count(*) FROM observatory.aave_liquidations").fetchone()[0] == 0
        assert con.execute("SELECT actionability FROM state_engine.v02").fetchone()[0] == "DESCRIPTIVE_ONLY"
        assert con.execute("SELECT risk_multiplier FROM state_engine.v02_prospective").fetchone()[0] is None
        assert con.execute("SELECT shadow_risk_multiplier FROM state_engine.shadow_v01").fetchone()[0] == 1.0
    finally:
        con.close()
