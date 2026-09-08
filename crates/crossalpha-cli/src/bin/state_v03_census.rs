use anyhow::Result;
use chrono::{DateTime, Utc};
use clap::Parser;
use crossalpha_state::v03_census::{AccountDataRow, CensusPolicy, compute_borrower_census};
use serde::Deserialize;
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
struct Args {
    input: PathBuf,
}

#[derive(Debug, Deserialize)]
struct Fixture {
    rows: Vec<AccountDataRow>,
    total_candidate_addresses: usize,
    bootstrap_complete: bool,
    block_number: u64,
    captured_at: DateTime<Utc>,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let fixture: Fixture = serde_json::from_reader(File::open(args.input)?)?;
    let report = compute_borrower_census(
        &fixture.rows,
        fixture.total_candidate_addresses,
        fixture.bootstrap_complete,
        fixture.block_number,
        fixture.captured_at,
        CensusPolicy::default(),
    )?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
