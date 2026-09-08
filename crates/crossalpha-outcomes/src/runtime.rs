use crate::{OutcomeMark, SourceRecord, expected_dates, outcome_metrics, select_daily_anchors};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Datelike, NaiveDate, Utc};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

pub const PROTOCOL: &str = "CROSSALPHA_OUTCOME_LINKAGE_V0_1";
pub const MODE: &str = "PROSPECTIVE_STATE_TO_REALIZED_OUTCOME_LINKAGE";
pub const BINDING_PROTOCOL: &str = "CROSSALPHA_OUTCOME_LINKAGE_V0_1_RUST_RUNTIME_BINDING";
pub const HORIZONS_DAYS: [usize; 5] = [1, 3, 7, 14, 28];
pub const SOURCE_LAYERS: [&str; 3] = ["STATE_V02", "STATE_V03", "STATE_V04"];

pub fn root(data_root: &Path) -> PathBuf {
    data_root.join("research/outcome_linkage_v01")
}

pub fn freeze_path(data_root: &Path) -> PathBuf {
    root(data_root).join("freeze.json")
}

pub fn binding_path(data_root: &Path) -> PathBuf {
    root(data_root).join("rust_runtime_binding.json")
}

pub fn load_legacy_freeze(data_root: &Path) -> Result<Value> {
    let path = freeze_path(data_root);
    if !path.exists() {
        bail!("Outcome Linkage legacy freeze missing: {}", path.display());
    }
    let value: Value = serde_json::from_reader(File::open(&path)?)?;
    if !verify_seal(&value)? {
        bail!("Outcome Linkage legacy freeze failed seal verification");
    }
    if value.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
        bail!("Outcome Linkage legacy freeze protocol mismatch");
    }
    verify_reference_freezes(data_root, &value)?;
    Ok(value)
}

pub fn runtime_binding_preview(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let freeze = load_legacy_freeze(data_root)?;
    let repo = repo_root();
    let cargo_lock = repo.join("Cargo.lock");
    let lock_present = cargo_lock.is_file();
    let lock_tracked = lock_present && git_tracks(&repo, "Cargo.lock");
    let mut payload = json!({
        "schema_version":1,
        "protocol":BINDING_PROTOCOL,
        "outcome_protocol":PROTOCOL,
        "legacy_freeze":{
            "path":freeze_path(data_root),
            "file_sha256":sha256_file(&freeze_path(data_root))?,
            "record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        },
        "bound_at":bound_at.to_rfc3339(),
        "runtime":"RUST",
        "python_runtime_required":false,
        "cargo_lock_present":lock_present,
        "cargo_lock_tracked":lock_tracked,
        "cargo_lock_sha256":lock_present.then(||sha256_file(&cargo_lock)).transpose()?,
        "native_source_sha256":native_source_hashes(&repo)?,
        "production_binding_eligible":lock_present && lock_tracked,
    });
    seal_in_place(&mut payload)?;
    Ok(payload)
}

pub fn write_runtime_binding(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let path = binding_path(data_root);
    if path.exists() {
        if !verify_runtime_binding_file(&path)? {
            bail!("existing Outcome Linkage Rust runtime binding invalid or stale");
        }
        return Ok(serde_json::from_reader(File::open(path)?)?);
    }
    let value = runtime_binding_preview(data_root, bound_at)?;
    if value
        .get("production_binding_eligible")
        .and_then(Value::as_bool)
        != Some(true)
    {
        bail!("Outcome Linkage Rust runtime binding refused: Cargo.lock must exist and be tracked");
    }
    write_atomic_json(&path, &value)?;
    Ok(value)
}

pub fn verify_runtime_binding_file(path: &Path) -> Result<bool> {
    if !path.exists() {
        return Ok(false);
    }
    let current: Value = serde_json::from_reader(File::open(path)?)?;
    if !verify_seal(&current)? {
        return Ok(false);
    }
    let bound_at = parse_time(current.get("bound_at"))?;
    let data_root = path
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .context("Outcome binding path is not under <data_root>/research/outcome_linkage_v01")?;
    Ok(runtime_binding_preview(data_root, bound_at)? == current)
}

pub fn materialize(data_root: &Path, now: DateTime<Utc>) -> Result<Value> {
    require_binding(data_root)?;
    let freeze = load_legacy_freeze(data_root)?;
    let frozen_at = parse_time(freeze.get("frozen_at"))?;
    let source_values = load_source_values(data_root)?;
    let source_records: Vec<SourceRecord> = source_values
        .iter()
        .map(|source| SourceRecord {
            source_layer: source.layer.clone(),
            known_at: source.known_at,
            record_sha256: source.record_sha256.clone(),
            path: Some(source.path.to_string_lossy().to_string()),
        })
        .collect();
    let anchors = select_daily_anchors(&source_records, frozen_at);
    let source_by_key: BTreeMap<(String, String), &SourceValue> = source_values
        .iter()
        .map(|row| ((row.layer.clone(), row.record_sha256.clone()), row))
        .collect();
    let (a_marks, b_marks, a_values, b_values) = load_marks(data_root)?;
    let mut written = 0usize;
    let mut existing = 0usize;
    let mut pending = 0usize;

    for anchor in &anchors {
        let source = source_by_key
            .get(&(anchor.source_layer.clone(), anchor.record_sha256.clone()))
            .context("selected outcome anchor source row missing")?;
        for horizon in HORIZONS_DAYS {
            let dates = expected_dates(anchor.known_at, horizon);
            if dates
                .iter()
                .any(|day| !a_marks.contains_key(day) || !b_marks.contains_key(day))
            {
                pending += 1;
                continue;
            }
            let latest_known = dates
                .iter()
                .flat_map(|day| {
                    [
                        parse_time(a_values.get(day).and_then(|value| value.get("known_at"))).ok(),
                        parse_time(b_values.get(day).and_then(|value| value.get("known_at"))).ok(),
                    ]
                })
                .flatten()
                .max()
                .context("outcome marks missing known_at")?;
            if latest_known > now {
                pending += 1;
                continue;
            }
            let metrics = outcome_metrics(&dates, &a_marks, &b_marks)?;
            let anchor_day = anchor.known_at.date_naive();
            let path = link_path(data_root, &anchor.source_layer, anchor_day, horizon);
            let a_links = dates
                .iter()
                .map(|day| {
                    let value = &a_values[day];
                    json!({
                        "date":day,
                        "record_sha256":value.get("record_sha256").cloned().unwrap_or(Value::Null),
                        "path":core_mark_path(data_root,*day),
                    })
                })
                .collect::<Vec<_>>();
            let b_links = dates
                .iter()
                .map(|day| {
                    let value = &b_values[day];
                    json!({
                        "date":day,
                        "record_sha256":value.get("record_sha256").cloned().unwrap_or(Value::Null),
                        "path":ab_mark_path(data_root,*day),
                        "a_mark_record_sha256":value.get("a_mark_record_sha256").cloned().unwrap_or(Value::Null),
                    })
                })
                .collect::<Vec<_>>();
            let mut payload = json!({
                "schema_version":1,
                "protocol":PROTOCOL,
                "mode":MODE,
                "freeze_record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
                "materialized_at":now.to_rfc3339(),
                "latest_outcome_mark_known_at":latest_known.to_rfc3339(),
                "source_layer":anchor.source_layer,
                "source_record_path":source.path,
                "source_record_file_sha256":sha256_file(&source.path)?,
                "source_record_sha256":anchor.record_sha256,
                "source_known_at":anchor.known_at.to_rfc3339(),
                "anchor_date":anchor_day,
                "anchor_selection":"latest_known_at_within_source_utc_day",
                "source_features":source_features(&anchor.source_layer,&source.value),
                "horizon_days":horizon,
                "outcome_start_date":dates.first(),
                "outcome_end_date":dates.last(),
                "same_day_outcome_included":false,
                "complete_daily_marks_required":true,
                "A_cumulative_net_return":metrics.a_cumulative_net_return,
                "B_cumulative_net_return":metrics.b_cumulative_net_return,
                "B_minus_A_cumulative_return":metrics.b_minus_a_cumulative_return,
                "cash_cumulative_return":metrics.cash_cumulative_return,
                "A_max_drawdown":metrics.a_max_drawdown,
                "B_max_drawdown":metrics.b_max_drawdown,
                "A_worst_daily_return":metrics.a_worst_daily_return,
                "B_worst_daily_return":metrics.b_worst_daily_return,
                "A_negative_day_count":metrics.a_negative_day_count,
                "B_negative_day_count":metrics.b_negative_day_count,
                "intervention_day_count":metrics.intervention_day_count,
                "average_B_multiplier":metrics.average_b_multiplier,
                "A_mark_links":a_links,
                "B_mark_links":b_links,
                "actionability":"NONE",
                "risk_multiplier":Value::Null,
                "selective_linking_allowed":false,
            });
            seal_in_place(&mut payload)?;
            if path.exists() {
                let existing_value = read_verified(&path)?;
                for field in [
                    "source_record_sha256",
                    "source_layer",
                    "anchor_date",
                    "horizon_days",
                    "outcome_start_date",
                    "outcome_end_date",
                ] {
                    if existing_value.get(field) != payload.get(field) {
                        bail!("OUTCOME_LINK_COLLISION: {}", path.display());
                    }
                }
                existing += 1;
            } else {
                write_atomic_json(&path, &payload)?;
                written += 1;
            }
        }
    }
    Ok(json!({
        "protocol":PROTOCOL,
        "status":"materialized",
        "anchor_count":anchors.len(),
        "written_links":written,
        "existing_links":existing,
        "pending_incomplete_horizons":pending,
        "horizons_days":HORIZONS_DAYS,
        "same_day_outcome_allowed":false,
        "selective_linking_allowed":false,
        "deterministic_late_materialization_allowed":true,
        "python_runtime_required":false,
    }))
}

pub fn integrity(data_root: &Path) -> Result<Value> {
    let freeze = load_legacy_freeze(data_root)?;
    let binding_ok = verify_runtime_binding_file(&binding_path(data_root))?;
    let links = load_records(&root(data_root).join("links"), "horizon=")?;
    let seals_ok = links
        .iter()
        .all(|(_, value)| verify_seal(value).unwrap_or(false));
    let freeze_links = links
        .iter()
        .all(|(_, value)| value.get("freeze_record_sha256") == freeze.get("record_sha256"));
    let source_hash_links = links.iter().all(|(_, value)| {
        let Some(path) = value.get("source_record_path").and_then(Value::as_str) else {
            return false;
        };
        let expected = value
            .get("source_record_file_sha256")
            .and_then(Value::as_str);
        let path = Path::new(path);
        path.exists() && sha256_file(path).ok().as_deref() == expected
    });
    let policy_ok = links.iter().all(|(_, value)| {
        value
            .get("same_day_outcome_included")
            .and_then(Value::as_bool)
            == Some(false)
            && value
                .get("selective_linking_allowed")
                .and_then(Value::as_bool)
                == Some(false)
            && value.get("actionability").and_then(Value::as_str) == Some("NONE")
            && value.get("risk_multiplier").is_some_and(Value::is_null)
    });
    let ok = binding_ok && seals_ok && freeze_links && source_hash_links && policy_ok;
    Ok(json!({
        "protocol":PROTOCOL,
        "ok":ok,
        "runtime_binding":binding_ok,
        "link_seals":seals_ok,
        "freeze_links":freeze_links,
        "source_file_hash_links":source_hash_links,
        "policy_invariants":policy_ok,
        "link_count":links.len(),
        "python_runtime_required":false,
    }))
}

#[derive(Debug)]
struct SourceValue {
    layer: String,
    known_at: DateTime<Utc>,
    record_sha256: String,
    path: PathBuf,
    value: Value,
}

fn load_source_values(data_root: &Path) -> Result<Vec<SourceValue>> {
    let sources = [
        (
            "STATE_V02",
            data_root.join("research/state_v02/prospective"),
            "state_at=",
        ),
        (
            "STATE_V03",
            data_root.join("research/state_v03/prospective"),
            "block=",
        ),
        (
            "STATE_V04",
            data_root.join("research/state_v04/prospective"),
            "state_at=",
        ),
    ];
    let mut result = Vec::new();
    for (layer, root, prefix) in sources {
        for (path, value) in load_records(&root, prefix)? {
            let known_at = parse_time(value.get("known_at"))?;
            let record_sha256 = value
                .get("record_sha256")
                .and_then(Value::as_str)
                .context("source record_sha256 missing")?
                .to_owned();
            result.push(SourceValue {
                layer: layer.to_owned(),
                known_at,
                record_sha256,
                path,
                value,
            });
        }
    }
    Ok(result)
}

type MarkLoad = (
    BTreeMap<NaiveDate, OutcomeMark>,
    BTreeMap<NaiveDate, OutcomeMark>,
    BTreeMap<NaiveDate, Value>,
    BTreeMap<NaiveDate, Value>,
);

fn load_marks(data_root: &Path) -> Result<MarkLoad> {
    let mut a = BTreeMap::new();
    let mut b = BTreeMap::new();
    let mut a_values = BTreeMap::new();
    let mut b_values = BTreeMap::new();
    for (path, value) in load_records(&data_root.join("research/free_v01/paper/marks"), "date=")? {
        let date = parse_date(value.get("date"))?;
        a.insert(
            date,
            OutcomeMark {
                date,
                record_sha256: value
                    .get("record_sha256")
                    .and_then(Value::as_str)
                    .context("A mark sha missing")?
                    .to_owned(),
                net_return: value
                    .get("net_return")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
                cash_return: value
                    .get("cash_return")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
                a_mark_record_sha256: None,
                shadow_risk_multiplier: None,
                path: Some(path.to_string_lossy().to_string()),
            },
        );
        a_values.insert(date, value);
    }
    for (path, value) in load_records(
        &data_root.join("research/free_v01/state_ab_v01/marks"),
        "date=",
    )? {
        let date = parse_date(value.get("date"))?;
        b.insert(
            date,
            OutcomeMark {
                date,
                record_sha256: value
                    .get("record_sha256")
                    .and_then(Value::as_str)
                    .context("B mark sha missing")?
                    .to_owned(),
                net_return: value
                    .get("net_return")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
                cash_return: value
                    .get("cash_return")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0),
                a_mark_record_sha256: value
                    .get("a_mark_record_sha256")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                shadow_risk_multiplier: value.get("shadow_risk_multiplier").and_then(Value::as_f64),
                path: Some(path.to_string_lossy().to_string()),
            },
        );
        b_values.insert(date, value);
    }
    Ok((a, b, a_values, b_values))
}

fn source_features(layer: &str, row: &Value) -> Value {
    match layer {
        "STATE_V02" => pick_fields(
            row,
            &[
                "data_confidence",
                "descriptive_stress_score",
                "aave_market_pressure",
                "stablecoin_flow_pressure",
                "stablecoin_net_change_ratio",
                "stablecoin_migration_ratio",
                "basis_dispersion_pressure",
                "contagion_pressure",
                "aave_liquidation_events_24h",
                "aave_liquidation_events_7d",
                "deployment_activation_proxy",
            ],
        ),
        "STATE_V03" => pick_fields(
            row,
            &[
                "account_call_coverage_ratio",
                "active_borrower_count",
                "total_active_debt_usd",
                "debt_weighted_hf_p10",
                "debt_weighted_hf_p25",
                "debt_weighted_hf_p50",
                "liquidatable_debt_share",
                "critical_hf_le_1_05_debt_share",
                "near_cliff_hf_le_1_20_debt_share",
                "watchlist_count",
            ],
        ),
        "STATE_V04" => {
            let mut result = Map::new();
            result.insert(
                "data_confidence".to_owned(),
                row.get("data_confidence").cloned().unwrap_or(Value::Null),
            );
            result.insert(
                "funding_semantics".to_owned(),
                row.get("funding_semantics").cloned().unwrap_or(Value::Null),
            );
            for asset in ["BTC", "ETH"] {
                for key in [
                    "valid_venue_count",
                    "funding_comparable_venue_count",
                    "spot_cross_venue_range_bps",
                    "basis_median_bps",
                    "basis_range_bps",
                    "basis_std_bps",
                    "funding_8h_median",
                    "funding_8h_range",
                    "perp_spread_median_bps",
                    "perp_spread_max_bps",
                    "total_open_interest_usd",
                    "open_interest_hhi",
                ] {
                    result.insert(
                        format!("{asset}_{key}"),
                        row.pointer(&format!("/assets/{asset}/{key}"))
                            .cloned()
                            .unwrap_or(Value::Null),
                    );
                }
            }
            Value::Object(result)
        }
        _ => Value::Object(Map::new()),
    }
}

fn pick_fields(row: &Value, fields: &[&str]) -> Value {
    Value::Object(
        fields
            .iter()
            .map(|field| {
                (
                    (*field).to_owned(),
                    row.get(*field).cloned().unwrap_or(Value::Null),
                )
            })
            .collect(),
    )
}

fn verify_reference_freezes(data_root: &Path, freeze: &Value) -> Result<()> {
    let references = [
        (
            "frozen_b3",
            data_root.join("research/free_v01/paper/freeze.json"),
        ),
        (
            "state_ab_v01",
            data_root.join("research/free_v01/state_ab_v01/freeze.json"),
        ),
        (
            "state_v02",
            data_root.join("research/state_v02/freeze.json"),
        ),
        (
            "state_v03",
            data_root.join("research/state_v03/freeze.json"),
        ),
        (
            "state_v04",
            data_root.join("research/state_v04/freeze.json"),
        ),
    ];
    for (name, path) in references {
        let expected = freeze
            .pointer(&format!("/reference_freezes/{name}/file_sha256"))
            .and_then(Value::as_str)
            .with_context(|| format!("Outcome legacy freeze missing reference {name}"))?;
        if !path.exists() || sha256_file(&path)? != expected {
            bail!("Outcome Linkage predecessor freeze changed: {name}");
        }
    }
    Ok(())
}

fn load_records(root: &Path, prefix: &str) -> Result<Vec<(PathBuf, Value)>> {
    if !root.exists() {
        return Ok(Vec::new());
    }
    let mut paths = Vec::new();
    collect_json(root, prefix, &mut paths)?;
    paths.sort();
    paths
        .into_iter()
        .map(|path| {
            let value = read_verified(&path)?;
            Ok((path, value))
        })
        .collect()
}

fn collect_json(root: &Path, prefix: &str, out: &mut Vec<PathBuf>) -> Result<()> {
    for entry in fs::read_dir(root)? {
        let path = entry?.path();
        if path.is_dir() {
            collect_json(&path, prefix, out)?;
        } else if path.extension().and_then(|value| value.to_str()) == Some("json")
            && path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.starts_with(prefix))
        {
            out.push(path);
        }
    }
    Ok(())
}

fn read_verified(path: &Path) -> Result<Value> {
    let value: Value = serde_json::from_reader(File::open(path)?)?;
    if !verify_seal(&value)? {
        bail!(
            "immutable Outcome/source record failed seal verification: {}",
            path.display()
        );
    }
    Ok(value)
}

fn link_path(data_root: &Path, source: &str, day: NaiveDate, horizon: usize) -> PathBuf {
    root(data_root)
        .join("links")
        .join(format!("source={source}"))
        .join(format!("year={:04}", day.year()))
        .join(format!("month={:02}", day.month()))
        .join(format!("anchor_date={day}"))
        .join(format!("horizon={horizon:02}d.json"))
}

fn core_mark_path(data_root: &Path, day: NaiveDate) -> PathBuf {
    data_root
        .join("research/free_v01/paper/marks")
        .join(format!("year={:04}", day.year()))
        .join(format!("month={:02}", day.month()))
        .join(format!("date={day}.json"))
}

fn ab_mark_path(data_root: &Path, day: NaiveDate) -> PathBuf {
    data_root
        .join("research/free_v01/state_ab_v01/marks")
        .join(format!("year={:04}", day.year()))
        .join(format!("month={:02}", day.month()))
        .join(format!("date={day}.json"))
}

fn parse_date(value: Option<&Value>) -> Result<NaiveDate> {
    Ok(NaiveDate::parse_from_str(
        value.and_then(Value::as_str).context("date missing")?,
        "%Y-%m-%d",
    )?)
}

fn parse_time(value: Option<&Value>) -> Result<DateTime<Utc>> {
    Ok(
        DateTime::parse_from_rfc3339(value.and_then(Value::as_str).context("timestamp missing")?)?
            .with_timezone(&Utc),
    )
}

fn require_binding(data_root: &Path) -> Result<()> {
    if !verify_runtime_binding_file(&binding_path(data_root))? {
        bail!("Outcome Linkage native runtime binding missing, invalid, or stale");
    }
    Ok(())
}

pub fn verify_seal(value: &Value) -> Result<bool> {
    let expected = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

fn payload_hash(value: &Value) -> Result<String> {
    let mut payload = value.clone();
    payload
        .as_object_mut()
        .context("sealed value must be object")?
        .remove("record_sha256");
    Ok(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&sort_json(&payload))?)
    ))
}

fn seal_in_place(value: &mut Value) -> Result<()> {
    let hash = payload_hash(value)?;
    value
        .as_object_mut()
        .context("payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(hash));
    Ok(())
}

fn sort_json(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .map(|(key, value)| (key.clone(), sort_json(value)))
                .collect::<BTreeMap<_, _>>()
                .into_iter()
                .collect::<Map<_, _>>(),
        ),
        Value::Array(values) => Value::Array(values.iter().map(sort_json).collect()),
        _ => value.clone(),
    }
}

fn write_atomic_json(path: &Path, value: &Value) -> Result<()> {
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

pub fn sha256_file(path: &Path) -> Result<String> {
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

fn git_tracks(root: &Path, path: &str) -> bool {
    Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "--error-unmatch", path])
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn native_source_hashes(root: &Path) -> Result<BTreeMap<String, String>> {
    let files = [
        ("workspace", "Cargo.toml"),
        ("outcomes_cargo", "crates/crossalpha-outcomes/Cargo.toml"),
        ("outcomes_kernel", "crates/crossalpha-outcomes/src/lib.rs"),
        (
            "outcomes_runtime",
            "crates/crossalpha-outcomes/src/runtime.rs",
        ),
        ("outcome_cli", "crates/crossalpha-cli/src/bin/outcome.rs"),
        ("config", "config/outcome_linkage_v01.yaml"),
    ];
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        let path = root.join(relative);
        if !path.exists() {
            bail!("Outcome binding source missing: {}", path.display());
        }
        result.insert(name.to_owned(), sha256_file(&path)?);
    }
    Ok(result)
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
