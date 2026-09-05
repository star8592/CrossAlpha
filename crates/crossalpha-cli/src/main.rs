use anyhow::Result;
use clap::{Parser, Subcommand};
use tracing::info;

#[derive(Debug, Parser)]
#[command(name = "crossalpha-rs", version, about = "CrossAlpha Rust control plane")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Validate the Rust workspace/control-plane wiring.
    Doctor,
    /// Validate that an existing YAML config can be parsed.
    ConfigCheck { path: std::path::PathBuf },
    /// Show migration status for the Rust rewrite.
    MigrationStatus,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let cli = Cli::parse();
    match cli.command {
        Command::Doctor => {
            info!("Rust control plane is alive");
            println!("ok=true runtime=rust");
        }
        Command::ConfigCheck { path } => {
            let value: serde_json::Value = crossalpha_config::load_yaml(&path)?;
            println!("ok=true path={} type={}", path.display(), json_type(&value));
        }
        Command::MigrationStatus => {
            println!("phase=R0 workspace=ready python_compat=true data_format_compat=required");
        }
    }
    Ok(())
}

fn json_type(value: &serde_json::Value) -> &'static str {
    match value {
        serde_json::Value::Null => "null",
        serde_json::Value::Bool(_) => "bool",
        serde_json::Value::Number(_) => "number",
        serde_json::Value::String(_) => "string",
        serde_json::Value::Array(_) => "array",
        serde_json::Value::Object(_) => "object",
    }
}
