from pathlib import Path

from crossalpha.state.v04_config import strict_v04_config_report


def test_state_v04_yaml_matches_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    report = strict_v04_config_report(root / "config" / "state_v04.yaml")
    assert report["ok"] is True, report
    assert all(report["checks"].values())
