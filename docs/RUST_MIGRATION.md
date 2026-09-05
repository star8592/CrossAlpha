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
  crossalpha-domain/       # identifiers, timestamps, observations, states, schema types
  crossalpha-config/       # typed configuration + validation
  crossalpha-storage/      # atomic files, JSONL manifests, Parquet, schema/version contracts
  crossalpha-data/         # HTTP clients, retry/rate-limit, collectors/adapters
  crossalpha-observatory/  # immutable snapshots, canonicalization, health/indexes
  crossalpha-features/     # causal rolling features and transformations
  crossalpha-state/        # state v02/v03/v04 engines, freeze/preflight/integrity
  crossalpha-outcomes/     # outcome materialization/linkage
  crossalpha-research/     # trend/momentum/risk/backtest research engine
  crossalpha-market/       # future venue/instrument/funding/basis execution-facing domain
  crossalpha-cli/          # unified CLI/control plane
  crossalpha-py/           # optional PyO3 bindings only where notebooks need Rust kernels
```

## Migration phases

### R0 - Foundation

Status: started on `feat/rust-core-v01`.

- Cargo workspace.
- Shared domain types.
- Typed YAML loader.
- Rust CLI control plane.
- Preserve Python package and command surface.

Exit gate:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo run -p crossalpha-cli -- doctor
cargo run -p crossalpha-cli -- config-check config/state_v03.yaml
```

### R1 - Storage and manifests

Rust becomes owner of:

- atomic writes/rename/fsync policy;
- content hashing;
- immutable JSONL append;
- daily/series manifest indexes;
- Parquet schema contracts;
- local data-path resolution.

Why first: every later subsystem depends on deterministic storage and this removes Python I/O overhead while preserving research semantics.

Parity gate: generate indexes from the same immutable manifest and byte/schema-compare canonical outputs where deterministic.

### R2 - Observatory collectors

Port public/free collectors to Tokio + reqwest:

- Binance public market data;
- Hyperliquid snapshots;
- DefiLlama stablecoins;
- FRED/Tiingo free research adapters;
- generic EVM RPC collector scaffold.

Required runtime primitives:

- bounded concurrency;
- per-source rate limiter;
- exponential retry with jitter;
- request timeout and cancellation;
- structured tracing;
- deterministic source timestamps and ingest timestamps.

Python collectors remain callable until source-by-source parity is accepted.

### R3 - Canonicalization and causal features

Move dataframe-heavy transforms to Polars Rust / Arrow kernels where appropriate.

Critical rule: rolling features are causal and must preserve null/min-observation behavior exactly. No future leakage can be introduced during vectorization.

First candidates:

- Hyperliquid basis/spread/OI transforms;
- 24h causal rolling z-scores;
- stablecoin stock/chain accounting;
- canonical daily price tables.

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

Port performance-critical research kernels only after storage/state parity is stable:

- returns/alignment;
- trend and relative momentum;
- volatility/risk allocation;
- walk-forward evaluation;
- bootstrap/resampling;
- turnover/cost accounting;
- outcome linkage.

Python notebooks may call Rust through PyO3, but production/reproducible runs use the native binary.

### R6 - Unified daemon and operations

Replace Python/systemd entrypoints with one binary supporting subcommands and daemon modes:

```text
crossalpha observatory run
crossalpha observatory health
crossalpha state preflight --version v03
crossalpha state freeze --version v03
crossalpha research run --config ...
crossalpha doctor
```

Add graceful shutdown, lock files/single-instance protection, health JSON, structured logs and explicit exit codes.

### R7 - Python retirement

Remove a Python subsystem only when:

1. golden fixtures match;
2. full local historical replay passes;
3. result deltas are explained and accepted;
4. operational soak test passes;
5. rollback path is documented.

Keep only notebook/report glue that demonstrably benefits from Python.

## Recommended Rust stack

- async/runtime: `tokio`
- HTTP: `reqwest`
- serialization: `serde`, `serde_json`, `serde_yaml`
- errors: `thiserror`, `anyhow` at app boundary
- CLI: `clap`
- logging: `tracing`, `tracing-subscriber`
- time: `chrono` initially; consider `time` only if needed
- Arrow/Parquet/DataFrame: `polars`, `arrow`, `parquet`
- hashing: `blake3` or `sha2` according to existing manifest compatibility
- tests: native unit/integration + golden fixture comparisons
- Python bridge: `pyo3`/`maturin`, optional and isolated

## Architecture rules

- Domain crates do not depend on network/storage implementations.
- Config structs are versioned and validated at the boundary.
- Collectors emit typed observations; they do not write arbitrary files directly.
- Storage owns persistence and artifact atomicity.
- Feature/state engines are pure or side-effect-minimized and testable with fixtures.
- CLI/service layer owns Tokio runtime and cancellation.
- No `unwrap()`/`expect()` in production data paths unless an invariant is statically guaranteed and documented.
- No silent schema coercion.
- No global mutable singleton state.

## Immediate implementation order

1. `crossalpha-storage` + manifest compatibility fixtures.
2. Observatory health/index rebuild in Rust.
3. Hyperliquid collector + canonicalizer.
4. V03 capability-probed preflight/freeze, because it is currently an operational pain point.
5. V04 state engine and shared state trait.
6. Free Core data adapters and canonical price pipeline.
7. Research kernels and PyO3 only where notebook interoperability is valuable.

## Definition of done

CrossAlpha is considered Rust-first when the native binary can collect, canonicalize, build features, run state cycles, run research, audit integrity and produce the same accepted artifacts without requiring a Python runtime. Python then becomes optional analysis/notebook tooling rather than the production engine.
