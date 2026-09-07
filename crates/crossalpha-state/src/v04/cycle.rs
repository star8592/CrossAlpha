use crate::StateRuntimeContext;
use crate::v04::{ACTIONABILITY, MAXIMUM_SNAPSHOT_AGE_SECONDS, PROTOCOL, compute_market_mechanics};
use crate::v04_artifacts::write_venue_rows;
use crate::v04_provider::{MultiVenueCollector, VenuePayload, parse_venue_snapshot};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Datelike, Timelike, Utc};
use crossalpha_storage::{
    ObservationEnvelope, RAW_ENVELOPE_CANONICAL_SCHEMA_VERSION, RawSnapshotStore,
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub async fn preflight(context: &StateRuntimeContext) -> Result<Value> {
    run(context, false).await
}

pub async fn run_cycle(context: &StateRuntimeContext) -> Result<Value> {
    require_runtime_binding(&context.data_root)?;
    run(context, true).await
}

async fn run(context: &StateRuntimeContext, write: bool) -> Result<Value> {
    let collector = MultiVenueCollector::new(context.http_timeout)?;
    let collected_at = Utc::now();
    let payloads = collector.collect().await?;
    if payloads.len() != 6 {
        bail!(
            "State V0.4 expected 6 venue/asset slots, got {}",
            payloads.len()
        );
    }

    let mut normalized = Vec::with_capacity(6);
    let mut raw_records = Vec::with_capacity(6);
    let store = RawSnapshotStore::new(&context.data_root);
    for payload in &payloads {
        let mut row = parse_venue_snapshot(payload, collected_at)?;
        if write {
            let metadata: Map<String, Value> = serde_json::from_value(json!({
                "asset": payload.asset,
                "venue": payload.venue,
                "data_cost_usd": 0,
                "authentication_required": false,
                "collection_error": payload.collection_error,
            }))?;
            let envelope = ObservationEnvelope {
                schema_version: RAW_ENVELOPE_CANONICAL_SCHEMA_VERSION,
                event_time: Some(row.observed_at),
                observed_at: row.observed_at,
                known_at: collected_at,
                source_type: "exchange".to_owned(),
                source_id: format!("{}:public", payload.venue),
                observation_type: "multi_venue_market_mechanics_snapshot".to_owned(),
                payload: serde_json::to_value(payload)?,
                metadata,
            };
            let manifest = store.write(&envelope)?;
            let compressed_hash = sha256_file(Path::new(&manifest.path))?;
            row.raw_sha256 = Some(manifest.sha256.clone());
            row.raw_compressed_file_sha256 = Some(compressed_hash.clone());
            row.raw_path = Some(manifest.path.clone());
            raw_records.push(json!({
                "venue": payload.venue,
                "asset": payload.asset,
                "collection_error": payload.collection_error,
                "raw_sha256": manifest.sha256,
                "raw_compressed_file_sha256": compressed_hash,
                "raw_path": manifest.path,
            }));
        } else {
            raw_records.push(json!({
                "venue": payload.venue,
                "asset": payload.asset,
                "collection_error": payload.collection_error,
            }));
        }
        normalized.push(row);
    }
    normalized.sort_by(|left, right| {
        left.asset
            .cmp(&right.asset)
            .then(left.venue.cmp(&right.venue))
    });
    let generated = Utc::now();
    let report = compute_market_mechanics(&normalized, generated, MAXIMUM_SNAPSHOT_AGE_SECONDS);
    if report.get("data_confidence").and_then(Value::as_str) == Some("INSUFFICIENT") {
        bail!("State V0.4 insufficient multi-venue data");
    }

    let (venue_path, mechanics_path) = snapshot_paths(&context.data_root, generated);
    let prospective = if write {
        write_venue_rows(&venue_path, &normalized)?;
        let mut payload = report.clone();
        let object = payload
            .as_object_mut()
            .context("State V0.4 mechanics report must be object")?;
        object.insert(
            "venue_snapshot_path".to_owned(),
            Value::String(venue_path.to_string_lossy().into_owned()),
        );
        object.insert("raw_records".to_owned(), Value::Array(raw_records.clone()));
        atomic_write_json(&mechanics_path, &payload)?;
        if crate::v04_freeze::freeze_path(&context.data_root).exists() {
            crate::v04_prospective::write_live_observation(
                &context.data_root,
                &mechanics_path,
                &venue_path,
                Utc::now(),
            )?
        } else {
            json!({"status": "not_frozen_no_prospective_write"})
        }
    } else {
        json!({"status": "preflight_no_write"})
    };

    Ok(json!({
        "protocol": "CROSSALPHA_STATE_V0_4_CYCLE",
        "data_cost_usd": 0,
        "authentication_required": false,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "mutates_predecessors": false,
        "generated_at": generated.to_rfc3339(),
        "collection_error_count": normalized.iter().filter(|row| row.collection_error.is_some()).count(),
        "state": report,
        "venue_rows": normalized,
        "venue_snapshot_path": write.then(|| venue_path.to_string_lossy().into_owned()),
        "mechanics_snapshot_path": write.then(|| mechanics_path.to_string_lossy().into_owned()),
        "prospective": prospective,
        "written": write,
    }))
}

pub fn snapshot_paths(data_root: &Path, generated: DateTime<Utc>) -> (PathBuf, PathBuf) {
    let root = data_root
        .join("derived/state/v04")
        .join(format!("year={:04}", generated.year()))
        .join(format!("month={:02}", generated.month()))
        .join(format!("day={:02}", generated.day()));
    let stamp = format!(
        "{:02}{:02}{:02}{:06}",
        generated.hour(),
        generated.minute(),
        generated.second(),
        generated.timestamp_subsec_micros()
    );
    (
        root.join(format!("venues_at={stamp}.parquet")),
        root.join(format!("mechanics_at={stamp}.json")),
    )
}

fn require_runtime_binding(data_root: &Path) -> Result<()> {
    let path = crate::v04_runtime_binding::runtime_binding_path(data_root);
    if !crate::v04_runtime_binding::verify_runtime_binding_file(&path)? {
        bail!("Native State V0.4 cycle refused: Rust runtime binding missing, invalid, or stale");
    }
    Ok(())
}

fn atomic_write_json(path: &Path, value: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut file = File::create(&tmp)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn snapshot_names_follow_python_contract() {
        let time = Utc.with_ymd_and_hms(2026, 9, 6, 12, 34, 56).unwrap();
        let (venue, mechanics) = snapshot_paths(Path::new("/tmp/data"), time);
        assert!(
            venue
                .to_string_lossy()
                .contains("year=2026/month=09/day=06")
        );
        assert!(
            venue
                .file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("venues_at=123456")
        );
        assert!(
            mechanics
                .file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("mechanics_at=123456")
        );
    }
}
