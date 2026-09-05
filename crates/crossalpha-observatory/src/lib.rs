use anyhow::{Context, Result};
use chrono::{DateTime, SecondsFormat, Timelike, Utc};
use crossalpha_storage::{ManifestLock, RawSnapshotManifest};
use flate2::read::GzDecoder;
use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};

pub const DEFAULT_EXPECTED_SERIES: [(&str, &str); 3] = [
    ("hyperliquid", "metaAndAssetCtxs"),
    ("hyperliquid", "allMids"),
    ("defillama", "stablecoins_snapshot"),
];

#[derive(Debug, Clone, Serialize)]
pub struct SeriesHealth {
    pub ok: bool,
    pub count: usize,
    pub latest_observed_at: Option<String>,
    pub age_seconds: Option<f64>,
    pub stale: bool,
    pub gap_count: usize,
    pub max_gap_seconds: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duplicate_timestamps: Option<usize>,
    pub latest_integrity: Option<Value>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ObservatoryHealthReport {
    pub ok: bool,
    pub checked_at: String,
    pub manifest_records: usize,
    pub manifest_errors: Vec<String>,
    pub expected_interval_seconds: u64,
    pub stale_after_seconds: u64,
    pub raw_bytes_manifested: u64,
    pub compression_sample_records: usize,
    pub compression_sample_raw_bytes: u64,
    pub compressed_bytes_manifested: u64,
    pub compression_ratio: Option<f64>,
    pub series: BTreeMap<String, SeriesHealth>,
}

pub fn load_manifest(data_root: &Path) -> Result<(Vec<RawSnapshotManifest>, Vec<String>)> {
    let path = data_root.join("manifests/raw_snapshots.jsonl");
    if !path.exists() {
        return Ok((
            Vec::new(),
            vec![format!("manifest missing: {}", path.display())],
        ));
    }

    let _lock = ManifestLock::acquire(data_root)?;
    let file = File::open(&path).with_context(|| format!("open manifest {}", path.display()))?;
    let mut records = Vec::new();
    let mut errors = Vec::new();

    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.with_context(|| format!("read manifest line {}", index + 1))?;
        if line.trim().is_empty() {
            continue;
        }
        match serde_json::from_str::<RawSnapshotManifest>(&line) {
            Ok(record) => records.push(record),
            Err(error) => errors.push(format!("line {}: {error}", index + 1)),
        }
    }
    Ok((records, errors))
}

pub fn verify_snapshot(record: &RawSnapshotManifest) -> Value {
    let path = Path::new(&record.path);
    if !path.exists() {
        return json!({
            "ok": false,
            "path": record.path,
            "error": "file missing",
        });
    }

    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) => {
            return json!({
                "ok": false,
                "path": record.path,
                "error": format!("gzip read failed: {error}"),
            });
        }
    };
    let mut decoder = GzDecoder::new(file);
    let mut payload = Vec::new();
    if let Err(error) = decoder.read_to_end(&mut payload) {
        return json!({
            "ok": false,
            "path": record.path,
            "error": format!("gzip read failed: {error}"),
        });
    }

    let digest = hex::encode(Sha256::digest(&payload));
    let compressed_bytes = match fs::metadata(path) {
        Ok(metadata) => metadata.len(),
        Err(error) => {
            return json!({
                "ok": false,
                "path": record.path,
                "error": format!("stat failed: {error}"),
            });
        }
    };

    let mut errors = Vec::new();
    if digest != record.sha256 {
        errors.push("sha256 mismatch");
    }
    if payload.len() as u64 != record.bytes {
        errors.push("uncompressed byte count mismatch");
    }
    if let Some(expected) = record.compressed_bytes
        && compressed_bytes != expected
    {
        errors.push("compressed byte count mismatch");
    }

    json!({
        "ok": errors.is_empty(),
        "path": record.path,
        "sha256": digest,
        "bytes": payload.len(),
        "compressed_bytes": compressed_bytes,
        "errors": errors,
    })
}

pub fn observatory_health(
    data_root: &Path,
    expected_interval_seconds: u64,
    stale_after_seconds: u64,
    verify_latest: bool,
    now: Option<DateTime<Utc>>,
) -> Result<ObservatoryHealthReport> {
    let now = now.unwrap_or_else(Utc::now);
    let (records, manifest_errors) = load_manifest(data_root)?;
    let mut grouped: BTreeMap<(String, String), Vec<&RawSnapshotManifest>> = BTreeMap::new();
    for record in &records {
        grouped
            .entry((record.source_id.clone(), record.observation_type.clone()))
            .or_default()
            .push(record);
    }

    let mut series = BTreeMap::new();
    let mut current_ok = manifest_errors.is_empty();
    let expected = expected_interval_seconds as f64;
    let gap_threshold = (expected * 2.5).max(expected + 1.0);

    for (source_id, observation_type) in DEFAULT_EXPECTED_SERIES {
        let label = format!("{source_id}/{observation_type}");
        let key = (source_id.to_owned(), observation_type.to_owned());
        let mut items = grouped.get(&key).cloned().unwrap_or_default();
        items.sort_by(|left, right| left.observed_at.cmp(&right.observed_at));

        if items.is_empty() {
            current_ok = false;
            series.insert(
                label,
                SeriesHealth {
                    ok: false,
                    count: 0,
                    latest_observed_at: None,
                    age_seconds: None,
                    stale: true,
                    gap_count: 0,
                    max_gap_seconds: None,
                    duplicate_timestamps: None,
                    latest_integrity: None,
                },
            );
            continue;
        }

        let latest = items.last().copied().expect("items checked non-empty above");
        let age_seconds = duration_seconds(now - latest.observed_at).max(0.0);
        let stale = age_seconds > stale_after_seconds as f64;
        let mut gaps = Vec::new();
        let mut duplicate_timestamps = 0usize;

        for pair in items.windows(2) {
            let delta = duration_seconds(pair[1].observed_at - pair[0].observed_at);
            if delta == 0.0 {
                duplicate_timestamps += 1;
            }
            if delta > gap_threshold {
                gaps.push(delta);
            }
        }

        let latest_integrity = verify_latest.then(|| verify_snapshot(latest));
        let integrity_ok = latest_integrity
            .as_ref()
            .and_then(|value| value.get("ok"))
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let series_ok = !stale && integrity_ok;
        current_ok = current_ok && series_ok;
        let gap_count = gaps.len();
        let max_gap_seconds = gaps.into_iter().reduce(f64::max).map(round3);

        series.insert(
            label,
            SeriesHealth {
                ok: series_ok,
                count: items.len(),
                latest_observed_at: Some(python_isoformat(latest.observed_at)),
                age_seconds: Some(round3(age_seconds)),
                stale,
                gap_count,
                max_gap_seconds,
                duplicate_timestamps: Some(duplicate_timestamps),
                latest_integrity,
            },
        );
    }

    let raw_bytes_manifested = records.iter().map(|record| record.bytes).sum();
    let compression_sample_records = records
        .iter()
        .filter(|record| record.compressed_bytes.is_some())
        .count();
    let compression_sample_raw_bytes = records
        .iter()
        .filter(|record| record.compressed_bytes.is_some())
        .map(|record| record.bytes)
        .sum();
    let compressed_bytes_manifested = records
        .iter()
        .filter_map(|record| record.compressed_bytes)
        .sum();
    let compression_ratio = if compression_sample_raw_bytes > 0 && compressed_bytes_manifested > 0 {
        Some(round3(
            compression_sample_raw_bytes as f64 / compressed_bytes_manifested as f64,
        ))
    } else {
        None
    };

    Ok(ObservatoryHealthReport {
        ok: current_ok,
        checked_at: python_isoformat(now),
        manifest_records: records.len(),
        manifest_errors,
        expected_interval_seconds,
        stale_after_seconds,
        raw_bytes_manifested,
        compression_sample_records,
        compression_sample_raw_bytes,
        compressed_bytes_manifested,
        compression_ratio,
        series,
    })
}

pub fn write_health_report(data_root: &Path, report: &ObservatoryHealthReport) -> Result<PathBuf> {
    let path = data_root.join("manifests/observatory_health.json");
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut file = File::create(&tmp)?;
        serde_json::to_writer_pretty(&mut file, report)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, &path)?;
    if let Some(parent) = path.parent()
        && let Ok(directory) = File::open(parent)
    {
        let _ = directory.sync_all();
    }
    Ok(path)
}

fn duration_seconds(duration: chrono::Duration) -> f64 {
    duration
        .num_microseconds()
        .map(|value| value as f64 / 1_000_000.0)
        .unwrap_or_else(|| duration.num_seconds() as f64)
}

fn python_isoformat(timestamp: DateTime<Utc>) -> String {
    let format = if timestamp.nanosecond() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    timestamp.to_rfc3339_opts(format, false)
}

fn round3(value: f64) -> f64 {
    (value * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round3_matches_health_contract() {
        assert_eq!(round3(1.23456), 1.235);
        assert_eq!(round3(0.0), 0.0);
    }

    #[test]
    fn python_isoformat_matches_datetime_shape() {
        let whole: DateTime<Utc> = "2026-09-05T12:34:56Z".parse().unwrap();
        let micros: DateTime<Utc> = "2026-09-05T12:34:56.123456Z".parse().unwrap();
        assert_eq!(python_isoformat(whole), "2026-09-05T12:34:56+00:00");
        assert_eq!(
            python_isoformat(micros),
            "2026-09-05T12:34:56.123456+00:00"
        );
    }

    #[test]
    fn expected_series_matches_python_contract() {
        assert_eq!(DEFAULT_EXPECTED_SERIES.len(), 3);
        assert_eq!(
            DEFAULT_EXPECTED_SERIES[0],
            ("hyperliquid", "metaAndAssetCtxs")
        );
        assert_eq!(
            DEFAULT_EXPECTED_SERIES[2],
            ("defillama", "stablecoins_snapshot")
        );
    }
}
