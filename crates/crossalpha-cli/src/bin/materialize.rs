use anyhow::{Result, bail};
use clap::{Parser, ValueEnum};
use crossalpha_features::{materialize_recent_canonical, materialize_recent_features};
use serde::Serialize;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, ValueEnum)]
enum Scope {
    Canonical,
    Features,
    All,
}

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-materialize-rs",
    about = "Bounded native CrossAlpha canonical/feature materializer"
)]
struct Args {
    #[arg(long, env = "CROSSALPHA_DATA_DIR", default_value = "./data")]
    data_root: PathBuf,

    #[arg(long)]
    output_root: Option<PathBuf>,

    #[arg(long, default_value_t = 2)]
    recent_days: usize,

    #[arg(long, value_enum, default_value_t = Scope::All)]
    scope: Scope,

    #[arg(long, default_value_t = false)]
    allow_production_write: bool,
}

#[derive(Debug, Serialize)]
struct MaterializeReport {
    protocol: &'static str,
    bounded: bool,
    recent_days: usize,
    data_root: PathBuf,
    output_root: PathBuf,
    production_write: bool,
    canonical: Option<crossalpha_features::CanonicalMaterializeReport>,
    features: Option<crossalpha_features::FeatureMaterializeReport>,
}

fn main() -> Result<()> {
    dotenvy::dotenv().ok();
    let args = Args::parse();
    if args.recent_days == 0 {
        bail!("recent-days must be positive");
    }
    let output_root = args
        .output_root
        .clone()
        .unwrap_or_else(|| args.data_root.clone());
    let production_write = same_path(&output_root, &args.data_root);
    if production_write && !args.allow_production_write {
        bail!(
            "production materialization refused: pass --allow-production-write only after canonical/feature parity gates pass"
        );
    }

    let canonical = match args.scope {
        Scope::Canonical | Scope::All => Some(materialize_recent_canonical(
            &args.data_root,
            &output_root,
            args.recent_days,
        )?),
        Scope::Features => None,
    };
    let features = match args.scope {
        Scope::Features | Scope::All => Some(materialize_recent_features(
            &args.data_root,
            &output_root,
            args.recent_days,
        )?),
        Scope::Canonical => None,
    };

    let report = MaterializeReport {
        protocol: "CROSSALPHA_NATIVE_MATERIALIZER_V1",
        bounded: true,
        recent_days: args.recent_days,
        data_root: args.data_root,
        output_root,
        production_write,
        canonical,
        features,
    };
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

fn same_path(left: &Path, right: &Path) -> bool {
    match (left.canonicalize(), right.canonicalize()) {
        (Ok(left), Ok(right)) => left == right,
        _ => left == right,
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn default_report_protocol_is_explicit() {
        let value = serde_json::json!({"protocol": "CROSSALPHA_NATIVE_MATERIALIZER_V1"});
        assert_eq!(value["protocol"], "CROSSALPHA_NATIVE_MATERIALIZER_V1");
    }
}
