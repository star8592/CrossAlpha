use crate::{StateConfigReport, StateRuntimeContext, StateSpec};
use anyhow::{Context, Result, bail};
use async_trait::async_trait;
use chrono::{DateTime, Duration, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};

pub const PROTOCOL: &str = "CROSSALPHA_STATE_V0_4";
pub const MODE: &str = "PROSPECTIVE_MULTI_VENUE_MARKET_MECHANICS_SHADOW";
pub const ACTIONABILITY: &str = "DESCRIPTIVE_ONLY";
pub const ASSETS: [&str; 2] = ["BTC", "ETH"];
pub const VENUES: [&str; 3] = ["binance", "okx", "bybit"];
pub const MINIMUM_VALID_VENUES: usize = 2;
pub const FULL_CONFIDENCE_VENUES: usize = 3;
pub const MAXIMUM_SNAPSHOT_AGE_SECONDS: i64 = 90;
pub const FUNDING_SEMANTICS: &str = "LATEST_SETTLED_NORMALIZED_TO_8H";

#[derive(Debug, Clone, Copy, Default)]
pub struct NativeStateV04;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NormalizedVenueRow {
    pub protocol: String,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub venue: String,
    pub asset: String,
    pub spot_symbol: Option<String>,
    pub perp_symbol: Option<String>,
    pub spot_bid: Option<f64>,
    pub spot_ask: Option<f64>,
    pub spot_mid: Option<f64>,
    pub spot_spread_bps: Option<f64>,
    pub perp_bid: Option<f64>,
    pub perp_ask: Option<f64>,
    pub perp_mid: Option<f64>,
    pub perp_spread_bps: Option<f64>,
    pub mark_price: Option<f64>,
    pub index_price: Option<f64>,
    pub basis_bps: Option<f64>,
    pub mark_index_basis_bps: Option<f64>,
    pub funding_semantics: String,
    pub funding_rate_settled_raw: Option<f64>,
    pub funding_settlement_time: Option<String>,
    pub funding_interval_hours: Option<f64>,
    pub funding_rate_8h: Option<f64>,
    pub open_interest_usd: Option<f64>,
    pub data_cost_usd: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub collection_error: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw_compressed_file_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw_path: Option<String>,
}

impl NativeStateV04 {
    pub fn strict_config_report(path: &Path) -> Result<StateConfigReport> {
        let text = fs::read_to_string(path)
            .with_context(|| format!("read State V0.4 config {}", path.display()))?;
        let yaml: serde_yaml::Value = serde_yaml::from_str(&text)?;
        let raw = serde_json::to_value(yaml)?;
        let mut checks = BTreeMap::new();
        let safe_provider = repo_root().join("src/crossalpha/state/v04_safe_provider.py");
        let safe_hash = safe_provider
            .is_file()
            .then(|| sha256_file(&safe_provider))
            .transpose()?;

        check(
            &mut checks,
            "protocol",
            string_at(&raw, "/protocol") == Some(PROTOCOL),
        );
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
            "no_predecessor_mutation",
            bool_at(&raw, "/research_policy/mutates_predecessors") == Some(false),
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
            "historical_not_evidence",
            bool_at(&raw, "/research_policy/historical_backfill_is_evidence") == Some(false),
        );
        check(
            &mut checks,
            "zero_cost",
            number_at(&raw, "/research_policy/required_data_cost_usd") == Some(0.0),
        );
        check(
            &mut checks,
            "fault_isolation_module_hash",
            safe_hash.as_deref()
                == string_at(&raw, "/research_policy/fault_isolation_module_sha256"),
        );
        check(
            &mut checks,
            "assets",
            string_array_at(&raw, "/universe/assets")
                == Some(ASSETS.iter().map(|value| (*value).to_owned()).collect()),
        );
        check(
            &mut checks,
            "venues",
            string_array_at(&raw, "/universe/venues")
                == Some(VENUES.iter().map(|value| (*value).to_owned()).collect()),
        );
        check(
            &mut checks,
            "minimum_venues",
            integer_at(&raw, "/universe/minimum_valid_venues") == Some(MINIMUM_VALID_VENUES as i64),
        );
        check(
            &mut checks,
            "full_venues",
            integer_at(&raw, "/universe/full_confidence_venues")
                == Some(FULL_CONFIDENCE_VENUES as i64),
        );
        check(
            &mut checks,
            "cadence_minutes",
            integer_at(&raw, "/cadence/observation_minutes") == Some(5),
        );
        check(
            &mut checks,
            "max_age",
            integer_at(&raw, "/cadence/maximum_snapshot_age_seconds")
                == Some(MAXIMUM_SNAPSHOT_AGE_SECONDS),
        );
        check(
            &mut checks,
            "funding_semantics",
            string_at(&raw, "/normalization/funding_semantics") == Some(FUNDING_SEMANTICS),
        );
        check(
            &mut checks,
            "funding_period",
            integer_at(&raw, "/normalization/funding_comparison_period_hours") == Some(8),
        );
        check(
            &mut checks,
            "funding_interval_source",
            string_at(&raw, "/normalization/funding_interval_source")
                == Some("difference_between_latest_two_settlement_timestamps"),
        );
        check(
            &mut checks,
            "funding_unknown_policy",
            string_at(&raw, "/normalization/funding_unknown_interval_policy")
                == Some("exclude_from_cross_venue_funding_dispersion"),
        );
        check(
            &mut checks,
            "oi_unit",
            string_at(&raw, "/normalization/open_interest_unit") == Some("USD_NOTIONAL"),
        );
        check(
            &mut checks,
            "binance_funding_endpoint",
            string_at(&raw, "/venues/binance/settled_funding_history")
                == Some("/fapi/v1/fundingRate"),
        );
        check(
            &mut checks,
            "binance_funding_fields",
            string_at(&raw, "/venues/binance/settled_rate_field") == Some("fundingRate")
                && string_at(&raw, "/venues/binance/settlement_time_field") == Some("fundingTime"),
        );
        check(
            &mut checks,
            "okx_funding_endpoint",
            string_at(&raw, "/venues/okx/settled_funding_history")
                == Some("/api/v5/public/funding-rate-history"),
        );
        check(
            &mut checks,
            "okx_funding_fields",
            string_at(&raw, "/venues/okx/settled_rate_field") == Some("realizedRate")
                && string_at(&raw, "/venues/okx/settlement_time_field") == Some("fundingTime"),
        );
        check(
            &mut checks,
            "bybit_funding_endpoint",
            string_at(&raw, "/venues/bybit/settled_funding_history")
                == Some("/v5/market/funding/history"),
        );
        check(
            &mut checks,
            "bybit_funding_fields",
            string_at(&raw, "/venues/bybit/settled_rate_field") == Some("fundingRate")
                && string_at(&raw, "/venues/bybit/settlement_time_field")
                    == Some("fundingRateTimestamp"),
        );
        check(
            &mut checks,
            "no_composite",
            bool_at(&raw, "/features/no_composite_stress_score") == Some(true),
        );
        check(
            &mut checks,
            "prospective_gate",
            raw.pointer("/prospective_gate") == Some(&expected_gate()),
        );
        let ok = checks.values().all(|value| *value);
        Ok(StateConfigReport {
            protocol: PROTOCOL.to_owned(),
            audit_level: "STRICT_CONFIG_IMPLEMENTATION_MATCH_WITH_FAULT_ISOLATION_HASH".to_owned(),
            ok,
            checks,
        })
    }
}

pub fn compute_market_mechanics(
    rows: &[NormalizedVenueRow],
    generated_at: DateTime<Utc>,
    maximum_age_seconds: i64,
) -> Value {
    let mut latest: BTreeMap<(String, String), &NormalizedVenueRow> = BTreeMap::new();
    let mut eligible_before_age = false;
    for row in rows {
        let venue = row.venue.to_ascii_lowercase();
        let asset = row.asset.to_ascii_uppercase();
        if !VENUES.contains(&venue.as_str()) || !ASSETS.contains(&asset.as_str()) {
            continue;
        }
        if row.known_at > generated_at || row.observed_at > generated_at {
            continue;
        }
        eligible_before_age = true;
        let age = generated_at - row.observed_at;
        if age < Duration::zero() || age > Duration::seconds(maximum_age_seconds) {
            continue;
        }
        let key = (asset, venue);
        let replace = latest.get(&key).is_none_or(|previous| {
            row.observed_at > previous.observed_at
                || (row.observed_at == previous.observed_at && row.known_at > previous.known_at)
        });
        if replace {
            latest.insert(key, row);
        }
    }
    if latest.is_empty() && !eligible_before_age {
        return json!({
            "protocol": PROTOCOL,
            "mode": MODE,
            "actionability": ACTIONABILITY,
            "risk_multiplier": Value::Null,
            "generated_at": generated_at.to_rfc3339_opts(SecondsFormat::Micros, false),
            "data_confidence": "INSUFFICIENT",
            "assets": {},
            "funding_semantics": FUNDING_SEMANTICS,
            "no_composite_stress_score": true,
        });
    }

    let mut asset_reports = Map::new();
    let mut valid_counts = Vec::new();
    for asset in ASSETS {
        let complete: Vec<&NormalizedVenueRow> = VENUES
            .iter()
            .filter_map(|venue| {
                latest
                    .get(&(asset.to_owned(), (*venue).to_owned()))
                    .copied()
            })
            .filter(|row| {
                row.spot_mid.is_some_and(|value| value > 0.0)
                    && row.perp_mid.is_some_and(|value| value > 0.0)
                    && row.basis_bps.is_some()
            })
            .collect();
        let valid_venues: Vec<String> = complete.iter().map(|row| row.venue.clone()).collect();
        let valid_count = valid_venues.len();
        valid_counts.push(valid_count);
        let confidence = confidence(valid_count);
        let comparable_funding: Vec<&NormalizedVenueRow> = complete
            .iter()
            .copied()
            .filter(|row| {
                row.funding_semantics == FUNDING_SEMANTICS
                    && row.funding_interval_hours.is_some_and(|value| value > 0.0)
                    && row.funding_rate_8h.is_some()
            })
            .collect();
        let oi_values: Vec<f64> = complete
            .iter()
            .filter_map(|row| row.open_interest_usd)
            .collect();
        let total_oi = (!oi_values.is_empty()).then(|| oi_values.iter().sum::<f64>());
        let mut venues = Map::new();
        for row in &complete {
            venues.insert(
                row.venue.clone(),
                json!({
                    "observed_at": row.observed_at.to_rfc3339_opts(SecondsFormat::Micros, false),
                    "spot_mid": row.spot_mid,
                    "perp_mid": row.perp_mid,
                    "basis_bps": row.basis_bps,
                    "funding_semantics": row.funding_semantics,
                    "funding_rate_settled_raw": row.funding_rate_settled_raw,
                    "funding_settlement_time": row.funding_settlement_time,
                    "funding_interval_hours": row.funding_interval_hours,
                    "funding_rate_8h": row.funding_rate_8h,
                    "perp_spread_bps": row.perp_spread_bps,
                    "open_interest_usd": row.open_interest_usd,
                }),
            );
        }
        asset_reports.insert(
            asset.to_owned(),
            json!({
                "data_confidence": confidence,
                "valid_venue_count": valid_count,
                "valid_venues": valid_venues,
                "funding_comparable_venue_count": comparable_funding.len(),
                "spot_cross_venue_range_bps": range_bps(complete.iter().filter_map(|row| row.spot_mid).collect()),
                "basis_median_bps": median(complete.iter().filter_map(|row| row.basis_bps).collect()),
                "basis_range_bps": range(complete.iter().filter_map(|row| row.basis_bps).collect()),
                "basis_std_bps": population_std(complete.iter().filter_map(|row| row.basis_bps).collect()),
                "funding_8h_median": median(comparable_funding.iter().filter_map(|row| row.funding_rate_8h).collect()),
                "funding_8h_range": range(comparable_funding.iter().filter_map(|row| row.funding_rate_8h).collect()),
                "perp_spread_median_bps": median(complete.iter().filter_map(|row| row.perp_spread_bps).collect()),
                "perp_spread_max_bps": max_value(complete.iter().filter_map(|row| row.perp_spread_bps).collect()),
                "total_open_interest_usd": total_oi,
                "open_interest_hhi": hhi(oi_values),
                "venues": venues,
            }),
        );
    }
    let minimum = valid_counts.into_iter().min().unwrap_or(0);
    json!({
        "protocol": PROTOCOL,
        "mode": MODE,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "mutates_frozen_core": false,
        "mutates_state_v01": false,
        "mutates_state_ab_v01": false,
        "mutates_state_v02": false,
        "mutates_state_v03": false,
        "generated_at": generated_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "data_confidence": confidence(minimum),
        "assets": asset_reports,
        "funding_semantics": FUNDING_SEMANTICS,
        "no_composite_stress_score": true,
        "interpretation": "Descriptive cross-venue market-mechanics vector only. Funding uses the most recent settled rate normalized by the observed settlement interval. Basis, funding, spread, spot dislocation and OI concentration are not trading signals in V0.4."
    })
}

#[async_trait]
impl StateSpec for NativeStateV04 {
    fn version(&self) -> &'static str {
        "v04"
    }

    fn protocol(&self) -> &'static str {
        PROTOCOL
    }

    fn validate_config(&self, path: &Path) -> Result<StateConfigReport> {
        Self::strict_config_report(path)
    }

    async fn preflight(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("Native State V0.4 provider preflight is not yet attached")
    }

    async fn freeze(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("Native State V0.4 freeze is not yet attached")
    }

    async fn cycle(&self, _context: &StateRuntimeContext) -> Result<Value> {
        bail!("Native State V0.4 cycle is not yet attached")
    }

    fn integrity(&self, _data_root: &Path) -> Result<Value> {
        Ok(json!({"protocol": PROTOCOL, "implemented": false, "phase": "R4_V04_CORE"}))
    }

    fn status(&self, data_root: &Path) -> Result<Value> {
        self.integrity(data_root)
    }
}

fn confidence(valid_count: usize) -> &'static str {
    if valid_count >= FULL_CONFIDENCE_VENUES {
        "FULL"
    } else if valid_count >= MINIMUM_VALID_VENUES {
        "PARTIAL"
    } else {
        "INSUFFICIENT"
    }
}

fn median(mut values: Vec<f64>) -> Option<f64> {
    values.retain(|value| value.is_finite());
    if values.is_empty() {
        return None;
    }
    values.sort_by(|left, right| left.partial_cmp(right).unwrap_or(Ordering::Equal));
    let middle = values.len() / 2;
    if values.len().is_multiple_of(2) {
        Some((values[middle - 1] + values[middle]) / 2.0)
    } else {
        Some(values[middle])
    }
}

fn range(values: Vec<f64>) -> Option<f64> {
    let values: Vec<f64> = values
        .into_iter()
        .filter(|value| value.is_finite())
        .collect();
    if values.len() < 2 {
        return None;
    }
    let min = values.iter().copied().reduce(f64::min)?;
    let max = values.iter().copied().reduce(f64::max)?;
    Some(max - min)
}

fn range_bps(values: Vec<f64>) -> Option<f64> {
    let values: Vec<f64> = values
        .into_iter()
        .filter(|value| value.is_finite() && *value > 0.0)
        .collect();
    if values.len() < 2 {
        return None;
    }
    let midpoint = median(values.clone())?;
    if midpoint <= 0.0 {
        return None;
    }
    let min = values.iter().copied().reduce(f64::min)?;
    let max = values.iter().copied().reduce(f64::max)?;
    Some((max - min) / midpoint * 10_000.0)
}

fn population_std(values: Vec<f64>) -> Option<f64> {
    let values: Vec<f64> = values
        .into_iter()
        .filter(|value| value.is_finite())
        .collect();
    if values.len() < 2 {
        return None;
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    Some(
        (values
            .iter()
            .map(|value| {
                let delta = *value - mean;
                delta * delta
            })
            .sum::<f64>()
            / values.len() as f64)
            .sqrt(),
    )
}

fn max_value(values: Vec<f64>) -> Option<f64> {
    values
        .into_iter()
        .filter(|value| value.is_finite())
        .reduce(f64::max)
}

fn hhi(values: Vec<f64>) -> Option<f64> {
    let values: Vec<f64> = values
        .into_iter()
        .filter(|value| value.is_finite() && *value > 0.0)
        .collect();
    if values.len() < 2 {
        return None;
    }
    let total = values.iter().sum::<f64>();
    if total <= 0.0 {
        return None;
    }
    Some(
        values
            .iter()
            .map(|value| {
                let share = *value / total;
                share * share
            })
            .sum(),
    )
}

fn expected_gate() -> Value {
    json!({
        "minimum_calendar_days_before_O2_candidate": 180,
        "minimum_observations": 500,
        "minimum_valid_venue_share": 0.95,
        "requires_outcome_linkage_test": true,
        "requires_predeclared_O2_rule": true,
        "automatic_promotion_to_actionable_modifier_allowed": false,
    })
}

fn check(checks: &mut BTreeMap<String, bool>, name: &str, value: bool) {
    checks.insert(name.to_owned(), value);
}

fn string_at<'a>(value: &'a Value, pointer: &str) -> Option<&'a str> {
    value.pointer(pointer).and_then(Value::as_str)
}

fn bool_at(value: &Value, pointer: &str) -> Option<bool> {
    value.pointer(pointer).and_then(Value::as_bool)
}

fn integer_at(value: &Value, pointer: &str) -> Option<i64> {
    value.pointer(pointer).and_then(Value::as_i64)
}

fn number_at(value: &Value, pointer: &str) -> Option<f64> {
    value.pointer(pointer).and_then(Value::as_f64)
}

fn string_array_at(value: &Value, pointer: &str) -> Option<Vec<String>> {
    value
        .pointer(pointer)?
        .as_array()?
        .iter()
        .map(|item| item.as_str().map(str::to_owned))
        .collect()
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn mechanics_never_emits_composite_stress_score() {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let row = NormalizedVenueRow {
            protocol: PROTOCOL.to_owned(),
            observed_at: now,
            known_at: now,
            venue: "binance".to_owned(),
            asset: "BTC".to_owned(),
            spot_symbol: Some("BTCUSDT".to_owned()),
            perp_symbol: Some("BTCUSDT".to_owned()),
            spot_bid: Some(99.0),
            spot_ask: Some(101.0),
            spot_mid: Some(100.0),
            spot_spread_bps: Some(200.0),
            perp_bid: Some(100.0),
            perp_ask: Some(102.0),
            perp_mid: Some(101.0),
            perp_spread_bps: Some(198.0),
            mark_price: Some(101.0),
            index_price: Some(100.0),
            basis_bps: Some(100.0),
            mark_index_basis_bps: Some(100.0),
            funding_semantics: FUNDING_SEMANTICS.to_owned(),
            funding_rate_settled_raw: Some(0.0001),
            funding_settlement_time: Some(now.to_rfc3339()),
            funding_interval_hours: Some(8.0),
            funding_rate_8h: Some(0.0001),
            open_interest_usd: Some(1_000_000.0),
            data_cost_usd: 0,
            collection_error: None,
            raw_sha256: None,
            raw_compressed_file_sha256: None,
            raw_path: None,
        };
        let report = compute_market_mechanics(&[row], now, 90);
        assert_eq!(report["no_composite_stress_score"], true);
        assert!(report.get("composite_stress_score").is_none());
    }
}
