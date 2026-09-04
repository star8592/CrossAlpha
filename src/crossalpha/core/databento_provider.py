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


class DatabentoCoreProvider:
    """Historical downloader; continuous data is staging, not naive roll PnL."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("DATABENTO_API_KEY is required")
        self.api_key = api_key

    def fetch_continuous_daily(self, request: DatabentoRequest, output_dir: Path) -> Path:
        try:
            import databento as db
        except ImportError as exc:
            raise RuntimeError("Install Databento support with: pip install -e '.[databento]'") from exc

        client = db.Historical(self.api_key)
        kwargs = {"dataset": request.dataset, "schema": request.schema, "symbols": list(request.symbols), "stype_in": request.stype_in, "start": request.start}
        if request.end:
            kwargs["end"] = request.end
        data = client.timeseries.get_range(**kwargs)
        df = data.to_df()
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "continuous_daily.parquet"
        df.to_parquet(out)
        return out
