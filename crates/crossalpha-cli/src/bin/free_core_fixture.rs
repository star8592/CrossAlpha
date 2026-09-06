use anyhow::{Context, Result};
use clap::Parser;
use crossalpha_data::{parse_binance_payload, parse_fred_payload, parse_tiingo_payload};
use serde_json::{Value, json};
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-free-core-fixture-rs",
    about = "Parse deterministic Free Core provider fixtures without network access"
)]
struct Args {
    input: PathBuf,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let fixture: Value = serde_json::from_reader(File::open(&args.input)?)?;

    let tiingo = fixture.get("tiingo").context("tiingo fixture missing")?;
    let tiingo_rows = parse_tiingo_payload(
        tiingo
            .get("economic_asset")
            .and_then(Value::as_str)
            .context("tiingo economic_asset missing")?,
        tiingo
            .get("ticker")
            .and_then(Value::as_str)
            .context("tiingo ticker missing")?,
        tiingo.get("payload").context("tiingo payload missing")?,
    )?;

    let binance = fixture.get("binance").context("binance fixture missing")?;
    let binance_payload = binance
        .get("payload")
        .and_then(Value::as_array)
        .context("binance payload missing")?;
    let binance_rows = parse_binance_payload(
        binance
            .get("economic_asset")
            .and_then(Value::as_str)
            .context("binance economic_asset missing")?,
        binance
            .get("symbol")
            .and_then(Value::as_str)
            .context("binance symbol missing")?,
        binance_payload,
    )?;

    let fred = fixture.get("fred").context("fred fixture missing")?;
    let fred_rows = parse_fred_payload(
        fred.get("series_id")
            .and_then(Value::as_str)
            .context("fred series_id missing")?,
        fred.get("payload").context("fred payload missing")?,
    )?;

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "tiingo": tiingo_rows,
            "binance": binance_rows,
            "fred": fred_rows,
        }))?
    );
    Ok(())
}
