from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb

from crossalpha.observatory.query import latest_hyperliquid_market_state


def test_latest_market_state_handles_timestamptz(tmp_path: Path) -> None:
    db_dir = tmp_path / "catalog"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "crossalpha.duckdb"

    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA observatory")
        con.execute(
            """
            CREATE TABLE observatory.hyperliquid_market_state (
                observed_at TIMESTAMPTZ,
                known_at TIMESTAMPTZ,
                asset VARCHAR,
                mark_price DOUBLE,
                oracle_price DOUBLE,
                mark_oracle_basis_bps DOUBLE,
                impact_spread_bps DOUBLE,
                funding_rate DOUBLE,
                funding_bps DOUBLE,
                open_interest DOUBLE,
                open_interest_notional DOUBLE,
                open_interest_change_pct DOUBLE,
                day_notional_volume DOUBLE,
                funding_z_24h DOUBLE,
                basis_z_24h DOUBLE,
                oi_change_z_24h DOUBLE,
                spread_z_24h DOUBLE,
                rolling_observations_24h BIGINT
            )
            """
        )
        observed = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        con.execute(
            """
            INSERT INTO observatory.hyperliquid_market_state VALUES (
                ?, ?, 'BTC', 100000.0, 99990.0, 1.0, 0.5,
                0.0001, 1.0, 1234.0, 123400000.0, 0.01,
                500000000.0, NULL, NULL, NULL, NULL, 12
            )
            """,
            [observed, observed],
        )
    finally:
        con.close()

    rows = latest_hyperliquid_market_state(tmp_path, ["BTC"])
    assert len(rows) == 1
    assert rows[0]["asset"] == "BTC"
    assert rows[0]["mark_price"] == 100000.0
    assert rows[0]["observed_at"].tzinfo is not None
