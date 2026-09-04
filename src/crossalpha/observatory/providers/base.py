from __future__ import annotations

from abc import ABC, abstractmethod

from crossalpha.domain.models import ObservationEnvelope


class SnapshotProvider(ABC):
    @abstractmethod
    async def collect(self) -> list[ObservationEnvelope]:
        raise NotImplementedError
