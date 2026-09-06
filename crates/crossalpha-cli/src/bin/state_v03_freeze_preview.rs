use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use clap::Parser;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-state-v03-freeze-preview-rs",
    about = "Preview the legacy State V0.3 V1 freeze payload without writing it"
)]
struct Args {
    data_root: PathBuf,
    #[arg(long)]
    minimum_eligible_block: u64,
    /// Fixed RFC3339 time required for deterministic parity.
    #[arg(long)]
    now: String,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let frozen_at = DateTime::parse_from_rfc3339(&args.now)
        .with_context(|| format!("invalid --now RFC3339 timestamp: {}", args.now))?
        .with_timezone(&Utc);
    let payload = crossalpha_state::v03_freeze::legacy_v1_freeze_preview(
        &args.data_root,
        args.minimum_eligible_block,
        frozen_at,
    )?;
    println!("{}", serde_json::to_string_pretty(&payload)?);
    Ok(())
}
