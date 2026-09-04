# CrossAlpha architecture

## Mission

Reconstruct the state of global capital, then allocate risk accordingly.

## Three engines

1. **Core Engine** — long-history cross-asset trend/momentum/risk research.
2. **State Engine / Observatory** — point-in-time liquidity, leverage, collateral, flow and contagion state.
3. **Market Engine** — instrument/venue/funding/basis/liquidity translation and routing (V0.2+).

## Non-negotiable research rules

- Economic asset != research instrument != tradable proxy != venue instrument.
- Raw observations are append-only and immutable.
- `event_time`, `observed_at`, and `known_at` are distinct concepts.
- Facts and inferences live in separate layers.
- Onchain transfer != economic capital flow until conservation/semantic reconciliation passes.
- Continuous futures prices are never naively `pct_change()` across roll boundaries for PnL.
- A new model may not rescue a failed simpler hypothesis without a new preregistered research question.
