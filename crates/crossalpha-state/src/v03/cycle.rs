use crate::StateRuntimeContext;
use crate::v03::{
    AAVE_V3_ETHEREUM_CORE_POOL, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK, ADAPTIVE_MINIMUM_SPAN_BLOCKS,
    BLOCKSCOUT_LOG_SOURCE, BOOTSTRAP_CHUNK_BLOCKS, BORROW_EVENT_TOPIC0,
    FULL_CENSUS_CADENCE_MINUTES, MAX_BOOTSTRAP_CHUNKS_PER_CYCLE,
};
use crate::v03_artifacts::{read_address_set, write_account_rows, write_address_set};
use crate::v03_census::{CensusPolicy, borrow_log_debtor, compute_borrower_census};
use crate::v03_network::{adaptive_borrow_logs, build_http_client, select_state_rpc};
use crate::v03_runtime_binding::{runtime_binding_path, verify_runtime_binding_file};
use crate::v03_watchlist::compute_watchlist_snapshot;
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Duration as ChronoDuration, SecondsFormat, Utc};
use crossalpha_storage::{ObservationEnvelope, RawSnapshotStore};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BootstrapState {
    pub schema_version: u32,
    pub next_block: u64,
    pub last_scanned_block: Option<u64>,
    pub bootstrap_complete: bool,
    pub last_valid_full_census_at: Option<String>,
    pub last_valid_full_census_block: Option<u64>,
    #[serde(default)]
    pub last_valid_full_census_block_time: Option<String>,
    #[serde(default)]
    pub last_valid_full_census_summary: Option<String>,
    #[serde(default)]
    pub last_valid_full_census_summary_sha256: Option<String>,
    #[serde(default)]
    pub pending_new_borrowers_since_full: Vec<String>,
    #[serde(default)]
    pub candidate_address_count: usize,
    #[serde(default)]
    pub latest_seen_block: Option<u64>,
    #[serde(default)]
    pub latest_finalized_block: Option<u64>,
    #[serde(default)]
    pub latest_finalized_block_time: Option<String>,
    #[serde(default)]
    pub borrow_log_source: Option<String>,
    #[serde(default)]
    pub state_rpc_source: Option<String>,
    #[serde(default)]
    pub rpc_source: Option<String>,
    #[serde(default)]
    pub state_rpc_candidate_failures_before_selection: std::collections::BTreeMap<String, String>,
}

impl Default for BootstrapState {
    fn default() -> Self {
        Self {
            schema_version: 1,
            next_block: AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64,
            last_scanned_block: None,
            bootstrap_complete: false,
            last_valid_full_census_at: None,
            last_valid_full_census_block: None,
            last_valid_full_census_block_time: None,
            last_valid_full_census_summary: None,
            last_valid_full_census_summary_sha256: None,
            pending_new_borrowers_since_full: Vec::new(),
            candidate_address_count: 0,
            latest_seen_block: None,
            latest_finalized_block: None,
            latest_finalized_block_time: None,
            borrow_log_source: None,
            state_rpc_source: None,
            rpc_source: None,
            state_rpc_candidate_failures_before_selection: Default::default(),
        }
    }
}

pub async fn run_cycle(context: &StateRuntimeContext) -> Result<Value> {
    require_runtime_binding(&context.data_root)?;
    let scan_started_at = Utc::now();
    let http = build_http_client(context.http_timeout)?;
    let mut state = load_state(&context.data_root)?;
    let universe_path = universe_path(&context.data_root);
    let mut borrowers = read_address_set(&universe_path)?;
    let mut pending_new_borrowers: BTreeSet<String> = state
        .pending_new_borrowers_since_full
        .iter()
        .map(|value| value.to_ascii_lowercase())
        .collect();
    let mut next_block = state
        .next_block
        .max(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64);

    let log_probe_from = next_block.max(AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64);
    adaptive_borrow_logs(
        &http,
        log_probe_from,
        log_probe_from + ADAPTIVE_MINIMUM_SPAN_BLOCKS as u64 - 1,
        ADAPTIVE_MINIMUM_SPAN_BLOCKS as u64,
    )
    .await
    .context("State V0.3 indexed Borrow-log source unavailable")?;

    let selected = select_state_rpc(&http, context.evm_rpc_url.as_deref()).await?;
    let latest_block = selected.latest_block;
    let finalized_block = selected.finalized_block;
    let finalized_block_time = selected.finalized_block_time;
    let state_rpc_source = selected.client.source().to_owned();
    let rpc_attempts = selected.failures_before_selection.clone();

    let store = RawSnapshotStore::new(&context.data_root);
    let mut scanned_ranges = Vec::new();
    for _ in 0..MAX_BOOTSTRAP_CHUNKS_PER_CYCLE {
        if next_block > finalized_block {
            break;
        }
        let end = (next_block + BOOTSTRAP_CHUNK_BLOCKS as u64 - 1).min(finalized_block);
        let logs =
            adaptive_borrow_logs(&http, next_block, end, ADAPTIVE_MINIMUM_SPAN_BLOCKS as u64)
                .await?;
        let previously_known = borrowers.clone();
        let debtors: BTreeSet<String> = logs
            .iter()
            .filter_map(|log| borrow_log_debtor(log, BORROW_EVENT_TOPIC0))
            .collect();
        let new_debtors: BTreeSet<String> =
            debtors.difference(&previously_known).cloned().collect();
        borrowers.extend(debtors);
        if state.bootstrap_complete {
            pending_new_borrowers.extend(new_debtors.iter().cloned());
        }

        let observed = Utc::now();
        let metadata: Map<String, Value> = serde_json::from_value(json!({
            "pool_address": AAVE_V3_ETHEREUM_CORE_POOL,
            "from_block": next_block,
            "to_block": end,
            "log_count": logs.len(),
            "new_candidate_count_in_chunk": new_debtors.len(),
            "historical_bootstrap_is_evidence": false,
            "borrow_log_source": BLOCKSCOUT_LOG_SOURCE,
            "state_rpc_source": state_rpc_source,
            "rpc_source": state_rpc_source,
            "state_rpc_candidate_failures_before_selection": rpc_attempts,
            "data_cost_usd": 0,
        }))?;
        let envelope = ObservationEnvelope {
            schema_version: 1,
            event_time: None,
            observed_at: observed,
            known_at: observed,
            source_type: "chain".to_owned(),
            source_id: "aave:v3:ethereum:borrowers".to_owned(),
            observation_type: "borrow_logs_chunk".to_owned(),
            payload: Value::Array(logs.clone()),
            metadata,
        };
        let manifest = store.write(&envelope)?;
        write_address_set(&universe_path, &borrowers)?;
        scanned_ranges.push(json!({
            "from_block": next_block,
            "to_block": end,
            "log_count": logs.len(),
            "new_candidate_count": new_debtors.len(),
            "candidate_count": borrowers.len(),
            "raw_sha256": manifest.sha256,
        }));
        state.last_scanned_block = Some(end);
        state.next_block = end + 1;
        state.pending_new_borrowers_since_full = pending_new_borrowers.iter().cloned().collect();
        next_block = end + 1;
        write_state(&context.data_root, &state)?;
    }

    let caught_up = next_block > finalized_block;
    state.bootstrap_complete = caught_up;
    state.candidate_address_count = borrowers.len();
    state.latest_seen_block = Some(latest_block);
    state.latest_finalized_block = Some(finalized_block);
    state.latest_finalized_block_time = Some(finalized_block_time.to_rfc3339());
    state.borrow_log_source = Some(BLOCKSCOUT_LOG_SOURCE.to_owned());
    state.state_rpc_source = Some(state_rpc_source.clone());
    state.rpc_source = Some(state_rpc_source.clone());
    state.state_rpc_candidate_failures_before_selection = rpc_attempts.clone();
    state.pending_new_borrowers_since_full = pending_new_borrowers.iter().cloned().collect();
    write_state(&context.data_root, &state)?;

    let common = json!({
        "protocol": "CROSSALPHA_STATE_V0_3_CYCLE",
        "actionability": "DESCRIPTIVE_ONLY",
        "risk_multiplier": Value::Null,
        "data_cost_usd": 0,
        "split_data_plane": true,
        "archive_rpc_required": false,
        "borrow_log_source": BLOCKSCOUT_LOG_SOURCE,
        "state_rpc_source": state_rpc_source,
        "rpc_source": state_rpc_source,
        "state_rpc_candidate_failures_before_selection": rpc_attempts,
        "rpc_candidate_failures_before_selection": rpc_attempts,
        "latest_block": latest_block,
        "finalized_block": finalized_block,
        "finalized_block_time": finalized_block_time.to_rfc3339(),
        "candidate_address_count": borrowers.len(),
        "pending_new_borrower_count": pending_new_borrowers.len(),
        "mutates_v01_or_v02": false,
    });

    if !caught_up {
        return Ok(merge(
            common,
            json!({
                "status": "BORROWER_UNIVERSE_BOOTSTRAPPING",
                "next_block": next_block,
                "scanned_ranges": scanned_ranges,
                "historical_bootstrap_is_evidence": false,
            }),
        ));
    }

    let decision_now = Utc::now();
    if full_census_due(&state, decision_now, finalized_block)? {
        let addresses: Vec<String> = borrowers.iter().cloned().collect();
        let accounts = selected
            .client
            .account_data(&addresses, finalized_block)
            .await?;
        let captured = Utc::now();
        let mut summary = compute_borrower_census(
            &accounts,
            borrowers.len(),
            true,
            finalized_block,
            captured,
            CensusPolicy::default(),
        )?;
        summary
            .as_object_mut()
            .context("census summary must be object")?
            .insert(
                "block_time".to_owned(),
                Value::String(finalized_block_time.to_rfc3339()),
            );
        let artifacts = write_census_artifacts(
            &context.data_root,
            &accounts,
            &summary,
            captured,
            "full_census",
        )?;
        let valid = summary
            .get("valid_full_census")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let prospective = if valid {
            let watchlist: BTreeSet<String> = summary
                .get("watchlist_addresses")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect();
            write_address_set(&watchlist_path(&context.data_root), &watchlist)?;
            state.last_valid_full_census_at = Some(captured.to_rfc3339());
            state.last_valid_full_census_block = Some(finalized_block);
            state.last_valid_full_census_block_time = Some(finalized_block_time.to_rfc3339());
            state.last_valid_full_census_summary = Some(artifacts.summary.clone());
            state.last_valid_full_census_summary_sha256 = Some(artifacts.summary_sha256.clone());
            state.pending_new_borrowers_since_full.clear();
            write_state(&context.data_root, &state)?;
            json!({"status": "native_prospective_write_pending_ledger_gate"})
        } else {
            json!({"status": "invalid_full_census_no_prospective_write"})
        };
        return Ok(merge(
            common,
            json!({
                "status": if valid { "FULL_CENSUS_RECORDED" } else { "FULL_CENSUS_PARTIAL_RETRY_REQUIRED" },
                "scan_started_at": scan_started_at.to_rfc3339_opts(SecondsFormat::Micros, false),
                "scanned_ranges": scanned_ranges,
                "census": summary,
                "artifacts": artifacts,
                "prospective": prospective,
            }),
        ));
    }

    let mut watchlist = read_address_set(&watchlist_path(&context.data_root))?;
    watchlist.extend(pending_new_borrowers);
    if watchlist.is_empty() {
        return Ok(merge(
            common,
            json!({
                "status": "CAUGHT_UP_AWAITING_NEXT_FULL_CENSUS"
            }),
        ));
    }
    let addresses: Vec<String> = watchlist.iter().cloned().collect();
    let accounts = selected
        .client
        .account_data(&addresses, finalized_block)
        .await?;
    let captured = Utc::now();
    let mut watch =
        compute_watchlist_snapshot(&accounts, watchlist.len(), finalized_block, captured);
    let object = watch
        .as_object_mut()
        .context("watchlist report must be object")?;
    object.insert(
        "block_time".to_owned(),
        Value::String(finalized_block_time.to_rfc3339()),
    );
    object.insert(
        "includes_pending_new_borrowers".to_owned(),
        Value::Bool(!state.pending_new_borrowers_since_full.is_empty()),
    );
    object.insert(
        "pending_new_borrower_count".to_owned(),
        Value::from(state.pending_new_borrowers_since_full.len()),
    );
    let artifacts =
        write_census_artifacts(&context.data_root, &accounts, &watch, captured, "watchlist")?;
    Ok(merge(
        common,
        json!({
            "status": "WATCHLIST_RECORDED",
            "watchlist": watch,
            "artifacts": artifacts,
        }),
    ))
}

#[derive(Debug, Clone, Serialize)]
struct CensusArtifacts {
    detail: String,
    detail_sha256: String,
    summary: String,
    summary_sha256: String,
}

fn load_state(data_root: &Path) -> Result<BootstrapState> {
    let path = bootstrap_state_path(data_root);
    if !path.exists() {
        return Ok(BootstrapState::default());
    }
    let file = File::open(&path).with_context(|| format!("open {}", path.display()))?;
    let mut state: BootstrapState = serde_json::from_reader(file)?;
    if state.next_block < AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64 {
        state.next_block = AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK as u64;
    }
    Ok(state)
}

fn write_state(data_root: &Path, state: &BootstrapState) -> Result<()> {
    atomic_write_json(&bootstrap_state_path(data_root), state)
}

fn full_census_due(
    state: &BootstrapState,
    now: DateTime<Utc>,
    finalized_block: u64,
) -> Result<bool> {
    let Some(raw_time) = state.last_valid_full_census_at.as_deref() else {
        return Ok(true);
    };
    if state
        .last_valid_full_census_block
        .is_some_and(|block| finalized_block <= block)
    {
        return Ok(false);
    }
    let previous = DateTime::parse_from_rfc3339(raw_time)
        .with_context(|| format!("invalid last_valid_full_census_at: {raw_time}"))?
        .with_timezone(&Utc);
    Ok(now - previous >= ChronoDuration::minutes(FULL_CENSUS_CADENCE_MINUTES))
}

fn write_census_artifacts(
    data_root: &Path,
    rows: &[crate::v03_census::AccountDataRow],
    summary: &Value,
    captured: DateTime<Utc>,
    scope: &str,
) -> Result<CensusArtifacts> {
    let directory = snapshot_dir(data_root, captured, scope);
    fs::create_dir_all(&directory)?;
    let stamp = captured.format("%H%M%S%6f").to_string();
    let detail_path = directory.join(format!("accounts_at={stamp}.parquet"));
    let summary_path = directory.join(format!("summary_at={stamp}.json"));
    write_account_rows(&detail_path, rows)?;
    let detail_sha256 = sha256_file(&detail_path)?;
    let mut payload = summary.clone();
    let object = payload
        .as_object_mut()
        .context("census summary must be object")?;
    object.insert(
        "detail_path".to_owned(),
        Value::String(detail_path.to_string_lossy().into_owned()),
    );
    object.insert(
        "detail_sha256".to_owned(),
        Value::String(detail_sha256.clone()),
    );
    atomic_write_json(&summary_path, &payload)?;
    let summary_sha256 = sha256_file(&summary_path)?;
    Ok(CensusArtifacts {
        detail: detail_path.to_string_lossy().into_owned(),
        detail_sha256,
        summary: summary_path.to_string_lossy().into_owned(),
        summary_sha256,
    })
}

fn require_runtime_binding(data_root: &Path) -> Result<()> {
    let path = runtime_binding_path(data_root);
    if !path.exists() {
        bail!("Native State V0.3 cycle refused: Rust runtime binding missing");
    }
    if !verify_runtime_binding_file(&path)? {
        bail!("Native State V0.3 cycle refused: Rust runtime binding invalid or stale");
    }
    Ok(())
}

fn merge(mut left: Value, right: Value) -> Value {
    if let (Some(left), Some(right)) = (left.as_object_mut(), right.as_object()) {
        left.extend(right.clone());
    }
    left
}

fn atomic_write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut tmp = path.as_os_str().to_os_string();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    {
        let mut file = File::create(&tmp)?;
        serde_json::to_writer_pretty(&mut file, value)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn research_root(data_root: &Path) -> PathBuf {
    data_root.join("research/state_v03")
}

fn bootstrap_state_path(data_root: &Path) -> PathBuf {
    research_root(data_root).join("bootstrap_state.json")
}

fn universe_path(data_root: &Path) -> PathBuf {
    data_root.join("derived/state/v03/borrower_universe.parquet")
}

fn watchlist_path(data_root: &Path) -> PathBuf {
    data_root.join("derived/state/v03/watchlist.parquet")
}

fn snapshot_dir(data_root: &Path, captured: DateTime<Utc>, scope: &str) -> PathBuf {
    data_root
        .join("derived/state/v03")
        .join(scope)
        .join(format!("year={:04}", captured.format("%Y")))
        .join(format!("month={:02}", captured.format("%m")))
        .join(format!("day={:02}", captured.format("%d")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn full_census_requires_new_block_and_cadence() {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let mut state = BootstrapState::default();
        assert!(full_census_due(&state, now, 100).unwrap());
        state.last_valid_full_census_at = Some((now - ChronoDuration::hours(7)).to_rfc3339());
        state.last_valid_full_census_block = Some(100);
        assert!(!full_census_due(&state, now, 100).unwrap());
        assert!(full_census_due(&state, now, 101).unwrap());
    }

    #[test]
    fn cycle_binding_gate_fails_closed() {
        let temp = tempfile::tempdir().unwrap();
        let error = require_runtime_binding(temp.path())
            .unwrap_err()
            .to_string();
        assert!(error.contains("runtime binding missing"));
    }
}
