from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class QualityReport:
    rows: int
    duplicate_index: int
    null_close: int
    monotonic_index: bool

    @property
    def ok(self) -> bool:
        return self.rows > 0 and self.duplicate_index == 0 and self.null_close == 0 and self.monotonic_index


def validate_ohlcv_parquet(path: Path) -> QualityReport:
    df = pd.read_parquet(path)
    close_col = "close" if "close" in df.columns else None
    null_close = int(df[close_col].isna().sum()) if close_col else len(df)
    index = pd.Index(df.index)
    return QualityReport(rows=len(df), duplicate_index=int(index.duplicated().sum()), null_close=null_close, monotonic_index=index.is_monotonic_increasing)
