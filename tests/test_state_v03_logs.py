from __future__ import annotations

import pytest

from crossalpha.state import v03_prospective
from crossalpha.state.v03_logs import (
    BLOCKSCOUT_ETHEREUM_RPC_URL,
    BLOCKSCOUT_MAX_LOG_RESULTS,
    BLOCKSCOUT_STATE_RPC_SOURCE,
    BorrowLogResultLimit,
    parse_blockscout_logs,
    resolve_state_rpc_candidates,
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


def test_state_rpc_candidates_prefer_operator_then_blockscout() -> None:
    configured = resolve_state_rpc_candidates("http://operator.invalid")
    assert configured[0] == ("http://operator.invalid", "EVM_RPC_URL")
    assert configured[1] == (BLOCKSCOUT_ETHEREUM_RPC_URL, BLOCKSCOUT_STATE_RPC_SOURCE)
    public = resolve_state_rpc_candidates(None)
    assert public[0] == (BLOCKSCOUT_ETHEREUM_RPC_URL, BLOCKSCOUT_STATE_RPC_SOURCE)


def test_v03_freeze_implementation_set_includes_indexed_log_reader() -> None:
    files = v03_prospective._implementation_files()
    assert "state_v03_logs" in files
    assert files["state_v03_logs"].name == "v03_logs.py"
