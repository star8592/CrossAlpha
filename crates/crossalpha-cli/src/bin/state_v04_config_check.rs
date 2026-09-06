use anyhow::Result;
use clap::Parser;
use std::path::PathBuf;

#[derive(Debug, Parser)]
struct Args {
    config: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let report = crossalpha_state::v04::NativeStateV04::strict_config_report(&args.config)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    if !report.ok {
        std::process::exit(2);
    }
    Ok(())
}
