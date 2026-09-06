use anyhow::{Context, Result, bail};
use chrono::{NaiveDate, Utc};
use clap::{Parser, Subcommand};
use crossalpha_data::{FreeCoreProvider, FreeCoreRange, build_free_core_returns};
use crossalpha_research::paper_runtime::{
    HISTORICAL_START, create_snapshot, integrity, mark_forward, returns_path, sha256_file,
    write_runtime_binding,
};
use serde_json::json;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-paper-rs",
    about = "Native Rust Frozen B3 paper control plane"
)]
struct Args {
    #[command(subcommand)]
    command: Command,

    #[arg(long, env = "CROSSALPHA_DATA_DIR", default_value = "./data", global = true)]
    data_root: PathBuf,

    #[arg(long, default_value_t = 30.0, global = true)]
    http_timeout: f64,
}

#[derive(Debug, Subcommand)]
enum Command {
    Bind,
    Refresh {
        #[arg(long, default_value = HISTORICAL_START)]
        start: String,
        #[arg(long)]
        end: String,
    },
    Snapshot {
        #[arg(long)]
        effective_date: String,
    },
    Mark {
        #[arg(long)]
        end: String,
    },
    Integrity,
    Status,
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    let args = Args::parse();
    if !args.http_timeout.is_finite() || args.http_timeout <= 0.0 {
        bail!("--http-timeout must be a finite positive number");
    }
    let require_ok = matches!(args.command, Command::Integrity);
    let now = Utc::now();
    let output = match args.command {
        Command::Bind => write_runtime_binding(&args.data_root, now)?,
        Command::Refresh { start, end } => {
            let start = parse_date(&start)?;
            let end = parse_date(&end)?;
            let range = FreeCoreRange::new(start, end)?;
            let target = returns_path(&args.data_root, start, end);
            if target.exists() {
                json!({
                    "paper_protocol": "CROSSALPHA_FREE_V0_1_PAPER",
                    "status": "cached",
                    "data_cost_usd": 0,
                    "start": start,
                    "end_exclusive": end,
                    "returns_path": target,
                    "returns_sha256": sha256_file(&target)?,
                })
            } else {
                let tiingo = std::env::var("TIINGO_API_TOKEN")
                    .context("TIINGO_API_TOKEN is required for native paper refresh")?;
                let fred = std::env::var("FRED_API_KEY")
                    .context("FRED_API_KEY is required for native paper refresh")?;
                let provider = FreeCoreProvider::new(
                    &tiingo,
                    &fred,
                    Duration::from_secs_f64(args.http_timeout),
                )?;
                let fetched = provider.fetch_all(&range, &args.data_root).await?;
                let derived = build_free_core_returns(&args.data_root, &range)?;
                json!({
                    "paper_protocol": "CROSSALPHA_FREE_V0_1_PAPER",
                    "status": "fetched",
                    "data_cost_usd": 0,
                    "start": start,
                    "end_exclusive": end,
                    "fetched": fetched,
                    "derived": derived,
                })
            }
        }
        Command::Snapshot { effective_date } => {
            create_snapshot(&args.data_root, parse_date(&effective_date)?, now)?
        }
        Command::Mark { end } => mark_forward(&args.data_root, parse_date(&end)?, now)?,
        Command::Integrity | Command::Status => integrity(&args.data_root)?,
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    if require_ok && output.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
        bail!("Frozen B3 native paper integrity failed");
    }
    Ok(())
}

fn parse_date(value: &str) -> Result<NaiveDate> {
    Ok(NaiveDate::parse_from_str(value, "%Y-%m-%d")?)
}
