use crate::v02::{ACTIONABILITY, MODE, PROSPECTIVE_PROTOCOL, PROTOCOL, strict_config_report};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub const FREEZE_SCHEMA_VERSION: u32 = 1;
pub const EXPECTED_CADENCE_MINUTES: u64 = 15;

pub fn freeze_path(data_root: &Path) -> PathBuf {
    data_root.join("research/state_v02/freeze.json")
}

pub fn legacy_v1_freeze_preview(data_root: &Path, frozen_at: DateTime<Utc>) -> Result<Value> {
    let root = repo_root();
    let report = strict_config_report(&root.join("config/state_v02.yaml"))?;
    if !report.ok {
        bail!("State V0.2 config/implementation mismatch");
    }
    let frozen = frozen_at.to_rfc3339_opts(SecondsFormat::Micros, false);
    let payload = json!({
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": PROTOCOL,
        "mode": MODE,
        "maturity": "O0_DATA_TO_O1_DESCRIPTION",
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "frozen_at": frozen,
        "first_eligible_observed_at": frozen,
        "expected_cadence_minutes": EXPECTED_CADENCE_MINUTES,
        "retrospective_backfill_allowed": false,
        "parameter_optimization_allowed": false,
        "historical_data_can_promote_to_O2": false,
        "implementation_file_sha256": implementation_hashes(&root)?,
        "reference_freezes": reference_freezes(data_root)?,
        "promotion_gate": {
            "minimum_calendar_days_before_O2_candidate": 180,
            "minimum_observations": 500,
            "minimum_distinct_stress_episodes": 5,
            "stress_episode_score_threshold": 0.67,
            "stress_episode_cooldown_hours": 24,
            "requires_outcome_linkage_test": true,
            "requires_predeclared_O2_rule": true,
            "maximum_automatic_state": "ELIGIBLE_FOR_O2_PROTOCOL_DESIGN"
        }
    });
    seal(payload)
}

pub fn write_freeze(data_root: &Path, frozen_at: DateTime<Utc>) -> Result<Value> {
    let path = freeze_path(data_root);
    if path.exists() {
        let existing: Value = serde_json::from_reader(File::open(&path)?)?;
        if !verify_seal(&existing)? {
            bail!("existing State V0.2 freeze failed seal verification");
        }
        return Ok(existing);
    }
    let payload = legacy_v1_freeze_preview(data_root, frozen_at)?;
    write_atomic(&path, &payload)?;
    Ok(payload)
}

pub fn verify_freeze_file(path: &Path) -> Result<bool> {
    if !path.exists() {
        return Ok(false);
    }
    let value: Value = serde_json::from_reader(File::open(path)?)?;
    verify_seal(&value)
}

pub fn verify_seal(value: &Value) -> Result<bool> {
    let expected = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("State V0.2 record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

pub fn payload_hash(value: &Value) -> Result<String> {
    let mut payload = value.clone();
    payload
        .as_object_mut()
        .context("State V0.2 sealed payload must be an object")?
        .remove("record_sha256");
    let bytes = serde_json::to_vec(&sort_json(&payload))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

pub fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path).with_context(|| format!("open {}", path.display()))?;
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
    let hash = payload_hash(&value)?;
    value
        .as_object_mut()
        .context("State V0.2 freeze payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(hash));
    Ok(value)
}

fn implementation_hashes(root: &Path) -> Result<BTreeMap<String, String>> {
    let files = [
        ("state_v02", "src/crossalpha/state/v02.py"),
        ("prospective_v02", "src/crossalpha/state/v02_prospective.py"),
        (
            "aave_provider",
            "src/crossalpha/observatory/providers/aave.py",
        ),
        (
            "aave_canonical",
            "src/crossalpha/observatory/canonical/aave.py",
        ),
        ("config", "config/state_v02.yaml"),
    ];
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        result.insert(name.to_owned(), sha256_file(&root.join(relative))?);
    }
    Ok(result)
}

fn reference_freezes(data_root: &Path) -> Result<BTreeMap<String, Value>> {
    let paths = [
        (
            "frozen_b3",
            data_root.join("research/free_v01/paper/freeze.json"),
        ),
        (
            "state_ab_v01",
            data_root.join("research/free_v01/state_ab_v01/freeze.json"),
        ),
    ];
    let mut result = BTreeMap::new();
    for (name, path) in paths {
        if !path.exists() {
            bail!(
                "State V0.2 requires frozen predecessor reference: {}",
                path.display()
            );
        }
        result.insert(
            name.to_owned(),
            json!({"path": path.to_string_lossy(), "file_sha256": sha256_file(&path)?}),
        );
    }
    Ok(result)
}

fn sort_json(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let sorted: BTreeMap<String, Value> = object
                .iter()
                .map(|(key, value)| (key.clone(), sort_json(value)))
                .collect();
            Value::Object(sorted.into_iter().collect::<Map<String, Value>>())
        }
        Value::Array(values) => Value::Array(values.iter().map(sort_json).collect()),
        _ => value.clone(),
    }
}

fn write_atomic(path: &Path, value: &Value) -> Result<()> {
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

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
