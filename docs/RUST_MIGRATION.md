# CrossAlpha Rust Migration

## Objective

Move CrossAlpha from a Python-first research/runtime stack to a Rust-first production architecture without invalidating existing research artifacts, point-in-time semantics, manifests, Parquet files, YAML configs, freeze evidence, or prospective ledgers.

Python remains a parity/reference implementation until the final local acceptance and production-soak gates pass. It must not share production-writer ownership with Rust.

## Non-negotiable invariants

1. Point-in-time correctness must not change.
2. Raw snapshots and audit manifests remain immutable.
3. Historical Raw Envelope V1 records remain readable and are never rewritten.
4. New Rust raw writers use the Raw Envelope V2 canonical-byte contract.
5. Existing YAML remains readable during migration.
6. Parquet/JSONL schemas are explicit; no silent coercion or rewrite is allowed.
7. Research/state kernels must pass deterministic and real-data parity gates before retirement.
8. Required V0.1 market-data cost remains USD 0.
9. GitHub is the source/control plane. Repository qualification runs only on the dedicated CrossAlpha self-hosted runner; production data, live production state, binding activation, cutover and soak remain explicit local operations.
10. Python and Rust production writers must never run concurrently.
11. Runtime bindings are fail-closed and bind production source, Cargo.lock and predecessor freeze evidence.

## Rust workspace

```text
crates/
  crossalpha-domain/
  crossalpha-config/
  crossalpha-storage/
  crossalpha-data/
  crossalpha-observatory/
  crossalpha-features/
  crossalpha-state/
  crossalpha-outcomes/
  crossalpha-research/
  crossalpha-market/
  crossalpha-cli/
```

## Automation control plane

The sole repository qualification workflow is `.github/workflows/rust-ci.yml`.

It targets only the dedicated runner label set:

```text
[self-hosted, linux, x64, crossalpha]
```

The runner can be installed with:

```bash
bash scripts/install_github_self_hosted_runner.sh
```

The workflow owns repeatable source qualification only:

1. checkout the exact PR/branch commit;
2. Rustfmt gate;
3. workspace Clippy with `-D warnings`;
4. Rust workspace tests;
5. Python reference tests;
6. debug build;
7. deterministic Python/Rust parity;
8. release build.

GitHub Actions is therefore the development/test feedback channel. ChatGPT reads workflow/job logs through the GitHub connector, updates `feat/rust-core-v01` when a failure has a clear safe source fix, and the self-hosted runner validates the next commit.

The former local autopilot/report control plane has been retired and removed. Issue #2 is archived and is no longer an automation channel.

CI must not:

- use GitHub-hosted compute for CrossAlpha qualification;
- run fork-controlled code on the CrossAlpha self-hosted machine;
- consume production credentials unnecessarily;
- use `/mnt/disk2/CrossAlphaData` as a CI data root;
- mutate production manifests or freezes;
- activate runtime bindings;
- perform production cutover or rollback;
- start production writers;
- decide Python production retirement.

Those operations remain explicit local promotion gates after the exact commit is CI-green.

## Raw Envelope V2

Historical V1 serialization depended on Python/provider object insertion order and cannot be made byte-identical for arbitrary nested JSON across languages. V1 remains immutable and readable.

Rust-native producers use schema V2:

- recursively lexicographically sorted object keys;
- array order preserved;
- UTC timestamps with six microseconds and `Z`;
- compact UTF-8 JSON;
- SHA256 and filename derived from those canonical bytes.

The cross-language gate is `scripts/verify_rust_raw_envelope_v2.py`.

## Migration phases

### R0 - Foundation

Status: source complete; earlier local gates passed.

### R1 - Storage and manifests

Status: source complete and previously proven against real data.

Earlier Python/Rust manifest rebuild parity completed with zero mismatches. Rust owns raw snapshot persistence, gzip/SHA, immutable audit, daily indexes and series state.

### R2 - Observatory

Status: production-native Rust path implemented and previously cut over successfully.

Rust owns Hyperliquid and DefiLlama collection, live/full health, supervision and storage. Standalone Observatory remains available only as a guarded rollback/compatibility path. Unified-daemon ownership blocks standalone installers/cutover/rollback from creating duplicate writers.

### R3 - Canonicalization and causal features

Status: source complete; final workspace/parity acceptance still required on the current head.

Implemented:

- Hyperliquid canonical parser;
- DefiLlama stablecoin canonical parser;
- Aave V3 canonical parser;
- Arrow/Parquet canonical writers;
- bounded recent-day raw manifest loader;
- bounded canonical materializer;
- Hyperliquid causal market-state features;
- stablecoin system/chain features;
- bounded raw-to-feature production materializer;
- `crossalpha-materialize-rs` with explicit production-write authorization.

Earlier canonical parser real-data parity passed with zero mismatches. The final acceptance reruns canonical Parquet/materializer/feature parity on the current source.

Feature Parquet preserves Python-compatible `timestamp[ns, UTC]` for `observed_at` and `known_at`; a native test locks this schema contract.

### R4 - State engine

Status: V02/V03/V04 native source complete; final compile/parity/live acceptance and runtime binding activation remain gated.

The shared `StateSpec` control plane provides:

```text
validate_config()
preflight()
freeze()
cycle()
integrity()
status()
```

Native implementations:

- V02: Aave market/liquidation evidence plus stablecoin/Hyperliquid descriptive state;
- V03: borrower census, adaptive Borrow-log acquisition, watchlist and borrower-risk evidence;
- V04: BTC/ETH × Binance/OKX/Bybit multi-venue mechanics.

`crossalpha-state-rs integrity` is a process-level fail-closed gate: exit status is non-zero unless `ok=true` and `cycle_enabled=true`.

V02/V03/V04 runtime bindings include the production CLI/daemon, storage contract and all state source modules. V02 additionally binds its transitive canonical/feature kernels. Source drift therefore invalidates the prior runtime binding.

New Rust prospective records bind the exact native runtime-binding file/record hash. V02/V03/V04 cycle enablement also depends on prospective-ledger integrity, so a broken historical evidence chain prevents additional writes.

### R5 - Research, Paper, A/B and Outcomes

Status: native source complete; final parity acceptance remains.

Implemented:

- point-in-time futures-definition normalization;
- previous-volume futures roll selection and same-contract MTM returns;
- Frozen B3 baseline/risk target kernels;
- zero-cost Free Core provider: Tiingo ETF proxies, Binance BTC/ETH, FRED DGS3MO;
- Free Core quality and derived return builder;
- Frozen B3 Paper snapshots/marks/runtime binding;
- State Shadow V0.1;
- State A/B snapshots/marks/runtime binding;
- Outcome Linkage anchors, horizons, metrics and runtime binding;
- descriptive market-routing research kernel.

Free Core deterministic parity covers provider parsing, six TradFi assets, BTC/ETH, FRED, canonical Parquet schema/rows, quality JSON, derived returns and strict-prior FRED CASH semantics.

State A/B runtime binding includes the transitive storage and feature kernels used by Shadow V0.1, so changes to those kernels invalidate old bindings.

### R6 - Unified daemon and operations

Status: source complete; production cutover remains acceptance-gated.

`crossalpha-daemon-rs` owns one Tokio runtime and supports:

- single-instance file lock;
- graceful SIGTERM/Ctrl-C shutdown;
- component failure counters and structured health JSON;
- Observatory cadence: 300 seconds;
- Materializer cadence: 900 seconds;
- State V02 cadence: 900 seconds;
- State V03 cadence: 900 seconds;
- State V04 cadence: 300 seconds;
- no automatic State freeze;
- State cycles only when runtime binding and prospective-ledger integrity are valid.

Standalone Observatory/Materializer/State installers and the legacy Observatory cutover/rollback refuse to run when `crossalpha-daemon.service` is active or enabled. This prevents writer ownership from silently splitting again after cutover.

Paper daily/weekly and Outcome Linkage remain separate Rust systemd timers because their schedules are calendar-oriented rather than daemon polling loops.

### R7 - Acceptance and Python retirement

Status: acceptance framework implemented; retirement has **not** yet been authorized on the current head.

The production-data acceptance runner is:

```bash
.venv/bin/python scripts/run_rust_migration_acceptance.py \
  --data-root /mnt/disk2/CrossAlphaData
```

Protocol: `CROSSALPHA_RUST_MIGRATION_ACCEPTANCE_V2`.

Repository-safe parts are exercised continuously by the self-hosted CI workflow. Final local acceptance additionally covers real production data, live preflights, tracked `Cargo.lock`, clean worktree, runtime binding integrity, installed systemd writer ownership and post-cutover soak.

The complete acceptance surface includes:

- `cargo fmt --check`;
- workspace `clippy -D warnings`;
- workspace tests;
- debug and release builds;
- Raw Envelope V2 byte parity;
- Free Core end-to-end parity;
- canonical/parser/Parquet/materializer/feature parity;
- research/Paper/A-B/Shadow parity;
- V02/V03/V04 deterministic parity and live preflights;
- Cargo.lock presence and git tracking;
- clean worktree;
- runtime binding integrity;
- production systemd writer audit;
- post-cutover soak.

Python retirement is allowed only when all eight production lines are native and healthy:

1. Observatory
2. Materializer
3. State V02
4. State V03
5. State V04
6. Frozen B3 Paper
7. State A/B
8. Outcome Linkage

The final machine-readable condition is:

```json
{"python_retirement_allowed": true}
```

Until that condition is produced after cutover and soak, Python source remains in the repository as reference/rollback evidence and must not be deleted.

## Safe promotion sequence

The development/test loop is automated by GitHub Actions on the dedicated self-hosted runner. Production promotion remains deliberately separate and explicit:

1. require the exact branch/PR commit to be CI-green;
2. sync that exact commit into `/mnt/disk2/CrossAlpha`;
3. run the full R7 acceptance against `/mnt/disk2/CrossAlphaData`;
4. generate and commit the real Cargo resolver `Cargo.lock` if the current workspace requires a new lockfile;
5. rerun R7 on a clean tracked commit and require `daemon_cutover_allowed=true`;
6. activate native runtime bindings with `scripts/bind_native_state_runtime.sh --activate`;
7. rerun State/Paper/A-B/Outcome integrity;
8. transfer Observatory/Materializer/State V02/V03/V04 writer ownership with `scripts/cutover_unified_rust_daemon.sh --activate`;
9. install/verify Rust Paper and Outcome timers;
10. confirm all legacy split writers are inactive and disabled;
11. complete the required production soak while monitoring daemon health;
12. rerun post-cutover acceptance;
13. retire Python production ownership only when `python_retirement_allowed=true`.

Rollback remains explicit through the guarded rollback scripts. CI never performs promotion or rollback automatically.

## Definition of done

CrossAlpha is Rust-first only when the accepted Rust binaries collect data, canonicalize, build features, run V02/V03/V04, operate Paper/A-B/Outcome ledgers, audit integrity and reproduce the accepted evidence without a Python production runtime, while the production writer audit proves there are no concurrent legacy writers.
