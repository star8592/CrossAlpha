from __future__ import annotations

from pathlib import Path

import duckdb


def _sql_string(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def build_catalog(data_root: Path) -> dict[str, object]:
    catalog_dir = data_root / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    db_path = catalog_dir / "crossalpha.duckdb"

    manifest_path = data_root / "manifests" / "raw_snapshots.jsonl"
    hyperliquid_root = data_root / "canonical" / "hyperliquid" / "asset_contexts"
    hyperliquid_glob = hyperliquid_root / "**" / "*.parquet"

    created_views: list[str] = []
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS observatory")
        if manifest_path.exists():
            con.execute(
                "CREATE OR REPLACE VIEW observatory.raw_manifest AS "
                f"SELECT * FROM read_json_auto({_sql_string(manifest_path)}, format='newline_delimited')"
            )
            created_views.append("observatory.raw_manifest")

        parquet_files = list(hyperliquid_root.glob("**/*.parquet")) if hyperliquid_root.exists() else []
        if parquet_files:
            con.execute(
                "CREATE OR REPLACE VIEW observatory.hyperliquid_asset_contexts AS "
                f"SELECT * FROM read_parquet({_sql_string(hyperliquid_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.hyperliquid_asset_contexts")
    finally:
        con.close()

    return {"database": str(db_path), "views": created_views}
