use crate::shadow_v01::{MODE as SHADOW_MODE, PROTOCOL as SHADOW_PROTOCOL, build_latest_shadow_state};
use anyhow::{Context, Result, bail};
use chrono::{Datelike, DateTime, Duration, NaiveDate, Utc};
use crossalpha_research::paper::{ALL_ASSETS, ONE_WAY_COST_BPS, RISK_ASSETS, apply_shadow_multiplier};
use crossalpha_research::paper_runtime::{
    freeze_path as paper_freeze_path, payload_hash, sha256_file, verify_seal as verify_core_seal,
};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const AB_PROTOCOL: &str = "CROSSALPHA_STATE_AB_V0_1";
pub const MODE: &str = "PROSPECTIVE_SHADOW_AB";
pub const BINDING_PROTOCOL: &str = "CROSSALPHA_STATE_AB_V0_1_RUST_RUNTIME_BINDING";
pub const ALLOWED_MULTIPLIERS: [f64; 3] = [1.0, 0.75, 0.50];

pub fn ab_root(data_root: &Path) -> PathBuf {
    data_root.join("research/free_v01/state_ab_v01")
}

pub fn freeze_path(data_root: &Path) -> PathBuf {
    ab_root(data_root).join("freeze.json")
}

pub fn binding_path(data_root: &Path) -> PathBuf {
    ab_root(data_root).join("rust_runtime_binding.json")
}

pub fn load_legacy_freeze(data_root: &Path) -> Result<Value> {
    let path = freeze_path(data_root);
    if !path.exists() {
        bail!("State A/B legacy freeze missing: {}", path.display());
    }
    let value: Value = serde_json::from_reader(File::open(&path)?)?;
    if !verify_seal(&value)? {
        bail!("State A/B legacy freeze failed seal verification");
    }
    if value.get("protocol").and_then(Value::as_str) != Some(AB_PROTOCOL) {
        bail!("State A/B legacy freeze protocol mismatch");
    }
    let core: Value = serde_json::from_reader(File::open(paper_freeze_path(data_root))?)?;
    if !verify_core_seal(&core)? {
        bail!("Frozen B3 paper freeze failed seal verification");
    }
    if value.get("core_freeze_record_sha256") != core.get("record_sha256") {
        bail!("State A/B freeze no longer matches Frozen B3 paper freeze");
    }
    Ok(value)
}

pub fn runtime_binding_preview(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let freeze = load_legacy_freeze(data_root)?;
    let root = repo_root();
    let cargo_lock = root.join("Cargo.lock");
    let lock_present = cargo_lock.is_file();
    let lock_tracked = lock_present && git_tracks(&root, "Cargo.lock");
    let mut payload = json!({
        "schema_version":1,
        "protocol":BINDING_PROTOCOL,
        "ab_protocol":AB_PROTOCOL,
        "legacy_freeze":{
            "path":freeze_path(data_root),
            "file_sha256":sha256_file(&freeze_path(data_root))?,
            "record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        },
        "bound_at":bound_at.to_rfc3339(),
        "runtime":"RUST_TOKIO",
        "python_runtime_required":false,
        "cargo_lock_present":lock_present,
        "cargo_lock_tracked":lock_tracked,
        "cargo_lock_sha256":lock_present.then(||sha256_file(&cargo_lock)).transpose()?,
        "native_source_sha256":native_source_hashes(&root)?,
        "production_binding_eligible":lock_present && lock_tracked,
    });
    seal_in_place(&mut payload)?;
    Ok(payload)
}

pub fn write_runtime_binding(data_root: &Path, bound_at: DateTime<Utc>) -> Result<Value> {
    let path = binding_path(data_root);
    if path.exists() {
        if !verify_runtime_binding_file(&path)? {
            bail!("existing State A/B Rust runtime binding invalid or stale");
        }
        return Ok(serde_json::from_reader(File::open(path)?)?);
    }
    let value = runtime_binding_preview(data_root, bound_at)?;
    if value.get("production_binding_eligible").and_then(Value::as_bool) != Some(true) {
        bail!("State A/B Rust runtime binding refused: Cargo.lock must exist and be tracked");
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
        .and_then(Path::parent)
        .context("State A/B binding path is not under <data_root>/research/free_v01/state_ab_v01")?;
    Ok(runtime_binding_preview(data_root, bound_at)? == current)
}

pub fn create_snapshot(data_root: &Path, effective: NaiveDate, now: DateTime<Utc>) -> Result<Value> {
    require_binding(data_root)?;
    let freeze = load_legacy_freeze(data_root)?;
    if effective != now.date_naive() {
        bail!("State A/B snapshots cannot be backfilled or future-dated");
    }
    if effective.weekday().num_days_from_monday() != 0 {
        bail!("State A/B snapshots are allowed only on Monday UTC");
    }
    let first_eligible = parse_date(freeze.get("first_eligible_effective_date"))?;
    if effective < first_eligible {
        bail!("State A/B snapshot predates the prospective experiment");
    }
    let path = snapshot_path(data_root, effective);
    if path.exists() {
        return read_verified(&path);
    }
    let a_path = core_snapshot_path(data_root, effective);
    if !a_path.exists() {
        bail!("State A/B snapshot requires same-date Frozen B3 snapshot: {}", a_path.display());
    }
    let a_snapshot: Value = serde_json::from_reader(File::open(&a_path)?)?;
    if !verify_core_seal(&a_snapshot)? {
        bail!("Frozen B3 snapshot failed seal verification");
    }

    let decision_path = decision_path(data_root, effective);
    let decision = if decision_path.exists() {
        read_verified(&decision_path)?
    } else {
        let mut state = build_latest_shadow_state(data_root, now)?;
        if state.get("status").and_then(Value::as_str) == Some("no_inputs") {
            state = json!({
                "protocol":SHADOW_PROTOCOL,
                "mode":SHADOW_MODE,
                "shadow_only":true,
                "core_protocol_mutated":false,
                "as_of":Value::Null,
                "generated_at":now.to_rfc3339(),
                "state_band":"NO_MODIFIER_DATA_INSUFFICIENT",
                "shadow_risk_multiplier":1.0,
                "data_confidence":"NONE",
                "state_pressure":Value::Null,
                "leverage_pressure":Value::Null,
                "stablecoin_pressure":Value::Null,
                "valid_source_components":0,
                "expected_source_components":3,
                "status":"no_inputs",
            });
        }
        let multiplier = state
            .get("shadow_risk_multiplier")
            .and_then(Value::as_f64)
            .unwrap_or(1.0);
        if !ALLOWED_MULTIPLIERS.contains(&multiplier) {
            bail!("State multiplier outside frozen set: {multiplier}");
        }
        if state.get("shadow_only").and_then(Value::as_bool) != Some(true)
            || state.get("core_protocol_mutated").and_then(Value::as_bool) != Some(false)
        {
            bail!("State decision violates Frozen Core isolation");
        }
        let mut value = json!({
            "schema_version":1,
            "protocol":AB_PROTOCOL,
            "state_protocol":SHADOW_PROTOCOL,
            "effective_date":effective.to_string(),
            "known_at":now.to_rfc3339(),
            "ab_freeze_record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
            "a_snapshot_record_sha256":a_snapshot.get("record_sha256").cloned().unwrap_or(Value::Null),
            "state_band":state.get("state_band").cloned().unwrap_or(Value::Null),
            "shadow_risk_multiplier":multiplier,
            "data_confidence":state.get("data_confidence").cloned().unwrap_or(Value::Null),
            "state_as_of":state.get("as_of").cloned().unwrap_or(Value::Null),
            "state_generated_at":state.get("generated_at").cloned().unwrap_or(Value::Null),
            "state_payload":state,
            "retrospective_backfill_allowed":false,
        });
        seal_in_place(&mut value)?;
        write_immutable(&decision_path, &value)?;
        value
    };

    let multiplier = decision
        .get("shadow_risk_multiplier")
        .and_then(Value::as_f64)
        .context("State A/B decision multiplier missing")?;
    let a_weights = weights_from(&a_snapshot, "weights")?;
    let b_weights = apply_shadow_multiplier(&a_weights, multiplier)?;
    let a_risk_gross = RISK_ASSETS
        .iter()
        .map(|asset| a_weights.get(*asset).copied().unwrap_or(0.0))
        .sum::<f64>();
    let b_risk_gross = RISK_ASSETS
        .iter()
        .map(|asset| b_weights.get(*asset).copied().unwrap_or(0.0))
        .sum::<f64>();
    let mut payload = json!({
        "schema_version":1,
        "protocol":AB_PROTOCOL,
        "mode":MODE,
        "effective_date":effective.to_string(),
        "known_at":now.to_rfc3339(),
        "ab_freeze_record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        "a_snapshot_effective_date":a_snapshot.get("effective_date").cloned().unwrap_or(Value::Null),
        "a_snapshot_record_sha256":a_snapshot.get("record_sha256").cloned().unwrap_or(Value::Null),
        "state_decision_record_sha256":decision.get("record_sha256").cloned().unwrap_or(Value::Null),
        "state_band":decision.get("state_band").cloned().unwrap_or(Value::Null),
        "shadow_risk_multiplier":multiplier,
        "data_confidence":decision.get("data_confidence").cloned().unwrap_or(Value::Null),
        "a_weights":a_weights,
        "b_weights":b_weights,
        "a_risk_gross":a_risk_gross,
        "b_risk_gross":b_risk_gross,
        "b_cash_weight":b_weights.get("CASH").copied().unwrap_or(0.0),
        "relative_core_weights_changed":false,
        "risk_increased_above_A":false,
    });
    seal_in_place(&mut payload)?;
    write_immutable(&path, &payload)?;
    Ok(payload)
}

pub fn mark(data_root: &Path, end: NaiveDate, now: DateTime<Utc>) -> Result<Value> {
    require_binding(data_root)?;
    let freeze = load_legacy_freeze(data_root)?;
    let target = end - Duration::days(1);
    let snapshots = load_records(&ab_root(data_root).join("snapshots"), "effective_date=")?;
    if snapshots.is_empty() {
        return Ok(json!({
            "protocol":AB_PROTOCOL,
            "status":"no_snapshots",
            "created_marks":0,
            "end_exclusive":end,
        }));
    }
    let first_effective = parse_date(snapshots[0].get("effective_date"))?;
    if target < first_effective {
        return Ok(json!({
            "protocol":AB_PROTOCOL,
            "status":"before_first_snapshot",
            "created_marks":0,
            "target_mark_date":target,
            "end_exclusive":end,
        }));
    }
    let path = mark_path(data_root, target);
    if path.exists() {
        let mut existing = read_verified(&path)?;
        existing
            .as_object_mut()
            .expect("mark is object")
            .insert("status".to_owned(), Value::String("already_marked".to_owned()));
        existing
            .as_object_mut()
            .expect("mark is object")
            .insert("created_marks".to_owned(), json!(0));
        return Ok(existing);
    }
    let marks = load_records(&ab_root(data_root).join("marks"), "date=")?;
    if let Some(previous) = marks.last() {
        let previous_date = parse_date(previous.get("date"))?;
        let expected = previous_date + Duration::days(1);
        if target != expected {
            bail!(
                "STATE_AB_LEDGER_GAP: refusing retrospective backfill; last mark={previous_date}, next required={expected}, requested={target}"
            );
        }
    } else if target != first_effective {
        bail!(
            "STATE_AB_LEDGER_GAP: first A/B mark was missed; first snapshot={first_effective}, requested={target}"
        );
    }

    let a_path = core_mark_path(data_root, target);
    if !a_path.exists() {
        bail!("State A/B mark requires same-date Frozen B3 mark: {}", a_path.display());
    }
    let a_mark: Value = serde_json::from_reader(File::open(&a_path)?)?;
    if !verify_core_seal(&a_mark)? {
        bail!("Frozen B3 mark failed seal verification");
    }
    let active = snapshots
        .iter()
        .filter(|row| parse_date(row.get("effective_date")).is_ok_and(|day| day <= target))
        .last()
        .context("no eligible State A/B snapshot for mark")?;
    if active.get("a_snapshot_effective_date") != a_mark.get("active_snapshot_effective_date") {
        bail!("STATE_AB_SNAPSHOT_GAP: Frozen B3 changed snapshot without matching A/B decision");
    }
    if active.get("a_snapshot_record_sha256") != a_mark.get("active_snapshot_record_sha256") {
        bail!("State A/B snapshot link does not match Frozen B3 mark snapshot hash");
    }
    let weights = weights_from(active, "b_weights")?;
    let (previous_weights, previous_equity) = if let Some(previous) = marks.last() {
        (
            weights_from(previous, "weights")?,
            previous.get("equity_after").and_then(Value::as_f64).unwrap_or(1.0),
        )
    } else {
        let mut cash = ALL_ASSETS
            .iter()
            .map(|asset| ((*asset).to_owned(), 0.0))
            .collect::<BTreeMap<_, _>>();
        cash.insert("CASH".to_owned(), 1.0);
        (cash, 1.0)
    };
    let turnover = ALL_ASSETS
        .iter()
        .map(|asset| {
            (weights.get(*asset).copied().unwrap_or(0.0)
                - previous_weights.get(*asset).copied().unwrap_or(0.0))
            .abs()
        })
        .sum::<f64>()
        * 0.5;
    let asset_returns = weights_from(&a_mark, "asset_returns")?;
    let gross_return = ALL_ASSETS
        .iter()
        .map(|asset| {
            weights.get(*asset).copied().unwrap_or(0.0)
                * asset_returns.get(*asset).copied().unwrap_or(0.0)
        })
        .sum::<f64>();
    let cost = turnover * (ONE_WAY_COST_BPS / 10_000.0);
    let net_return = gross_return - cost;
    let cash_return = asset_returns.get("CASH").copied().unwrap_or(0.0);
    let equity_after = previous_equity * (1.0 + net_return);
    let a_net_return = a_mark.get("net_return").and_then(Value::as_f64).unwrap_or(0.0);
    let mut payload = json!({
        "schema_version":1,
        "protocol":AB_PROTOCOL,
        "mode":MODE,
        "known_at":now.to_rfc3339(),
        "date":target.to_string(),
        "ab_freeze_record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        "a_mark_record_sha256":a_mark.get("record_sha256").cloned().unwrap_or(Value::Null),
        "a_snapshot_effective_date":a_mark.get("active_snapshot_effective_date").cloned().unwrap_or(Value::Null),
        "a_snapshot_record_sha256":a_mark.get("active_snapshot_record_sha256").cloned().unwrap_or(Value::Null),
        "b_snapshot_effective_date":active.get("effective_date").cloned().unwrap_or(Value::Null),
        "b_snapshot_record_sha256":active.get("record_sha256").cloned().unwrap_or(Value::Null),
        "state_decision_record_sha256":active.get("state_decision_record_sha256").cloned().unwrap_or(Value::Null),
        "state_band":active.get("state_band").cloned().unwrap_or(Value::Null),
        "shadow_risk_multiplier":active.get("shadow_risk_multiplier").cloned().unwrap_or(json!(1.0)),
        "data_confidence":active.get("data_confidence").cloned().unwrap_or(Value::Null),
        "weights":weights,
        "asset_returns":asset_returns,
        "turnover":turnover,
        "cost":cost,
        "gross_return":gross_return,
        "net_return":net_return,
        "cash_return":cash_return,
        "equity_after":equity_after,
        "a_net_return":a_net_return,
        "a_equity_after":a_mark.get("equity_after").cloned().unwrap_or(Value::Null),
        "return_delta_B_minus_A":net_return-a_net_return,
        "retrospective_backfill_allowed":false,
    });
    seal_in_place(&mut payload)?;
    write_immutable(&path, &payload)?;
    let mut result = payload;
    result
        .as_object_mut()
        .expect("mark is object")
        .insert("status".to_owned(), Value::String("marked".to_owned()));
    result
        .as_object_mut()
        .expect("mark is object")
        .insert("created_marks".to_owned(), json!(1));
    Ok(result)
}

pub fn integrity(data_root: &Path) -> Result<Value> {
    let freeze = load_legacy_freeze(data_root)?;
    let binding_ok = verify_runtime_binding_file(&binding_path(data_root))?;
    let decisions = load_records(&ab_root(data_root).join("decisions"), "effective_date=")?;
    let snapshots = load_records(&ab_root(data_root).join("snapshots"), "effective_date=")?;
    let marks = load_records(&ab_root(data_root).join("marks"), "date=")?;
    let mut checks = Map::new();
    checks.insert("runtime_binding".to_owned(), Value::Bool(binding_ok));
    checks.insert(
        "record_seals".to_owned(),
        Value::Bool(
            decisions
                .iter()
                .chain(snapshots.iter())
                .chain(marks.iter())
                .all(|row| verify_seal(row).unwrap_or(false)),
        ),
    );
    let snapshot_dates: Vec<_> = snapshots
        .iter()
        .filter_map(|row| parse_date(row.get("effective_date")).ok())
        .collect();
    let mark_dates: Vec<_> = marks
        .iter()
        .filter_map(|row| parse_date(row.get("date")).ok())
        .collect();
    checks.insert(
        "snapshot_dates_unique".to_owned(),
        Value::Bool(snapshot_dates.len() == snapshot_dates.iter().collect::<BTreeSet<_>>().len()),
    );
    checks.insert(
        "snapshots_are_mondays".to_owned(),
        Value::Bool(snapshot_dates.iter().all(|day| day.weekday().num_days_from_monday() == 0)),
    );
    checks.insert(
        "mark_dates_contiguous".to_owned(),
        Value::Bool(mark_dates.windows(2).all(|pair| pair[1] - pair[0] == Duration::days(1))),
    );
    checks.insert(
        "freeze_links".to_owned(),
        Value::Bool(
            decisions
                .iter()
                .chain(snapshots.iter())
                .chain(marks.iter())
                .all(|row| row.get("ab_freeze_record_sha256") == freeze.get("record_sha256")),
        ),
    );
    checks.insert(
        "multipliers_frozen".to_owned(),
        Value::Bool(
            snapshots.iter().chain(marks.iter()).all(|row| {
                row.get("shadow_risk_multiplier")
                    .and_then(Value::as_f64)
                    .is_some_and(|value| ALLOWED_MULTIPLIERS.contains(&value))
            }),
        ),
    );
    checks.insert(
        "B_never_increases_risk".to_owned(),
        Value::Bool(snapshots.iter().all(|row| {
            row.get("b_risk_gross").and_then(Value::as_f64).unwrap_or(2.0)
                <= row.get("a_risk_gross").and_then(Value::as_f64).unwrap_or(1.0) + 1e-12
        })),
    );
    let ok = checks.values().all(|value| value.as_bool() == Some(true));
    Ok(json!({
        "protocol":AB_PROTOCOL,
        "frozen":true,
        "ok":ok,
        "first_eligible_effective_date":freeze.get("first_eligible_effective_date").cloned().unwrap_or(Value::Null),
        "decision_count":decisions.len(),
        "snapshot_count":snapshots.len(),
        "mark_count":marks.len(),
        "checks":Value::Object(checks),
        "policy":"NO_RETROSPECTIVE_AB_BACKFILL",
        "python_runtime_required":false,
    }))
}

pub fn verify_seal(value: &Value) -> Result<bool> {
    let expected = value
        .get("record_sha256")
        .and_then(Value::as_str)
        .context("record_sha256 missing")?;
    Ok(expected == payload_hash(value)?)
}

fn seal_in_place(value: &mut Value) -> Result<()> {
    let hash = payload_hash(value)?;
    value
        .as_object_mut()
        .context("payload must be object")?
        .insert("record_sha256".to_owned(), Value::String(hash));
    Ok(())
}

fn read_verified(path: &Path) -> Result<Value> {
    let value: Value = serde_json::from_reader(File::open(path)?)?;
    if !verify_seal(&value)? {
        bail!("State A/B immutable record failed seal verification: {}", path.display());
    }
    Ok(value)
}

fn write_immutable(path: &Path, value: &Value) -> Result<()> {
    if path.exists() {
        let _ = read_verified(path)?;
        return Ok(());
    }
    write_atomic_json(path, value)
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

fn weights_from(value: &Value, field: &str) -> Result<BTreeMap<String, f64>> {
    let object = value
        .get(field)
        .and_then(Value::as_object)
        .with_context(|| format!("{field} missing"))?;
    Ok(ALL_ASSETS
        .iter()
        .map(|asset| {
            (
                (*asset).to_owned(),
                object.get(*asset).and_then(Value::as_f64).unwrap_or(0.0),
            )
        })
        .collect())
}

fn parse_date(value: Option<&Value>) -> Result<NaiveDate> {
    Ok(NaiveDate::parse_from_str(
        value.and_then(Value::as_str).context("date missing")?,
        "%Y-%m-%d",
    )?)
}

fn parse_time(value: Option<&Value>) -> Result<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339(
        value.and_then(Value::as_str).context("time missing")?,
    )?
    .with_timezone(&Utc))
}

fn load_records(root: &Path, name_prefix: &str) -> Result<Vec<Value>> {
    if !root.exists() {
        return Ok(Vec::new());
    }
    let mut paths = Vec::new();
    collect_json(root, name_prefix, &mut paths)?;
    paths.sort();
    paths.into_iter().map(|path| read_verified(&path)).collect()
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

fn require_binding(data_root: &Path) -> Result<()> {
    if !verify_runtime_binding_file(&binding_path(data_root))? {
        bail!("State A/B native runtime binding missing, invalid, or stale");
    }
    Ok(())
}

fn decision_path(data_root: &Path, date: NaiveDate) -> PathBuf {
    ab_root(data_root)
        .join("decisions")
        .join(format!("year={:04}", date.year()))
        .join(format!("month={:02}", date.month()))
        .join(format!("effective_date={date}.json"))
}

fn snapshot_path(data_root: &Path, date: NaiveDate) -> PathBuf {
    ab_root(data_root)
        .join("snapshots")
        .join(format!("year={:04}", date.year()))
        .join(format!("month={:02}", date.month()))
        .join(format!("effective_date={date}.json"))
}

fn mark_path(data_root: &Path, date: NaiveDate) -> PathBuf {
    ab_root(data_root)
        .join("marks")
        .join(format!("year={:04}", date.year()))
        .join(format!("month={:02}", date.month()))
        .join(format!("date={date}.json"))
}

fn core_snapshot_path(data_root: &Path, date: NaiveDate) -> PathBuf {
    data_root
        .join("research/free_v01/paper/snapshots")
        .join(format!("year={:04}", date.year()))
        .join(format!("month={:02}", date.month()))
        .join(format!("effective_date={date}.json"))
}

fn core_mark_path(data_root: &Path, date: NaiveDate) -> PathBuf {
    data_root
        .join("research/free_v01/paper/marks")
        .join(format!("year={:04}", date.year()))
        .join(format!("month={:02}", date.month()))
        .join(format!("date={date}.json"))
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
        ("state_cargo", "crates/crossalpha-state/Cargo.toml"),
        ("shadow_v01", "crates/crossalpha-state/src/shadow_v01.rs"),
        ("ab_runtime", "crates/crossalpha-state/src/ab_runtime.rs"),
        ("paper_kernel", "crates/crossalpha-research/src/paper.rs"),
        ("paper_runtime", "crates/crossalpha-research/src/paper_runtime.rs"),
        ("ab_cli", "crates/crossalpha-cli/src/bin/ab.rs"),
        ("state_config", "config/state_shadow_v01.yaml"),
        ("ab_config", "config/state_ab_v01.yaml"),
    ];
    let mut result = BTreeMap::new();
    for (name, relative) in files {
        let path = root.join(relative);
        if !path.exists() {
            bail!("State A/B binding source missing: {}", path.display());
        }
        result.insert(name.to_owned(), sha256_file(&path)?);
    }
    Ok(result)
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}
