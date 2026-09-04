from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatabentoRequest:
    symbols: tuple[str, ...]
    start: str
    end: str | None = None
    dataset: str = "GLBX.MDP3"
    schema: str = "ohlcv-1d"
    stype_in: str = "continuous"


@dataclass(frozen=True)
class ParentFuturesRequest:
    roots: tuple[str, ...]
    start: str
    end: str | None = None
    dataset: str = "GLBX.MDP3"

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(root if root.endswith(".FUT") else f"{root}.FUT" for root in self.roots)


class DatabentoCoreProvider:
    """Historical downloader with cost-first parent-futures staging helpers."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("DATABENTO_API_KEY is required")
        self.api_key = api_key

    def _client(self):
        try:
            import databento as db
        except ImportError as exc:
            raise RuntimeError("Install Databento support with: pip install -e '.[databento]'") from exc
        return db, db.Historical(self.api_key)

    @staticmethod
    def _with_optional_end(kwargs: dict[str, object], end: str | None) -> dict[str, object]:
        if end:
            kwargs["end"] = end
        return kwargs

    def fetch_continuous_daily(self, request: DatabentoRequest, output_dir: Path) -> Path:
        _, client = self._client()
        kwargs: dict[str, object] = {
            "dataset": request.dataset,
            "schema": request.schema,
            "symbols": list(request.symbols),
            "stype_in": request.stype_in,
            "start": request.start,
        }
        self._with_optional_end(kwargs, request.end)
        data = client.timeseries.get_range(**kwargs)
        df = data.to_df()
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "continuous_daily.parquet"
        df.to_parquet(out)
        return out

    def estimate_parent_cost(self, request: ParentFuturesRequest, *, schema: str) -> float:
        """Estimate Databento billed cost before any parent-futures download."""
        _, client = self._client()
        kwargs: dict[str, object] = {
            "dataset": request.dataset,
            "schema": schema,
            "symbols": list(request.symbols),
            "stype_in": "parent",
            "start": request.start,
        }
        self._with_optional_end(kwargs, request.end)
        return float(client.metadata.get_cost(**kwargs))

    def fetch_parent_definitions(self, request: ParentFuturesRequest, output_dir: Path) -> Path:
        """Fetch point-in-time parent definitions, including outrights and spreads.

        Filtering to outright futures is deliberately deferred to canonicalization so
        the raw staging file preserves exactly what the parent request returned.
        """
        _, client = self._client()
        kwargs: dict[str, object] = {
            "dataset": request.dataset,
            "schema": "definition",
            "symbols": list(request.symbols),
            "stype_in": "parent",
            "start": request.start,
        }
        self._with_optional_end(kwargs, request.end)
        data = client.timeseries.get_range(**kwargs)
        frame = data.to_df()
        if frame.index.name and frame.index.name not in frame.columns:
            frame = frame.reset_index()
        else:
            frame = frame.reset_index(drop=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "parent_definitions.parquet"
        frame.to_parquet(out, index=False)
        return out

    def fetch_parent_daily(self, request: ParentFuturesRequest, output_dir: Path) -> Path:
        """Fetch all parent futures/spread daily bars as immutable staging data."""
        _, client = self._client()
        kwargs: dict[str, object] = {
            "dataset": request.dataset,
            "schema": "ohlcv-1d",
            "symbols": list(request.symbols),
            "stype_in": "parent",
            "start": request.start,
        }
        self._with_optional_end(kwargs, request.end)
        data = client.timeseries.get_range(**kwargs)
        frame = data.to_df()
        if frame.index.name and frame.index.name not in frame.columns:
            frame = frame.reset_index()
        else:
            frame = frame.reset_index(drop=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "parent_ohlcv_1d.parquet"
        frame.to_parquet(out, index=False)
        return out
