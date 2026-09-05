use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use crossalpha_storage::{ObservationEnvelope, RawSnapshotManifest};
use serde::Serialize;
use serde_json::{Map, Value};
use std::collections::HashSet;

pub const CANONICAL_STABLECOIN_SCHEMA_VERSION: u32 = 3;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct StablecoinAssetRow {
    pub canonical_schema_version: u32,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub stablecoin_id: Value,
    pub name: Value,
    pub symbol: Value,
    pub peg_type: Value,
    pub peg_mechanism: Value,
    pub price_source: Value,
    pub price_usd: Option<f64>,
    pub circulating_native: Option<f64>,
    pub circulating_prev_day_native: Option<f64>,
    pub circulating_prev_week_native: Option<f64>,
    pub circulating_prev_month_native: Option<f64>,
    pub delta_1d_native: Option<f64>,
    pub delta_7d_native: Option<f64>,
    pub delta_30d_native: Option<f64>,
    pub market_value_usd: Option<f64>,
    pub peg_deviation_bps: Option<f64>,
    pub chain_count: usize,
    pub raw_sha256: String,
    pub raw_path: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct StablecoinChainSupplyRow {
    pub canonical_schema_version: u32,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub stablecoin_id: Value,
    pub name: Value,
    pub symbol: Value,
    pub peg_type: Value,
    pub chain: String,
    pub circulating_native: Option<f64>,
    pub circulating_prev_day_native: Option<f64>,
    pub circulating_prev_week_native: Option<f64>,
    pub circulating_prev_month_native: Option<f64>,
    pub delta_1d_native: Option<f64>,
    pub delta_7d_native: Option<f64>,
    pub delta_30d_native: Option<f64>,
    pub market_value_usd: Option<f64>,
    pub price_usd: Option<f64>,
    pub raw_sha256: String,
    pub raw_path: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct StablecoinCanonicalSnapshot {
    pub assets: Vec<StablecoinAssetRow>,
    pub chains: Vec<StablecoinChainSupplyRow>,
}

pub fn parse_stablecoin_snapshot(
    envelope: &ObservationEnvelope,
    raw_record: &RawSnapshotManifest,
) -> Result<StablecoinCanonicalSnapshot> {
    let payload = envelope
        .payload
        .as_object()
        .context("DefiLlama stablecoin payload must be an object")?;
    let assets = payload
        .get("peggedAssets")
        .and_then(Value::as_array)
        .context("DefiLlama stablecoin payload.peggedAssets is missing")?;

    let mut asset_rows = Vec::new();
    let mut chain_rows = Vec::new();
    let mut ids = HashSet::new();

    for item_value in assets {
        let Some(item) = item_value.as_object() else {
            continue;
        };
        let stablecoin_id = item.get("id").cloned().unwrap_or(Value::Null);
        if stablecoin_id.is_null() {
            bail!("stablecoin ids are missing or duplicated");
        }
        let id_key = stablecoin_id.to_string();
        if !ids.insert(id_key) {
            bail!("stablecoin ids are missing or duplicated");
        }

        let name = item.get("name").cloned().unwrap_or(Value::Null);
        let symbol = item.get("symbol").cloned().unwrap_or(Value::Null);
        let peg_type = item.get("pegType").cloned().unwrap_or(Value::Null);
        let peg_type_str = peg_type.as_str();
        let price_usd = item.get("price").and_then(to_float);
        let current = peg_amount(item.get("circulating"), peg_type_str);
        let prev_day = peg_amount(item.get("circulatingPrevDay"), peg_type_str);
        let prev_week = peg_amount(item.get("circulatingPrevWeek"), peg_type_str);
        let prev_month = peg_amount(item.get("circulatingPrevMonth"), peg_type_str);
        let market_value_usd = multiply(current, price_usd);
        let peg_deviation_bps = if peg_type_str == Some("peggedUSD") {
            price_usd.map(|price| (price - 1.0) * 10_000.0)
        } else {
            None
        };

        let chain_map = item
            .get("chainCirculating")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_else(Map::new);

        asset_rows.push(StablecoinAssetRow {
            canonical_schema_version: CANONICAL_STABLECOIN_SCHEMA_VERSION,
            observed_at: envelope.observed_at,
            known_at: envelope.known_at,
            stablecoin_id: stablecoin_id.clone(),
            name: name.clone(),
            symbol: symbol.clone(),
            peg_type: peg_type.clone(),
            peg_mechanism: item.get("pegMechanism").cloned().unwrap_or(Value::Null),
            price_source: item.get("priceSource").cloned().unwrap_or(Value::Null),
            price_usd,
            circulating_native: current,
            circulating_prev_day_native: prev_day,
            circulating_prev_week_native: prev_week,
            circulating_prev_month_native: prev_month,
            delta_1d_native: subtract(current, prev_day),
            delta_7d_native: subtract(current, prev_week),
            delta_30d_native: subtract(current, prev_month),
            market_value_usd,
            peg_deviation_bps,
            chain_count: chain_map.len(),
            raw_sha256: raw_record.sha256.clone(),
            raw_path: raw_record.path.clone(),
        });

        for (chain, chain_value) in chain_map {
            let chain_current = chain_measure(&chain_value, "circulating", peg_type_str);
            let chain_prev_day = chain_measure(&chain_value, "circulatingPrevDay", peg_type_str);
            let chain_prev_week = chain_measure(&chain_value, "circulatingPrevWeek", peg_type_str);
            let chain_prev_month = chain_measure(&chain_value, "circulatingPrevMonth", peg_type_str);
            chain_rows.push(StablecoinChainSupplyRow {
                canonical_schema_version: CANONICAL_STABLECOIN_SCHEMA_VERSION,
                observed_at: envelope.observed_at,
                known_at: envelope.known_at,
                stablecoin_id: stablecoin_id.clone(),
                name: name.clone(),
                symbol: symbol.clone(),
                peg_type: peg_type.clone(),
                chain,
                circulating_native: chain_current,
                circulating_prev_day_native: chain_prev_day,
                circulating_prev_week_native: chain_prev_week,
                circulating_prev_month_native: chain_prev_month,
                delta_1d_native: subtract(chain_current, chain_prev_day),
                delta_7d_native: subtract(chain_current, chain_prev_week),
                delta_30d_native: subtract(chain_current, chain_prev_month),
                market_value_usd: multiply(chain_current, price_usd),
                price_usd,
                raw_sha256: raw_record.sha256.clone(),
                raw_path: raw_record.path.clone(),
            });
        }
    }

    if asset_rows.is_empty() {
        bail!("stablecoin canonicalization produced zero assets");
    }
    if !chain_rows.is_empty() && chain_rows.iter().all(|row| row.circulating_native.is_none()) {
        bail!(
            "stablecoin chainCirculating rows exist but no current amounts were parsed; upstream schema likely changed"
        );
    }

    Ok(StablecoinCanonicalSnapshot {
        assets: asset_rows,
        chains: chain_rows,
    })
}

fn to_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) if !text.is_empty() => text.parse::<f64>().ok(),
        _ => None,
    }
}

fn peg_amount(value: Option<&Value>, peg_type: Option<&str>) -> Option<f64> {
    let value = value?;
    if let Some(object) = value.as_object() {
        if let Some(peg_type) = peg_type
            && let Some(value) = object.get(peg_type)
        {
            return to_float(value);
        }
        let mut numeric = object.values().filter_map(to_float);
        let first = numeric.next()?;
        if numeric.next().is_none() {
            return Some(first);
        }
        return None;
    }
    to_float(value)
}

fn chain_measure(chain_value: &Value, field: &str, peg_type: Option<&str>) -> Option<f64> {
    let object = chain_value.as_object()?;
    let upstream_field = if field == "circulating" {
        "current"
    } else {
        field
    };
    if let Some(value) = object.get(upstream_field) {
        return peg_amount(Some(value), peg_type);
    }
    if let Some(value) = object.get(field) {
        return peg_amount(Some(value), peg_type);
    }
    if field == "circulating" {
        return peg_amount(Some(chain_value), peg_type);
    }
    None
}

fn subtract(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    left.zip(right).map(|(left, right)| left - right)
}

fn multiply(left: Option<f64>, right: Option<f64>) -> Option<f64> {
    left.zip(right).map(|(left, right)| left * right)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use serde_json::{Map, json};

    fn manifest() -> RawSnapshotManifest {
        RawSnapshotManifest {
            path: "/tmp/raw.json.gz".to_owned(),
            sha256: "abc123".to_owned(),
            bytes: 1,
            compressed_bytes: Some(1),
            observed_at: Utc.timestamp_opt(1_700_000_000, 0).unwrap(),
            source_id: "defillama".to_owned(),
            observation_type: "stablecoins_snapshot".to_owned(),
        }
    }

    fn envelope(payload: Value) -> ObservationEnvelope {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        ObservationEnvelope {
            schema_version: 1,
            event_time: None,
            observed_at: now,
            known_at: now,
            source_type: "AGGREGATOR".to_owned(),
            source_id: "defillama".to_owned(),
            observation_type: "stablecoins_snapshot".to_owned(),
            payload,
            metadata: Map::new(),
        }
    }

    #[test]
    fn parses_current_chain_shape_and_deltas() {
        let payload = json!({
            "peggedAssets": [{
                "id": "1",
                "name": "USD Coin",
                "symbol": "USDC",
                "pegType": "peggedUSD",
                "price": 1.001,
                "circulating": {"peggedUSD": 100.0},
                "circulatingPrevDay": {"peggedUSD": 90.0},
                "circulatingPrevWeek": {"peggedUSD": 80.0},
                "circulatingPrevMonth": {"peggedUSD": 70.0},
                "chainCirculating": {
                    "Ethereum": {
                        "current": {"peggedUSD": 60.0},
                        "circulatingPrevDay": {"peggedUSD": 55.0}
                    }
                }
            }]
        });
        let parsed = parse_stablecoin_snapshot(&envelope(payload), &manifest()).unwrap();
        assert_eq!(parsed.assets.len(), 1);
        assert_eq!(parsed.chains.len(), 1);
        assert_eq!(parsed.assets[0].delta_1d_native, Some(10.0));
        assert_eq!(parsed.assets[0].peg_deviation_bps, Some(9.999999999998899));
        assert_eq!(parsed.chains[0].circulating_native, Some(60.0));
        assert_eq!(parsed.chains[0].delta_1d_native, Some(5.0));
    }

    #[test]
    fn peg_amount_only_falls_back_when_one_numeric_value_exists() {
        assert_eq!(
            peg_amount(Some(&json!({"other": "12"})), Some("peggedUSD")),
            Some(12.0)
        );
        assert_eq!(
            peg_amount(Some(&json!({"a": "1", "b": "2"})), None),
            None
        );
    }
}
