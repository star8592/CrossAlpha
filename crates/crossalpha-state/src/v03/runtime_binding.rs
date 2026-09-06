use crate::v03::{PROTOCOL, PROSPECTIVE_PROTOCOL};
use crate::v03_freeze::{payload_hash, verify_legacy_v1_seal};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};

pub const RUNTIME_BINDING_PROTOCOL: &str = "CROSSALPHA_STATE_V0_3_RUST_RUNTIME_BINDING";
pub const RUNTIME_BINDING_SCHEMA_VERSION: u32 = 1;

pub fn runtime_binding_preview(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let legacy_path = data_root.join("research/state_v03/freeze.json");
    if !legacy_path.exists() {
        bail!("State V0.3 legacy freeze missing: {}", legacy_path.display());
    }
    let legacy: Value = serde_json::from_reader(
        File::open(&legacy_path).with_context(|| format!("open {}", legacy_path.display()))?,
    )?;
    if !verify_legacy_v1_seal(&legacy)? {
        bail!("State V0.3 legacy freeze seal is invalid");
    }
    if legacy.get("protocol").and_then(Value::as_str) != Some(PROSPECTIVE_PROTOCOL) {
        bail!("State V0.3 legacy freeze protocol mismatch");
    }

    let repo_root = repo_root();
    let cargo_lock = repo_root.join("Cargo.lock");
    let lockfile_present = cargo_lock.is_file();
    let lockfile_sha256 = lockfile_present
        .then(|| sha256_file(&cargo_lock))
        .transpose()?;
    let source_hashes = native_source_hashes(&repo_root)?;

    let mut payload = json!({
        "schema_version": RUNTIME_BINDING_SCHEMA_VERSION,
        "protocol": RUNTIME_BINDING_PROTOCOL,
        "state_protocol": PROTOCOL,
        "legacy_freeze": {
            "path": legacy_path.to_string_lossy(),
            "file_sha256": sha256_file(&legacy_path)?,
            "record_sha256": legacy.get("record_sha256").cloned().unwrap_or(Value::Null),
        },
        "bound_at": bound_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "runtime": "RUST_TOKIO",
        "python_runtime_required": false,
        "cargo_lock_present": lockfile_present,
        "cargo_lock_sha256": lockfile_sha256,
        "native_source_sha256": source_hashes,
        "production_binding_eligible": lockfile_present,
    });
    let digest = payload_hash(&payload)?;
    payload
        .as_object_mut()
        .context("runtime binding payload must be an object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(payload)
}

pub fn verify_runtime_binding(value: &Value) -> Result<bool> {
    let expected = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("runtime binding record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

fn native_source_hashes(repo_root: &Path) -> Result<BTreeMap<String, String>> {
    let files = [
        ("workspace_cargo", "Cargo.toml"),
        ("state_cargo", "crates/crossalpha-state/Cargo.toml"),
        ("state_lib", "crates/crossalpha-state/src/lib.rs"),
        ("state_v03", "crates/crossalpha-state/src/v03.rs"),
        ("state_v03_preflight", "crates/crossalpha-state/src/v03/preflight.rs"),
        ("state_v03_freeze", "crates/crossalpha-state/src/v03/freeze.rs"),
        ("cli_cargo", "crates/crossalpha-cli/Cargo.toml"),
        (
            "cli_v03_config",
            "crates/crossalpha-cli/src/bin/state_v03_config_check.rs",
        ),
        (
            "cli_v03_preflight",
            "crates/crossalpha-cli/src/bin/state_v03_preflight.rs",
        ),
        (
            "cli_v03_freeze_preview",
            "crates/crossalpha-cli/src/bin/state_v03_freeze_preview.rs",
        ),
        ("config", "config/state_v03.yaml"),
    ];
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        result.insert(name.to_owned(), sha256_file(&repo_root.join(relative))?);
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

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binding_protocol_is_explicitly_rust_native() {
        assert_eq!(RUNTIME_BINDING_SCHEMA_VERSION, 1);
        assert!(RUNTIME_BINDING_PROTOCOL.contains("RUST_RUNTIME_BINDING"));
    }
}
