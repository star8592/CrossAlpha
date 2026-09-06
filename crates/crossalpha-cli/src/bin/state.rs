use anyhow::Result;
use clap::{Parser, Subcommand, ValueEnum};
use crossalpha_state::v03_engine::NativeStateV03;
use crossalpha_state::v04_engine::NativeStateV04Engine;
use crossalpha_state::{StateRuntimeContext, StateSpec};
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Clone, Copy, ValueEnum)]
enum Version {
    V03,
    V04,
}

#[derive(Debug, Parser)]
#[command(name = "crossalpha-state-rs", about = "Native Rust CrossAlpha State control plane")]
struct Args {
    version: Version,
    #[command(subcommand)]
    command: Command,
    #[arg(long, env = "CROSSALPHA_DATA_DIR", default_value = "./data")]
    data_root: PathBuf,
    #[arg(long, env = "CROSSALPHA_HTTP_TIMEOUT", default_value_t = 30.0)]
    http_timeout: f64,
}

#[derive(Debug, Subcommand)]
enum Command {
    ConfigCheck {
        #[arg(long)]
        config: Option<PathBuf>,
    },
    Preflight,
    Freeze,
    Cycle,
    Integrity,
    Status,
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    let args = Args::parse();
    if !args.http_timeout.is_finite() || args.http_timeout <= 0.0 {
        anyhow::bail!("--http-timeout must be a finite positive number");
    }
    let state: Box<dyn StateSpec> = match args.version {
        Version::V03 => Box::new(NativeStateV03),
        Version::V04 => Box::new(NativeStateV04Engine),
    };
    let context = StateRuntimeContext {
        data_root: args.data_root.clone(),
        http_timeout: Duration::from_secs_f64(args.http_timeout),
        evm_rpc_url: std::env::var("EVM_RPC_URL").ok().filter(|value| !value.is_empty()),
    };
    let output = match args.command {
        Command::ConfigCheck { config } => {
            let config = config.unwrap_or_else(|| match args.version {
                Version::V03 => PathBuf::from("config/state_v03.yaml"),
                Version::V04 => PathBuf::from("config/state_v04.yaml"),
            });
            serde_json::to_value(state.validate_config(&config)?)?
        }
        Command::Preflight => state.preflight(&context).await?,
        Command::Freeze => state.freeze(&context).await?,
        Command::Cycle => state.cycle(&context).await?,
        Command::Integrity => state.integrity(&args.data_root)?,
        Command::Status => state.status(&args.data_root)?,
    };
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}
