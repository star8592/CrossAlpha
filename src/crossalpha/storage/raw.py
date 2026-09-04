from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from crossalpha.domain.models import ObservationEnvelope, RawSnapshotManifest


class RawSnapshotStore:
    """Append-only raw fact store. Existing snapshots are never overwritten."""

    def __init__(self, root: Path):
        self.root = root

    def write(self, envelope: ObservationEnvelope) -> RawSnapshotManifest:
        observed = envelope.observed_at
        rel_dir = Path(
            envelope.source_id,
            envelope.observation_type,
            f"year={observed:%Y}",
            f"month={observed:%m}",
            f"day={observed:%d}",
        )
        directory = self.root / "raw" / rel_dir
        directory.mkdir(parents=True, exist_ok=True)

        stamp = observed.strftime("%Y%m%dT%H%M%S.%fZ")
        payload_bytes = envelope.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(payload_bytes).hexdigest()
        filename = f"{stamp}_{digest[:12]}.json.gz"
        path = directory / filename

        with gzip.open(path, "xb") as fh:
            fh.write(payload_bytes)

        manifest = RawSnapshotManifest(
            path=str(path),
            sha256=digest,
            bytes=len(payload_bytes),
            observed_at=observed,
            source_id=envelope.source_id,
            observation_type=envelope.observation_type,
        )
        self._append_manifest(manifest)
        return manifest

    def _append_manifest(self, manifest: RawSnapshotManifest) -> None:
        manifest_dir = self.root / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        path = manifest_dir / "raw_snapshots.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False) + "\n")
