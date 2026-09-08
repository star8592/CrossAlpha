use crate::v03::{ACTIONABILITY, HF_THRESHOLDS, MODE, PROTOCOL};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use num_bigint::BigUint;
use num_traits::ToPrimitive;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::HashSet;

const BASE_CURRENCY_SCALE: f64 = 100_000_000.0;
const HEALTH_FACTOR_SCALE: f64 = 1_000_000_000_000_000_000.0;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AccountMetrics {
    pub total_collateral_usd: f64,
    pub total_debt_usd: f64,
    pub available_borrows_usd: f64,
    pub current_liquidation_threshold_pct: f64,
    pub ltv_pct: f64,
    pub health_factor: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AccountDataRow {
    pub address: String,
    pub success: bool,
    pub total_collateral_usd: Option<f64>,
    pub total_debt_usd: Option<f64>,
    pub available_borrows_usd: Option<f64>,
    pub current_liquidation_threshold_pct: Option<f64>,
    pub ltv_pct: Option<f64>,
    pub health_factor: Option<f64>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub struct CensusPolicy {
    pub maximum_failed_call_ratio: f64,
    pub watchlist_health_factor_max: f64,
    pub watchlist_debt_usd_min: f64,
}

impl Default for CensusPolicy {
    fn default() -> Self {
        Self {
            maximum_failed_call_ratio: 0.01,
            watchlist_health_factor_max: 1.50,
            watchlist_debt_usd_min: 1_000_000.0,
        }
    }
}

pub fn normalize_address(address: &str) -> Result<String> {
    let raw = address
        .strip_prefix("0x")
        .or_else(|| address.strip_prefix("0X"))
        .context("Ethereum address lacks 0x prefix")?;
    if raw.len() != 40 || !raw.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        bail!("Ethereum address must contain exactly 20 hex bytes");
    }
    Ok(format!("0x{}", raw.to_ascii_lowercase()))
}

pub fn encode_get_user_account_data(address: &str, selector: &str) -> Result<String> {
    let address = normalize_address(address)?;
    let selector = selector
        .strip_prefix("0x")
        .or_else(|| selector.strip_prefix("0X"))
        .unwrap_or(selector);
    if selector.len() != 8 || !selector.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        bail!("getUserAccountData selector must contain exactly 4 hex bytes");
    }
    Ok(format!(
        "0x{}{:0>64}",
        selector.to_ascii_lowercase(),
        &address[2..]
    ))
}

pub fn borrow_log_debtor(log: &Value, expected_topic0: &str) -> Option<String> {
    let object = log.as_object()?;
    let topics = object.get("topics").and_then(Value::as_array);
    let topic0 = object
        .get("topic0")
        .and_then(Value::as_str)
        .or_else(|| topics?.first()?.as_str())?;
    if !topic0.eq_ignore_ascii_case(expected_topic0) {
        return None;
    }
    let encoded = object
        .get("topic2")
        .and_then(Value::as_str)
        .or_else(|| topics?.get(2)?.as_str())?;
    let raw = encoded
        .strip_prefix("0x")
        .or_else(|| encoded.strip_prefix("0X"))?;
    if raw.len() < 40 || !raw.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    normalize_address(&format!("0x{}", &raw[raw.len() - 40..])).ok()
}

pub fn decode_get_user_account_data(result: &str) -> Result<AccountMetrics> {
    let raw = result
        .strip_prefix("0x")
        .context("eth_call result is not hex")?;
    if raw.len() < 64 * 6 || raw.len() % 64 != 0 {
        bail!("getUserAccountData returned unexpected byte length");
    }
    let mut words = Vec::with_capacity(6);
    for index in 0..6 {
        let word = &raw[index * 64..(index + 1) * 64];
        let value = BigUint::parse_bytes(word.as_bytes(), 16)
            .context("getUserAccountData contains invalid uint256 hex")?;
        words.push((word, value));
    }
    let number = |value: &BigUint| -> Result<f64> {
        value
            .to_f64()
            .context("getUserAccountData uint256 cannot be represented as f64")
    };
    let health_factor = if words[5].0.bytes().all(|byte| byte == b'f' || byte == b'F') {
        None
    } else {
        Some(number(&words[5].1)? / HEALTH_FACTOR_SCALE)
    };
    Ok(AccountMetrics {
        total_collateral_usd: number(&words[0].1)? / BASE_CURRENCY_SCALE,
        total_debt_usd: number(&words[1].1)? / BASE_CURRENCY_SCALE,
        available_borrows_usd: number(&words[2].1)? / BASE_CURRENCY_SCALE,
        current_liquidation_threshold_pct: number(&words[3].1)? / 100.0,
        ltv_pct: number(&words[4].1)? / 100.0,
        health_factor,
    })
}

pub fn compute_borrower_census(
    rows: &[AccountDataRow],
    total_candidate_addresses: usize,
    bootstrap_complete: bool,
    block_number: u64,
    captured_at: DateTime<Utc>,
    policy: CensusPolicy,
) -> Result<Value> {
    let mut addresses = HashSet::new();
    for row in rows {
        if !addresses.insert(row.address.as_str()) {
            bail!("borrower census contains duplicate addresses");
        }
    }

    let success: Vec<(usize, &AccountDataRow)> = rows
        .iter()
        .enumerate()
        .filter(|(_, row)| row.success)
        .collect();
    let successful_calls = success.len();
    let failed_calls = total_candidate_addresses.saturating_sub(successful_calls);
    let failed_ratio = if total_candidate_addresses > 0 {
        failed_calls as f64 / total_candidate_addresses as f64
    } else {
        0.0
    };
    let coverage_ratio = if total_candidate_addresses > 0 {
        successful_calls as f64 / total_candidate_addresses as f64
    } else {
        1.0
    };

    let active: Vec<(usize, &AccountDataRow)> = success
        .into_iter()
        .filter(|(_, row)| row.total_debt_usd.unwrap_or(0.0) > 0.0 && row.health_factor.is_some())
        .collect();
    let total_debt = active
        .iter()
        .map(|(_, row)| row.total_debt_usd.unwrap_or(0.0))
        .sum::<f64>();
    let total_collateral = active
        .iter()
        .map(|(_, row)| row.total_collateral_usd.unwrap_or(0.0))
        .sum::<f64>();

    let mut thresholds = Map::new();
    for threshold in HF_THRESHOLDS {
        let selected: Vec<_> = active
            .iter()
            .filter(|(_, row)| row.health_factor.is_some_and(|value| value <= threshold))
            .collect();
        let debt = selected
            .iter()
            .map(|(_, row)| row.total_debt_usd.unwrap_or(0.0))
            .sum::<f64>();
        thresholds.insert(
            format!("hf_le_{threshold:.2}").replace('.', "_"),
            json!({
                "threshold": threshold,
                "borrower_count": selected.len(),
                "debt_usd": debt,
                "debt_share": ratio_or_zero(debt, total_debt),
            }),
        );
    }

    let bands_contract = [
        (0.00, Some(1.00)),
        (1.00, Some(1.02)),
        (1.02, Some(1.05)),
        (1.05, Some(1.10)),
        (1.10, Some(1.20)),
        (1.20, Some(1.50)),
        (1.50, None),
    ];
    let mut bands = Map::new();
    for (low, high) in bands_contract {
        let selected: Vec<_> = active
            .iter()
            .filter(|(_, row)| {
                let Some(hf) = row.health_factor else {
                    return false;
                };
                match high {
                    None => hf >= low,
                    Some(high) if low == 0.0 => hf < high,
                    Some(high) => hf >= low && hf < high,
                }
            })
            .collect();
        let debt = selected
            .iter()
            .map(|(_, row)| row.total_debt_usd.unwrap_or(0.0))
            .sum::<f64>();
        bands.insert(
            band_name(low, high),
            json!({
                "low": low,
                "high": high,
                "borrower_count": selected.len(),
                "debt_usd": debt,
                "debt_share": ratio_or_zero(debt, total_debt),
            }),
        );
    }

    let mut watchlist: Vec<(usize, &AccountDataRow)> = active
        .iter()
        .copied()
        .filter(|(_, row)| {
            row.health_factor
                .is_some_and(|value| value <= policy.watchlist_health_factor_max)
                || row
                    .total_debt_usd
                    .is_some_and(|value| value >= policy.watchlist_debt_usd_min)
        })
        .collect();
    watchlist.sort_by(|left, right| {
        left.1
            .health_factor
            .partial_cmp(&right.1.health_factor)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                right
                    .1
                    .total_debt_usd
                    .partial_cmp(&left.1.total_debt_usd)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .then(left.0.cmp(&right.0))
    });

    let full_census = bootstrap_complete
        && total_candidate_addresses > 0
        && failed_ratio <= policy.maximum_failed_call_ratio;
    let confidence = if full_census {
        "FULL_CENSUS"
    } else if bootstrap_complete && total_candidate_addresses == 0 {
        "FULL_CENSUS_EMPTY_UNIVERSE"
    } else if bootstrap_complete {
        "PARTIAL_RPC_COVERAGE"
    } else {
        "BOOTSTRAP_INCOMPLETE"
    };

    let liquidatable = thresholds
        .get("hf_le_1_00")
        .context("liquidatable threshold missing")?;
    let critical = thresholds
        .get("hf_le_1_05")
        .context("critical threshold missing")?;
    let near_cliff = thresholds
        .get("hf_le_1_20")
        .context("near-cliff threshold missing")?;

    let captured = captured_at.to_rfc3339_opts(SecondsFormat::Micros, false);
    Ok(json!({
        "protocol": PROTOCOL,
        "mode": MODE,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "mutates_frozen_core": false,
        "mutates_state_v01": false,
        "mutates_state_ab_v01": false,
        "mutates_state_v02": false,
        "captured_at": captured,
        "block_number": block_number,
        "bootstrap_complete": bootstrap_complete,
        "candidate_address_count": total_candidate_addresses,
        "successful_account_calls": successful_calls,
        "failed_account_calls": failed_calls,
        "account_call_coverage_ratio": coverage_ratio,
        "account_call_failed_ratio": failed_ratio,
        "data_confidence": confidence,
        "valid_full_census": full_census,
        "active_borrower_count": active.len(),
        "total_active_debt_usd": total_debt,
        "total_active_collateral_usd": total_collateral,
        "debt_weighted_hf_p10": weighted_quantile(&active, 0.10)?,
        "debt_weighted_hf_p25": weighted_quantile(&active, 0.25)?,
        "debt_weighted_hf_p50": weighted_quantile(&active, 0.50)?,
        "thresholds": thresholds,
        "liquidation_cliff_bands": bands,
        "liquidatable_debt_usd": liquidatable["debt_usd"],
        "liquidatable_debt_share": liquidatable["debt_share"],
        "critical_hf_le_1_05_debt_usd": critical["debt_usd"],
        "critical_hf_le_1_05_debt_share": critical["debt_share"],
        "near_cliff_hf_le_1_20_debt_usd": near_cliff["debt_usd"],
        "near_cliff_hf_le_1_20_debt_share": near_cliff["debt_share"],
        "watchlist_count": watchlist.len(),
        "watchlist_addresses": watchlist.iter().map(|(_, row)| row.address.clone()).collect::<Vec<_>>(),
        "interpretation": "Current Aave V3 Ethereum Core borrower health distribution at one block tag. This is descriptive evidence, not a liquidation-price simulation or trading signal."
    }))
}

fn weighted_quantile(rows: &[(usize, &AccountDataRow)], q: f64) -> Result<Option<f64>> {
    if !(0.0..=1.0).contains(&q) {
        bail!("q must be in [0, 1]");
    }
    let mut values: Vec<(f64, f64, usize)> = rows
        .iter()
        .filter_map(|(index, row)| {
            let value = row.health_factor?;
            let weight = row.total_debt_usd?;
            (value.is_finite() && weight > 0.0).then_some((value, weight, *index))
        })
        .collect();
    if values.is_empty() {
        return Ok(None);
    }
    values.sort_by(|left, right| {
        left.0
            .partial_cmp(&right.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(left.2.cmp(&right.2))
    });
    let total = values.iter().map(|(_, weight, _)| *weight).sum::<f64>();
    let target = total * q;
    let mut cumulative = 0.0;
    for (value, weight, _) in &values {
        cumulative += *weight;
        if cumulative >= target {
            return Ok(Some(*value));
        }
    }
    Ok(values.last().map(|(value, _, _)| *value))
}

fn band_name(low: f64, high: Option<f64>) -> String {
    match high {
        Some(high) => format!("hf_{low:.2}_to_{high:.2}").replace('.', "_"),
        None => format!("hf_ge_{low:.2}").replace('.', "_"),
    }
}

fn ratio_or_zero(numerator: f64, denominator: f64) -> f64 {
    if denominator > 0.0 {
        numerator / denominator
    } else {
        0.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn word(value: u128) -> String {
        format!("{value:064x}")
    }

    #[test]
    fn decodes_aave_six_word_account_data() {
        let encoded = format!(
            "0x{}{}{}{}{}{}",
            word(250_000_000),
            word(100_000_000),
            word(50_000_000),
            word(8_000),
            word(7_500),
            word(1_250_000_000_000_000_000),
        );
        let decoded = decode_get_user_account_data(&encoded).unwrap();
        assert_eq!(decoded.total_collateral_usd, 2.5);
        assert_eq!(decoded.total_debt_usd, 1.0);
        assert_eq!(decoded.current_liquidation_threshold_pct, 80.0);
        assert_eq!(decoded.health_factor, Some(1.25));
    }

    #[test]
    fn uint256_max_health_factor_becomes_none() {
        let encoded = format!(
            "0x{}{}{}{}{}{}",
            word(0),
            word(0),
            word(0),
            word(0),
            word(0),
            "f".repeat(64),
        );
        assert_eq!(
            decode_get_user_account_data(&encoded)
                .unwrap()
                .health_factor,
            None
        );
    }

    #[test]
    fn census_matches_threshold_and_watchlist_contract() {
        let rows = vec![
            AccountDataRow {
                address: "0x1".to_owned(),
                success: true,
                total_collateral_usd: Some(200.0),
                total_debt_usd: Some(100.0),
                available_borrows_usd: Some(0.0),
                current_liquidation_threshold_pct: Some(80.0),
                ltv_pct: Some(75.0),
                health_factor: Some(1.01),
                error: None,
            },
            AccountDataRow {
                address: "0x2".to_owned(),
                success: true,
                total_collateral_usd: Some(300.0),
                total_debt_usd: Some(2_000_000.0),
                available_borrows_usd: Some(0.0),
                current_liquidation_threshold_pct: Some(80.0),
                ltv_pct: Some(75.0),
                health_factor: Some(2.0),
                error: None,
            },
        ];
        let report = compute_borrower_census(
            &rows,
            2,
            true,
            23_000_000,
            Utc.timestamp_opt(1_700_000_000, 123_456_000).unwrap(),
            CensusPolicy::default(),
        )
        .unwrap();
        assert_eq!(report["valid_full_census"], true);
        assert_eq!(report["thresholds"]["hf_le_1_05"]["borrower_count"], 1);
        assert_eq!(report["watchlist_count"], 2);
    }
}
