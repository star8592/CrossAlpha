use crate::StateRuntimeContext;
use crate::v03::StateV03;
use crate::v03_cycle;
use crate::v03_freeze::{freeze_path, verify_legacy_v1_freeze_file, write_legacy_v1_freeze};
use crate::v03_preflight;
use crate::v03_prospective::{prospective_integrity, write_full_census_observation};
use crate::v03_runtime_binding::{
    runtime_binding_path, verify_runtime_binding_file, write_runtime_binding,
};
use anyhow::{Context, Result, bail};
use chrono::Utc;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;

pub async fn freeze_native(context: &StateRuntimeContext) -> Result<Value> {
    ensure_tracked_lockfile()?;
    let config_path = repo_root().join("config/state_v03.yaml");
    let config = StateV03::strict_config_report(&config_path)?;
    if !config.ok {
        bail!("STATE V0.3 FREEZE REFUSED: strict config consistency failed");
    }
    let preflight = v03_preflight::run_preflight(
        context.http_timeout,
        context.evm_rpc_url.as_deref(),
    )
    .await?;
    let freeze = write_legacy_v1_freeze(
        &context.data_root,
        preflight.finalized_block,
        Utc::now(),
    )?;
    let binding = write_runtime_binding(&context.data_root, Utc::now())?;
    Ok(json!({
        "protocol": "CROSSALPHA_STATE_V0_3_NATIVE_FREEZE",
        "status": "frozen_and_rust_bound",
        "minimum_eligible_block": preflight.finalized_block,
        "preflight": preflight,
        "freeze": freeze,
        "runtime_binding": binding,
    }))
}

pub async fn run_cycle_native(context: &StateRuntimeContext) -> Result<Value> {
    let mut report = v03_cycle::run_cycle(context).await?;
    let pending = report
        .get("prospective")
        .and_then(Value::as_object)
        .and_then(|object| object.get("status"))
        .and_then(Value::as_str)
        == Some("native_prospective_write_pending_ledger_gate");
    if pending {
        let artifacts = report
            .get("artifacts")
            .and_then(Value::as_object)
            .context("full census report missing artifacts")?;
        let summary = artifacts
            .get("summary")
            .and_then(Value::as_str)
            .context("full census summary path missing")?;
        let detail = artifacts
            .get("detail")
            .and_then(Value::as_str)
            .context("full census detail path missing")?;
        let prospective = write_full_census_observation(
            &context.data_root,
            Path::new(summary),
            Path::new(detail),
            Utc::now(),
        )?;
        report
            .as_object_mut()
            .context("cycle report must be object")?
            .insert("prospective".to_owned(), prospective);
    }
    Ok(report)
}

pub fn native_integrity(data_root: &Path) -> Result<Value> {
    let freeze = freeze_path(data_root);
    let binding = runtime_binding_path(data_root);
    let freeze_ok = verify_legacy_v1_freeze_file(&freeze)?;
    let binding_ok = verify_runtime_binding_file(&binding)?;
    let prospective = if freeze_ok && binding_ok {
        prospective_integrity(data_root)?
    } else {
        json!({
            "protocol": crate::v03::PROSPECTIVE_PROTOCOL,
            "ok": false,
            "error": "freeze_or_runtime_binding_invalid"
        })
    };
    let prospective_ok = prospective.get("ok").and_then(Value::as_bool) == Some(true);
    let cycle_enabled = freeze_ok && binding_ok && prospective_ok;
    Ok(json!({
        "protocol": "CROSSALPHA_STATE_V0_3_NATIVE_INTEGRITY",
        "ok": cycle_enabled,
        "legacy_freeze_present": freeze.is_file(),
        "legacy_freeze_ok": freeze_ok,
        "runtime_binding_present": binding.is_file(),
        "runtime_binding_ok": binding_ok,
        "prospective_ok": prospective_ok,
        "prospective": prospective,
        "cycle_enabled": cycle_enabled,
        "python_event_loop_required": false,
    }))
}

fn ensure_tracked_lockfile() -> Result<()> {
    let root = repo_root();
    let lock = root.join("Cargo.lock");
    if !lock.is_file() {
        bail!("native State freeze refused before mutation: Cargo.lock is missing");
    }
    let tracked = Command::new("git")
        .arg("-C")
        .arg(&root)
        .args(["ls-files", "--error-unmatch", "Cargo.lock"])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    if !tracked {
        bail!("native State freeze refused before mutation: Cargo.lock is not tracked by git");
    }
    Ok(())
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
