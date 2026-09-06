use crate::v03::{ACTIONABILITY, MODE, PROSPECTIVE_PROTOCOL, PROTOCOL, StateV03};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

pub const FREEZE_SCHEMA_VERSION: u32 = 1;

pub fn legacy_v1_freeze_preview(
    data_root: &Path,
    minimum_eligible_block: u64,
    frozen_at: DateTime<Utc>,
) -> Result<Value> {
    let repo_root = repo_root();
    let config_path = repo_root.join("config/state_v03.yaml");
    let config_report = StateV03::strict_config_report(&config_path)?;
    if !config_report.ok {
        bail!("State V0.3 config/implementation mismatch");
    }

    let references = reference_freezes(data_root)?;
    let implementation = implementation_hashes(&repo_root)?;
    let frozen_at = frozen_at.to_rfc3339_opts(SecondsFormat::Micros, false);
    let payload = json!({
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": PROTOCOL,
        "mode": MODE,
        "maturity": "O0_DATA_TO_O1_DESCRIPTION",
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "frozen_at": frozen_at,
        "minimum_eligible_block": minimum_eligible_block,
        "historical_bootstrap_is_evidence": false,
        "retrospective_backfill_allowed": false,
        "parameter_optimization_allowed": false,
        "automatic_actionability_allowed": false,
        "implementation_file_sha256": implementation,
        "reference_freezes": references,
        "promotion_gate": {
            "minimum_calendar_days_before_O2_candidate": 180,
            "minimum_valid_full_censuses": 120,
            "minimum_distinct_cliff_stress_episodes": 5,
            "cliff_episode_critical_debt_share_threshold": 0.05,
            "cliff_episode_cooldown_hours": 24,
            "requires_complete_borrower_bootstrap": true,
            "requires_outcome_linkage_test": true,
            "requires_predeclared_O2_rule": true,
            "automatic_promotion_to_actionable_modifier_allowed": false
        }
    });
    seal(payload)
}

pub fn verify_legacy_v1_seal(value: &Value) -> Result<bool> {
    let expected = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("State V0.3 record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

pub fn payload_hash(value: &Value) -> Result<String> {
    let mut payload = value.clone();
    let object = payload
        .as_object_mut()
        .context("State V0.3 sealed payload must be an object")?;
    object.remove("record_sha256");
    let bytes = canonical_json_bytes(&payload)?;
    Ok(hex_sha256(&bytes))
}

fn seal(mut payload: Value) -> Result<Value> {
    let digest = payload_hash(&payload)?;
    payload
        .as_object_mut()
        .context("State V0.3 freeze payload must be an object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(payload)
}

fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>> {
    // serde_json::Map is key-sorted without the preserve_order feature. Rebuild every
    // object recursively to make the Python sort_keys=True contract explicit.
    let sorted = sort_json(value);
    Ok(serde_json::to_vec(&sorted)?)
}

fn sort_json(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let sorted: BTreeMap<String, Value> = object
                .iter()
                .map(|(key, value)| (key.clone(), sort_json(value)))
                .collect();
            let map: Map<String, Value> = sorted.into_iter().collect();
            Value::Object(map)
        }
        Value::Array(values) => Value::Array(values.iter().map(sort_json).collect()),
        _ => value.clone(),
    }
}

fn implementation_hashes(repo_root: &Path) -> Result<BTreeMap<String, String>> {
    let files = [
        ("state_v03", "src/crossalpha/state/v03.py"),
        ("state_v03_rpc", "src/crossalpha/state/v03_rpc.py"),
        ("state_v03_logs", "src/crossalpha/state/v03_logs.py"),
        ("state_v03_preflight", "src/crossalpha/state/v03_preflight.py"),
        ("state_v03_cycle", "src/crossalpha/state/v03_cycle.py"),
        ("state_v03_watchlist", "src/crossalpha/state/v03_watchlist.py"),
        ("state_v03_prospective", "src/crossalpha/state/v03_prospective.py"),
        ("state_v03_config", "src/crossalpha/state/v03_config.py"),
        ("config", "config/state_v03.yaml"),
    ];
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        result.insert(name.to_owned(), sha256_file(&repo_root.join(relative))?);
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
        (
            "state_v02",
            data_root.join("research/state_v02/freeze.json"),
        ),
    ];
    let mut result = BTreeMap::new();
    for (name, path) in paths {
        if !path.exists() {
            bail!(
                "State V0.3 requires frozen predecessor protocol before freeze: {}",
                path.display()
            );
        }
        result.insert(
            name.to_owned(),
            json!({
                "path": path.to_string_lossy(),
                "file_sha256": sha256_file(&path)?,
            }),
        );
    }
    Ok(result)
}

fn sha256_file(path: &Path) -> Result<String> {
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

fn hex_sha256(bytes: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(bytes);
    format!("{:x}", digest.finalize())
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seal_round_trip_is_self_consistent() {
        let sealed = seal(json!({"b": 2, "a": {"z": 3, "x": 1}})).unwrap();
        assert!(verify_legacy_v1_seal(&sealed).unwrap());
    }

    #[test]
    fn canonical_json_is_key_sorted_and_compact() {
        let bytes = canonical_json_bytes(&json!({"b": 2, "a": 1})).unwrap();
        assert_eq!(String::from_utf8(bytes).unwrap(), r#"{"a":1,"b":2}"#);
    }
}
