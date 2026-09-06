use crate::v02::{PROTOCOL, PROSPECTIVE_PROTOCOL};
use crate::v02_freeze::{payload_hash, sha256_file, verify_seal};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const RUNTIME_BINDING_PROTOCOL: &str = "CROSSALPHA_STATE_V0_2_RUST_RUNTIME_BINDING";
pub const RUNTIME_BINDING_SCHEMA_VERSION: u32 = 1;

pub fn runtime_binding_path(data_root: &Path) -> PathBuf {
    data_root.join("research/state_v02/rust_runtime_binding.json")
}

pub fn runtime_binding_preview(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let freeze_path = data_root.join("research/state_v02/freeze.json");
    if !freeze_path.exists() {
        bail!("State V0.2 legacy freeze missing: {}", freeze_path.display());
    }
    let freeze: Value = serde_json::from_reader(File::open(&freeze_path)?)?;
    if !verify_seal(&freeze)? {
        bail!("State V0.2 legacy freeze seal invalid");
    }
    if freeze.get("protocol").and_then(Value::as_str) != Some(PROSPECTIVE_PROTOCOL) {
        bail!("State V0.2 freeze protocol mismatch");
    }
    let root = repo_root();
    let cargo_lock = root.join("Cargo.lock");
    let lock_present = cargo_lock.is_file();
    let lock_tracked = lock_present && git_tracks_cargo_lock(&root);
    let lock_hash = lock_present.then(|| sha256_file(&cargo_lock)).transpose()?;
    let mut payload = json!({
        "schema_version": RUNTIME_BINDING_SCHEMA_VERSION,
        "protocol": RUNTIME_BINDING_PROTOCOL,
        "state_protocol": PROTOCOL,
        "legacy_freeze": {
            "path": freeze_path.to_string_lossy(),
            "file_sha256": sha256_file(&freeze_path)?,
            "record_sha256": freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        },
        "bound_at": bound_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "runtime": "RUST_TOKIO",
        "python_runtime_required": false,
        "cargo_lock_present": lock_present,
        "cargo_lock_tracked": lock_tracked,
        "cargo_lock_sha256": lock_hash,
        "native_source_sha256": native_source_hashes(&root)?,
        "production_binding_eligible": lock_present && lock_tracked,
    });
    let digest = payload_hash(&payload)?;
    payload
        .as_object_mut()
        .context("runtime binding payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(payload)
}

pub fn write_runtime_binding(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let path = runtime_binding_path(data_root);
    if path.exists() {
        if !verify_runtime_binding_file(&path)? {
            bail!("existing State V0.2 Rust runtime binding invalid or stale");
        }
        return Ok(serde_json::from_reader(File::open(path)?)?);
    }
    let value = runtime_binding_preview(data_root, bound_at)?;
    if value.get("production_binding_eligible").and_then(Value::as_bool) != Some(true) {
        bail!("State V0.2 Rust binding refused: Cargo.lock must exist and be tracked");
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    {
        let mut file = File::create(&tmp)?;
        serde_json::to_writer_pretty(&mut file, &value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, &path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(value)
}

pub fn verify_runtime_binding_file(path: &Path) -> Result<bool> {
    if !path.exists() {
        return Ok(false);
    }
    let current: Value = serde_json::from_reader(File::open(path)?)?;
    let bound_at = current
        .get("bound_at")
        .and_then(Value::as_str)
        .context("runtime binding bound_at missing")?;
    let bound_at = DateTime::parse_from_rfc3339(bound_at)?.with_timezone(&Utc);
    let data_root = path
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .context("binding path is not under <data_root>/research/state_v02")?;
    Ok(runtime_binding_preview(data_root, bound_at)? == current)
}

fn native_source_hashes(root: &Path) -> Result<BTreeMap<String, String>> {
    let files = [
        ("workspace_cargo", "Cargo.toml"),
        ("storage_cargo", "crates/crossalpha-storage/Cargo.toml"),
        ("storage_lib", "crates/crossalpha-storage/src/lib.rs"),
        ("storage_recent", "crates/crossalpha-storage/src/recent.rs"),
        ("features_cargo", "crates/crossalpha-features/Cargo.toml"),
        ("features_lib", "crates/crossalpha-features/src/lib.rs"),
        ("features_aave", "crates/crossalpha-features/src/canonical/aave.rs"),
        ("features_hyperliquid", "crates/crossalpha-features/src/canonical/hyperliquid.rs"),
        ("features_stablecoins", "crates/crossalpha-features/src/canonical/stablecoins.rs"),
        ("features_market_state", "crates/crossalpha-features/src/market_state.rs"),
        ("features_stablecoin_state", "crates/crossalpha-features/src/stablecoin_state.rs"),
        ("features_recent", "crates/crossalpha-features/src/recent_features.rs"),
        ("state_cargo", "crates/crossalpha-state/Cargo.toml"),
        ("state_lib", "crates/crossalpha-state/src/lib.rs"),
        ("state_v02", "crates/crossalpha-state/src/v02.rs"),
        ("state_v02_provider", "crates/crossalpha-state/src/v02/provider.rs"),
        ("state_v02_artifacts", "crates/crossalpha-state/src/v02/artifacts.rs"),
        ("state_v02_cycle", "crates/crossalpha-state/src/v02/cycle.rs"),
        ("state_v02_engine", "crates/crossalpha-state/src/v02/engine.rs"),
        ("state_v02_freeze", "crates/crossalpha-state/src/v02/freeze.rs"),
        ("state_v02_prospective", "crates/crossalpha-state/src/v02/prospective.rs"),
        ("state_v02_runtime_binding", "crates/crossalpha-state/src/v02/runtime_binding.rs"),
        ("cli_state", "crates/crossalpha-cli/src/bin/state.rs"),
        ("cli_daemon", "crates/crossalpha-cli/src/bin/daemon.rs"),
        ("config", "config/state_v02.yaml"),
    ];
    let mut hashes = BTreeMap::new();
    for (name, relative) in files {
        let path = root.join(relative);
        if !path.exists() {
            bail!("State V0.2 binding source missing: {}", path.display());
        }
        hashes.insert(name.to_owned(), sha256_file(&path)?);
    }
    Ok(hashes)
}

fn git_tracks_cargo_lock(root: &Path) -> bool {
    Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "--error-unmatch", "Cargo.lock"])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
