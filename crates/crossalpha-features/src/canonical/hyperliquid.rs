use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use crossalpha_storage::{ObservationEnvelope, RawSnapshotManifest};
use serde::Serialize;
use serde_json::Value;
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct HyperliquidAssetContextRow {
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
}

pub fn parse_meta_and_asset_contexts(
    envelope: &ObservationEnvelope,
    raw_record: &RawSnapshotManifest,
) -> Result<Vec<HyperliquidAssetContextRow>> {
    let payload = envelope
        .payload
        .as_array()
        .context("Hyperliquid metaAndAssetCtxs payload must be [meta, asset_contexts]")?;
    if payload.len() != 2 {
        bail!("Hyperliquid metaAndAssetCtxs payload must be [meta, asset_contexts]");
    }

    let meta = payload[0]
        .as_object()
        .context("Hyperliquid metaAndAssetCtxs payload shape is invalid")?;
    let contexts = payload[1]
        .as_array()
        .context("Hyperliquid metaAndAssetCtxs payload shape is invalid")?;
    let universe = meta
        .get("universe")
        .and_then(Value::as_array)
        .context("Hyperliquid meta.universe is missing")?;
    if universe.len() != contexts.len() {
        bail!(
            "Hyperliquid universe/context length mismatch: {} != {}",
            universe.len(),
            contexts.len()
        );
    }

    let mut rows = Vec::with_capacity(universe.len());
    let mut assets = HashSet::with_capacity(universe.len());
    for (spec_value, ctx_value) in universe.iter().zip(contexts.iter()) {
        let spec = spec_value
            .as_object()
            .context("Hyperliquid universe/context item is not an object")?;
        let ctx = ctx_value
            .as_object()
            .context("Hyperliquid universe/context item is not an object")?;
        let asset = spec
            .get("name")
            .and_then(Value::as_str)
            .context("Hyperliquid canonical asset name is missing")?
            .to_owned();
        if !assets.insert(asset.clone()) {
            bail!("Hyperliquid canonical asset names are missing or duplicated");
        }

        let impact = ctx.get("impactPxs").and_then(Value::as_array);
        let impact_bid = impact.and_then(|items| items.first()).and_then(to_float);
        let impact_ask = impact.and_then(|items| items.get(1)).and_then(to_float);

        rows.push(HyperliquidAssetContextRow {
            observed_at: envelope.observed_at,
            known_at: envelope.known_at,
            asset,
            sz_decimals: spec.get("szDecimals").cloned().unwrap_or(Value::Null),
            max_leverage: spec.get("maxLeverage").cloned().unwrap_or(Value::Null),
            only_isolated: spec.get("onlyIsolated").cloned().unwrap_or(Value::Null),
            mark_price: ctx.get("markPx").and_then(to_float),
            oracle_price: ctx.get("oraclePx").and_then(to_float),
            mid_price: ctx.get("midPx").and_then(to_float),
            prev_day_price: ctx.get("prevDayPx").and_then(to_float),
            premium: ctx.get("premium").and_then(to_float),
            funding_rate: ctx.get("funding").and_then(to_float),
            open_interest: ctx.get("openInterest").and_then(to_float),
            day_notional_volume: ctx.get("dayNtlVlm").and_then(to_float),
            day_base_volume: ctx.get("dayBaseVlm").and_then(to_float),
            impact_bid,
            impact_ask,
            raw_sha256: raw_record.sha256.clone(),
            raw_path: raw_record.path.clone(),
        });
    }

    if rows.is_empty() {
        bail!("Hyperliquid canonicalization produced zero assets");
    }
    Ok(rows)
}

fn to_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) if !text.is_empty() => text.parse::<f64>().ok(),
        _ => None,
    }
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
            source_id: "hyperliquid".to_owned(),
            observation_type: "metaAndAssetCtxs".to_owned(),
        }
    }

    fn envelope(payload: Value) -> ObservationEnvelope {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        ObservationEnvelope {
            schema_version: 1,
            event_time: None,
            observed_at: now,
            known_at: now,
            source_type: "EXCHANGE".to_owned(),
            source_id: "hyperliquid".to_owned(),
            observation_type: "metaAndAssetCtxs".to_owned(),
            payload,
            metadata: Map::new(),
        }
    }

    #[test]
    fn parses_python_contract_fields() {
        let payload = json!([
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]},
            [{
                "markPx": "100.5",
                "oraclePx": "100",
                "midPx": "100.25",
                "prevDayPx": "99",
                "premium": "0.001",
                "funding": "0.0001",
                "openInterest": "12",
                "dayNtlVlm": "1234",
                "dayBaseVlm": "5",
                "impactPxs": ["100.1", "100.4"]
            }]
        ]);
        let rows = parse_meta_and_asset_contexts(&envelope(payload), &manifest()).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].asset, "BTC");
        assert_eq!(rows[0].mark_price, Some(100.5));
        assert_eq!(rows[0].impact_bid, Some(100.1));
        assert_eq!(rows[0].impact_ask, Some(100.4));
    }

    #[test]
    fn rejects_universe_context_length_mismatch() {
        let payload = json!([{"universe": [{"name": "BTC"}]}, []]);
        assert!(parse_meta_and_asset_contexts(&envelope(payload), &manifest()).is_err());
    }
}
