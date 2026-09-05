use crate::{ManifestLock, RawSnapshotManifest, rebuild_manifest_indexes};
use anyhow::{Context, Result};
use chrono::{DateTime, SecondsFormat, Utc};
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Serialize)]
pub struct ManifestParityReport {
    pub ok: bool,
    pub records: usize,
    pub daily_files: usize,
    pub series_files: usize,
    pub mismatches: Vec<String>,
}

pub fn verify_manifest_parity(data_root: &Path) -> Result<ManifestParityReport> {
    let temp = tempfile::tempdir().context("create manifest parity tempdir")?;
    let reference_root = temp.path().join("python");
    let rust_root = temp.path().join("rust");

    snapshot_audit_ledger(data_root, &reference_root, &rust_root)?;
    let python_records = rebuild_python_indexes(&reference_root)?;
    let rust_records = rebuild_manifest_indexes(&rust_root)?;

    let mut mismatches = Vec::new();
    if python_records != rust_records {
        mismatches.push(format!(
            "record count mismatch: python={python_records} rust={rust_records}"
        ));
    }

    let daily_files = compare_daily_manifests(&reference_root, &rust_root, &mut mismatches)?;
    let series_files = compare_series_states(&reference_root, &rust_root, &mut mismatches)?;

    Ok(ManifestParityReport {
        ok: mismatches.is_empty(),
        records: rust_records,
        daily_files,
        series_files,
        mismatches,
    })
}

fn snapshot_audit_ledger(data_root: &Path, python_root: &Path, rust_root: &Path) -> Result<()> {
    let _lock = ManifestLock::acquire(data_root)?;
    let audit = data_root.join("manifests/raw_snapshots.jsonl");
    if !audit.exists() {
        anyhow::bail!("audit manifest missing: {}", audit.display());
    }

    for destination_root in [python_root, rust_root] {
        let destination = destination_root.join("manifests");
        fs::create_dir_all(&destination)?;
        fs::copy(&audit, destination.join("raw_snapshots.jsonl"))?;
    }
    Ok(())
}

fn rebuild_python_indexes(data_root: &Path) -> Result<usize> {
    let python = env::var("CROSSALPHA_PYTHON").unwrap_or_else(|_| "python3".to_string());
    let repo_src = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../src");

    let mut python_paths = vec![repo_src];
    if let Some(existing) = env::var_os("PYTHONPATH") {
        python_paths.extend(env::split_paths(&existing));
    }
    let python_path = env::join_paths(python_paths).context("construct PYTHONPATH for parity")?;

    let code = concat!(
        "from pathlib import Path\n",
        "import sys\n",
        "from crossalpha.storage.indexes import rebuild_manifest_indexes\n",
        "result = rebuild_manifest_indexes(Path(sys.argv[1]))\n",
        "print(result['records'])\n"
    );

    let output = Command::new(&python)
        .arg("-c")
        .arg(code)
        .arg(data_root)
        .env("PYTHONPATH", python_path)
        .output()
        .with_context(|| format!("run Python manifest rebuild via {python}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        anyhow::bail!(
            "Python manifest rebuild failed with status {}: {}",
            output.status,
            stderr.trim()
        );
    }

    let stdout = String::from_utf8(output.stdout).context("decode Python rebuild output")?;
    stdout
        .lines()
        .rev()
        .find_map(|line| line.trim().parse::<usize>().ok())
        .with_context(|| format!("parse Python rebuild record count from output: {stdout:?}"))
}

fn compare_daily_manifests(
    reference_root: &Path,
    rust_root: &Path,
    mismatches: &mut Vec<String>,
) -> Result<usize> {
    let reference = reference_root.join("manifests/daily");
    let generated = rust_root.join("manifests/daily");
    let reference_files = collect_relative_files(&reference)?;
    let generated_files = collect_relative_files(&generated)?;

    compare_path_sets("daily", &reference_files, &generated_files, mismatches);

    for relative in reference_files.intersection(&generated_files) {
        let expected = read_manifest_records(&reference.join(relative))?;
        let actual = read_manifest_records(&generated.join(relative))?;
        if expected != actual {
            let first_difference = expected
                .iter()
                .zip(&actual)
                .position(|(left, right)| left != right);
            mismatches.push(format!(
                "daily content mismatch: {} python_records={} rust_records={} first_difference={}",
                relative.display(),
                expected.len(),
                actual.len(),
                first_difference
                    .map(|index| (index + 1).to_string())
                    .unwrap_or_else(|| "length-only".to_string())
            ));
        }
    }

    Ok(reference_files.len())
}

fn compare_series_states(
    reference_root: &Path,
    rust_root: &Path,
    mismatches: &mut Vec<String>,
) -> Result<usize> {
    let reference = reference_root.join("manifests/series");
    let generated = rust_root.join("manifests/series");
    let reference_files = collect_relative_files(&reference)?;
    let generated_files = collect_relative_files(&generated)?;

    compare_path_sets("series", &reference_files, &generated_files, mismatches);

    for relative in reference_files.intersection(&generated_files) {
        let expected = normalized_series_state(&reference.join(relative))?;
        let actual = normalized_series_state(&generated.join(relative))?;
        if expected != actual {
            mismatches.push(format!("series content mismatch: {}", relative.display()));
        }
    }

    Ok(reference_files.len())
}

fn compare_path_sets(
    label: &str,
    reference: &BTreeSet<PathBuf>,
    generated: &BTreeSet<PathBuf>,
    mismatches: &mut Vec<String>,
) {
    for missing in reference.difference(generated) {
        mismatches.push(format!(
            "{label} missing from Rust rebuild: {}",
            missing.display()
        ));
    }
    for extra in generated.difference(reference) {
        mismatches.push(format!(
            "{label} unexpected in Rust rebuild: {}",
            extra.display()
        ));
    }
}

fn collect_relative_files(root: &Path) -> Result<BTreeSet<PathBuf>> {
    let mut files = BTreeSet::new();
    if !root.exists() {
        return Ok(files);
    }
    collect_relative_files_inner(root, root, &mut files)?;
    Ok(files)
}

fn collect_relative_files_inner(
    root: &Path,
    current: &Path,
    files: &mut BTreeSet<PathBuf>,
) -> Result<()> {
    for entry in fs::read_dir(current)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            collect_relative_files_inner(root, &entry.path(), files)?;
        } else if file_type.is_file() {
            files.insert(entry.path().strip_prefix(root)?.to_path_buf());
        }
    }
    Ok(())
}

fn read_manifest_records(path: &Path) -> Result<Vec<RawSnapshotManifest>> {
    let raw = fs::read_to_string(path)
        .with_context(|| format!("read daily manifest {}", path.display()))?;
    raw.lines()
        .filter(|line| !line.trim().is_empty())
        .enumerate()
        .map(|(index, line)| {
            serde_json::from_str(line).with_context(|| {
                format!("parse daily manifest {} line {}", path.display(), index + 1)
            })
        })
        .collect()
}

fn normalized_series_state(path: &Path) -> Result<Value> {
    let mut value: Value = serde_json::from_slice(&fs::read(path)?)
        .with_context(|| format!("parse series state {}", path.display()))?;
    let Some(object) = value.as_object_mut() else {
        anyhow::bail!("series state is not an object: {}", path.display());
    };

    object.remove("updated_at");
    for key in [
        "first_observed_at",
        "latest_observed_at",
        "previous_observed_at",
    ] {
        if let Some(value) = object.get_mut(key) {
            normalize_datetime(value);
        }
    }
    if let Some(Value::Object(manifest)) = object.get_mut("latest_manifest")
        && let Some(value) = manifest.get_mut("observed_at")
    {
        normalize_datetime(value);
    }
    Ok(value)
}

fn normalize_datetime(value: &mut Value) {
    let Value::String(raw) = value else {
        return;
    };
    if let Ok(parsed) = DateTime::parse_from_rfc3339(raw) {
        *raw = parsed
            .with_timezone(&Utc)
            .to_rfc3339_opts(SecondsFormat::Micros, true);
    }
}
