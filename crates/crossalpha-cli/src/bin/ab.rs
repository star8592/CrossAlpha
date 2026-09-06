use anyhow::{Result, bail};
use chrono::{NaiveDate, Utc};
use clap::{Parser, Subcommand};
use crossalpha_state::ab_runtime::{create_snapshot, integrity, mark, write_runtime_binding};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-ab-rs",
    about = "Native Rust State A/B prospective control plane"
)]
struct Args {
    #[command(subcommand)]
    command: Command,

    #[arg(long, env = "CROSSALPHA_DATA_DIR", default_value = "./data", global = true)]
    data_root: PathBuf,
}

#[derive(Debug, Subcommand)]
enum Command {
    Bind,
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

fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    let args = Args::parse();
    let require_ok = matches!(args.command, Command::Integrity);
    let now = Utc::now();
    let output = match args.command {
        Command::Bind => write_runtime_binding(&args.data_root, now)?,
        Command::Snapshot { effective_date } => {
            create_snapshot(&args.data_root, parse_date(&effective_date)?, now)?
        }
        Command::Mark { end } => mark(&args.data_root, parse_date(&end)?, now)?,
        Command::Integrity | Command::Status => integrity(&args.data_root)?,
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    if require_ok && output.get("ok").and_then(serde_json::Value::as_bool) != Some(true) {
        bail!("State A/B native integrity failed");
    }
    Ok(())
}

fn parse_date(value: &str) -> Result<NaiveDate> {
    Ok(NaiveDate::parse_from_str(value, "%Y-%m-%d")?)
}
