use anyhow::{Context, Result, bail};
use chrono::{TimeZone, Utc};
use crossalpha_storage::{ObservationEnvelope, RAW_ENVELOPE_CANONICAL_SCHEMA_VERSION};
use reqwest::Client;
use serde_json::{Map, Value, json};
use std::time::Duration;
use tokio::time::sleep;

pub const AAVE_V3_GRAPHQL: &str = "https://api.v3.aave.com/graphql";
pub const AAVE_V3_ETHEREUM_POOL: &str = "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2";
pub const AAVE_V3_ETHEREUM_CHAIN_ID: u64 = 1;
pub const LIQUIDATION_CALL_TOPIC: &str =
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286";

#[derive(Clone)]
pub struct AaveV02Client {
    client: Client,
}

impl AaveV02Client {
    pub fn new(timeout: Duration) -> Result<Self> {
        if timeout.is_zero() {
            bail!("Aave HTTP timeout must be greater than zero");
        }
        let client = Client::builder()
            .timeout(timeout)
            .user_agent("crossalpha-state-rs/0.1")
            .build()
            .context("build Aave HTTP client")?;
        Ok(Self { client })
    }

    pub async fn collect_market(&self) -> Result<ObservationEnvelope> {
        let body = json!({"query": markets_query(AAVE_V3_ETHEREUM_CHAIN_ID)});
        let payload = self
            .post_json_with_retry(AAVE_V3_GRAPHQL, &body, "Aave GraphQL")
            .await?;
        let errors = payload.get("errors").filter(|value| !value.is_null());
        if let Some(errors) = errors {
            bail!("Aave V3 GraphQL returned errors: {errors}");
        }
        let markets = payload
            .pointer("/data/markets")
            .and_then(Value::as_array)
            .context("Aave V3 GraphQL returned no markets")?;
        let pool = AAVE_V3_ETHEREUM_POOL.to_ascii_lowercase();
        let core: Vec<Value> = markets
            .iter()
            .filter(|market| {
                market
                    .get("address")
                    .and_then(Value::as_str)
                    .is_some_and(|address| address.eq_ignore_ascii_case(&pool))
            })
            .cloned()
            .collect();
        if core.len() != 1 {
            bail!(
                "Aave V3 GraphQL did not return exactly one preregistered Ethereum Core market: address={} matches={}",
                pool,
                core.len()
            );
        }
        let mut filtered = payload;
        let data = filtered
            .get_mut("data")
            .and_then(Value::as_object_mut)
            .context("Aave GraphQL data is not an object")?;
        data.insert("markets".to_owned(), Value::Array(core));

        let now = Utc::now();
        let mut metadata = Map::new();
        metadata.insert(
            "endpoint".to_owned(),
            Value::String(AAVE_V3_GRAPHQL.to_owned()),
        );
        metadata.insert("chain_id".to_owned(), json!(AAVE_V3_ETHEREUM_CHAIN_ID));
        metadata.insert("market_address".to_owned(), Value::String(pool));
        metadata.insert("data_cost_usd".to_owned(), json!(0));
        metadata.insert(
            "scope".to_owned(),
            Value::String(
                "ethereum_v3_core_market_level_not_user_health_factor_distribution".to_owned(),
            ),
        );
        Ok(ObservationEnvelope {
            schema_version: RAW_ENVELOPE_CANONICAL_SCHEMA_VERSION,
            event_time: None,
            observed_at: now,
            known_at: now,
            source_type: "AGGREGATOR".to_owned(),
            source_id: "aave:v3:graphql".to_owned(),
            observation_type: "markets_snapshot".to_owned(),
            payload: filtered,
            metadata,
        })
    }

    pub async fn collect_liquidations(
        &self,
        rpc_url: &str,
        lookback_blocks: u64,
    ) -> Result<ObservationEnvelope> {
        let lookback_blocks = lookback_blocks.max(1);
        let latest_raw = self.rpc(rpc_url, "eth_blockNumber", json!([])).await?;
        let latest = parse_hex_u64(&latest_raw).context("invalid eth_blockNumber result")?;
        let first = latest.saturating_sub(lookback_blocks.saturating_sub(1));
        let logs = self
            .rpc(
                rpc_url,
                "eth_getLogs",
                json!([{
                    "address": AAVE_V3_ETHEREUM_POOL,
                    "fromBlock": format!("0x{first:x}"),
                    "toBlock": format!("0x{latest:x}"),
                    "topics": [LIQUIDATION_CALL_TOPIC],
                }]),
            )
            .await?;
        let logs = logs
            .as_array()
            .context("Aave liquidation eth_getLogs did not return a list")?;
        let mut enriched = Vec::with_capacity(logs.len());
        let mut block_cache = std::collections::BTreeMap::<String, Option<String>>::new();
        for item in logs {
            let Some(object) = item.as_object() else {
                continue;
            };
            let mut row = object.clone();
            if let Some(block_number) = row.get("blockNumber").and_then(Value::as_str) {
                let timestamp = if let Some(cached) = block_cache.get(block_number) {
                    cached.clone()
                } else {
                    let block = self
                        .rpc(
                            rpc_url,
                            "eth_getBlockByNumber",
                            json!([block_number, false]),
                        )
                        .await?;
                    let timestamp = block
                        .get("timestamp")
                        .and_then(Value::as_str)
                        .map(str::to_owned);
                    block_cache.insert(block_number.to_owned(), timestamp.clone());
                    timestamp
                };
                if let Some(raw_timestamp) = timestamp
                    && let Ok(seconds) =
                        u64::from_str_radix(raw_timestamp.trim_start_matches("0x"), 16)
                    && let Some(dt) = Utc.timestamp_opt(seconds as i64, 0).single()
                {
                    row.insert("blockTimestamp".to_owned(), Value::String(dt.to_rfc3339()));
                }
            }
            enriched.push(Value::Object(row));
        }

        let now = Utc::now();
        let mut metadata = Map::new();
        metadata.insert(
            "pool_address".to_owned(),
            Value::String(AAVE_V3_ETHEREUM_POOL.to_owned()),
        );
        metadata.insert("chain_id".to_owned(), json!(AAVE_V3_ETHEREUM_CHAIN_ID));
        metadata.insert("from_block".to_owned(), json!(first));
        metadata.insert("to_block".to_owned(), json!(latest));
        metadata.insert("lookback_blocks".to_owned(), json!(lookback_blocks));
        metadata.insert("data_cost_usd".to_owned(), json!(0));
        Ok(ObservationEnvelope {
            schema_version: RAW_ENVELOPE_CANONICAL_SCHEMA_VERSION,
            event_time: None,
            observed_at: now,
            known_at: now,
            source_type: "CHAIN".to_owned(),
            source_id: "aave:v3:ethereum".to_owned(),
            observation_type: "liquidation_logs".to_owned(),
            payload: Value::Array(enriched),
            metadata,
        })
    }

    async fn rpc(&self, url: &str, method: &str, params: Value) -> Result<Value> {
        let body = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
        let payload = self.post_json_with_retry(url, &body, "EVM RPC").await?;
        if let Some(error) = payload.get("error") {
            bail!("EVM RPC {method} returned error: {error}");
        }
        payload
            .get("result")
            .cloned()
            .with_context(|| format!("EVM RPC {method} result missing"))
    }

    async fn post_json_with_retry(&self, url: &str, body: &Value, label: &str) -> Result<Value> {
        let mut last_error = None;
        for attempt in 0..3_u32 {
            let result = async {
                let response = self
                    .client
                    .post(url)
                    .header("Accept", "application/json")
                    .header("Content-Type", "application/json")
                    .json(body)
                    .send()
                    .await
                    .with_context(|| format!("POST {label}"))?
                    .error_for_status()
                    .with_context(|| format!("POST {label} returned error status"))?;
                response
                    .json::<Value>()
                    .await
                    .with_context(|| format!("decode JSON from {label}"))
            }
            .await;
            match result {
                Ok(value) => return Ok(value),
                Err(error) => {
                    last_error = Some(error);
                    if attempt < 2 {
                        sleep(Duration::from_secs(1_u64 << attempt)).await;
                    }
                }
            }
        }
        Err(last_error
            .context("request exhausted retries without an error")?
            .context(format!("{label} request exhausted retries")))
    }
}

pub fn markets_query(chain_id: u64) -> String {
    assert!(chain_id > 0, "chain_id must be positive");
    format!(
        "\nquery CrossAlphaAaveMarkets {{\n  markets(request: {{ chainIds: [{chain_id}] }}) {{\n    address\n    name\n    reserves {{\n      underlyingToken {{ address symbol decimals }}\n      supplyInfo {{ apy {{ formatted }} }}\n      borrowInfo {{\n        apy {{ formatted }}\n        availableLiquidity {{ amount {{ value }} usd }}\n        borrowCapReached\n      }}\n      isFrozen\n      isPaused\n    }}\n  }}\n}}\n"
    )
}

fn parse_hex_u64(value: &Value) -> Option<u64> {
    let raw = value.as_str()?.strip_prefix("0x")?;
    u64::from_str_radix(raw, 16).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn market_query_uses_literal_chain_id() {
        let query = markets_query(1);
        assert!(query.contains("chainIds: [1]"));
        assert!(query.contains("availableLiquidity"));
    }
}
