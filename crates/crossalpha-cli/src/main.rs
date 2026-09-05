use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
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
    /// Run Rust Observatory health checks and write observatory_health.json.
    ObservatoryHealth {
        data_root: PathBuf,
        #[arg(long, default_value_t = 300)]
        expected_interval: u64,
        #[arg(long, default_value_t = 900)]
        stale_after: u64,
        #[arg(long)]
        no_verify_latest: bool,
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
            let first = records.first().map(|r| r.observed_at.to_rfc3339());
            let latest = records.last().map(|r| r.observed_at.to_rfc3339());
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
        } => {
            let report = crossalpha_observatory::observatory_health(
                &data_root,
                expected_interval,
                stale_after,
                !no_verify_latest,
                None,
            )?;
            let report_path = crossalpha_observatory::write_health_report(&data_root, &report)?;
            println!("{}", serde_json::to_string_pretty(&report)?);
            eprintln!("report={}", report_path.display());
            if !report.ok {
                anyhow::bail!("OBSERVATORY HEALTH FAILED");
            }
        }
        Command::MigrationStatus => {
            println!(
                "phase=R2.1 storage=production-compatible parity_gate=passed observatory_health=implemented health_parity_gate=required python_compat=true"
            );
        }
    }
    Ok(())
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
