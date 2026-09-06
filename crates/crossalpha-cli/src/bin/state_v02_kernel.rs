use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use clap::Parser;
use crossalpha_state::v02::{StateV02Config, StateV02Inputs, compute_state_v02};
use serde::Deserialize;
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-state-v02-kernel-rs",
    about = "Deterministic State V0.2 kernel runner for parity gates"
)]
struct Args {
    #[arg(long)]
    input: PathBuf,
}

#[derive(Debug, Deserialize)]
struct Fixture {
    inputs: StateV02Inputs,
    as_of: DateTime<Utc>,
    generated_at: DateTime<Utc>,
    #[serde(default)]
    config: Option<StateV02Config>,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let fixture: Fixture = serde_json::from_reader(
        File::open(&args.input).with_context(|| format!("open {}", args.input.display()))?,
    )?;
    let output = compute_state_v02(
        &fixture.inputs,
        fixture.as_of,
        fixture.generated_at,
        fixture.config.unwrap_or_default(),
    )?;
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}
