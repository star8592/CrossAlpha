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


def latest_stablecoin_state(data_root: Path, *, top_chains: int = 10) -> dict[str, object]:
    db_path = data_root / "catalog" / "crossalpha.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB catalog missing: {db_path}; run `crossalpha materialize-observatory`")
    if top_chains < 1:
        raise ValueError("top_chains must be >= 1")

    system_sql = """
        SELECT
            observed_at,
            known_at,
            usd_stablecoin_count,
            usd_supply_native,
            usd_market_value_usd,
            usd_delta_1d_native,
            usd_delta_7d_native,
            usd_delta_30d_native,
            usdt_market_value_usd,
            usdc_market_value_usd,
            usdt_share,
            usdc_share,
            asset_hhi,
            weighted_abs_peg_deviation_bps,
            max_abs_peg_deviation_bps,
            offpeg_50bps_market_value_usd,
            chain_sum_native,
            chain_coverage_ratio,
            chain_residual_native,
            chain_abs_residual_native,
            chain_abs_residual_ratio
        FROM observatory.stablecoin_system_state
        ORDER BY observed_at DESC, known_at DESC
        LIMIT 1
    """
    chains_sql = """
        WITH latest AS (
            SELECT max(observed_at) AS observed_at
            FROM observatory.stablecoin_chain_state
        )
        SELECT
            s.observed_at,
            s.chain,
            s.circulating_native,
            s.market_value_usd,
            s.market_share,
            s.stablecoin_count,
            s.system_chain_hhi
        FROM observatory.stablecoin_chain_state s
        JOIN latest l USING (observed_at)
        ORDER BY s.market_value_usd DESC NULLS LAST
        LIMIT ?
    """

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        sys_cur = con.execute(system_sql)
        sys_cols = [item[0] for item in sys_cur.description]
        sys_row = sys_cur.fetchone()
        chain_cur = con.execute(chains_sql, [top_chains])
        chain_cols = [item[0] for item in chain_cur.description]
        chain_rows = chain_cur.fetchall()
    finally:
        con.close()

    return {
        "system": dict(zip(sys_cols, sys_row, strict=True)) if sys_row is not None else None,
        "top_chains": [dict(zip(chain_cols, row, strict=True)) for row in chain_rows],
    }
