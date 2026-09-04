from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SourceType(StrEnum):
    CHAIN = "CHAIN"
    EXCHANGE = "EXCHANGE"
    DATA_VENDOR = "DATA_VENDOR"
    AGGREGATOR = "AGGREGATOR"
    ORACLE = "ORACLE"
    ISSUER = "ISSUER"
    REGULATOR = "REGULATOR"
    INFERENCE = "INFERENCE"


class ObservationEnvelope(BaseModel):
    schema_version: int = 1
    event_time: datetime | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    known_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_type: SourceType
    source_id: str
    observation_type: str
    payload: Any
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawSnapshotManifest(BaseModel):
    path: str
    sha256: str
    bytes: int
    observed_at: datetime
    source_id: str
    observation_type: str
