use crate::v03::{ACTIONABILITY, PROSPECTIVE_PROTOCOL, PROTOCOL};
use crate::v03_freeze::{freeze_path, payload_hash, verify_legacy_v1_seal};
use crate::v03_runtime_binding::{runtime_binding_path, verify_runtime_binding_file};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub fn write_full_census_observation(
    data_root: &Path,
    summary_path: &Path,
    detail_path: &Path,
    known_at: DateTime<Utc>,
) -> Result<Value> {
    let freeze = load_freeze(data_root)?;
    verify_hash_graph(data_root, &freeze)?;
    let binding_path = runtime_binding_path(data_root);
    if !verify_runtime_binding_file(&binding_path)? {
        bail!("STATE_V03_RUST_RUNTIME_BINDING_INVALID");
    }
    let binding: Value = serde_json::from_reader(File::open(&binding_path)?)?;
    let binding_file_sha = sha256_file(&binding_path)?;
    let binding_record_sha = binding
        .get("record_sha256")
        .cloned()
        .context("State V0.3 runtime binding record_sha256 missing")?;
    if !summary_path.exists() || !detail_path.exists() {
        bail!("full census summary/detail artifact missing");
    }

    let summary: Value = serde_json::from_reader(File::open(summary_path)?)?;
    if summary.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
        bail!("full census artifact is not State V0.3");
    }
    if summary.get("actionability").and_then(Value::as_str) != Some(ACTIONABILITY)
        || !summary.get("risk_multiplier").is_some_and(Value::is_null)
    {
        bail!("State V0.3 prospective ledger accepts descriptive-only censuses");
    }
    if summary.get("bootstrap_complete").and_then(Value::as_bool) != Some(true)
        || summary.get("valid_full_census").and_then(Value::as_bool) != Some(true)
    {
        bail!("prospective State V0.3 requires a valid complete full census");
    }
    if summary.get("detail_path").and_then(Value::as_str) != Some(&*detail_path.to_string_lossy()) {
        bail!("full census summary points to a different detail artifact");
    }
    let detail_hash = sha256_file(detail_path)?;
    if summary.get("detail_sha256").and_then(Value::as_str) != Some(detail_hash.as_str()) {
        bail!("full census detail hash does not match its summary artifact");
    }

    let frozen_at = parse_time(&freeze, "frozen_at")?;
    let captured = parse_time(&summary, "captured_at")?;
    let block_time = parse_time(&summary, "block_time")?;
    if known_at < frozen_at {
        bail!("prospective census known_at cannot predate State V0.3 freeze");
    }
    if captured < frozen_at {
        bail!("prospective census captured_at cannot predate State V0.3 freeze");
    }
    if known_at < captured {
        bail!("prospective census known_at cannot precede captured_at");
    }
    if block_time > captured {
        bail!("prospective census block_time cannot follow captured_at");
    }

    let block_number = summary
        .get("block_number")
        .and_then(Value::as_u64)
        .context("full census block_number missing")?;
    let minimum_block = freeze
        .get("minimum_eligible_block")
        .and_then(Value::as_u64)
        .context("State V0.3 freeze minimum_eligible_block missing")?;
    if block_number < minimum_block {
        bail!("prospective census block predates the frozen minimum eligible block; backfill refused");
    }

    let payload = json!({
        "schema_version": 1,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": PROTOCOL,
        "freeze_record_sha256": freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        "rust_runtime_binding_path": binding_path.to_string_lossy(),
        "rust_runtime_binding_file_sha256": binding_file_sha,
        "rust_runtime_binding_record_sha256": binding_record_sha,
        "known_at": known_at.to_rfc3339_opts(SecondsFormat::Micros, false),
        "captured_at": captured.to_rfc3339_opts(SecondsFormat::Micros, false),
        "block_time": block_time.to_rfc3339_opts(SecondsFormat::Micros, false),
        "block_number": block_number,
        "minimum_eligible_block": minimum_block,
        "summary_path": summary_path.to_string_lossy(),
        "summary_sha256": sha256_file(summary_path)?,
        "detail_path": detail_path.to_string_lossy(),
        "detail_sha256": detail_hash,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "candidate_address_count": summary.get("candidate_address_count").cloned().unwrap_or(Value::Null),
        "account_call_coverage_ratio": summary.get("account_call_coverage_ratio").cloned().unwrap_or(Value::Null),
        "active_borrower_count": summary.get("active_borrower_count").cloned().unwrap_or(Value::Null),
        "total_active_debt_usd": summary.get("total_active_debt_usd").cloned().unwrap_or(Value::Null),
        "debt_weighted_hf_p10": summary.get("debt_weighted_hf_p10").cloned().unwrap_or(Value::Null),
        "debt_weighted_hf_p25": summary.get("debt_weighted_hf_p25").cloned().unwrap_or(Value::Null),
        "debt_weighted_hf_p50": summary.get("debt_weighted_hf_p50").cloned().unwrap_or(Value::Null),
        "liquidatable_debt_share": summary.get("liquidatable_debt_share").cloned().unwrap_or(Value::Null),
        "critical_hf_le_1_05_debt_share": summary.get("critical_hf_le_1_05_debt_share").cloned().unwrap_or(Value::Null),
        "near_cliff_hf_le_1_20_debt_share": summary.get("near_cliff_hf_le_1_20_debt_share").cloned().unwrap_or(Value::Null),
        "watchlist_count": summary.get("watchlist_count").cloned().unwrap_or(Value::Null),
    });
    let path = record_path(data_root, block_number);
    if path.exists() {
        let existing: Value = serde_json::from_reader(File::open(&path)?)?;
        if !verify_seal(&existing)? {
            bail!("existing block record failed seal verification: {}", path.display());
        }
        for key in [
            "summary_sha256",
            "detail_sha256",
            "freeze_record_sha256",
            "rust_runtime_binding_file_sha256",
            "rust_runtime_binding_record_sha256",
        ] {
            if existing.get(key) != payload.get(key) {
                bail!("STATE_V03_BLOCK_COLLISION: same finalized block cannot be relabeled with different {key}");
            }
        }
        return Ok(merge_status(existing, "already_exists", &path));
    }

    let sealed = seal(payload)?;
    write_immutable_json(&path, &sealed)?;
    Ok(merge_status(sealed, "written", &path))
}

pub fn verify_prospective_record(path: &Path) -> Result<bool> {
    if !path.exists() {
        return Ok(false);
    }
    let value: Value = serde_json::from_reader(File::open(path)?)?;
    verify_seal(&value)
}

pub fn prospective_integrity(data_root: &Path) -> Result<Value> {
    let freeze = load_freeze(data_root)?;
    let binding_path = runtime_binding_path(data_root);
    let binding_ok = verify_runtime_binding_file(&binding_path)?;
    let binding = binding_ok
        .then(|| serde_json::from_reader::<_, Value>(File::open(&binding_path)?))
        .transpose()?;
    let binding_file_sha = binding_ok.then(|| sha256_file(&binding_path)).transpose()?;
    let binding_record_sha = binding
        .as_ref()
        .and_then(|value| value.get("record_sha256"));
    let root = data_root.join("research/state_v03/prospective");
    let mut paths = Vec::new();
    if root.exists() {
        for entry in fs::read_dir(&root)? {
            let path = entry?.path();
            if path.extension().and_then(|value| value.to_str()) == Some("json") {
                paths.push(path);
            }
        }
    }
    paths.sort();

    let mut seals_ok = true;
    let mut freeze_links = true;
    let mut runtime_links = true;
    let mut artifact_links = true;
    let mut descriptive_only = true;
    let mut native_binding_linked_count = 0_usize;
    for path in &paths {
        let value: Value = serde_json::from_reader(File::open(path)?)?;
        seals_ok &= verify_seal(&value)?;
        freeze_links &= value.get("freeze_record_sha256") == freeze.get("record_sha256");
        descriptive_only &= value.get("actionability").and_then(Value::as_str) == Some(ACTIONABILITY)
            && value.get("risk_multiplier").is_some_and(Value::is_null);
        if value.get("rust_runtime_binding_record_sha256").is_some() {
            native_binding_linked_count += 1;
            runtime_links &= value.get("rust_runtime_binding_record_sha256") == binding_record_sha;
            runtime_links &= value
                .get("rust_runtime_binding_file_sha256")
                .and_then(Value::as_str)
                == binding_file_sha.as_deref();
        }
        for (path_key, sha_key) in [
            ("summary_path", "summary_sha256"),
            ("detail_path", "detail_sha256"),
        ] {
            let artifact = value.get(path_key).and_then(Value::as_str).map(PathBuf::from);
            let expected = value.get(sha_key).and_then(Value::as_str);
            artifact_links &= artifact.as_ref().is_some_and(|path| path.exists())
                && artifact
                    .as_ref()
                    .and_then(|path| sha256_file(path).ok())
                    .as_deref()
                    == expected;
        }
    }
    let ok = binding_ok && seals_ok && freeze_links && runtime_links && artifact_links && descriptive_only;
    Ok(json!({
        "protocol": PROSPECTIVE_PROTOCOL,
        "ok": ok,
        "record_count": paths.len(),
        "native_binding_linked_count": native_binding_linked_count,
        "record_seals": seals_ok,
        "freeze_links": freeze_links,
        "rust_runtime_binding": binding_ok,
        "rust_runtime_binding_links": runtime_links,
        "artifact_hash_links": artifact_links,
        "descriptive_only": descriptive_only,
    }))
}

fn load_freeze(data_root: &Path) -> Result<Value> {
    let path = freeze_path(data_root);
    if !path.exists() {
        bail!("State V0.3 freeze missing: {}", path.display());
    }
    let value: Value = serde_json::from_reader(File::open(&path)?)?;
    if !verify_legacy_v1_seal(&value)? {
        bail!("State V0.3 freeze failed seal verification");
    }
    Ok(value)
}

fn verify_hash_graph(data_root: &Path, freeze: &Value) -> Result<()> {
    let expected_impl = freeze
        .get("implementation_file_sha256")
        .and_then(Value::as_object)
        .context("freeze implementation_file_sha256 missing")?;
    for (name, relative) in implementation_files() {
        let expected = expected_impl.get(name).and_then(Value::as_str);
        let current = sha256_file(&repo_root().join(relative))?;
        if expected != Some(current.as_str()) {
            bail!("STATE_V03_HASH_GRAPH_MUTATED: implementation {name}");
        }
    }
    let expected_refs = freeze
        .get("reference_freezes")
        .and_then(Value::as_object)
        .context("freeze reference_freezes missing")?;
    for (name, path) in reference_paths(data_root) {
        let expected = expected_refs
            .get(name)
            .and_then(Value::as_object)
            .and_then(|row| row.get("file_sha256"))
            .and_then(Value::as_str);
        let current = sha256_file(&path)?;
        if expected != Some(current.as_str()) {
            bail!("STATE_V03_HASH_GRAPH_MUTATED: reference {name}");
        }
    }
    Ok(())
}

fn implementation_files() -> [(&'static str, &'static str); 9] {
    [
        ("state_v03", "src/crossalpha/state/v03.py"),
        ("state_v03_rpc", "src/crossalpha/state/v03_rpc.py"),
        ("state_v03_logs", "src/crossalpha/state/v03_logs.py"),
        ("state_v03_preflight", "src/crossalpha/state/v03_preflight.py"),
        ("state_v03_cycle", "src/crossalpha/state/v03_cycle.py"),
        ("state_v03_watchlist", "src/crossalpha/state/v03_watchlist.py"),
        ("state_v03_prospective", "src/crossalpha/state/v03_prospective.py"),
        ("state_v03_config", "src/crossalpha/state/v03_config.py"),
        ("config", "config/state_v03.yaml"),
    ]
}

fn reference_paths(data_root: &Path) -> [(&'static str, PathBuf); 3] {
    [
        ("frozen_b3", data_root.join("research/free_v01/paper/freeze.json")),
        ("state_ab_v01", data_root.join("research/free_v01/state_ab_v01/freeze.json")),
        ("state_v02", data_root.join("research/state_v02/freeze.json")),
    ]
}

fn parse_time(value: &Value, key: &str) -> Result<DateTime<Utc>> {
    let raw = value
        .get(key)
        .and_then(Value::as_str)
        .with_context(|| format!("{key} missing"))?;
    Ok(DateTime::parse_from_rfc3339(raw)?.with_timezone(&Utc))
}

fn record_path(data_root: &Path, block_number: u64) -> PathBuf {
    data_root
        .join("research/state_v03/prospective")
        .join(format!("block={block_number}.json"))
}

fn seal(mut value: Value) -> Result<Value> {
    let digest = payload_hash(&value)?;
    value
        .as_object_mut()
        .context("prospective payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(digest));
    Ok(value)
}

fn verify_seal(value: &Value) -> Result<bool> {
    let expected = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

fn write_immutable_json(path: &Path, value: &Value) -> Result<()> {
    if path.exists() {
        bail!("immutable record already exists: {}", path.display());
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
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

fn merge_status(mut value: Value, status: &str, path: &Path) -> Value {
    if let Some(object) = value.as_object_mut() {
        object.insert("status".to_owned(), Value::String(status.to_owned()));
        object.insert(
            "output".to_owned(),
            Value::String(path.to_string_lossy().into_owned()),
        );
    }
    value
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path).with_context(|| format!("open {}", path.display()))?;
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

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prospective_record_path_is_block_keyed() {
        let path = record_path(Path::new("/tmp/data"), 123);
        assert!(path.ends_with("research/state_v03/prospective/block=123.json"));
    }
}
