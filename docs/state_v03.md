# CrossAlpha State Engine V0.3 — Aave Borrower Health & Liquidation Cliff

## Purpose

State V0.3 measures borrower solvency rather than market-level lending liquidity. It builds an auditable Aave V3 Ethereum Core borrower universe and measures all eligible accounts at one common finalized Ethereum block.

V0.3 is **descriptive only**. It does not modify Frozen B3, State V0.1, A/B V0.1, or State V0.2. It has no risk multiplier and no automatic path into trading logic.

## Frozen identity

- Protocol: `CROSSALPHA_STATE_V0_3`
- Prospective ledger: `CROSSALPHA_STATE_V0_3_PROSPECTIVE`
- Mode: `PROSPECTIVE_BORROWER_RISK_SHADOW`
- Maturity: O0 data -> O1 description
- Actionability: `DESCRIPTIVE_ONLY`
- Risk multiplier: `null`
- Required data cost: `$0`
- Historical bootstrap is **not** prospective evidence.

## Aave object

Ethereum Core Pool:

`0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2`

Borrower-universe bootstrap begins at block `16291127`. Debt identity is `Borrow.onBehalfOf`.

Current account state is read with `Pool.getUserAccountData(address)` at one common finalized block tag. CrossAlpha decodes collateral, debt, available borrows, liquidation threshold, LTV, and health factor. Ethereum Core base-currency values use 8 decimals; health factor uses 18 decimals. A zero-debt `uint256.max` health factor remains unknown/non-finite for distribution purposes.

The finalized block timestamp is independently obtained with `eth_getBlockByNumber(finalized_block)` and stored as `block_time`.

## RPC capability policy

V0.3 requires more than a node that can answer `eth_blockNumber`. It needs historical logs and fixed-block state because the borrower universe starts at the 2023 Aave V3 Core deployment block.

Candidate order is frozen as:

1. `EVM_RPC_URL`, when explicitly configured by the operator;
2. `https://ethereum-rpc.blockreq.com/v1/rpc/public`;
3. `https://ethereum-rpc.publicnode.com`;
4. `https://eth.llamarpc.com`.

The public candidates are zero-cost fallbacks. A configured RPC is preferred, but it is **not** a single point of failure: if it cannot satisfy the required capability probes, V0.3 may select the next zero-cost candidate.

Before freeze, a candidate must prove all of these capabilities:

1. `eth_blockNumber`;
2. `eth_getBlockByNumber` for the finalized block;
3. recent `eth_getLogs` for Aave `Borrow`;
4. historical `eth_getLogs` near deployment block `16291127`;
5. fixed-block `eth_call` for `getUserAccountData`.

The first candidate passing the complete probe is selected. Failure diagnostics persist only the safe source label and exception class; configured URLs, tokens, and exception messages are never serialized.

The regular V0.3 cycle also performs capability selection **before any state/raw/parquet write**. One selected RPC is then used for that whole cycle. CrossAlpha does not switch RPC providers after a cycle has begun writing evidence. If the selected endpoint later fails mid-cycle, the cycle fails and retries on a future scheduled run rather than mixing providers inside one census.

The preflight implementation itself is included in the V0.3 frozen implementation hash.

## Borrower universe bootstrap

Frozen rules:

- start block: `16291127`;
- chunk size: 25,000 blocks;
- maximum chunks per 15-minute cycle: 8;
- recursive split floor after RPC range errors: 256 blocks;
- finalized-head lag: 64 blocks;
- historical candidate addresses are monotonic and never deleted.

Historical reconstruction is operational bootstrap only. Until the scan reaches the current finalized head, the healthy state is `FROZEN_BORROWER_UNIVERSE_BOOTSTRAPPING`; no full-market census claim is allowed.

A historical/reorg false-positive candidate is harmless: current active borrower status is defined by current `totalDebtBase > 0`, not merely historical event presence.

## Full census and watchlist

After bootstrap catches up, a full census queries every candidate at the same finalized block.

A full census is valid only when:

- bootstrap is complete;
- candidate universe is non-empty;
- failed account-call ratio is <= 1%;
- each address appears at most once.

A new full census requires both:

- at least six hours since the previous valid full census; and
- a finalized block strictly greater than the previous full-census block.

A failed/partial census remains an operational artifact but does not enter the prospective evidence ledger or advance the last-valid-census marker.

The 15-minute watchlist includes addresses with HF <= 1.50 or debt >= $1,000,000. A borrower newly observed between full censuses is temporarily added to the watchlist until the next valid full census. Watchlist output never claims full-market coverage.

## Measured facts

V0.3 records facts rather than a tuned stress score:

- active borrower count;
- total active debt;
- debt-weighted HF p10 / p25 / p50;
- borrower/debt amounts below HF 1.00 / 1.02 / 1.05 / 1.10 / 1.20 / 1.50;
- debt share under each threshold;
- liquidation-cliff debt bands;
- liquidatable debt share (HF <= 1.00);
- critical debt share (HF <= 1.05);
- near-cliff debt share (HF <= 1.20).

These are current-position health distributions, not single-token liquidation-price forecasts.

## Point-in-time model

V0.3 keeps three clocks:

- `block_time`: finalized chain state time;
- `captured_at`: account sampling time;
- `known_at`: time the prospective record was sealed.

Every admitted record must satisfy:

`block_time <= captured_at <= known_at`

At freeze, CrossAlpha also seals the current minimum eligible finalized block. A later calculation for an older block cannot be inserted as prospective evidence.

## Prospective anti-backfill and hash graph

A prospective full census requires:

- block >= frozen minimum eligible block;
- `captured_at` not before freeze;
- `known_at` not before `captured_at`;
- `block_time` not after `captured_at`;
- complete borrower bootstrap;
- valid full-census coverage;
- unchanged V0.1 / A-B V0.1 / V0.2 reference freeze hashes;
- unchanged V0.3 implementation/config hashes.

Records are keyed by finalized block. The same block cannot later be relabeled with different artifacts (`STATE_V03_BLOCK_COLLISION`).

The frozen implementation set includes the borrower metrics, RPC reader, RPC capability preflight, cycle, watchlist, prospective writer, config checker, and YAML config.

The strict auditor verifies record/freeze seals, implementation/reference hashes, block identity and ordering, PTI clocks, artifact hashes, unique addresses, coverage, active debt, cliff debt shares, watchlist membership, and debt-weighted HF quantiles. Key metrics are independently recomputed from account-detail parquet rather than trusted from the summary JSON alone.

Audit level:

`STRICT_HASH_GRAPH_EVENT_TIME_AND_DETAIL_RECOMPUTE`

## Research maturity gate

V0.3 can never automatically become a trading modifier. Eligibility to design a separate preregistered O2 protocol requires all of:

- >= 180 genuine prospective calendar days;
- >= 120 valid full censuses;
- >= 5 distinct cliff-stress episodes;
- complete borrower bootstrap;
- valid ledger integrity;
- completed outcome-linkage test;
- a separately preregistered O2 decision rule before performance evaluation.

The provisional descriptive episode definition is HF <= 1.05 debt share >= 5%, with a 24-hour cooldown. It is not a trading threshold.

## Healthy operational states

Healthy states include:

- `FROZEN_BORROWER_UNIVERSE_BOOTSTRAPPING`;
- `FROZEN_AWAITING_FIRST_VALID_FULL_CENSUS`;
- `O1_PROSPECTIVE_BORROWER_EVIDENCE_ACCUMULATING`;
- `ELIGIBLE_FOR_O2_PROTOCOL_DESIGN`.

The final state only means there is enough prospective evidence to design a new protocol. It does not mean V0.3 is actionable.