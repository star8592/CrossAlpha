use anyhow::{Context, Result};
use chrono::{DateTime, Datelike, SecondsFormat, Timelike, Utc};
use flate2::{Compression, write::GzEncoder};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ObservationEnvelope {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    pub event_time: Option<DateTime<Utc>>,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub source_type: String,
    pub source_id: String,
    pub observation_type: String,
    pub payload: Value,
    #[serde(default)]
    pub metadata: serde_json::Map<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RawSnapshotManifest {
    pub path: String,
    pub sha256: String,
    pub bytes: u64,
    pub compressed_bytes: Option<u64>,
    pub observed_at: DateTime<Utc>,
    pub source_id: String,
    pub observation_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SeriesState {
    pub schema_version: u32,
    pub source_id: String,
    pub observation_type: String,
    pub count: u64,
    pub first_observed_at: Option<DateTime<Utc>>,
    pub latest_observed_at: Option<DateTime<Utc>>,
    pub previous_observed_at: Option<DateTime<Utc>>,
    pub last_interval_seconds: Option<f64>,
    pub max_interval_seconds: Option<f64>,
    pub raw_bytes_total: u64,
    pub compressed_bytes_total: u64,
    pub compression_sample_records: u64,
    pub compression_sample_raw_bytes: u64,
    pub latest_manifest: Option<RawSnapshotManifest>,
    pub updated_at: DateTime<Utc>,
}

fn default_schema_version() -> u32 {
    1
}

pub struct ManifestLock {
    file: File,
}

impl ManifestLock {
    pub fn acquire(data_root: &Path) -> Result<Self> {
        let dir = data_root.join("manifests");
        fs::create_dir_all(&dir)?;
        let path = dir.join(".manifest.lock");
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(path)?;
        file.lock_exclusive()?;
        Ok(Self { file })
    }
}

impl Drop for ManifestLock {
    fn drop(&mut self) {
        let _ = FileExt::unlock(&self.file);
    }
}

pub struct RawSnapshotStore {
    root: PathBuf,
}

impl RawSnapshotStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }

    pub fn write(&self, envelope: &ObservationEnvelope) -> Result<RawSnapshotManifest> {
        let observed = envelope.observed_at;
        let rel_dir = PathBuf::from(&envelope.source_id)
            .join(&envelope.observation_type)
            .join(format!("year={:04}", observed.year()))
            .join(format!("month={:02}", observed.month()))
            .join(format!("day={:02}", observed.day()));
        let directory = self.root.join("raw").join(rel_dir);
        fs::create_dir_all(&directory)?;

        let payload = serde_json::to_vec(envelope)?;
        let digest = hex::encode(Sha256::digest(&payload));
        let stamp = format!(
            "{:04}{:02}{:02}T{:02}{:02}{:02}.{:06}Z",
            observed.year(),
            observed.month(),
            observed.day(),
            observed.hour(),
            observed.minute(),
            observed.second(),
            observed.timestamp_subsec_micros()
        );
        let path = directory.join(format!("{}_{}.json.gz", stamp, &digest[..12]));

        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .with_context(|| format!("create snapshot {}", path.display()))?;
        let mut encoder = GzEncoder::new(file, Compression::default());
        encoder.write_all(&payload)?;
        let file = encoder.finish()?;
        file.sync_all()?;
        let compressed_bytes = file.metadata()?.len();

        let manifest = RawSnapshotManifest {
            path: path.to_string_lossy().into_owned(),
            sha256: digest,
            bytes: payload.len() as u64,
            compressed_bytes: Some(compressed_bytes),
            observed_at: observed,
            source_id: envelope.source_id.clone(),
            observation_type: envelope.observation_type.clone(),
        };

        let _lock = ManifestLock::acquire(&self.root)?;
        append_audit_manifest_unlocked(&self.root, &manifest)?;
        append_daily_manifest_unlocked(&self.root, &manifest)?;
        update_series_state_unlocked(&self.root, &manifest)?;
        Ok(manifest)
    }
}

pub fn append_audit_manifest_unlocked(
    data_root: &Path,
    record: &RawSnapshotManifest,
) -> Result<PathBuf> {
    let dir = data_root.join("manifests");
    fs::create_dir_all(&dir)?;
    let path = dir.join("raw_snapshots.jsonl");
    let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
    serde_json::to_writer(&mut file, record)?;
    file.write_all(b"\n")?;
    file.sync_data()?;
    Ok(path)
}

pub fn append_daily_manifest_unlocked(
    data_root: &Path,
    record: &RawSnapshotManifest,
) -> Result<PathBuf> {
    let observed = record.observed_at;
    let path = data_root
        .join("manifests")
        .join("daily")
        .join(format!("year={:04}", observed.year()))
        .join(format!("month={:02}", observed.month()))
        .join(format!("day={:02}", observed.day()))
        .join("raw_snapshots.jsonl");
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
    serde_json::to_writer(&mut file, record)?;
    file.write_all(b"\n")?;
    file.sync_data()?;
    Ok(path)
}

pub fn update_series_state_unlocked(
    data_root: &Path,
    record: &RawSnapshotManifest,
) -> Result<PathBuf> {
    let source = safe_component(&record.source_id);
    let observation = safe_component(&record.observation_type);
    let path = data_root
        .join("manifests")
        .join("series")
        .join(source)
        .join(format!("{}.json", observation));
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }

    let now = Utc::now();
    let mut state = if path.exists() {
        serde_json::from_slice::<SeriesState>(&fs::read(&path)?)?
    } else {
        SeriesState {
            schema_version: 1,
            source_id: record.source_id.clone(),
            observation_type: record.observation_type.clone(),
            count: 0,
            first_observed_at: None,
            latest_observed_at: None,
            previous_observed_at: None,
            last_interval_seconds: None,
            max_interval_seconds: None,
            raw_bytes_total: 0,
            compressed_bytes_total: 0,
            compression_sample_records: 0,
            compression_sample_raw_bytes: 0,
            latest_manifest: None,
            updated_at: now,
        }
    };

    let previous = state.latest_observed_at;
    let interval =
        previous.map(|prev| (record.observed_at - prev).num_milliseconds() as f64 / 1000.0);
    state.count += 1;
    if state.first_observed_at.is_none() {
        state.first_observed_at = Some(record.observed_at);
    }
    state.previous_observed_at = previous;
    state.latest_observed_at = Some(record.observed_at);
    state.last_interval_seconds = interval;
    if let Some(interval) = interval {
        state.max_interval_seconds = Some(state.max_interval_seconds.unwrap_or(0.0).max(interval));
    }
    state.raw_bytes_total += record.bytes;
    if let Some(compressed) = record.compressed_bytes {
        state.compressed_bytes_total += compressed;
        state.compression_sample_records += 1;
        state.compression_sample_raw_bytes += record.bytes;
    }
    state.latest_manifest = Some(record.clone());
    state.updated_at = now;

    atomic_write_json(&path, &state)?;
    Ok(path)
}

pub fn rebuild_manifest_indexes(data_root: &Path) -> Result<usize> {
    let audit_path = data_root.join("manifests").join("raw_snapshots.jsonl");
    if !audit_path.exists() {
        anyhow::bail!("audit manifest missing: {}", audit_path.display());
    }

    let _lock = ManifestLock::acquire(data_root)?;
    let file = File::open(&audit_path)?;
    let mut records = Vec::new();
    for (idx, line) in BufReader::new(file).lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let record: RawSnapshotManifest = serde_json::from_str(&line)
            .with_context(|| format!("invalid audit manifest line {}", idx + 1))?;
        records.push(record);
    }
    records.sort_by_key(|r| r.observed_at);

    let daily_root = data_root.join("manifests").join("daily");
    let series_root = data_root.join("manifests").join("series");
    if daily_root.exists() {
        fs::remove_dir_all(&daily_root)?;
    }
    if series_root.exists() {
        fs::remove_dir_all(&series_root)?;
    }
    for record in &records {
        append_daily_manifest_unlocked(data_root, record)?;
        update_series_state_unlocked(data_root, record)?;
    }
    Ok(records.len())
}

pub fn read_audit_manifest(data_root: &Path) -> Result<Vec<RawSnapshotManifest>> {
    let path = data_root.join("manifests").join("raw_snapshots.jsonl");
    let file = File::open(&path)?;
    let mut records = Vec::new();
    for (idx, line) in BufReader::new(file).lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        records.push(
            serde_json::from_str(&line)
                .with_context(|| format!("invalid audit manifest line {}", idx + 1))?,
        );
    }
    Ok(records)
}

fn atomic_write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let tmp = path.with_extension("json.tmp");
    {
        let mut file = File::create(&tmp)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent()
        && let Ok(dir) = File::open(parent)
    {
        let _ = dir.sync_all();
    }
    Ok(())
}

fn safe_component(value: &str) -> String {
    value.replace('/', "_").replace("..", "_")
}

pub fn isoformat(dt: DateTime<Utc>) -> String {
    dt.to_rfc3339_opts(SecondsFormat::Micros, false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn safe_component_matches_python_contract() {
        assert_eq!(safe_component("a/b"), "a_b");
        assert_eq!(safe_component("a..b"), "a_b");
    }

    #[test]
    fn manifest_json_round_trip() {
        let record = RawSnapshotManifest {
            path: "/tmp/x.json.gz".into(),
            sha256: "abc".into(),
            bytes: 10,
            compressed_bytes: Some(8),
            observed_at: "2026-09-05T12:34:56Z".parse().unwrap(),
            source_id: "hyperliquid".into(),
            observation_type: "market_state".into(),
        };
        let encoded = serde_json::to_string(&record).unwrap();
        let decoded: RawSnapshotManifest = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded, record);
    }
}
