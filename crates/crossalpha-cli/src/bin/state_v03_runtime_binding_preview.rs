use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use clap::Parser;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-state-v03-runtime-binding-preview-rs",
    about = "Preview the immutable Rust State V0.3 runtime binding without writing it"
)]
struct Args {
    data_root: PathBuf,
    #[arg(long)]
    now: String,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let bound_at = DateTime::parse_from_rfc3339(&args.now)
        .with_context(|| format!("invalid --now RFC3339 timestamp: {}", args.now))?
        .with_timezone(&Utc);
    let payload = crossalpha_state::v03_runtime_binding::runtime_binding_preview(
        &args.data_root,
        bound_at,
    )?;
    println!("{}", serde_json::to_string_pretty(&payload)?);
    Ok(())
}
