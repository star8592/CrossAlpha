# CrossAlpha Rust Migration

## Objective

Move CrossAlpha from a Python-first research/runtime stack to a Rust-first production architecture without invalidating existing research artifacts, point-in-time semantics, manifests, Parquet files, YAML configs, or CLI contracts.

The migration is intentionally incremental. Python remains the reference implementation until parity tests prove a Rust component equivalent.

## Non-negotiable invariants

1. Point-in-time correctness must not change.
2. Raw Observatory snapshots and audit manifests remain immutable.
3. Existing YAML configuration remains readable during migration.
4. Parquet/JSONL output schemas are versioned; Rust must not silently rewrite old artifacts.
5. Research results must pass fixture/golden parity tests before Python implementations are retired.
6. Required V0.1 market-data cost remains USD 0.
7. GitHub is source/version control; research/backtests/large computation remain local-first.

## Target workspace

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
  crossalpha-py/
```

## Migration phases

### R0 - Foundation

Status: complete.

### R1 - Storage and manifests

Status: complete and production-compatible.

Real-data Python/Rust rebuild parity passed with zero mismatches. Raw snapshot, SHA256, gzip, immutable audit, daily index and series-state contracts are owned by Rust.

### R2 - Observatory collectors

Status: complete and production-native Rust.

The production `crossalpha-observatory.service` now runs the release Rust binary and retains the Python unit/script as an explicit rollback path. Hyperliquid and DefiLlama provider dry-run, shadow-write, full-health and live-health parity gates all passed before cutover.

### R3 - Canonicalization and causal features

Status: in progress.

R3.1 canonical parser parity is complete:

- Hyperliquid `metaAndAssetCtxs`: 233 real rows matched Python.
- DefiLlama stablecoin assets: 423 real rows matched Python.
- DefiLlama stablecoin chain supply: 1637 real rows matched Python.
- full row-level semantic parity: `mismatches=0`.

R3.2 is the isolated Parquet compatibility gate:

- Rust uses native Arrow `RecordBatch` + Parquet `ArrowWriter`.
- output is restricted to an explicit preview directory; production `canonical/` is not touched.
- compare column order, Arrow field types/nullability, row counts, null behavior and all values against pandas/pyarrow output from the exact same frozen real snapshots.
- byte-for-byte file identity is intentionally not required because writer metadata/compression may differ without changing the data contract.

Exit gate for R3.2:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build -p crossalpha-cli
.venv/bin/python scripts/verify_rust_canonical_parquet_parity.py \
  --data-root /mnt/disk2/CrossAlphaData
```

After Parquet parity passes, enable bounded recent-day canonical materialization, then port causal Hyperliquid and stablecoin features.

Critical rule: rolling features are causal and must preserve null/min-observation behavior exactly. No future leakage can be introduced during vectorization.

### R4 - State engine

Replace the Python state package with a versioned Rust engine:

```text
StateSpec trait
  validate_config()
  preflight()
  freeze()
  cycle()
  integrity()
  status()
```

V02/V03/V04 become implementations sharing common capability probes, artifact metadata, freeze semantics and integrity machinery instead of duplicating command orchestration.

This phase should eliminate nested Python `asyncio.run()`/event-loop ownership from state commands. Async ownership belongs to one Tokio runtime at the CLI/service boundary.

### R5 - Research engine

Port performance-critical research kernels only after storage/state parity is stable.

### R6 - Unified daemon and operations

Converge production entrypoints on the native binary with graceful shutdown, single-instance protection, health JSON, structured logs and explicit exit codes.

### R7 - Python retirement

Remove a Python subsystem only when golden fixtures match, historical replay passes, result deltas are accepted, operational soak passes and rollback is documented.

## Recommended Rust stack

- async/runtime: `tokio`
- HTTP: `reqwest`
- serialization: `serde`, `serde_json`, `serde_yaml`
- errors: `thiserror`, `anyhow` at app boundary
- CLI: `clap`
- logging: `tracing`, `tracing-subscriber`
- time: `chrono`
- Arrow/Parquet: Apache Arrow Rust + `parquet`
- hashing: `sha2` where compatibility requires SHA256
- tests: native unit/integration + golden fixture comparisons
- Python bridge: `pyo3`/`maturin`, optional and isolated

## Architecture rules

- Domain crates do not depend on network/storage implementations.
- Config structs are versioned and validated at the boundary.
- Collectors emit typed observations; storage owns persistence and artifact atomicity.
- Feature/state engines are pure or side-effect-minimized and fixture-testable.
- CLI/service layer owns Tokio runtime and cancellation.
- No silent schema coercion or future leakage.
- No global mutable singleton state.

## Immediate implementation order

1. R3.2 Parquet schema/value parity.
2. bounded recent-day canonical materializer.
3. Hyperliquid basis/spread/OI and 24h causal rolling features.
4. stablecoin state features.
5. V03 capability-probed preflight/freeze.
6. V04 state engine and shared state trait.
7. free Core adapters and research kernels.

## Definition of done

CrossAlpha is Rust-first when the native binary can collect, canonicalize, build features, run state cycles, run research, audit integrity and produce the same accepted artifacts without requiring a Python runtime. Python then becomes optional analysis/notebook tooling rather than the production engine.
