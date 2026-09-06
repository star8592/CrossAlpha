use crate::HyperliquidAssetContextRow;
use chrono::{DateTime, Duration, Utc};
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;

pub const ROLLING_MIN_PERIODS: usize = 24;
pub const FEATURE_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct HyperliquidMarketStateRow {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub asset: String,
    pub sz_decimals: Value,
    pub max_leverage: Value,
    pub only_isolated: Value,
    pub mark_price: Option<f64>,
    pub oracle_price: Option<f64>,
    pub mid_price: Option<f64>,
    pub prev_day_price: Option<f64>,
    pub premium: Option<f64>,
    pub funding_rate: Option<f64>,
    pub open_interest: Option<f64>,
    pub day_notional_volume: Option<f64>,
    pub day_base_volume: Option<f64>,
    pub impact_bid: Option<f64>,
    pub impact_ask: Option<f64>,
    pub raw_sha256: String,
    pub raw_path: String,
    pub mark_oracle_basis_bps: Option<f64>,
    pub impact_spread_bps: Option<f64>,
    pub day_return: Option<f64>,
    pub funding_bps: Option<f64>,
    pub premium_bps: Option<f64>,
    pub open_interest_notional: Option<f64>,
    pub observation_interval_seconds: Option<f64>,
    pub open_interest_change_pct: Option<f64>,
    pub open_interest_notional_change_pct: Option<f64>,
    pub funding_change: Option<f64>,
    pub basis_change_bps: Option<f64>,
    pub funding_z_24h: Option<f64>,
    pub basis_z_24h: Option<f64>,
    pub oi_change_z_24h: Option<f64>,
    pub spread_z_24h: Option<f64>,
    pub rolling_observations_24h: i64,
    pub feature_schema_version: u32,
}

#[derive(Debug, Clone)]
struct Intermediate {
    source: HyperliquidAssetContextRow,
    mark_oracle_basis_bps: Option<f64>,
    impact_spread_bps: Option<f64>,
    day_return: Option<f64>,
    funding_bps: Option<f64>,
    premium_bps: Option<f64>,
    open_interest_notional: Option<f64>,
    observation_interval_seconds: Option<f64>,
    open_interest_change_pct: Option<f64>,
    open_interest_notional_change_pct: Option<f64>,
    funding_change: Option<f64>,
    basis_change_bps: Option<f64>,
}

pub fn compute_hyperliquid_market_state(
    rows: &[HyperliquidAssetContextRow],
) -> Vec<HyperliquidMarketStateRow> {
    let mut by_asset: BTreeMap<&str, Vec<HyperliquidAssetContextRow>> = BTreeMap::new();
    for row in rows {
        by_asset.entry(&row.asset).or_default().push(row.clone());
    }

    let mut result = Vec::with_capacity(rows.len());
    for (_, mut asset_rows) in by_asset {
        asset_rows.sort_by(|left, right| {
            left.observed_at
                .cmp(&right.observed_at)
                .then(left.known_at.cmp(&right.known_at))
        });

        let mut intermediate = Vec::with_capacity(asset_rows.len());
        for (index, source) in asset_rows.into_iter().enumerate() {
            let previous = index.checked_sub(1).and_then(|idx| intermediate.get(idx));
            let basis = ratio_bps(source.mark_price, positive(source.oracle_price));
            let impact_mid = average(source.impact_bid, source.impact_ask).and_then(positive_value);
            let spread = match (source.impact_bid, source.impact_ask, impact_mid) {
                (Some(bid), Some(ask), Some(mid)) => Some((ask - bid) / mid * 10_000.0),
                _ => None,
            };
            let day_return = ratio_return(source.mark_price, positive(source.prev_day_price));
            let oi_notional = multiply(source.open_interest, source.mark_price);
            let interval = previous.map(|row: &Intermediate| {
                duration_seconds(source.observed_at - row.source.observed_at)
            });
            let oi_change = previous.and_then(|row| {
                pct_change(source.open_interest, row.source.open_interest)
            });
            let oi_notional_change = previous.and_then(|row| {
                pct_change(oi_notional, row.open_interest_notional)
            });
            let funding_change = previous.and_then(|row| subtract(source.funding_rate, row.source.funding_rate));
            let basis_change = previous.and_then(|row| subtract(basis, row.mark_oracle_basis_bps));

            intermediate.push(Intermediate {
                mark_oracle_basis_bps: basis,
                impact_spread_bps: spread,
                day_return,
                funding_bps: source.funding_rate.map(|value| value * 10_000.0),
                premium_bps: source.premium.map(|value| value * 10_000.0),
                open_interest_notional: oi_notional,
                observation_interval_seconds: interval,
                open_interest_change_pct: oi_change,
                open_interest_notional_change_pct: oi_notional_change,
                funding_change,
                basis_change_bps: basis_change,
                source,
            });
        }

        for index in 0..intermediate.len() {
            let current_time = intermediate[index].source.observed_at;
            let lower = current_time - Duration::hours(24);
            let start = intermediate[..=index]
                .partition_point(|row| row.source.observed_at <= lower);
            let window = &intermediate[start..=index];
            let current = &intermediate[index];
            let rolling_observations = window
                .iter()
                .filter(|row| row.source.mark_price.is_some())
                .count() as i64;

            result.push(HyperliquidMarketStateRow {
                observed_at: current.source.observed_at,
                known_at: current.source.known_at,
                asset: current.source.asset.clone(),
                sz_decimals: current.source.sz_decimals.clone(),
                max_leverage: current.source.max_leverage.clone(),
                only_isolated: current.source.only_isolated.clone(),
                mark_price: current.source.mark_price,
                oracle_price: current.source.oracle_price,
                mid_price: current.source.mid_price,
                prev_day_price: current.source.prev_day_price,
                premium: current.source.premium,
                funding_rate: current.source.funding_rate,
                open_interest: current.source.open_interest,
                day_notional_volume: current.source.day_notional_volume,
                day_base_volume: current.source.day_base_volume,
                impact_bid: current.source.impact_bid,
                impact_ask: current.source.impact_ask,
                raw_sha256: current.source.raw_sha256.clone(),
                raw_path: current.source.raw_path.clone(),
                mark_oracle_basis_bps: current.mark_oracle_basis_bps,
                impact_spread_bps: current.impact_spread_bps,
                day_return: current.day_return,
                funding_bps: current.funding_bps,
                premium_bps: current.premium_bps,
                open_interest_notional: current.open_interest_notional,
                observation_interval_seconds: current.observation_interval_seconds,
                open_interest_change_pct: current.open_interest_change_pct,
                open_interest_notional_change_pct: current.open_interest_notional_change_pct,
                funding_change: current.funding_change,
                basis_change_bps: current.basis_change_bps,
                funding_z_24h: rolling_zscore(
                    window.iter().map(|row| row.source.funding_rate),
                    current.source.funding_rate,
                ),
                basis_z_24h: rolling_zscore(
                    window.iter().map(|row| row.mark_oracle_basis_bps),
                    current.mark_oracle_basis_bps,
                ),
                oi_change_z_24h: rolling_zscore(
                    window.iter().map(|row| row.open_interest_change_pct),
                    current.open_interest_change_pct,
                ),
                spread_z_24h: rolling_zscore(
                    window.iter().map(|row| row.impact_spread_bps),
                    current.impact_spread_bps,
                ),
                rolling_observations_24h: rolling_observations,
                feature_schema_version: FEATURE_SCHEMA_VERSION,
            });
        }
    }

    result.sort_by(|left, right| {
        left.observed_at
            .cmp(&right.observed_at)
            .then(left.asset.cmp(&right.asset))
    });
    result
}

fn rolling_zscore<I>(values: I, current: Option<f64>) -> Option<f64>
where
    I: IntoIterator<Item = Option<f64>>,
{
    let values: Vec<f64> = values.into_iter().flatten().collect();
    if values.len() < ROLLING_MIN_PERIODS {
        return None;
    }
    let current = current?;
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    let variance = values
        .iter()
        .map(|value| {
            let delta = *value - mean;
            delta * delta
        })
        .sum::<f64>()
        / values.len() as f64;
    let std = variance.sqrt();
    if std == 0.0 {
        None
    } else {
        Some((current - mean) / std)
    }
}

fn positive(value: Option<f64>) -> Option<f64> {
    value.and_then(positive_value)
}

fn positive_value(value: f64) -> Option<f64> {
    (value > 0.0).then_some(value)
}

fn average(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    left.zip(right).map(|(left, right)| (left + right) / 2.0)
}

fn ratio_bps(numerator: Option<f64>, denominator: Option<f64>) -> Option<f64> {
    numerator
        .zip(denominator)
        .map(|(numerator, denominator)| (numerator / denominator - 1.0) * 10_000.0)
}

fn ratio_return(numerator: Option<f64>, denominator: Option<f64>) -> Option<f64> {
    numerator
        .zip(denominator)
        .map(|(numerator, denominator)| numerator / denominator - 1.0)
}

fn multiply(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    left.zip(right).map(|(left, right)| left * right)
}

fn subtract(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    left.zip(right).map(|(left, right)| left - right)
}

fn pct_change(current: Option<f64>, previous: Option<f64>) -> Option<f64> {
    current
        .zip(previous)
        .map(|(current, previous)| current / previous - 1.0)
}

fn duration_seconds(duration: chrono::Duration) -> f64 {
    duration
        .num_microseconds()
        .map(|value| value as f64 / 1_000_000.0)
        .unwrap_or_else(|| duration.num_milliseconds() as f64 / 1_000.0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use serde_json::Value;

    fn row(minute: i64, funding: f64) -> HyperliquidAssetContextRow {
        let observed_at = Utc.timestamp_opt(1_700_000_000 + minute * 60, 0).unwrap();
        HyperliquidAssetContextRow {
            observed_at,
            known_at: observed_at,
            asset: "BTC".to_owned(),
            sz_decimals: Value::from(5),
            max_leverage: Value::from(40),
            only_isolated: Value::Null,
            mark_price: Some(100.0 + minute as f64),
            oracle_price: Some(100.0),
            mid_price: Some(100.0),
            prev_day_price: Some(99.0),
            premium: Some(0.001),
            funding_rate: Some(funding),
            open_interest: Some(10.0 + minute as f64),
            day_notional_volume: Some(1_000.0),
            day_base_volume: Some(10.0),
            impact_bid: Some(99.0),
            impact_ask: Some(101.0),
            raw_sha256: "sha".to_owned(),
            raw_path: "/tmp/raw".to_owned(),
        }
    }

    #[test]
    fn rolling_zscore_requires_24_known_observations() {
        let rows: Vec<_> = (0..24).map(|index| row(index, index as f64)).collect();
        let result = compute_hyperliquid_market_state(&rows);
        assert!(result[22].funding_z_24h.is_none());
        assert!(result[23].funding_z_24h.is_some());
        assert_eq!(result[23].rolling_observations_24h, 24);
    }

    #[test]
    fn window_excludes_exactly_24_hours_old_row() {
        let mut rows: Vec<_> = (0..24).map(|index| row(index, index as f64)).collect();
        rows.push(row(24 * 60, 100.0));
        let result = compute_hyperliquid_market_state(&rows);
        let latest = result.last().unwrap();
        assert_eq!(latest.rolling_observations_24h, 1);
        assert!(latest.funding_z_24h.is_none());
    }
}
