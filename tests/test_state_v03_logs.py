from __future__ import annotations

import pytest

from crossalpha.state.v03_logs import (
    BLOCKSCOUT_MAX_LOG_RESULTS,
    BorrowLogResultLimit,
    parse_blockscout_logs,
)


def test_blockscout_log_parser_accepts_complete_rows() -> None:
    rows = [{"topics": ["0x1"]}, {"topics": ["0x2"]}]
    assert parse_blockscout_logs({"status": "1", "message": "OK", "result": rows}) == rows


def test_blockscout_log_parser_accepts_empty_no_records_response() -> None:
    assert parse_blockscout_logs(
        {"status": "0", "message": "No records found", "result": []}
    ) == []


def test_blockscout_log_parser_rejects_possible_hard_limit_truncation() -> None:
    rows = [{"logIndex": hex(index)} for index in range(BLOCKSCOUT_MAX_LOG_RESULTS)]
    with pytest.raises(BorrowLogResultLimit, match="hard result limit"):
        parse_blockscout_logs({"status": "1", "result": rows})


def test_blockscout_log_parser_fails_closed_on_non_list_result() -> None:
    with pytest.raises(RuntimeError, match="indexed-log query failed"):
        parse_blockscout_logs({"status": "0", "message": "gateway error", "result": "error"})
