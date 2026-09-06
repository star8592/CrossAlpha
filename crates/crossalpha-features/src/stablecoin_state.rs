use crate::{StablecoinAssetRow, StablecoinChainSupplyRow};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::Value;
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};

pub const STABLECOIN_FEATURE_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct StablecoinSystemStateRow {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub usd_stablecoin_count: i64,
    pub usd_supply_native: f64,
    pub usd_market_value_usd: f64,
    pub usd_delta_1d_native: Option<f64>,
    pub usd_delta_7d_native: Option<f64>,
    pub usd_delta_30d_native: Option<f64>,
    pub delta_1d_market_value_coverage: Option<f64>,
    pub delta_7d_market_value_coverage: Option<f64>,
    pub delta_30d_market_value_coverage: Option<f64>,
    pub usdt_market_value_usd: Option<f64>,
    pub usdc_market_value_usd: Option<f64>,
    pub usdt_share: Option<f64>,
    pub usdc_share: Option<f64>,
    pub asset_hhi: Option<f64>,
    pub weighted_abs_peg_deviation_bps: Option<f64>,
    pub max_abs_peg_deviation_bps: Option<f64>,
    pub offpeg_50bps_market_value_usd: f64,
    pub chain_sum_native: f64,
    pub chain_coverage_ratio: Option<f64>,
    pub chain_residual_native: f64,
    pub chain_abs_residual_native: f64,
    pub chain_abs_residual_ratio: Option<f64>,
    pub feature_schema_version: u32,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct StablecoinChainStateRow {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub chain: String,
    pub circulating_native: Option<f64>,
    pub market_value_usd: Option<f64>,
    pub market_share: Option<f64>,
    pub stablecoin_count: i64,
    pub system_chain_hhi: Option<f64>,
    pub feature_schema_version: u32,
}

#[derive(Debug, Clone)]
struct AssetAccounting<'a> {
    row: &'a StablecoinAssetRow,
    chain_sum_native: f64,
    chain_residual_native: Option<f64>,
}

#[derive(Debug, Clone)]
struct ChainAggregate {
    known_at: DateTime<Utc>,
    circulating_native: Option<f64>,
    market_value_usd: Option<f64>,
    stablecoin_ids: BTreeSet<String>,
}

pub fn compute_stablecoin_system_state(
    assets: &[StablecoinAssetRow],
    chains: &[StablecoinChainSupplyRow],
) -> (Vec<StablecoinSystemStateRow>, Vec<StablecoinChainStateRow>) {
    let usd_assets: Vec<&StablecoinAssetRow> = assets
        .iter()
        .filter(|row| row.peg_type.as_str() == Some("peggedUSD"))
        .collect();
    let usd_chains: Vec<&StablecoinChainSupplyRow> = chains
        .iter()
        .filter(|row| row.peg_type.as_str() == Some("peggedUSD"))
        .collect();

    let mut chain_by_asset: BTreeMap<(DateTime<Utc>, String), Vec<Option<f64>>> = BTreeMap::new();
    for row in &usd_chains {
        chain_by_asset
            .entry((row.observed_at, value_key(&row.stablecoin_id)))
            .or_default()
            .push(row.circulating_native);
    }

    let mut assets_by_time: BTreeMap<DateTime<Utc>, Vec<AssetAccounting<'_>>> = BTreeMap::new();
    for row in usd_assets {
        let chain_sum = sum_min_count_one(
            chain_by_asset
                .get(&(row.observed_at, value_key(&row.stablecoin_id)))
                .map(Vec::as_slice)
                .unwrap_or(&[]),
        )
        .unwrap_or(0.0);
        let residual = row
            .circulating_native
            .map(|supply| chain_sum - supply);
        assets_by_time
            .entry(row.observed_at)
            .or_default()
            .push(AssetAccounting {
                row,
                chain_sum_native: chain_sum,
                chain_residual_native: residual,
            });
    }

    let mut system_rows = Vec::with_capacity(assets_by_time.len());
    for (observed_at, part) in assets_by_time {
        if part.is_empty() {
            continue;
        }
        let total_market_value = part
            .iter()
            .map(|item| item.row.market_value_usd.unwrap_or(0.0))
            .sum::<f64>();
        let total_supply_native = part
            .iter()
            .map(|item| item.row.circulating_native.unwrap_or(0.0))
            .sum::<f64>();
        let chain_sum_native = part.iter().map(|item| item.chain_sum_native).sum::<f64>();
        let residual_native = chain_sum_native - total_supply_native;
        let abs_residual_native = part
            .iter()
            .map(|item| item.chain_residual_native.map(f64::abs).unwrap_or(0.0))
            .sum::<f64>();

        let usdt = symbol_value(&part, "USDT");
        let usdc = symbol_value(&part, "USDC");
        let weighted_abs_peg = weighted_abs_peg(&part);
        let max_abs_peg = part
            .iter()
            .filter_map(|item| item.row.peg_deviation_bps.map(f64::abs))
            .reduce(f64::max);
        let offpeg_50 = part
            .iter()
            .filter(|item| {
                item.row
                    .peg_deviation_bps
                    .map(|value| value.abs() >= 50.0)
                    .unwrap_or(false)
            })
            .map(|item| item.row.market_value_usd.unwrap_or(0.0))
            .sum::<f64>();
        let known_at = part
            .iter()
            .map(|item| item.row.known_at)
            .max()
            .expect("part is non-empty");
        let stablecoin_count = part
            .iter()
            .map(|item| value_key(&item.row.stablecoin_id))
            .collect::<BTreeSet<_>>()
            .len() as i64;

        system_rows.push(StablecoinSystemStateRow {
            observed_at,
            known_at,
            usd_stablecoin_count: stablecoin_count,
            usd_supply_native: total_supply_native,
            usd_market_value_usd: total_market_value,
            usd_delta_1d_native: known_sum(part.iter().map(|item| item.row.delta_1d_native)),
            usd_delta_7d_native: known_sum(part.iter().map(|item| item.row.delta_7d_native)),
            usd_delta_30d_native: known_sum(part.iter().map(|item| item.row.delta_30d_native)),
            delta_1d_market_value_coverage: market_value_coverage(
                &part,
                |row| row.delta_1d_native,
                total_market_value,
            ),
            delta_7d_market_value_coverage: market_value_coverage(
                &part,
                |row| row.delta_7d_native,
                total_market_value,
            ),
            delta_30d_market_value_coverage: market_value_coverage(
                &part,
                |row| row.delta_30d_native,
                total_market_value,
            ),
            usdt_market_value_usd: usdt,
            usdc_market_value_usd: usdc,
            usdt_share: share(usdt, total_market_value),
            usdc_share: share(usdc, total_market_value),
            asset_hhi: hhi(part.iter().map(|item| item.row.market_value_usd)),
            weighted_abs_peg_deviation_bps: weighted_abs_peg,
            max_abs_peg_deviation_bps: max_abs_peg,
            offpeg_50bps_market_value_usd: offpeg_50,
            chain_sum_native,
            chain_coverage_ratio: positive_ratio(chain_sum_native, total_supply_native),
            chain_residual_native: residual_native,
            chain_abs_residual_native: abs_residual_native,
            chain_abs_residual_ratio: positive_ratio(abs_residual_native, total_supply_native),
            feature_schema_version: STABLECOIN_FEATURE_SCHEMA_VERSION,
        });
    }

    let mut chain_groups: BTreeMap<(DateTime<Utc>, String), ChainAggregate> = BTreeMap::new();
    for row in usd_chains {
        let key = (row.observed_at, row.chain.clone());
        let entry = chain_groups.entry(key).or_insert_with(|| ChainAggregate {
            known_at: row.known_at,
            circulating_native: None,
            market_value_usd: None,
            stablecoin_ids: BTreeSet::new(),
        });
        entry.known_at = entry.known_at.max(row.known_at);
        entry.circulating_native = add_min_count_one(entry.circulating_native, row.circulating_native);
        entry.market_value_usd = add_min_count_one(entry.market_value_usd, row.market_value_usd);
        entry.stablecoin_ids.insert(value_key(&row.stablecoin_id));
    }

    let mut by_time: BTreeMap<DateTime<Utc>, Vec<(String, ChainAggregate)>> = BTreeMap::new();
    for ((observed_at, chain), aggregate) in chain_groups {
        by_time
            .entry(observed_at)
            .or_default()
            .push((chain, aggregate));
    }

    let mut chain_state = Vec::new();
    for (observed_at, mut groups) in by_time {
        let total_market = groups
            .iter()
            .map(|(_, aggregate)| aggregate.market_value_usd.unwrap_or(0.0))
            .sum::<f64>();
        let chain_hhi = hhi(groups.iter().map(|(_, aggregate)| aggregate.market_value_usd));
        groups.sort_by(|left, right| descending_optional(left.1.market_value_usd, right.1.market_value_usd));
        for (chain, aggregate) in groups {
            chain_state.push(StablecoinChainStateRow {
                observed_at,
                known_at: aggregate.known_at,
                chain,
                circulating_native: aggregate.circulating_native,
                market_value_usd: aggregate.market_value_usd,
                market_share: share(aggregate.market_value_usd, total_market),
                stablecoin_count: aggregate.stablecoin_ids.len() as i64,
                system_chain_hhi: chain_hhi,
                feature_schema_version: STABLECOIN_FEATURE_SCHEMA_VERSION,
            });
        }
    }

    system_rows.sort_by_key(|row| row.observed_at);
    chain_state.sort_by(|left, right| {
        left.observed_at.cmp(&right.observed_at).then_with(|| {
            descending_optional(left.market_value_usd, right.market_value_usd)
        })
    });
    (system_rows, chain_state)
}

fn value_key(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        _ => value.to_string(),
    }
}

fn sum_min_count_one(values: &[Option<f64>]) -> Option<f64> {
    known_sum(values.iter().copied())
}

fn add_min_count_one(current: Option<f64>, value: Option<f64>) -> Option<f64> {
    match (current, value) {
        (Some(current), Some(value)) => Some(current + value),
        (Some(current), None) => Some(current),
        (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

fn known_sum<I>(values: I) -> Option<f64>
where
    I: IntoIterator<Item = Option<f64>>,
{
    let mut count = 0_usize;
    let mut total = 0.0;
    for value in values.into_iter().flatten() {
        count += 1;
        total += value;
    }
    (count > 0).then_some(total)
}

fn symbol_value(part: &[AssetAccounting<'_>], symbol: &str) -> Option<f64> {
    known_sum(part.iter().filter_map(|item| {
        (item.row.symbol.as_str() == Some(symbol)).then_some(item.row.market_value_usd)
    }))
}

fn weighted_abs_peg(part: &[AssetAccounting<'_>]) -> Option<f64> {
    let mut weighted = 0.0;
    let mut weight = 0.0;
    for item in part {
        let Some(deviation) = item.row.peg_deviation_bps else {
            continue;
        };
        let market_value = item.row.market_value_usd.unwrap_or(0.0);
        weighted += deviation.abs() * market_value;
        weight += market_value;
    }
    (weight > 0.0).then_some(weighted / weight)
}

fn market_value_coverage<F>(
    part: &[AssetAccounting<'_>],
    value: F,
    total_market_value: f64,
) -> Option<f64>
where
    F: Fn(&StablecoinAssetRow) -> Option<f64>,
{
    if total_market_value <= 0.0 {
        return None;
    }
    let covered = part
        .iter()
        .filter(|item| value(item.row).is_some())
        .map(|item| item.row.market_value_usd.unwrap_or(0.0))
        .sum::<f64>();
    Some(covered / total_market_value)
}

fn hhi<I>(values: I) -> Option<f64>
where
    I: IntoIterator<Item = Option<f64>>,
{
    let values: Vec<f64> = values
        .into_iter()
        .flatten()
        .filter(|value| *value > 0.0)
        .collect();
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

fn share(value: Option<f64>, total: f64) -> Option<f64> {
    value.filter(|_| total > 0.0).map(|value| value / total)
}

fn positive_ratio(numerator: f64, denominator: f64) -> Option<f64> {
    (denominator > 0.0).then_some(numerator / denominator)
}

fn descending_optional(left: Option<f64>, right: Option<f64>) -> Ordering {
    match (left, right) {
        (Some(left), Some(right)) => right.partial_cmp(&left).unwrap_or(Ordering::Equal),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => Ordering::Equal,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn asset(id: &str, symbol: &str, market: f64, delta: Option<f64>) -> StablecoinAssetRow {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        StablecoinAssetRow {
            canonical_schema_version: 3,
            observed_at: now,
            known_at: now,
            stablecoin_id: Value::String(id.to_owned()),
            name: Value::String(symbol.to_owned()),
            symbol: Value::String(symbol.to_owned()),
            peg_type: Value::String("peggedUSD".to_owned()),
            peg_mechanism: Value::Null,
            price_source: Value::Null,
            price_usd: Some(1.0),
            circulating_native: Some(market),
            circulating_prev_day_native: None,
            circulating_prev_week_native: None,
            circulating_prev_month_native: None,
            delta_1d_native: delta,
            delta_7d_native: None,
            delta_30d_native: None,
            market_value_usd: Some(market),
            peg_deviation_bps: Some(0.0),
            chain_count: 0,
            raw_sha256: "sha".to_owned(),
            raw_path: "/tmp/raw".to_owned(),
        }
    }

    #[test]
    fn aggregates_usd_assets_without_forcing_missing_deltas_to_zero() {
        let assets = vec![
            asset("1", "USDT", 60.0, Some(3.0)),
            asset("2", "USDC", 40.0, None),
        ];
        let (system, chains) = compute_stablecoin_system_state(&assets, &[]);
        assert!(chains.is_empty());
        assert_eq!(system.len(), 1);
        assert_eq!(system[0].usd_market_value_usd, 100.0);
        assert_eq!(system[0].usd_delta_1d_native, Some(3.0));
        assert_eq!(system[0].delta_1d_market_value_coverage, Some(0.6));
        assert_eq!(system[0].usdt_share, Some(0.6));
        assert_eq!(system[0].usdc_share, Some(0.4));
        assert_eq!(system[0].chain_sum_native, 0.0);
        assert_eq!(system[0].chain_residual_native, -100.0);
    }
}
