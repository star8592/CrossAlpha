from __future__ import annotations

from pathlib import Path

import pytest

from crossalpha.core.databento_provider import DatabentoCoreProvider, ParentFuturesRequest


class _FakeMetadata:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def get_cost(self, **kwargs):
        self.kwargs = kwargs
        return 1.2345


class _FakeClient:
    def __init__(self) -> None:
        self.metadata = _FakeMetadata()


def test_parent_request_and_cost_estimate_use_parent_symbology(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = DatabentoCoreProvider("db-test")
    fake = _FakeClient()
    monkeypatch.setattr(provider, "_client", lambda: (object(), fake))
    request = ParentFuturesRequest(roots=("ES", "NQ.FUT"), start="2026-01-01", end="2026-02-01")

    cost = provider.estimate_parent_cost(request, schema="ohlcv-1d")
    assert cost == pytest.approx(1.2345)
    assert request.symbols == ("ES.FUT", "NQ.FUT")
    assert fake.metadata.kwargs is not None
    assert fake.metadata.kwargs["symbols"] == ["ES.FUT", "NQ.FUT"]
    assert fake.metadata.kwargs["stype_in"] == "parent"
    assert fake.metadata.kwargs["schema"] == "ohlcv-1d"
    assert fake.metadata.kwargs["start"] == "2026-01-01"
    assert fake.metadata.kwargs["end"] == "2026-02-01"


def test_existing_paid_output_is_rejected_before_client_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = DatabentoCoreProvider("db-test")
    request = ParentFuturesRequest(roots=("ES",), start="2026-01-01", end="2026-02-01")
    existing = tmp_path / "parent_ohlcv_1d.parquet"
    existing.write_bytes(b"already-downloaded")

    def _must_not_create_client():
        raise AssertionError("Databento client must not be created for duplicate output")

    monkeypatch.setattr(provider, "_client", _must_not_create_client)
    with pytest.raises(FileExistsError, match="refusing duplicate paid download"):
        provider.fetch_parent_daily(request, tmp_path)
