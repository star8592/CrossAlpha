use anyhow::{Context, Result, bail};
use clap::{Parser, ValueEnum};
use crossalpha_features::{materialize_recent_canonical, materialize_recent_features};
use crossalpha_observatory::{
    ProviderSource, SupervisorConfig, run_supervisor, write_json_report,
};
use crossalpha_state::v03_engine::NativeStateV03;
use crossalpha_state::v04_engine::NativeStateV04Engine;
use crossalpha_state::{StateRuntimeContext, StateSpec};
use fs2::FileExt;
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::task::JoinSet;
use tokio::time::sleep;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, ValueEnum)]
enum Component {
    Observatory,
    Materializer,
    StateV03,
    StateV04,
}

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-daemon-rs",
    about = "Unified native CrossAlpha runtime with one Tokio owner"
)]
struct Args {
    #[arg(long, env = "CROSSALPHA_DATA_DIR", default_value = "./data")]
    data_root: PathBuf,

    #[arg(long = "component", value_enum, required = true)]
    components: Vec<Component>,

    #[arg(long, default_value_t = 300)]
    interval_seconds: u64,

    #[arg(long, default_value_t = 30)]
    http_timeout_seconds: u64,

    #[arg(long, default_value_t = 120)]
    collector_timeout_seconds: u64,

    #[arg(long, default_value_t = 900)]
    stale_after_seconds: u64,

    #[arg(long, default_value_t = 3)]
    max_consecutive_failures: u32,

    #[arg(long, default_value_t = 2)]
    materializer_recent_days: usize,

    #[arg(long, default_value_t = false)]
    allow_production_materialization: bool,

    #[arg(long, env = "EVM_RPC_URL")]
    evm_rpc_url: Option<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let args = Args::parse();
    validate(&args)?;
    let components: BTreeSet<Component> = args.components.iter().copied().collect();
    let _daemon_lock = acquire_daemon_lock(&args.data_root)?;

    validate_state_bindings(&args.data_root, &components)?;
    write_daemon_status(
        &args.data_root,
        "running",
        &components,
        None,
    )?;

    let mut tasks: JoinSet<Result<()>> = JoinSet::new();
    let interval = Duration::from_secs(args.interval_seconds);
    let http_timeout = Duration::from_secs(args.http_timeout_seconds);
    let failure_limit = args.max_consecutive_failures.max(1);

    if components.contains(&Component::Observatory) {
        let config = SupervisorConfig {
            data_root: args.data_root.clone(),
            sources: vec![ProviderSource::Hyperliquid, ProviderSource::DefiLlama],
            interval,
            collector_timeout: Duration::from_secs(args.collector_timeout_seconds),
            http_timeout,
            max_consecutive_failures: failure_limit,
            stale_after_seconds: args.stale_after_seconds,
        };
        tasks.spawn(async move { run_supervisor(config).await.context("Observatory supervisor") });
    }

    if components.contains(&Component::Materializer) {
        let data_root = args.data_root.clone();
        let recent_days = args.materializer_recent_days;
        tasks.spawn(async move {
            materializer_loop(data_root, recent_days, interval, failure_limit).await
        });
    }

    if components.contains(&Component::StateV03) {
        let context = StateRuntimeContext {
            data_root: args.data_root.clone(),
            http_timeout,
            evm_rpc_url: args.evm_rpc_url.clone(),
        };
        tasks.spawn(async move {
            state_loop(Component::StateV03, context, interval, failure_limit).await
        });
    }

    if components.contains(&Component::StateV04) {
        let context = StateRuntimeContext {
            data_root: args.data_root.clone(),
            http_timeout,
            evm_rpc_url: args.evm_rpc_url.clone(),
        };
        tasks.spawn(async move {
            state_loop(Component::StateV04, context, interval, failure_limit).await
        });
    }

    tokio::select! {
        signal = shutdown_signal() => {
            signal?;
            info!("CrossAlpha daemon received shutdown signal");
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
            write_daemon_status(&args.data_root, "stopped", &components, None)?;
            Ok(())
        }
        joined = tasks.join_next() => {
            let error = match joined {
                Some(Ok(Ok(()))) => anyhow::anyhow!("daemon component exited unexpectedly"),
                Some(Ok(Err(error))) => error,
                Some(Err(error)) => anyhow::anyhow!("daemon component task failed: {error}"),
                None => anyhow::anyhow!("daemon has no running components"),
            };
            error!(error = %error, "CrossAlpha daemon component failed");
            tasks.abort_all();
            while tasks.join_next().await.is_some() {}
            write_daemon_status(
                &args.data_root,
                "failed",
                &components,
                Some(format!("{error:#}")),
            )?;
            Err(error)
        }
    }
}

fn validate(args: &Args) -> Result<()> {
    if args.interval_seconds == 0 {
        bail!("interval-seconds must be positive");
    }
    if args.http_timeout_seconds == 0 {
        bail!("http-timeout-seconds must be positive");
    }
    if args.collector_timeout_seconds == 0 {
        bail!("collector-timeout-seconds must be positive");
    }
    if args.materializer_recent_days == 0 {
        bail!("materializer-recent-days must be positive");
    }
    let components: BTreeSet<Component> = args.components.iter().copied().collect();
    if components.contains(&Component::Materializer) && !args.allow_production_materialization {
        bail!(
            "native production materializer is gated; pass --allow-production-materialization only after R3 materializer parity passes"
        );
    }
    Ok(())
}

fn validate_state_bindings(data_root: &Path, components: &BTreeSet<Component>) -> Result<()> {
    if components.contains(&Component::StateV03) {
        let integrity = NativeStateV03.integrity(data_root)?;
        if integrity.get("cycle_enabled").and_then(Value::as_bool) != Some(true) {
            bail!("State V0.3 daemon start refused: native freeze/runtime binding gate is not valid");
        }
    }
    if components.contains(&Component::StateV04) {
        let integrity = NativeStateV04Engine.integrity(data_root)?;
        if integrity.get("cycle_enabled").and_then(Value::as_bool) != Some(true) {
            bail!("State V0.4 daemon start refused: native freeze/runtime binding gate is not valid");
        }
    }
    Ok(())
}

async fn materializer_loop(
    data_root: PathBuf,
    recent_days: usize,
    interval: Duration,
    failure_limit: u32,
) -> Result<()> {
    let mut consecutive_failures = 0_u32;
    loop {
        let started = Instant::now();
        let root = data_root.clone();
        let result = tokio::task::spawn_blocking(move || -> Result<Value> {
            let canonical = materialize_recent_canonical(&root, &root, recent_days)?;
            let features = materialize_recent_features(&root, &root, recent_days)?;
            Ok(json!({
                "canonical": canonical,
                "features": features,
            }))
        })
        .await
        .context("join native materializer worker")?;

        match result {
            Ok(report) => {
                consecutive_failures = 0;
                write_json_report(
                    &data_root,
                    "materializer_health.json",
                    &json!({
                        "protocol": "CROSSALPHA_NATIVE_MATERIALIZER_HEALTH_V1",
                        "ok": true,
                        "checked_at": chrono::Utc::now().to_rfc3339(),
                        "bounded": true,
                        "recent_days": recent_days,
                        "consecutive_failures": consecutive_failures,
                        "report": report,
                    }),
                )?;
                info!(recent_days, "native materializer cycle succeeded");
            }
            Err(error) => {
                consecutive_failures = consecutive_failures.saturating_add(1);
                warn!(error = %error, consecutive_failures, "native materializer cycle failed");
                write_json_report(
                    &data_root,
                    "materializer_health.json",
                    &json!({
                        "protocol": "CROSSALPHA_NATIVE_MATERIALIZER_HEALTH_V1",
                        "ok": false,
                        "checked_at": chrono::Utc::now().to_rfc3339(),
                        "bounded": true,
                        "recent_days": recent_days,
                        "consecutive_failures": consecutive_failures,
                        "error": format!("{error:#}"),
                    }),
                )?;
                if consecutive_failures >= failure_limit {
                    return Err(error.context("native materializer failure limit reached"));
                }
            }
        }
        sleep_remaining(started, interval).await;
    }
}

async fn state_loop(
    component: Component,
    context: StateRuntimeContext,
    interval: Duration,
    failure_limit: u32,
) -> Result<()> {
    let mut consecutive_failures = 0_u32;
    loop {
        let started = Instant::now();
        let result = match component {
            Component::StateV03 => NativeStateV03.cycle(&context).await,
            Component::StateV04 => NativeStateV04Engine.cycle(&context).await,
            _ => bail!("state loop called for non-state component"),
        };
        let (filename, protocol) = match component {
            Component::StateV03 => ("state_v03_daemon_health.json", "CROSSALPHA_STATE_V0_3_DAEMON_HEALTH"),
            Component::StateV04 => ("state_v04_daemon_health.json", "CROSSALPHA_STATE_V0_4_DAEMON_HEALTH"),
            _ => unreachable!(),
        };
        match result {
            Ok(report) => {
                consecutive_failures = 0;
                write_json_report(
                    &context.data_root,
                    filename,
                    &json!({
                        "protocol": protocol,
                        "ok": true,
                        "checked_at": chrono::Utc::now().to_rfc3339(),
                        "consecutive_failures": consecutive_failures,
                        "report": report,
                    }),
                )?;
                info!(component = ?component, "native State cycle succeeded");
            }
            Err(error) => {
                consecutive_failures = consecutive_failures.saturating_add(1);
                warn!(component = ?component, error = %error, consecutive_failures, "native State cycle failed");
                write_json_report(
                    &context.data_root,
                    filename,
                    &json!({
                        "protocol": protocol,
                        "ok": false,
                        "checked_at": chrono::Utc::now().to_rfc3339(),
                        "consecutive_failures": consecutive_failures,
                        "error": format!("{error:#}"),
                    }),
                )?;
                if consecutive_failures >= failure_limit {
                    return Err(error.context("native State failure limit reached"));
                }
            }
        }
        sleep_remaining(started, interval).await;
    }
}

async fn sleep_remaining(started: Instant, interval: Duration) {
    let elapsed = started.elapsed();
    sleep(interval.saturating_sub(elapsed).max(Duration::from_secs(1))).await;
}

fn acquire_daemon_lock(data_root: &Path) -> Result<File> {
    let manifests = data_root.join("manifests");
    fs::create_dir_all(&manifests)?;
    let path = manifests.join(".crossalpha-daemon.lock");
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .open(&path)
        .with_context(|| format!("open daemon lock {}", path.display()))?;
    file.try_lock_exclusive()
        .with_context(|| format!("another CrossAlpha daemon already owns {}", path.display()))?;
    Ok(file)
}

fn write_daemon_status(
    data_root: &Path,
    status: &str,
    components: &BTreeSet<Component>,
    error: Option<String>,
) -> Result<()> {
    write_json_report(
        data_root,
        "crossalpha_daemon_health.json",
        &json!({
            "protocol": "CROSSALPHA_NATIVE_DAEMON_HEALTH_V1",
            "status": status,
            "ok": status == "running" || status == "stopped",
            "checked_at": chrono::Utc::now().to_rfc3339(),
            "runtime": "RUST_TOKIO",
            "single_instance_lock": true,
            "components": components.iter().map(|component| format!("{component:?}")).collect::<Vec<_>>(),
            "error": error,
        }),
    )?;
    Ok(())
}

async fn shutdown_signal() -> Result<()> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
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
    fn duplicate_components_collapse_for_runtime() {
        let components = BTreeSet::from([Component::Observatory, Component::Observatory]);
        assert_eq!(components.len(), 1);
    }
}
