# CrossAlpha State Engine V0.2

Protocol: `CROSSALPHA_STATE_V0_2`

Mode: `PROSPECTIVE_DESCRIPTIVE_SHADOW`

Maturity: `O0_DATA_TO_O1_DESCRIPTION`

## Purpose

State V0.2 extends the capital-state observatory without changing any frozen V0.1
allocation or prospective A/B experiment. It is deliberately descriptive. The output
contains `risk_multiplier = null`; it cannot trade, de-risk, increase risk, alter
relative Frozen B3 weights, or mutate the State V0.1 A/B ledger.

The research question is narrower than "can these metrics improve returns?":

> Can free, point-in-time capital-state observations identify distinct leverage,
> liquidity, migration and connectivity states strongly enough to justify a separately
> preregistered O2 risk-modifier experiment later?

## Frozen references

V0.2 records the immutable file hashes of:

- Frozen B3 Paper V0.1 freeze;
- prospective State A/B V0.1 freeze;
- State V0.2 implementation;
- State V0.2 prospective writer;
- Aave collector;
- Aave canonicalizer;
- `config/state_v02.yaml`.

If a record-producing V0.2 implementation/config hash changes after the V0.2 freeze,
the prospective writer refuses to continue. A new protocol version is required.

V0.2 never rewrites or replaces the V0.1 freeze files.

## Data cost

Required data cost remains exactly `$0`.

### Required

- Existing Hyperliquid public market-state collector.
- Existing DefiLlama stablecoin collector.
- Aave V3 public GraphQL API, Ethereum chain ID 1, preregistered Ethereum Core Pool:
  `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2`.

The Aave collector fails closed if the API does not return exactly that Core market.
Ethereum Prime/EtherFi/specialized markets are not mixed into V0.2.

### Optional

If `EVM_RPC_URL` is configured, V0.2 also reads a bounded overlapping window of Aave
V3 `LiquidationCall` logs from Ethereum. This is an O0 event-confirmation stream only.
An RPC failure cannot fail the required Aave market collection and cannot affect V0.1.

## Components

### 1. Aave market stress

Focus reserves:

`WETH, ETH, WBTC, USDC, USDT, GHO, DAI`

For every focus reserve, V0.2 computes descriptive pressure from:

- borrow APY relative to the frozen 20% full-stress reference;
- low available USD liquidity relative to the frozen $10m reference;
- borrow-cap-reached / frozen / paused flags.

Reserve pressure is the maximum of available terms. The Aave market component is the
maximum focus-reserve pressure. This is intentionally a conservative market-level
stress descriptor, not a borrower liquidation probability.

The 24-hour change in summed focus-reserve available liquidity is retained as a
separate descriptor and does not alter the current Aave pressure score.

### 2. Borrower health-factor distribution

Current status:

`REQUIRES_AUDITABLE_BORROWER_UNIVERSE`

Aave health factor is a user-level object. V0.2 refuses to replace it with market
utilization, borrow APY, or available liquidity. Until CrossAlpha has a point-in-time,
auditable borrower universe, this component is invalid/unknown and contributes no
pressure.

The liquidation threshold remains `HF < 1`, but V0.2 does not claim to know the
market-wide mass near that threshold.

### 3. Aave liquidation activity

Optional Ethereum RPC scans overlap by 512 blocks. Canonical events preserve:

- source block timestamp as `event_time`;
- collector `observed_at` and `known_at` separately;
- transaction hash and log index;
- collateral/debt/user addresses;
- raw debt-to-cover and collateral amounts;
- reorg `removed` flag.

Overlapping scans are deduplicated by transaction hash + log index at the feature
layer. Counts over 24h/7d are factual O0 descriptors only; they are not used as a
predictive liquidation-cliff pressure in V0.2.

### 4. Stablecoin issuance/redemption vs chain migration

V0.2 uses point-in-time system and chain stablecoin accounting with strict coverage
and residual gates.

For the approximately seven-day matched window:

`net_system_change = total_latest - total_lag`

This is the issuance/redemption proxy.

For chain changes:

`migration_proxy = min(sum(positive_chain_changes), abs(sum(negative_chain_changes)))`

This is an offsetting chain-migration proxy.

**Migration is not direct stress.** Only negative system-wide net change contributes
to the stablecoin liquidity-pressure term. The preregistered full-stress contraction
reference is -2% over the matched lookback. Migration intensity is retained for
analysis using a 10% normalization reference but cannot itself create a stress signal.

### 5. Hyperliquid basis dispersion

V0.2 compares the existing point-in-time 24h basis z-scores for BTC and ETH:

`dispersion = abs(BTC_basis_z_24h - ETH_basis_z_24h)`

The frozen descriptive normalization is 3 z-score points.

Scope is explicitly `CROSS_ASSET_HYPERLIQUID_ONLY`. V0.2 is not allowed to describe
this as multi-venue basis dispersion until an independently collected venue set exists.

### 6. Stablecoin connectivity / contagion graph

Rows are chains, columns are stablecoin IDs, and edge values are stablecoin market
value. V0.2 filters very small nodes and computes pairwise cosine overlap in chain
stablecoin composition. Chain-pair overlaps are weighted by the geometric mean of
chain stablecoin market values.

The resulting 0-1 connectivity descriptor measures potential common-stablecoin
transmission channels. It is **not causal proof of contagion**.

### 7. Deployment / collateral-activation descriptor

V0.2 records whether system stablecoin liquidity is expanding while Aave focus-reserve
available liquidity is falling. This is a coincident deployment proxy only. It makes no
causal claim and does not change the composite stress score.

A future version may refine this into chain-specific collateral activation after a
clean chain-to-protocol flow mapping is preregistered.

## Composite descriptive score

Frozen available-component weights:

- Aave market stress: 30%
- stablecoin system-contraction stress: 30%
- Hyperliquid BTC/ETH basis dispersion: 20%
- stablecoin chain-composition connectivity: 20%

Missing components are not imputed. Available weights are renormalized.

Confidence:

- `FULL`: all 4 pressure components valid;
- `PARTIAL`: at least 2 valid;
- `INSUFFICIENT`: fewer than 2 valid.

The result is named `descriptive_stress_score`, never `signal`, `position`, or
`multiplier`.

## Prospective ledger

Before live observation, V0.2 is frozen. Every accepted observation must:

- be generated after the freeze;
- be written within 10 minutes of its generated time;
- reference the immutable V0.2 freeze hash;
- reference the exact derived-state file and SHA-256;
- remain descriptive-only with `risk_multiplier = null`;
- preserve V0.1 reference hashes.

The target cadence is 15 minutes. Missing cycles remain visible as gaps. Unlike a
trading ledger, a missed observational cycle does not lock all future observations;
it simply cannot be retrospectively manufactured.

## O2 eligibility gate

V0.2 can never automatically become an actionable modifier.

Data volume is only sufficient to *design* a separate O2 protocol after all of:

- at least 180 prospective calendar days;
- at least 500 immutable observations;
- at least 5 distinct stress episodes;
- stress-episode threshold fixed at 0.67;
- 24h episode cooldown;
- ledger integrity passes.

Even then, status is at most:

`ELIGIBLE_FOR_O2_PROTOCOL_DESIGN`

An O2 experiment additionally requires:

1. a preregistered outcome-linkage test;
2. a preregistered actionable rule;
3. a new immutable protocol version frozen before its evaluation period.

Historical reconstruction can never satisfy those requirements.

## Isolation guarantee

State V0.2 runs from a separate 15-minute systemd timer. Its API/RPC/materialization
failure cannot stop:

- the V0.1 Observatory collector;
- the V0.1 materializer;
- Frozen B3 daily marks;
- Frozen B3 weekly snapshots;
- State A/B V0.1 prospective marks.

The final milestone audit verifies the V0.1 A and A/B freeze files remain byte-identical
before and after V0.2 installation.
