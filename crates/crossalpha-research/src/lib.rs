pub mod baseline;
pub mod paper;
pub mod paper_runtime;

use anyhow::{Context, Result, bail};
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct FuturesBar {
    pub date: DateTime<Utc>,
    pub contract: String,
    pub close: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ContractMeta {
    pub contract: String,
    pub expiration_date: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RollSelection {
    pub date: DateTime<Utc>,
    pub contract: String,
    pub expiration_date: DateTime<Utc>,
    pub decision_volume_date: DateTime<Utc>,
    pub rolled: bool,
    pub forced_roll: bool,
    pub decision_reason: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct FuturesReturnRow {
    pub date: DateTime<Utc>,
    pub contract: String,
    pub previous_contract: Option<String>,
    pub close: f64,
    pub previous_close: Option<f64>,
    pub rolled: bool,
    pub excess_return: Option<f64>,
    pub return_index: f64,
}

pub fn build_previous_volume_roll_map(
    bars: &[FuturesBar],
    metadata: &[ContractMeta],
    safety_days: i64,
) -> Result<Vec<RollSelection>> {
    if safety_days < 0 {
        bail!("safety_days must be non-negative");
    }
    let mut meta = BTreeMap::new();
    for row in metadata {
        if meta.insert(row.contract.clone(), row.expiration_date).is_some() {
            bail!("contract_metadata contains duplicate contracts");
        }
    }
    let mut by_date: BTreeMap<DateTime<Utc>, BTreeMap<String, f64>> = BTreeMap::new();
    for row in bars {
        if !row.volume.is_finite() || row.volume < 0.0 {
            bail!("volume must be non-negative and non-null");
        }
        if !meta.contains_key(&row.contract) {
            bail!("missing contract metadata for: {}", row.contract);
        }
        if by_date
            .entry(row.date)
            .or_default()
            .insert(row.contract.clone(), row.volume)
            .is_some()
        {
            bail!("bars contain duplicate date/contract rows");
        }
    }
    let dates: Vec<DateTime<Utc>> = by_date.keys().copied().collect();
    if dates.len() < 2 {
        bail!("at least two trading dates are required");
    }

    let mut held_contract: Option<String> = None;
    let mut held_expiry: Option<DateTime<Utc>> = None;
    let mut result = Vec::with_capacity(dates.len() - 1);
    for pair in dates.windows(2) {
        let previous_date = pair[0];
        let current_date = pair[1];
        let previous = &by_date[&previous_date];
        let current_available: BTreeSet<&str> = by_date[&current_date]
            .keys()
            .map(String::as_str)
            .collect();
        let cutoff = current_date + Duration::days(safety_days);
        let mut eligible: Vec<(&str, f64, DateTime<Utc>)> = previous
            .iter()
            .filter_map(|(contract, volume)| {
                let expiry = meta[contract];
                let not_backwards = held_expiry.is_none_or(|held| expiry >= held);
                (expiry > cutoff && current_available.contains(contract.as_str()) && not_backwards)
                    .then_some((contract.as_str(), *volume, expiry))
            })
            .collect();
        if eligible.is_empty() {
            bail!("no eligible contract for {}", current_date.to_rfc3339());
        }
        eligible.sort_by(|left, right| {
            right
                .1
                .partial_cmp(&left.1)
                .unwrap_or(Ordering::Equal)
                .then(left.2.cmp(&right.2))
                .then(left.0.cmp(right.0))
        });
        let mut candidate = eligible[0].0.to_owned();
        let mut candidate_expiry = eligible[0].2;
        let mut forced_roll = false;
        let mut decision_reason = "previous_volume".to_owned();

        if let (Some(held), Some(held_expiry_value)) = (&held_contract, held_expiry) {
            let held_available = current_available.contains(held.as_str());
            let held_safe = held_expiry_value > cutoff;
            let held_volume = previous.get(held).copied();
            if held_available && held_safe {
                if let Some(held_volume) = held_volume {
                    let candidate_volume = eligible[0].1;
                    if candidate_expiry == held_expiry_value || candidate_volume <= held_volume {
                        candidate = held.clone();
                        candidate_expiry = held_expiry_value;
                        decision_reason = "hold".to_owned();
                    }
                }
            } else {
                forced_roll = true;
                decision_reason = if !held_safe {
                    "expiry_safety".to_owned()
                } else {
                    "contract_unavailable".to_owned()
                };
            }
        }
        let rolled = held_contract.as_ref().is_some_and(|held| held != &candidate);
        result.push(RollSelection {
            date: current_date,
            contract: candidate.clone(),
            expiration_date: candidate_expiry,
            decision_volume_date: previous_date,
            rolled,
            forced_roll,
            decision_reason,
        });
        held_contract = Some(candidate);
        held_expiry = Some(candidate_expiry);
    }
    Ok(result)
}

pub fn build_roll_mtm_returns(
    bars: &[FuturesBar],
    roll_map: &[RollSelection],
    roll_cost_bps: f64,
) -> Result<Vec<FuturesReturnRow>> {
    if !roll_cost_bps.is_finite() || roll_cost_bps < 0.0 {
        bail!("roll_cost_bps must be non-negative");
    }
    if roll_map.is_empty() {
        bail!("roll_map is empty");
    }
    let mut prices = BTreeMap::new();
    for row in bars {
        if !row.close.is_finite() || row.close <= 0.0 {
            bail!("bars close prices must be positive and non-null");
        }
        if prices.insert((row.date, row.contract.clone()), row.close).is_some() {
            bail!("bars contain duplicate date/contract rows");
        }
    }
    let mut seen_dates = BTreeSet::new();
    for row in roll_map {
        if !seen_dates.insert(row.date) {
            bail!("roll_map contains duplicate dates");
        }
    }

    let mut previous_date = None;
    let mut previous_selected: Option<String> = None;
    let mut return_index = 1.0;
    let mut result = Vec::with_capacity(roll_map.len());
    for row in roll_map {
        let current_close = *prices
            .get(&(row.date, row.contract.clone()))
            .with_context(|| {
                format!(
                    "missing current price for {} on {}",
                    row.contract,
                    row.date.to_rfc3339()
                )
            })?;
        let rolled = previous_selected
            .as_ref()
            .is_some_and(|previous| previous != &row.contract);
        let mut previous_close = None;
        let mut excess_return = None;
        if let Some(previous_date) = previous_date {
            let prior = *prices
                .get(&(previous_date, row.contract.clone()))
                .with_context(|| {
                    format!(
                        "missing prior-date price for newly selected contract {} on {}; cannot construct gap-free MTM return",
                        row.contract,
                        previous_date.to_rfc3339()
                    )
                })?;
            let mut value = current_close / prior - 1.0;
            if rolled && roll_cost_bps > 0.0 {
                value -= roll_cost_bps / 10_000.0;
            }
            return_index *= 1.0 + value;
            previous_close = Some(prior);
            excess_return = Some(value);
        }
        result.push(FuturesReturnRow {
            date: row.date,
            contract: row.contract.clone(),
            previous_contract: previous_selected.clone(),
            close: current_close,
            previous_close,
            rolled,
            excess_return,
            return_index,
        });
        previous_date = Some(row.date);
        previous_selected = Some(row.contract.clone());
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn day(value: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2026, 1, value, 0, 0, 0).unwrap()
    }

    #[test]
    fn roll_decision_uses_previous_day_volume() {
        let metadata = vec![
            ContractMeta { contract: "F1".into(), expiration_date: day(20) },
            ContractMeta { contract: "F2".into(), expiration_date: day(30) },
        ];
        let bars = vec![
            FuturesBar { date: day(1), contract: "F1".into(), close: 100.0, volume: 10.0 },
            FuturesBar { date: day(1), contract: "F2".into(), close: 110.0, volume: 20.0 },
            FuturesBar { date: day(2), contract: "F1".into(), close: 101.0, volume: 1000.0 },
            FuturesBar { date: day(2), contract: "F2".into(), close: 111.0, volume: 1.0 },
        ];
        let map = build_previous_volume_roll_map(&bars, &metadata, 5).unwrap();
        assert_eq!(map[0].contract, "F2");
    }

    #[test]
    fn roll_mtm_never_uses_cross_contract_gap_as_return() {
        let bars = vec![
            FuturesBar { date: day(1), contract: "F1".into(), close: 100.0, volume: 1.0 },
            FuturesBar { date: day(1), contract: "F2".into(), close: 200.0, volume: 1.0 },
            FuturesBar { date: day(2), contract: "F1".into(), close: 101.0, volume: 1.0 },
            FuturesBar { date: day(2), contract: "F2".into(), close: 202.0, volume: 1.0 },
            FuturesBar { date: day(3), contract: "F2".into(), close: 204.0, volume: 1.0 },
        ];
        let roll_map = vec![
            RollSelection { date: day(2), contract: "F1".into(), expiration_date: day(20), decision_volume_date: day(1), rolled: false, forced_roll: false, decision_reason: "x".into() },
            RollSelection { date: day(3), contract: "F2".into(), expiration_date: day(30), decision_volume_date: day(2), rolled: true, forced_roll: false, decision_reason: "x".into() },
        ];
        let rows = build_roll_mtm_returns(&bars, &roll_map, 0.0).unwrap();
        assert!((rows[1].excess_return.unwrap() - (204.0 / 202.0 - 1.0)).abs() < 1e-12);
    }
}
