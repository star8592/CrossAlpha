from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd


def _hhi(values: pd.Series) -> float | None:
    values = pd.to_numeric(values, errors="coerce").dropna()
    values = values[values > 0]
    total = float(values.sum())
    if total <= 0:
        return None
    shares = values / total
    return float((shares * shares).sum())


def _partition_day(path: Path) -> date:
    day_value = int(path.parent.name.split("=", 1)[1])
    month_value = int(path.parent.parent.name.split("=", 1)[1])
    year_value = int(path.parent.parent.parent.name.split("=", 1)[1])
    return date(year_value, month_value, day_value)


def _day_dir(root: Path, value: date) -> Path:
    return root / f"year={value:%Y}" / f"month={value:%m}" / f"day={value:%d}"


def _latest_stablecoin_day_from_series_state(data_root: Path) -> date:
    path = data_root / "manifests" / "series" / "defillama" / "stablecoins_snapshot.json"
    if not path.exists():
        raise FileNotFoundError(f"DefiLlama series state missing: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    latest = state.get("latest_observed_at")
    if not isinstance(latest, str):
        raise ValueError("DefiLlama series state has no latest_observed_at")
    latest_dt = datetime.fromisoformat(latest)
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    return latest_dt.astimezone(timezone.utc).date()


def _known_sum(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return float(values.sum()) if not values.empty else None


def _market_value_coverage(part: pd.DataFrame, column: str, total_market_value: float) -> float | None:
    if total_market_value <= 0:
        return None
    known = part[column].notna()
    covered_value = float(part.loc[known, "market_value_usd"].fillna(0.0).sum())
    return covered_value / total_market_value


def compute_stablecoin_system_state(
    assets: pd.DataFrame,
    chains: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate point-in-time USD stablecoin stock and chain distribution state.

    This is an accounting/descriptive layer, not a liquidity or trading signal. Only
    `peggedUSD` assets are aggregated into the USD system totals. Chain sums are kept
    separate from asset totals so coverage/residuals remain visible instead of silently
    forcing conservation when the upstream provider's chain coverage may be incomplete.
    Missing historical deltas remain unknown and are accompanied by coverage ratios.
    """
    asset_required = {
        "observed_at",
        "known_at",
        "stablecoin_id",
        "symbol",
        "peg_type",
        "price_usd",
        "circulating_native",
        "delta_1d_native",
        "delta_7d_native",
        "delta_30d_native",
        "market_value_usd",
        "peg_deviation_bps",
        "raw_sha256",
    }
    chain_required = {
        "observed_at",
        "known_at",
        "stablecoin_id",
        "symbol",
        "peg_type",
        "chain",
        "circulating_native",
        "market_value_usd",
        "raw_sha256",
    }
    missing_assets = sorted(asset_required - set(assets.columns))
    missing_chains = sorted(chain_required - set(chains.columns))
    if missing_assets:
        raise ValueError(f"stablecoin asset frame missing columns: {missing_assets}")
    if missing_chains:
        raise ValueError(f"stablecoin chain frame missing columns: {missing_chains}")

    assets = assets.copy()
    chains = chains.copy()
    for frame in (assets, chains):
        frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True)
        frame["known_at"] = pd.to_datetime(frame["known_at"], utc=True)

    usd_assets = assets.loc[assets["peg_type"] == "peggedUSD"].copy()
    usd_chains = chains.loc[chains["peg_type"] == "peggedUSD"].copy()
    for column in (
        "price_usd",
        "circulating_native",
        "delta_1d_native",
        "delta_7d_native",
        "delta_30d_native",
        "market_value_usd",
        "peg_deviation_bps",
    ):
        usd_assets[column] = pd.to_numeric(usd_assets[column], errors="coerce")
    for column in ("circulating_native", "market_value_usd"):
        usd_chains[column] = pd.to_numeric(usd_chains[column], errors="coerce")

    chain_by_asset = (
        usd_chains.groupby(["observed_at", "stablecoin_id"], dropna=False)["circulating_native"]
        .sum(min_count=1)
        .rename("chain_sum_native")
        .reset_index()
    )
    asset_with_chain = usd_assets.merge(
        chain_by_asset,
        on=["observed_at", "stablecoin_id"],
        how="left",
    )
    # No chain rows means zero observed chain coverage, not zero residual.
    asset_with_chain["chain_sum_native"] = asset_with_chain["chain_sum_native"].fillna(0.0)
    asset_with_chain["chain_residual_native"] = (
        asset_with_chain["chain_sum_native"] - asset_with_chain["circulating_native"]
    )
    asset_with_chain["chain_coverage_ratio"] = (
        asset_with_chain["chain_sum_native"]
        / asset_with_chain["circulating_native"].where(asset_with_chain["circulating_native"] > 0)
    )

    system_rows: list[dict[str, object]] = []
    for observed_at, part in asset_with_chain.groupby("observed_at", sort=True):
        market_values = part["market_value_usd"].fillna(0.0)
        total_market_value = float(market_values.sum())
        total_supply_native = float(part["circulating_native"].fillna(0.0).sum())
        chain_sum_native = float(part["chain_sum_native"].sum())
        residual_native = chain_sum_native - total_supply_native
        abs_residual_native = float(part["chain_residual_native"].abs().fillna(0.0).sum())

        def _symbol_value(symbol: str) -> float | None:
            values = part.loc[part["symbol"] == symbol, "market_value_usd"].dropna()
            return float(values.sum()) if not values.empty else None

        usdt = _symbol_value("USDT")
        usdc = _symbol_value("USDC")
        abs_peg = part["peg_deviation_bps"].abs()
        weighted_abs_peg = None
        valid_weight = market_values.where(abs_peg.notna(), 0.0)
        if float(valid_weight.sum()) > 0:
            weighted_abs_peg = float((abs_peg.fillna(0.0) * valid_weight).sum() / valid_weight.sum())

        delta_1d = _known_sum(part["delta_1d_native"])
        delta_7d = _known_sum(part["delta_7d_native"])
        delta_30d = _known_sum(part["delta_30d_native"])
        offpeg_50 = part.loc[abs_peg >= 50.0, "market_value_usd"].fillna(0.0)
        known_at = part["known_at"].max()
        system_rows.append(
            {
                "observed_at": observed_at,
                "known_at": known_at,
                "usd_stablecoin_count": int(part["stablecoin_id"].nunique()),
                "usd_supply_native": total_supply_native,
                "usd_market_value_usd": total_market_value,
                "usd_delta_1d_native": delta_1d,
                "usd_delta_7d_native": delta_7d,
                "usd_delta_30d_native": delta_30d,
                "delta_1d_market_value_coverage": _market_value_coverage(part, "delta_1d_native", total_market_value),
                "delta_7d_market_value_coverage": _market_value_coverage(part, "delta_7d_native", total_market_value),
                "delta_30d_market_value_coverage": _market_value_coverage(part, "delta_30d_native", total_market_value),
                "usdt_market_value_usd": usdt,
                "usdc_market_value_usd": usdc,
                "usdt_share": usdt / total_market_value if usdt is not None and total_market_value > 0 else None,
                "usdc_share": usdc / total_market_value if usdc is not None and total_market_value > 0 else None,
                "asset_hhi": _hhi(part["market_value_usd"]),
                "weighted_abs_peg_deviation_bps": weighted_abs_peg,
                "max_abs_peg_deviation_bps": float(abs_peg.max()) if abs_peg.notna().any() else None,
                "offpeg_50bps_market_value_usd": float(offpeg_50.sum()),
                "chain_sum_native": chain_sum_native,
                "chain_coverage_ratio": chain_sum_native / total_supply_native if total_supply_native > 0 else None,
                "chain_residual_native": residual_native,
                "chain_abs_residual_native": abs_residual_native,
                "chain_abs_residual_ratio": abs_residual_native / total_supply_native if total_supply_native > 0 else None,
                "feature_schema_version": 1,
            }
        )

    chain_rows: list[dict[str, object]] = []
    for observed_at, part in usd_chains.groupby("observed_at", sort=True):
        grouped = (
            part.groupby("chain", dropna=False)
            .agg(
                circulating_native=("circulating_native", "sum"),
                market_value_usd=("market_value_usd", "sum"),
                stablecoin_count=("stablecoin_id", "nunique"),
                known_at=("known_at", "max"),
            )
            .reset_index()
        )
        total_market = float(grouped["market_value_usd"].fillna(0.0).sum())
        chain_hhi = _hhi(grouped["market_value_usd"])
        for row in grouped.itertuples(index=False):
            value = float(row.market_value_usd) if pd.notna(row.market_value_usd) else None
            chain_rows.append(
                {
                    "observed_at": observed_at,
                    "known_at": row.known_at,
                    "chain": row.chain,
                    "circulating_native": (
                        float(row.circulating_native) if pd.notna(row.circulating_native) else None
                    ),
                    "market_value_usd": value,
                    "market_share": value / total_market if value is not None and total_market > 0 else None,
                    "stablecoin_count": int(row.stablecoin_count),
                    "system_chain_hhi": chain_hhi,
                    "feature_schema_version": 1,
                }
            )

    system = pd.DataFrame(system_rows).sort_values("observed_at").reset_index(drop=True)
    chain_state = pd.DataFrame(chain_rows).sort_values(
        ["observed_at", "market_value_usd"], ascending=[True, False]
    ).reset_index(drop=True)
    return system, chain_state


def _write_state_day(data_root: Path, current_day: date, asset_files: list[Path], chain_files: list[Path]) -> tuple[int, int]:
    if not asset_files or not chain_files:
        raise ValueError(f"missing canonical stablecoin inputs for {current_day}")
    assets = pd.concat((pd.read_parquet(path) for path in asset_files), ignore_index=True)
    chains = pd.concat((pd.read_parquet(path) for path in chain_files), ignore_index=True)
    system, chain_state = compute_stablecoin_system_state(assets, chains)

    output_system_root = data_root / "derived" / "stablecoins" / "system_state"
    output_chain_root = data_root / "derived" / "stablecoins" / "chain_state"
    sys_dir = _day_dir(output_system_root, current_day)
    chn_dir = _day_dir(output_chain_root, current_day)
    sys_dir.mkdir(parents=True, exist_ok=True)
    chn_dir.mkdir(parents=True, exist_ok=True)
    sys_path = sys_dir / "stablecoin_system_state.parquet"
    chn_path = chn_dir / "stablecoin_chain_state.parquet"
    sys_tmp = sys_path.with_suffix(".parquet.tmp")
    chn_tmp = chn_path.with_suffix(".parquet.tmp")
    system.to_parquet(sys_tmp, index=False)
    chain_state.to_parquet(chn_tmp, index=False)
    sys_tmp.replace(sys_path)
    chn_tmp.replace(chn_path)
    return len(system), len(chain_state)


def build_stablecoin_state(data_root: Path, *, recent_only: bool = False) -> dict[str, int | str]:
    asset_root = data_root / "canonical" / "defillama" / "stablecoin_assets"
    chain_root = data_root / "canonical" / "defillama" / "stablecoin_chain_supply"
    if not asset_root.exists() or not chain_root.exists():
        return {
            "mode": "recent" if recent_only else "full",
            "asset_source_files": 0,
            "chain_source_files": 0,
            "days": 0,
            "written_days": 0,
            "rows_written": 0,
            "chain_rows_written": 0,
        }

    if recent_only:
        latest_day = _latest_stablecoin_day_from_series_state(data_root)
        asset_files = sorted(_day_dir(asset_root, latest_day).glob("*.parquet"))
        chain_files = sorted(_day_dir(chain_root, latest_day).glob("*.parquet"))
        rows, chain_rows = _write_state_day(data_root, latest_day, asset_files, chain_files)
        return {
            "mode": "recent",
            "asset_source_files": len(asset_files),
            "chain_source_files": len(chain_files),
            "days": 1,
            "written_days": 1,
            "rows_written": rows,
            "chain_rows_written": chain_rows,
        }

    asset_files = sorted(asset_root.glob("**/*.parquet"))
    chain_files = sorted(chain_root.glob("**/*.parquet"))
    if not asset_files or not chain_files:
        return {
            "mode": "full",
            "asset_source_files": len(asset_files),
            "chain_source_files": len(chain_files),
            "days": 0,
            "written_days": 0,
            "rows_written": 0,
            "chain_rows_written": 0,
        }

    assets_by_day: dict[date, list[Path]] = defaultdict(list)
    chains_by_day: dict[date, list[Path]] = defaultdict(list)
    for path in asset_files:
        assets_by_day[_partition_day(path)].append(path)
    for path in chain_files:
        chains_by_day[_partition_day(path)].append(path)
    days = sorted(set(assets_by_day) & set(chains_by_day))

    written_days = 0
    rows_written = 0
    chain_rows_written = 0
    for current_day in days:
        rows, chain_rows = _write_state_day(
            data_root,
            current_day,
            assets_by_day[current_day],
            chains_by_day[current_day],
        )
        written_days += 1
        rows_written += rows
        chain_rows_written += chain_rows

    return {
        "mode": "full",
        "asset_source_files": len(asset_files),
        "chain_source_files": len(chain_files),
        "days": len(days),
        "written_days": written_days,
        "rows_written": rows_written,
        "chain_rows_written": chain_rows_written,
    }
