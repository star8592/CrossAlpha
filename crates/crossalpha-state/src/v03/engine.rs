use crate::v03::{PROTOCOL, StateV03};
use crate::v03_preflight;
use crate::{StateConfigReport, StateRuntimeContext, StateSpec};
use anyhow::{Result, bail};
use async_trait::async_trait;
use serde_json::{Value, json};
use std::path::Path;

#[derive(Debug, Clone, Copy, Default)]
pub struct NativeStateV03;

#[async_trait]
impl StateSpec for NativeStateV03 {
    fn version(&self) -> &'static str {
        "v03"
    }

    fn protocol(&self) -> &'static str {
        PROTOCOL
    }

    fn validate_config(&self, path: &Path) -> Result<StateConfigReport> {
        StateV03::strict_config_report(path)
    }

    async fn preflight(&self, context: &StateRuntimeContext) -> Result<Value> {
        let report = v03_preflight::run_preflight(
            context.http_timeout,
            context.evm_rpc_url.as_deref(),
        )
        .await?;
        Ok(serde_json::to_value(report)?)
    }

    async fn freeze(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("Native State V0.3 freeze remains locked until freeze parity passes")
    }

    async fn cycle(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("Native State V0.3 cycle remains locked until runtime binding is committed")
    }

    fn integrity(&self, data_root: &Path) -> Result<Value> {
        let legacy = data_root.join("research/state_v03/freeze.json");
        let binding = data_root.join("research/state_v03/rust_runtime_binding.json");
        Ok(json!({
            "protocol": PROTOCOL,
            "runtime": "RUST_TOKIO",
            "legacy_freeze_present": legacy.is_file(),
            "runtime_binding_present": binding.is_file(),
            "cycle_enabled": false,
        }))
    }

    fn status(&self, data_root: &Path) -> Result<Value> {
        let integrity = self.integrity(data_root)?;
        Ok(json!({
            "protocol": PROTOCOL,
            "version": self.version(),
            "phase": "R4_PREFLIGHT_FREEZE_PARITY",
            "runtime": "RUST_TOKIO",
            "integrity": integrity,
        }))
    }
}
