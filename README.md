# CrossAlpha

**Reconstruct the state of global capital, then allocate risk accordingly.**

CrossAlpha is a research-first, crypto-native systematic global macro platform. The project intentionally separates:

- **Core Engine**: long-history cross-asset trend / momentum / risk research.
- **Observatory**: immutable point-in-time capital-state data collection.
- **Market Engine**: funding / basis / liquidity / instrument / venue routing (later phase).

## Current milestone: V0.1 + Observatory O0

V0.1 tests simple economic alpha before regime models, ML or RL are allowed. O0 begins accumulating point-in-time public market/onchain state before those histories become impossible to recreate perfectly.

### Frozen research universe

ES, NQ, GC, SI, HG, CL, CME BTC futures, CME ETH futures, plus cash in the later return engine.

### O0 collectors

- Hyperliquid public market-state snapshots (`metaAndAssetCtxs`, `allMids`).
- DefiLlama stablecoin state snapshot.
- Generic EVM ERC-20 transfer-log collector scaffold (disabled until RPC/contracts are configured).

No wallet labeling, whale-following or alpha inference is performed in O0.

## Local quick start

```bash
git clone https://github.com/star8592/CrossAlpha.git
cd CrossAlpha
bash scripts/bootstrap_local.sh
```

Edit `.env` and preferably store data outside the Git working tree:

```bash
CROSSALPHA_DATA_DIR=/mnt/disk1/CrossAlphaData
DATABENTO_API_KEY=db-...
```

Start public Observatory collection immediately:

```bash
source .venv/bin/activate
crossalpha collect-observatory
python scripts/collect_loop.py --interval 300
```

Fetch first-pass core historical futures staging data:

```bash
crossalpha fetch-core --start 2010-06-01
```

> Important: continuous futures staging data is **not** used as naive strategy PnL across rolls. The explicit real-contract roll/MTM engine is the next development milestone.

## Repository policy

GitHub stores source code, config, tests and research protocol only. Raw market/onchain data, backtests and heavy compute remain local.

See `docs/ARCHITECTURE.md` and `docs/LOCAL_DATA.md`.
