use crate::v03_freeze::{payload_hash, verify_legacy_v1_seal};
use crate::v04::{ACTIONABILITY, FUNDING_SEMANTICS, MODE, NativeStateV04, PROTOCOL};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub const PROSPECTIVE_PROTOCOL: &str = "CROSSALPHA_STATE_V0_4_PROSPECTIVE";
pub const FREEZE_SCHEMA_VERSION: u32 = 1;

pub fn freeze_path(data_root: &Path) -> PathBuf {
    data_root.join("research/state_v04/freeze.json")
}

pub fn freeze_preview(data_root: &Path, frozen_at: DateTime<Utc>) -> Result<Value> {
    let config = NativeStateV04::strict_config_report(&repo_root().join("config/state_v04.yaml"))?;
    if !config.ok {
        bail!("State V0.4 config/implementation mismatch");
    }
    let mut references = BTreeMap::new();
    for (name, path) in reference_paths(data_root) {
        if !path.exists() {
            bail!(
                "State V0.4 requires frozen predecessor protocol before freeze: {}",
                path.display()
            );
        }
        references.insert(
            name.to_owned(),
            json!({
                "path": path.to_string_lossy(),
                "file_sha256": sha256_file(&path)?,
            }),
        );
    }
    let payload = json!({
        "schema_version": FREEZE_SCHEMA_VERSION,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": PROTOCOL,
        "mode": MODE,
        "maturity": "O0_DATA_TO_O1_DESCRIPTION",
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "frozen_at": frozen_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "first_eligible_generated_at": frozen_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "retrospective_backfill_allowed": false,
        "parameter_optimization_allowed": false,
        "automatic_actionability_allowed": false,
        "no_composite_stress_score": true,
        "funding_semantics": FUNDING_SEMANTICS,
        "implementation_file_sha256": implementation_hashes()?,
        "reference_freezes": references,
        "promotion_gate": {
            "minimum_calendar_days_before_O2_candidate": 180,
            "minimum_observations": 500,
            "minimum_valid_venue_share": 0.95,
            "requires_outcome_linkage_test": true,
            "requires_predeclared_O2_rule": true,
            "automatic_promotion_to_actionable_modifier_allowed": false,
        },
    });
    seal(payload)
}

pub fn write_freeze(data_root: &Path, frozen_at: DateTime<Utc>) -> Result<Value> {
    let path = freeze_path(data_root);
    if path.exists() {
        let existing: Value = serde_json::from_reader(File::open(&path)?)?;
        if !verify_legacy_v1_seal(&existing)? {
            bail!("existing State V0.4 freeze failed seal verification");
        }
        return Ok(existing);
    }
    let payload = freeze_preview(data_root, frozen_at)?;
    write_immutable(&path, &payload)?;
    Ok(payload)
}

pub fn verify_freeze_file(path: &Path) -> Result<bool> {
    if !path.exists() {
        return Ok(false);
    }
    let value: Value = serde_json::from_reader(File::open(path)?)?;
    verify_legacy_v1_seal(&value)
}

pub fn verify_hash_graph(data_root: &Path, freeze: &Value) -> Result<bool> {
    let expected_impl = freeze
        .get("implementation_file_sha256")
        .and_then(Value::as_object)
        .context("State V0.4 implementation hash map missing")?;
    for (name, current) in implementation_hashes()? {
        if expected_impl.get(&name).and_then(Value::as_str) != Some(current.as_str()) {
            return Ok(false);
        }
    }
    let expected_refs = freeze
        .get("reference_freezes")
        .and_then(Value::as_object)
        .context("State V0.4 reference freeze map missing")?;
    for (name, path) in reference_paths(data_root) {
        let current = sha256_file(&path)?;
        let expected = expected_refs
            .get(name)
            .and_then(Value::as_object)
            .and_then(|row| row.get("file_sha256"))
            .and_then(Value::as_str);
        if expected != Some(current.as_str()) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn seal(mut value: Value) -> Result<Value> {
    let digest = payload_hash(&value)?;
    value
        .as_object_mut()
        .context("State V0.4 freeze must be object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(value)
}

fn implementation_hashes() -> Result<BTreeMap<String, String>> {
    let files = [
        ("state_v04", "src/crossalpha/state/v04.py"),
        ("state_v04_provider", "src/crossalpha/state/v04_provider.py"),
        (
            "state_v04_safe_provider",
            "src/crossalpha/state/v04_safe_provider.py",
        ),
        ("state_v04_cycle", "src/crossalpha/state/v04_cycle.py"),
        (
            "state_v04_prospective",
            "src/crossalpha/state/v04_prospective.py",
        ),
        ("state_v04_config", "src/crossalpha/state/v04_config.py"),
        ("config", "config/state_v04.yaml"),
    ];
    let root = repo_root();
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        result.insert(name.to_owned(), sha256_file(&root.join(relative))?);
    }
    Ok(result)
}

fn reference_paths(data_root: &Path) -> [(&'static str, PathBuf); 4] {
    [
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
        (
            "state_v03",
            data_root.join("research/state_v03/freeze.json"),
        ),
    ]
}

fn write_immutable(path: &Path, value: &Value) -> Result<()> {
    if path.exists() {
        bail!(
            "immutable State V0.4 file already exists: {}",
            path.display()
        );
    }
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

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
