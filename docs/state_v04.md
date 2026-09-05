# CrossAlpha State Engine V0.4 — Multi-Venue Market Mechanics

## Purpose

State V0.4 measures BTC/ETH market mechanics across Binance, OKX and Bybit using only public, unauthenticated, zero-cost endpoints. It is descriptive evidence only. It does not change Frozen B3, State V0.1, A/B V0.1, State V0.2 or State V0.3.

Protocol: `CROSSALPHA_STATE_V0_4`
Prospective ledger: `CROSSALPHA_STATE_V0_4_PROSPECTIVE`
Mode: `PROSPECTIVE_MULTI_VENUE_MARKET_MECHANICS_SHADOW`
Actionability: `DESCRIPTIVE_ONLY`
Risk multiplier: `null`

## Universe and cadence

Assets: BTC and ETH. Venues: Binance, OKX and Bybit. Quotes are USDT. The service targets one observation every five minutes. A venue row older than 90 seconds is excluded from mechanics calculations.

Three valid venues for an asset is `FULL`; two is `PARTIAL`; fewer than two is `INSUFFICIENT` and the whole cycle is rejected.

The fault-isolated collector always preserves all six asset×venue slots. A failed public API call is retained as a raw snapshot carrying `collection_error` and produces null mechanics for that slot. Failure of one venue therefore does not silently erase the slot or force a false full-confidence record.

## Comparable funding semantics

Funding dispersion compares like with like. V0.4 does not compare a settled Binance funding rate with a predicted/current OKX or Bybit rate.

Frozen definition:

`LATEST_SETTLED_NORMALIZED_TO_8H`

For each venue, the two latest funding settlements are fetched. The newest settled/realized rate is used and the actual settlement interval is inferred from the difference between the two settlement timestamps. The rate is then normalized to an eight-hour equivalent.

- Binance: `/fapi/v1/fundingRate`, fields `fundingRate` and `fundingTime`.
- OKX: `/api/v5/public/funding-rate-history`, fields `realizedRate` and `fundingTime`.
- Bybit: `/v5/market/funding/history`, fields `fundingRate` and `fundingRateTimestamp`.

If fewer than two valid settlements exist, the interval is unknown and that venue is excluded from funding dispersion. V0.4 never assumes an eight-hour interval merely because eight hours is common.

## Other normalized mechanics

Spot/perpetual basis uses the same venue on both legs:

`basis_bps = (perp_mid / spot_mid - 1) * 10000`

Bid/ask spread uses:

`spread_bps = (ask - bid) / mid * 10000`

Open interest is normalized to USD notional:

- Binance: base OI × perp mid.
- OKX: `oiUsd`.
- Bybit: `openInterestValue`.

Per asset V0.4 records:

- cross-venue spot price range,
- basis median/range/std,
- settled funding 8h median/range,
- perpetual spread median/max,
- total OI USD,
- OI concentration HHI.

There is deliberately no composite stress score in V0.4.

## Point-in-time discipline

Each normalized venue row carries `observed_at` and `known_at`. Only rows satisfying `observed_at <= known_at <= generated_at` can enter a prospective observation. A prospective write must occur within three minutes of `generated_at`; otherwise it is rejected as retrospective.

## Three-layer evidence hash graph

Every prospective observation links:

1. six append-only compressed raw exchange snapshots,
2. six normalized venue rows in Parquet,
3. the mechanics vector JSON.

Raw evidence stores two different hashes because CrossAlpha's raw manifest SHA covers the uncompressed envelope payload while the persisted file is gzip-compressed:

- `raw_sha256`: uncompressed payload SHA-256,
- `raw_compressed_file_sha256`: actual `.json.gz` bytes SHA-256.

The strict auditor verifies both and then recomputes the full mechanics vector from the venue Parquet.

## Fault-isolation hash guard

The collector that defines failed-venue behavior is itself guarded. Its SHA-256 is preregistered in `config/state_v04.yaml`, and every cycle runs the strict config/implementation check before collecting. Changing failure semantics therefore invalidates the frozen protocol rather than silently changing live evidence.

## Prospective gate

V0.4 can become eligible only to design a separate O2 protocol after at least:

- 180 prospective calendar days,
- 500 observations,
- 95% valid venue-slot share,
- completed outcome linkage,
- a separately preregistered O2 rule.

Eligibility never automatically makes V0.4 actionable.
