use anyhow::{Context, Result};
use clap::Parser;
use crossalpha_storage::{ObservationEnvelope, raw_envelope_bytes};
use serde_json::json;
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-raw-envelope-fixture-rs",
    about = "Emit canonical raw ObservationEnvelope bytes for deterministic parity"
)]
struct Args {
    input: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let envelope: ObservationEnvelope = serde_json::from_reader(File::open(&args.input)?)?;
    let bytes = raw_envelope_bytes(&envelope)?;
    let text = String::from_utf8(bytes).context("raw envelope bytes must be UTF-8 JSON")?;
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "schema_version": envelope.schema_version,
            "bytes": text.len(),
            "canonical_utf8": text,
        }))?
    );
    Ok(())
}
