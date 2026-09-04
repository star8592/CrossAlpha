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

    canonical_dir = tmp_path / "canonical" / "hyperliquid" / "asset_contexts" / "year=2026" / "month=09" / "day=04"
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

    result = build_catalog(tmp_path)
    assert "observatory.raw_manifest" in result["views"]
    assert "observatory.hyperliquid_asset_contexts" in result["views"]

    con = duckdb.connect(result["database"], read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM observatory.raw_manifest").fetchone()[0] == 1
        row = con.execute(
            "SELECT asset, mark_price FROM observatory.hyperliquid_asset_contexts"
        ).fetchone()
        assert row == ("BTC", 100.0)
    finally:
        con.close()
