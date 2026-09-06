use crate::v02::{ACTIONABILITY, PROSPECTIVE_PROTOCOL, PROTOCOL};
use crate::v02_freeze::{freeze_path, payload_hash, sha256_file, verify_seal};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Datelike, Duration, SecondsFormat, Timelike, Utc};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};

pub const MAX_LIVE_WRITE_AGE_MINUTES: i64 = 10;
pub const EXPECTED_CADENCE_MINUTES: i64 = 15;

pub fn write_live_observation(
    data_root: &Path,
    snapshot: &Value,
    derived_path: &Path,
    now: DateTime<Utc>,
) -> Result<Value> {
    let freeze_file = freeze_path(data_root);
    let freeze: Value = serde_json::from_reader(File::open(&freeze_file)?)?;
    if !verify_seal(&freeze)? {
        bail!("State V0.2 freeze seal invalid");
    }
    if !crate::v02_runtime_binding::verify_runtime_binding_file(
        &crate::v02_runtime_binding::runtime_binding_path(data_root),
    )? {
        bail!("State V0.2 Rust runtime binding missing, invalid, or stale");
    }
    if snapshot.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
        || snapshot.get("actionability").and_then(Value::as_str) != Some(ACTIONABILITY)
        || !snapshot.get("risk_multiplier").is_some_and(Value::is_null)
    {
        bail!("State V0.2 prospective ledger accepts descriptive-only snapshots");
    }
    for field in ["mutates_frozen_core", "mutates_state_v01", "mutates_state_ab_v01"] {
        if snapshot.get(field).and_then(Value::as_bool) != Some(false) {
            bail!("State V0.2 snapshot claims mutation of frozen predecessor");
        }
    }
    let generated = parse_time(snapshot.get("generated_at"))?;
    let first_eligible = parse_time(freeze.get("first_eligible_observed_at"))?;
    if generated < first_eligible {
        bail!("State V0.2 prospective observation predates freeze");
    }
    let age = now - generated;
    if age < Duration::zero() || age > Duration::minutes(MAX_LIVE_WRITE_AGE_MINUTES) {
        bail!("State V0.2 prospective observation is not live; backfill refused");
    }
    if !derived_path.exists() {
        bail!("State V0.2 derived state missing: {}", derived_path.display());
    }
    let mut payload = json!({
        "schema_version": 1,
        "protocol": PROSPECTIVE_PROTOCOL,
        "state_protocol": PROTOCOL,
        "freeze_record_sha256": freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        "known_at": now.to_rfc3339_opts(SecondsFormat::Micros, false),
        "as_of": snapshot.get("as_of").cloned().unwrap_or(Value::Null),
        "generated_at": generated.to_rfc3339_opts(SecondsFormat::Micros, false),
        "derived_state_path": derived_path.to_string_lossy(),
        "derived_state_sha256": sha256_file(derived_path)?,
        "actionability": ACTIONABILITY,
        "risk_multiplier": Value::Null,
        "data_confidence": snapshot.get("data_confidence").cloned().unwrap_or(Value::Null),
        "descriptive_stress_score": snapshot.get("descriptive_stress_score").cloned().unwrap_or(Value::Null),
        "valid_pressure_component_count": snapshot.get("valid_pressure_component_count").cloned().unwrap_or(Value::Null),
        "valid_pressure_components": snapshot.get("valid_pressure_components").cloned().unwrap_or_else(|| Value::Array(Vec::new())),
        "aave_market_pressure": snapshot.pointer("/components/aave_market_stress/pressure").cloned().unwrap_or(Value::Null),
        "stablecoin_flow_pressure": snapshot.pointer("/components/stablecoin_flow_decomposition/pressure").cloned().unwrap_or(Value::Null),
        "stablecoin_net_change_ratio": snapshot.pointer("/components/stablecoin_flow_decomposition/net_system_change_ratio").cloned().unwrap_or(Value::Null),
        "stablecoin_migration_ratio": snapshot.pointer("/components/stablecoin_flow_decomposition/migration_ratio").cloned().unwrap_or(Value::Null),
        "basis_dispersion_pressure": snapshot.pointer("/components/basis_dispersion/pressure").cloned().unwrap_or(Value::Null),
        "contagion_pressure": snapshot.pointer("/components/contagion_graph/pressure").cloned().unwrap_or(Value::Null),
        "borrower_health_factor_distribution_valid": snapshot.pointer("/borrower_health_factor_distribution/valid").cloned().unwrap_or(Value::Bool(false)),
        "aave_liquidation_events_24h": snapshot.pointer("/aave_liquidation_activity/events_24h").cloned().unwrap_or(Value::Null),
        "aave_liquidation_events_7d": snapshot.pointer("/aave_liquidation_activity/events_7d").cloned().unwrap_or(Value::Null),
        "deployment_activation_proxy": snapshot.pointer("/deployment_activation/coincident_activation_proxy").cloned().unwrap_or(Value::Null),
    });
    let hash = payload_hash(&payload)?;
    payload
        .as_object_mut()
        .context("prospective payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(hash));
    let path = observation_path(data_root, generated);
    if path.exists() {
        let existing: Value = serde_json::from_reader(File::open(&path)?)?;
        let expected = existing.get("record_sha256").and_then(Value::as_str);
        let computed = payload_hash(&existing)?;
        if expected != Some(computed.as_str()) {
            bail!("existing State V0.2 prospective record failed seal verification");
        }
        return Ok(existing);
    }
    write_atomic(&path, &payload)?;
    Ok(json!({"status":"written", "output": path, "record": payload}))
}

pub fn integrity_report(data_root: &Path) -> Result<Value> {
    let freeze_file = freeze_path(data_root);
    if !freeze_file.exists() {
        return Ok(json!({"protocol":PROSPECTIVE_PROTOCOL,"frozen":false,"ok":false,"error":"not_frozen"}));
    }
    let freeze: Value = serde_json::from_reader(File::open(&freeze_file)?)?;
    let freeze_ok = verify_seal(&freeze)?;
    let binding_ok = crate::v02_runtime_binding::verify_runtime_binding_file(
        &crate::v02_runtime_binding::runtime_binding_path(data_root),
    )?;
    let first_eligible = parse_time(freeze.get("first_eligible_observed_at"))?;
    let mut rows = load_observations(data_root)?;
    rows.sort_by_key(|row| parse_time(row.get("generated_at")).ok());
    let mut checks = serde_json::Map::new();
    checks.insert("freeze_seal".to_owned(), Value::Bool(freeze_ok));
    checks.insert("rust_runtime_binding".to_owned(), Value::Bool(binding_ok));
    let mut observation_seals = true;
    let mut freeze_links = true;
    let mut no_pre_freeze = true;
    let mut descriptive_only = true;
    let mut derived_links = true;
    let mut unique = true;
    let mut seen = BTreeSet::new();
    let mut times = Vec::new();
    for row in &rows {
        let computed = payload_hash(row)?;
        observation_seals &= row.get("record_sha256").and_then(Value::as_str) == Some(computed.as_str());
        freeze_links &= row.get("freeze_record_sha256") == freeze.get("record_sha256");
        let ts = parse_time(row.get("generated_at"))?;
        times.push(ts);
        no_pre_freeze &= ts >= first_eligible;
        unique &= seen.insert(ts.to_rfc3339());
        descriptive_only &= row.get("actionability").and_then(Value::as_str) == Some(ACTIONABILITY)
            && row.get("risk_multiplier").is_some_and(Value::is_null);
        let derived = row.get("derived_state_path").and_then(Value::as_str).map(PathBuf::from);
        let expected = row.get("derived_state_sha256").and_then(Value::as_str);
        derived_links &= derived.as_ref().is_some_and(|path| path.exists())
            && derived.as_ref().and_then(|path| sha256_file(path).ok()).as_deref() == expected;
    }
    let monotonic = times.windows(2).all(|pair| pair[0] <= pair[1]);
    checks.insert("observation_seals".to_owned(), Value::Bool(observation_seals));
    checks.insert("freeze_links".to_owned(), Value::Bool(freeze_links));
    checks.insert("no_pre_freeze_observations".to_owned(), Value::Bool(no_pre_freeze));
    checks.insert("descriptive_only".to_owned(), Value::Bool(descriptive_only));
    checks.insert("derived_state_hash_links".to_owned(), Value::Bool(derived_links));
    checks.insert("generated_at_unique".to_owned(), Value::Bool(unique));
    checks.insert("generated_at_monotonic".to_owned(), Value::Bool(monotonic));
    let ok = checks.values().all(|value| value.as_bool() == Some(true));
    let mut gap_count = 0_u64;
    let mut max_gap = 0.0_f64;
    for pair in times.windows(2) {
        let minutes = (pair[1] - pair[0]).num_seconds() as f64 / 60.0;
        max_gap = max_gap.max(minutes);
        if minutes > EXPECTED_CADENCE_MINUTES as f64 * 2.25 {
            gap_count += 1;
        }
    }
    Ok(json!({
        "protocol": PROSPECTIVE_PROTOCOL,
        "frozen": true,
        "ok": ok,
        "audit_level": "STRICT_NON_MUTATING_HASH_GRAPH_RUST_BOUND",
        "observation_count": rows.len(),
        "first_observation_at": times.first().map(DateTime::<Utc>::to_rfc3339),
        "last_observation_at": times.last().map(DateTime::<Utc>::to_rfc3339),
        "gap_count": gap_count,
        "max_gap_minutes": max_gap,
        "gaps_are_visible_not_backfilled": true,
        "checks": checks,
    }))
}

fn observation_path(data_root: &Path, generated: DateTime<Utc>) -> PathBuf {
    data_root
        .join("research/state_v02/prospective")
        .join(format!("year={:04}", generated.year()))
        .join(format!("month={:02}", generated.month()))
        .join(format!("day={:02}", generated.day()))
        .join(format!(
            "state_at={:02}{:02}{:02}{:06}.json",
            generated.hour(),
            generated.minute(),
            generated.second(),
            generated.timestamp_subsec_micros()
        ))
}

fn load_observations(data_root: &Path) -> Result<Vec<Value>> {
    let root = data_root.join("research/state_v02/prospective");
    if !root.exists() {
        return Ok(Vec::new());
    }
    let mut result = Vec::new();
    for year in fs::read_dir(&root)? {
        let year = year?.path();
        if !year.is_dir() {
            continue;
        }
        for month in fs::read_dir(year)? {
            let month = month?.path();
            if !month.is_dir() {
                continue;
            }
            for day in fs::read_dir(month)? {
                let day = day?.path();
                if !day.is_dir() {
                    continue;
                }
                for file in fs::read_dir(day)? {
                    let path = file?.path();
                    if path.extension().and_then(|value| value.to_str()) == Some("json") {
                        result.push(serde_json::from_reader(File::open(path)?)?);
                    }
                }
            }
        }
    }
    Ok(result)
}

fn parse_time(value: Option<&Value>) -> Result<DateTime<Utc>> {
    let text = value.and_then(Value::as_str).context("timestamp missing")?;
    Ok(DateTime::parse_from_rfc3339(text)?.with_timezone(&Utc))
}

fn write_atomic(path: &Path, value: &Value) -> Result<()> {
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
