use crate::v03_freeze::payload_hash;
use crate::v04::PROTOCOL;
use crate::v04_freeze::{freeze_path, verify_freeze_file};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

pub const RUNTIME_BINDING_PROTOCOL: &str = "CROSSALPHA_STATE_V0_4_RUST_RUNTIME_BINDING";

pub fn runtime_binding_path(data_root: &Path) -> PathBuf {
    data_root.join("research/state_v04/rust_runtime_binding.json")
}

pub fn runtime_binding_preview(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let freeze = freeze_path(data_root);
    if !verify_freeze_file(&freeze)? {
        bail!("State V0.4 legacy freeze missing or invalid");
    }
    let root = repo_root();
    let cargo_lock = root.join("Cargo.lock");
    let lock_present = cargo_lock.is_file();
    let lock_tracked = lock_present && git_tracks(&root, "Cargo.lock");
    let mut payload = json!({
        "schema_version": 1,
        "protocol": RUNTIME_BINDING_PROTOCOL,
        "state_protocol": PROTOCOL,
        "legacy_freeze": {
            "path": freeze.to_string_lossy(),
            "file_sha256": sha256_file(&freeze)?,
        },
        "bound_at": bound_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "runtime": "RUST_TOKIO",
        "python_runtime_required": false,
        "cargo_lock_present": lock_present,
        "cargo_lock_tracked": lock_tracked,
        "cargo_lock_sha256": lock_present.then(|| sha256_file(&cargo_lock)).transpose()?,
        "native_source_sha256": native_source_hashes(&root)?,
        "production_binding_eligible": lock_present && lock_tracked,
    });
    let digest = payload_hash(&payload)?;
    payload
        .as_object_mut()
        .context("State V0.4 binding must be object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(payload)
}

pub fn write_runtime_binding(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let path = runtime_binding_path(data_root);
    if path.exists() {
        if !verify_runtime_binding_file(&path)? {
            bail!("existing State V0.4 Rust runtime binding invalid or stale");
        }
        return Ok(serde_json::from_reader(File::open(path)?)?);
    }
    let payload = runtime_binding_preview(data_root, bound_at)?;
    if payload.get("production_binding_eligible").and_then(Value::as_bool) != Some(true) {
        bail!("State V0.4 Rust runtime binding refused: Cargo.lock must exist and be tracked");
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut file = File::create(&tmp)?;
        serde_json::to_writer_pretty(&mut file, &payload)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, &path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(payload)
}

pub fn verify_runtime_binding_file(path: &Path) -> Result<bool> {
    if !path.exists() {
        return Ok(false);
    }
    let value: Value = serde_json::from_reader(File::open(path)?)?;
    let digest = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("State V0.4 binding record_sha256 missing")?;
    if digest != payload_hash(&value)? {
        return Ok(false);
    }
    if value.get("production_binding_eligible").and_then(Value::as_bool) != Some(true) {
        return Ok(false);
    }
    let bound_at = value
        .get("bound_at")
        .and_then(Value::as_str)
        .context("State V0.4 binding bound_at missing")?;
    let bound_at = DateTime::parse_from_rfc3339(bound_at)?.with_timezone(&Utc);
    let data_root = path
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .context("State V0.4 binding path invalid")?;
    Ok(runtime_binding_preview(data_root, bound_at)? == value)
}

fn native_source_hashes(root: &Path) -> Result<BTreeMap<String, String>> {
    let files = [
        ("workspace_cargo", "Cargo.toml"),
        ("state_cargo", "crates/crossalpha-state/Cargo.toml"),
        ("state_lib", "crates/crossalpha-state/src/lib.rs"),
        ("state_v04", "crates/crossalpha-state/src/v04.rs"),
        ("state_v04_artifacts", "crates/crossalpha-state/src/v04/artifacts.rs"),
        ("state_v04_cycle", "crates/crossalpha-state/src/v04/cycle.rs"),
        ("state_v04_freeze", "crates/crossalpha-state/src/v04/freeze.rs"),
        ("state_v04_provider", "crates/crossalpha-state/src/v04/provider.rs"),
        ("state_v04_runtime_binding", "crates/crossalpha-state/src/v04/runtime_binding.rs"),
        ("config", "config/state_v04.yaml"),
    ];
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        result.insert(name.to_owned(), sha256_file(&root.join(relative))?);
    }
    Ok(result)
}

fn git_tracks(root: &Path, path: &str) -> bool {
    Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "--error-unmatch", path])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
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

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
