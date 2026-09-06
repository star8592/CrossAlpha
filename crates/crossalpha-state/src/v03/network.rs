use crate::v03::{
    AAVE_V3_ETHEREUM_CORE_POOL, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    BLOCKSCOUT_ETHEREUM_API_URL, BLOCKSCOUT_MAX_LOG_RESULTS, BORROW_EVENT_TOPIC0,
    FINALITY_LAG_BLOCKS, GET_USER_ACCOUNT_DATA_SELECTOR, ZERO_COST_PUBLIC_RPC_URLS,
};
use crate::v03_census::{
    AccountDataRow, decode_get_user_account_data, encode_get_user_account_data, normalize_address,
};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use reqwest::Client;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

pub const BLOCKSCOUT_STATE_RPC_SOURCE: &str = "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK";

#[derive(Debug, Clone)]
pub struct RpcCandidate {
    pub url: String,
    pub source: String,
}

#[derive(Debug, Clone)]
pub struct SelectedStateRpc {
    pub client: StateRpcClient,
    pub latest_block: u64,
    pub finalized_block: u64,
    pub finalized_block_time: DateTime<Utc>,
    pub failures_before_selection: BTreeMap<String, String>,
}

#[derive(Debug, Clone)]
pub struct StateRpcClient {
    http: Client,
    url: String,
    source: String,
    batch_size: usize,
}

impl StateRpcClient {
    pub fn new(http: Client, url: String, source: String, batch_size: usize) -> Self {
        Self {
            http,
            url,
            source,
            batch_size: batch_size.max(1),
        }
    }

    pub fn source(&self) -> &str {
        &self.source
    }

    pub async fn latest_block(&self) -> Result<u64> {
        let raw = self.rpc_single("eth_blockNumber", json!([]), 1).await?;
        parse_hex_u64(&raw, "eth_blockNumber")
    }

    pub async fn block_timestamp(&self, block_number: u64) -> Result<DateTime<Utc>> {
        let block = self
            .rpc_single(
                "eth_getBlockByNumber",
                json!([format!("0x{block_number:x}"), false]),
                1,
            )
            .await?;
        let timestamp = block
            .as_object()
            .and_then(|object| object.get("timestamp"))
            .context("eth_getBlockByNumber returned no timestamp")?;
        let seconds = parse_hex_u64(timestamp, "block timestamp")?;
        DateTime::<Utc>::from_timestamp(seconds as i64, 0)
            .context("block timestamp is out of range")
    }

    pub async fn account_data(
        &self,
        addresses: &[String],
        block_number: u64,
    ) -> Result<Vec<AccountDataRow>> {
        let normalized: BTreeSet<String> = addresses
            .iter()
            .map(|address| normalize_address(address))
            .collect::<Result<_>>()?;
        let normalized: Vec<String> = normalized.into_iter().collect();
        if normalized.is_empty() {
            return Ok(Vec::new());
        }

        let mut result = BTreeMap::new();
        for (chunk_index, chunk) in normalized.chunks(self.batch_size).enumerate() {
            let chunk_rows = match self
                .batch_account_calls(
                    chunk,
                    block_number,
                    chunk_index * self.batch_size + 1,
                )
                .await
            {
                Ok(rows) => rows,
                Err(_) => self.sequential_account_calls(chunk, block_number).await?,
            };
            for address in chunk {
                let row = chunk_rows.get(address).cloned().unwrap_or_else(|| AccountDataRow {
                    address: address.clone(),
                    success: false,
                    total_collateral_usd: None,
                    total_debt_usd: None,
                    available_borrows_usd: None,
                    current_liquidation_threshold_pct: None,
                    ltv_pct: None,
                    health_factor: None,
                    error: Some("missing_json_rpc_batch_response".to_owned()),
                });
                result.insert(address.clone(), row);
            }
        }
        Ok(normalized
            .iter()
            .filter_map(|address| result.remove(address))
            .collect())
    }

    async fn batch_account_calls(
        &self,
        addresses: &[String],
        block_number: u64,
        id_offset: usize,
    ) -> Result<BTreeMap<String, AccountDataRow>> {
        let block_tag = format!("0x{block_number:x}");
        let mut payload = Vec::with_capacity(addresses.len());
        let mut id_to_address = BTreeMap::new();
        for (offset, address) in addresses.iter().enumerate() {
            let request_id = id_offset + offset;
            id_to_address.insert(request_id as u64, address.clone());
            payload.push(json!({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "eth_call",
                "params": [
                    {
                        "to": AAVE_V3_ETHEREUM_CORE_POOL,
                        "data": encode_get_user_account_data(address, GET_USER_ACCOUNT_DATA_SELECTOR)?,
                    },
                    block_tag,
                ],
            }));
        }
        let response = self
            .http
            .post(&self.url)
            .json(&payload)
            .send()
            .await
            .context("RpcBatchTransportError")?
            .error_for_status()
            .context("RpcBatchHttpStatusError")?;
        let body: Value = response.json().await.context("RpcBatchJsonDecodeError")?;
        let items = body
            .as_array()
            .context("JSON-RPC endpoint does not support batch responses")?;
        let mut rows = BTreeMap::new();
        for item in items {
            let Some(object) = item.as_object() else {
                continue;
            };
            let Some(request_id) = object.get("id").and_then(Value::as_u64) else {
                continue;
            };
            let Some(address) = id_to_address.get(&request_id) else {
                continue;
            };
            if object.get("error").is_some_and(|value| !value.is_null()) {
                rows.insert(address.clone(), failed_row(address, "rpc_error"));
                continue;
            }
            let Some(raw) = object.get("result").and_then(Value::as_str) else {
                rows.insert(address.clone(), failed_row(address, "missing_rpc_result"));
                continue;
            };
            rows.insert(address.clone(), decode_row(address, raw));
        }
        Ok(rows)
    }

    async fn sequential_account_calls(
        &self,
        addresses: &[String],
        block_number: u64,
    ) -> Result<BTreeMap<String, AccountDataRow>> {
        let mut rows = BTreeMap::new();
        let block_tag = format!("0x{block_number:x}");
        for (index, address) in addresses.iter().enumerate() {
            let params = json!([
                {
                    "to": AAVE_V3_ETHEREUM_CORE_POOL,
                    "data": encode_get_user_account_data(address, GET_USER_ACCOUNT_DATA_SELECTOR)?,
                },
                block_tag,
            ]);
            match self.rpc_single("eth_call", params, index + 1).await {
                Ok(value) => match value.as_str() {
                    Some(raw) => {
                        rows.insert(address.clone(), decode_row(address, raw));
                    }
                    None => {
                        rows.insert(address.clone(), failed_row(address, "eth_call_result_not_hex"));
                    }
                },
                Err(_) => {
                    rows.insert(address.clone(), failed_row(address, "eth_call_failed"));
                }
            }
        }
        Ok(rows)
    }

    async fn rpc_single(&self, method: &str, params: Value, request_id: usize) -> Result<Value> {
        let response = self
            .http
            .post(&self.url)
            .json(&json!({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }))
            .send()
            .await
            .context("RpcTransportError")?
            .error_for_status()
            .context("RpcHttpStatusError")?;
        let body: Value = response.json().await.context("RpcJsonDecodeError")?;
        let object = body
            .as_object()
            .context("JSON-RPC returned non-object response")?;
        if object.get("error").is_some_and(|value| !value.is_null()) {
            bail!("RpcResponseError");
        }
        object
            .get("result")
            .cloned()
            .context("JSON-RPC response missing result")
    }
}

pub fn build_http_client(timeout: Duration) -> Result<Client> {
    if timeout.is_zero() {
        bail!("State V0.3 HTTP timeout must be positive");
    }
    Client::builder()
        .timeout(timeout)
        .build()
        .context("build State V0.3 HTTP client")
}

pub fn resolve_rpc_candidates(configured: Option<&str>) -> Vec<RpcCandidate> {
    let sources = [
        BLOCKSCOUT_STATE_RPC_SOURCE,
        "BLOCKREQ_ZERO_COST_FALLBACK",
        "PUBLICNODE_ZERO_COST_FALLBACK",
        "LLAMARPC_ZERO_COST_FALLBACK",
    ];
    let mut result = Vec::new();
    let mut seen = BTreeSet::new();
    if let Some(configured) = configured.filter(|value| !value.is_empty()) {
        result.push(RpcCandidate {
            url: configured.to_owned(),
            source: "EVM_RPC_URL".to_owned(),
        });
        seen.insert(configured.to_owned());
    }
    for (url, source) in ZERO_COST_PUBLIC_RPC_URLS.iter().zip(sources) {
        if seen.insert((*url).to_owned()) {
            result.push(RpcCandidate {
                url: (*url).to_owned(),
                source: source.to_owned(),
            });
        }
    }
    result
}

pub async fn select_state_rpc(
    http: &Client,
    configured: Option<&str>,
) -> Result<SelectedStateRpc> {
    let mut failures = BTreeMap::new();
    for candidate in resolve_rpc_candidates(configured) {
        let client = StateRpcClient::new(
            http.clone(),
            candidate.url.clone(),
            candidate.source.clone(),
            100,
        );
        let attempt = async {
            let latest_block = client.latest_block().await?;
            let finalized_block = latest_block
                .saturating_sub(FINALITY_LAG_BLOCKS as u64)
                .max(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64);
            let finalized_block_time = client.block_timestamp(finalized_block).await?;
            if finalized_block_time > Utc::now() {
                bail!("finalized block timestamp is in the future");
            }
            let probe = client
                .account_data(&[AAVE_V3_ETHEREUM_CORE_POOL.to_ascii_lowercase()], finalized_block)
                .await?;
            if probe.first().is_none_or(|row| !row.success) {
                bail!("getUserAccountData fixed-block probe failed");
            }
            Ok::<_, anyhow::Error>((latest_block, finalized_block, finalized_block_time))
        }
        .await;
        match attempt {
            Ok((latest_block, finalized_block, finalized_block_time)) => {
                return Ok(SelectedStateRpc {
                    client,
                    latest_block,
                    finalized_block,
                    finalized_block_time,
                    failures_before_selection: failures,
                });
            }
            Err(error) => {
                failures.insert(candidate.source, error_category(&error));
            }
        }
    }
    bail!("No State V0.3 state RPC passed finalized-block/fixed-call probes; attempts={failures:?}")
}

pub async fn borrow_logs_complete(
    http: &Client,
    from_block: u64,
    to_block: u64,
) -> Result<Vec<Value>> {
    borrow_logs_complete_boxed(http, from_block, to_block).await
}

fn borrow_logs_complete_boxed<'a>(
    http: &'a Client,
    from_block: u64,
    to_block: u64,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Vec<Value>>> + Send + 'a>> {
    Box::pin(async move {
        if to_block < from_block {
            bail!("invalid block range");
        }
        let response = http
            .get(BLOCKSCOUT_ETHEREUM_API_URL)
            .query(&[
                ("module", "logs".to_owned()),
                ("action", "getLogs".to_owned()),
                ("fromBlock", from_block.to_string()),
                ("toBlock", to_block.to_string()),
                ("address", AAVE_V3_ETHEREUM_CORE_POOL.to_owned()),
                ("topic0", BORROW_EVENT_TOPIC0.to_owned()),
            ])
            .send()
            .await
            .context("BlockscoutTransportError")?
            .error_for_status()
            .context("BlockscoutHttpStatusError")?;
        let body: Value = response.json().await.context("BlockscoutJsonDecodeError")?;
        let result = body
            .as_object()
            .and_then(|object| object.get("result"))
            .and_then(Value::as_array)
            .context("Blockscout indexed-log query failed")?;
        if result.len() < BLOCKSCOUT_MAX_LOG_RESULTS as usize {
            return Ok(result.iter().filter(|item| item.is_object()).cloned().collect());
        }
        if from_block == to_block {
            bail!("Blockscout single-block Borrow log count reached the provider result limit; completeness cannot be proven");
        }
        let midpoint = (from_block + to_block) / 2;
        let mut left = borrow_logs_complete_boxed(http, from_block, midpoint).await?;
        let right = borrow_logs_complete_boxed(http, midpoint + 1, to_block).await?;
        left.extend(right);
        Ok(left)
    })
}

pub async fn adaptive_borrow_logs(
    http: &Client,
    start: u64,
    end: u64,
    minimum_span: u64,
) -> Result<Vec<Value>> {
    adaptive_borrow_logs_boxed(http, start, end, minimum_span).await
}

fn adaptive_borrow_logs_boxed<'a>(
    http: &'a Client,
    start: u64,
    end: u64,
    minimum_span: u64,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Vec<Value>>> + Send + 'a>> {
    Box::pin(async move {
        match borrow_logs_complete(http, start, end).await {
            Ok(logs) => Ok(logs),
            Err(error) => {
                if end.saturating_sub(start) + 1 <= minimum_span.max(1) {
                    return Err(error);
                }
                let midpoint = (start + end) / 2;
                let mut left = adaptive_borrow_logs_boxed(http, start, midpoint, minimum_span).await?;
                let right = adaptive_borrow_logs_boxed(http, midpoint + 1, end, minimum_span).await?;
                left.extend(right);
                Ok(left)
            }
        }
    })
}

fn decode_row(address: &str, raw: &str) -> AccountDataRow {
    match decode_get_user_account_data(raw) {
        Ok(metrics) => AccountDataRow {
            address: address.to_owned(),
            success: true,
            total_collateral_usd: Some(metrics.total_collateral_usd),
            total_debt_usd: Some(metrics.total_debt_usd),
            available_borrows_usd: Some(metrics.available_borrows_usd),
            current_liquidation_threshold_pct: Some(metrics.current_liquidation_threshold_pct),
            ltv_pct: Some(metrics.ltv_pct),
            health_factor: metrics.health_factor,
            error: None,
        },
        Err(_) => failed_row(address, "decode_error"),
    }
}

fn failed_row(address: &str, error: &str) -> AccountDataRow {
    AccountDataRow {
        address: address.to_owned(),
        success: false,
        total_collateral_usd: None,
        total_debt_usd: None,
        available_borrows_usd: None,
        current_liquidation_threshold_pct: None,
        ltv_pct: None,
        health_factor: None,
        error: Some(error.to_owned()),
    }
}

fn parse_hex_u64(value: &Value, label: &str) -> Result<u64> {
    let text = value
        .as_str()
        .with_context(|| format!("{label} result is not hex string"))?;
    let digits = text
        .strip_prefix("0x")
        .with_context(|| format!("{label} result lacks 0x prefix"))?;
    u64::from_str_radix(digits, 16).with_context(|| format!("invalid {label} hex"))
}

fn error_category(error: &anyhow::Error) -> String {
    let text = format!("{error:#}");
    for category in [
        "RpcBatchTransportError",
        "RpcBatchHttpStatusError",
        "RpcBatchJsonDecodeError",
        "RpcTransportError",
        "RpcHttpStatusError",
        "RpcJsonDecodeError",
        "RpcResponseError",
        "BlockscoutTransportError",
        "BlockscoutHttpStatusError",
        "BlockscoutJsonDecodeError",
    ] {
        if text.contains(category) {
            return category.to_owned();
        }
    }
    "RuntimeError".to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configured_rpc_precedes_free_pool_without_duplicates() {
        let candidates = resolve_rpc_candidates(Some(ZERO_COST_PUBLIC_RPC_URLS[0]));
        assert_eq!(candidates[0].source, "EVM_RPC_URL");
        assert_eq!(candidates.len(), ZERO_COST_PUBLIC_RPC_URLS.len());
    }
}
