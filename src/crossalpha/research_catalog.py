from __future__ import annotations

from pathlib import Path

import duckdb

from crossalpha.catalog import build_catalog


def _sql(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _has(root: Path, pattern: str) -> bool:
    return root.exists() and next(root.rglob(pattern), None) is not None


def build_research_catalog(data_root: Path) -> dict[str, object]:
    """Build base catalog, then add append-only V0.4/outcome research views."""
    base = build_catalog(data_root)
    db_path = Path(str(base["database"]))
    v04_root = data_root / "derived" / "state" / "v04"
    v04_prospective = data_root / "research" / "state_v04" / "prospective"
    outcome_root = data_root / "research" / "outcome_linkage_v01" / "links"
    created = list(base.get("views", []))

    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS state_engine")
        con.execute("CREATE SCHEMA IF NOT EXISTS research")
        for view in (
            "state_engine.v04_venues",
            "state_engine.v04_mechanics",
            "state_engine.v04_prospective",
            "research.outcome_links",
        ):
            con.execute(f"DROP VIEW IF EXISTS {view}")

        if _has(v04_root, "venues_at=*.parquet"):
            con.execute(
                "CREATE VIEW state_engine.v04_venues AS "
                f"SELECT * FROM read_parquet({_sql(v04_root / '**' / 'venues_at=*.parquet')}, "
                "union_by_name=true, hive_partitioning=true)"
            )
            created.append("state_engine.v04_venues")
        if _has(v04_root, "mechanics_at=*.json"):
            con.execute(
                "CREATE VIEW state_engine.v04_mechanics AS "
                f"SELECT * FROM read_json_auto({_sql(v04_root / '**' / 'mechanics_at=*.json')})"
            )
            created.append("state_engine.v04_mechanics")
        if _has(v04_prospective, "state_at=*.json"):
            con.execute(
                "CREATE VIEW state_engine.v04_prospective AS "
                f"SELECT * FROM read_json_auto({_sql(v04_prospective / '**' / 'state_at=*.json')})"
            )
            created.append("state_engine.v04_prospective")
        if _has(outcome_root, "horizon=*d.json"):
            con.execute(
                "CREATE VIEW research.outcome_links AS "
                f"SELECT * FROM read_json_auto({_sql(outcome_root / '**' / 'horizon=*d.json')})"
            )
            created.append("research.outcome_links")
    finally:
        con.close()
    return {"database": str(db_path), "views": created}


def main() -> None:
    from crossalpha.settings import Settings

    settings = Settings()
    settings.ensure_dirs()
    print(build_research_catalog(settings.crossalpha_data_dir))
