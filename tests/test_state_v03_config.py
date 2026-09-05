from __future__ import annotations

from pathlib import Path

from crossalpha.state.v03_config import strict_v03_config_report


def test_state_v03_yaml_matches_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    report = strict_v03_config_report(root / "config" / "state_v03.yaml")
    assert report["ok"] is True, report
    assert all(report["checks"].values())
