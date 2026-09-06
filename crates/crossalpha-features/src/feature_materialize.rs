use crate::canonical::hyperliquid::parse_meta_and_asset_contexts;
use crate::canonical::stablecoins::parse_stablecoin_snapshot;
use crate::canonical::load_envelope;
use crate::feature_parquet::write_struct_rows;
use crate::market_state::{HyperliquidMarketStateRow, compute_hyperliquid_market_state};
use crate::stablecoin_state::{
    StablecoinChainStateRow, StablecoinSystemStateRow, compute_stablecoin_system_state,
};
use anyhow::{Context, Result, bail};
use chrono::{Datelike, NaiveDate};
use crossalpha_storage::load_recent_daily_manifests;
use serde::Serialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize)]
pub struct FeatureMaterializeReport {
    pub recent_days: usize,
    pub manifest_records: usize,
    pub hyperliquid_source_snapshots: usize,
    pub stablecoin_source_snapshots: usize,
    pub hyperliquid_rows_written: usize,
    pub stablecoin_system_rows_written: usize,
    pub stablecoin_chain_rows_written: usize,
    pub hyperliquid_output: Option<PathBuf>,
    pub stablecoin_system_output: Option<PathBuf>,
    pub stablecoin_chain_output: Option<PathBuf>,
}

pub fn materialize_recent_features(
    data_root: &Path,
    output_root: &Path,
    recent_days: usize,
) -> Result<FeatureMaterializeReport> {
    let recent = load_recent_daily_manifests(data_root, recent_days)?;
    if !recent.errors.is_empty() {
        bail!("recent daily manifests contain errors: {:?}", recent.errors);
    }
    let mut hyper_snapshots = 0_usize;
    let mut stable_snapshots = 0_usize;
    let mut hyper_rows = Vec::new();
    let mut stable_assets = Vec::new();
    let mut stable_chains = Vec::new();

    for record in &recent.records {
        match (record.source_id.as_str(), record.observation_type.as_str()) {
            ("hyperliquid", "metaAndAssetCtxs") => {
                let envelope = load_envelope(record)?;
                hyper_rows.extend(parse_meta_and_asset_contexts(&envelope, record)?);
                hyper_snapshots += 1;
            }
            ("defillama", "stablecoins_snapshot") => {
                let envelope = load_envelope(record)?;
                let parsed = parse_stablecoin_snapshot(&envelope, record)?;
                stable_assets.extend(parsed.assets);
                stable_chains.extend(parsed.chains);
                stable_snapshots += 1;
            }
            _ => {}
        }
    }

    let mut report = FeatureMaterializeReport {
        recent_days,
        manifest_records: recent.records.len(),
        hyperliquid_source_snapshots: hyper_snapshots,
        stablecoin_source_snapshots: stable_snapshots,
        hyperliquid_rows_written: 0,
        stablecoin_system_rows_written: 0,
        stablecoin_chain_rows_written: 0,
        hyperliquid_output: None,
        stablecoin_system_output: None,
        stablecoin_chain_output: None,
    };

    if !hyper_rows.is_empty() {
        let latest_day = hyper_rows
            .iter()
            .map(|row| row.observed_at.date_naive())
            .max()
            .context("Hyperliquid latest day missing")?;
        let features = compute_hyperliquid_market_state(&hyper_rows);
        let current: Vec<HyperliquidMarketStateRow> = features
            .into_iter()
            .filter(|row| row.observed_at.date_naive() == latest_day)
            .collect();
        if current.is_empty() {
            bail!("market-state materialization produced zero rows for {latest_day}");
        }
        let path = day_dir(
            &output_root.join("derived/hyperliquid/market_state"),
            latest_day,
        )
        .join("market_state.parquet");
        write_struct_rows(&current, HYPERLIQUID_FEATURE_COLUMNS, &path)?;
        report.hyperliquid_rows_written = current.len();
        report.hyperliquid_output = Some(path);
    }

    if !stable_assets.is_empty() && !stable_chains.is_empty() {
        let latest_day = stable_assets
            .iter()
            .map(|row| row.observed_at.date_naive())
            .max()
            .context("stablecoin latest day missing")?;
        let (system, chains) = compute_stablecoin_system_state(&stable_assets, &stable_chains);
        let current_system: Vec<StablecoinSystemStateRow> = system
            .into_iter()
            .filter(|row| row.observed_at.date_naive() == latest_day)
            .collect();
        let current_chains: Vec<StablecoinChainStateRow> = chains
            .into_iter()
            .filter(|row| row.observed_at.date_naive() == latest_day)
            .collect();
        if current_system.is_empty() || current_chains.is_empty() {
            bail!("stablecoin state materialization produced zero rows for {latest_day}");
        }
        let system_path = day_dir(
            &output_root.join("derived/stablecoins/system_state"),
            latest_day,
        )
        .join("stablecoin_system_state.parquet");
        let chain_path = day_dir(
            &output_root.join("derived/stablecoins/chain_state"),
            latest_day,
        )
        .join("stablecoin_chain_state.parquet");
        write_struct_rows(&current_system, STABLECOIN_SYSTEM_COLUMNS, &system_path)?;
        write_struct_rows(&current_chains, STABLECOIN_CHAIN_COLUMNS, &chain_path)?;
        report.stablecoin_system_rows_written = current_system.len();
        report.stablecoin_chain_rows_written = current_chains.len();
        report.stablecoin_system_output = Some(system_path);
        report.stablecoin_chain_output = Some(chain_path);
    }
    Ok(report)
}

fn day_dir(root: &Path, day: NaiveDate) -> PathBuf {
    root.join(format!("year={:04}", day.year()))
        .join(format!("month={:02}", day.month()))
        .join(format!("day={:02}", day.day()))
}

const HYPERLIQUID_FEATURE_COLUMNS: &[&str] = &[
    "observed_at", "known_at", "asset", "sz_decimals", "max_leverage", "only_isolated",
    "mark_price", "oracle_price", "mid_price", "prev_day_price", "premium", "funding_rate",
    "open_interest", "day_notional_volume", "day_base_volume", "impact_bid", "impact_ask",
    "raw_sha256", "raw_path", "mark_oracle_basis_bps", "impact_spread_bps", "day_return",
    "funding_bps", "premium_bps", "open_interest_notional", "observation_interval_seconds",
    "open_interest_change_pct", "open_interest_notional_change_pct", "funding_change",
    "basis_change_bps", "funding_z_24h", "basis_z_24h", "oi_change_z_24h", "spread_z_24h",
    "rolling_observations_24h", "feature_schema_version",
];

const STABLECOIN_SYSTEM_COLUMNS: &[&str] = &[
    "observed_at", "known_at", "usd_stablecoin_count", "usd_supply_native",
    "usd_market_value_usd", "usd_delta_1d_native", "usd_delta_7d_native",
    "usd_delta_30d_native", "delta_1d_market_value_coverage", "delta_7d_market_value_coverage",
    "delta_30d_market_value_coverage", "usdt_market_value_usd", "usdc_market_value_usd",
    "usdt_share", "usdc_share", "asset_hhi", "weighted_abs_peg_deviation_bps",
    "max_abs_peg_deviation_bps", "offpeg_50bps_market_value_usd", "chain_sum_native",
    "chain_coverage_ratio", "chain_residual_native", "chain_abs_residual_native",
    "chain_abs_residual_ratio", "feature_schema_version",
];

const STABLECOIN_CHAIN_COLUMNS: &[&str] = &[
    "observed_at", "known_at", "chain", "circulating_native", "market_value_usd",
    "market_share", "stablecoin_count", "system_chain_hhi", "feature_schema_version",
];
