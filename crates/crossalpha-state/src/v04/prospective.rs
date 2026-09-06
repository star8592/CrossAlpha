use crate::v03_freeze::payload_hash;
use crate::v04::{ACTIONABILITY, FUNDING_SEMANTICS, PROTOCOL};
use crate::v04_freeze::{freeze_path, verify_freeze_file, verify_hash_graph, PROSPECTIVE_PROTOCOL};
use crate::v04_runtime_binding::{runtime_binding_path, verify_runtime_binding_file};
use anyhow::{Context, Result, bail};
use arrow_array::{Array, StringArray};
use chrono::{DateTime, Datelike, Timelike, Utc};
use flate2::read::GzDecoder;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub const MAX_LIVE_WRITE_AGE_SECONDS: i64 = 180;

pub fn write_live_observation(
    data_root: &Path,
    mechanics_path: &Path,
    venue_path: &Path,
    known_at: DateTime<Utc>,
) -> Result<Value> {
    let freeze_file = freeze_path(data_root);
    if !verify_freeze_file(&freeze_file)? {
        bail!("State V0.4 freeze missing or invalid");
    }
    let freeze: Value = serde_json::from_reader(File::open(&freeze_file)?)?;
    if !verify_hash_graph(data_root, &freeze)? {
        bail!("STATE_V04_HASH_GRAPH_MUTATED");
    }
    if !verify_runtime_binding_file(&runtime_binding_path(data_root))? {
        bail!("STATE_V04_RUST_RUNTIME_BINDING_INVALID");
    }
    if !mechanics_path.exists() || !venue_path.exists() {
        bail!("State V0.4 mechanics/venue artifact missing");
    }

    let mechanics: Value = serde_json::from_reader(File::open(mechanics_path)?)?;
    if mechanics.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
        bail!("mechanics artifact is not State V0.4");
    }
    if mechanics.get("actionability").and_then(Value::as_str) != Some(ACTIONABILITY)
        || !mechanics.get("risk_multiplier").is_some_and(Value::is_null)
    {
        bail!("State V0.4 prospective ledger is descriptive only");
    }
    if mechanics.get("no_composite_stress_score").and_then(Value::as_bool) != Some(true) {
        bail!("State V0.4 composite stress score is forbidden");
    }
    if mechanics.get("venue_snapshot_path").and_then(Value::as_str)
        != Some(&*venue_path.to_string_lossy())
    {
        bail!("mechanics artifact points to another venue snapshot");
    }

    let venue_rows = read_venue_identity_and_times(venue_path)?;
    let identities: BTreeSet<(String, String)> = venue_rows
        .iter()
        .map(|row| (row.asset.clone(), row.venue.clone()))
        .collect();
    let expected: BTreeSet<(String, String)> = ["BTC", "ETH"]
        .into_iter()
        .flat_map(|asset| ["binance", "okx", "bybit"].into_iter().map(move |venue| (asset.to_owned(), venue.to_owned())))
        .collect();
    if venue_rows.len() != 6 || identities != expected {
        bail!("State V0.4 prospective venue snapshot must contain six unique BTC/ETH venue rows");
    }

    let generated = parse_time(&mechanics, "generated_at")?;
    let frozen_at = parse_time(&freeze, "frozen_at")?;
    if generated < frozen_at {
        bail!("State V0.4 prospective observation predates freeze");
    }
    let age = known_at - generated;
    if age.num_milliseconds() < 0 || age.num_seconds() > MAX_LIVE_WRITE_AGE_SECONDS {
        bail!("State V0.4 live observation is stale; retrospective backfill refused");
    }
    for row in &venue_rows {
        if row.observed_at > row.known_at || row.known_at > generated {
            bail!("State V0.4 PTI ordering violated");
        }
    }

    let raw_records = mechanics
        .get("raw_records")
        .and_then(Value::as_array)
        .context("State V0.4 mechanics artifact must link six raw records")?;
    if raw_records.len() != 6 {
        bail!("State V0.4 mechanics artifact must link six raw records");
    }
    let mut raw_links = Vec::with_capacity(6);
    for raw in raw_records {
        let raw_path = PathBuf::from(
            raw.get("raw_path")
                .and_then(Value::as_str)
                .context("State V0.4 raw_path missing")?,
        );
        let payload_sha = raw
            .get("raw_sha256")
            .and_then(Value::as_str)
            .context("State V0.4 raw_sha256 missing")?;
        let compressed_sha = raw
            .get("raw_compressed_file_sha256")
            .and_then(Value::as_str)
            .context("State V0.4 raw compressed hash missing")?;
        if sha256_gzip_payload(&raw_path)? != payload_sha {
            bail!("State V0.4 raw uncompressed payload hash link failed");
        }
        if sha256_file(&raw_path)? != compressed_sha {
            bail!("State V0.4 raw compressed-file hash link failed");
        }
        raw_links.push(json!({
            "venue": raw.get("venue").cloned().unwrap_or(Value::Null),
            "asset": raw.get("asset").cloned().unwrap_or(Value::Null),
            "raw_path": raw_path.to_string_lossy(),
            "raw_sha256": payload_sha,
            "raw_compressed_file_sha256": compressed_sha,
        }));
    }

    let payload = json!({
        "schema_version": 1,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": PROTOCOL,
        "freeze_record_sha256": freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        "known_at": known_at.to_rfc3339(),
        "generated_at": generated.to_rfc3339(),
        "mechanics_path": mechanics_path.to_string_lossy(),
        "mechanics_sha256": sha256_file(mechanics_path)?,
        "venue_snapshot_path": venue_path.to_string_lossy(),
        "venue_snapshot_sha256": sha256_file(venue_path)?,
        "raw_links": raw_links,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "no_composite_stress_score": true,
        "data_confidence": mechanics.get("data_confidence").cloned().unwrap_or(Value::Null),
        "funding_semantics": mechanics.get("funding_semantics").cloned().unwrap_or(Value::String(FUNDING_SEMANTICS.to_owned())),
        "assets": mechanics.get("assets").cloned().unwrap_or(Value::Null),
    });
    let path = record_path(data_root, generated);
    if path.exists() {
        let existing: Value = serde_json::from_reader(File::open(&path)?)?;
        if !verify_seal(&existing)? {
            bail!("existing State V0.4 record failed seal verification");
        }
        for key in ["mechanics_sha256", "venue_snapshot_sha256"] {
            if existing.get(key) != payload.get(key) {
                bail!("STATE_V04_TIMESTAMP_COLLISION");
            }
        }
        return Ok(with_status(existing, "already_exists", &path));
    }
    let sealed = seal(payload)?;
    write_immutable(&path, &sealed)?;
    Ok(with_status(sealed, "written", &path))
}

#[derive(Debug)]
struct VenueIdentityTime {
    asset: String,
    venue: String,
    observed_at: DateTime<Utc>,
    known_at: DateTime<Utc>,
}

fn read_venue_identity_and_times(path: &Path) -> Result<Vec<VenueIdentityTime>> {
    let file = File::open(path)?;
    let mut reader = ParquetRecordBatchReaderBuilder::try_new(file)?.build()?;
    let mut result = Vec::new();
    while let Some(batch) = reader.next() {
        let batch = batch?;
        let asset = string_column(&batch, "asset")?;
        let venue = string_column(&batch, "venue")?;
        let observed = string_column(&batch, "observed_at")?;
        let known = string_column(&batch, "known_at")?;
        for index in 0..batch.num_rows() {
            if asset.is_null(index) || venue.is_null(index) || observed.is_null(index) || known.is_null(index) {
                bail!("State V0.4 venue snapshot has invalid PTI timestamps");
            }
            result.push(VenueIdentityTime {
                asset: asset.value(index).to_owned(),
                venue: venue.value(index).to_owned(),
                observed_at: DateTime::parse_from_rfc3339(observed.value(index))?.with_timezone(&Utc),
                known_at: DateTime::parse_from_rfc3339(known.value(index))?.with_timezone(&Utc),
            });
        }
    }
    Ok(result)
}

fn string_column<'a>(batch: &'a arrow_array::RecordBatch, name: &str) -> Result<&'a StringArray> {
    batch
        .column_by_name(name)
        .with_context(|| format!("venue snapshot missing {name}"))?
        .as_any()
        .downcast_ref::<StringArray>()
        .with_context(|| format!("venue snapshot {name} is not utf8"))
}

fn record_path(data_root: &Path, generated: DateTime<Utc>) -> PathBuf {
    data_root
        .join("research/state_v04/prospective")
        .join(format!("year={:04}", generated.year()))
        .join(format!("month={:02}", generated.month()))
        .join(format!("day={:02}", generated.day()))
        .join(format!(
            "state_at={:02}{:02}{:02}{:06}.json",
            generated.hour(),
            generated.minute(),
            generated.second(),
            generated.timestamp_subsec_micros()
        ))
}

fn parse_time(value: &Value, key: &str) -> Result<DateTime<Utc>> {
    let raw = value.get(key).and_then(Value::as_str).with_context(|| format!("{key} missing"))?;
    Ok(DateTime::parse_from_rfc3339(raw)?.with_timezone(&Utc))
}

fn sha256_gzip_payload(path: &Path) -> Result<String> {
    let file = File::open(path)?;
    let mut decoder = GzDecoder::new(file);
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = decoder.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
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

fn seal(mut value: Value) -> Result<Value> {
    let digest = payload_hash(&value)?;
    value
        .as_object_mut()
        .context("State V0.4 prospective payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(value)
}

fn verify_seal(value: &Value) -> Result<bool> {
    let expected = value.get("record_sha256").and_then(Value::as_str).context("record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

fn write_immutable(path: &Path, value: &Value) -> Result<()> {
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

fn with_status(mut value: Value, status: &str, path: &Path) -> Value {
    if let Some(object) = value.as_object_mut() {
        object.insert("status".to_owned(), Value::String(status.to_owned()));
        object.insert("output".to_owned(), Value::String(path.to_string_lossy().into_owned()));
    }
    value
}
