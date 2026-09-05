use anyhow::Context;
use anyhow::Result;
use chrono::{DateTime, SecondsFormat, Timelike, Utc};
use crossalpha_storage::SeriesState;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use crate::health::DEFAULT_EXPECTED_SERIES;
use crate::health::observatory_health;
use crate::health::verify_snapshot;

pub fn observatory_live_health(
    data_root: &Path,
    stale_after_seconds: u64,
    verify_latest: bool,
    now: Option<DateTime<Utc>>,
) -> Result<Value> {
    let now = now.unwrap_or_else(Utc::now);
    let state_paths: Vec<PathBuf> = DEFAULT_EXPECTED_SERIES
        .iter()
        .map(|(source, observation)| state_path(data_root, source, observation))
        .collect();

    if !state_paths.iter().all(|path| path.exists()) {
        let report = observatory_health(
            data_root,
            300,
            stale_after_seconds,
            verify_latest,
            Some(now),
        )?;
        let mut value = serde_json::to_value(report)?;
        let object = value
            .as_object_mut()
            .context("full health report is not a JSON object")?;
        object.insert(
            "mode".to_owned(),
            Value::String("full_manifest_fallback".to_owned()),
        );
        return Ok(value);
    }

    let mut current_ok = true;
    let mut series_report = BTreeMap::new();
    let mut total_records = 0u64;
    let mut total_raw_bytes = 0u64;
    let mut compression_sample_records = 0u64;
    let mut compression_sample_raw_bytes = 0u64;
    let mut total_compressed_bytes = 0u64;

    for ((source_id, observation_type), path) in
        DEFAULT_EXPECTED_SERIES.iter().zip(state_paths.iter())
    {
        let state: SeriesState = serde_json::from_slice(&fs::read(path)?)
            .with_context(|| format!("parse series state {}", path.display()))?;
        let latest_manifest = state
            .latest_manifest
            .as_ref()
            .with_context(|| format!("series state missing latest_manifest: {}", path.display()))?;
        let latest_observed = latest_manifest.observed_at;
        let age_seconds = duration_seconds(now - latest_observed).max(0.0);
        let stale = age_seconds > stale_after_seconds as f64;
        let integrity = verify_latest.then(|| verify_snapshot(latest_manifest));
        let integrity_ok = integrity
            .as_ref()
            .and_then(|value| value.get("ok"))
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let series_ok = !stale && integrity_ok;
        current_ok = current_ok && series_ok;

        total_records += state.count;
        total_raw_bytes += state.raw_bytes_total;
        compression_sample_records += state.compression_sample_records;
        compression_sample_raw_bytes += state.compression_sample_raw_bytes;
        total_compressed_bytes += state.compressed_bytes_total;

        let label = format!("{source_id}/{observation_type}");
        series_report.insert(
            label,
            json!({
                "ok": series_ok,
                "count": state.count,
                "latest_observed_at": python_isoformat(latest_observed),
                "age_seconds": round3(age_seconds),
                "stale": stale,
                "last_interval_seconds": state.last_interval_seconds,
                "max_interval_seconds": state.max_interval_seconds,
                "latest_integrity": integrity,
            }),
        );
    }

    let compression_ratio = if compression_sample_raw_bytes > 0 && total_compressed_bytes > 0 {
        Some(round3(
            compression_sample_raw_bytes as f64 / total_compressed_bytes as f64,
        ))
    } else {
        None
    };

    let mut root = Map::new();
    root.insert("ok".to_owned(), Value::Bool(current_ok));
    root.insert("mode".to_owned(), Value::String("series_state".to_owned()));
    root.insert(
        "checked_at".to_owned(),
        Value::String(python_isoformat(now)),
    );
    root.insert("manifest_records".to_owned(), json!(total_records));
    root.insert("manifest_errors".to_owned(), json!([]));
    root.insert("stale_after_seconds".to_owned(), json!(stale_after_seconds));
    root.insert("raw_bytes_manifested".to_owned(), json!(total_raw_bytes));
    root.insert(
        "compression_sample_records".to_owned(),
        json!(compression_sample_records),
    );
    root.insert(
        "compression_sample_raw_bytes".to_owned(),
        json!(compression_sample_raw_bytes),
    );
    root.insert(
        "compressed_bytes_manifested".to_owned(),
        json!(total_compressed_bytes),
    );
    root.insert("compression_ratio".to_owned(), json!(compression_ratio));
    root.insert("series".to_owned(), serde_json::to_value(series_report)?);
    Ok(Value::Object(root))
}

fn state_path(data_root: &Path, source_id: &str, observation_type: &str) -> PathBuf {
    let source = safe_component(source_id);
    let observation = safe_component(observation_type);
    data_root
        .join("manifests")
        .join("series")
        .join(source)
        .join(format!("{observation}.json"))
}

fn safe_component(value: &str) -> String {
    value.replace('/', "_").replace("..", "_")
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
    fn state_path_matches_python_contract() {
        assert_eq!(
            state_path(Path::new("/tmp/data"), "a/b", "x..y"),
            PathBuf::from("/tmp/data/manifests/series/a_b/x_y.json")
        );
    }
}
