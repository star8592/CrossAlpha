use crate::v04::{NativeStateV04, PROTOCOL};
use crate::v04_cycle;
use crate::v04_freeze::{freeze_path, verify_freeze_file, write_freeze};
use crate::v04_prospective::prospective_integrity;
use crate::v04_runtime_binding::{
    runtime_binding_path, verify_runtime_binding_file, write_runtime_binding,
};
use crate::{StateConfigReport, StateRuntimeContext, StateSpec};
use anyhow::{Result, bail};
use async_trait::async_trait;
use chrono::Utc;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Copy, Default)]
pub struct NativeStateV04Engine;

#[async_trait]
impl StateSpec for NativeStateV04Engine {
    fn version(&self) -> &'static str {
        "v04"
    }

    fn protocol(&self) -> &'static str {
        PROTOCOL
    }

    fn validate_config(&self, path: &Path) -> Result<StateConfigReport> {
        NativeStateV04::strict_config_report(path)
    }

    async fn preflight(&self, context: &StateRuntimeContext) -> Result<Value> {
        v04_cycle::preflight(context).await
    }

    async fn freeze(&self, context: &StateRuntimeContext) -> Result<Value> {
        ensure_tracked_lockfile()?;
        let config =
            NativeStateV04::strict_config_report(&repo_root().join("config/state_v04.yaml"))?;
        if !config.ok {
            bail!("STATE V0.4 FREEZE REFUSED: strict config consistency failed");
        }
        let preflight = v04_cycle::preflight(context).await?;
        if preflight
            .get("state")
            .and_then(|state| state.get("data_confidence"))
            .and_then(Value::as_str)
            == Some("INSUFFICIENT")
        {
            bail!("STATE V0.4 FREEZE REFUSED: live multi-venue preflight insufficient");
        }
        let freeze = write_freeze(&context.data_root, Utc::now())?;
        let binding = write_runtime_binding(&context.data_root, Utc::now())?;
        Ok(json!({
            "protocol": "CROSSALPHA_STATE_V0_4_NATIVE_FREEZE",
            "status": "frozen_and_rust_bound",
            "preflight": preflight,
            "freeze": freeze,
            "runtime_binding": binding,
        }))
    }

    async fn cycle(&self, context: &StateRuntimeContext) -> Result<Value> {
        v04_cycle::run_cycle(context).await
    }

    fn integrity(&self, data_root: &Path) -> Result<Value> {
        let freeze = freeze_path(data_root);
        let binding = runtime_binding_path(data_root);
        let freeze_ok = verify_freeze_file(&freeze)?;
        let binding_ok = verify_runtime_binding_file(&binding)?;
        let prospective = if freeze_ok && binding_ok {
            prospective_integrity(data_root)?
        } else {
            json!({
                "protocol": crate::v04_freeze::PROSPECTIVE_PROTOCOL,
                "ok": false,
                "error": "freeze_or_runtime_binding_invalid"
            })
        };
        let prospective_ok = prospective.get("ok").and_then(Value::as_bool) == Some(true);
        let cycle_enabled = freeze_ok && binding_ok && prospective_ok;
        Ok(json!({
            "protocol": "CROSSALPHA_STATE_V0_4_NATIVE_INTEGRITY",
            "ok": cycle_enabled,
            "legacy_freeze_present": freeze.is_file(),
            "legacy_freeze_ok": freeze_ok,
            "runtime_binding_present": binding.is_file(),
            "runtime_binding_ok": binding_ok,
            "prospective_ok": prospective_ok,
            "prospective": prospective,
            "cycle_enabled": cycle_enabled,
            "python_event_loop_required": false,
            "no_composite_stress_score": true,
        }))
    }

    fn status(&self, data_root: &Path) -> Result<Value> {
        let integrity = self.integrity(data_root)?;
        Ok(json!({
            "protocol": PROTOCOL,
            "version": self.version(),
            "runtime": "RUST_TOKIO",
            "phase": if integrity.get("cycle_enabled").and_then(Value::as_bool) == Some(true) {
                "R4_NATIVE_ACTIVE"
            } else {
                "R4_NATIVE_GATED"
            },
            "integrity": integrity,
        }))
    }
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
