from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


ROLLING_MIN_PERIODS = 24


def _zscore(series: pd.Series, *, window: str = "24h", min_periods: int = ROLLING_MIN_PERIODS) -> pd.Series:
    rolling = series.rolling(window, min_periods=min_periods)
    mean = rolling.mean()
    std = rolling.std(ddof=0).mask(lambda value: value == 0.0)
    return (series - mean) / std


def compute_hyperliquid_market_state(frame: pd.DataFrame) -> pd.DataFrame:
    """Create causal descriptive market-state features from canonical asset contexts.

    This layer is intentionally descriptive. It does not emit BUY/SELL signals or a
    composite risk score. Rolling statistics only use observations available at or
    before each row's `observed_at`, which keeps the feature history point-in-time safe.
    """
    required = {
        "observed_at",
        "known_at",
        "asset",
        "mark_price",
        "oracle_price",
        "mid_price",
        "prev_day_price",
        "premium",
        "funding_rate",
        "open_interest",
        "day_notional_volume",
        "impact_bid",
        "impact_ask",
        "raw_sha256",
        "raw_path",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"canonical Hyperliquid frame missing columns: {missing}")

    out = frame.copy()
    out["observed_at"] = pd.to_datetime(out["observed_at"], utc=True)
    out["known_at"] = pd.to_datetime(out["known_at"], utc=True)
    out = out.sort_values(["asset", "observed_at", "known_at"]).reset_index(drop=True)

    numeric_cols = [
        "mark_price",
        "oracle_price",
        "mid_price",
        "prev_day_price",
        "premium",
        "funding_rate",
        "open_interest",
        "day_notional_volume",
        "impact_bid",
        "impact_ask",
    ]
    for column in numeric_cols:
        out[column] = pd.to_numeric(out[column], errors="coerce")

    valid_oracle = out["oracle_price"].where(out["oracle_price"] > 0)
    valid_prev_day = out["prev_day_price"].where(out["prev_day_price"] > 0)
    impact_mid = (out["impact_bid"] + out["impact_ask"]) / 2.0
    impact_mid = impact_mid.where(impact_mid > 0)

    out["mark_oracle_basis_bps"] = (out["mark_price"] / valid_oracle - 1.0) * 10_000.0
    out["impact_spread_bps"] = (out["impact_ask"] - out["impact_bid"]) / impact_mid * 10_000.0
    out["day_return"] = out["mark_price"] / valid_prev_day - 1.0
    out["funding_bps"] = out["funding_rate"] * 10_000.0
    out["premium_bps"] = out["premium"] * 10_000.0
    out["open_interest_notional"] = out["open_interest"] * out["mark_price"]

    grouped = out.groupby("asset", sort=False, group_keys=False)
    out["observation_interval_seconds"] = grouped["observed_at"].diff().dt.total_seconds()
    out["open_interest_change_pct"] = grouped["open_interest"].pct_change(fill_method=None)
    out["open_interest_notional_change_pct"] = grouped["open_interest_notional"].pct_change(fill_method=None)
    out["funding_change"] = grouped["funding_rate"].diff()
    out["basis_change_bps"] = grouped["mark_oracle_basis_bps"].diff()

    feature_parts: list[pd.DataFrame] = []
    for asset, part in out.groupby("asset", sort=False):
        part = part.sort_values("observed_at").copy()
        part = part.set_index("observed_at", drop=False)
        part["funding_z_24h"] = _zscore(part["funding_rate"])
        part["basis_z_24h"] = _zscore(part["mark_oracle_basis_bps"])
        part["oi_change_z_24h"] = _zscore(part["open_interest_change_pct"])
        part["spread_z_24h"] = _zscore(part["impact_spread_bps"])
        part["rolling_observations_24h"] = (
            part["mark_price"].rolling("24h", min_periods=1).count().astype("int64")
        )
        part["asset"] = asset
        feature_parts.append(part.reset_index(drop=True))

    result = pd.concat(feature_parts, ignore_index=True) if feature_parts else out.iloc[0:0].copy()
    result["feature_schema_version"] = 1
    result = result.sort_values(["observed_at", "asset"]).reset_index(drop=True)
    return result


def _partition_day(path: Path) -> date:
    day_value = int(path.parent.name.split("=", 1)[1])
    month_value = int(path.parent.parent.name.split("=", 1)[1])
    year_value = int(path.parent.parent.parent.name.split("=", 1)[1])
    return date(year_value, month_value, day_value)


def _day_dir(root: Path, value: date) -> Path:
    return root / f"year={value:%Y}" / f"month={value:%m}" / f"day={value:%d}"


def _latest_canonical_day_from_series_state(data_root: Path) -> date:
    path = data_root / "manifests" / "series" / "hyperliquid" / "metaAndAssetCtxs.json"
    if not path.exists():
        raise FileNotFoundError(f"Hyperliquid series state missing: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    latest = state.get("latest_observed_at")
    if not isinstance(latest, str):
        raise ValueError("Hyperliquid series state has no latest_observed_at")
    latest_dt = datetime.fromisoformat(latest)
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    return latest_dt.astimezone(timezone.utc).date()


def _write_market_state_day(
    data_root: Path,
    current_day: date,
    input_files: list[Path],
) -> int:
    if not input_files:
        raise ValueError(f"no canonical input files for market-state day {current_day}")
    input_frame = pd.concat((pd.read_parquet(path) for path in input_files), ignore_index=True)
    features = compute_hyperliquid_market_state(input_frame)
    current_mask = features["observed_at"].dt.date == current_day
    current = features.loc[current_mask].copy()
    if current.empty:
        raise ValueError(f"market-state materialization produced zero rows for {current_day}")

    output_root = data_root / "derived" / "hyperliquid" / "market_state"
    out_dir = _day_dir(output_root, current_day)
    out_path = out_dir / "market_state.parquet"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    current.to_parquet(tmp, index=False)
    tmp.replace(out_path)
    return len(current)


def build_hyperliquid_market_state(
    data_root: Path,
    *,
    recent_only: bool = False,
) -> dict[str, int | str]:
    """Materialize market-state parquet from canonical Hyperliquid snapshots.

    Full mode is for explicit rebuilds. Online mode reads only the latest canonical day
    plus its previous day for causal 24h lookback, then rewrites only the latest day's
    derived partition. Runtime therefore stays bounded as history grows.
    """
    source_root = data_root / "canonical" / "hyperliquid" / "asset_contexts"
    if not source_root.exists():
        return {
            "mode": "recent" if recent_only else "full",
            "source_files": 0,
            "days": 0,
            "written_days": 0,
            "skipped_days": 0,
            "rows_written": 0,
        }

    if recent_only:
        latest_day = _latest_canonical_day_from_series_state(data_root)
        previous_day = latest_day - timedelta(days=1)
        previous_files = sorted(_day_dir(source_root, previous_day).glob("*.parquet"))
        current_files = sorted(_day_dir(source_root, latest_day).glob("*.parquet"))
        input_files = previous_files + current_files
        rows = _write_market_state_day(data_root, latest_day, input_files)
        return {
            "mode": "recent",
            "source_files": len(input_files),
            "days": 1,
            "written_days": 1,
            "skipped_days": 0,
            "rows_written": rows,
        }

    files = sorted(source_root.glob("**/*.parquet"))
    if not files:
        return {
            "mode": "full",
            "source_files": 0,
            "days": 0,
            "written_days": 0,
            "skipped_days": 0,
            "rows_written": 0,
        }

    by_day: dict[date, list[Path]] = defaultdict(list)
    for path in files:
        by_day[_partition_day(path)].append(path)
    days = sorted(by_day)
    latest_day = days[-1]

    output_root = data_root / "derived" / "hyperliquid" / "market_state"
    written_days = 0
    skipped_days = 0
    rows_written = 0

    for current_day in days:
        out_path = _day_dir(output_root, current_day) / "market_state.parquet"
        if out_path.exists() and current_day != latest_day:
            skipped_days += 1
            continue
        previous_day = current_day - timedelta(days=1)
        input_files = list(by_day.get(previous_day, [])) + list(by_day[current_day])
        rows_written += _write_market_state_day(data_root, current_day, input_files)
        written_days += 1

    return {
        "mode": "full",
        "source_files": len(files),
        "days": len(days),
        "written_days": written_days,
        "skipped_days": skipped_days,
        "rows_written": rows_written,
    }
