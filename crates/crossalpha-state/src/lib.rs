pub mod v02;
pub mod v03;
pub mod v04;
#[path = "v02/artifacts.rs"]
pub mod v02_artifacts;
#[path = "v02/cycle.rs"]
pub mod v02_cycle;
#[path = "v02/engine.rs"]
pub mod v02_engine;
#[path = "v02/freeze.rs"]
pub mod v02_freeze;
#[path = "v02/provider.rs"]
pub mod v02_provider;
#[path = "v02/prospective.rs"]
pub mod v02_prospective;
#[path = "v02/runtime_binding.rs"]
pub mod v02_runtime_binding;
#[path = "v03/artifacts.rs"]
pub mod v03_artifacts;
#[path = "v03/census.rs"]
pub mod v03_census;
#[path = "v03/cycle.rs"]
pub mod v03_cycle;
#[path = "v03/engine.rs"]
pub mod v03_engine;
#[path = "v03/freeze.rs"]
pub mod v03_freeze;
#[path = "v03/network.rs"]
pub mod v03_network;
#[path = "v03/orchestrator.rs"]
pub mod v03_orchestrator;
#[path = "v03/preflight.rs"]
pub mod v03_preflight;
#[path = "v03/prospective.rs"]
pub mod v03_prospective;
#[path = "v03/runtime_binding.rs"]
pub mod v03_runtime_binding;
#[path = "v03/watchlist.rs"]
pub mod v03_watchlist;
#[path = "v04/artifacts.rs"]
pub mod v04_artifacts;
#[path = "v04/cycle.rs"]
pub mod v04_cycle;
#[path = "v04/engine.rs"]
pub mod v04_engine;
#[path = "v04/freeze.rs"]
pub mod v04_freeze;
#[path = "v04/provider.rs"]
pub mod v04_provider;
#[path = "v04/prospective.rs"]
pub mod v04_prospective;
#[path = "v04/runtime_binding.rs"]
pub mod v04_runtime_binding;

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
