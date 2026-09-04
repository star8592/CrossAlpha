from __future__ import annotations

from pathlib import Path

import duckdb


def build_catalog(data_root: Path) -> dict[str, object]:
    catalog_dir = data_root / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    db_path = catalog_dir / "crossalpha.duckdb"

    manifest_path = data_root / "manifests" / "raw_snapshots.jsonl"
    hyperliquid_glob = data_root / "canonical" / "hyperliquid" / "asset_contexts" / "**" / "*.parquet"

    created_views: list[str] = []
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS observatory")
        if manifest_path.exists():
            con.execute(
                "CREATE OR REPLACE VIEW observatory.raw_manifest AS "
                "SELECT * FROM read_json_auto(?, format='newline_delimited')",
                [str(manifest_path)],
            )
            created_views.append("observatory.raw_manifest")

        parquet_files = list((data_root / "canonical" / "hyperliquid" / "asset_contexts").glob("**/*.parquet"))
        if parquet_files:
            con.execute(
                "CREATE OR REPLACE VIEW observatory.hyperliquid_asset_contexts AS "
                "SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=true)",
                [str(hyperliquid_glob)],
            )
            created_views.append("observatory.hyperliquid_asset_contexts")
    finally:
        con.close()

    return {"database": str(db_path), "views": created_views}
