from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from crossalpha.core.free_dataset import audit_free_core, build_free_core_returns
from crossalpha.core.free_provider import (
    FREE_CRYPTO_PROXIES,
    FREE_TRADFI_PROXIES,
    FRED_CASH_SERIES,
    FreeCoreRange,
    parse_binance_klines,
    parse_fred_observations,
    parse_tiingo_eod_payload,
)

REPO = Path(__file__).resolve().parents[1]
BINARY = REPO / "target" / "debug" / "crossalpha-free-core-fixture-rs"
START = "2026-09-02"
END = "2026-09-05"

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
RETURN_COLUMNS = ["date", "economic_asset", "source", "symbol", "price", "return"]


def normalize(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        stamp = value
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        else:
            stamp = stamp.tz_convert("UTC")
        return stamp.isoformat()
    if isinstance(value, np.generic):
        return normalize(value.item())
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return value
    if isinstance(value, str) and (value.endswith("Z") or "+00:00" in value):
        try:
            stamp = pd.Timestamp(value)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            else:
                stamp = stamp.tz_convert("UTC")
            return stamp.isoformat()
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


def schema_signature(path: Path) -> list[tuple[str, str, bool]]:
    return [(field.name, str(field.type), field.nullable) for field in pq.read_schema(path)]


def range_paths(root: Path) -> dict[str, Path]:
    range_dir = Path(f"start={START}", f"end={END}")
    return {
        "tradfi": root / "canonical" / "core" / "free_proxy_daily" / range_dir / "tradfi.parquet",
        "crypto": root / "canonical" / "core" / "free_proxy_daily" / range_dir / "crypto.parquet",
        "cash": root / "canonical" / "core" / "cash_rate" / range_dir / f"{FRED_CASH_SERIES}.parquet",
        "returns": root / "derived" / "core" / "free_v01" / range_dir / "asset_returns.parquet",
        "quality": root / "manifests" / "free_core_quality.json",
    }


def tiingo_payload(base: float, volume_seed: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for offset, day in enumerate(("2026-09-02", "2026-09-03", "2026-09-04")):
        price = base + offset
        result.append(
            {
                "date": f"{day}T00:00:00.000Z",
                "open": price,
                "high": price + 1.5,
                "low": price - 0.5,
                "close": price + 0.75,
                "volume": volume_seed + offset * 1000,
                "adjOpen": price - 1.0,
                "adjHigh": price + 0.5,
                "adjLow": price - 1.5,
                "adjClose": price - 0.5 + offset * 0.1,
                "adjVolume": volume_seed + 500 + offset * 1000,
                "divCash": 0.25 if offset == 0 else 0.0,
                "splitFactor": 1.0,
            }
        )
    return result


def millis(day: str) -> int:
    return int(pd.Timestamp(f"{day}T00:00:00Z").timestamp() * 1000)


def binance_payload(base: float, trade_seed: int) -> list[list[Any]]:
    result: list[list[Any]] = []
    for offset, day in enumerate(("2026-09-02", "2026-09-03", "2026-09-04")):
        open_price = base + offset * 500.0
        result.append(
            [
                millis(day),
                f"{open_price:.1f}",
                f"{open_price + 1200.0:.1f}",
                f"{open_price - 700.0:.1f}",
                f"{open_price + 400.0:.1f}",
                f"{123.5 + offset:.2f}",
                millis(day) + 86_400_000 - 1,
                f"{13_708_500.0 + offset * 100_000.0:.1f}",
                trade_seed + offset,
                f"{63.1 + offset:.2f}",
                f"{7_000_000.0 + offset * 50_000.0:.1f}",
                "0",
            ]
        )
    return result


def build_fixture() -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tiingo_entries: list[dict[str, Any]] = []
    tiingo_frames: list[pd.DataFrame] = []
    for index, (asset, ticker) in enumerate(FREE_TRADFI_PROXIES.items()):
        payload = tiingo_payload(100.0 + index * 20.0, 1_000_000 + index * 100_000)
        tiingo_entries.append(
            {"economic_asset": asset, "ticker": ticker, "payload": payload}
        )
        tiingo_frames.append(parse_tiingo_eod_payload(asset, ticker, payload))

    binance_entries: list[dict[str, Any]] = []
    binance_frames: list[pd.DataFrame] = []
    for index, (asset, symbol) in enumerate(FREE_CRYPTO_PROXIES.items()):
        payload = binance_payload(110_000.0 + index * 5_000.0, 54_321 + index * 10_000)
        binance_entries.append(
            {"economic_asset": asset, "symbol": symbol, "payload": payload}
        )
        binance_frames.append(parse_binance_klines(asset, symbol, payload))

    fred_payload = {
        "observations": [
            {"date": "2026-09-02", "value": "5.20"},
            {"date": "2026-09-03", "value": "."},
            {"date": "2026-09-04", "value": "5.18"},
        ]
    }
    fred_frame = parse_fred_observations(FRED_CASH_SERIES, fred_payload)
    fixture = {
        "tiingo": tiingo_entries,
        "binance": binance_entries,
        "fred": {"series_id": FRED_CASH_SERIES, "payload": fred_payload},
    }
    tradfi = pd.concat(tiingo_frames, ignore_index=True).sort_values(
        ["date", "economic_asset"]
    )
    crypto = pd.concat(binance_frames, ignore_index=True).sort_values(
        ["date", "economic_asset"]
    )
    return fixture, tradfi.reset_index(drop=True), crypto.reset_index(drop=True), fred_frame


def write_python_canonical(
    root: Path, tradfi: pd.DataFrame, crypto: pd.DataFrame, cash: pd.DataFrame
) -> None:
    paths = range_paths(root)
    for key, frame in (("tradfi", tradfi), ("crypto", crypto), ("cash", cash)):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(paths[key], index=False)


def assert_parquet_equal(label: str, expected_path: Path, actual_path: Path, sort: list[str]) -> None:
    expected_schema = schema_signature(expected_path)
    actual_schema = schema_signature(actual_path)
    if actual_schema != expected_schema:
        raise AssertionError(
            f"{label} schema mismatch\nexpected={expected_schema}\nactual={actual_schema}"
        )
    expected = pd.read_parquet(expected_path).sort_values(sort).reset_index(drop=True)
    actual = pd.read_parquet(actual_path).sort_values(sort).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        actual,
        expected,
        check_dtype=False,
        check_exact=False,
        rtol=1e-12,
        atol=1e-15,
        obj=label,
    )


def report_projection(value: dict[str, Any]) -> dict[str, Any]:
    return normalize(
        {
            key: value.get(key)
            for key in (
                "mode",
                "data_cost_usd",
                "range_semantics",
                "canonical_vendor_boundary_policy",
                "rows",
                "assets",
                "coverage",
            )
        }
    )


def main() -> int:
    if not BINARY.exists():
        raise SystemExit(f"missing Rust Free Core fixture binary: {BINARY}")

    fixture, tradfi, crypto, cash = build_fixture()
    expected_parser = {
        "tiingo": records(tradfi, TIINGO_COLUMNS),
        "binance": records(crypto, BINANCE_COLUMNS),
        "fred": records(cash, FRED_COLUMNS),
    }

    with tempfile.TemporaryDirectory(prefix="crossalpha-free-core-") as tmp:
        root = Path(tmp)
        python_root = root / "python"
        rust_root = root / "rust"
        fixture_path = root / "fixture.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        write_python_canonical(python_root, tradfi, crypto, cash)
        core_range = FreeCoreRange(START, END)
        python_quality = audit_free_core(python_root, core_range)
        if not python_quality.get("ok"):
            raise AssertionError(f"Python fixture quality unexpectedly failed: {python_quality}")
        python_returns = build_free_core_returns(python_root, core_range)

        completed = subprocess.run(
            [
                str(BINARY),
                str(fixture_path),
                "--output-root",
                str(rust_root),
                "--start",
                START,
                "--end",
                END,
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr or completed.stdout)
        actual_raw = json.loads(completed.stdout)

        actual_parser = {
            "tiingo": project(actual_raw["tiingo"], TIINGO_COLUMNS),
            "binance": project(actual_raw["binance"], BINANCE_COLUMNS),
            "fred": project(actual_raw["fred"], FRED_COLUMNS),
        }
        if actual_parser != expected_parser:
            raise AssertionError(
                "Free Core parser parity mismatch\n"
                f"expected={json.dumps(expected_parser, ensure_ascii=False, indent=2)}\n"
                f"actual={json.dumps(actual_parser, ensure_ascii=False, indent=2)}"
            )

        python_paths = range_paths(python_root)
        rust_paths = range_paths(rust_root)
        assert_parquet_equal(
            "tradfi canonical", python_paths["tradfi"], rust_paths["tradfi"], ["date", "economic_asset"]
        )
        assert_parquet_equal(
            "crypto canonical", python_paths["crypto"], rust_paths["crypto"], ["date", "economic_asset"]
        )
        assert_parquet_equal("cash canonical", python_paths["cash"], rust_paths["cash"], ["date"])
        assert_parquet_equal(
            "asset returns", python_paths["returns"], rust_paths["returns"], ["date", "economic_asset"]
        )

        rust_quality = json.loads(rust_paths["quality"].read_text(encoding="utf-8"))
        if normalize(rust_quality) != normalize(python_quality):
            raise AssertionError(
                "Free Core quality parity mismatch\n"
                f"python={json.dumps(normalize(python_quality), ensure_ascii=False, indent=2)}\n"
                f"rust={json.dumps(normalize(rust_quality), ensure_ascii=False, indent=2)}"
            )

        rust_returns = actual_raw["pipeline"]["returns"]
        if report_projection(rust_returns) != report_projection(python_returns):
            raise AssertionError(
                "Free Core returns report mismatch\n"
                f"python={json.dumps(report_projection(python_returns), ensure_ascii=False, indent=2)}\n"
                f"rust={json.dumps(report_projection(rust_returns), ensure_ascii=False, indent=2)}"
            )

        cash_returns = pd.read_parquet(rust_paths["returns"])
        cash_returns = cash_returns.loc[cash_returns["economic_asset"] == "CASH"].set_index("date")
        sep2 = pd.Timestamp("2026-09-02T00:00:00Z")
        sep3 = pd.Timestamp("2026-09-03T00:00:00Z")
        sep4 = pd.Timestamp("2026-09-04T00:00:00Z")
        if not pd.isna(cash_returns.loc[sep2, "return"]):
            raise AssertionError("CASH first-day return must be null without a prior-known FRED rate")
        expected_520 = (1.0 + 5.20 / 100.0) ** (1.0 / 365.0) - 1.0
        if not math.isclose(cash_returns.loc[sep3, "return"], expected_520, rel_tol=1e-12):
            raise AssertionError("CASH Sep-03 return did not use Sep-02 known rate")
        if not math.isclose(cash_returns.loc[sep4, "return"], expected_520, rel_tol=1e-12):
            raise AssertionError("CASH Sep-04 return incorrectly used same-day Sep-04 rate")

    print(
        "ok=true tiingo_rows={} binance_rows={} fred_rows={} schemas=4 quality=true returns=true cash_known_rate=strict_prior data_cost_usd=0".format(
            len(expected_parser["tiingo"]),
            len(expected_parser["binance"]),
            len(expected_parser["fred"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
