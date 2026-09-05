from crossalpha.outcomes.prospective import config_consistency_report


def test_outcome_linkage_yaml_matches_implementation() -> None:
    report = config_consistency_report()
    assert report["ok"] is True, report
    assert all(report["checks"].values())
