use anyhow::Context;
use anyhow::Result;
use anyhow::bail;
use serde_json::json;
use std::path::PathBuf;
use std::time::Duration;
use std::time::Instant;
use tokio::time::sleep;
use tokio::time::timeout;
use tracing::error;
use tracing::info;
use tracing::warn;

use crate::ProviderClient;
use crate::ProviderSource;
use crate::observatory_live_health;
use crate::write_json_report;

#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    pub data_root: PathBuf,
    pub sources: Vec<ProviderSource>,
    pub interval: Duration,
    pub collector_timeout: Duration,
    pub http_timeout: Duration,
    pub max_consecutive_failures: u32,
    pub stale_after_seconds: u64,
}

impl SupervisorConfig {
    fn validate(&self) -> Result<()> {
        if self.interval.is_zero() {
            bail!("supervisor interval must be greater than zero");
        }
        if self.collector_timeout.is_zero() {
            bail!("collector timeout must be greater than zero");
        }
        if self.http_timeout.is_zero() {
            bail!("HTTP timeout must be greater than zero");
        }
        if self.sources.is_empty() {
            bail!("supervisor requires at least one Observatory source");
        }
        Ok(())
    }
}

pub async fn run_supervisor_until_shutdown(config: SupervisorConfig) -> Result<()> {
    tokio::select! {
        result = run_supervisor(config) => result,
        signal = shutdown_signal() => {
            signal?;
            info!("Observatory supervisor received shutdown signal");
            Ok(())
        }
    }
}

async fn run_supervisor(config: SupervisorConfig) -> Result<()> {
    config.validate()?;
    let client = ProviderClient::new(config.http_timeout)?;
    let failure_limit = config.max_consecutive_failures.max(1);
    let mut consecutive_failures = 0u32;
    let mut consecutive_health_failures = 0u32;

    loop {
        let cycle_started = Instant::now();
        info!(
            sources = ?config.sources,
            data_root = %config.data_root.display(),
            "Observatory collecting"
        );

        let collector_returncode = match timeout(
            config.collector_timeout,
            client.collect_and_store(&config.sources, &config.data_root),
        )
        .await
        {
            Ok(Ok(manifests)) => {
                consecutive_failures = 0;
                info!(
                    written = manifests.len(),
                    "Observatory collection succeeded"
                );
                0u32
            }
            Ok(Err(error)) => {
                consecutive_failures = consecutive_failures.saturating_add(1);
                error!(
                    error = %error,
                    consecutive_failures,
                    "Observatory collector failed"
                );
                1u32
            }
            Err(_) => {
                consecutive_failures = consecutive_failures.saturating_add(1);
                error!(
                    timeout_seconds = config.collector_timeout.as_secs_f64(),
                    consecutive_failures, "Observatory collector timed out"
                );
                124u32
            }
        };

        let stale_after_seconds = config
            .stale_after_seconds
            .max(config.interval.as_secs().saturating_mul(2));
        let mut health =
            observatory_live_health(&config.data_root, stale_after_seconds, true, None)?;
        let health_ok = health
            .get("ok")
            .and_then(|value| value.as_bool())
            .unwrap_or(false);
        if health_ok {
            consecutive_health_failures = 0;
        } else {
            consecutive_health_failures = consecutive_health_failures.saturating_add(1);
            warn!(
                consecutive_health_failures,
                "Observatory live health is unhealthy"
            );
        }

        let object = health
            .as_object_mut()
            .context("Observatory live health report is not an object")?;
        object.insert(
            "collector_returncode".to_owned(),
            json!(collector_returncode),
        );
        object.insert(
            "consecutive_failures".to_owned(),
            json!(consecutive_failures),
        );
        object.insert(
            "consecutive_health_failures".to_owned(),
            json!(consecutive_health_failures),
        );
        let health_path = write_json_report(&config.data_root, "observatory_health.json", &health)?;
        info!(
            health_ok,
            health_mode = health
                .get("mode")
                .and_then(|value| value.as_str())
                .unwrap_or("unknown"),
            health_path = %health_path.display(),
            consecutive_failures,
            consecutive_health_failures,
            "Observatory cycle health"
        );

        if consecutive_failures >= failure_limit || consecutive_health_failures >= failure_limit {
            bail!(
                "collector is repeatedly failing or stale; exiting for systemd restart: collector_failures={} health_failures={} limit={}",
                consecutive_failures,
                consecutive_health_failures,
                failure_limit
            );
        }

        let elapsed = cycle_started.elapsed();
        let delay = config
            .interval
            .saturating_sub(elapsed)
            .max(Duration::from_secs(1));
        sleep(delay).await;
    }
}

async fn shutdown_signal() -> Result<()> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::SignalKind;
        use tokio::signal::unix::signal;

        let mut terminate = signal(SignalKind::terminate()).context("install SIGTERM handler")?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                result.context("listen for Ctrl-C")?;
            }
            _ = terminate.recv() => {}
        }
    }

    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c().await.context("listen for Ctrl-C")?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supervisor_rejects_empty_sources() {
        let config = SupervisorConfig {
            data_root: PathBuf::from("/tmp/crossalpha"),
            sources: Vec::new(),
            interval: Duration::from_secs(300),
            collector_timeout: Duration::from_secs(120),
            http_timeout: Duration::from_secs(30),
            max_consecutive_failures: 3,
            stale_after_seconds: 900,
        };
        assert!(config.validate().is_err());
    }
}
