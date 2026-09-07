use anyhow::{Context, Result, bail};
use chrono::NaiveDate;
use clap::Parser;
use crossalpha_data::{
    FreeCoreRange, build_free_core_returns, parse_binance_payload, parse_fred_payload,
    parse_tiingo_payload, write_free_core_fixture_canonical,
};
use serde_json::{Value, json};
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-free-core-fixture-rs",
    about = "Parse and optionally materialize deterministic Free Core fixtures without network access"
)]
struct Args {
    input: PathBuf,

    /// Explicit temporary output root for canonical/quality/returns parity.
    #[arg(long)]
    output_root: Option<PathBuf>,

    #[arg(long)]
    start: Option<String>,

    #[arg(long)]
    end: Option<String>,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let fixture: Value = serde_json::from_reader(File::open(&args.input)?)?;

    let mut tiingo_rows = Vec::new();
    for tiingo in fixture_entries(fixture.get("tiingo").context("tiingo fixture missing")?)? {
        tiingo_rows.extend(parse_tiingo_payload(
            tiingo
                .get("economic_asset")
                .and_then(Value::as_str)
                .context("tiingo economic_asset missing")?,
            tiingo
                .get("ticker")
                .and_then(Value::as_str)
                .context("tiingo ticker missing")?,
            tiingo.get("payload").context("tiingo payload missing")?,
        )?);
    }

    let mut binance_rows = Vec::new();
    for binance in fixture_entries(fixture.get("binance").context("binance fixture missing")?)? {
        let payload = binance
            .get("payload")
            .and_then(Value::as_array)
            .context("binance payload missing")?;
        binance_rows.extend(parse_binance_payload(
            binance
                .get("economic_asset")
                .and_then(Value::as_str)
                .context("binance economic_asset missing")?,
            binance
                .get("symbol")
                .and_then(Value::as_str)
                .context("binance symbol missing")?,
            payload,
        )?);
    }

    let fred = fixture.get("fred").context("fred fixture missing")?;
    let fred_rows = parse_fred_payload(
        fred.get("series_id")
            .and_then(Value::as_str)
            .context("fred series_id missing")?,
        fred.get("payload").context("fred payload missing")?,
    )?;

    let pipeline = match (&args.output_root, &args.start, &args.end) {
        (None, None, None) => Value::Null,
        (Some(output_root), Some(start), Some(end)) => {
            let range = FreeCoreRange::new(parse_date(start)?, parse_date(end)?)?;
            let canonical = write_free_core_fixture_canonical(
                output_root,
                &range,
                &tiingo_rows,
                &binance_rows,
                &fred_rows,
            )?;
            let returns = build_free_core_returns(output_root, &range)?;
            json!({
                "range": {"start": range.start, "end": range.end},
                "canonical": canonical,
                "returns": returns,
            })
        }
        _ => bail!("--output-root, --start, and --end must be supplied together"),
    };

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "tiingo": tiingo_rows,
            "binance": binance_rows,
            "fred": fred_rows,
            "pipeline": pipeline,
        }))?
    );
    Ok(())
}

fn fixture_entries(value: &Value) -> Result<Vec<&Value>> {
    match value {
        Value::Object(_) => Ok(vec![value]),
        Value::Array(values) => Ok(values.iter().collect()),
        _ => bail!("fixture section must be an object or list"),
    }
}

fn parse_date(value: &str) -> Result<NaiveDate> {
    Ok(NaiveDate::parse_from_str(value, "%Y-%m-%d")?)
}
