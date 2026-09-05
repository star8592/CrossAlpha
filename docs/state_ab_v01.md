# CrossAlpha State A/B V0.1

## Purpose

`CROSSALPHA_STATE_AB_V0_1` is a genuinely prospective experiment. It compares:

- **A — Frozen B3**: the immutable `CROSSALPHA_FREE_V0_1_PAPER` allocation.
- **B — Frozen B3 + State Shadow**: the exact same risky-asset proportions, uniformly scaled by the contemporaneous State Shadow multiplier and with released notional moved to `CASH`.

The experiment exists to answer one question only:

> Can independently observed capital-state stress reduce downside risk without changing the Core alpha thesis or sacrificing too much long-run return?

It is not a new alpha strategy and it is not allowed to select assets, add leverage, short, or increase risk above A.

## Frozen multipliers

| State | Multiplier | Effect |
|---|---:|---|
| NORMAL | 1.00 | No change |
| MODERATE | 0.75 | Scale every risky A weight by 0.75 |
| SEVERE | 0.50 | Scale every risky A weight by 0.50 |
| Unknown / insufficient data | 1.00 | No modifier |

No other multiplier is valid in V0.1.

## Prospective timing

The A/B protocol must be frozen **before** the first eligible Frozen B3 Monday. For the current V0.1 freeze, the first eligible date is inherited from A.

On each Monday UTC the system executes in this order:

1. Refresh the point-in-time free Core range.
2. Seal the immutable A snapshot.
3. Compute the contemporaneous State Shadow using only already-known Observatory data.
4. Seal an immutable State decision.
5. Seal the B snapshot, referencing the A snapshot hash and State decision hash.
6. Mark the preceding day for A and B where applicable.

A persistent systemd timer is never allowed to manufacture a missed historical Monday snapshot. If a prospective decision is missed, the gap is evidence.

## Identical return data

B never downloads or derives a second market-return dataset.

Every B daily mark references the immutable same-date A mark by SHA-256 and copies A's sealed `asset_returns` exactly. B then applies its own already-sealed weights and its own turnover cost.

Therefore:

`A/B return difference = weight difference + corresponding turnover cost difference`

and cannot be caused by vendor refresh timing, revised prices, or different market calendars.

## Immutable hash graph

A B snapshot must contain links to:

- A/B protocol freeze SHA-256
- same-date Frozen B3 A snapshot SHA-256
- same-date State decision SHA-256

A B daily mark must contain links to:

- A/B protocol freeze SHA-256
- same-date immutable A mark SHA-256
- active A snapshot SHA-256
- active B snapshot SHA-256
- active State decision SHA-256

All State A/B decisions, snapshots, and marks are sealed JSON records. Existing records are never overwritten.

## No-backfill policy

`NO_RETROSPECTIVE_AB_BACKFILL` is a hard rule.

A daily B mark may create only the immediately preceding UTC day. If an earlier day is missing, V0.1 preserves the gap and refuses repair from later vendor data.

If A changes its weekly snapshot but the same-date B snapshot is absent, B refuses to carry the old State-adjusted weights forward.

## Strict bidirectional audit

The authoritative audit proves both directions:

- every eligible A snapshot has exactly one B snapshot;
- every B snapshot links to the exact A snapshot;
- every State decision has exactly one B snapshot;
- after B starts, every A mark has exactly one B mark;
- every B mark links to the exact A mark;
- B `asset_returns` exactly equal A `asset_returns`;
- each risky B weight equals `A weight × frozen multiplier`;
- B cash is exactly the released risk notional;
- B never has greater risky gross exposure than A;
- B mark dates are contiguous once the experiment begins.

The auditor may become stricter operationally, but it may never repair or rewrite the prospective ledger.

## Promotion gate

State Shadow begins as `O1_DESCRIPTION_TO_O2_SHADOW`.

Promotion to `O2_PROSPECTIVE_RISK_MODIFIER_SUPPORTED` requires all of:

- at least 365 comparable prospective days;
- at least 20 intervention days (`multiplier < 1`);
- B maximum drawdown not worse than A;
- B downside volatility not worse than A;
- cumulative-return sacrifice versus A no greater than 5 percentage points;
- strict ledger integrity true.

Historical reconstruction cannot satisfy this gate.

## Storage

```text
$CROSSALPHA_DATA_DIR/research/free_v01/state_ab_v01/
  freeze.json
  decisions/year=YYYY/month=MM/effective_date=YYYY-MM-DD.json
  snapshots/year=YYYY/month=MM/effective_date=YYYY-MM-DD.json
  marks/year=YYYY/month=MM/date=YYYY-MM-DD.json
```

Once marks exist, DuckDB exposes:

- `core.frozen_b3_paper_marks`
- `state_engine.shadow_ab_marks`

## Operations

The existing Frozen B3 paper timers also run B; no additional timers are required:

- Tue–Sun 04:00 UTC: refresh and seal A mark, then seal B mark.
- Monday: seal A snapshot, State decision, B snapshot, then preceding-day marks.

Core/Paper remains the benchmark and State remains a separate shadow layer. State failures must never rewrite Frozen B3 history or parameters.
