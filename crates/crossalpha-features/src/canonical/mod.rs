pub mod hyperliquid;
pub mod stablecoins;

use anyhow::{Context, Result, bail};
use crossalpha_storage::{ObservationEnvelope, RawSnapshotManifest, read_audit_manifest};
use flate2::read::GzDecoder;
use std::fs::File;
use std::io::Read;
use std::path::Path;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CanonicalSource {
    Hyperliquid,
    Stablecoins,
}

impl CanonicalSource {
    pub fn source_id(self) -> &'static str {
        match self {
            Self::Hyperliquid => "hyperliquid",
            Self::Stablecoins => "defillama",
        }
    }

    pub fn observation_type(self) -> &'static str {
        match self {
            Self::Hyperliquid => "metaAndAssetCtxs",
            Self::Stablecoins => "stablecoins_snapshot",
        }
    }
}

pub fn latest_record_for_source(
    data_root: &Path,
    source: CanonicalSource,
) -> Result<RawSnapshotManifest> {
    let records = read_audit_manifest(data_root)?;
    records
        .into_iter()
        .filter(|record| {
            record.source_id == source.source_id()
                && record.observation_type == source.observation_type()
        })
        .max_by_key(|record| record.observed_at)
        .with_context(|| {
            format!(
                "no raw record found for {}/{}",
                source.source_id(),
                source.observation_type()
            )
        })
}

pub fn load_envelope(record: &RawSnapshotManifest) -> Result<ObservationEnvelope> {
    let path = Path::new(&record.path);
    let file = File::open(path).with_context(|| format!("open raw snapshot {}", path.display()))?;
    let mut decoder = GzDecoder::new(file);
    let mut bytes = Vec::new();
    decoder
        .read_to_end(&mut bytes)
        .with_context(|| format!("decompress raw snapshot {}", path.display()))?;
    let envelope: ObservationEnvelope = serde_json::from_slice(&bytes)
        .with_context(|| format!("parse raw snapshot {}", path.display()))?;

    if envelope.source_id != record.source_id || envelope.observation_type != record.observation_type {
        bail!(
            "raw envelope identity mismatch: envelope={}/{} manifest={}/{}",
            envelope.source_id,
            envelope.observation_type,
            record.source_id,
            record.observation_type
        );
    }
    Ok(envelope)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_source_contract() {
        assert_eq!(CanonicalSource::Hyperliquid.source_id(), "hyperliquid");
        assert_eq!(
            CanonicalSource::Hyperliquid.observation_type(),
            "metaAndAssetCtxs"
        );
        assert_eq!(CanonicalSource::Stablecoins.source_id(), "defillama");
        assert_eq!(
            CanonicalSource::Stablecoins.observation_type(),
            "stablecoins_snapshot"
        );
    }
}
