use crate::{
    CanonicalSource, HyperliquidMarketStateRow, StablecoinChainStateRow,
    StablecoinSystemStateRow, compute_hyperliquid_market_state, compute_stablecoin_system_state,
    load_envelope, parse_meta_and_asset_contexts, parse_stablecoin_snapshot,
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
