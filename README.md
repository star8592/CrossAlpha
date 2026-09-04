# CrossAlpha

**Reconstruct the state of global capital, then allocate risk accordingly.**

CrossAlpha is a research-first, crypto-native systematic global macro platform. The project intentionally separates:

- **Core Engine**: long-history cross-asset trend / momentum / risk research.
- **Observatory**: immutable point-in-time capital-state data collection.
- **Market Engine**: funding / basis / liquidity / instrument / venue routing.

## Current milestone: V0.1 + Observatory O0.4

V0.1 tests simple economic alpha before regime models, ML or RL are allowed. Observatory keeps immutable raw point-in-time facts, rebuildable indexes/canonical data, a causal descriptive Hyperliquid market-state feature layer, and a stablecoin stock/chain accounting layer. No composite risk or trading score is emitted yet.

### Frozen research universe

ES, NQ, GC, SI, HG, CL, CME BTC futures, CME ETH futures, plus cash in the later return engine.

### O0 collectors

- Hyperliquid public market-state snapshots (`metaAndAssetCtxs`, `allMids`).
- DefiLlama stablecoin state snapshot.
- Generic EVM ERC-20 transfer-log collector scaffold (disabled until RPC/contracts are configured).

No wallet labeling, whale-following or alpha inference is performed in O0.

## Local deployment layout

This deployment uses the dedicated 16 TB `/mnt/disk2` volume:

```text
/mnt/disk2/
├── CrossAlpha/       # Git repository / source code
└── CrossAlphaData/   # raw, canonical, derived and manifest data
```

Clone the repository:

```bash
cd /mnt/disk2
git clone https://github.com/star8592/CrossAlpha.git
cd CrossAlpha
bash scripts/bootstrap_local.sh
```

The generated `.env` defaults to:

```bash
CROSSALPHA_DATA_DIR=/mnt/disk2/CrossAlphaData
DATABENTO_API_KEY=db-...
```

Verify the storage layout before starting collectors:

```bash
source .venv/bin/activate
crossalpha doctor
```

Start one public Observatory collection:

```bash
crossalpha collect-observatory
```

Check full historical freshness/gap audit or constant-time live health:

```bash
crossalpha observatory-health
crossalpha observatory-live-health
```

For unattended raw collection on Linux:

```bash
bash scripts/install_user_service.sh
systemctl --user status crossalpha-observatory.service
journalctl --user -u crossalpha-observatory.service -f
```

## O0.2 indexes and canonical layer

The global audit manifest remains immutable. Derived indexes can always be deleted and rebuilt from it:

```bash
crossalpha manifest-rebuild-indexes
```

This creates:

```text
manifests/
├── raw_snapshots.jsonl          # immutable global audit ledger
├── daily/.../raw_snapshots.jsonl
└── series/<source>/<type>.json  # incremental latest/count/interval state
```

Manual full-history canonical rebuilds use:

```bash
crossalpha canonicalize-hyperliquid
crossalpha canonicalize-stablecoins
```

The scheduled materializer does not scan the full audit ledger. It reads only the latest two daily manifest partitions and skips already canonicalized snapshots.

## O0.3 Hyperliquid market-state layer

A manual full-history feature rebuild uses:

```bash
crossalpha build-market-state
```

Current features include:

- mark/oracle basis in bps;
- impact-price spread in bps;
- day return;
- current funding and premium in bps;
- estimated open-interest notional (`open_interest * mark_price`);
- observation-to-observation OI/funding/basis changes;
- causal 24h rolling z-scores for funding, basis, OI change and impact spread;
- rolling observation count to make insufficient history explicit.

The 24h z-scores require at least 24 observations; before that they remain null rather than manufacturing an early signal.

Query latest BTC/ETH state without writing SQL:

```bash
crossalpha market-state --asset BTC --asset ETH
```

## O0.4 stablecoin accounting layer

DefiLlama raw snapshots are canonicalized into two independent tables:

```text
canonical/defillama/
├── stablecoin_assets/
└── stablecoin_chain_supply/
```

The accounting layer then materializes:

```text
derived/stablecoins/
├── system_state/
└── chain_state/
```

Build it manually:

```bash
crossalpha build-stablecoin-state
```

Query the latest system and top chains:

```bash
crossalpha stablecoin-state --top-chains 10
```

The system state currently reports:

- USD-pegged stablecoin count and total stock;
- market value and snapshot-provided 1d/7d/30d supply changes;
- USDT/USDC market shares;
- asset concentration HHI;
- weighted and maximum USD-peg deviation;
- market value more than 50 bps away from the USD peg;
- summed chain supply, chain coverage ratio and conservation residual.

The chain state reports per-chain stock, market value, market share, stablecoin count and chain concentration HHI.

**Important:** this is a stock/accounting layer, not a liquidity-creation signal. CrossAlpha does not assume:

```text
stablecoin supply increase = external capital creation = risk-on
```

A later capital-flow layer must distinguish issuer mint/burn, bridge migration, inventory movement, exchange/protocol deployment and true external capital creation. Chain coverage/residuals are recorded now but are not yet hard quality gates until the upstream accounting behavior is empirically characterized.

### Incremental online materialization

The command used by the timer is incremental:

```bash
crossalpha materialize-observatory
```

It canonicalizes only recent daily partitions, rebuilds only the latest Hyperliquid and stablecoin state days, and refreshes the DuckDB catalog. Its online runtime therefore does not grow linearly with total history.

For an explicit full rebuild after changing parsers or feature definitions:

```bash
crossalpha canonicalize-hyperliquid
crossalpha canonicalize-stablecoins
crossalpha build-market-state
crossalpha build-stablecoin-state
crossalpha build-catalog
```

The database is stored at:

```text
/mnt/disk2/CrossAlphaData/catalog/crossalpha.duckdb
```

Current Observatory views include:

```text
observatory.raw_manifest
observatory.hyperliquid_asset_contexts
observatory.hyperliquid_market_state
observatory.stablecoin_assets
observatory.stablecoin_chain_supply
observatory.stablecoin_system_state
observatory.stablecoin_chain_state
```

### Independent derived-data timer

Raw collection is the critical path; canonicalization/features/catalog are deliberately isolated in a separate timer so a parser/feature failure cannot stop raw history accumulation.

```bash
bash scripts/install_materializer_timer.sh
systemctl --user status crossalpha-materializer.timer
journalctl --user -u crossalpha-materializer.service -n 100 --no-pager
```

The materializer runs every 15 minutes. Raw collection remains every 5 minutes.

## Core V0.1

Fetch first-pass historical futures staging data after adding a Databento API key:

```bash
crossalpha fetch-core --start 2010-06-01
```

> Important: continuous futures staging data is **not** used as naive strategy PnL across rolls. `src/crossalpha/core/futures_roll.py` constructs explicit same-contract MTM returns across a point-in-time-safe roll map.

## Raw-data invariants

- Raw snapshots are append-only gzip envelopes.
- Every snapshot is SHA-256 hashed.
- Manifest records keep both uncompressed `bytes` and, for new snapshots, `compressed_bytes`.
- `observed_at` and `known_at` are preserved for point-in-time research.
- Historical gaps are reported, not silently filled.
- Audit-manifest reads are locked against concurrent appends.
- Canonical and derived Parquet writes use temporary files plus atomic replacement.
- `canonical/`, `derived/`, `catalog/`, and manifest indexes are disposable/rebuildable; `raw/` and the global audit ledger are not.

## Repository policy

GitHub stores source code, config, tests and research protocol only. Raw market/onchain data, backtests and heavy compute remain local under `/mnt/disk2/CrossAlphaData`.

See `docs/ARCHITECTURE.md` and `docs/LOCAL_DATA.md`.
