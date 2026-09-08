use crate::v02_freeze::sha256_file;
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
use std::path::{Path, PathBuf};

pub const LEGACY_ATTESTATION_PROTOCOL: &str =
    "CROSSALPHA_STATE_V0_2_PYTHON_LEGACY_INTEGRITY_ATTESTATION";
pub const LEGACY_ATTESTATION_SCHEMA_VERSION: u32 = 1;

pub fn attestation_path(data_root: &Path) -> PathBuf {
    data_root.join("research/state_v02/legacy_python_integrity_attestation.json")
}

pub fn ledger_snapshot(data_root: &Path, before: Option<DateTime<Utc>>) -> Result<Value> {
    let root = data_root.join("research/state_v02/prospective");
    let mut files = Vec::new();
    collect_json_files(&root, &mut files)?;
    files.sort();

    let mut digest = Sha256::new();
    let mut count = 0_u64;
    for path in files {
        let row: Value = serde_json::from_reader(File::open(&path)?)?;
        let generated_at = row
            .get("generated_at")
            .and_then(Value::as_str)
            .context("legacy State V0.2 observation generated_at missing")?;
        let generated_at = DateTime::parse_from_rfc3339(generated_at)?.with_timezone(&Utc);
        if before.is_some_and(|boundary| generated_at >= boundary) {
            continue;
        }
        let relative = path
            .strip_prefix(data_root)
            .with_context(|| format!("{} is outside data root", path.display()))?
            .to_string_lossy()
            .replace('\\', "/");
        let file_hash = sha256_file(&path)?;
        digest.update(relative.as_bytes());
        digest.update([0_u8]);
        digest.update(file_hash.as_bytes());
        digest.update(*b"\n");
        count += 1;
    }

    Ok(json!({
        "observation_count": count,
        "ledger_root_sha256": format!("{:x}", digest.finalize()),
    }))
}

pub fn verify_attestation_for_binding(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let path = attestation_path(data_root);
    if !path.is_file() {
        bail!(
            "State V0.2 Python legacy integrity attestation missing: {}",
            path.display()
        );
    }
    let attestation: Value = serde_json::from_reader(File::open(&path)?)?;
    if attestation.get("protocol").and_then(Value::as_str) != Some(LEGACY_ATTESTATION_PROTOCOL)
        || attestation.get("schema_version").and_then(Value::as_u64)
            != Some(LEGACY_ATTESTATION_SCHEMA_VERSION as u64)
        || attestation
            .get("python_integrity_ok")
            .and_then(Value::as_bool)
            != Some(true)
    {
        bail!("State V0.2 Python legacy integrity attestation is invalid");
    }

    let freeze_path = data_root.join("research/state_v02/freeze.json");
    let freeze_hash = sha256_file(&freeze_path)?;
    if attestation
        .get("freeze_file_sha256")
        .and_then(Value::as_str)
        != Some(freeze_hash.as_str())
    {
        bail!("State V0.2 legacy freeze changed after Python integrity attestation");
    }

    let expected = json!({
        "observation_count": attestation
            .get("observation_count")
            .and_then(Value::as_u64)
            .context("legacy attestation observation_count missing")?,
        "ledger_root_sha256": attestation
            .get("ledger_root_sha256")
            .and_then(Value::as_str)
            .context("legacy attestation ledger_root_sha256 missing")?,
    });
    let current_all = ledger_snapshot(data_root, None)?;
    if current_all != expected {
        bail!("State V0.2 legacy ledger changed after Python integrity attestation");
    }
    let current_before_binding = ledger_snapshot(data_root, Some(bound_at))?;
    if current_before_binding != expected {
        bail!("State V0.2 binding boundary does not cover exactly the attested legacy ledger");
    }

    Ok(json!({
        "path": path.to_string_lossy(),
        "file_sha256": sha256_file(&path)?,
        "protocol": LEGACY_ATTESTATION_PROTOCOL,
        "python_integrity_ok": true,
        "observation_count": expected["observation_count"],
        "ledger_root_sha256": expected["ledger_root_sha256"],
        "freeze_file_sha256": freeze_hash,
    }))
}

pub fn verify_bound_legacy_ledger(
    data_root: &Path,
    bound_at: DateTime<Utc>,
    binding: &Value,
) -> Result<bool> {
    let Some(expected) = binding.get("legacy_python_integrity_attestation") else {
        return Ok(false);
    };
    if expected.get("protocol").and_then(Value::as_str) != Some(LEGACY_ATTESTATION_PROTOCOL)
        || expected.get("python_integrity_ok").and_then(Value::as_bool) != Some(true)
    {
        return Ok(false);
    }
    let snapshot = ledger_snapshot(data_root, Some(bound_at))?;
    Ok(
        snapshot.get("observation_count") == expected.get("observation_count")
            && snapshot.get("ledger_root_sha256") == expected.get("ledger_root_sha256"),
    )
}

fn collect_json_files(root: &Path, output: &mut Vec<PathBuf>) -> Result<()> {
    if !root.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if path.is_dir() {
            collect_json_files(&path, output)?;
        } else if path.extension().and_then(|value| value.to_str()) == Some("json") {
            output.push(path);
        }
    }
    Ok(())
}
