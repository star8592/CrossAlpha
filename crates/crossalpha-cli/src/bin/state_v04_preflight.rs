use anyhow::Result;
use clap::Parser;
use crossalpha_state::v04_engine::NativeStateV04Engine;
use crossalpha_state::{StateRuntimeContext, StateSpec};
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Parser)]
struct Args {
    #[arg(long, env = "CROSSALPHA_DATA_DIR", default_value = "./data")]
    data_root: PathBuf,
    #[arg(long, env = "CROSSALPHA_HTTP_TIMEOUT", default_value_t = 30.0)]
    http_timeout: f64,
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    let args = Args::parse();
    if !args.http_timeout.is_finite() || args.http_timeout <= 0.0 {
        anyhow::bail!("--http-timeout must be a finite positive number");
    }
    let context = StateRuntimeContext {
        data_root: args.data_root,
        http_timeout: Duration::from_secs_f64(args.http_timeout),
        evm_rpc_url: None,
    };
    let report = NativeStateV04Engine.preflight(&context).await?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
