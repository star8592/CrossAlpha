# Raw Observation Envelope V2

## Decision

CrossAlpha preserves every historical raw `ObservationEnvelope` V1 snapshot exactly as written.
V1 files, hashes, filenames, audit-manifest rows, daily indexes, and downstream readers are never rewritten by the Rust migration.

New Rust-native raw producers use `schema_version = 2`.

The reason for V2 is byte determinism. Python V1 hashes `ObservationEnvelope.model_dump_json()` bytes. Nested dictionaries preserve the provider JSON insertion order. Rust `serde_json::Value` does not provide a cross-language guarantee that the same semantic object will retain the same insertion order. Therefore arbitrary Python V1 and Rust V1 writers cannot honestly promise identical bytes or SHA-256 values.

## V2 canonical-byte contract

The uncompressed bytes hashed by `RawSnapshotStore` are compact UTF-8 JSON with:

1. `schema_version = 2`.
2. Every JSON object, including the envelope object, `payload`, `metadata`, and nested objects, recursively ordered by Unicode key lexicographic order.
3. JSON arrays preserving their original element order.
4. UTC envelope datetimes serialized as RFC3339 with exactly six fractional-second digits and a trailing `Z`.
5. No insignificant whitespace.
6. Normal JSON scalar semantics; non-finite JSON numbers are not valid input.

The SHA-256 in `RawSnapshotManifest.sha256` and the first 12 hash characters in the raw filename are computed from exactly these uncompressed canonical bytes.

## Compatibility

- V1: historical/read compatibility only. Existing V1 evidence remains immutable.
- V2: canonical writer contract for new Rust-native raw observations.
- Readers deserialize both versions through the same envelope model and must not reject V2 merely because the writer contract changed.
- A new incompatible byte contract requires a new schema version. Do not silently change V2 serialization.

## Producers

The Rust-native producers that write through `RawSnapshotStore` must use `RAW_ENVELOPE_CANONICAL_SCHEMA_VERSION` rather than a literal `1`:

- Observatory Hyperliquid and DefiLlama collectors.
- State V0.2 Aave raw observations.
- State V0.4 multi-venue raw observations.

Any future raw producer must do the same unless it deliberately introduces and documents a later schema version.

## Gates

`crossalpha-storage` includes a unit test proving that different object insertion orders produce identical V2 bytes.

`scripts/verify_rust_raw_envelope_v2.py` independently implements the V2 canonical JSON rule in Python and compares its exact UTF-8 bytes against `crossalpha-raw-envelope-fixture-rs` for two insertion-order variants. This gate is required by the R7 migration acceptance suite.

State V0.2/V0.3/V0.4 runtime bindings include the storage crate in their native source hash graph. A V2 storage implementation change therefore invalidates an old State binding and requires an explicit re-bind after acceptance.

## Non-goals

V2 does not alter historical V1 SHA values, reconstruct old provider wire-order JSON, rebuild production manifests, or authorize Python retirement by itself. Full retirement still requires the complete R7 build/parity/live/runtime/soak gates.
