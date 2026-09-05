import json
from pathlib import Path

import duckdb
import pandas as pd

from crossalpha.catalog import build_catalog


def test_build_catalog_creates_queryable_views(tmp_path: Path) -> None:
    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "raw_snapshots.jsonl"
    manifest_path.write_text(
        json.dumps(
            {
                "path": "/tmp/a.json.gz",
                "sha256": "a" * 64,
                "bytes": 100,
                "compressed_bytes": 50,
                "observed_at": "2026-09-04T12:00:00Z",
                "source_id": "hyperliquid",
                "observation_type": "metaAndAssetCtxs",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    canonical_dir = (
        tmp_path
        / "canonical"
        / "hyperliquid"
        / "asset_contexts"
        / "year=2026"
        / "month=09"
        / "day=04"
    )
    canonical_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "observed_at": "2026-09-04T12:00:00Z",
                "asset": "BTC",
                "mark_price": 100.0,
            }
        ]
    ).to_parquet(canonical_dir / "part.parquet", index=False)

    state_dir = (
        tmp_path
        / "derived"
        / "state"
        / "shadow_v01"
        / "year=2026"
        / "month=09"
        / "day=04"
    )
    state_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "as_of": "2026-09-04T12:00:00Z",
                "protocol": "CROSSALPHA_STATE_SHADOW_V0_1",
                "state_band": "NORMAL",
                "shadow_risk_multiplier": 1.0,
            }
        ]
    ).to_parquet(state_dir / "state_at=120000.parquet", index=False)

    core_dir = (
        tmp_path
        / "derived"
        / "core"
        / "free_v01"
        / "start=2026-01-01"
        / "end=2026-02-01"
    )
    core_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "date": "2026-01-02T00:00:00Z",
                "economic_asset": "US_EQUITY",
                "source": "tiingo_eod",
                "symbol": "SPY",
                "price": 100.0,
                "return": 0.01,
            }
        ]
    ).to_parquet(core_dir / "asset_returns.parquet", index=False)

    a_marks = (
        tmp_path
        / "research"
        / "free_v01"
        / "paper"
        / "marks"
        / "year=2026"
        / "month=09"
    )
    a_marks.mkdir(parents=True)
    (a_marks / "date=2026-09-07.json").write_text(
        json.dumps(
            {
                "paper_protocol": "CROSSALPHA_FREE_V0_1_PAPER",
                "date": "2026-09-07",
                "net_return": 0.01,
                "record_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    b_marks = (
        tmp_path
        / "research"
        / "free_v01"
        / "state_ab_v01"
        / "marks"
        / "year=2026"
        / "month=09"
    )
    b_marks.mkdir(parents=True)
    (b_marks / "date=2026-09-07.json").write_text(
        json.dumps(
            {
                "protocol": "CROSSALPHA_STATE_AB_V0_1",
                "date": "2026-09-07",
                "net_return": 0.008,
                "shadow_risk_multiplier": 0.75,
                "record_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    result = build_catalog(tmp_path)
    assert "observatory.raw_manifest" in result["views"]
    assert "observatory.hyperliquid_asset_contexts" in result["views"]
    assert "state_engine.shadow_v01" in result["views"]
    assert "core.free_asset_returns" in result["views"]
    assert "core.frozen_b3_paper_marks" in result["views"]
    assert "state_engine.shadow_ab_marks" in result["views"]

    con = duckdb.connect(result["database"], read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM observatory.raw_manifest").fetchone()[0] == 1
        row = con.execute(
            "SELECT asset, mark_price FROM observatory.hyperliquid_asset_contexts"
        ).fetchone()
        assert row == ("BTC", 100.0)
        state_row = con.execute(
            "SELECT state_band, shadow_risk_multiplier FROM state_engine.shadow_v01"
        ).fetchone()
        assert state_row == ("NORMAL", 1.0)
        core_row = con.execute(
            "SELECT economic_asset, symbol, return FROM core.free_asset_returns"
        ).fetchone()
        assert core_row == ("US_EQUITY", "SPY", 0.01)
        a_row = con.execute(
            "SELECT paper_protocol, net_return FROM core.frozen_b3_paper_marks"
        ).fetchone()
        assert a_row == ("CROSSALPHA_FREE_V0_1_PAPER", 0.01)
        b_row = con.execute(
            "SELECT protocol, net_return, shadow_risk_multiplier FROM state_engine.shadow_ab_marks"
        ).fetchone()
        assert b_row == ("CROSSALPHA_STATE_AB_V0_1", 0.008, 0.75)
    finally:
        con.close()
