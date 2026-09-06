use anyhow::Result;
use clap::Parser;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-state-v03-config-check-rs",
    about = "Native Rust strict State V0.3 config/implementation audit"
)]
struct Args {
    config: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let report = crossalpha_state::v03::StateV03::strict_config_report(&args.config)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    if !report.ok {
        std::process::exit(2);
    }
    Ok(())
}
