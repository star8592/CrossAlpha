use anyhow::Result;
use chrono::Utc;
use clap::{Parser, Subcommand};
use crossalpha_outcomes::runtime::{integrity, materialize, write_runtime_binding};
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-outcome-rs",
    about = "Native Rust prospective State-to-outcome linkage control plane"
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
    Materialize,
    Integrity,
    Status,
}

fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    let args = Args::parse();
    let now = Utc::now();
    let output = match args.command {
        Command::Bind => write_runtime_binding(&args.data_root, now)?,
        Command::Materialize => materialize(&args.data_root, now)?,
        Command::Integrity | Command::Status => integrity(&args.data_root)?,
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}
