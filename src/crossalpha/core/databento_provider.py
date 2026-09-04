from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatabentoRequest:
    symbols: tuple[str, ...]
    start: str
    end: str
    dataset: str = "GLBX.MDP3"
    schema: str = "ohlcv-1d"
    stype_in: str = "continuous"


@dataclass(frozen=True)
class ParentFuturesRequest:
    roots: tuple[str, ...]
    start: str
    end: str
    dataset: str = "GLBX.MDP3"

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(root if root.endswith(".FUT") else f"{root}.FUT" for root in self.roots)


class DatabentoCoreProvider:
    """Historical downloader with explicit ranges and cost-first parent-futures helpers."""

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
    def _refuse_existing(path: Path) -> None:
        if path.exists():
            raise FileExistsError(
                f"refusing duplicate paid download because output already exists: {path}"
            )

    @staticmethod
    def _validate_range(start: str, end: str) -> None:
        if not start or not end:
            raise ValueError("Databento start and end must both be explicit")
        if start == end:
            raise ValueError("Databento end must differ from start")

    def estimate_cost(self, request: DatabentoRequest) -> float:
        self._validate_range(request.start, request.end)
        _, client = self._client()
        return float(
            client.metadata.get_cost(
                dataset=request.dataset,
                schema=request.schema,
                symbols=list(request.symbols),
                stype_in=request.stype_in,
                start=request.start,
                end=request.end,
            )
        )

    def fetch_continuous_daily(self, request: DatabentoRequest, output_dir: Path) -> Path:
        self._validate_range(request.start, request.end)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "continuous_daily.parquet"
        self._refuse_existing(out)

        _, client = self._client()
        data = client.timeseries.get_range(
            dataset=request.dataset,
            schema=request.schema,
            symbols=list(request.symbols),
            stype_in=request.stype_in,
            start=request.start,
            end=request.end,
        )
        df = data.to_df()
        df.to_parquet(out)
        return out

    def estimate_parent_cost(self, request: ParentFuturesRequest, *, schema: str) -> float:
        """Estimate Databento billed cost before any parent-futures download."""
        self._validate_range(request.start, request.end)
        _, client = self._client()
        return float(
            client.metadata.get_cost(
                dataset=request.dataset,
                schema=schema,
                symbols=list(request.symbols),
                stype_in="parent",
                start=request.start,
                end=request.end,
            )
        )

    def fetch_parent_definitions(self, request: ParentFuturesRequest, output_dir: Path) -> Path:
        """Fetch point-in-time parent definitions, including outrights and spreads."""
        self._validate_range(request.start, request.end)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "parent_definitions.parquet"
        self._refuse_existing(out)

        _, client = self._client()
        data = client.timeseries.get_range(
            dataset=request.dataset,
            schema="definition",
            symbols=list(request.symbols),
            stype_in="parent",
            start=request.start,
            end=request.end,
        )
        frame = data.to_df()
        if frame.index.name and frame.index.name not in frame.columns:
            frame = frame.reset_index()
        else:
            frame = frame.reset_index(drop=True)
        frame.to_parquet(out, index=False)
        return out

    def fetch_parent_daily(self, request: ParentFuturesRequest, output_dir: Path) -> Path:
        """Fetch all parent futures/spread daily bars as immutable staging data."""
        self._validate_range(request.start, request.end)
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "parent_ohlcv_1d.parquet"
        self._refuse_existing(out)

        _, client = self._client()
        data = client.timeseries.get_range(
            dataset=request.dataset,
            schema="ohlcv-1d",
            symbols=list(request.symbols),
            stype_in="parent",
            start=request.start,
            end=request.end,
        )
        frame = data.to_df()
        if frame.index.name and frame.index.name not in frame.columns:
            frame = frame.reset_index()
        else:
            frame = frame.reset_index(drop=True)
        frame.to_parquet(out, index=False)
        return out
