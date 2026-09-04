# Local data plan

Recommended local path:

```bash
mkdir -p /mnt/disk1/CrossAlphaData
```

Set `.env`:

```bash
CROSSALPHA_DATA_DIR=/mnt/disk1/CrossAlphaData
```

The Git repository contains code/config only. Raw and derived data remain local.

## Observatory O0

```bash
crossalpha collect-observatory
python scripts/collect_loop.py --interval 300
```

Raw files are immutable gzipped JSON envelopes partitioned by source/type/date. Each contains `observed_at`, `known_at`, source metadata and raw payload. No wallet labels or alpha inference are applied at O0.

## Core historical futures

Set `DATABENTO_API_KEY` in `.env`, then:

```bash
crossalpha fetch-core --start 2010-06-01
```

This staging download uses continuous volume-front symbols for ES/NQ/GC/SI/HG/CL/BTC/ETH. It is not yet a valid strategy-PnL series across rolls. The next milestone builds explicit contract/roll/MTM accounting.
