use anyhow::Result;
use chrono::{DateTime, Utc};
use clap::Parser;
use crossalpha_state::v04_provider::{VenuePayload, parse_venue_snapshot};
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
struct Args {
    input: PathBuf,
    #[arg(long)]
    known_at: String,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let payload: VenuePayload = serde_json::from_reader(File::open(args.input)?)?;
    let known_at = DateTime::parse_from_rfc3339(&args.known_at)?.with_timezone(&Utc);
    let row = parse_venue_snapshot(&payload, known_at)?;
    println!("{}", serde_json::to_string_pretty(&row)?);
    Ok(())
}
