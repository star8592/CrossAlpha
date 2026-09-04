from __future__ import annotations

from pathlib import Path

import duckdb


def latest_hyperliquid_market_state(data_root: Path, assets: list[str] | None = None) -> list[dict[str, object]]:
    db_path = data_root / "catalog" / "crossalpha.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB catalog missing: {db_path}; run `crossalpha materialize-observatory`")

    assets = assets or ["BTC", "ETH"]
    placeholders = ",".join("?" for _ in assets)
    sql = f"""
        SELECT
            observed_at,
            known_at,
            asset,
            mark_price,
            oracle_price,
            mark_oracle_basis_bps,
            impact_spread_bps,
            funding_rate,
            funding_bps,
            open_interest,
            open_interest_notional,
            open_interest_change_pct,
            day_notional_volume,
            funding_z_24h,
            basis_z_24h,
            oi_change_z_24h,
            spread_z_24h,
            rolling_observations_24h
        FROM observatory.hyperliquid_market_state
        WHERE asset IN ({placeholders})
        QUALIFY row_number() OVER (PARTITION BY asset ORDER BY observed_at DESC, known_at DESC) = 1
        ORDER BY asset
    """

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cursor = con.execute(sql, assets)
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchall()
    finally:
        con.close()

    return [dict(zip(columns, row, strict=True)) for row in rows]
