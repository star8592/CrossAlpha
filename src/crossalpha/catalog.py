from __future__ import annotations

from pathlib import Path

import duckdb


def _sql_string(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _has_parquet(root: Path) -> bool:
    if not root.exists():
        return False
    return next(root.rglob("*.parquet"), None) is not None


def _has_json(root: Path) -> bool:
    if not root.exists():
        return False
    return next(root.rglob("*.json"), None) is not None


def build_catalog(data_root: Path) -> dict[str, object]:
    catalog_dir = data_root / "catalog"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    db_path = catalog_dir / "crossalpha.duckdb"

    manifest_path = data_root / "manifests" / "raw_snapshots.jsonl"
    hyperliquid_root = data_root / "canonical" / "hyperliquid" / "asset_contexts"
    hyperliquid_glob = hyperliquid_root / "**" / "*.parquet"
    market_state_root = data_root / "derived" / "hyperliquid" / "market_state"
    market_state_glob = market_state_root / "**" / "*.parquet"
    stablecoin_assets_root = data_root / "canonical" / "defillama" / "stablecoin_assets"
    stablecoin_assets_glob = stablecoin_assets_root / "**" / "*.parquet"
    stablecoin_chains_root = data_root / "canonical" / "defillama" / "stablecoin_chain_supply"
    stablecoin_chains_glob = stablecoin_chains_root / "**" / "*.parquet"
    stablecoin_system_root = data_root / "derived" / "stablecoins" / "system_state"
    stablecoin_system_glob = stablecoin_system_root / "**" / "*.parquet"
    stablecoin_chain_state_root = data_root / "derived" / "stablecoins" / "chain_state"
    stablecoin_chain_state_glob = stablecoin_chain_state_root / "**" / "*.parquet"
    state_shadow_root = data_root / "derived" / "state" / "shadow_v01"
    state_shadow_glob = state_shadow_root / "**" / "*.parquet"
    free_core_returns_root = data_root / "derived" / "core" / "free_v01"
    free_core_returns_glob = free_core_returns_root / "**" / "asset_returns.parquet"
    core_paper_marks_root = data_root / "research" / "free_v01" / "paper" / "marks"
    core_paper_marks_glob = core_paper_marks_root / "**" / "date=*.json"
    state_ab_marks_root = data_root / "research" / "free_v01" / "state_ab_v01" / "marks"
    state_ab_marks_glob = state_ab_marks_root / "**" / "date=*.json"

    created_views: list[str] = []
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS observatory")
        con.execute("CREATE SCHEMA IF NOT EXISTS core")
        con.execute("CREATE SCHEMA IF NOT EXISTS state_engine")
        for view in (
            "observatory.raw_manifest",
            "observatory.hyperliquid_asset_contexts",
            "observatory.hyperliquid_market_state",
            "observatory.stablecoin_assets",
            "observatory.stablecoin_chain_supply",
            "observatory.stablecoin_system_state",
            "observatory.stablecoin_chain_state",
            "state_engine.shadow_v01",
            "state_engine.shadow_ab_marks",
            "core.free_asset_returns",
            "core.frozen_b3_paper_marks",
        ):
            con.execute(f"DROP VIEW IF EXISTS {view}")

        if manifest_path.exists():
            con.execute(
                "CREATE VIEW observatory.raw_manifest AS "
                f"SELECT * FROM read_json_auto({_sql_string(manifest_path)}, format='newline_delimited')"
            )
            created_views.append("observatory.raw_manifest")

        if _has_parquet(hyperliquid_root):
            con.execute(
                "CREATE VIEW observatory.hyperliquid_asset_contexts AS "
                f"SELECT * FROM read_parquet({_sql_string(hyperliquid_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.hyperliquid_asset_contexts")

        if _has_parquet(market_state_root):
            con.execute(
                "CREATE VIEW observatory.hyperliquid_market_state AS "
                f"SELECT * FROM read_parquet({_sql_string(market_state_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.hyperliquid_market_state")

        if _has_parquet(stablecoin_assets_root):
            con.execute(
                "CREATE VIEW observatory.stablecoin_assets AS "
                f"SELECT * FROM read_parquet({_sql_string(stablecoin_assets_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.stablecoin_assets")

        if _has_parquet(stablecoin_chains_root):
            con.execute(
                "CREATE VIEW observatory.stablecoin_chain_supply AS "
                f"SELECT * FROM read_parquet({_sql_string(stablecoin_chains_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.stablecoin_chain_supply")

        if _has_parquet(stablecoin_system_root):
            con.execute(
                "CREATE VIEW observatory.stablecoin_system_state AS "
                f"SELECT * FROM read_parquet({_sql_string(stablecoin_system_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.stablecoin_system_state")

        if _has_parquet(stablecoin_chain_state_root):
            con.execute(
                "CREATE VIEW observatory.stablecoin_chain_state AS "
                f"SELECT * FROM read_parquet({_sql_string(stablecoin_chain_state_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("observatory.stablecoin_chain_state")

        if _has_parquet(state_shadow_root):
            con.execute(
                "CREATE VIEW state_engine.shadow_v01 AS "
                f"SELECT * FROM read_parquet({_sql_string(state_shadow_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("state_engine.shadow_v01")

        if _has_json(state_ab_marks_root):
            con.execute(
                "CREATE VIEW state_engine.shadow_ab_marks AS "
                f"SELECT * FROM read_json_auto({_sql_string(state_ab_marks_glob)})"
            )
            created_views.append("state_engine.shadow_ab_marks")

        if _has_parquet(free_core_returns_root):
            con.execute(
                "CREATE VIEW core.free_asset_returns AS "
                f"SELECT * FROM read_parquet({_sql_string(free_core_returns_glob)}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created_views.append("core.free_asset_returns")

        if _has_json(core_paper_marks_root):
            con.execute(
                "CREATE VIEW core.frozen_b3_paper_marks AS "
                f"SELECT * FROM read_json_auto({_sql_string(core_paper_marks_glob)})"
            )
            created_views.append("core.frozen_b3_paper_marks")
    finally:
        con.close()

    return {"database": str(db_path), "views": created_views}
