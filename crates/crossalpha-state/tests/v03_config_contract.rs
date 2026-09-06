use crossalpha_state::v03::StateV03;
use std::path::PathBuf;

#[test]
fn repository_state_v03_config_matches_native_contract() {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("config/state_v03.yaml");
    let report = StateV03::strict_config_report(&path).unwrap();
    let failed: Vec<_> = report
        .checks
        .iter()
        .filter_map(|(name, ok)| (!ok).then_some(name.as_str()))
        .collect();
    assert!(report.ok, "failed State V0.3 checks: {failed:?}");
    assert!(report.checks.len() >= 50);
}
