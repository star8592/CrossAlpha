use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const RISK_ASSETS: [&str; 8] = [
    "US_EQUITY",
    "US_GROWTH",
    "GOLD",
    "SILVER",
    "COPPER",
    "WTI",
    "BTC",
    "ETH",
];

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
pub struct BaselineConfig {
    pub vol_window_days: usize,
    pub target_vol: f64,
    pub trend_window_days: usize,
    pub annualization_days: f64,
    pub single_asset_max: f64,
}

impl Default for BaselineConfig {
    fn default() -> Self {
        Self {
            vol_window_days: 63,
            target_vol: 0.10,
            trend_window_days: 365,
            annualization_days: 365.0,
            single_asset_max: 0.25,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BaselineFeatures {
    pub vol: Vec<Option<f64>>,
    pub trend: Vec<Option<f64>>,
    pub horizon_30: Vec<Option<f64>>,
    pub horizon_90: Vec<Option<f64>>,
    pub horizon_180: Vec<Option<f64>>,
    pub horizon_365: Vec<Option<f64>>,
    pub multi_score: Vec<Option<f64>>,
}

pub fn compute_features(returns: &[f64], config: BaselineConfig) -> Result<BaselineFeatures> {
    validate_returns(returns)?;
    let vol = rolling_sample_vol(
        returns,
        config.vol_window_days,
        config.annualization_days,
    );
    let trend = rolling_compound(returns, config.trend_window_days)?;
    let horizon_30 = rolling_compound(returns, 30)?;
    let horizon_90 = rolling_compound(returns, 90)?;
    let horizon_180 = rolling_compound(returns, 180)?;
    let horizon_365 = rolling_compound(returns, 365)?;
    let multi_score = (0..returns.len())
        .map(|index| {
            let values = [
                horizon_30[index],
                horizon_90[index],
                horizon_180[index],
                horizon_365[index],
            ];
            if values.iter().any(Option::is_none) {
                return None;
            }
            Some(
                values
                    .into_iter()
                    .flatten()
                    .map(|value| value.signum())
                    .sum::<f64>()
                    / 4.0,
            )
        })
        .collect();
    Ok(BaselineFeatures {
        vol,
        trend,
        horizon_30,
        horizon_90,
        horizon_180,
        horizon_365,
        multi_score,
    })
}

pub fn rolling_compound(returns: &[f64], window: usize) -> Result<Vec<Option<f64>>> {
    if window == 0 {
        bail!("rolling window must be positive");
    }
    validate_returns(returns)?;
    let mut result = vec![None; returns.len()];
    for end in window..=returns.len() {
        let mut log_sum = 0.0;
        for value in &returns[end - window..end] {
            log_sum += value.ln_1p();
        }
        result[end - 1] = Some(log_sum.exp_m1());
    }
    Ok(result)
}

pub fn rolling_sample_vol(
    returns: &[f64],
    window: usize,
    annualization_days: f64,
) -> Vec<Option<f64>> {
    let mut result = vec![None; returns.len()];
    if window < 2 || !annualization_days.is_finite() || annualization_days <= 0.0 {
        return result;
    }
    for end in window..=returns.len() {
        let slice = &returns[end - window..end];
        if slice.iter().any(|value| !value.is_finite()) {
            continue;
        }
        let mean = slice.iter().sum::<f64>() / slice.len() as f64;
        let variance = slice
            .iter()
            .map(|value| {
                let delta = *value - mean;
                delta * delta
            })
            .sum::<f64>()
            / (slice.len() - 1) as f64;
        result[end - 1] = Some(variance.sqrt() * annualization_days.sqrt());
    }
    result
}

pub fn apply_constraints(
    raw: &BTreeMap<String, f64>,
    config: BaselineConfig,
) -> BTreeMap<String, f64> {
    let mut positive = BTreeMap::new();
    let mut total = 0.0;
    for asset in RISK_ASSETS {
        let value = raw
            .get(asset)
            .copied()
            .filter(|value| value.is_finite())
            .unwrap_or(0.0)
            .max(0.0);
        positive.insert(asset.to_owned(), value);
        total += value;
    }
    if total > 0.0 {
        for value in positive.values_mut() {
            *value /= total;
            *value = value.min(config.single_asset_max);
        }
    } else {
        positive.values_mut().for_each(|value| *value = 0.0);
    }

    for (assets, cap) in sleeve_caps() {
        let sleeve_total = assets
            .iter()
            .map(|asset| positive.get(*asset).copied().unwrap_or(0.0))
            .sum::<f64>();
        if sleeve_total > cap && sleeve_total > 0.0 {
            let scale = cap / sleeve_total;
            for asset in assets {
                if let Some(value) = positive.get_mut(*asset) {
                    *value *= scale;
                }
            }
        }
    }
    let mut gross = positive.values().sum::<f64>();
    if gross > 1.0 {
        for value in positive.values_mut() {
            *value /= gross;
        }
        gross = 1.0;
    }
    positive.insert("CASH".to_owned(), (1.0 - gross).max(0.0));
    positive
}

pub fn scale_to_target_vol(
    weights: &BTreeMap<String, f64>,
    history: &BTreeMap<String, Vec<f64>>,
    config: BaselineConfig,
) -> Result<BTreeMap<String, f64>> {
    let mut result = weights.clone();
    let selected: Vec<String> = RISK_ASSETS
        .iter()
        .filter_map(|asset| {
            (weights.get(*asset).copied().unwrap_or(0.0) > 0.0).then_some((*asset).to_owned())
        })
        .collect();
    if selected.is_empty() || config.target_vol <= 0.0 {
        return Ok(result);
    }
    let window = config.vol_window_days;
    for asset in &selected {
        let values = history
            .get(asset)
            .with_context(|| format!("missing return history for {asset}"))?;
        if values.len() < window {
            return Ok(result);
        }
    }
    let rows = (0..window)
        .map(|offset| {
            selected
                .iter()
                .map(|asset| {
                    let values = &history[asset];
                    let value = values[values.len() - window + offset];
                    if value.is_finite() { value } else { 0.0 }
                })
                .collect::<Vec<_>>()
        })
        .collect::<Vec<_>>();
    let covariance = sample_covariance(&rows);
    let vector = selected
        .iter()
        .map(|asset| weights.get(asset).copied().unwrap_or(0.0))
        .collect::<Vec<_>>();
    let mut variance = 0.0;
    for i in 0..vector.len() {
        for j in 0..vector.len() {
            variance += vector[i] * covariance[i][j] * vector[j];
        }
    }
    variance *= config.annualization_days;
    if !variance.is_finite() || variance <= 0.0 {
        return Ok(result);
    }
    let predicted_vol = variance.sqrt();
    let scale = (config.target_vol / predicted_vol).min(1.0);
    if scale >= 1.0 {
        return Ok(result);
    }
    for asset in &selected {
        if let Some(value) = result.get_mut(asset) {
            *value *= scale;
        }
    }
    let gross = RISK_ASSETS
        .iter()
        .map(|asset| result.get(*asset).copied().unwrap_or(0.0))
        .sum::<f64>();
    result.insert("CASH".to_owned(), (1.0 - gross).max(0.0));
    Ok(result)
}

fn sample_covariance(rows: &[Vec<f64>]) -> Vec<Vec<f64>> {
    if rows.len() < 2 || rows.is_empty() {
        return Vec::new();
    }
    let columns = rows[0].len();
    let means: Vec<f64> = (0..columns)
        .map(|column| rows.iter().map(|row| row[column]).sum::<f64>() / rows.len() as f64)
        .collect();
    (0..columns)
        .map(|left| {
            (0..columns)
                .map(|right| {
                    rows.iter()
                        .map(|row| (row[left] - means[left]) * (row[right] - means[right]))
                        .sum::<f64>()
                        / (rows.len() - 1) as f64
                })
                .collect()
        })
        .collect()
}

fn sleeve_caps() -> [(&'static [&'static str], f64); 4] {
    [
        (&["BTC", "ETH"], 0.35),
        (&["US_EQUITY", "US_GROWTH"], 0.40),
        (&["GOLD", "SILVER"], 0.35),
        (&["COPPER", "WTI"], 0.35),
    ]
}

fn validate_returns(returns: &[f64]) -> Result<()> {
    if returns
        .iter()
        .any(|value| !value.is_finite() || *value <= -1.0)
    {
        bail!("returns must be finite and greater than -1");
    }
    Ok(())
}

use anyhow::Context;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constraints_never_leverage_and_preserve_cash_residual() {
        let raw = RISK_ASSETS
            .iter()
            .map(|asset| ((*asset).to_owned(), 1.0))
            .collect();
        let weights = apply_constraints(&raw, BaselineConfig::default());
        let risk = RISK_ASSETS
            .iter()
            .map(|asset| weights[*asset])
            .sum::<f64>();
        assert!(risk <= 1.0 + 1e-12);
        assert!((risk + weights["CASH"] - 1.0).abs() < 1e-12);
        assert!(weights["BTC"] + weights["ETH"] <= 0.35 + 1e-12);
    }

    #[test]
    fn target_vol_never_scales_up() {
        let weights = BTreeMap::from([
            ("US_EQUITY".to_owned(), 0.2),
            ("CASH".to_owned(), 0.8),
        ]);
        let history = BTreeMap::from([("US_EQUITY".to_owned(), vec![0.0001; 63])]);
        let scaled = scale_to_target_vol(&weights, &history, BaselineConfig::default()).unwrap();
        assert_eq!(scaled["US_EQUITY"], 0.2);
    }
}
