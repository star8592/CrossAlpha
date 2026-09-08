use crate::v03::{PROTOCOL, StateV03};
use crate::v03_orchestrator::{freeze_native, native_integrity, run_cycle_native};
use crate::v03_preflight;
use crate::{StateConfigReport, StateRuntimeContext, StateSpec};
use anyhow::Result;
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
        let report =
            v03_preflight::run_preflight(context.http_timeout, context.evm_rpc_url.as_deref())
                .await?;
        Ok(serde_json::to_value(report)?)
    }

    async fn freeze(&self, context: &StateRuntimeContext) -> Result<Value> {
        freeze_native(context).await
    }

    async fn cycle(&self, context: &StateRuntimeContext) -> Result<Value> {
        run_cycle_native(context).await
    }

    fn integrity(&self, data_root: &Path) -> Result<Value> {
        native_integrity(data_root)
    }

    fn status(&self, data_root: &Path) -> Result<Value> {
        let integrity = self.integrity(data_root)?;
        let enabled = integrity
            .get("cycle_enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        Ok(json!({
            "protocol": PROTOCOL,
            "version": self.version(),
            "phase": if enabled { "R4_NATIVE_ACTIVE" } else { "R4_NATIVE_GATED" },
            "runtime": "RUST_TOKIO",
            "python_event_loop_required": false,
            "integrity": integrity,
        }))
    }
}
