use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use crossalpha_storage::{ObservationEnvelope, RawSnapshotManifest};
use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AaveMarketRow {
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub chain_id: Option<i64>,
    pub market_address: Option<String>,
    pub market_name: Option<String>,
    pub reserve_address: Option<String>,
    pub symbol: Option<String>,
    pub decimals: Option<i64>,
    pub supply_apy_pct: Option<f64>,
    pub borrow_apy_pct: Option<f64>,
    pub available_liquidity_native: Option<f64>,
    pub available_liquidity_usd: Option<f64>,
    pub borrow_cap_reached: bool,
    pub is_frozen: bool,
    pub is_paused: bool,
    pub raw_sha256: String,
    pub raw_path: String,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct AaveLiquidationRow {
    pub event_time: Option<DateTime<Utc>>,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub block_number: Option<u64>,
    pub transaction_hash: Option<String>,
    pub transaction_index: Option<u64>,
    pub log_index: Option<u64>,
    pub block_hash: Option<String>,
    pub removed: bool,
    pub collateral_asset: Option<String>,
    pub debt_asset: Option<String>,
    pub user: Option<String>,
    /// Decimal uint256 text. State V0.2 never treats raw token units as floating point.
    pub debt_to_cover_raw: Option<String>,
    /// Decimal uint256 text. State V0.2 never treats raw token units as floating point.
    pub liquidated_collateral_amount_raw: Option<String>,
    pub liquidator: Option<String>,
    pub receive_atoken: Option<bool>,
    pub raw_sha256: String,
    pub raw_path: String,
}

pub fn parse_aave_markets(
    envelope: &ObservationEnvelope,
    raw_record: &RawSnapshotManifest,
) -> Result<Vec<AaveMarketRow>> {
    let payload = envelope
        .payload
        .as_object()
        .context("Aave markets payload must be an object")?;
    let markets = payload
        .get("data")
        .and_then(Value::as_object)
        .and_then(|data| data.get("markets"))
        .and_then(Value::as_array)
        .context("Aave markets payload has no markets")?;
    if markets.is_empty() {
        bail!("Aave markets payload has no markets");
    }

    let chain_id = envelope.metadata.get("chain_id").and_then(value_i64);
    let mut rows = Vec::new();
    for market in markets {
        let Some(market) = market.as_object() else {
            continue;
        };
        let reserves = market
            .get("reserves")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        for reserve in reserves {
            let Some(reserve) = reserve.as_object() else {
                continue;
            };
            let token = reserve.get("underlyingToken").and_then(Value::as_object);
            let supply = reserve.get("supplyInfo").and_then(Value::as_object);
            let borrow = reserve.get("borrowInfo").and_then(Value::as_object);
            let supply_apy = supply
                .and_then(|value| value.get("apy"))
                .and_then(Value::as_object)
                .and_then(|value| value.get("formatted"))
                .and_then(to_float);
            let borrow_apy = borrow
                .and_then(|value| value.get("apy"))
                .and_then(Value::as_object)
                .and_then(|value| value.get("formatted"))
                .and_then(to_float);
            let available = borrow
                .and_then(|value| value.get("availableLiquidity"))
                .and_then(Value::as_object);
            let native = available
                .and_then(|value| value.get("amount"))
                .and_then(Value::as_object)
                .and_then(|value| value.get("value"))
                .and_then(to_float);
            let usd = available
                .and_then(|value| value.get("usd"))
                .and_then(to_float);

            rows.push(AaveMarketRow {
                observed_at: envelope.observed_at,
                known_at: envelope.known_at,
                chain_id,
                market_address: market
                    .get("address")
                    .and_then(Value::as_str)
                    .map(|value| value.to_ascii_lowercase()),
                market_name: market
                    .get("name")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                reserve_address: token
                    .and_then(|value| value.get("address"))
                    .and_then(Value::as_str)
                    .map(|value| value.to_ascii_lowercase()),
                symbol: token
                    .and_then(|value| value.get("symbol"))
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                decimals: token
                    .and_then(|value| value.get("decimals"))
                    .and_then(value_i64),
                supply_apy_pct: supply_apy,
                borrow_apy_pct: borrow_apy,
                available_liquidity_native: native,
                available_liquidity_usd: usd,
                borrow_cap_reached: borrow
                    .and_then(|value| value.get("borrowCapReached"))
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                is_frozen: reserve
                    .get("isFrozen")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                is_paused: reserve
                    .get("isPaused")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                raw_sha256: raw_record.sha256.clone(),
                raw_path: raw_record.path.clone(),
            });
        }
    }
    if rows.is_empty() {
        bail!("Aave markets canonicalization produced zero reserves");
    }
    if rows.iter().all(|row| row.symbol.is_none()) {
        bail!("Aave markets canonicalization produced no reserve symbols");
    }
    Ok(rows)
}

pub fn parse_aave_liquidations(
    envelope: &ObservationEnvelope,
    raw_record: &RawSnapshotManifest,
) -> Result<Vec<AaveLiquidationRow>> {
    let payload = envelope
        .payload
        .as_array()
        .context("Aave liquidation payload must be a list")?;
    let mut rows = Vec::new();
    for item in payload {
        let Some(item) = item.as_object() else {
            continue;
        };
        let topics = item
            .get("topics")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if topics.len() < 4 {
            continue;
        }
        let words = data_words(item.get("data"));
        let removed = item
            .get("removed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let event_time = if removed {
            None
        } else {
            item.get("blockTimestamp")
                .and_then(Value::as_str)
                .and_then(parse_datetime)
        };
        rows.push(AaveLiquidationRow {
            event_time,
            observed_at: envelope.observed_at,
            known_at: envelope.known_at,
            block_number: item.get("blockNumber").and_then(hex_u64),
            transaction_hash: item
                .get("transactionHash")
                .and_then(Value::as_str)
                .map(str::to_owned),
            transaction_index: item.get("transactionIndex").and_then(hex_u64),
            log_index: item.get("logIndex").and_then(hex_u64),
            block_hash: item
                .get("blockHash")
                .and_then(Value::as_str)
                .map(str::to_owned),
            removed,
            collateral_asset: topic_address(topics.get(1)),
            debt_asset: topic_address(topics.get(2)),
            user: topic_address(topics.get(3)),
            debt_to_cover_raw: words.first().and_then(|word| hex_uint_decimal(word)),
            liquidated_collateral_amount_raw: words.get(1).and_then(|word| hex_uint_decimal(word)),
            liquidator: words.get(2).map(|word| {
                format!("0x{}", &word[word.len().saturating_sub(40)..]).to_ascii_lowercase()
            }),
            receive_atoken: words.get(3).and_then(|word| {
                u8::from_str_radix(&word[word.len().saturating_sub(2)..], 16)
                    .ok()
                    .map(|value| value != 0)
            }),
            raw_sha256: raw_record.sha256.clone(),
            raw_path: raw_record.path.clone(),
        });
    }
    Ok(rows)
}

fn to_float(value: &Value) -> Option<f64> {
    let number = if let Some(number) = value.as_f64() {
        number
    } else if let Some(text) = value.as_str() {
        text.trim()
            .replace('%', "")
            .replace(',', "")
            .parse::<f64>()
            .ok()?
    } else {
        return None;
    };
    number.is_finite().then_some(number)
}

fn value_i64(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
        .or_else(|| value.as_str().and_then(|value| value.parse::<i64>().ok()))
}

fn hex_u64(value: &Value) -> Option<u64> {
    let text = value.as_str()?.strip_prefix("0x")?;
    u64::from_str_radix(text, 16).ok()
}

fn topic_address(value: Option<&Value>) -> Option<String> {
    let text = value?.as_str()?;
    let raw = text.strip_prefix("0x")?;
    if raw.len() < 40 || !raw.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    Some(format!("0x{}", &raw[raw.len() - 40..]).to_ascii_lowercase())
}

fn data_words(value: Option<&Value>) -> Vec<String> {
    let Some(raw) = value
        .and_then(Value::as_str)
        .and_then(|value| value.strip_prefix("0x"))
    else {
        return Vec::new();
    };
    raw.as_bytes()
        .chunks(64)
        .filter(|chunk| chunk.len() == 64)
        .filter_map(|chunk| std::str::from_utf8(chunk).ok().map(str::to_owned))
        .collect()
}

fn parse_datetime(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|value| value.with_timezone(&Utc))
}

fn hex_uint_decimal(word: &str) -> Option<String> {
    let value = num_bigint::BigUint::parse_bytes(word.as_bytes(), 16)?;
    Some(value.to_str_radix(10))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use crossalpha_storage::ObservationEnvelope;
    use serde_json::json;

    fn record() -> RawSnapshotManifest {
        RawSnapshotManifest {
            path: "/tmp/aave.json.gz".to_owned(),
            sha256: "abc".to_owned(),
            bytes: 10,
            compressed_bytes: Some(5),
            observed_at: Utc.timestamp_opt(1_700_000_000, 0).unwrap(),
            source_id: "aave:v3:graphql".to_owned(),
            observation_type: "markets_snapshot".to_owned(),
        }
    }

    #[test]
    fn parses_market_reserve_fields_without_schema_drift() {
        let at = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let envelope = ObservationEnvelope {
            schema_version: 1,
            event_time: None,
            observed_at: at,
            known_at: at,
            source_type: "AGGREGATOR".to_owned(),
            source_id: "aave:v3:graphql".to_owned(),
            observation_type: "markets_snapshot".to_owned(),
            payload: json!({"data":{"markets":[{"address":"0xABC","name":"Core","reserves":[{"underlyingToken":{"address":"0xDEF","symbol":"WETH","decimals":18},"supplyInfo":{"apy":{"formatted":"2.0"}},"borrowInfo":{"apy":{"formatted":"5.0"},"availableLiquidity":{"amount":{"value":"12"},"usd":"100"},"borrowCapReached":false},"isFrozen":false,"isPaused":false}]}]}}),
            metadata: serde_json::Map::from_iter([("chain_id".to_owned(), json!(1))]),
        };
        let rows = parse_aave_markets(&envelope, &record()).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].symbol.as_deref(), Some("WETH"));
        assert_eq!(rows[0].available_liquidity_usd, Some(100.0));
    }
}
