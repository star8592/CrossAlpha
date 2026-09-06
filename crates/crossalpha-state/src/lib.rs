pub mod v03;
#[path = "v03/artifacts.rs"]
pub mod v03_artifacts;
#[path = "v03/census.rs"]
pub mod v03_census;
#[path = "v03/engine.rs"]
pub mod v03_engine;
#[path = "v03/freeze.rs"]
pub mod v03_freeze;
#[path = "v03/network.rs"]
pub mod v03_network;
#[path = "v03/preflight.rs"]
pub mod v03_preflight;
#[path = "v03/runtime_binding.rs"]
pub mod v03_runtime_binding;
#[path = "v03/watchlist.rs"]
pub mod v03_watchlist;

use anyhow::Result;
use async_trait::async_trait;
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::Duration;

#[derive(Debug, Clone)]
pub struct StateRuntimeContext {
    pub data_root: PathBuf,
    pub http_timeout: Duration,
    pub evm_rpc_url: Option<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct StateConfigReport {
    pub protocol: String,
    pub audit_level: String,
    pub ok: bool,
    pub checks: std::collections::BTreeMap<String, bool>,
}

#[async_trait]
pub trait StateSpec: Send + Sync {
    fn version(&self) -> &'static str;
    fn protocol(&self) -> &'static str;
    fn validate_config(&self, path: &Path) -> Result<StateConfigReport>;
    async fn preflight(&self, context: &StateRuntimeContext) -> Result<Value>;
    async fn freeze(&self, context: &StateRuntimeContext) -> Result<Value>;
    async fn cycle(&self, context: &StateRuntimeContext) -> Result<Value>;
    fn integrity(&self, data_root: &Path) -> Result<Value>;
    fn status(&self, data_root: &Path) -> Result<Value>;
}
