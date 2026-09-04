# Local data plan

CrossAlpha uses the dedicated 16 TB `/mnt/disk2` volume. Keep source code and data physically separated:

```text
/mnt/disk2/
├── CrossAlpha/
└── CrossAlphaData/
    ├── raw/
    ├── canonical/
    ├── derived/
    ├── manifests/
    ├── research/
    └── archive/
```

Recommended setup:

```bash
mkdir -p /mnt/disk2/CrossAlphaData/{raw,canonical,derived,manifests,research,archive}
```

Set `.env`:

```bash
CROSSALPHA_DATA_DIR=/mnt/disk2/CrossAlphaData
```

The Git repository contains code/config only. Raw and derived data remain local and are never committed.

## Storage policy

- `raw/`: immutable source payloads and vendor/native records. Append-only.
- `canonical/`: normalized point-in-time tables with stable schemas.
- `derived/`: features, return indices, roll maps and state aggregates that can be rebuilt from canonical/raw data.
- `manifests/`: hashes, provenance, gaps, observations and data-version manifests.
- `research/`: backtest and validation outputs; reproducible but retained for research lineage.
- `archive/`: cold snapshots and retired source versions.

Raw data should never be overwritten in place. Corrections create a new observation/version and preserve the original record.

## Observatory O0

```bash
crossalpha collect-observatory
python scripts/collect_loop.py --interval 300
```

Raw files are immutable gzipped JSON envelopes partitioned by source/type/date. Each contains `observed_at`, `known_at`, source metadata and raw payload. No wallet labels or alpha inference are applied at O0.

For unattended collection:

```bash
bash scripts/install_user_service.sh
```

## Core historical futures

Set `DATABENTO_API_KEY` in `.env`, then:

```bash
crossalpha fetch-core --start 2010-06-01
```

This staging download uses continuous volume-front symbols for ES/NQ/GC/SI/HG/CL/BTC/ETH. It is not yet a valid strategy-PnL series across rolls. The next milestone builds explicit contract/roll/MTM accounting.

## Capacity guidance

The 16 TB disk is intentionally much larger than V0.1 needs. Do not fill it with indiscriminate full-chain or full-order-book history. Retain high-value point-in-time state first, then increase granularity only where a research hypothesis justifies the storage cost.
