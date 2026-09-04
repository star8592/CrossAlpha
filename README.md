# CrossAlpha

**Reconstruct the state of global capital, then allocate risk accordingly.**

CrossAlpha is a research-first, crypto-native systematic global macro platform. The project intentionally separates:

- **Core Engine**: long-history cross-asset trend / momentum / risk research.
- **Observatory**: immutable point-in-time capital-state data collection.
- **Market Engine**: funding / basis / liquidity / instrument / venue routing.

## Hard data policy: V0.1 must be free

CrossAlpha V0.1 has a hard requirement:

```text
required market-data cost = $0
```

Every required research, backtest and Observatory path must work without purchasing data. Paid vendors are optional validation adapters only; the project must remain fully usable if no paid key is configured.

This changes the Core research object. V0.1 **does not claim to be a CME futures excess-return study**. It studies free, investable economic-exposure proxies:

| Economic exposure | Free V0.1 research proxy | Required source |
|---|---|---|
| Broad US equity | SPY | Tiingo Starter |
| US growth / Nasdaq-100 | QQQ | Tiingo Starter |
| Gold | GLD | Tiingo Starter |
| Silver | SLV | Tiingo Starter |
| Copper | CPER | Tiingo Starter |
| WTI crude | USO | Tiingo Starter |
| Bitcoin | BTCUSDT spot | Binance public market data |
| Ether | ETHUSDT spot | Binance public market data |
| Cash rate | DGS3MO | FRED |

Tiingo Starter is a free account/token path used only for local/internal research. Raw Tiingo data is not intended for redistribution. Binance spot market data requires no API key. FRED API keys are free.

Commodity ETF/ETP returns include fund fees and roll effects by construction. Availability is respected: no proxy or crypto series is backfilled before its actual inception/listing.

## Current milestone: Core V0.1 + Observatory O0.4

V0.1 tests simple economic alpha before regime models, ML or RL are allowed. Observatory keeps immutable raw point-in-time facts, rebuildable indexes/canonical data, a causal descriptive Hyperliquid market-state feature layer, and a stablecoin stock/chain accounting layer. No composite risk or trading score is emitted yet.

## Local deployment layout

This deployment uses the dedicated 16 TB `/mnt/disk2` volume:

```text
/mnt/disk2/
├── CrossAlpha/       # Git repository / source code
└── CrossAlphaData/   # raw, canonical, derived and manifest data
```

Clone/bootstrap:

```bash
cd /mnt/disk2
git clone https://github.com/star8592/CrossAlpha.git
cd CrossAlpha
bash scripts/bootstrap_local.sh
```

The generated `.env` uses the free path by default:

```bash
TIINGO_API_TOKEN=
FRED_API_KEY=
DATABENTO_API_KEY=        # optional paid validation only
EVM_RPC_URL=
CROSSALPHA_DATA_DIR=/mnt/disk2/CrossAlphaData
CROSSALPHA_HTTP_TIMEOUT=30
```

`bootstrap_local.sh` installs only `.[dev]`; Databento is not installed by default.

Check storage and free-data readiness:

```bash
source .venv/bin/activate
crossalpha doctor
crossalpha free-core-status
```

## Free Core V0.1

After adding a free Tiingo token and free FRED API key to `.env`, fetch the complete required Core dataset:

```bash
crossalpha fetch-core-free \
  --start 2010-06-01 \
  --end 2026-09-01
```

The output explicitly reports:

```text
mode = free_only
data_cost_usd = 0
```

Source layout:

```text
raw/free_core/
├── tiingo/      # SPY QQQ GLD SLV CPER USO raw EOD responses
├── binance/     # BTCUSDT ETHUSDT public daily kline pages
└── fred/        # DGS3MO raw observations

canonical/core/
├── free_proxy_daily/
│   ├── tradfi.parquet
│   └── crypto.parquet
└── cash_rate/
    └── DGS3MO.parquet
```

For TradFi proxies both raw and adjusted prices are retained. Strategy research uses adjusted close so splits/distributions do not create artificial return jumps. Crypto spot uses raw daily OHLCV. Cash rate is stored separately and will be converted to accrual in the portfolio return engine.

### What free V0.1 can test honestly

The primary question is:

> Without macro prediction, ML, leverage or shorts, can trend + relative momentum + risk allocation across freely reproducible economic-exposure proxies deliver robust out-of-sample results?

It can test allocation robustness across equities, precious metals, industrial commodities, energy and crypto. It cannot claim to isolate CME roll mechanics, exchange-specific futures carry or institutional futures execution costs.

## Optional paid futures validation

The old Databento adapter is retained only for future validation of the free-proxy conclusions against actual CME child contracts. It is not required for V0.1 and is not installed by bootstrap.

If intentionally needed later:

```bash
pip install -e ".[databento]"
```

Then the existing cost-first commands remain available:

```bash
crossalpha estimate-core-parent --start 2010-06-01 --end 2026-09-01
crossalpha fetch-core-parent --start 2010-06-01 --end 2026-09-01 --max-cost-usd 5
```

These commands are **outside the free V0.1 research requirement**.

## O0 collectors

Current required Observatory sources are free/public:

- Hyperliquid public market-state snapshots (`metaAndAssetCtxs`, `allMids`).
- DefiLlama stablecoin state snapshot.
- Generic EVM ERC-20 transfer-log collector scaffold (disabled until free/public RPC/contracts are configured).

No wallet labeling, whale-following or alpha inference is performed in O0.

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

Manual canonical rebuilds:

```bash
crossalpha canonicalize-hyperliquid
crossalpha canonicalize-stablecoins
```

## O0.3 Hyperliquid market-state layer

Build full-history features:

```bash
crossalpha build-market-state
```

Current descriptive features include:

- mark/oracle basis in bps;
- impact-price spread in bps;
- day return;
- current funding and premium in bps;
- estimated open-interest notional (`open_interest * mark_price`);
- observation-to-observation OI/funding/basis changes;
- causal 24h rolling z-scores for funding, basis, OI change and impact spread;
- rolling observation count to make insufficient history explicit.

The 24h z-scores require at least 24 observations; before that they remain null rather than manufacturing an early signal.

Query latest BTC/ETH state:

```bash
crossalpha market-state --asset BTC --asset ETH
```

## O0.4 stablecoin accounting layer

DefiLlama raw snapshots are canonicalized into:

```text
canonical/defillama/
├── stablecoin_assets/
└── stablecoin_chain_supply/
```

The accounting layer materializes:

```text
derived/stablecoins/
├── system_state/
└── chain_state/
```

Query the latest state:

```bash
crossalpha stablecoin-state --top-chains 10
```

The system reports USD-pegged supply, 1d/7d/30d changes with coverage ratios, USDT/USDC shares, asset concentration, peg stress, observed chain supply, coverage and conservation residual.

**Important:** this is a stock/accounting layer, not a liquidity-creation signal. CrossAlpha does not assume:

```text
stablecoin supply increase = external capital creation = risk-on
```

A later capital-flow layer must distinguish issuer mint/burn, bridge migration, inventory movement, exchange/protocol deployment and true external capital creation.

## Incremental online materialization

```bash
crossalpha materialize-observatory
```

It canonicalizes recent partitions, rebuilds only the latest state days and refreshes DuckDB. Derived failures cannot stop raw collection.

Install the independent derived-data timer:

```bash
bash scripts/install_materializer_timer.sh
systemctl --user status crossalpha-materializer.timer
journalctl --user -u crossalpha-materializer.service -n 100 --no-pager
```

Raw collection runs every 5 minutes; materialization runs every 15 minutes.

DuckDB is stored at:

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

## Raw-data invariants

- Raw Observatory snapshots are append-only gzip envelopes.
- Every snapshot is SHA-256 hashed.
- Manifest records keep both uncompressed `bytes` and `compressed_bytes` when available.
- `observed_at` and `known_at` are preserved for point-in-time research.
- Historical gaps are reported, not silently filled.
- Audit-manifest reads are locked against concurrent appends.
- Canonical and derived Parquet writes use temporary files plus atomic replacement.
- `canonical/`, `derived/`, `catalog/`, and manifest indexes are disposable/rebuildable; Observatory `raw/` and the global audit ledger are not.

## Repository policy

GitHub stores source code, config, tests and research protocol only. Raw market/onchain data, backtests and heavy compute remain local under `/mnt/disk2/CrossAlphaData`.

See `docs/ARCHITECTURE.md` and `docs/LOCAL_DATA.md`.
