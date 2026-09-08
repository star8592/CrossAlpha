use anyhow::{Result, bail};
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use crate::StateConfigReport;

pub const PROTOCOL: &str = "CROSSALPHA_STATE_V0_2";
pub const MODE: &str = "PROSPECTIVE_DESCRIPTIVE_SHADOW";
pub const ACTIONABILITY: &str = "DESCRIPTIVE_ONLY";
pub const PROSPECTIVE_PROTOCOL: &str = "CROSSALPHA_STATE_V0_2_PROSPECTIVE";
pub const FOCUS_AAVE_SYMBOLS: [&str; 7] = ["WETH", "ETH", "WBTC", "USDC", "USDT", "GHO", "DAI"];

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct StateV02Config {
    pub max_source_age_minutes: i64,
    pub aave_minimum_reserves: usize,
    pub aave_borrow_apy_full_stress_pct: f64,
    pub aave_low_available_liquidity_full_stress_usd: f64,
    pub stablecoin_lookback_hours: i64,
    pub stablecoin_lag_tolerance_hours: i64,
    pub stablecoin_min_chain_coverage: f64,
    pub stablecoin_max_chain_abs_residual_ratio: f64,
    pub stablecoin_contraction_full_stress_ratio: f64,
    pub stablecoin_migration_full_reference_ratio: f64,
    pub basis_full_stress_z_dispersion: f64,
    pub contagion_min_stablecoin_market_value_usd: f64,
    pub contagion_min_chain_market_value_usd: f64,
    pub aave_weight: f64,
    pub stablecoin_weight: f64,
    pub basis_weight: f64,
    pub contagion_weight: f64,
    pub minimum_valid_components: usize,
    pub full_confidence_components: usize,
}

impl Default for StateV02Config {
    fn default() -> Self {
        Self {
            max_source_age_minutes: 30,
            aave_minimum_reserves: 3,
            aave_borrow_apy_full_stress_pct: 20.0,
            aave_low_available_liquidity_full_stress_usd: 10_000_000.0,
            stablecoin_lookback_hours: 168,
            stablecoin_lag_tolerance_hours: 24,
            stablecoin_min_chain_coverage: 0.98,
            stablecoin_max_chain_abs_residual_ratio: 0.02,
            stablecoin_contraction_full_stress_ratio: 0.02,
            stablecoin_migration_full_reference_ratio: 0.10,
            basis_full_stress_z_dispersion: 3.0,
            contagion_min_stablecoin_market_value_usd: 10_000_000.0,
            contagion_min_chain_market_value_usd: 50_000_000.0,
            aave_weight: 0.30,
            stablecoin_weight: 0.30,
            basis_weight: 0.20,
            contagion_weight: 0.20,
            minimum_valid_components: 2,
            full_confidence_components: 4,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AaveMarketInput {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub symbol: Option<String>,
    pub market_name: Option<String>,
    pub borrow_apy_pct: Option<f64>,
    pub available_liquidity_usd: Option<f64>,
    #[serde(default)]
    pub borrow_cap_reached: bool,
    #[serde(default)]
    pub is_frozen: bool,
    #[serde(default)]
    pub is_paused: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StablecoinSystemInput {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub usd_market_value_usd: f64,
    pub chain_coverage_ratio: Option<f64>,
    pub chain_abs_residual_ratio: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StablecoinChainInput {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub chain: String,
    pub market_value_usd: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BasisInput {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub asset: String,
    pub basis_z_24h: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CompositionInput {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub stablecoin_id: String,
    pub chain: String,
    pub market_value_usd: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct LiquidationInput {
    pub event_time: Option<DateTime<Utc>>,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub transaction_hash: Option<String>,
    pub log_index: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StateV02Inputs {
    #[serde(default)]
    pub aave_markets: Vec<AaveMarketInput>,
    #[serde(default)]
    pub stablecoin_system: Vec<StablecoinSystemInput>,
    #[serde(default)]
    pub stablecoin_chain_state: Vec<StablecoinChainInput>,
    #[serde(default)]
    pub hyperliquid_market_state: Vec<BasisInput>,
    #[serde(default)]
    pub stablecoin_chain_composition: Vec<CompositionInput>,
    #[serde(default)]
    pub aave_liquidations: Vec<LiquidationInput>,
}

pub fn compute_state_v02(
    inputs: &StateV02Inputs,
    as_of: DateTime<Utc>,
    generated_at: DateTime<Utc>,
    cfg: StateV02Config,
) -> Result<Value> {
    if generated_at < as_of {
        bail!("generated_at cannot precede as_of");
    }
    let aave = aave_component(&inputs.aave_markets, as_of, generated_at, cfg);
    let stable = stablecoin_component(
        &inputs.stablecoin_system,
        &inputs.stablecoin_chain_state,
        as_of,
        generated_at,
        cfg,
    );
    let basis = basis_component(&inputs.hyperliquid_market_state, as_of, generated_at, cfg);
    let contagion = contagion_component(
        &inputs.stablecoin_chain_composition,
        as_of,
        generated_at,
        cfg,
    );
    let liquidations = liquidation_component(&inputs.aave_liquidations, as_of, generated_at);
    let borrower_health = json!({
        "valid": false,
        "reason": "auditable_borrower_universe_not_yet_available",
        "status": "REQUIRES_AUDITABLE_BORROWER_UNIVERSE",
        "liquidation_threshold_health_factor": 1.0,
        "market_level_substitution_allowed": false,
        "pressure": Value::Null,
    });
    let deployment = deployment_descriptor(&aave, &stable);

    let components = [
        ("aave_market_stress", &aave, cfg.aave_weight),
        (
            "stablecoin_flow_decomposition",
            &stable,
            cfg.stablecoin_weight,
        ),
        ("basis_dispersion", &basis, cfg.basis_weight),
        ("contagion_graph", &contagion, cfg.contagion_weight),
    ];
    let mut valid_names = Vec::new();
    let mut valid_weight = 0.0;
    let mut weighted_pressure = 0.0;
    for (name, component, weight) in components {
        if component.get("valid").and_then(Value::as_bool) != Some(true) {
            continue;
        }
        let Some(pressure) = component.get("pressure").and_then(Value::as_f64) else {
            continue;
        };
        valid_names.push(name);
        valid_weight += weight;
        weighted_pressure += weight * pressure;
    }
    let composite = (valid_weight > 0.0).then_some(weighted_pressure / valid_weight);
    let valid_count = valid_names.len();
    let confidence = if valid_count >= cfg.full_confidence_components {
        "FULL"
    } else if valid_count >= cfg.minimum_valid_components {
        "PARTIAL"
    } else {
        "INSUFFICIENT"
    };

    Ok(json!({
        "protocol": PROTOCOL,
        "mode": MODE,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "mutates_frozen_core": false,
        "mutates_state_v01": false,
        "mutates_state_ab_v01": false,
        "as_of": as_of.to_rfc3339(),
        "generated_at": generated_at.to_rfc3339(),
        "components": {
            "aave_market_stress": aave,
            "stablecoin_flow_decomposition": stable,
            "basis_dispersion": basis,
            "contagion_graph": contagion,
        },
        "aave_liquidation_activity": liquidations,
        "borrower_health_factor_distribution": borrower_health,
        "deployment_activation": deployment,
        "valid_pressure_components": valid_names,
        "valid_pressure_component_count": valid_count,
        "expected_pressure_component_count": 4,
        "data_confidence": confidence,
        "descriptive_stress_score": composite,
        "interpretation": "O1 descriptive state only. This score is not a trading signal and cannot alter Frozen B3, State V0.1, or the prospective A/B V0.1 ledger.",
    }))
}

pub fn strict_config_report(path: &Path) -> Result<StateConfigReport> {
    let raw: Value = serde_yaml::from_reader(std::fs::File::open(path)?)?;
    let cfg = StateV02Config::default();
    let at = |pointer: &str| raw.pointer(pointer);
    let mut checks = BTreeMap::new();
    checks.insert(
        "protocol".to_owned(),
        at("/protocol").and_then(Value::as_str) == Some(PROTOCOL),
    );
    checks.insert(
        "mode".to_owned(),
        at("/mode").and_then(Value::as_str) == Some(MODE),
    );
    checks.insert(
        "actionability".to_owned(),
        at("/research_policy/actionability").and_then(Value::as_str) == Some(ACTIONABILITY),
    );
    checks.insert(
        "risk_multiplier_null".to_owned(),
        at("/research_policy/risk_multiplier").is_some_and(Value::is_null),
    );
    checks.insert(
        "mutates_frozen_core_false".to_owned(),
        at("/research_policy/mutates_frozen_core").and_then(Value::as_bool) == Some(false),
    );
    checks.insert(
        "mutates_state_v01_false".to_owned(),
        at("/research_policy/mutates_state_v01").and_then(Value::as_bool) == Some(false),
    );
    checks.insert(
        "mutates_state_ab_v01_false".to_owned(),
        at("/research_policy/mutates_state_ab_v01").and_then(Value::as_bool) == Some(false),
    );
    checks.insert(
        "parameter_optimization_disabled".to_owned(),
        at("/research_policy/parameter_optimization_allowed").and_then(Value::as_bool)
            == Some(false),
    );
    checks.insert(
        "retrospective_backfill_disabled".to_owned(),
        at("/research_policy/retrospective_prospective_backfill_allowed").and_then(Value::as_bool)
            == Some(false),
    );
    checks.insert(
        "historical_promotion_disabled".to_owned(),
        at("/research_policy/historical_data_can_promote_to_O2").and_then(Value::as_bool)
            == Some(false),
    );
    checks.insert(
        "aave_max_age".to_owned(),
        value_i64(at("/components/aave_market_stress/max_source_age_minutes"))
            == Some(cfg.max_source_age_minutes),
    );
    checks.insert(
        "aave_minimum_reserves".to_owned(),
        value_usize(at("/components/aave_market_stress/minimum_reserves"))
            == Some(cfg.aave_minimum_reserves),
    );
    checks.insert(
        "aave_apy_threshold".to_owned(),
        value_f64(at(
            "/components/aave_market_stress/borrow_apy_full_stress_pct",
        )) == Some(cfg.aave_borrow_apy_full_stress_pct),
    );
    checks.insert(
        "aave_liquidity_threshold".to_owned(),
        value_f64(at(
            "/components/aave_market_stress/low_available_liquidity_full_stress_usd",
        )) == Some(cfg.aave_low_available_liquidity_full_stress_usd),
    );
    checks.insert(
        "stable_lookback".to_owned(),
        value_i64(at(
            "/components/stablecoin_flow_decomposition/lookback_hours",
        )) == Some(cfg.stablecoin_lookback_hours),
    );
    checks.insert(
        "stable_lag_tolerance".to_owned(),
        value_i64(at(
            "/components/stablecoin_flow_decomposition/lag_tolerance_hours",
        )) == Some(cfg.stablecoin_lag_tolerance_hours),
    );
    checks.insert(
        "stable_chain_coverage".to_owned(),
        value_f64(at(
            "/components/stablecoin_flow_decomposition/minimum_chain_coverage",
        )) == Some(cfg.stablecoin_min_chain_coverage),
    );
    checks.insert(
        "stable_residual_ratio".to_owned(),
        value_f64(at(
            "/components/stablecoin_flow_decomposition/maximum_chain_abs_residual_ratio",
        )) == Some(cfg.stablecoin_max_chain_abs_residual_ratio),
    );
    checks.insert(
        "stable_contraction_threshold".to_owned(),
        value_f64(at(
            "/components/stablecoin_flow_decomposition/contraction_full_stress_ratio",
        )) == Some(cfg.stablecoin_contraction_full_stress_ratio),
    );
    checks.insert(
        "stable_migration_reference".to_owned(),
        value_f64(at(
            "/components/stablecoin_flow_decomposition/migration_full_reference_ratio",
        )) == Some(cfg.stablecoin_migration_full_reference_ratio),
    );
    checks.insert(
        "basis_threshold".to_owned(),
        value_f64(at("/components/basis_dispersion/full_stress_z_dispersion"))
            == Some(cfg.basis_full_stress_z_dispersion),
    );
    checks.insert(
        "contagion_min_stablecoin".to_owned(),
        value_f64(at(
            "/components/contagion_graph/minimum_stablecoin_market_value_usd",
        )) == Some(cfg.contagion_min_stablecoin_market_value_usd),
    );
    checks.insert(
        "contagion_min_chain".to_owned(),
        value_f64(at(
            "/components/contagion_graph/minimum_chain_market_value_usd",
        )) == Some(cfg.contagion_min_chain_market_value_usd),
    );
    checks.insert(
        "minimum_valid_components".to_owned(),
        value_usize(at("/aggregation/minimum_valid_components"))
            == Some(cfg.minimum_valid_components),
    );
    checks.insert(
        "full_confidence_components".to_owned(),
        value_usize(at("/aggregation/full_confidence_components"))
            == Some(cfg.full_confidence_components),
    );
    let weights = at("/aggregation/component_weights").and_then(Value::as_object);
    checks.insert(
        "weights".to_owned(),
        weights.is_some_and(|weights| {
            value_f64(weights.get("aave_market_stress")) == Some(cfg.aave_weight)
                && value_f64(weights.get("stablecoin_flow_stress")) == Some(cfg.stablecoin_weight)
                && value_f64(weights.get("basis_dispersion_stress")) == Some(cfg.basis_weight)
                && value_f64(weights.get("contagion_connectivity_stress"))
                    == Some(cfg.contagion_weight)
        }),
    );
    checks.insert(
        "borrower_health_not_substituted".to_owned(),
        at("/components/borrower_health_factor_distribution/no_market_level_substitution_allowed")
            .and_then(Value::as_bool)
            == Some(true),
    );
    checks.insert(
        "borrower_liquidation_threshold".to_owned(),
        value_f64(at(
            "/components/borrower_health_factor_distribution/liquidation_threshold_health_factor",
        )) == Some(1.0),
    );
    let ok = checks.values().all(|value| *value);
    Ok(StateConfigReport {
        protocol: PROTOCOL.to_owned(),
        audit_level: "STRICT_CONFIG_IMPLEMENTATION_CONSISTENCY".to_owned(),
        ok,
        checks,
    })
}

fn aave_component(
    rows: &[AaveMarketInput],
    as_of: DateTime<Utc>,
    generated: DateTime<Utc>,
    cfg: StateV02Config,
) -> Value {
    let eligible: Vec<&AaveMarketInput> = rows
        .iter()
        .filter(|row| row.observed_at <= as_of && row.known_at <= generated)
        .collect();
    let Some(latest_at) = eligible.iter().map(|row| row.observed_at).max() else {
        return json!({"valid": false, "reason": "missing_aave_market_snapshots"});
    };
    if !fresh(latest_at, generated, cfg.max_source_age_minutes) {
        return json!({"valid": false, "reason": "stale_aave_market_snapshot", "observed_at": latest_at.to_rfc3339()});
    }
    let focus: Vec<&AaveMarketInput> = eligible
        .iter()
        .copied()
        .filter(|row| {
            row.observed_at == latest_at
                && row.symbol.as_deref().is_some_and(|symbol| {
                    FOCUS_AAVE_SYMBOLS.contains(&symbol.to_ascii_uppercase().as_str())
                })
        })
        .collect();
    if focus.len() < cfg.aave_minimum_reserves {
        return json!({"valid": false, "reason": "insufficient_focus_reserves", "observed_at": latest_at.to_rfc3339(), "focus_reserve_count": focus.len()});
    }
    let mut reserve_rows = Vec::new();
    let mut pressures = Vec::new();
    for row in &focus {
        let apy_pressure = row
            .borrow_apy_pct
            .map(|value| clip01(value / cfg.aave_borrow_apy_full_stress_pct));
        let liquidity_pressure = row.available_liquidity_usd.map(|value| {
            clip01(
                (cfg.aave_low_available_liquidity_full_stress_usd - value).max(0.0)
                    / cfg.aave_low_available_liquidity_full_stress_usd,
            )
        });
        let flag_pressure = if row.borrow_cap_reached || row.is_frozen || row.is_paused {
            1.0
        } else {
            0.0
        };
        let pressure = apy_pressure
            .into_iter()
            .chain(liquidity_pressure)
            .chain(std::iter::once(flag_pressure))
            .max_by(|left, right| left.partial_cmp(right).unwrap_or(Ordering::Equal));
        if let Some(value) = pressure {
            pressures.push(value);
        }
        reserve_rows.push(json!({
            "symbol": row.symbol,
            "borrow_apy_pct": row.borrow_apy_pct,
            "available_liquidity_usd": row.available_liquidity_usd,
            "borrow_cap_reached": row.borrow_cap_reached,
            "is_frozen": row.is_frozen,
            "is_paused": row.is_paused,
            "pressure": pressure,
        }));
    }
    let pressure = pressures
        .into_iter()
        .max_by(|left, right| left.partial_cmp(right).unwrap_or(Ordering::Equal));
    let total_latest = sum_option(focus.iter().map(|row| row.available_liquidity_usd));
    let prior_target = latest_at - Duration::hours(24);
    let prior_at = closest_time(
        eligible.iter().map(|row| row.observed_at),
        prior_target,
        Duration::hours(6),
    );
    let liquidity_delta = prior_at.and_then(|prior_at| {
        let prior_focus: Vec<&AaveMarketInput> = eligible
            .iter()
            .copied()
            .filter(|row| {
                row.observed_at == prior_at
                    && row.symbol.as_deref().is_some_and(|symbol| {
                        FOCUS_AAVE_SYMBOLS.contains(&symbol.to_ascii_uppercase().as_str())
                    })
            })
            .collect();
        let prior = sum_option(prior_focus.iter().map(|row| row.available_liquidity_usd))?;
        let current = total_latest?;
        (prior > 0.0).then_some(current / prior - 1.0)
    });
    json!({
        "valid": pressure.is_some(),
        "reason": if pressure.is_some() { "ok" } else { "no_numeric_market_stress_fields" },
        "observed_at": latest_at.to_rfc3339(),
        "market_name": focus.iter().find_map(|row| row.market_name.clone()),
        "focus_reserve_count": focus.len(),
        "focus_available_liquidity_usd": total_latest,
        "available_liquidity_delta_ratio_24h": liquidity_delta,
        "pressure": pressure,
        "aggregation": "max_across_focus_reserves",
        "reserves": reserve_rows,
    })
}

fn stablecoin_component(
    rows: &[StablecoinSystemInput],
    chains: &[StablecoinChainInput],
    as_of: DateTime<Utc>,
    generated: DateTime<Utc>,
    cfg: StateV02Config,
) -> Value {
    let eligible: Vec<&StablecoinSystemInput> = rows
        .iter()
        .filter(|row| row.observed_at <= as_of && row.known_at <= generated)
        .collect();
    let chain_eligible: Vec<&StablecoinChainInput> = chains
        .iter()
        .filter(|row| row.observed_at <= as_of && row.known_at <= generated)
        .collect();
    if eligible.is_empty() || chain_eligible.is_empty() {
        return json!({"valid": false, "reason": "missing_stablecoin_history"});
    }
    let latest_at = eligible.iter().map(|row| row.observed_at).max().unwrap();
    let latest = eligible
        .iter()
        .rev()
        .find(|row| row.observed_at == latest_at)
        .copied()
        .unwrap();
    let coverage = latest.chain_coverage_ratio;
    let residual = latest.chain_abs_residual_ratio;
    if coverage.is_none_or(|value| value < cfg.stablecoin_min_chain_coverage)
        || residual.is_none_or(|value| value > cfg.stablecoin_max_chain_abs_residual_ratio)
    {
        return json!({"valid": false, "reason": "stablecoin_accounting_gate_failed", "observed_at": latest_at.to_rfc3339(), "chain_coverage_ratio": coverage, "chain_abs_residual_ratio": residual});
    }
    let target = latest_at - Duration::hours(cfg.stablecoin_lookback_hours);
    let Some(lag_at) = closest_time(
        eligible.iter().map(|row| row.observed_at),
        target,
        Duration::hours(cfg.stablecoin_lag_tolerance_hours),
    ) else {
        return json!({"valid": false, "reason": "insufficient_lookback_history", "observed_at": latest_at.to_rfc3339(), "required_lookback_hours": cfg.stablecoin_lookback_hours});
    };
    let lag = eligible
        .iter()
        .rev()
        .find(|row| row.observed_at == lag_at)
        .copied()
        .unwrap();
    if !latest.usd_market_value_usd.is_finite()
        || !lag.usd_market_value_usd.is_finite()
        || lag.usd_market_value_usd <= 0.0
    {
        return json!({"valid": false, "reason": "invalid_system_market_value"});
    }
    let mut latest_map = BTreeMap::<String, f64>::new();
    let mut lag_map = BTreeMap::<String, f64>::new();
    for row in &chain_eligible {
        if let Some(value) = row.market_value_usd.filter(|value| value.is_finite()) {
            if row.observed_at == latest_at {
                *latest_map.entry(row.chain.clone()).or_default() += value;
            }
            if row.observed_at == lag_at {
                *lag_map.entry(row.chain.clone()).or_default() += value;
            }
        }
    }
    if latest_map.is_empty() || lag_map.is_empty() {
        return json!({"valid": false, "reason": "missing_chain_rows_at_matched_timestamps"});
    }
    let chain_names: BTreeSet<String> = latest_map.keys().chain(lag_map.keys()).cloned().collect();
    let mut positive = 0.0;
    let mut negative_abs = 0.0;
    let mut moves = Vec::new();
    for chain in chain_names {
        let delta = latest_map.get(&chain).copied().unwrap_or(0.0)
            - lag_map.get(&chain).copied().unwrap_or(0.0);
        if delta > 0.0 {
            positive += delta;
        } else {
            negative_abs += -delta;
        }
        moves.push((chain, delta));
    }
    moves.sort_by(|left, right| {
        right
            .1
            .abs()
            .partial_cmp(&left.1.abs())
            .unwrap_or(Ordering::Equal)
    });
    let migration_proxy = positive.min(negative_abs);
    let net = latest.usd_market_value_usd - lag.usd_market_value_usd;
    let net_ratio = net / lag.usd_market_value_usd;
    let migration_ratio = migration_proxy / lag.usd_market_value_usd;
    let contraction_pressure =
        clip01((-net_ratio).max(0.0) / cfg.stablecoin_contraction_full_stress_ratio);
    let migration_reference =
        clip01(migration_ratio / cfg.stablecoin_migration_full_reference_ratio);
    json!({
        "valid": true,
        "reason": "ok",
        "observed_at": latest_at.to_rfc3339(),
        "lag_observed_at": lag_at.to_rfc3339(),
        "lookback_hours_actual": (latest_at - lag_at).num_seconds() as f64 / 3600.0,
        "system_market_value_usd": latest.usd_market_value_usd,
        "net_system_change_usd": net,
        "net_system_change_ratio": net_ratio,
        "issuance_or_redemption_proxy_usd": net,
        "offsetting_chain_migration_proxy_usd": migration_proxy,
        "migration_ratio": migration_ratio,
        "migration_reference_intensity": migration_reference,
        "pressure": contraction_pressure,
        "pressure_semantics": "system_contraction_only_migration_is_not_stress",
        "chain_coverage_ratio": coverage,
        "chain_abs_residual_ratio": residual,
        "top_chain_moves": moves.into_iter().take(10).map(|(chain, delta)| json!({"chain": chain, "delta_market_value_usd": delta})).collect::<Vec<_>>(),
    })
}

fn basis_component(
    rows: &[BasisInput],
    as_of: DateTime<Utc>,
    generated: DateTime<Utc>,
    cfg: StateV02Config,
) -> Value {
    let eligible: Vec<&BasisInput> = rows
        .iter()
        .filter(|row| row.observed_at <= as_of && row.known_at <= generated)
        .collect();
    let mut chosen = BTreeMap::<String, &BasisInput>::new();
    for asset in ["BTC", "ETH"] {
        let row = eligible
            .iter()
            .copied()
            .filter(|row| row.asset.eq_ignore_ascii_case(asset))
            .max_by(|left, right| {
                left.observed_at
                    .cmp(&right.observed_at)
                    .then(left.known_at.cmp(&right.known_at))
            });
        let Some(row) = row else {
            return json!({"valid": false, "reason": format!("missing_{}_market_state", asset.to_ascii_lowercase())});
        };
        chosen.insert(asset.to_owned(), row);
    }
    let btc = chosen["BTC"];
    let eth = chosen["ETH"];
    let values = json!({"BTC": btc.basis_z_24h, "ETH": eth.basis_z_24h});
    let (Some(btc_z), Some(eth_z)) = (btc.basis_z_24h, eth.basis_z_24h) else {
        return json!({"valid": false, "reason": "missing_basis_z_24h", "basis_z_24h": values});
    };
    let latest = btc.observed_at.max(eth.observed_at);
    if !fresh(latest, generated, cfg.max_source_age_minutes) {
        return json!({"valid": false, "reason": "stale_hyperliquid_market_state"});
    }
    let dispersion = (btc_z - eth_z).abs();
    json!({
        "valid": true,
        "reason": "ok",
        "observed_at": latest.to_rfc3339(),
        "basis_z_24h": values,
        "basis_z_dispersion": dispersion,
        "pressure": clip01(dispersion / cfg.basis_full_stress_z_dispersion),
        "scope": "CROSS_ASSET_HYPERLIQUID_ONLY",
        "multi_venue_claim_allowed": false,
    })
}

fn contagion_component(
    rows: &[CompositionInput],
    as_of: DateTime<Utc>,
    generated: DateTime<Utc>,
    cfg: StateV02Config,
) -> Value {
    let eligible: Vec<&CompositionInput> = rows
        .iter()
        .filter(|row| row.observed_at <= as_of && row.known_at <= generated)
        .collect();
    let Some(latest) = eligible.iter().map(|row| row.observed_at).max() else {
        return json!({"valid": false, "reason": "missing_stablecoin_chain_composition"});
    };
    let latest_rows: Vec<&CompositionInput> = eligible
        .into_iter()
        .filter(|row| row.observed_at == latest)
        .collect();
    let mut stable_totals = BTreeMap::<String, f64>::new();
    let mut chain_totals = BTreeMap::<String, f64>::new();
    for row in &latest_rows {
        let value = row.market_value_usd.unwrap_or(0.0);
        *stable_totals.entry(row.stablecoin_id.clone()).or_default() += value;
        *chain_totals.entry(row.chain.clone()).or_default() += value;
    }
    let valid_stables: BTreeSet<String> = stable_totals
        .iter()
        .filter(|(_, value)| **value >= cfg.contagion_min_stablecoin_market_value_usd)
        .map(|(key, _)| key.clone())
        .collect();
    let valid_chains: BTreeSet<String> = chain_totals
        .iter()
        .filter(|(_, value)| **value >= cfg.contagion_min_chain_market_value_usd)
        .map(|(key, _)| key.clone())
        .collect();
    let filtered: Vec<&CompositionInput> = latest_rows
        .into_iter()
        .filter(|row| {
            valid_stables.contains(&row.stablecoin_id) && valid_chains.contains(&row.chain)
        })
        .collect();
    if filtered.is_empty() || valid_chains.len() < 2 || valid_stables.is_empty() {
        return json!({"valid": false, "reason": "insufficient_graph_after_filters"});
    }
    let stables: Vec<String> = valid_stables.into_iter().collect();
    let chains: Vec<String> = valid_chains.into_iter().collect();
    let stable_index: BTreeMap<String, usize> = stables
        .iter()
        .enumerate()
        .map(|(i, value)| (value.clone(), i))
        .collect();
    let chain_index: BTreeMap<String, usize> = chains
        .iter()
        .enumerate()
        .map(|(i, value)| (value.clone(), i))
        .collect();
    let mut matrix = vec![vec![0.0; stables.len()]; chains.len()];
    for row in filtered {
        if let (Some(&i), Some(&j)) = (
            chain_index.get(&row.chain),
            stable_index.get(&row.stablecoin_id),
        ) {
            matrix[i][j] += row.market_value_usd.unwrap_or(0.0);
        }
    }
    let totals: Vec<f64> = matrix.iter().map(|row| row.iter().sum()).collect();
    let norms: Vec<f64> = matrix
        .iter()
        .map(|row| row.iter().map(|value| value * value).sum::<f64>().sqrt())
        .collect();
    let mut weighted = 0.0;
    let mut weight_sum = 0.0;
    let mut pair_count = 0usize;
    let mut pairs = Vec::new();
    for i in 0..chains.len() {
        for j in i + 1..chains.len() {
            if norms[i] <= 0.0 || norms[j] <= 0.0 {
                continue;
            }
            let dot = matrix[i]
                .iter()
                .zip(&matrix[j])
                .map(|(left, right)| left * right)
                .sum::<f64>();
            let cosine = dot / (norms[i] * norms[j]);
            let pair_weight = (totals[i] * totals[j]).sqrt();
            weighted += cosine * pair_weight;
            weight_sum += pair_weight;
            pair_count += 1;
            pairs.push((cosine * pair_weight, json!({"chain_a": chains[i], "chain_b": chains[j], "cosine_overlap": cosine, "pair_weight": pair_weight})));
        }
    }
    if weight_sum <= 0.0 || pair_count == 0 {
        return json!({"valid": false, "reason": "no_valid_chain_pairs"});
    }
    pairs.sort_by(|left, right| right.0.partial_cmp(&left.0).unwrap_or(Ordering::Equal));
    let connectivity = weighted / weight_sum;
    json!({
        "valid": true,
        "reason": "ok",
        "observed_at": latest.to_rfc3339(),
        "chain_count": chains.len(),
        "stablecoin_count": stables.len(),
        "edge_count": matrix.iter().flatten().filter(|value| **value > 0.0).count(),
        "chain_pair_count": pair_count,
        "weighted_chain_composition_cosine_overlap": connectivity,
        "pressure": clip01(connectivity),
        "top_overlapping_chain_pairs": pairs.into_iter().take(10).map(|(_, row)| row).collect::<Vec<_>>(),
        "interpretation": "potential_common_stablecoin_collateral_channel_not_causal_contagion_proof",
    })
}

fn liquidation_component(
    rows: &[LiquidationInput],
    as_of: DateTime<Utc>,
    generated: DateTime<Utc>,
) -> Value {
    if rows.is_empty() {
        return json!({"valid": false, "reason": "optional_rpc_not_observed_or_no_canonical_files"});
    }
    let mut eligible: Vec<&LiquidationInput> = rows
        .iter()
        .filter(|row| {
            row.known_at <= generated && row.event_time.is_none_or(|event| event <= as_of)
        })
        .collect();
    if eligible.is_empty() {
        return json!({"valid": true, "reason": "observed_no_events_by_as_of", "events_24h": 0, "events_7d": 0});
    }
    eligible.sort_by_key(|row| row.known_at);
    let mut dedup = BTreeMap::<(String, u64), &LiquidationInput>::new();
    let mut no_key = Vec::new();
    for row in eligible {
        if let (Some(tx), Some(log_index)) = (&row.transaction_hash, row.log_index) {
            dedup.insert((tx.clone(), log_index), row);
        } else {
            no_key.push(row);
        }
    }
    let all: Vec<&LiquidationInput> = dedup.into_values().chain(no_key).collect();
    let events_24h = all
        .iter()
        .filter_map(|row| row.event_time)
        .filter(|event| *event >= as_of - Duration::hours(24))
        .count();
    let events_7d = all
        .iter()
        .filter_map(|row| row.event_time)
        .filter(|event| *event >= as_of - Duration::days(7))
        .count();
    json!({
        "valid": true,
        "reason": "ok",
        "events_total_observed": all.len(),
        "events_24h": events_24h,
        "events_7d": events_7d,
        "role": "O0_EVENT_CONFIRMATION_ONLY",
        "pressure": Value::Null,
    })
}

fn deployment_descriptor(aave: &Value, stable: &Value) -> Value {
    let issuance = if stable.get("valid").and_then(Value::as_bool) == Some(true) {
        stable
            .get("net_system_change_ratio")
            .and_then(Value::as_f64)
    } else {
        None
    };
    let aave_delta = if aave.get("valid").and_then(Value::as_bool) == Some(true) {
        aave.get("available_liquidity_delta_ratio_24h")
            .and_then(Value::as_f64)
    } else {
        None
    };
    let valid = issuance.is_some() && aave_delta.is_some();
    json!({
        "valid": valid,
        "external_liquidity_expanding": issuance.map(|value| value > 0.0),
        "aave_available_liquidity_falling": aave_delta.map(|value| value < 0.0),
        "coincident_activation_proxy": issuance.zip(aave_delta).map(|(issuance, delta)| issuance > 0.0 && delta < 0.0),
        "stablecoin_net_change_ratio": issuance,
        "aave_available_liquidity_delta_ratio_24h": aave_delta,
        "actionability": "DESCRIPTION_ONLY_NO_CAUSAL_CLAIM",
    })
}

fn fresh(observed: DateTime<Utc>, generated: DateTime<Utc>, max_age_minutes: i64) -> bool {
    let age = generated - observed;
    age >= Duration::zero() && age <= Duration::minutes(max_age_minutes)
}

fn closest_time<I>(values: I, target: DateTime<Utc>, tolerance: Duration) -> Option<DateTime<Utc>>
where
    I: IntoIterator<Item = DateTime<Utc>>,
{
    values
        .into_iter()
        .min_by_key(|value| (*value - target).num_milliseconds().abs())
        .filter(|value| {
            (*value - target).num_milliseconds().abs() <= tolerance.num_milliseconds().abs()
        })
}

fn clip01(value: f64) -> f64 {
    value.clamp(0.0, 1.0)
}

fn sum_option<I>(values: I) -> Option<f64>
where
    I: IntoIterator<Item = Option<f64>>,
{
    let values: Vec<f64> = values
        .into_iter()
        .flatten()
        .filter(|value| value.is_finite())
        .collect();
    (!values.is_empty()).then(|| values.into_iter().sum())
}

fn value_f64(value: Option<&Value>) -> Option<f64> {
    value.and_then(Value::as_f64)
}
fn value_i64(value: Option<&Value>) -> Option<i64> {
    value.and_then(Value::as_i64)
}
fn value_usize(value: Option<&Value>) -> Option<usize> {
    value
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn missing_components_never_create_actionability() {
        let at = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let value = compute_state_v02(
            &StateV02Inputs {
                aave_markets: vec![],
                stablecoin_system: vec![],
                stablecoin_chain_state: vec![],
                hyperliquid_market_state: vec![],
                stablecoin_chain_composition: vec![],
                aave_liquidations: vec![],
            },
            at,
            at,
            StateV02Config::default(),
        )
        .unwrap();
        assert_eq!(value["data_confidence"], "INSUFFICIENT");
        assert!(value["risk_multiplier"].is_null());
        assert_eq!(value["borrower_health_factor_distribution"]["valid"], false);
    }
}
