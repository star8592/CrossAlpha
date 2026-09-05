from pathlib import Path

from crossalpha.state.v04_config import strict_v04_config_report
from crossalpha.state.v04_prospective import _implementation_files


def test_state_v04_yaml_matches_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    report = strict_v04_config_report(root / "config" / "state_v04.yaml")
    assert report["ok"] is True, report
    assert all(report["checks"].values())


def test_state_v04_freeze_hashes_fault_isolation_runtime() -> None:
    files = _implementation_files()
    assert "state_v04_safe_provider" in files
    assert files["state_v04_safe_provider"].name == "v04_safe_provider.py"
    assert files["state_v04_safe_provider"].exists()
