use crate::paper::{
    ALL_ASSETS, EXECUTION_LAG_DAYS, ONE_WAY_COST_BPS, PAPER_PROTOCOL, RISK_ASSETS, STRATEGY,
    build_daily_panel, compute_frozen_b3_target,
};
use anyhow::{Context, Result, bail};
use chrono::{Datelike, DateTime, Duration, NaiveDate, Utc};
use crossalpha_data::{FreeCoreRange, read_asset_returns};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

pub const HISTORICAL_START: &str = "2010-06-01";
pub const PAPER_BINDING_PROTOCOL: &str = "CROSSALPHA_FREE_V0_1_PAPER_RUST_RUNTIME_BINDING";

pub fn paper_root(data_root: &Path) -> PathBuf {
    data_root.join("research/free_v01/paper")
}

pub fn freeze_path(data_root: &Path) -> PathBuf {
    paper_root(data_root).join("freeze.json")
}

pub fn binding_path(data_root: &Path) -> PathBuf {
    paper_root(data_root).join("rust_runtime_binding.json")
}

pub fn returns_path(data_root: &Path, start: NaiveDate, end: NaiveDate) -> PathBuf {
    data_root
        .join("derived/core/free_v01")
        .join(format!("start={start}"))
        .join(format!("end={end}"))
        .join("asset_returns.parquet")
}

pub fn load_legacy_freeze(data_root: &Path) -> Result<Value> {
    let path = freeze_path(data_root);
    if !path.exists() {
        bail!("Frozen B3 paper freeze missing: {}", path.display());
    }
    let value: Value = serde_json::from_reader(File::open(&path)?)?;
    if !verify_seal(&value)? {
        bail!("Frozen B3 paper freeze failed seal verification");
    }
    if value.get("paper_protocol").and_then(Value::as_str) != Some(PAPER_PROTOCOL) {
        bail!("Frozen B3 paper protocol mismatch");
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
        "protocol":PAPER_BINDING_PROTOCOL,
        "paper_protocol":PAPER_PROTOCOL,
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
            bail!("existing Paper Rust runtime binding invalid or stale");
        }
        return Ok(serde_json::from_reader(File::open(path)?)?);
    }
    let value = runtime_binding_preview(data_root, bound_at)?;
    if value.get("production_binding_eligible").and_then(Value::as_bool) != Some(true) {
        bail!("Paper Rust runtime binding refused: Cargo.lock must exist and be tracked");
    }
    write_atomic_json(&path, &value)?;
    Ok(value)
}

pub fn verify_runtime_binding_file(path: &Path) -> Result<bool> {
    if !path.exists() { return Ok(false); }
    let current: Value = serde_json::from_reader(File::open(path)?)?;
    if !verify_seal(&current)? { return Ok(false); }
    let bound_at = parse_time(current.get("bound_at"))?;
    let data_root = path.parent().and_then(Path::parent).and_then(Path::parent).and_then(Path::parent)
        .context("paper binding path is not under <data_root>/research/free_v01/paper")?;
    Ok(runtime_binding_preview(data_root, bound_at)? == current)
}

pub fn create_snapshot(data_root: &Path, effective: NaiveDate, now: DateTime<Utc>) -> Result<Value> {
    require_binding(data_root)?;
    let freeze = load_legacy_freeze(data_root)?;
    if effective != now.date_naive() { bail!("prospective snapshots cannot be backfilled or future-dated"); }
    if effective.weekday().num_days_from_monday() != 0 { bail!("frozen B3 paper snapshots are allowed only on Monday UTC"); }
    let first_eligible = parse_date(freeze.get("first_eligible_effective_date"))?;
    if effective < first_eligible { bail!("effective date predates first prospective eligible date"); }
    let path = snapshot_path(data_root, effective);
    if path.exists() { return read_verified(&path); }

    let start = NaiveDate::parse_from_str(HISTORICAL_START, "%Y-%m-%d")?;
    let input_path = returns_path(data_root, start, effective);
    if !input_path.exists() { bail!("paper snapshot requires exact point-in-time Core range: {}", input_path.display()); }
    let rows = read_asset_returns(&input_path)?;
    let panel = build_daily_panel(&rows, start, effective)?;
    let signal = effective - Duration::days(EXECUTION_LAG_DAYS);
    if panel.dates.last().copied() != Some(signal) { bail!("paper snapshot input must end exactly on signal date {signal}"); }
    let weights = compute_frozen_b3_target(&panel, signal)?;
    let mut last_return_dates = Map::new();
    for asset in ALL_ASSETS {
        let last = rows.iter().filter(|row| row.economic_asset == asset && row.daily_return.is_some()).map(|row| row.date).max();
        last_return_dates.insert(asset.to_owned(), last.map(|value|Value::String(value.to_rfc3339())).unwrap_or(Value::Null));
    }
    let risk_gross = RISK_ASSETS.iter().map(|asset|weights.get(*asset).copied().unwrap_or(0.0)).sum::<f64>();
    let mut payload = json!({
        "schema_version":1,
        "paper_protocol":PAPER_PROTOCOL,
        "research_protocol":"CROSSALPHA_FREE_V0_1",
        "strategy":STRATEGY,
        "known_at":now.to_rfc3339(),
        "effective_date":effective.to_string(),
        "signal_date":signal.to_string(),
        "input_end_exclusive":effective.to_string(),
        "input_returns_path":input_path,
        "input_returns_sha256":sha256_file(&input_path)?,
        "asset_last_return_dates":Value::Object(last_return_dates),
        "freeze_record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        "frozen_implementation_sha256":freeze.get("frozen_implementation_sha256").cloned().unwrap_or(Value::Null),
        "weights":weights,
        "risk_gross":risk_gross,
        "cash_weight":weights.get("CASH").copied().unwrap_or(0.0),
        "execution_model":freeze.get("execution_model").cloned().unwrap_or(Value::Null),
    });
    seal_in_place(&mut payload)?;
    write_immutable(&path, &payload)?;
    Ok(payload)
}

pub fn mark_forward(data_root: &Path, end: NaiveDate, now: DateTime<Utc>) -> Result<Value> {
    require_binding(data_root)?;
    let freeze = load_legacy_freeze(data_root)?;
    if end > now.date_naive() { bail!("paper marks cannot use a future end date"); }
    let start = NaiveDate::parse_from_str(HISTORICAL_START, "%Y-%m-%d")?;
    let input_path = returns_path(data_root, start, end);
    if !input_path.exists() { bail!("paper mark requires Core returns ending {end}: {}",input_path.display()); }
    let rows = read_asset_returns(&input_path)?;
    let panel = build_daily_panel(&rows, start, end)?;
    let snapshots = load_records(&paper_root(data_root).join("snapshots"), "effective_date=")?;
    if snapshots.is_empty() { return Ok(json!({"paper_protocol":PAPER_PROTOCOL,"status":"no_snapshots","created_marks":0,"skipped_existing_marks":0,"end_exclusive":end})); }
    let mut snapshot_by_date = BTreeMap::<NaiveDate, Value>::new();
    for row in snapshots { snapshot_by_date.insert(parse_date(row.get("effective_date"))?, row); }
    let existing = load_records(&paper_root(data_root).join("marks"), "date=")?;
    let existing_dates: BTreeSet<NaiveDate> = existing.iter().filter_map(|row|parse_date(row.get("date")).ok()).collect();
    let mut previous_equity = existing.last().and_then(|row|row.get("equity_after")).and_then(Value::as_f64).unwrap_or(1.0);
    let mut previous_cash_equity = existing.last().and_then(|row|row.get("cash_equity_after")).and_then(Value::as_f64).unwrap_or(1.0);
    let first_effective = *snapshot_by_date.keys().next().context("snapshot date missing")?;
    let mut current_weights = ALL_ASSETS.iter().map(|asset|((*asset).to_owned(),if *asset=="CASH"{1.0}else{0.0})).collect::<BTreeMap<_,_>>();
    let mut active_snapshot: Option<Value> = None;
    if let Some(last_existing)=existing_dates.iter().max().copied(){for (day,snapshot) in &snapshot_by_date{if *day<=last_existing{active_snapshot=Some(snapshot.clone());current_weights=weights_from(snapshot)?;}}}
    let by_date = panel.dates.iter().enumerate().map(|(i,d)|(*d,i)).collect::<BTreeMap<_,_>>();
    let mut created=0usize; let mut skipped=0usize;
    for day in panel.dates.iter().copied(){
        if day<first_effective || day>=end{continue;} if existing_dates.contains(&day){skipped+=1;continue;}
        let mut turnover=0.0;
        if let Some(snapshot)=snapshot_by_date.get(&day){let new_weights=weights_from(snapshot)?;turnover=ALL_ASSETS.iter().map(|asset|(new_weights.get(*asset).copied().unwrap_or(0.0)-current_weights.get(*asset).copied().unwrap_or(0.0)).abs()).sum::<f64>()*0.5;current_weights=new_weights;active_snapshot=Some(snapshot.clone());}
        else if active_snapshot.is_none(){if let Some((_,snapshot))=snapshot_by_date.range(..=day).next_back(){active_snapshot=Some(snapshot.clone());current_weights=weights_from(snapshot)?;}else{continue;}}
        let index=by_date[&day]; let gross=ALL_ASSETS.iter().map(|asset|current_weights.get(*asset).copied().unwrap_or(0.0)*panel.returns.get(*asset).and_then(|v|v.get(index)).copied().unwrap_or(0.0)).sum::<f64>();
        let cost=turnover*(ONE_WAY_COST_BPS/10_000.0); let net=gross-cost; let cash=panel.returns["CASH"][index]; previous_equity*=1.0+net; previous_cash_equity*=1.0+cash;
        let active=active_snapshot.as_ref().context("active snapshot missing")?;
        let mut payload=json!({"schema_version":1,"paper_protocol":PAPER_PROTOCOL,"strategy":STRATEGY,"known_at":now.to_rfc3339(),"date":day.to_string(),"active_snapshot_effective_date":active.get("effective_date").cloned().unwrap_or(Value::Null),"active_snapshot_record_sha256":active.get("record_sha256").cloned().unwrap_or(Value::Null),"weights":current_weights,"asset_returns":ALL_ASSETS.iter().map(|asset|((*asset).to_owned(),panel.returns.get(*asset).and_then(|v|v.get(index)).copied().unwrap_or(0.0))).collect::<BTreeMap<_,_>>(),"turnover":turnover,"cost":cost,"gross_return":gross,"net_return":net,"cash_return":cash,"equity_after":previous_equity,"cash_equity_after":previous_cash_equity,"input_end_exclusive":end.to_string(),"input_returns_sha256":sha256_file(&input_path)?,"freeze_record_sha256":freeze.get("record_sha256").cloned().unwrap_or(Value::Null)});
        seal_in_place(&mut payload)?; write_immutable(&mark_path(data_root,day),&payload)?; created+=1;
    }
    Ok(json!({"paper_protocol":PAPER_PROTOCOL,"status":"marked","created_marks":created,"skipped_existing_marks":skipped,"end_exclusive":end,"latest_equity":previous_equity,"latest_cash_equity":previous_cash_equity}))
}

pub fn integrity(data_root:&Path)->Result<Value>{let freeze=load_legacy_freeze(data_root)?;let binding=verify_runtime_binding_file(&binding_path(data_root))?;let snapshots=load_records(&paper_root(data_root).join("snapshots"),"effective_date=")?;let marks=load_records(&paper_root(data_root).join("marks"),"date=")?;let records_ok=snapshots.iter().chain(marks.iter()).all(|row|verify_seal(row).unwrap_or(false));let freeze_links=snapshots.iter().chain(marks.iter()).all(|row|row.get("freeze_record_sha256")==freeze.get("record_sha256"));Ok(json!({"protocol":PAPER_PROTOCOL,"ok":binding&&records_ok&&freeze_links,"runtime_binding_ok":binding,"freeze_seal":true,"record_seals":records_ok,"freeze_links":freeze_links,"snapshot_count":snapshots.len(),"mark_count":marks.len(),"python_runtime_required":false}))}

fn snapshot_path(root:&Path,date:NaiveDate)->PathBuf{paper_root(root).join("snapshots").join(format!("year={:04}",date.year())).join(format!("month={:02}",date.month())).join(format!("effective_date={date}.json"))}
fn mark_path(root:&Path,date:NaiveDate)->PathBuf{paper_root(root).join("marks").join(format!("year={:04}",date.year())).join(format!("month={:02}",date.month())).join(format!("date={date}.json"))}
fn weights_from(value:&Value)->Result<BTreeMap<String,f64>>{let object=value.get("weights").and_then(Value::as_object).context("snapshot weights missing")?;Ok(ALL_ASSETS.iter().map(|asset|((*asset).to_owned(),object.get(*asset).and_then(Value::as_f64).unwrap_or(0.0))).collect())}
fn parse_date(value:Option<&Value>)->Result<NaiveDate>{NaiveDate::parse_from_str(value.and_then(Value::as_str).context("date missing")?,"%Y-%m-%d").map_err(Into::into)}
fn parse_time(value:Option<&Value>)->Result<DateTime<Utc>>{Ok(DateTime::parse_from_rfc3339(value.and_then(Value::as_str).context("time missing")?)?.with_timezone(&Utc))}
fn read_verified(path:&Path)->Result<Value>{let value:Value=serde_json::from_reader(File::open(path)?)?;if !verify_seal(&value)?{bail!("immutable paper record failed seal verification: {}",path.display());}Ok(value)}
fn load_records(root:&Path,name_prefix:&str)->Result<Vec<Value>>{if !root.exists(){return Ok(Vec::new());}let mut paths=Vec::new();collect_json(root,name_prefix,&mut paths)?;paths.sort();paths.into_iter().map(|path|read_verified(&path)).collect()}
fn collect_json(root:&Path,prefix:&str,out:&mut Vec<PathBuf>)->Result<()>{for entry in fs::read_dir(root)?{let path=entry?.path();if path.is_dir(){collect_json(&path,prefix,out)?;}else if path.extension().and_then(|v|v.to_str())==Some("json")&&path.file_name().and_then(|v|v.to_str()).is_some_and(|name|name.starts_with(prefix)){out.push(path);}}Ok(())}
fn require_binding(root:&Path)->Result<()> { if !verify_runtime_binding_file(&binding_path(root))?{bail!("Paper native runtime binding missing, invalid, or stale");}Ok(()) }

pub fn verify_seal(value:&Value)->Result<bool>{let expected=value.get("record_sha256").and_then(Value::as_str).context("record_sha256 missing")?;Ok(expected==payload_hash(value)?)}
pub fn payload_hash(value:&Value)->Result<String>{let mut payload=value.clone();payload.as_object_mut().context("sealed value must be object")?.remove("record_sha256");Ok(format!("{:x}",Sha256::digest(serde_json::to_vec(&sort_json(&payload))?)))}
fn seal_in_place(value:&mut Value)->Result<()> { let hash=payload_hash(value)?;value.as_object_mut().context("payload must be object")?.insert("record_sha256".to_owned(),Value::String(hash));Ok(()) }
fn sort_json(value:&Value)->Value{match value{Value::Object(object)=>Value::Object(object.iter().map(|(k,v)|(k.clone(),sort_json(v))).collect::<BTreeMap<_,_>>().into_iter().collect::<Map<_,_>>()),Value::Array(values)=>Value::Array(values.iter().map(sort_json).collect()),_=>value.clone()}}
pub fn sha256_file(path:&Path)->Result<String>{let mut file=File::open(path)?;let mut digest=Sha256::new();let mut buffer=[0u8;1024*1024];loop{let n=file.read(&mut buffer)?;if n==0{break;}digest.update(&buffer[..n]);}Ok(format!("{:x}",digest.finalize()))}
fn write_immutable(path:&Path,value:&Value)->Result<()> { if path.exists(){let _=read_verified(path)?;return Ok(());}write_atomic_json(path,value) }
fn write_atomic_json(path:&Path,value:&Value)->Result<()> {if let Some(parent)=path.parent(){fs::create_dir_all(parent)?;}let tmp=path.with_extension("json.tmp");{let mut file=File::create(&tmp)?;serde_json::to_writer_pretty(&mut file,value)?;file.write_all(b"\n")?;file.sync_all()?;}fs::rename(&tmp,path)?;if let Some(parent)=path.parent(){File::open(parent)?.sync_all()?;}Ok(())}
fn git_tracks(root:&Path,path:&str)->bool{std::process::Command::new("git").arg("-C").arg(root).args(["ls-files","--error-unmatch",path]).output().map(|o|o.status.success()).unwrap_or(false)}
fn native_source_hashes(root:&Path)->Result<BTreeMap<String,String>>{let files=[("workspace","Cargo.toml"),("data","crates/crossalpha-data/src/free_core.rs"),("returns","crates/crossalpha-data/src/free_returns.rs"),("research_cargo","crates/crossalpha-research/Cargo.toml"),("baseline","crates/crossalpha-research/src/baseline.rs"),("paper_kernel","crates/crossalpha-research/src/paper.rs"),("paper_runtime","crates/crossalpha-research/src/paper_runtime.rs"),("paper_cli","crates/crossalpha-cli/src/bin/paper.rs")];let mut result=BTreeMap::new();for(name,relative)in files{let path=root.join(relative);if !path.exists(){bail!("paper binding source missing: {}",path.display());}result.insert(name.to_owned(),sha256_file(&path)?);}Ok(result)}
fn repo_root()->PathBuf{PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")}
