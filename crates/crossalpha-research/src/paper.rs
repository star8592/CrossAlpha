use crate::baseline::{BaselineConfig, RISK_ASSETS, apply_constraints, scale_to_target_vol};
use anyhow::{Context, Result, bail};
use chrono::{Duration, NaiveDate};
use crossalpha_data::AssetReturnRow;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

pub const PAPER_PROTOCOL: &str = "CROSSALPHA_FREE_V0_1_PAPER";
pub const RESEARCH_PROTOCOL: &str = "CROSSALPHA_FREE_V0_1";
pub const STRATEGY: &str = "B3_ABSOLUTE_TREND_EQUAL_WEIGHT";
pub const ALL_ASSETS: [&str; 9] = [
    "US_EQUITY",
    "US_GROWTH",
    "GOLD",
    "SILVER",
    "COPPER",
    "WTI",
    "BTC",
    "ETH",
    "CASH",
];
pub const EXECUTION_LAG_DAYS: i64 = 1;
pub const ONE_WAY_COST_BPS: f64 = 5.0;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DailyPanel {
    pub dates: Vec<NaiveDate>,
    pub returns: BTreeMap<String, Vec<f64>>,
    pub available: BTreeMap<String, Vec<bool>>,
}

pub fn build_daily_panel(
    rows: &[AssetReturnRow],
    start: NaiveDate,
    end: NaiveDate,
) -> Result<DailyPanel> {
    if end <= start {
        bail!("daily panel end must be after start");
    }
    let mut dates = Vec::new();
    let mut day = start;
    while day < end {
        dates.push(day);
        day += Duration::days(1);
    }
    let position: BTreeMap<NaiveDate, usize> = dates
        .iter()
        .copied()
        .enumerate()
        .map(|(index, day)| (day, index))
        .collect();

    let mut returns = BTreeMap::<String, Vec<f64>>::new();
    let mut available = BTreeMap::<String, Vec<bool>>::new();
    for asset in RISK_ASSETS {
        returns.insert(asset.to_owned(), vec![f64::NAN; dates.len()]);
        available.insert(asset.to_owned(), vec![false; dates.len()]);
    }
    returns.insert("CASH".to_owned(), vec![f64::NAN; dates.len()]);

    let mut by_asset = BTreeMap::<String, Vec<&AssetReturnRow>>::new();
    for row in rows {
        if position.contains_key(&row.date.date_naive()) {
            by_asset
                .entry(row.economic_asset.clone())
                .or_default()
                .push(row);
        }
    }
    for asset in RISK_ASSETS {
        let Some(asset_rows) = by_asset.get_mut(asset) else {
            continue;
        };
        asset_rows.sort_by_key(|row| row.date);
        let mut seen = BTreeSet::new();
        let inception = asset_rows
            .first()
            .map(|row| row.date.date_naive())
            .context("asset rows unexpectedly empty")?;
        for row in asset_rows.iter() {
            let day = row.date.date_naive();
            if !seen.insert(day) {
                bail!("duplicate research dates for {asset}");
            }
            if let Some(index) = position.get(&day).copied()
                && let Some(value) = row.daily_return
            {
                returns.get_mut(asset).expect("risk series exists")[index] = value;
            }
        }
        for (index, day) in dates.iter().copied().enumerate() {
            if day >= inception {
                available
                    .get_mut(asset)
                    .expect("availability series exists")[index] = true;
                if !returns[asset][index].is_finite() {
                    returns.get_mut(asset).expect("risk series exists")[index] = 0.0;
                }
            }
        }
    }

    let cash_rows = by_asset
        .get_mut("CASH")
        .context("CASH series missing from free Core returns")?;
    cash_rows.sort_by_key(|row| row.date);
    let mut cash_by_day = BTreeMap::<NaiveDate, Option<f64>>::new();
    for row in cash_rows.iter() {
        cash_by_day.insert(row.date.date_naive(), row.daily_return);
    }
    let mut last_cash = 0.0;
    for (index, day) in dates.iter().copied().enumerate() {
        if let Some(Some(value)) = cash_by_day.get(&day) {
            last_cash = *value;
        }
        returns.get_mut("CASH").expect("cash series exists")[index] = last_cash;
    }
    Ok(DailyPanel {
        dates,
        returns,
        available,
    })
}

pub fn compute_frozen_b3_target(
    panel: &DailyPanel,
    signal_date: NaiveDate,
) -> Result<BTreeMap<String, f64>> {
    let index = panel
        .dates
        .iter()
        .position(|day| *day == signal_date)
        .with_context(|| format!("signal date missing from frozen B3 inputs: {signal_date}"))?;
    if panel
        .dates
        .last()
        .copied()
        .is_some_and(|day| day > signal_date)
    {
        bail!("frozen B3 inputs contain observations after signal_date");
    }

    let cfg = BaselineConfig::default();
    let mut raw = BTreeMap::<String, f64>::new();
    for asset in RISK_ASSETS {
        let series = panel.returns.get(asset).context("risk series missing")?;
        let trend = trailing_compound_at(series, index, cfg.trend_window_days);
        let available = panel
            .available
            .get(asset)
            .and_then(|values| values.get(index))
            .copied()
            .unwrap_or(false);
        let eligible = available && trend.is_some_and(|value| value > 0.0);
        raw.insert(asset.to_owned(), if eligible { 1.0 } else { 0.0 });
    }

    let constrained = apply_constraints(&raw, cfg);
    let history_start = index.saturating_sub(cfg.vol_window_days.saturating_sub(1));
    let mut history = BTreeMap::<String, Vec<f64>>::new();
    for asset in RISK_ASSETS {
        let source = panel.returns.get(asset).context("risk history missing")?;
        history.insert(asset.to_owned(), source[history_start..=index].to_vec());
    }
    let weights = scale_to_target_vol(&constrained, &history, cfg)?;
    let total = ALL_ASSETS
        .iter()
        .map(|asset| weights.get(*asset).copied().unwrap_or(0.0))
        .sum::<f64>();
    if (total - 1.0).abs() > 1e-10 {
        bail!("frozen B3 snapshot weights do not sum to one");
    }
    if weights.values().any(|value| *value < -1e-12) {
        bail!("frozen B3 snapshot contains negative weight");
    }
    Ok(weights)
}

pub fn apply_shadow_multiplier(
    a_weights: &BTreeMap<String, f64>,
    multiplier: f64,
) -> Result<BTreeMap<String, f64>> {
    if ![1.0, 0.75, 0.50].contains(&multiplier) {
        bail!("State multiplier outside frozen set");
    }
    let mut result = BTreeMap::new();
    let mut risk_gross = 0.0;
    for asset in RISK_ASSETS {
        let value = a_weights.get(asset).copied().unwrap_or(0.0) * multiplier;
        result.insert(asset.to_owned(), value);
        risk_gross += value;
    }
    result.insert("CASH".to_owned(), (1.0 - risk_gross).max(0.0));
    Ok(result)
}

fn trailing_compound_at(series: &[f64], index: usize, window: usize) -> Option<f64> {
    if window == 0 || index + 1 < window {
        return None;
    }
    let values = &series[index + 1 - window..=index];
    if values
        .iter()
        .any(|value| !value.is_finite() || *value <= -1.0)
    {
        return None;
    }
    Some(
        values
            .iter()
            .map(|value| value.ln_1p())
            .sum::<f64>()
            .exp_m1(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pre_inception_nan_does_not_abort_trend_evaluation() {
        let mut series = vec![f64::NAN; 20];
        series.extend(vec![0.001; 365]);
        assert!(trailing_compound_at(&series, series.len() - 1, 365).unwrap() > 0.0);
        assert!(trailing_compound_at(&series, 200, 365).is_none());
    }
}
