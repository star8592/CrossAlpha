use crate::{ManifestLock, RawSnapshotManifest};
use anyhow::{Context, Result, bail};
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default)]
pub struct RecentManifestLoad {
    pub records: Vec<RawSnapshotManifest>,
    pub errors: Vec<String>,
    pub selected_paths: Vec<PathBuf>,
}

pub fn load_recent_daily_manifests(data_root: &Path, days: usize) -> Result<RecentManifestLoad> {
    if days < 1 {
        bail!("days must be >= 1");
    }

    let daily_root = data_root.join("manifests").join("daily");
    let mut paths = daily_manifest_paths(&daily_root)?;
    paths.sort();
    if paths.len() > days {
        paths = paths.split_off(paths.len() - days);
    }
    if paths.is_empty() {
        return Ok(RecentManifestLoad {
            records: Vec::new(),
            errors: vec![format!(
                "daily manifests missing under: {}",
                daily_root.display()
            )],
            selected_paths: Vec::new(),
        });
    }

    let _lock = ManifestLock::acquire(data_root)?;
    let mut records = Vec::new();
    let mut errors = Vec::new();
    for path in &paths {
        let file = match File::open(path) {
            Ok(file) => file,
            Err(error) => {
                errors.push(format!("{}: {error}", path.display()));
                continue;
            }
        };
        for (index, line) in BufReader::new(file).lines().enumerate() {
            let line = match line {
                Ok(line) => line,
                Err(error) => {
                    errors.push(format!("{}: line {}: {error}", path.display(), index + 1));
                    continue;
                }
            };
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<RawSnapshotManifest>(&line) {
                Ok(record) => records.push(record),
                Err(error) => {
                    errors.push(format!("{}: line {}: {error}", path.display(), index + 1))
                }
            }
        }
    }

    Ok(RecentManifestLoad {
        records,
        errors,
        selected_paths: paths,
    })
}

fn daily_manifest_paths(daily_root: &Path) -> Result<Vec<PathBuf>> {
    if !daily_root.exists() {
        return Ok(Vec::new());
    }

    let mut paths = Vec::new();
    for year in sorted_dirs(daily_root, "year=")? {
        for month in sorted_dirs(&year, "month=")? {
            for day in sorted_dirs(&month, "day=")? {
                let path = day.join("raw_snapshots.jsonl");
                if path.is_file() {
                    paths.push(path);
                }
            }
        }
    }
    Ok(paths)
}

fn sorted_dirs(root: &Path, prefix: &str) -> Result<Vec<PathBuf>> {
    let mut paths = Vec::new();
    for entry in fs::read_dir(root).with_context(|| format!("read directory {}", root.display()))? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        if !file_type.is_dir() {
            continue;
        }
        let name = entry.file_name();
        if name.to_string_lossy().starts_with(prefix) {
            paths.push(entry.path());
        }
    }
    paths.sort();
    Ok(paths)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Datelike, TimeZone, Utc};
    use std::fs;

    fn record(day: u32, source: &str) -> RawSnapshotManifest {
        RawSnapshotManifest {
            path: format!("/tmp/{day}.json.gz"),
            sha256: format!("sha{day}"),
            bytes: 10,
            compressed_bytes: Some(8),
            observed_at: Utc.with_ymd_and_hms(2026, 9, day, 12, 0, 0).unwrap(),
            source_id: source.to_owned(),
            observation_type: "snapshot".to_owned(),
        }
    }

    #[test]
    fn reads_only_latest_daily_partitions() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        for day in [3_u32, 4, 5] {
            let path = root
                .join("manifests/daily/year=2026/month=09")
                .join(format!("day={day:02}"))
                .join("raw_snapshots.jsonl");
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(
                &path,
                format!("{}\n", serde_json::to_string(&record(day, "x")).unwrap()),
            )
            .unwrap();
        }

        let loaded = load_recent_daily_manifests(root, 2).unwrap();
        assert!(loaded.errors.is_empty());
        assert_eq!(loaded.selected_paths.len(), 2);
        assert_eq!(loaded.records.len(), 2);
        assert_eq!(loaded.records[0].observed_at.day(), 4);
        assert_eq!(loaded.records[1].observed_at.day(), 5);
    }

    #[test]
    fn missing_daily_root_matches_python_error_semantics() {
        let temp = tempfile::tempdir().unwrap();
        let loaded = load_recent_daily_manifests(temp.path(), 2).unwrap();
        assert!(loaded.records.is_empty());
        assert_eq!(loaded.errors.len(), 1);
        assert!(loaded.errors[0].contains("daily manifests missing under"));
    }
}
