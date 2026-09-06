use crate::v03::{
    AAVE_V3_ETHEREUM_CORE_POOL, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK, ACTIONABILITY,
    BLOCKSCOUT_ETHEREUM_API_URL, BLOCKSCOUT_LOG_SOURCE, BLOCKSCOUT_MAX_LOG_RESULTS,
    BORROW_EVENT_TOPIC0, FINALITY_LAG_BLOCKS, GET_USER_ACCOUNT_DATA_SELECTOR,
    ZERO_COST_PUBLIC_RPC_URLS,
};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::Serialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

pub const BLOCKSCOUT_STATE_RPC_SOURCE: &str = "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK";

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct StateV03PreflightReport {
    pub protocol: String,
    pub data_cost_usd: i64,
    pub split_data_plane: bool,
    pub archive_rpc_required: bool,
    pub borrow_log_source: String,
    pub state_rpc_source: String,
    pub rpc_source: String,
    pub state_rpc_candidate_failures_before_selection: BTreeMap<String, String>,
    pub rpc_candidate_failures_before_selection: BTreeMap<String, String>,
    pub latest_block: u64,
    pub finalized_block: u64,
    pub finalized_block_time: String,
    pub block_time_source: String,
    pub finality_lag_blocks: u64,
    pub recent_borrow_scan_from_block: u64,
    pub recent_borrow_scan_to_block: u64,
    pub recent_borrow_log_count: usize,
    pub historical_log_scan_from_block: u64,
    pub historical_log_scan_to_block: u64,
    pub historical_log_scan_ok: bool,
    pub historical_borrow_log_count: usize,
    pub fixed_block_account_call_ok: bool,
    pub actionability: String,
    pub risk_multiplier: Option<f64>,
}

#[derive(Debug, Clone)]
struct RpcCandidate {
    url: String,
    source: &'static str,
}

pub async fn run_preflight(
    timeout: Duration,
    configured_rpc: Option<&str>,
) -> Result<StateV03PreflightReport> {
    if timeout.is_zero() {
        bail!("State V0.3 preflight timeout must be positive");
    }
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .context("build State V0.3 HTTP client")?;

    let historical_from = AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64;
    let historical_to = historical_from + 255;
    let historical_logs = borrow_logs_complete(&client, historical_from, historical_to)
        .await
        .map_err(|error| {
            anyhow::anyhow!(
                "State V0.3 indexed Borrow-log source failed historical probe: {}",
                error_category(&error)
            )
        })?;

    let mut attempts = BTreeMap::new();
    for candidate in resolve_state_rpc_candidates(configured_rpc) {
        match probe_rpc_candidate(&client, &candidate).await {
            Ok(probe) => {
                let recent_from = probe
                    .finalized_block
                    .saturating_sub(127)
                    .max(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64);
                let recent_logs = borrow_logs_complete(&client, recent_from, probe.finalized_block)
                    .await
                    .context("State V0.3 indexed Borrow-log source failed recent probe")?;
                return Ok(StateV03PreflightReport {
                    protocol: "CROSSALPHA_STATE_V0_3_PREFLIGHT".to_owned(),
                    data_cost_usd: 0,
                    split_data_plane: true,
                    archive_rpc_required: false,
                    borrow_log_source: BLOCKSCOUT_LOG_SOURCE.to_owned(),
                    state_rpc_source: candidate.source.to_owned(),
                    rpc_source: candidate.source.to_owned(),
                    state_rpc_candidate_failures_before_selection: attempts.clone(),
                    rpc_candidate_failures_before_selection: attempts,
                    latest_block: probe.latest_block,
                    finalized_block: probe.finalized_block,
                    finalized_block_time: probe.finalized_block_time.to_rfc3339(),
                    block_time_source: "eth_getBlockByNumber(finalized_block)".to_owned(),
                    finality_lag_blocks: FINALITY_LAG_BLOCKS as u64,
                    recent_borrow_scan_from_block: recent_from,
                    recent_borrow_scan_to_block: probe.finalized_block,
                    recent_borrow_log_count: recent_logs.len(),
                    historical_log_scan_from_block: historical_from,
                    historical_log_scan_to_block: historical_to,
                    historical_log_scan_ok: true,
                    historical_borrow_log_count: historical_logs.len(),
                    fixed_block_account_call_ok: true,
                    actionability: ACTIONABILITY.to_owned(),
                    risk_multiplier: None,
                });
            }
            Err(error) => {
                attempts.insert(candidate.source.to_owned(), error_category(&error));
            }
        }
    }

    bail!(
        "No State V0.3 state RPC passed finalized-block/fixed-call probes; attempts={attempts:?}"
    )
}

#[derive(Debug, Clone)]
struct RpcProbe {
    latest_block: u64,
    finalized_block: u64,
    finalized_block_time: DateTime<Utc>,
}

async fn probe_rpc_candidate(client: &Client, candidate: &RpcCandidate) -> Result<RpcProbe> {
    let latest_raw = rpc_single(client, &candidate.url, "eth_blockNumber", json!([])).await?;
    let latest_block = parse_hex_u64(&latest_raw, "eth_blockNumber")?;
    let finalized_block = latest_block
        .saturating_sub(FINALITY_LAG_BLOCKS as u64)
        .max(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64);

    let block = rpc_single(
        client,
        &candidate.url,
        "eth_getBlockByNumber",
        json!([format!("0x{finalized_block:x}"), false]),
    )
    .await?;
    let timestamp = block
        .as_object()
        .and_then(|object| object.get("timestamp"))
        .context("eth_getBlockByNumber returned no timestamp")?;
    let timestamp_seconds = parse_hex_u64(timestamp, "block timestamp")?;
    let finalized_block_time = DateTime::<Utc>::from_timestamp(timestamp_seconds as i64, 0)
        .context("finalized block timestamp is out of range")?;
    if finalized_block_time > Utc::now() {
        bail!("finalized block timestamp is in the future");
    }

    let data = encode_get_user_account_data(AAVE_V3_ETHEREUM_CORE_POOL)?;
    let account = rpc_single(
        client,
        &candidate.url,
        "eth_call",
        json!([
            {"to": AAVE_V3_ETHEREUM_CORE_POOL, "data": data},
            format!("0x{finalized_block:x}")
        ]),
    )
    .await?;
    validate_account_data_result(&account)?;

    Ok(RpcProbe {
        latest_block,
        finalized_block,
        finalized_block_time,
    })
}

async fn rpc_single(client: &Client, url: &str, method: &str, params: Value) -> Result<Value> {
    let response = client
        .post(url)
        .json(&json!({
            "jsonrpc": "2.0",
            "id": 1,
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

fn parse_hex_u64(value: &Value, label: &str) -> Result<u64> {
    let text = value
        .as_str()
        .with_context(|| format!("{label} result is not hex string"))?;
    let digits = text
        .strip_prefix("0x")
        .with_context(|| format!("{label} result lacks 0x prefix"))?;
    u64::from_str_radix(digits, 16).with_context(|| format!("invalid {label} hex"))
}

fn encode_get_user_account_data(address: &str) -> Result<String> {
    let normalized = normalize_address(address)?;
    Ok(format!(
        "{}{:0>64}",
        GET_USER_ACCOUNT_DATA_SELECTOR,
        &normalized[2..]
    ))
}

fn normalize_address(value: &str) -> Result<String> {
    let text = value.to_ascii_lowercase();
    let Some(hex) = text.strip_prefix("0x") else {
        bail!("invalid EVM address");
    };
    if hex.len() != 40 || !hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        bail!("invalid EVM address");
    }
    Ok(text)
}

fn validate_account_data_result(value: &Value) -> Result<()> {
    let text = value.as_str().context("eth_call result is not hex")?;
    let raw = text.strip_prefix("0x").context("eth_call result is not hex")?;
    if raw.len() < 64 * 6
        || raw.len() % 64 != 0
        || !raw.bytes().all(|byte| byte.is_ascii_hexdigit())
    {
        bail!("getUserAccountData returned unexpected byte length");
    }
    Ok(())
}

fn resolve_state_rpc_candidates(configured: Option<&str>) -> Vec<RpcCandidate> {
    let mut result = Vec::new();
    let mut seen = BTreeSet::new();
    if let Some(configured) = configured.filter(|value| !value.is_empty()) {
        result.push(RpcCandidate {
            url: configured.to_owned(),
            source: "EVM_RPC_URL",
        });
        seen.insert(configured.to_owned());
    }
    let sources = [
        BLOCKSCOUT_STATE_RPC_SOURCE,
        "BLOCKREQ_ZERO_COST_FALLBACK",
        "PUBLICNODE_ZERO_COST_FALLBACK",
        "LLAMARPC_ZERO_COST_FALLBACK",
    ];
    for (url, source) in ZERO_COST_PUBLIC_RPC_URLS.iter().zip(sources) {
        if seen.insert((*url).to_owned()) {
            result.push(RpcCandidate {
                url: (*url).to_owned(),
                source,
            });
        }
    }
    result
}

async fn query_blockscout_logs(
    client: &Client,
    from_block: u64,
    to_block: u64,
) -> Result<(usize, Vec<Value>)> {
    let response = client
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
    let raw_len = result.len();
    let rows = result
        .iter()
        .filter(|item| item.is_object())
        .cloned()
        .collect();
    Ok((raw_len, rows))
}

fn borrow_logs_complete<'a>(
    client: &'a Client,
    from_block: u64,
    to_block: u64,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Vec<Value>>> + Send + 'a>> {
    Box::pin(async move {
        if to_block < from_block {
            bail!("invalid block range");
        }
        let (raw_len, rows) = query_blockscout_logs(client, from_block, to_block).await?;
        if raw_len < BLOCKSCOUT_MAX_LOG_RESULTS as usize {
            return Ok(rows);
        }
        if from_block == to_block {
            bail!(
                "Blockscout single-block Borrow log count reached the provider result limit; completeness cannot be proven"
            );
        }
        let midpoint = (from_block + to_block) / 2;
        let mut left = borrow_logs_complete(client, from_block, midpoint).await?;
        let right = borrow_logs_complete(client, midpoint + 1, to_block).await?;
        left.extend(right);
        Ok(left)
    })
}

fn error_category(error: &anyhow::Error) -> String {
    let text = format!("{error:#}");
    for category in [
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
    fn account_data_encoding_matches_python_contract() {
        let encoded = encode_get_user_account_data(AAVE_V3_ETHEREUM_CORE_POOL).unwrap();
        assert_eq!(encoded.len(), 10 + 64);
        assert!(encoded.starts_with(GET_USER_ACCOUNT_DATA_SELECTOR));
        assert!(encoded.ends_with(&AAVE_V3_ETHEREUM_CORE_POOL[2..].to_ascii_lowercase()));
    }

    #[test]
    fn rpc_candidate_order_deduplicates_configured_blockscout() {
        let candidates = resolve_state_rpc_candidates(Some(ZERO_COST_PUBLIC_RPC_URLS[0]));
        assert_eq!(candidates.len(), 4);
        assert_eq!(candidates[0].source, "EVM_RPC_URL");
        assert_eq!(candidates[1].source, "BLOCKREQ_ZERO_COST_FALLBACK");
    }

    #[test]
    fn account_data_result_requires_six_words() {
        let good = Value::String(format!("0x{}", "0".repeat(64 * 6)));
        let bad = Value::String(format!("0x{}", "0".repeat(64 * 5)));
        assert!(validate_account_data_result(&good).is_ok());
        assert!(validate_account_data_result(&bad).is_err());
    }
}
