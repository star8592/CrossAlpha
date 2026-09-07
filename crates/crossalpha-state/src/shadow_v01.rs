use anyhow::{Result, bail};
use chrono::{DateTime, Duration, Utc};
use crossalpha_features::{
    HyperliquidMarketStateRow, StablecoinSystemStateRow, compute_recent_hyperliquid_market_state,
    compute_recent_stablecoin_state,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::path::Path;

pub const PROTOCOL: &str = "CROSSALPHA_STATE_SHADOW_V0_1";
pub const MODE: &str = "SHADOW_ONLY";
pub const FOCUS_ASSETS: [&str; 2] = ["BTC", "ETH"];

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct StateShadowConfig {
    pub max_source_age_minutes: i64,
    pub min_hyperliquid_rolling_observations: i64,
    pub stablecoin_min_delta_7d_coverage: f64,
    pub stablecoin_min_chain_coverage: f64,
    pub stablecoin_max_chain_coverage: f64,
    pub stablecoin_max_chain_abs_residual_ratio: f64,
    pub z_full_stress: f64,
    pub stablecoin_contraction_full_stress: f64,
    pub peg_full_stress_bps: f64,
    pub moderate_pressure_threshold: f64,
    pub severe_pressure_threshold: f64,
    pub moderate_risk_multiplier: f64,
    pub severe_risk_multiplier: f64,
}

impl Default for StateShadowConfig {
    fn default() -> Self {
        Self {
            max_source_age_minutes: 30,
            min_hyperliquid_rolling_observations: 24,
            stablecoin_min_delta_7d_coverage: 0.80,
            stablecoin_min_chain_coverage: 0.98,
            stablecoin_max_chain_coverage: 1.02,
            stablecoin_max_chain_abs_residual_ratio: 0.02,
            z_full_stress: 3.0,
            stablecoin_contraction_full_stress: 0.02,
            peg_full_stress_bps: 100.0,
            moderate_pressure_threshold: 1.0 / 3.0,
            severe_pressure_threshold: 2.0 / 3.0,
            moderate_risk_multiplier: 0.75,
            severe_risk_multiplier: 0.50,
        }
    }
}

pub fn build_latest_shadow_state(data_root: &Path, generated_at: DateTime<Utc>) -> Result<Value> {
    let assets = vec!["BTC".to_owned(), "ETH".to_owned()];
    let hl = compute_recent_hyperliquid_market_state(data_root, 2, &assets)?;
    let (stable, _) = compute_recent_stablecoin_state(data_root, 2)?;
    if hl.is_empty() && stable.is_empty() {
        return Ok(json!({
            "protocol": PROTOCOL,
            "mode": MODE,
            "status": "no_inputs",
            "written": false,
        }));
    }
    let hl_max = hl.iter().map(|row| row.observed_at).max();
    let stable_max = stable.iter().map(|row| row.observed_at).max();
    let as_of = match (hl_max, stable_max) {
        (Some(left), Some(right)) => left.min(right),
        (Some(value), None) | (None, Some(value)) => value,
        (None, None) => unreachable!(),
    };
    let mut value = compute_shadow_state(
        &hl,
        &stable,
        as_of,
        generated_at,
        StateShadowConfig::default(),
    )?;
    value
        .as_object_mut()
        .expect("shadow state is object")
        .insert("status".to_owned(), Value::String("computed".to_owned()));
    value
        .as_object_mut()
        .expect("shadow state is object")
        .insert("written".to_owned(), Value::Bool(false));
    Ok(value)
}

pub fn compute_shadow_state(
    hyperliquid: &[HyperliquidMarketStateRow],
    stablecoin: &[StablecoinSystemStateRow],
    as_of: DateTime<Utc>,
    generated_at: DateTime<Utc>,
    config: StateShadowConfig,
) -> Result<Value> {
    if generated_at < as_of {
        bail!("generated_at cannot precede as_of");
    }
    let mut hl_json = serde_json::Map::new();
    let mut valid_hl = Vec::new();
    for asset in FOCUS_ASSETS {
        let selected = hyperliquid
            .iter()
            .filter(|row| {
                row.asset == asset && row.observed_at <= as_of && row.known_at <= generated_at
            })
            .max_by(|left, right| {
                left.observed_at
                    .cmp(&right.observed_at)
                    .then(left.known_at.cmp(&right.known_at))
            });
        let component = hyperliquid_component(selected, generated_at, config);
        if component.get("valid").and_then(Value::as_bool) == Some(true)
            && let Some(pressure) = component.get("pressure").and_then(Value::as_f64)
        {
            valid_hl.push(pressure);
        }
        hl_json.insert(asset.to_owned(), component);
    }

    let stable_selected = stablecoin
        .iter()
        .filter(|row| row.observed_at <= as_of && row.known_at <= generated_at)
        .max_by(|left, right| {
            left.observed_at
                .cmp(&right.observed_at)
                .then(left.known_at.cmp(&right.known_at))
        });
    let stable = stablecoin_component(stable_selected, generated_at, config);
    let leverage_pressure = valid_hl.iter().copied().reduce(f64::max);
    let stablecoin_pressure = if stable.get("valid").and_then(Value::as_bool) == Some(true) {
        stable.get("pressure").and_then(Value::as_f64)
    } else {
        None
    };
    let state_pressure = match (leverage_pressure, stablecoin_pressure) {
        (Some(left), Some(right)) => Some(left.max(right)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    };
    let (band, multiplier) = multiplier_from_pressure(state_pressure, config);
    let source_count =
        valid_hl.len() + usize::from(stable.get("valid").and_then(Value::as_bool) == Some(true));
    let confidence = match source_count {
        3 => "FULL",
        1 | 2 => "PARTIAL",
        _ => "NONE",
    };
    Ok(json!({
        "protocol": PROTOCOL,
        "mode": MODE,
        "shadow_only": true,
        "core_protocol_mutated": false,
        "as_of": as_of.to_rfc3339(),
        "generated_at": generated_at.to_rfc3339(),
        "focus_assets": FOCUS_ASSETS,
        "hyperliquid": Value::Object(hl_json),
        "stablecoin": stable,
        "leverage_pressure": leverage_pressure,
        "stablecoin_pressure": stablecoin_pressure,
        "state_pressure": state_pressure,
        "state_band": band,
        "shadow_risk_multiplier": multiplier,
        "data_confidence": confidence,
        "valid_source_components": source_count,
        "expected_source_components": 3,
        "interpretation": "Shadow-only de-risking overlay. It never changes relative Core weights, never increases risk above Frozen B3, and is not part of the B3 paper ledger.",
    }))
}

fn hyperliquid_component(
    row: Option<&HyperliquidMarketStateRow>,
    generated_at: DateTime<Utc>,
    config: StateShadowConfig,
) -> Value {
    let Some(row) = row else {
        return json!({"valid":false,"reason":"missing"});
    };
    if row.rolling_observations_24h < config.min_hyperliquid_rolling_observations {
        return json!({"valid":false,"reason":"insufficient_rolling_observations"});
    }
    if !source_fresh(row.observed_at, generated_at, config.max_source_age_minutes) {
        return json!({"valid":false,"reason":"stale"});
    }
    let values = [
        row.funding_z_24h,
        row.basis_z_24h,
        row.oi_change_z_24h,
        row.spread_z_24h,
    ];
    if values.iter().any(Option::is_none) {
        return json!({"valid":false,"reason":"missing_causal_zscore"});
    }
    let pressures = values.map(|value| positive_z_pressure(value.unwrap(), config.z_full_stress));
    let pressure = pressures.iter().sum::<f64>() / pressures.len() as f64;
    json!({
        "valid":true,
        "reason":"ok",
        "observed_at":row.observed_at.to_rfc3339(),
        "known_at":row.known_at.to_rfc3339(),
        "rolling_observations_24h":row.rolling_observations_24h,
        "funding_z_24h":row.funding_z_24h,
        "basis_z_24h":row.basis_z_24h,
        "oi_change_z_24h":row.oi_change_z_24h,
        "spread_z_24h":row.spread_z_24h,
        "pressure":pressure,
        "component_pressures":{
            "funding_z_24h":pressures[0],
            "basis_z_24h":pressures[1],
            "oi_change_z_24h":pressures[2],
            "spread_z_24h":pressures[3],
        },
    })
}

fn stablecoin_component(
    row: Option<&StablecoinSystemStateRow>,
    generated_at: DateTime<Utc>,
    config: StateShadowConfig,
) -> Value {
    let Some(row) = row else {
        return json!({"valid":false,"reason":"missing"});
    };
    if !source_fresh(row.observed_at, generated_at, config.max_source_age_minutes) {
        return json!({"valid":false,"reason":"stale"});
    }
    let accounting_valid = row.usd_supply_native > 0.0
        && row.usd_delta_7d_native.is_some()
        && row
            .delta_7d_market_value_coverage
            .is_some_and(|value| value >= config.stablecoin_min_delta_7d_coverage)
        && row.chain_coverage_ratio.is_some_and(|value| {
            value >= config.stablecoin_min_chain_coverage
                && value <= config.stablecoin_max_chain_coverage
        })
        && row
            .chain_abs_residual_ratio
            .is_some_and(|value| value <= config.stablecoin_max_chain_abs_residual_ratio);
    if !accounting_valid {
        return json!({
            "valid":false,
            "reason":"accounting_or_coverage_gate_failed",
            "delta_7d_market_value_coverage":row.delta_7d_market_value_coverage,
            "chain_coverage_ratio":row.chain_coverage_ratio,
            "chain_abs_residual_ratio":row.chain_abs_residual_ratio,
        });
    }
    let delta_7d = row.usd_delta_7d_native.unwrap();
    let contraction_ratio = (-delta_7d / row.usd_supply_native).max(0.0);
    let contraction_pressure =
        clip01(contraction_ratio / config.stablecoin_contraction_full_stress);
    let peg_pressure = row
        .weighted_abs_peg_deviation_bps
        .map(|value| clip01(value / config.peg_full_stress_bps))
        .unwrap_or(0.0);
    let pressure = contraction_pressure.max(peg_pressure);
    json!({
        "valid":true,
        "reason":"ok",
        "observed_at":row.observed_at.to_rfc3339(),
        "known_at":row.known_at.to_rfc3339(),
        "usd_supply_native":row.usd_supply_native,
        "usd_delta_7d_native":delta_7d,
        "delta_7d_ratio":delta_7d/row.usd_supply_native,
        "delta_7d_market_value_coverage":row.delta_7d_market_value_coverage,
        "chain_coverage_ratio":row.chain_coverage_ratio,
        "chain_abs_residual_ratio":row.chain_abs_residual_ratio,
        "weighted_abs_peg_deviation_bps":row.weighted_abs_peg_deviation_bps,
        "supply_contraction_pressure":contraction_pressure,
        "peg_pressure":peg_pressure,
        "pressure":pressure,
    })
}

fn source_fresh(
    observed_at: DateTime<Utc>,
    generated_at: DateTime<Utc>,
    max_age_minutes: i64,
) -> bool {
    let age = generated_at - observed_at;
    age >= Duration::zero() && age <= Duration::minutes(max_age_minutes)
}

fn positive_z_pressure(value: f64, full_stress: f64) -> f64 {
    if !value.is_finite() || full_stress <= 0.0 {
        return 0.0;
    }
    clip01(value.max(0.0) / full_stress)
}

fn clip01(value: f64) -> f64 {
    value.clamp(0.0, 1.0)
}

fn multiplier_from_pressure(
    pressure: Option<f64>,
    config: StateShadowConfig,
) -> (&'static str, f64) {
    match pressure {
        None => ("NO_MODIFIER_DATA_INSUFFICIENT", 1.0),
        Some(value) if value >= config.severe_pressure_threshold => {
            ("SEVERE", config.severe_risk_multiplier)
        }
        Some(value) if value >= config.moderate_pressure_threshold => {
            ("MODERATE", config.moderate_risk_multiplier)
        }
        Some(_) => ("NORMAL", 1.0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thresholds_match_frozen_shadow_contract() {
        let cfg = StateShadowConfig::default();
        assert_eq!(
            multiplier_from_pressure(None, cfg),
            ("NO_MODIFIER_DATA_INSUFFICIENT", 1.0)
        );
        assert_eq!(
            multiplier_from_pressure(Some(0.34), cfg),
            ("MODERATE", 0.75)
        );
        assert_eq!(multiplier_from_pressure(Some(0.67), cfg), ("SEVERE", 0.50));
    }
}
