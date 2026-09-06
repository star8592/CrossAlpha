use crate::{StateConfigReport, StateRuntimeContext, StateSpec};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

pub const PROTOCOL: &str = "CROSSALPHA_STATE_V0_3";
pub const MODE: &str = "PROSPECTIVE_BORROWER_RISK_SHADOW";
pub const ACTIONABILITY: &str = "DESCRIPTIVE_ONLY";
pub const PROSPECTIVE_PROTOCOL: &str = "CROSSALPHA_STATE_V0_3_PROSPECTIVE";
pub const BLOCKSCOUT_ETHEREUM_API_URL: &str = "https://eth.blockscout.com/api";
pub const BLOCKSCOUT_LOG_SOURCE: &str = "BLOCKSCOUT_INDEXED_LOGS";
pub const BLOCKSCOUT_MAX_LOG_RESULTS: i64 = 1000;
pub const AAVE_V3_ETHEREUM_CORE_POOL: &str = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2";
pub const AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK: i64 = 16_291_127;
pub const BORROW_EVENT_TOPIC0: &str =
    "0xb3d084820fb1a9decffb176436bd02558d15fac9b0ddfed8c465bc7359d7dce0";
pub const GET_USER_ACCOUNT_DATA_SELECTOR: &str = "0xbf92857c";
pub const RPC_BATCH_SIZE: i64 = 100;
pub const BOOTSTRAP_CHUNK_BLOCKS: i64 = 25_000;
pub const MAX_BOOTSTRAP_CHUNKS_PER_CYCLE: i64 = 8;
pub const ADAPTIVE_MINIMUM_SPAN_BLOCKS: i64 = 256;
pub const FINALITY_LAG_BLOCKS: i64 = 64;
pub const FULL_CENSUS_CADENCE_MINUTES: i64 = 360;
pub const WATCHLIST_CADENCE_MINUTES: i64 = 15;
pub const MAXIMUM_FAILED_CALL_RATIO: f64 = 0.01;
pub const WATCHLIST_HEALTH_FACTOR_MAX: f64 = 1.50;
pub const WATCHLIST_DEBT_USD_MIN: f64 = 1_000_000.0;
pub const HF_THRESHOLDS: [f64; 6] = [1.00, 1.02, 1.05, 1.10, 1.20, 1.50];
pub const ZERO_COST_PUBLIC_RPC_URLS: [&str; 4] = [
    "https://eth.blockscout.com/api/eth-rpc",
    "https://ethereum-rpc.blockreq.com/v1/rpc/public",
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
];

#[derive(Debug, Clone, Copy, Default)]
pub struct StateV03;

impl StateV03 {
    pub fn strict_config_report(path: &Path) -> Result<StateConfigReport> {
        let text = fs::read_to_string(path)
            .with_context(|| format!("read State V0.3 config {}", path.display()))?;
        let yaml: serde_yaml::Value = serde_yaml::from_str(&text)
            .with_context(|| format!("parse State V0.3 config {}", path.display()))?;
        let raw = serde_json::to_value(yaml)?;
        let mut checks = BTreeMap::new();

        check(&mut checks, "protocol", string_at(&raw, "/protocol") == Some(PROTOCOL));
        check(&mut checks, "mode", string_at(&raw, "/mode") == Some(MODE));
        check(
            &mut checks,
            "actionability",
            string_at(&raw, "/actionability") == Some(ACTIONABILITY),
        );
        check(
            &mut checks,
            "risk_multiplier_null",
            raw.pointer("/risk_multiplier").is_some_and(Value::is_null),
        );
        check(
            &mut checks,
            "no_core_mutation",
            bool_at(&raw, "/research_policy/mutates_frozen_core") == Some(false),
        );
        check(
            &mut checks,
            "no_v01_mutation",
            bool_at(&raw, "/research_policy/mutates_state_v01") == Some(false),
        );
        check(
            &mut checks,
            "no_ab_mutation",
            bool_at(&raw, "/research_policy/mutates_state_ab_v01") == Some(false),
        );
        check(
            &mut checks,
            "no_v02_mutation",
            bool_at(&raw, "/research_policy/mutates_state_v02") == Some(false),
        );
        check(
            &mut checks,
            "no_parameter_optimization",
            bool_at(&raw, "/research_policy/parameter_optimization_allowed") == Some(false),
        );
        check(
            &mut checks,
            "no_backfill",
            bool_at(
                &raw,
                "/research_policy/retrospective_prospective_backfill_allowed",
            ) == Some(false),
        );
        check(
            &mut checks,
            "no_auto_actionability",
            bool_at(&raw, "/research_policy/automatic_actionability_allowed") == Some(false),
        );
        check(
            &mut checks,
            "historical_bootstrap_not_evidence",
            bool_at(&raw, "/research_policy/historical_bootstrap_is_evidence") == Some(false),
        );
        check(
            &mut checks,
            "split_data_plane",
            bool_at(&raw, "/source/split_data_plane") == Some(true),
        );
        check(
            &mut checks,
            "archive_rpc_not_required",
            bool_at(&raw, "/source/archive_rpc_required") == Some(false),
        );
        check(
            &mut checks,
            "borrow_log_source",
            string_at(&raw, "/source/borrower_universe_log_source") == Some(BLOCKSCOUT_LOG_SOURCE),
        );
        check(
            &mut checks,
            "borrow_log_url",
            string_at(&raw, "/source/borrower_universe_log_api_url")
                == Some(BLOCKSCOUT_ETHEREUM_API_URL),
        );
        check(
            &mut checks,
            "borrow_log_no_auth",
            bool_at(&raw, "/source/borrower_universe_log_auth_required") == Some(false),
        );
        check(
            &mut checks,
            "borrow_log_result_limit",
            integer_at(&raw, "/source/borrower_universe_log_max_results")
                == Some(BLOCKSCOUT_MAX_LOG_RESULTS),
        );
        check(
            &mut checks,
            "borrow_log_split_policy",
            string_at(&raw, "/source/borrower_universe_log_limit_policy")
                == Some("RECURSIVE_BLOCK_RANGE_SPLIT_TO_SINGLE_BLOCK"),
        );
        check(
            &mut checks,
            "state_rpc_env",
            string_at(&raw, "/source/state_rpc_env") == Some("EVM_RPC_URL"),
        );
        check(
            &mut checks,
            "state_rpc_fallback_pool",
            string_array_at(&raw, "/source/state_rpc_fallback_candidates")
                == Some(ZERO_COST_PUBLIC_RPC_URLS.iter().map(|value| (*value).to_owned()).collect()),
        );
        check(
            &mut checks,
            "zero_cost",
            number_at(&raw, "/source/required_data_cost_usd") == Some(0.0),
        );
        check(
            &mut checks,
            "state_rpc_policy",
            string_at(&raw, "/source/state_rpc_policy")
                == Some("EVM_RPC_URL_PREFERRED_FINALIZED_STATE_ZERO_COST_FALLBACK_POOL"),
        );
        check(
            &mut checks,
            "state_rpc_required_capabilities",
            string_array_at(&raw, "/source/state_rpc_required_capabilities")
                == Some(vec![
                    "eth_blockNumber".to_owned(),
                    "eth_getBlockByNumber".to_owned(),
                    "fixed_block_eth_call".to_owned(),
                ]),
        );
        check(
            &mut checks,
            "single_cycle_state_rpc_selection",
            bool_at(&raw, "/source/single_cycle_state_rpc_selection") == Some(true),
        );
        check(
            &mut checks,
            "configured_rpc_may_fallback",
            bool_at(&raw, "/source/configured_rpc_failure_may_fallback") == Some(true),
        );
        check(
            &mut checks,
            "rpc_failure_diagnostics_redacted",
            bool_at(
                &raw,
                "/source/rpc_failure_diagnostics_must_not_expose_url_or_token",
            ) == Some(true),
        );
        check(
            &mut checks,
            "pool_address",
            string_at(&raw, "/source/aave_v3_core_pool")
                .is_some_and(|value| value.eq_ignore_ascii_case(AAVE_V3_ETHEREUM_CORE_POOL)),
        );
        check(
            &mut checks,
            "deployment_block",
            integer_at(&raw, "/source/deployment_block") == Some(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK),
        );
        check(
            &mut checks,
            "borrow_topic",
            string_at(&raw, "/source/borrow_event_topic0")
                .is_some_and(|value| value.eq_ignore_ascii_case(BORROW_EVENT_TOPIC0)),
        );
        check(
            &mut checks,
            "account_selector",
            string_at(&raw, "/source/get_user_account_data_selector")
                .is_some_and(|value| value.eq_ignore_ascii_case(GET_USER_ACCOUNT_DATA_SELECTOR)),
        );
        check(
            &mut checks,
            "block_time_source",
            string_at(&raw, "/source/block_time_source")
                == Some("eth_getBlockByNumber(finalized_block)"),
        );
        check(
            &mut checks,
            "base_decimals",
            integer_at(&raw, "/source/ethereum_core_base_currency_decimals") == Some(8),
        );
        check(
            &mut checks,
            "bootstrap_start",
            integer_at(&raw, "/borrower_universe/bootstrap_start_block")
                == Some(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK),
        );
        check(
            &mut checks,
            "bootstrap_chunk",
            integer_at(&raw, "/borrower_universe/bootstrap_chunk_blocks")
                == Some(BOOTSTRAP_CHUNK_BLOCKS),
        );
        check(
            &mut checks,
            "bootstrap_chunks_per_cycle",
            integer_at(&raw, "/borrower_universe/max_bootstrap_chunks_per_cycle")
                == Some(MAX_BOOTSTRAP_CHUNKS_PER_CYCLE),
        );
        check(
            &mut checks,
            "adaptive_minimum_span",
            integer_at(&raw, "/borrower_universe/adaptive_minimum_span_blocks")
                == Some(ADAPTIVE_MINIMUM_SPAN_BLOCKS),
        );
        check(
            &mut checks,
            "finality_lag",
            integer_at(&raw, "/borrower_universe/finality_lag_blocks")
                == Some(FINALITY_LAG_BLOCKS),
        );
        check(
            &mut checks,
            "candidate_identity",
            string_at(&raw, "/borrower_universe/identity") == Some("Borrow.onBehalfOf"),
        );
        check(
            &mut checks,
            "reorg_candidate_policy",
            string_at(&raw, "/borrower_universe/reorg_false_positive_policy")
                == Some("retain_candidate_then_filter_by_current_debt"),
        );
        check(
            &mut checks,
            "current_active_definition",
            string_at(&raw, "/borrower_universe/current_active_definition")
                == Some("total_debt_base_gt_0"),
        );
        check(
            &mut checks,
            "batch_size",
            integer_at(&raw, "/census/rpc_batch_size") == Some(RPC_BATCH_SIZE),
        );
        check(
            &mut checks,
            "failed_call_ratio",
            number_at(&raw, "/census/maximum_failed_call_ratio_for_valid_census")
                == Some(MAXIMUM_FAILED_CALL_RATIO),
        );
        check(
            &mut checks,
            "full_census_cadence",
            integer_at(&raw, "/census/full_census_cadence_minutes")
                == Some(FULL_CENSUS_CADENCE_MINUTES),
        );
        check(
            &mut checks,
            "full_census_requires_new_block",
            bool_at(&raw, "/census/full_census_requires_new_finalized_block") == Some(true),
        );
        check(
            &mut checks,
            "watchlist_cadence",
            integer_at(&raw, "/census/watchlist_cadence_minutes")
                == Some(WATCHLIST_CADENCE_MINUTES),
        );
        check(
            &mut checks,
            "watchlist_hf",
            number_at(&raw, "/census/watchlist_health_factor_max")
                == Some(WATCHLIST_HEALTH_FACTOR_MAX),
        );
        check(
            &mut checks,
            "watchlist_debt",
            number_at(&raw, "/census/watchlist_debt_usd_min") == Some(WATCHLIST_DEBT_USD_MIN),
        );
        check(
            &mut checks,
            "new_borrower_inter_census_policy",
            string_at(&raw, "/census/new_borrower_between_full_censuses_policy")
                == Some("include_in_temporary_watchlist_until_next_valid_full_census"),
        );
        check(
            &mut checks,
            "hf_scale_decimals",
            integer_at(&raw, "/census/health_factor_scale_decimals") == Some(18),
        );
        check(
            &mut checks,
            "hf_thresholds",
            number_array_at(&raw, "/census/thresholds") == Some(HF_THRESHOLDS.to_vec()),
        );
        check(
            &mut checks,
            "hf_bands",
            hf_bands_match(&raw),
        );
        check(
            &mut checks,
            "no_liquidation_price_claim",
            bool_at(
                &raw,
                "/liquidation_cliff/no_single_asset_liquidation_price_claim",
            ) == Some(true),
        );
        check(
            &mut checks,
            "prospective_gate",
            raw.pointer("/prospective_gate") == Some(&expected_prospective_gate()),
        );
        check(
            &mut checks,
            "prospective_protocol_name",
            PROSPECTIVE_PROTOCOL == "CROSSALPHA_STATE_V0_3_PROSPECTIVE",
        );

        let ok = checks.values().all(|value| *value);
        Ok(StateConfigReport {
            protocol: PROTOCOL.to_owned(),
            audit_level: "STRICT_CONFIG_IMPLEMENTATION_MATCH".to_owned(),
            ok,
            checks,
        })
    }
}

#[async_trait]
impl StateSpec for StateV03 {
    fn version(&self) -> &'static str {
        "v03"
    }

    fn protocol(&self) -> &'static str {
        PROTOCOL
    }

    fn validate_config(&self, path: &Path) -> Result<StateConfigReport> {
        Self::strict_config_report(path)
    }

    async fn preflight(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("State V0.3 Rust network preflight is not enabled until config parity passes")
    }

    async fn freeze(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("State V0.3 Rust freeze is not enabled until preflight parity passes")
    }

    async fn cycle(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("State V0.3 Rust cycle is not enabled until freeze parity passes")
    }

    fn integrity(&self, _data_root: &Path) -> Result<Value> {
        Ok(json!({"protocol": PROTOCOL, "implemented": false, "phase": "R4_CONFIG_PARITY"}))
    }

    fn status(&self, _data_root: &Path) -> Result<Value> {
        Ok(json!({"protocol": PROTOCOL, "implemented": false, "phase": "R4_CONFIG_PARITY"}))
    }
}

fn check(checks: &mut BTreeMap<String, bool>, name: &str, value: bool) {
    checks.insert(name.to_owned(), value);
}

fn string_at<'a>(value: &'a Value, pointer: &str) -> Option<&'a str> {
    value.pointer(pointer)?.as_str()
}

fn bool_at(value: &Value, pointer: &str) -> Option<bool> {
    value.pointer(pointer)?.as_bool()
}

fn integer_at(value: &Value, pointer: &str) -> Option<i64> {
    value.pointer(pointer)?.as_i64()
}

fn number_at(value: &Value, pointer: &str) -> Option<f64> {
    value.pointer(pointer)?.as_f64()
}

fn string_array_at(value: &Value, pointer: &str) -> Option<Vec<String>> {
    value
        .pointer(pointer)?
        .as_array()?
        .iter()
        .map(|item| item.as_str().map(str::to_owned))
        .collect()
}

fn number_array_at(value: &Value, pointer: &str) -> Option<Vec<f64>> {
    value
        .pointer(pointer)?
        .as_array()?
        .iter()
        .map(Value::as_f64)
        .collect()
}

fn hf_bands_match(raw: &Value) -> bool {
    let Some(bands) = raw.pointer("/liquidation_cliff/hf_bands").and_then(Value::as_array) else {
        return false;
    };
    let expected: [(f64, Option<f64>); 7] = [
        (0.00, Some(1.00)),
        (1.00, Some(1.02)),
        (1.02, Some(1.05)),
        (1.05, Some(1.10)),
        (1.10, Some(1.20)),
        (1.20, Some(1.50)),
        (1.50, None),
    ];
    if bands.len() != expected.len() {
        return false;
    }
    bands.iter().zip(expected).all(|(band, (low, high))| {
        let Some(values) = band.as_array() else {
            return false;
        };
        if values.len() != 2 || values[0].as_f64() != Some(low) {
            return false;
        }
        match high {
            Some(high) => values[1].as_f64() == Some(high),
            None => values[1].is_null(),
        }
    })
}

fn expected_prospective_gate() -> Value {
    json!({
        "minimum_calendar_days_before_O2_candidate": 180,
        "minimum_valid_full_censuses": 120,
        "minimum_distinct_cliff_stress_episodes": 5,
        "cliff_episode_critical_debt_share_threshold": 0.05,
        "cliff_episode_cooldown_hours": 24,
        "requires_complete_borrower_bootstrap": true,
        "requires_outcome_linkage_test": true,
        "requires_predeclared_O2_rule": true,
        "automatic_promotion_to_actionable_modifier_allowed": false
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_match_frozen_v03_contract() {
        assert_eq!(FINALITY_LAG_BLOCKS, 64);
        assert_eq!(BOOTSTRAP_CHUNK_BLOCKS, 25_000);
        assert_eq!(RPC_BATCH_SIZE, 100);
        assert_eq!(HF_THRESHOLDS, [1.0, 1.02, 1.05, 1.10, 1.20, 1.50]);
        assert_eq!(ZERO_COST_PUBLIC_RPC_URLS.len(), 4);
    }

    #[test]
    fn prospective_gate_is_frozen() {
        let gate = expected_prospective_gate();
        assert_eq!(gate["minimum_calendar_days_before_O2_candidate"], 180);
        assert_eq!(gate["automatic_promotion_to_actionable_modifier_allowed"], false);
    }
}
