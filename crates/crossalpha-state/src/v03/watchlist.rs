use crate::v03::{ACTIONABILITY, MODE, PROTOCOL};
use crate::v03_census::AccountDataRow;
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};

pub fn compute_watchlist_snapshot(
    rows: &[AccountDataRow],
    expected_addresses: usize,
    block_number: u64,
    captured_at: DateTime<Utc>,
) -> Value {
    let success: Vec<&AccountDataRow> = rows.iter().filter(|row| row.success).collect();
    let active: Vec<&AccountDataRow> = success
        .iter()
        .copied()
        .filter(|row| row.total_debt_usd.unwrap_or(0.0) > 0.0 && row.health_factor.is_some())
        .collect();
    let total_debt = active
        .iter()
        .map(|row| row.total_debt_usd.unwrap_or(0.0))
        .sum::<f64>();
    let minimum_health_factor = active
        .iter()
        .filter_map(|row| row.health_factor)
        .reduce(f64::min);

    let liquidatable: Vec<&AccountDataRow> = active
        .iter()
        .copied()
        .filter(|row| row.health_factor.is_some_and(|value| value <= 1.0))
        .collect();
    let critical: Vec<&AccountDataRow> = active
        .iter()
        .copied()
        .filter(|row| row.health_factor.is_some_and(|value| value <= 1.05))
        .collect();
    let near: Vec<&AccountDataRow> = active
        .iter()
        .copied()
        .filter(|row| row.health_factor.is_some_and(|value| value <= 1.20))
        .collect();
    let succeeded = success.len();
    let failed = expected_addresses.saturating_sub(succeeded);

    json!({
        "protocol": PROTOCOL,
        "mode": MODE,
        "scope": "WATCHLIST_ONLY",
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "captured_at": captured_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "block_number": block_number,
        "expected_watchlist_addresses": expected_addresses,
        "successful_account_calls": succeeded,
        "failed_account_calls": failed,
        "watchlist_call_coverage_ratio": if expected_addresses > 0 {
            succeeded as f64 / expected_addresses as f64
        } else {
            1.0
        },
        "active_watchlist_borrower_count": active.len(),
        "watchlist_active_debt_usd": total_debt,
        "minimum_health_factor": minimum_health_factor,
        "liquidatable_borrower_count": liquidatable.len(),
        "liquidatable_debt_usd": debt_sum(&liquidatable),
        "critical_hf_le_1_05_borrower_count": critical.len(),
        "critical_hf_le_1_05_debt_usd": debt_sum(&critical),
        "near_cliff_hf_le_1_20_borrower_count": near.len(),
        "near_cliff_hf_le_1_20_debt_usd": debt_sum(&near),
        "full_market_census_claim_allowed": false,
        "interpretation": "Fast refresh of addresses selected by the previous valid full census. It cannot estimate whole-market borrower shares or replace a full census."
    })
}

fn debt_sum(rows: &[&AccountDataRow]) -> f64 {
    rows.iter()
        .map(|row| row.total_debt_usd.unwrap_or(0.0))
        .sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn watchlist_scope_never_claims_full_market_coverage() {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let rows = vec![AccountDataRow {
            address: "0x0000000000000000000000000000000000000001".to_owned(),
            success: true,
            total_collateral_usd: Some(100.0),
            total_debt_usd: Some(50.0),
            available_borrows_usd: Some(0.0),
            current_liquidation_threshold_pct: Some(80.0),
            ltv_pct: Some(75.0),
            health_factor: Some(0.99),
            error: None,
        }];
        let report = compute_watchlist_snapshot(&rows, 1, 20_000_000, now);
        assert_eq!(report["scope"], "WATCHLIST_ONLY");
        assert_eq!(report["full_market_census_claim_allowed"], false);
        assert_eq!(report["liquidatable_borrower_count"], 1);
    }
}
