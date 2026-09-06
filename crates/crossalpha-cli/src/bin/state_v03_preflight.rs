use anyhow::Result;
use clap::Parser;
use std::time::Duration;

#[derive(Debug, Parser)]
#[command(
    name = "crossalpha-state-v03-preflight-rs",
    about = "Native Rust capability-probed State V0.3 preflight"
)]
struct Args {
    #[arg(long, default_value_t = 30.0)]
    http_timeout: f64,
    #[arg(long, env = "EVM_RPC_URL")]
    evm_rpc_url: Option<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let _ = dotenvy::dotenv();
    let args = Args::parse();
    if !args.http_timeout.is_finite() || args.http_timeout <= 0.0 {
        anyhow::bail!("--http-timeout must be a finite positive number");
    }
    let report = crossalpha_state::v03_preflight::run_preflight(
        Duration::from_secs_f64(args.http_timeout),
        args.evm_rpc_url.as_deref(),
    )
    .await?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
