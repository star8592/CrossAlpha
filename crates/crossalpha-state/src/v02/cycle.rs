use crate::StateRuntimeContext;
use crate::v02::{
    AaveMarketInput, BasisInput, CompositionInput, LiquidationInput, StablecoinChainInput,
    StablecoinSystemInput, StateV02Config, StateV02Inputs, compute_state_v02,
};
use crate::v02_artifacts::write_state_snapshot;
use crate::v02_provider::AaveV02Client;
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Datelike, Timelike, Utc};
use crossalpha_features::{
    AaveLiquidationRow, AaveMarketRow, compute_recent_hyperliquid_market_state,
    compute_recent_stablecoin_state, load_latest_stablecoin_chain_composition,
    load_recent_aave_canonical, parse_aave_liquidations, parse_aave_markets,
};
use crossalpha_storage::{ObservationEnvelope, RawSnapshotManifest, RawSnapshotStore};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

pub async fn preflight(context: &StateRuntimeContext) -> Result<Value> {
    run(context, false).await
}

pub async fn run_cycle(context: &StateRuntimeContext) -> Result<Value> {
    if !crate::v02_runtime_binding::verify_runtime_binding_file(
        &crate::v02_runtime_binding::runtime_binding_path(&context.data_root),
    )? {
        bail!("Native State V0.2 cycle refused: Rust runtime binding missing, invalid, or stale");
    }
    run(context, true).await
}

async fn run(context: &StateRuntimeContext, write: bool) -> Result<Value> {
    let started_at = Utc::now();
    let client = AaveV02Client::new(context.http_timeout)?;
    let market_envelope = client.collect_market().await?;
    let store = RawSnapshotStore::new(&context.data_root);

    let mut market_manifest = None;
    let mut current_market_rows = Vec::new();
    if write {
        market_manifest = Some(store.write(&market_envelope)?);
    } else {
        current_market_rows = parse_aave_markets(
            &market_envelope,
            &synthetic_manifest(&market_envelope, "preflight:aave-market"),
        )?;
    }

    let (liquidation_status, current_liquidations) = if let Some(rpc_url) =
        context.evm_rpc_url.as_deref()
    {
        match client.collect_liquidations(rpc_url, 512).await {
            Ok(envelope) => {
                if write {
                    let manifest = store.write(&envelope)?;
                    (
                        json!({"configured":true,"ok":true,"manifest":manifest,"error":Value::Null}),
                        Vec::new(),
                    )
                } else {
                    let rows = parse_aave_liquidations(
                        &envelope,
                        &synthetic_manifest(&envelope, "preflight:aave-liquidations"),
                    )?;
                    (
                        json!({"configured":true,"ok":true,"error":Value::Null}),
                        rows,
                    )
                }
            }
            Err(error) => (
                json!({"configured":true,"ok":false,"error":format!("{error:#}")}),
                Vec::new(),
            ),
        }
    } else {
        (
            json!({
                "configured":false,
                "ok":Value::Null,
                "error":Value::Null,
                "reason":"EVM_RPC_URL optional and not configured"
            }),
            Vec::new(),
        )
    };

    let (mut aave_markets, mut liquidations) = load_recent_aave_canonical(&context.data_root, 8)?;
    if !write {
        aave_markets.extend(current_market_rows);
        liquidations.extend(current_liquidations);
    }
    let (stable_system, stable_chains) = compute_recent_stablecoin_state(&context.data_root, 10)?;
    let assets = vec!["BTC".to_owned(), "ETH".to_owned()];
    let hyperliquid = compute_recent_hyperliquid_market_state(&context.data_root, 2, &assets)?;
    let composition = load_latest_stablecoin_chain_composition(&context.data_root, 2)?;

    if aave_markets.is_empty() {
        bail!("State V0.2 has no Aave market inputs after successful collection");
    }
    let inputs = StateV02Inputs {
        aave_markets: aave_markets.iter().map(aave_input).collect(),
        stablecoin_system: stable_system
            .iter()
            .map(|row| StablecoinSystemInput {
                observed_at: row.observed_at,
                known_at: row.known_at,
                usd_market_value_usd: row.usd_market_value_usd,
                chain_coverage_ratio: row.chain_coverage_ratio,
                chain_abs_residual_ratio: row.chain_abs_residual_ratio,
            })
            .collect(),
        stablecoin_chain_state: stable_chains
            .iter()
            .map(|row| StablecoinChainInput {
                observed_at: row.observed_at,
                known_at: row.known_at,
                chain: row.chain.clone(),
                market_value_usd: row.market_value_usd,
            })
            .collect(),
        hyperliquid_market_state: hyperliquid
            .iter()
            .map(|row| BasisInput {
                observed_at: row.observed_at,
                known_at: row.known_at,
                asset: row.asset.clone(),
                basis_z_24h: row.basis_z_24h,
            })
            .collect(),
        stablecoin_chain_composition: composition
            .iter()
            .map(|row| CompositionInput {
                observed_at: row.observed_at,
                known_at: row.known_at,
                stablecoin_id: value_key(&row.stablecoin_id),
                chain: row.chain.clone(),
                market_value_usd: row.market_value_usd,
            })
            .collect(),
        aave_liquidations: liquidations.iter().map(liquidation_input).collect(),
    };

    let as_of = latest_input_time(&inputs).context("State V0.2 has no point-in-time inputs")?;
    let generated_at = Utc::now();
    let mut state = compute_state_v02(&inputs, as_of, generated_at, StateV02Config::default())?;
    let output = output_path(&context.data_root, generated_at);
    let prospective = if write {
        write_state_snapshot(&output, &state)?;
        state
            .as_object_mut()
            .context("State V0.2 report must be object")?
            .insert("status".to_owned(), Value::String("written".to_owned()));
        state
            .as_object_mut()
            .unwrap()
            .insert("written".to_owned(), Value::Bool(true));
        state.as_object_mut().unwrap().insert(
            "output".to_owned(),
            Value::String(output.to_string_lossy().into_owned()),
        );
        if crate::v02_freeze::freeze_path(&context.data_root).exists() {
            crate::v02_prospective::write_live_observation(
                &context.data_root,
                &state,
                &output,
                Utc::now(),
            )?
        } else {
            json!({"protocol":"CROSSALPHA_STATE_V0_2_PROSPECTIVE","status":"not_frozen_no_prospective_write"})
        }
    } else {
        state
            .as_object_mut()
            .context("State V0.2 report must be object")?
            .insert("status".to_owned(), Value::String("computed".to_owned()));
        state
            .as_object_mut()
            .unwrap()
            .insert("written".to_owned(), Value::Bool(false));
        json!({"status":"preflight_no_write"})
    };

    Ok(json!({
        "protocol":"CROSSALPHA_STATE_V0_2_CYCLE",
        "started_at": started_at.to_rfc3339(),
        "completed_at": Utc::now().to_rfc3339(),
        "data_cost_usd":0,
        "v01_collector_or_paper_mutated":false,
        "aave_market":{
            "required":true,
            "ok":true,
            "manifest":market_manifest,
        },
        "aave_liquidations_rpc":liquidation_status,
        "state_v02":state,
        "prospective":prospective,
        "written":write,
    }))
}

pub fn output_path(data_root: &Path, generated: DateTime<Utc>) -> PathBuf {
    data_root
        .join("derived/state/v02")
        .join(format!("year={:04}", generated.year()))
        .join(format!("month={:02}", generated.month()))
        .join(format!("day={:02}", generated.day()))
        .join(format!(
            "state_at={:02}{:02}{:02}{:06}.parquet",
            generated.hour(),
            generated.minute(),
            generated.second(),
            generated.timestamp_subsec_micros()
        ))
}

fn aave_input(row: &AaveMarketRow) -> AaveMarketInput {
    AaveMarketInput {
        observed_at: row.observed_at,
        known_at: row.known_at,
        symbol: row.symbol.clone(),
        market_name: row.market_name.clone(),
        borrow_apy_pct: row.borrow_apy_pct,
        available_liquidity_usd: row.available_liquidity_usd,
        borrow_cap_reached: row.borrow_cap_reached,
        is_frozen: row.is_frozen,
        is_paused: row.is_paused,
    }
}

fn liquidation_input(row: &AaveLiquidationRow) -> LiquidationInput {
    LiquidationInput {
        event_time: row.event_time,
        observed_at: row.observed_at,
        known_at: row.known_at,
        transaction_hash: row.transaction_hash.clone(),
        log_index: row.log_index,
    }
}

fn latest_input_time(inputs: &StateV02Inputs) -> Option<DateTime<Utc>> {
    inputs
        .aave_markets
        .iter()
        .map(|row| row.observed_at)
        .chain(inputs.stablecoin_system.iter().map(|row| row.observed_at))
        .chain(
            inputs
                .hyperliquid_market_state
                .iter()
                .map(|row| row.observed_at),
        )
        .chain(
            inputs
                .stablecoin_chain_composition
                .iter()
                .map(|row| row.observed_at),
        )
        .max()
}

fn synthetic_manifest(envelope: &ObservationEnvelope, suffix: &str) -> RawSnapshotManifest {
    RawSnapshotManifest {
        path: suffix.to_owned(),
        sha256: suffix.to_owned(),
        bytes: 0,
        compressed_bytes: None,
        observed_at: envelope.observed_at,
        source_id: envelope.source_id.clone(),
        observation_type: envelope.observation_type.clone(),
    }
}

fn value_key(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}
