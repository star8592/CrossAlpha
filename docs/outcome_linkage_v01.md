# CrossAlpha Outcome Linkage V0.1

## Purpose

Outcome Linkage is the final data-engineering layer between frozen prospective state observations and realized A/B outcomes. It does not create a trading signal, choose thresholds, fit a model or claim alpha.

Protocol: `CROSSALPHA_OUTCOME_LINKAGE_V0_1`
Mode: `PROSPECTIVE_STATE_TO_REALIZED_OUTCOME_LINKAGE`
Actionability: `NONE`
Risk multiplier: `null`

Sources are the already-frozen prospective ledgers for State V0.2, V0.3 and V0.4. Outcomes are the immutable daily marks of A = Frozen B3 and B = Frozen B3 × State Shadow V0.1.

## Why daily anchors

V0.2 and V0.4 can produce many observations in one day. Treating every high-frequency row as an independent research sample while attaching the same following seven-day return would create severe pseudo-replication.

The primary sampling unit is therefore one observation per state source per UTC day. The deterministic anchor is the latest `known_at` observation for that source on that UTC day. Raw high-frequency evidence remains intact; it is only the inference/linkage sampling unit that is daily.

## Future-only outcome window

No outcome from the anchor's UTC day is allowed. Each horizon starts on the next complete UTC day.

Frozen horizons are 1, 3, 7, 14 and 28 days. For an anchor known on date D, horizon H requires every immutable A and B daily mark for D+1 through D+H.

If even one date is missing, that horizon remains pending. A six-day set can never be labeled a seven-day outcome.

## Outcome facts

Each matured horizon records:

- A cumulative net return,
- B cumulative net return,
- B minus A cumulative return,
- cash cumulative return,
- A and B max drawdown,
- A and B worst daily return,
- A and B negative-day count,
- State intervention-day count,
- average B multiplier.

Max drawdown is computed from starting equity 1.0, so a loss on the first outcome day is not hidden.

## Hash graph and known-at ordering

Each link seals:

- source state record path, file SHA and record SHA,
- exact source feature values,
- exact A daily mark paths and record hashes,
- exact B daily mark paths and record hashes,
- B→A same-date mark hashes,
- materialization time,
- latest outcome-mark known time,
- recomputed outcome metrics.

A link can be created only after both A and B marks for every horizon date exist and their `known_at` timestamps are no later than the materialization time.

The strict auditor independently reselects the daily source anchor, recomputes every outcome metric, checks every mark seal/link, and verifies `materialized_at >= source_known_at` and `materialized_at >= every A/B mark known_at`.

## Deterministic late materialization is not backfill

The state observation and A/B outcome marks are immutable before linkage. Therefore a missed Outcome Linkage service run may deterministically create a matured link later without changing either side of the evidence pair. This is allowed and is distinct from retrospectively manufacturing a state observation.

Selective linking is forbidden. Once a registered horizon has every required A/B mark, strict integrity requires its link to exist. Deleting or withholding an inconvenient matured result makes the ledger fail integrity.

## Statistical guardrails

Outcome Linkage is a data construction engine, not an inference engine. In particular:

- high-frequency state rows are not treated as independent samples,
- daily anchors are the primary sampling unit,
- 3/7/14/28-day windows overlap and therefore require dependence-aware inference later,
- naive p-values over overlapping link rows are forbidden,
- this layer does not establish predictiveness or alpha.

A future O2 hypothesis must be preregistered separately before evaluating predictive performance.
