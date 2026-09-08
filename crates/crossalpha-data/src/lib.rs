pub mod free_core;
pub mod free_returns;

pub use free_core::{
    CashRateRow, FRED_CASH_SERIES, FREE_CRYPTO_PROXIES, FREE_TRADFI_PROXIES, FreeCoreProvider,
    FreeCoreRange, ProxyDailyRow, parse_binance_payload, parse_fred_payload, parse_tiingo_payload,
    validate_fred_key, validate_tiingo_token, write_free_core_fixture_canonical,
};
pub use free_returns::{
    AssetReturnRow, build_free_core_returns, canonical_paths, read_asset_returns,
};

use anyhow::{Result, bail};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ParentBar {
    pub ts_event: DateTime<Utc>,
    pub instrument_id: u64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct InstrumentDefinition {
    pub ts_recv: DateTime<Utc>,
    pub instrument_id: u64,
    pub raw_symbol: String,
    pub expiration: DateTime<Utc>,
    pub instrument_class: String,
    #[serde(default)]
    pub asset: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NormalizedFutureBar {
    pub date: DateTime<Utc>,
    pub contract: String,
    pub instrument_id: u64,
    pub expiration_date: DateTime<Utc>,
    pub definition_known_at: DateTime<Utc>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
    pub asset: Option<String>,
}

pub fn normalize_parent_futures_daily(
    bars: &[ParentBar],
    definitions: &[InstrumentDefinition],
) -> Result<Vec<NormalizedFutureBar>> {
    let mut defs: BTreeMap<u64, Vec<&InstrumentDefinition>> = BTreeMap::new();
    for definition in definitions {
        defs.entry(definition.instrument_id)
            .or_default()
            .push(definition);
    }
    for rows in defs.values_mut() {
        rows.sort_by_key(|row| row.ts_recv);
    }

    let mut seen = BTreeSet::new();
    let mut result = Vec::new();
    for bar in bars {
        if !seen.insert((bar.ts_event, bar.instrument_id)) {
            bail!("parent bars contain duplicate timestamp/instrument_id rows");
        }
        for value in [bar.open, bar.high, bar.low, bar.close] {
            if !value.is_finite() {
                bail!("normalized futures contain missing OHLC values");
            }
        }
        if !bar.volume.is_finite() || bar.volume < 0.0 {
            bail!("normalized futures contain negative volume");
        }
        let Some(candidates) = defs.get(&bar.instrument_id) else {
            continue;
        };
        let index = candidates.partition_point(|definition| definition.ts_recv <= bar.ts_event);
        if index == 0 {
            continue;
        }
        let definition = candidates[index - 1];
        if class_code(&definition.instrument_class) != "F" {
            continue;
        }
        if definition.raw_symbol.is_empty() {
            continue;
        }
        result.push(NormalizedFutureBar {
            date: bar.ts_event,
            contract: definition.raw_symbol.clone(),
            instrument_id: bar.instrument_id,
            expiration_date: definition.expiration,
            definition_known_at: definition.ts_recv,
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
            asset: definition.asset.clone(),
        });
    }
    if result.is_empty() {
        bail!("no outright futures remained after definition join");
    }
    result.sort_by(|left, right| {
        left.date
            .cmp(&right.date)
            .then(left.contract.cmp(&right.contract))
    });
    let mut normalized_seen = BTreeSet::new();
    for row in &result {
        if !normalized_seen.insert((row.date, row.contract.clone())) {
            bail!("normalized futures contain duplicate date/contract rows");
        }
        if row.definition_known_at > row.date {
            bail!("point-in-time definition join used future metadata");
        }
    }
    Ok(result)
}

pub fn assert_stable_expiration(rows: &[NormalizedFutureBar]) -> Result<()> {
    let mut expirations: BTreeMap<&str, DateTime<Utc>> = BTreeMap::new();
    for row in rows {
        if let Some(previous) = expirations.insert(&row.contract, row.expiration_date)
            && previous != row.expiration_date
        {
            bail!("contract {} has changing expiration metadata", row.contract);
        }
    }
    Ok(())
}

fn class_code(value: &str) -> &str {
    if value.ends_with(".FUTURE") {
        "F"
    } else {
        value
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn asof_join_never_uses_future_definition_revision() {
        let bar_time = Utc.with_ymd_and_hms(2026, 1, 2, 0, 0, 0).unwrap();
        let defs = vec![
            InstrumentDefinition {
                ts_recv: Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap(),
                instrument_id: 1,
                raw_symbol: "F1".into(),
                expiration: Utc.with_ymd_and_hms(2026, 2, 1, 0, 0, 0).unwrap(),
                instrument_class: "F".into(),
                asset: Some("WTI".into()),
            },
            InstrumentDefinition {
                ts_recv: Utc.with_ymd_and_hms(2026, 1, 3, 0, 0, 0).unwrap(),
                instrument_id: 1,
                raw_symbol: "F1-REVISED".into(),
                expiration: Utc.with_ymd_and_hms(2026, 3, 1, 0, 0, 0).unwrap(),
                instrument_class: "F".into(),
                asset: Some("WTI".into()),
            },
        ];
        let bars = vec![ParentBar {
            ts_event: bar_time,
            instrument_id: 1,
            open: 1.0,
            high: 1.0,
            low: 1.0,
            close: 1.0,
            volume: 10.0,
        }];
        let rows = normalize_parent_futures_daily(&bars, &defs).unwrap();
        assert_eq!(rows[0].contract, "F1");
        assert!(rows[0].definition_known_at <= rows[0].date);
    }
}
