from __future__ import annotations

from pathlib import Path

# Exact-head trigger marker for the focused self-hosted repair workflow.


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text()
    old_count = text.count(old)
    new_count = text.count(new)
    if old_count == 1:
        file.write_text(text.replace(old, new, 1))
        print(f"patched: {label}")
        return
    if old_count == 0 and new_count == 1:
        print(f"already patched: {label}")
        return
    raise SystemExit(
        f"{label}: expected exactly one old or one already-patched match in {path}; "
        f"old={old_count} new={new_count}"
    )


def main() -> None:
    path = "crates/crossalpha-state/src/v04.rs"

    replace_once(
        path,
        '''    #[serde(default)]\n    pub collection_error: Option<String>,\n    #[serde(default)]\n    pub raw_sha256: Option<String>,\n    #[serde(default)]\n    pub raw_compressed_file_sha256: Option<String>,\n    #[serde(default)]\n    pub raw_path: Option<String>,\n''',
        '''    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub collection_error: Option<String>,\n    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub raw_sha256: Option<String>,\n    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub raw_compressed_file_sha256: Option<String>,\n    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub raw_path: Option<String>,\n''',
        "omit absent V0.4 provenance fields",
    )

    replace_once(
        path,
        '''    let mut latest: BTreeMap<(String, String), &NormalizedVenueRow> = BTreeMap::new();\n    for row in rows {\n''',
        '''    let mut latest: BTreeMap<(String, String), &NormalizedVenueRow> = BTreeMap::new();\n    let mut eligible_before_age = false;\n    for row in rows {\n''',
        "track rows eligible before freshness filtering",
    )

    replace_once(
        path,
        '''        if row.known_at > generated_at || row.observed_at > generated_at {\n            continue;\n        }\n        let age = generated_at - row.observed_at;\n''',
        '''        if row.known_at > generated_at || row.observed_at > generated_at {\n            continue;\n        }\n        eligible_before_age = true;\n        let age = generated_at - row.observed_at;\n''',
        "mark pre-age eligible rows",
    )

    replace_once(
        path,
        '''    if latest.is_empty() {\n        return json!({\n''',
        '''    if latest.is_empty() && !eligible_before_age {\n        return json!({\n''',
        "preserve Python stale-row mechanics envelope",
    )


if __name__ == "__main__":
    main()
