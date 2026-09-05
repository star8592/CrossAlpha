from __future__ import annotations

import pytest

from crossalpha.state.v03_rpc import (
    BORROW_EVENT_TOPIC0,
    DEFAULT_PUBLIC_ETHEREUM_RPC,
    UINT256_MAX,
    borrow_log_debtor,
    decode_get_user_account_data,
    encode_get_user_account_data,
    resolve_rpc_url,
)


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def _word(value: int) -> str:
    return f"{value:064x}"


def test_rpc_resolution_prefers_env_and_has_zero_cost_fallback() -> None:
    assert resolve_rpc_url("http://localhost:8545") == ("http://localhost:8545", "EVM_RPC_URL")
    assert resolve_rpc_url(None) == (
        DEFAULT_PUBLIC_ETHEREUM_RPC,
        "PUBLICNODE_ZERO_COST_FALLBACK",
    )


def test_borrow_log_uses_indexed_on_behalf_of_and_ignores_removed() -> None:
    reserve = "0x1111111111111111111111111111111111111111"
    debtor = "0x2222222222222222222222222222222222222222"
    referral = "0x" + "0" * 63 + "1"
    log = {
        "removed": False,
        "topics": [BORROW_EVENT_TOPIC0, _topic(reserve), _topic(debtor), referral],
    }
    assert borrow_log_debtor(log) == debtor
    assert borrow_log_debtor({**log, "removed": True}) is None


def test_get_user_account_data_encoding_is_selector_plus_padded_address() -> None:
    user = "0x3333333333333333333333333333333333333333"
    encoded = encode_get_user_account_data(user)
    assert encoded.startswith("0xbf92857c")
    assert len(encoded) == 2 + 8 + 64
    assert encoded.endswith(user[2:])


def test_decode_get_user_account_data_scales_base_and_health_factor() -> None:
    raw = "0x" + "".join(
        _word(value)
        for value in (
            12_345_00000000,
            4_000_00000000,
            2_000_00000000,
            8250,
            7000,
            1_234_000000000000000,
        )
    )
    decoded = decode_get_user_account_data(raw)
    assert decoded["total_collateral_usd"] == pytest.approx(12_345.0)
    assert decoded["total_debt_usd"] == pytest.approx(4_000.0)
    assert decoded["available_borrows_usd"] == pytest.approx(2_000.0)
    assert decoded["current_liquidation_threshold_pct"] == pytest.approx(82.5)
    assert decoded["ltv_pct"] == pytest.approx(70.0)
    assert decoded["health_factor"] == pytest.approx(1.234)


def test_zero_debt_uintmax_health_factor_is_unknown_not_infinite_float() -> None:
    raw = "0x" + "".join(_word(value) for value in (0, 0, 0, 0, 0, UINT256_MAX))
    decoded = decode_get_user_account_data(raw)
    assert decoded["health_factor"] is None


def test_invalid_account_data_length_fails_closed() -> None:
    with pytest.raises(ValueError, match="unexpected byte length"):
        decode_get_user_account_data("0x1234")
