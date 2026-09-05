from __future__ import annotations

from pathlib import Path

import pytest

from crossalpha.state import v03_prospective
from crossalpha.state.v03_config import strict_v03_config_report
from crossalpha.state.v03_logs import (
    BLOCKSCOUT_MAX_LOG_RESULTS,
    BorrowLogResultLimit,
    parse_blockscout_logs,
)


def test_state_v03_yaml_matches_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    report = strict_v03_config_report(root / "config" / "state_v03.yaml")
    assert report["ok"] is True, report
    assert all(report["checks"].values())


def test_targeted_suite_locks_indexed_log_completeness_and_freeze_hash() -> None:
    rows = [{"logIndex": hex(index)} for index in range(BLOCKSCOUT_MAX_LOG_RESULTS)]
    with pytest.raises(BorrowLogResultLimit):
        parse_blockscout_logs({"status": "1", "result": rows})
    assert "state_v03_logs" in v03_prospective._implementation_files()
