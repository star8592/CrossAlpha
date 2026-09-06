use crate::canonical::{CanonicalSource, load_envelope};
use crate::{
    CANONICAL_STABLECOIN_SCHEMA_VERSION, parse_meta_and_asset_contexts,
    parse_stablecoin_snapshot, write_hyperliquid_parquet, write_stablecoin_parquet,
};
use anyhow::{Context, Result, bail};
use arrow_array::{Array, Int64Array};
use chrono::{Datelike, Timelike};
use crossalpha_storage::{RawSnapshotManifest, load_recent_daily_manifests};
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde::Serialize;
use std::fs::File;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CanonicalSourceReport {
    pub snapshots: usize,
    pub written: usize,
    pub rewritten: usize,
    pub skipped: usize,
    pub rows_written: usize,
    pub secondary_rows_written: usize,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct CanonicalMaterializeReport {
    pub mode: String,
    pub recent_days: usize,
    pub manifest_records: usize,
    pub hyperliquid: CanonicalSourceReport,
    pub stablecoins: CanonicalSourceReport,
}

pub fn materialize_recent_canonical(
    data_root: &Path,
    output_root: &Path,
    recent_days: usize,
) -> Result<CanonicalMaterializeReport> {
    let loaded = load_recent_daily_manifests(data_root, recent_days)?;
    if !loaded.errors.is_empty() {
        bail!("raw manifest contains errors: {:?}", loaded.errors);
    }

    let hyperliquid = materialize_hyperliquid(&loaded.records, output_root)?;
    let stablecoins = materialize_stablecoins(&loaded.records, output_root)?;
    Ok(CanonicalMaterializeReport {
        mode: format!("recent_{recent_days}d"),
        recent_days,
        manifest_records: loaded.records.len(),
        hyperliquid,
        stablecoins,
    })
}

fn materialize_hyperliquid(
    records: &[RawSnapshotManifest],
    output_root: &Path,
) -> Result<CanonicalSourceReport> {
    let mut selected: Vec<&RawSnapshotManifest> = records
        .iter()
        .filter(|record| {
            record.source_id == CanonicalSource::Hyperliquid.source_id()
                && record.observation_type == CanonicalSource::Hyperliquid.observation_type()
        })
        .collect();
    selected.sort_by_key(|record| record.observed_at);

    let mut report = CanonicalSourceReport {
        snapshots: selected.len(),
        written: 0,
        rewritten: 0,
        skipped: 0,
        rows_written: 0,
        secondary_rows_written: 0,
    };

    for record in selected {
        let path = hyperliquid_path(output_root, record);
        if path.exists() {
            report.skipped += 1;
            continue;
        }
        let envelope = load_envelope(record)?;
        let rows = parse_meta_and_asset_contexts(&envelope, record)?;
        write_hyperliquid_parquet(&rows, &path)?;
        report.written += 1;
        report.rows_written += rows.len();
    }
    Ok(report)
}

fn materialize_stablecoins(
    records: &[RawSnapshotManifest],
    output_root: &Path,
) -> Result<CanonicalSourceReport> {
    let mut selected: Vec<&RawSnapshotManifest> = records
        .iter()
        .filter(|record| {
            record.source_id == CanonicalSource::Stablecoins.source_id()
                && record.observation_type == CanonicalSource::Stablecoins.observation_type()
        })
        .collect();
    selected.sort_by_key(|record| record.observed_at);

    let mut report = CanonicalSourceReport {
        snapshots: selected.len(),
        written: 0,
        rewritten: 0,
        skipped: 0,
        rows_written: 0,
        secondary_rows_written: 0,
    };

    for record in selected {
        let (asset_path, chain_path) = stablecoin_paths(output_root, record);
        let asset_version = parquet_schema_version(&asset_path)?;
        let chain_version = parquet_schema_version(&chain_path)?;
        if asset_version >= CANONICAL_STABLECOIN_SCHEMA_VERSION
            && chain_version >= CANONICAL_STABLECOIN_SCHEMA_VERSION
        {
            report.skipped += 1;
            continue;
        }
        let replacing_old = asset_path.exists() || chain_path.exists();
        let envelope = load_envelope(record)?;
        let snapshot = parse_stablecoin_snapshot(&envelope, record)?;
        write_stablecoin_parquet(&snapshot, &asset_path, &chain_path)?;
        report.written += 1;
        if replacing_old {
            report.rewritten += 1;
        }
        report.rows_written += snapshot.assets.len();
        report.secondary_rows_written += snapshot.chains.len();
    }
    Ok(report)
}

pub fn hyperliquid_path(output_root: &Path, record: &RawSnapshotManifest) -> PathBuf {
    let observed = record.observed_at;
    output_root
        .join("canonical")
        .join("hyperliquid")
        .join("asset_contexts")
        .join(format!("year={:04}", observed.year()))
        .join(format!("month={:02}", observed.month()))
        .join(format!("day={:02}", observed.day()))
        .join(format!(
            "{:04}{:02}{:02}T{:02}{:02}{:02}.{:06}Z_{}.parquet",
            observed.year(),
            observed.month(),
            observed.day(),
            observed.hour(),
            observed.minute(),
            observed.second(),
            observed.timestamp_subsec_micros(),
            &record.sha256[..record.sha256.len().min(12)]
        ))
}

pub fn stablecoin_paths(
    output_root: &Path,
    record: &RawSnapshotManifest,
) -> (PathBuf, PathBuf) {
    let observed = record.observed_at;
    let relative = PathBuf::from(format!("year={:04}", observed.year()))
        .join(format!("month={:02}", observed.month()))
        .join(format!("day={:02}", observed.day()))
        .join(format!(
            "{:04}{:02}{:02}T{:02}{:02}{:02}.{:06}Z_{}.parquet",
            observed.year(),
            observed.month(),
            observed.day(),
            observed.hour(),
            observed.minute(),
            observed.second(),
            observed.timestamp_subsec_micros(),
            &record.sha256[..record.sha256.len().min(12)]
        ));
    (
        output_root
            .join("canonical/defillama/stablecoin_assets")
            .join(&relative),
        output_root
            .join("canonical/defillama/stablecoin_chain_supply")
            .join(relative),
    )
}

fn parquet_schema_version(path: &Path) -> Result<u32> {
    if !path.exists() {
        return Ok(0);
    }
    let file = File::open(path).with_context(|| format!("open parquet {}", path.display()))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)
        .with_context(|| format!("read parquet metadata {}", path.display()))?;
    let Some(index) = builder
        .schema()
        .fields()
        .iter()
        .position(|field| field.name() == "canonical_schema_version")
    else {
        return Ok(0);
    };
    let mut reader = builder
        .with_batch_size(1)
        .build()
        .with_context(|| format!("build parquet reader {}", path.display()))?;
    let Some(batch) = reader.next() else {
        return Ok(0);
    };
    let batch = batch.with_context(|| format!("read parquet row {}", path.display()))?;
    if batch.num_rows() == 0 {
        return Ok(0);
    }
    let array = batch.column(index);
    let Some(values) = array.as_any().downcast_ref::<Int64Array>() else {
        return Ok(0);
    };
    if values.is_null(0) {
        return Ok(0);
    }
    Ok(u32::try_from(values.value(0)).unwrap_or(0))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{TimeZone, Utc};

    fn manifest() -> RawSnapshotManifest {
        RawSnapshotManifest {
            path: "/tmp/raw.json.gz".to_owned(),
            sha256: "1234567890abcdef".to_owned(),
            bytes: 1,
            compressed_bytes: Some(1),
            observed_at: Utc
                .with_ymd_and_hms(2026, 9, 6, 1, 2, 3)
                .unwrap()
                .with_nanosecond(456_789_000)
                .unwrap(),
            source_id: "hyperliquid".to_owned(),
            observation_type: "metaAndAssetCtxs".to_owned(),
        }
    }

    #[test]
    fn hyperliquid_path_matches_python_partition_contract() {
        let path = hyperliquid_path(Path::new("/data"), &manifest());
        assert_eq!(
            path,
            PathBuf::from(
                "/data/canonical/hyperliquid/asset_contexts/year=2026/month=09/day=06/20260906T010203.456789Z_1234567890ab.parquet"
            )
        );
    }

    #[test]
    fn stablecoin_paths_match_python_partition_contract() {
        let (assets, chains) = stablecoin_paths(Path::new("/data"), &manifest());
        assert!(assets.to_string_lossy().contains("stablecoin_assets/year=2026/month=09/day=06/20260906T010203.456789Z_1234567890ab.parquet"));
        assert!(chains.to_string_lossy().contains("stablecoin_chain_supply/year=2026/month=09/day=06/20260906T010203.456789Z_1234567890ab.parquet"));
    }
}
