from __future__ import annotations

from pathlib import Path

import pandas as pd


def _class_code(value: object) -> str:
    raw = getattr(value, "value", value)
    if isinstance(raw, int) and 0 <= raw <= 255:
        try:
            return chr(raw)
        except ValueError:
            pass
    text = str(raw)
    if text.endswith(".FUTURE"):
        return "F"
    return text


def normalize_parent_futures_daily(
    bars: pd.DataFrame,
    definitions: pd.DataFrame,
    *,
    bar_time_col: str = "ts_event",
    definition_time_col: str = "ts_recv",
) -> pd.DataFrame:
    """Join parent OHLCV bars to the latest definition known at each bar timestamp.

    Parent requests contain futures spreads as well as outrights. Definitions are joined
    point-in-time by instrument ID and the result is filtered to `instrument_class=F`.
    This avoids using a future definition revision to label an earlier daily bar.
    """
    required_bars = {
        bar_time_col,
        "instrument_id",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    required_defs = {
        definition_time_col,
        "instrument_id",
        "raw_symbol",
        "expiration",
        "instrument_class",
    }
    missing_bars = sorted(required_bars - set(bars.columns))
    missing_defs = sorted(required_defs - set(definitions.columns))
    if missing_bars:
        raise ValueError(f"parent bars missing columns: {missing_bars}")
    if missing_defs:
        raise ValueError(f"definitions missing columns: {missing_defs}")

    bars = bars.copy()
    defs = definitions.copy()
    bars[bar_time_col] = pd.to_datetime(bars[bar_time_col], utc=True)
    defs[definition_time_col] = pd.to_datetime(defs[definition_time_col], utc=True)
    defs["expiration"] = pd.to_datetime(defs["expiration"], utc=True)

    if bars[[bar_time_col, "instrument_id"]].isna().any().any():
        raise ValueError("parent bars contain missing timestamp/instrument_id")
    if defs[[definition_time_col, "instrument_id"]].isna().any().any():
        raise ValueError("definitions contain missing timestamp/instrument_id")

    joined_parts: list[pd.DataFrame] = []
    definition_columns = [
        definition_time_col,
        "instrument_id",
        "raw_symbol",
        "expiration",
        "instrument_class",
    ]
    for optional in (
        "asset",
        "contract_multiplier",
        "min_price_increment",
        "activation",
        "currency",
        "settl_currency",
    ):
        if optional in defs.columns:
            definition_columns.append(optional)

    for instrument_id, bar_part in bars.groupby("instrument_id", sort=False):
        def_part = defs.loc[defs["instrument_id"] == instrument_id, definition_columns].copy()
        if def_part.empty:
            # Parent OHLCV includes spreads; unmatched records are intentionally dropped.
            continue
        bar_part = bar_part.sort_values(bar_time_col)
        def_part = def_part.sort_values(definition_time_col)
        joined = pd.merge_asof(
            bar_part,
            def_part,
            left_on=bar_time_col,
            right_on=definition_time_col,
            by="instrument_id",
            direction="backward",
            allow_exact_matches=True,
        )
        joined_parts.append(joined)

    if not joined_parts:
        raise ValueError("no parent bars could be joined to definitions")

    joined = pd.concat(joined_parts, ignore_index=True)
    joined["instrument_class_code"] = joined["instrument_class"].map(_class_code)
    joined = joined.loc[joined["instrument_class_code"] == "F"].copy()
    joined = joined.loc[joined["raw_symbol"].notna() & joined["expiration"].notna()].copy()
    if joined.empty:
        raise ValueError("no outright futures remained after definition join")

    joined["date"] = joined[bar_time_col]
    joined["contract"] = joined["raw_symbol"].astype(str)
    joined["expiration_date"] = joined["expiration"]
    joined["definition_known_at"] = joined[definition_time_col]

    output_columns = [
        "date",
        "contract",
        "instrument_id",
        "expiration_date",
        "definition_known_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    for optional in (
        "asset",
        "contract_multiplier",
        "min_price_increment",
        "activation",
        "currency",
        "settl_currency",
    ):
        if optional in joined.columns:
            output_columns.append(optional)

    result = joined[output_columns].sort_values(["date", "contract"]).reset_index(drop=True)
    if result.duplicated(["date", "contract"]).any():
        raise ValueError("normalized futures contain duplicate date/contract rows")
    if result[["open", "high", "low", "close"]].isna().any().any():
        raise ValueError("normalized futures contain missing OHLC values")
    if (pd.to_numeric(result["volume"], errors="coerce") < 0).any():
        raise ValueError("normalized futures contain negative volume")
    return result


def normalize_parent_futures_files(
    definitions_path: Path,
    bars_path: Path,
    output_path: Path,
) -> Path:
    definitions = pd.read_parquet(definitions_path)
    bars = pd.read_parquet(bars_path)
    normalized = normalize_parent_futures_daily(bars, definitions)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".parquet.tmp")
    normalized.to_parquet(tmp, index=False)
    tmp.replace(output_path)
    return output_path
