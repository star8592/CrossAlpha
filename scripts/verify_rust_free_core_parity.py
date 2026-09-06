from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from crossalpha.core.free_provider import (
    parse_binance_klines,
    parse_fred_observations,
    parse_tiingo_eod_payload,
)

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "target" / "debug" / "crossalpha-free-core-fixture-rs"

TIINGO_COLUMNS = [
    "date",
    "economic_asset",
    "source",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "adj_volume",
    "div_cash",
    "split_factor",
]
BINANCE_COLUMNS = [
    "date",
    "economic_asset",
    "source",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
]
FRED_COLUMNS = ["date", "series_id", "rate_percent"]


def normalize(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.tz_convert("UTC").isoformat()
    if isinstance(value, np.generic):
        return normalize(value.item())
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return value
    if isinstance(value, str) and (value.endswith("Z") or "+00:00" in value):
        try:
            return pd.Timestamp(value).tz_convert("UTC").isoformat()
        except (TypeError, ValueError):
            return value
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def records(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    return normalize(frame.loc[:, columns].to_dict(orient="records"))


def project(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    return normalize([{column: row.get(column) for column in columns} for row in rows])


def main() -> int:
    if not BINARY.exists():
        raise SystemExit(f"missing Rust Free Core fixture binary: {BINARY}")

    tiingo_payload = [
        {
            "date": "2026-09-03T00:00:00.000Z",
            "open": 100.0,
            "high": 102.0,
            "low": 99.5,
            "close": 101.5,
            "volume": 1234567,
            "adjOpen": 99.0,
            "adjHigh": 101.0,
            "adjLow": 98.5,
            "adjClose": 100.5,
            "adjVolume": 1240000,
            "divCash": 0.25,
            "splitFactor": 1.0,
        },
        {
            "date": "2026-09-04T00:00:00.000Z",
            "open": 101.5,
            "high": 103.0,
            "low": 101.0,
            "close": 102.0,
            "volume": 2345678,
            "adjOpen": 100.5,
            "adjHigh": 102.0,
            "adjLow": 100.0,
            "adjClose": 101.0,
            "adjVolume": 2350000,
            "divCash": 0.0,
            "splitFactor": 1.0,
        },
    ]
    binance_payload = [
        [1788393600000, "110000.0", "112000.0", "109000.0", "111000.0", "123.5", 1788479999999, "13708500.0", 54321, "63.1", "7000000.0", "0"],
        [1788480000000, "111000.0", "113500.0", "110500.0", "112500.0", "150.25", 1788566399999, "16900000.0", 60001, "77.2", "8685000.0", "0"],
    ]
    fred_payload = {
        "observations": [
            {"date": "2026-09-02", "value": "5.20"},
            {"date": "2026-09-03", "value": "."},
            {"date": "2026-09-04", "value": "5.18"},
        ]
    }
    fixture = {
        "tiingo": {
            "economic_asset": "US_EQUITY",
            "ticker": "SPY",
            "payload": tiingo_payload,
        },
        "binance": {
            "economic_asset": "BTC",
            "symbol": "BTCUSDT",
            "payload": binance_payload,
        },
        "fred": {
            "series_id": "DGS3MO",
            "payload": fred_payload,
        },
    }

    expected = {
        "tiingo": records(
            parse_tiingo_eod_payload("US_EQUITY", "SPY", tiingo_payload),
            TIINGO_COLUMNS,
        ),
        "binance": records(
            parse_binance_klines("BTC", "BTCUSDT", binance_payload),
            BINANCE_COLUMNS,
        ),
        "fred": records(parse_fred_observations("DGS3MO", fred_payload), FRED_COLUMNS),
    }

    with tempfile.TemporaryDirectory(prefix="crossalpha-free-core-") as tmp:
        fixture_path = Path(tmp) / "fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        completed = subprocess.run(
            [str(BINARY), str(fixture_path)],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout)
        actual_raw = json.loads(completed.stdout)

    actual = {
        "tiingo": project(actual_raw["tiingo"], TIINGO_COLUMNS),
        "binance": project(actual_raw["binance"], BINANCE_COLUMNS),
        "fred": project(actual_raw["fred"], FRED_COLUMNS),
    }
    if actual != expected:
        raise AssertionError(
            "Free Core parser parity mismatch\n"
            f"expected={json.dumps(expected, ensure_ascii=False, indent=2)}\n"
            f"actual={json.dumps(actual, ensure_ascii=False, indent=2)}"
        )

    print(
        "ok=true tiingo_rows={} binance_rows={} fred_rows={} data_cost_usd=0".format(
            len(actual["tiingo"]), len(actual["binance"]), len(actual["fred"])
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
