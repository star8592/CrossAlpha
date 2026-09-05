# CrossAlpha State Engine V0.3 — Aave Borrower Health & Liquidation Cliff

## Purpose

State V0.3 closes a deliberate gap left by State V0.2: market-level lending
liquidity is not the same object as borrower solvency. V0.3 therefore builds an
auditable Aave V3 Ethereum Core borrower universe and measures current borrower
health at one common finalized Ethereum block.

V0.3 is **descriptive only**. It does not change Frozen B3, State V0.1, the
prospective A/B V0.1 ledger, or State V0.2. It has no risk multiplier and no
automatic promotion path into trading logic.

## Frozen identity

- Protocol: `CROSSALPHA_STATE_V0_3`
- Prospective ledger: `CROSSALPHA_STATE_V0_3_PROSPECTIVE`
- Mode: `PROSPECTIVE_BORROWER_RISK_SHADOW`
- Maturity: O0 data -> O1 description
- Actionability: `DESCRIPTIVE_ONLY`
- Risk multiplier: `null`
- Required data cost: `$0`

## Data source

The primary object is the Aave V3 Ethereum Core Pool:

`0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2`

The borrower candidate universe starts at the Core Pool deployment block
`16291127` and is reconstructed from the Pool `Borrow` event. The debt identity
is `Borrow.onBehalfOf`, not transaction sender and not necessarily the address
receiving borrowed funds.

Current account state is read with `Pool.getUserAccountData(address)` at one
common finalized block tag. CrossAlpha decodes:

- total collateral in base currency,
- total debt in base currency,
- available borrows,
- current liquidation threshold,
- LTV,
- health factor.

Ethereum Core base-currency monetary values are normalized using 8 decimals and
health factor using 18 decimals. A zero-debt `uint256.max` health factor is kept
as unknown/non-finite for distribution purposes rather than coerced into a
normal borrower value.

## RPC policy

`EVM_RPC_URL` is preferred when configured. If it is absent, V0.3 may use the
zero-cost public fallback:

`https://ethereum-rpc.publicnode.com`

No RPC source is accepted merely because it responds to `eth_blockNumber`.
Before V0.3 can freeze, the live preflight must prove:

1. current block access,
2. historical `eth_getLogs` access near the Aave V3 Core deployment block,
3. recent `Borrow` log access,
4. fixed-block `eth_call` compatibility for `getUserAccountData`.

If any of those fail, V0.3 does not freeze.

## Borrower universe bootstrap

Historical reconstruction is operational bootstrap, **not prospective research
evidence**.

Frozen bootstrap rules:

- start block: `16291127`,
- chunk size: 25,000 blocks,
- maximum chunks per 15-minute cycle: 8,
- recursive split floor after RPC range errors: 256 blocks,
- finalized-head lag: 64 blocks,
- historical candidate addresses are monotonic and never deleted.

Retaining historical candidates is intentional. If a short-lived/reorged borrow
causes a false-positive candidate, the next current account census will report
zero current debt. Current active borrower status is determined by
`totalDebtBase > 0`, not by historical event presence.

Until the scan reaches the current finalized head, the system state is
`FROZEN_BORROWER_UNIVERSE_BOOTSTRAPPING`. No full-market census claim is allowed.

## Full borrower census

After bootstrap catches up, every full census queries every candidate address at
one common finalized block.

A full census is valid only when:

- borrower bootstrap is complete,
- candidate universe is non-empty,
- failed account-call ratio is <= 1%,
- each address appears at most once.

The full-census cadence is six hours. A failed/partial census is preserved as an
operational artifact but is not admitted to the prospective evidence ledger and
does not advance the last-valid-census timestamp.

## Measured facts

V0.3 records facts rather than tuning a new stress score:

- active borrower count,
- total active debt,
- total active collateral,
- debt-weighted HF p10 / p25 / p50,
- borrower count and debt under HF thresholds 1.00, 1.02, 1.05, 1.10, 1.20, 1.50,
- debt share under each threshold,
- liquidation-cliff debt bands,
- liquidatable debt share (HF <= 1.00),
- critical debt share (HF <= 1.05),
- near-cliff debt share (HF <= 1.20).

This is a **current-position health distribution**. It is not a single-asset
liquidation-price simulator. Aave eMode, cross-collateral structure, oracle
prices and weighted liquidation thresholds are preserved through the Pool's own
`getUserAccountData` result rather than replaced by a simplistic price formula.

## Watchlist layer

A valid full census creates a fast-refresh watchlist containing addresses with:

- HF <= 1.50, or
- debt >= $1,000,000.

The watchlist is refreshed on the 15-minute service cadence. It reports only the
state of those previously selected addresses. It explicitly sets
`full_market_census_claim_allowed=false` and cannot replace the six-hour full
census or estimate whole-market debt shares.

## Prospective anti-backfill rules

At freeze time V0.3 seals both:

- UTC freeze timestamp,
- current minimum eligible finalized Ethereum block.

A full census may enter the prospective ledger only when:

- its `captured_at` is not before freeze,
- its `known_at` is not before `captured_at`,
- its block is not below the frozen minimum eligible block,
- bootstrap is complete,
- full-census coverage gate passes,
- V0.1, A/B V0.1 and V0.2 reference freeze hashes are unchanged,
- V0.3 implementation/config hashes are unchanged.

Therefore a researcher cannot freeze V0.3 and later calculate an old Ethereum
block to fabricate prospective history.

Records are keyed by finalized block. The same block cannot later be relabeled
with another summary/detail artifact (`STATE_V03_BLOCK_COLLISION`).

## Hash graph

A prospective record links:

`V0.3 freeze -> census summary JSON -> account-detail parquet`

The strict auditor independently verifies:

- freeze and record seals,
- predecessor and implementation hashes,
- record filename/block identity,
- temporal ordering,
- minimum block floor,
- summary/detail file hashes,
- summary protocol and full-census semantics,
- linked key metrics,
- unique borrower addresses,
- candidate count,
- RPC coverage recomputed from detail rows,
- active debt recomputed from detail rows.

The audit level is
`STRICT_NON_MUTATING_HASH_GRAPH_WITH_ARTIFACT_RECOMPUTE`.

## Research maturity gate

V0.3 can never automatically become a trading modifier.

It may only become eligible for a separately preregistered O2 protocol after all
of the following are true:

- >= 180 genuine prospective calendar days,
- >= 120 valid full censuses,
- >= 5 distinct cliff-stress episodes,
- borrower bootstrap remains complete,
- ledger integrity remains valid,
- an outcome-linkage test is completed,
- a separate O2 decision rule is preregistered before performance evaluation.

The provisional cliff-episode descriptor is critical debt share
(HF <= 1.05) >= 5%, with a 24-hour episode cooldown. This threshold is a frozen
descriptive event definition, not a trading threshold.

## What V0.3 does not claim

V0.3 does not claim that:

- HF predicts price direction,
- HF <= 1.05 will necessarily liquidate,
- all collateral can be liquidated at oracle value,
- liquidation volume equals the debt currently near the cliff,
- one token price shock can be inferred without full account collateral/debt
  composition,
- the watchlist represents the entire Aave market,
- the observed cliff distribution is alpha.

Those are separate hypotheses requiring separate, preregistered tests.

## Operational states

Healthy states include:

- `FROZEN_BORROWER_UNIVERSE_BOOTSTRAPPING`
- `FROZEN_AWAITING_FIRST_VALID_FULL_CENSUS`
- `O1_PROSPECTIVE_BORROWER_EVIDENCE_ACCUMULATING`
- `ELIGIBLE_FOR_O2_PROTOCOL_DESIGN`

The final state means only that enough prospective evidence exists to design a
new O2 protocol. It does not mean State V0.3 is actionable.
