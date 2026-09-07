use crate::{
    AaveLiquidationRow, AaveMarketRow, CanonicalSource, HyperliquidMarketStateRow,
    StablecoinChainStateRow, StablecoinSystemStateRow, compute_hyperliquid_market_state,
    compute_stablecoin_system_state, load_envelope, parse_aave_liquidations, parse_aave_markets,
    parse_meta_and_asset_contexts, parse_stablecoin_snapshot,
};
use anyhow::{Result, bail};
use crossalpha_storage::{RawSnapshotManifest, load_recent_daily_manifests};
use std::collections::BTreeSet;
use std::path::Path;

pub fn compute_recent_hyperliquid_market_state(
    data_root: &Path,
    recent_days: usize,
    assets: &[String],
) -> Result<Vec<HyperliquidMarketStateRow>> {
    if assets.is_empty() {
        bail!("at least one Hyperliquid asset is required for bounded feature preview");
    }
    let loaded = load_recent_daily_manifests(data_root, recent_days)?;
    if !loaded.errors.is_empty() {
        bail!("raw manifest contains errors: {:?}", loaded.errors);
    }
    let wanted: BTreeSet<&str> = assets.iter().map(String::as_str).collect();
    let mut records = select_records(&loaded.records, CanonicalSource::Hyperliquid);
    records.sort_by_key(|record| record.observed_at);

    let mut rows = Vec::new();
    for record in records {
        let envelope = load_envelope(record)?;
        rows.extend(
            parse_meta_and_asset_contexts(&envelope, record)?
                .into_iter()
                .filter(|row| wanted.contains(row.asset.as_str())),
        );
    }
    Ok(compute_hyperliquid_market_state(&rows))
}

pub fn compute_recent_stablecoin_state(
    data_root: &Path,
    recent_days: usize,
) -> Result<(Vec<StablecoinSystemStateRow>, Vec<StablecoinChainStateRow>)> {
    let loaded = load_recent_daily_manifests(data_root, recent_days)?;
    if !loaded.errors.is_empty() {
        bail!("raw manifest contains errors: {:?}", loaded.errors);
    }
    let mut records = select_records(&loaded.records, CanonicalSource::Stablecoins);
    records.sort_by_key(|record| record.observed_at);

    let mut assets = Vec::new();
    let mut chains = Vec::new();
    for record in records {
        let envelope = load_envelope(record)?;
        let snapshot = parse_stablecoin_snapshot(&envelope, record)?;
        assets.extend(snapshot.assets);
        chains.extend(snapshot.chains);
    }
    Ok(compute_stablecoin_system_state(&assets, &chains))
}

pub fn load_recent_aave_canonical(
    data_root: &Path,
    recent_days: usize,
) -> Result<(Vec<AaveMarketRow>, Vec<AaveLiquidationRow>)> {
    if recent_days == 0 {
        bail!("recent_days must be positive");
    }
    let loaded = load_recent_daily_manifests(data_root, recent_days)?;
    if !loaded.errors.is_empty() {
        bail!("raw manifest contains errors: {:?}", loaded.errors);
    }
    let mut market_records: Vec<&RawSnapshotManifest> = loaded
        .records
        .iter()
        .filter(|record| {
            record.source_id == "aave:v3:graphql" && record.observation_type == "markets_snapshot"
        })
        .collect();
    let mut liquidation_records: Vec<&RawSnapshotManifest> = loaded
        .records
        .iter()
        .filter(|record| {
            record.source_id == "aave:v3:ethereum" && record.observation_type == "liquidation_logs"
        })
        .collect();
    market_records.sort_by_key(|record| record.observed_at);
    liquidation_records.sort_by_key(|record| record.observed_at);

    let mut markets = Vec::new();
    for record in market_records {
        let envelope = load_envelope(record)?;
        markets.extend(parse_aave_markets(&envelope, record)?);
    }
    let mut liquidations = Vec::new();
    for record in liquidation_records {
        let envelope = load_envelope(record)?;
        liquidations.extend(parse_aave_liquidations(&envelope, record)?);
    }
    Ok((markets, liquidations))
}

pub fn load_latest_stablecoin_chain_composition(
    data_root: &Path,
    recent_days: usize,
) -> Result<Vec<crate::StablecoinChainSupplyRow>> {
    if recent_days == 0 {
        bail!("recent_days must be positive");
    }
    let loaded = load_recent_daily_manifests(data_root, recent_days)?;
    if !loaded.errors.is_empty() {
        bail!("raw manifest contains errors: {:?}", loaded.errors);
    }
    let record = select_records(&loaded.records, CanonicalSource::Stablecoins)
        .into_iter()
        .max_by_key(|record| record.observed_at)
        .ok_or_else(|| anyhow::anyhow!("no recent DefiLlama stablecoin snapshot"))?;
    let envelope = load_envelope(record)?;
    Ok(parse_stablecoin_snapshot(&envelope, record)?.chains)
}

fn select_records<'a>(
    records: &'a [RawSnapshotManifest],
    source: CanonicalSource,
) -> Vec<&'a RawSnapshotManifest> {
    records
        .iter()
        .filter(|record| {
            record.source_id == source.source_id()
                && record.observation_type == source.observation_type()
        })
        .collect()
}
