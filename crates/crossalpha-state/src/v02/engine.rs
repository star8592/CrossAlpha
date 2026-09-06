use crate::v02::{PROTOCOL, strict_config_report};
use crate::{StateConfigReport, StateRuntimeContext, StateSpec};
use anyhow::{Result, bail};
use async_trait::async_trait;
use chrono::Utc;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Copy, Default)]
pub struct NativeStateV02;

#[async_trait]
impl StateSpec for NativeStateV02 {
    fn version(&self) -> &'static str {
        "v02"
    }

    fn protocol(&self) -> &'static str {
        PROTOCOL
    }

    fn validate_config(&self, path: &Path) -> Result<StateConfigReport> {
        strict_config_report(path)
    }

    async fn preflight(&self, context: &StateRuntimeContext) -> Result<Value> {
        crate::v02_cycle::preflight(context).await
    }

    async fn freeze(&self, context: &StateRuntimeContext) -> Result<Value> {
        ensure_tracked_lockfile()?;
        let config = strict_config_report(&repo_root().join("config/state_v02.yaml"))?;
        if !config.ok {
            bail!("STATE V0.2 FREEZE REFUSED: strict config consistency failed");
        }
        let preflight = crate::v02_cycle::preflight(context).await?;
        let state = preflight
            .get("state_v02")
            .and_then(|value| value.get("data_confidence"))
            .and_then(Value::as_str);
        if state == Some("INSUFFICIENT") {
            bail!("STATE V0.2 FREEZE REFUSED: live descriptive preflight insufficient");
        }
        let now = Utc::now();
        let freeze = crate::v02_freeze::write_freeze(&context.data_root, now)?;
        let binding = crate::v02_runtime_binding::write_runtime_binding(&context.data_root, now)?;
        Ok(json!({
            "protocol":"CROSSALPHA_STATE_V0_2_NATIVE_FREEZE",
            "status":"frozen_and_rust_bound",
            "preflight":preflight,
            "freeze":freeze,
            "runtime_binding":binding,
        }))
    }

    async fn cycle(&self, context: &StateRuntimeContext) -> Result<Value> {
        crate::v02_cycle::run_cycle(context).await
    }

    fn integrity(&self, data_root: &Path) -> Result<Value> {
        let prospective = crate::v02_prospective::integrity_report(data_root)?;
        let freeze_ok = crate::v02_freeze::verify_freeze_file(&crate::v02_freeze::freeze_path(data_root))?;
        let binding_ok = crate::v02_runtime_binding::verify_runtime_binding_file(
            &crate::v02_runtime_binding::runtime_binding_path(data_root),
        )?;
        let cycle_enabled = freeze_ok && binding_ok;
        Ok(json!({
            "protocol":"CROSSALPHA_STATE_V0_2_NATIVE_INTEGRITY",
            "ok":cycle_enabled && prospective.get("ok").and_then(Value::as_bool).unwrap_or(true),
            "legacy_freeze_present":crate::v02_freeze::freeze_path(data_root).is_file(),
            "legacy_freeze_ok":freeze_ok,
            "runtime_binding_present":crate::v02_runtime_binding::runtime_binding_path(data_root).is_file(),
            "runtime_binding_ok":binding_ok,
            "cycle_enabled":cycle_enabled,
            "python_event_loop_required":false,
            "prospective":prospective,
        }))
    }

    fn status(&self, data_root: &Path) -> Result<Value> {
        let integrity = self.integrity(data_root)?;
        Ok(json!({
            "protocol":PROTOCOL,
            "version":"v02",
            "runtime":"RUST_TOKIO",
            "phase":if integrity.get("cycle_enabled").and_then(Value::as_bool)==Some(true) {"R4_NATIVE_ACTIVE"} else {"R4_NATIVE_GATED"},
            "integrity":integrity,
        }))
    }
}

fn ensure_tracked_lockfile() -> Result<()> {
    let root = repo_root();
    let lock = root.join("Cargo.lock");
    if !lock.is_file() {
        bail!("native State V0.2 freeze refused before mutation: Cargo.lock is missing");
    }
    let tracked = Command::new("git")
        .arg("-C")
        .arg(&root)
        .args(["ls-files", "--error-unmatch", "Cargo.lock"])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    if !tracked {
        bail!("native State V0.2 freeze refused before mutation: Cargo.lock is not tracked by git");
    }
    Ok(())
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
