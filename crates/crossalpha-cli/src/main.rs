use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::time::Duration;
use tracing::info;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-rs",
    version,
    about = "CrossAlpha Rust control plane"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Validate the Rust workspace/control-plane wiring.
    Doctor,
    /// Validate that an existing YAML config can be parsed.
    ConfigCheck { path: PathBuf },
    /// Read and validate the immutable audit manifest without modifying data.
    ManifestCheck { data_root: PathBuf },
    /// Rebuild daily and series indexes from the immutable audit manifest.
    ManifestRebuild { data_root: PathBuf },
    /// Compare Python and Rust rebuilds from the same immutable audit ledger.
    ManifestParity { data_root: PathBuf },
    /// Run the full Rust Observatory health scan.
    ObservatoryHealth {
        data_root: PathBuf,
        #[arg(long, default_value_t = 300)]
        expected_interval: u64,
        #[arg(long, default_value_t = 900)]
        stale_after: u64,
        #[arg(long)]
        no_verify_latest: bool,
        /// Fixed RFC3339 timestamp for deterministic Python/Rust parity tests.
        #[arg(long)]
        now: Option<String>,
        /// Do not update manifests/observatory_health.json.
        #[arg(long)]
        no_write_report: bool,
    },
    /// Run the O(1)-per-series Rust live health watchdog path.
    ObservatoryLiveHealth {
        data_root: PathBuf,
        #[arg(long, default_value_t = 900)]
        stale_after: u64,
        #[arg(long)]
        no_verify_latest: bool,
        /// Fixed RFC3339 timestamp for deterministic Python/Rust parity tests.
        #[arg(long)]
        now: Option<String>,
        /// Do not update manifests/observatory_health.json.
        #[arg(long)]
        no_write_report: bool,
    },
    /// Collect one Rust Observatory round from Hyperliquid/DefiLlama.
    ObservatoryCollect {
        data_root: PathBuf,
        /// Source to collect. Repeat for multiple sources. Defaults to both.
        #[arg(long = "source")]
        sources: Vec<String>,
        /// HTTP timeout in seconds.
        #[arg(long, default_value_t = 30.0)]
        timeout: f64,
        /// Fetch and validate envelopes but do not write raw snapshots or manifests.
        #[arg(long)]
        dry_run: bool,
    },
    /// Run the native Tokio Observatory collector/watchdog loop.
    ObservatoryRun {
        data_root: PathBuf,
        /// Source to collect. Repeat for multiple sources. Defaults to both.
        #[arg(long = "source")]
        sources: Vec<String>,
        #[arg(long, default_value_t = 300)]
        interval: u64,
        #[arg(long, default_value_t = 120)]
        collector_timeout: u64,
        #[arg(long, default_value_t = 30.0)]
        http_timeout: f64,
        #[arg(long, default_value_t = 3)]
        max_consecutive_failures: u32,
        #[arg(long, default_value_t = 900)]
        stale_after: u64,
    },
    /// Show migration status for the Rust rewrite.
    MigrationStatus,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let cli = Cli::parse();
    match cli.command {
        Command::Doctor => {
            info!("Rust control plane is alive");
            println!("ok=true runtime=rust");
        }
        Command::ConfigCheck { path } => {
            let value: serde_json::Value = crossalpha_config::load_yaml(&path)?;
            println!("ok=true path={} type={}", path.display(), json_type(&value));
        }
        Command::ManifestCheck { data_root } => {
            let records = crossalpha_storage::read_audit_manifest(&data_root)?;
            let first = records
                .first()
                .map(|record| record.observed_at.to_rfc3339());
            let latest = records.last().map(|record| record.observed_at.to_rfc3339());
            println!(
                "ok=true records={} first={} latest={} data_root={}",
                records.len(),
                first.as_deref().unwrap_or("none"),
                latest.as_deref().unwrap_or("none"),
                data_root.display()
            );
        }
        Command::ManifestRebuild { data_root } => {
            let records = crossalpha_storage::rebuild_manifest_indexes(&data_root)?;
            println!(
                "ok=true rebuilt_records={} data_root={}",
                records,
                data_root.display()
            );
        }
        Command::ManifestParity { data_root } => {
            let report = crossalpha_storage::verify_manifest_parity(&data_root)?;
            println!(
                "ok={} records={} daily_files={} series_files={} mismatches={} data_root={}",
                report.ok,
                report.records,
                report.daily_files,
                report.series_files,
                report.mismatches.len(),
                data_root.display()
            );
            for mismatch in &report.mismatches {
                println!("mismatch={mismatch}");
            }
            if !report.ok {
                anyhow::bail!(
                    "manifest parity failed with {} mismatch(es)",
                    report.mismatches.len()
                );
            }
        }
        Command::ObservatoryHealth {
            data_root,
            expected_interval,
            stale_after,
            no_verify_latest,
            now,
            no_write_report,
        } => {
            let now = parse_optional_rfc3339(now.as_deref())?;
            let report = crossalpha_observatory::observatory_health(
                &data_root,
                expected_interval,
                stale_after,
                !no_verify_latest,
                now,
            )?;
            if !no_write_report {
                let report_path = crossalpha_observatory::write_health_report(&data_root, &report)?;
                eprintln!("report={}", report_path.display());
            }
            println!("{}", serde_json::to_string_pretty(&report)?);
            if !report.ok {
                anyhow::bail!("OBSERVATORY HEALTH FAILED");
            }
        }
        Command::ObservatoryLiveHealth {
            data_root,
            stale_after,
            no_verify_latest,
            now,
            no_write_report,
        } => {
            let now = parse_optional_rfc3339(now.as_deref())?;
            let report = crossalpha_observatory::observatory_live_health(
                &data_root,
                stale_after,
                !no_verify_latest,
                now,
            )?;
            if !no_write_report {
                let report_path = crossalpha_observatory::write_json_report(
                    &data_root,
                    "observatory_health.json",
                    &report,
                )?;
                eprintln!("report={}", report_path.display());
            }
            println!("{}", serde_json::to_string_pretty(&report)?);
            if !report
                .get("ok")
                .and_then(|value| value.as_bool())
                .unwrap_or(false)
            {
                anyhow::bail!("OBSERVATORY LIVE HEALTH FAILED");
            }
        }
        Command::ObservatoryCollect {
            data_root,
            sources,
            timeout,
            dry_run,
        } => {
            let timeout = positive_duration(timeout, "--timeout")?;
            let sources = crossalpha_observatory::parse_sources(&sources)?;
            let client = crossalpha_observatory::ProviderClient::new(timeout)?;

            if dry_run {
                let envelopes = client.collect_many(&sources).await?;
                println!("{}", serde_json::to_string_pretty(&envelopes)?);
            } else {
                let manifests = client.collect_and_store(&sources, &data_root).await?;
                for manifest in &manifests {
                    println!("{}", serde_json::to_string(manifest)?);
                }
            }
        }
        Command::ObservatoryRun {
            data_root,
            sources,
            interval,
            collector_timeout,
            http_timeout,
            max_consecutive_failures,
            stale_after,
        } => {
            let sources = crossalpha_observatory::parse_sources(&sources)?;
            let config = crossalpha_observatory::SupervisorConfig {
                data_root,
                sources,
                interval: Duration::from_secs(interval),
                collector_timeout: Duration::from_secs(collector_timeout.max(1)),
                http_timeout: positive_duration(http_timeout, "--http-timeout")?,
                max_consecutive_failures: max_consecutive_failures.max(1),
                stale_after_seconds: stale_after,
            };
            crossalpha_observatory::run_supervisor_until_shutdown(config).await?;
        }
        Command::MigrationStatus => {
            println!(
                "phase=R2.5 storage=production-compatible parity_gate=passed observatory_health=production-compatible health_parity_gate=passed rust_providers=implemented collector_dry_run_gate=passed live_health=production-compatible live_health_parity_gate=passed supervisor=implemented shadow_write_gate=required systemd_cutover=false python_compat=true"
            );
        }
    }
    Ok(())
}

fn positive_duration(seconds: f64, flag: &str) -> Result<Duration> {
    if !seconds.is_finite() || seconds <= 0.0 {
        anyhow::bail!("{flag} must be a finite positive number");
    }
    Ok(Duration::from_secs_f64(seconds))
}

fn parse_optional_rfc3339(raw: Option<&str>) -> Result<Option<DateTime<Utc>>> {
    raw.map(|value| {
        DateTime::parse_from_rfc3339(value)
            .with_context(|| format!("invalid --now RFC3339 timestamp: {value}"))
            .map(|timestamp| timestamp.with_timezone(&Utc))
    })
    .transpose()
}

fn json_type(value: &serde_json::Value) -> &'static str {
    match value {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "array",
        serde_json::Value::Object(_) => "object",
    }
}
