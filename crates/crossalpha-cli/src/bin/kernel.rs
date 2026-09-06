use anyhow::Result;
use clap::{Parser, Subcommand};
use crossalpha_data::{
    AssetReturnRow, InstrumentDefinition, ParentBar, normalize_parent_futures_daily,
};
use crossalpha_market::{VenueQuality, assess_venue_quality};
use crossalpha_outcomes::{OutcomeMark, outcome_metrics};
use crossalpha_research::baseline::{BaselineConfig, apply_constraints, compute_features};
use crossalpha_research::paper::{
    apply_shadow_multiplier, build_daily_panel, compute_frozen_b3_target,
};
use crossalpha_research::{
    ContractMeta, FuturesBar, build_previous_volume_roll_map, build_roll_mtm_returns,
};
use serde::Deserialize;
use serde_json::json;
use std::collections::BTreeMap;
use std::fs::File;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-kernel-rs",
    about = "Deterministic Rust research kernel runner"
)]
struct Args {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    DataNormalize { input: PathBuf },
    FuturesRoll { input: PathBuf },
    Baseline { input: PathBuf },
    PaperTarget { input: PathBuf },
    AbMultiplier { input: PathBuf },
    Outcomes { input: PathBuf },
    Market { input: PathBuf },
}

#[derive(Debug, Deserialize)]
struct DataFixture {
    bars: Vec<ParentBar>,
    definitions: Vec<InstrumentDefinition>,
}

#[derive(Debug, Deserialize)]
struct RollFixture {
    bars: Vec<FuturesBar>,
    metadata: Vec<ContractMeta>,
    #[serde(default = "default_safety_days")]
    safety_days: i64,
    #[serde(default)]
    roll_cost_bps: f64,
}

#[derive(Debug, Deserialize)]
struct BaselineFixture {
    returns: Vec<f64>,
    #[serde(default)]
    raw_weights: BTreeMap<String, f64>,
}

#[derive(Debug, Deserialize)]
struct PaperFixture {
    rows: Vec<AssetReturnRow>,
    start: chrono::NaiveDate,
    end: chrono::NaiveDate,
    signal_date: chrono::NaiveDate,
}

#[derive(Debug, Deserialize)]
struct AbMultiplierFixture {
    weights: BTreeMap<String, f64>,
    multiplier: f64,
}

#[derive(Debug, Deserialize)]
struct OutcomesFixture {
    dates: Vec<chrono::NaiveDate>,
    a_marks: Vec<OutcomeMark>,
    b_marks: Vec<OutcomeMark>,
}

#[derive(Debug, Deserialize)]
struct MarketFixture {
    rows: Vec<VenueQuality>,
    asset: String,
    generated_at: chrono::DateTime<chrono::Utc>,
    maximum_age_seconds: i64,
    minimum_venues: usize,
}

fn main() -> Result<()> {
    let args = Args::parse();
    let output = match args.command {
        Command::DataNormalize { input } => {
            let fixture: DataFixture = serde_json::from_reader(File::open(input)?)?;
            serde_json::to_value(normalize_parent_futures_daily(
                &fixture.bars,
                &fixture.definitions,
            )?)?
        }
        Command::FuturesRoll { input } => {
            let fixture: RollFixture = serde_json::from_reader(File::open(input)?)?;
            let roll_map = build_previous_volume_roll_map(
                &fixture.bars,
                &fixture.metadata,
                fixture.safety_days,
            )?;
            let returns =
                build_roll_mtm_returns(&fixture.bars, &roll_map, fixture.roll_cost_bps)?;
            json!({"roll_map": roll_map, "returns": returns})
        }
        Command::Baseline { input } => {
            let fixture: BaselineFixture = serde_json::from_reader(File::open(input)?)?;
            let config = BaselineConfig::default();
            json!({
                "features": compute_features(&fixture.returns, config)?,
                "weights": apply_constraints(&fixture.raw_weights, config),
            })
        }
        Command::PaperTarget { input } => {
            let fixture: PaperFixture = serde_json::from_reader(File::open(input)?)?;
            let panel = build_daily_panel(&fixture.rows, fixture.start, fixture.end)?;
            serde_json::to_value(compute_frozen_b3_target(
                &panel,
                fixture.signal_date,
            )?)?
        }
        Command::AbMultiplier { input } => {
            let fixture: AbMultiplierFixture = serde_json::from_reader(File::open(input)?)?;
            serde_json::to_value(apply_shadow_multiplier(
                &fixture.weights,
                fixture.multiplier,
            )?)?
        }
        Command::Outcomes { input } => {
            let fixture: OutcomesFixture = serde_json::from_reader(File::open(input)?)?;
            let a: BTreeMap<_, _> = fixture
                .a_marks
                .into_iter()
                .map(|row| (row.date, row))
                .collect();
            let b: BTreeMap<_, _> = fixture
                .b_marks
                .into_iter()
                .map(|row| (row.date, row))
                .collect();
            serde_json::to_value(outcome_metrics(&fixture.dates, &a, &b)?)?
        }
        Command::Market { input } => {
            let fixture: MarketFixture = serde_json::from_reader(File::open(input)?)?;
            serde_json::to_value(assess_venue_quality(
                &fixture.rows,
                &fixture.asset,
                fixture.generated_at,
                fixture.maximum_age_seconds,
                fixture.minimum_venues,
            )?)?
        }
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

fn default_safety_days() -> i64 {
    5
}
