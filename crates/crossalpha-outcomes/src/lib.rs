use anyhow::{Result, bail};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const SOURCE_LAYERS: [&str; 3] = ["STATE_V02", "STATE_V03", "STATE_V04"];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SourceRecord {
    pub source_layer: String,
    pub known_at: DateTime<Utc>,
    pub record_sha256: String,
    #[serde(default)]
    pub path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OutcomeMark {
    pub date: NaiveDate,
    pub record_sha256: String,
    pub net_return: f64,
    pub cash_return: f64,
    #[serde(default)]
    pub a_mark_record_sha256: Option<String>,
    #[serde(default)]
    pub shadow_risk_multiplier: Option<f64>,
    #[serde(default)]
    pub path: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OutcomeMetrics {
    #[serde(rename = "A_cumulative_net_return")]
    pub a_cumulative_net_return: f64,
    #[serde(rename = "B_cumulative_net_return")]
    pub b_cumulative_net_return: f64,
    #[serde(rename = "B_minus_A_cumulative_return")]
    pub b_minus_a_cumulative_return: f64,
    pub cash_cumulative_return: f64,
    #[serde(rename = "A_max_drawdown")]
    pub a_max_drawdown: f64,
    #[serde(rename = "B_max_drawdown")]
    pub b_max_drawdown: f64,
    #[serde(rename = "A_worst_daily_return")]
    pub a_worst_daily_return: f64,
    #[serde(rename = "B_worst_daily_return")]
    pub b_worst_daily_return: f64,
    #[serde(rename = "A_negative_day_count")]
    pub a_negative_day_count: usize,
    #[serde(rename = "B_negative_day_count")]
    pub b_negative_day_count: usize,
    pub intervention_day_count: usize,
    #[serde(rename = "average_B_multiplier")]
    pub average_b_multiplier: f64,
}

pub fn select_daily_anchors(
    records: &[SourceRecord],
    not_before: DateTime<Utc>,
) -> Vec<SourceRecord> {
    let mut selected: BTreeMap<(String, NaiveDate), SourceRecord> = BTreeMap::new();
    for row in records {
        if row.known_at < not_before || !SOURCE_LAYERS.contains(&row.source_layer.as_str()) {
            continue;
        }
        let key = (row.source_layer.clone(), row.known_at.date_naive());
        let replace = selected
            .get(&key)
            .is_none_or(|previous| row.known_at > previous.known_at);
        if replace {
            selected.insert(key, row.clone());
        }
    }
    let mut rows: Vec<SourceRecord> = selected.into_values().collect();
    rows.sort_by(|left, right| {
        left.known_at
            .cmp(&right.known_at)
            .then(left.source_layer.cmp(&right.source_layer))
    });
    rows
}

pub fn expected_dates(anchor_known_at: DateTime<Utc>, horizon_days: usize) -> Vec<NaiveDate> {
    let start = anchor_known_at.date_naive() + Duration::days(1);
    (0..horizon_days)
        .map(|offset| start + Duration::days(offset as i64))
        .collect()
}

pub fn outcome_metrics(
    dates: &[NaiveDate],
    a_marks: &BTreeMap<NaiveDate, OutcomeMark>,
    b_marks: &BTreeMap<NaiveDate, OutcomeMark>,
) -> Result<OutcomeMetrics> {
    if dates.is_empty() {
        bail!("outcome horizon is empty");
    }
    let mut a_returns = Vec::with_capacity(dates.len());
    let mut b_returns = Vec::with_capacity(dates.len());
    let mut cash_returns = Vec::with_capacity(dates.len());
    let mut multipliers = Vec::with_capacity(dates.len());
    for day in dates {
        let a = a_marks
            .get(day)
            .ok_or_else(|| anyhow::anyhow!("missing A outcome mark for {day}"))?;
        let b = b_marks
            .get(day)
            .ok_or_else(|| anyhow::anyhow!("missing B outcome mark for {day}"))?;
        if b.a_mark_record_sha256.as_deref() != Some(a.record_sha256.as_str()) {
            bail!("Outcome A/B hash mismatch on {day}");
        }
        if a.date != b.date {
            bail!("Outcome A/B date mismatch on {day}");
        }
        for value in [a.net_return, b.net_return, a.cash_return] {
            if !value.is_finite() || value <= -1.0 {
                bail!("outcome return must be finite and greater than -1");
            }
        }
        let multiplier = b
            .shadow_risk_multiplier
            .ok_or_else(|| anyhow::anyhow!("B mark missing shadow_risk_multiplier on {day}"))?;
        if !multiplier.is_finite() {
            bail!("B multiplier is non-finite on {day}");
        }
        a_returns.push(a.net_return);
        b_returns.push(b.net_return);
        cash_returns.push(a.cash_return);
        multipliers.push(multiplier);
    }
    let a_cum = cumulative_return(&a_returns);
    let b_cum = cumulative_return(&b_returns);
    Ok(OutcomeMetrics {
        a_cumulative_net_return: a_cum,
        b_cumulative_net_return: b_cum,
        b_minus_a_cumulative_return: b_cum - a_cum,
        cash_cumulative_return: cumulative_return(&cash_returns),
        a_max_drawdown: max_drawdown(&a_returns),
        b_max_drawdown: max_drawdown(&b_returns),
        a_worst_daily_return: a_returns.iter().copied().reduce(f64::min).unwrap_or(0.0),
        b_worst_daily_return: b_returns.iter().copied().reduce(f64::min).unwrap_or(0.0),
        a_negative_day_count: a_returns.iter().filter(|value| **value < 0.0).count(),
        b_negative_day_count: b_returns.iter().filter(|value| **value < 0.0).count(),
        intervention_day_count: multipliers.iter().filter(|value| **value < 1.0).count(),
        average_b_multiplier: multipliers.iter().sum::<f64>() / multipliers.len() as f64,
    })
}

pub fn cumulative_return(returns: &[f64]) -> f64 {
    returns.iter().fold(1.0, |equity, value| equity * (1.0 + value)) - 1.0
}

pub fn max_drawdown(returns: &[f64]) -> f64 {
    let mut equity = 1.0;
    let mut peak = 1.0;
    let mut worst = 0.0;
    for value in returns {
        equity *= 1.0 + value;
        peak = peak.max(equity);
        worst = worst.min(equity / peak - 1.0);
    }
    worst
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn anchors_pick_latest_known_record_per_source_day() {
        let a = Utc.with_ymd_and_hms(2026, 9, 6, 10, 0, 0).unwrap();
        let b = Utc.with_ymd_and_hms(2026, 9, 6, 11, 0, 0).unwrap();
        let rows = vec![
            SourceRecord { source_layer: "STATE_V03".into(), known_at: a, record_sha256: "a".into(), path: None },
            SourceRecord { source_layer: "STATE_V03".into(), known_at: b, record_sha256: "b".into(), path: None },
        ];
        let selected = select_daily_anchors(&rows, a);
        assert_eq!(selected.len(), 1);
        assert_eq!(selected[0].record_sha256, "b");
    }

    #[test]
    fn drawdown_includes_initial_equity() {
        let returns = [0.10, -0.20, 0.05];
        assert!((max_drawdown(&returns) - -0.20).abs() < 1e-12);
    }
}
