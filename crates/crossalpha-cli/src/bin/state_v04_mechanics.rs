use anyhow::Result;
use chrono::{DateTime, Utc};
use clap::Parser;
use crossalpha_state::v04::{NormalizedVenueRow, compute_market_mechanics};
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
struct Args {
    input: PathBuf,
    #[arg(long)]
    generated_at: String,
    #[arg(long, default_value_t = 90)]
    maximum_age_seconds: i64,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let rows: Vec<NormalizedVenueRow> = serde_json::from_reader(File::open(args.input)?)?;
    let generated_at = DateTime::parse_from_rfc3339(&args.generated_at)?.with_timezone(&Utc);
    let report = compute_market_mechanics(&rows, generated_at, args.maximum_age_seconds);
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
