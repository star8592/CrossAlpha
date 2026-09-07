from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match in {path}, got {count}")
    file.write_text(text.replace(old, new, 1))
    print(f"patched: {label}")


def main() -> None:
    replace_once(
        "crates/crossalpha-state/src/v02.rs",
        "use anyhow::{Context, Result, bail};\n",
        "use anyhow::{Result, bail};\n",
        "remove unused v02 Context import",
    )
    replace_once(
        "crates/crossalpha-state/src/v03/census.rs",
        "use std::collections::{BTreeMap, HashSet};\n",
        "use std::collections::HashSet;\n",
        "remove unused v03 census BTreeMap import",
    )
    replace_once(
        "crates/crossalpha-state/src/v03/cycle.rs",
        "    FULL_CENSUS_CADENCE_MINUTES, MAX_BOOTSTRAP_CHUNKS_PER_CYCLE, PROTOCOL,\n",
        "    FULL_CENSUS_CADENCE_MINUTES, MAX_BOOTSTRAP_CHUNKS_PER_CYCLE,\n",
        "remove unused v03 cycle PROTOCOL import",
    )
    replace_once(
        "crates/crossalpha-state/src/v03/prospective.rs",
        "use std::collections::BTreeMap;\n",
        "",
        "remove unused v03 prospective BTreeMap import",
    )
    replace_once(
        "crates/crossalpha-state/src/v04.rs",
        "use std::collections::{BTreeMap, BTreeSet};\n",
        "use std::collections::BTreeMap;\n",
        "remove unused v04 BTreeSet import",
    )
    replace_once(
        "crates/crossalpha-state/src/v04/cycle.rs",
        "use crate::v04::{ACTIONABILITY, MAXIMUM_SNAPSHOT_AGE_SECONDS, PROTOCOL, compute_market_mechanics};\n",
        "use crate::v04::{ACTIONABILITY, MAXIMUM_SNAPSHOT_AGE_SECONDS, compute_market_mechanics};\n",
        "remove unused v04 cycle PROTOCOL import",
    )
    replace_once(
        "crates/crossalpha-state/src/v04/cycle.rs",
        "use crate::v04_provider::{MultiVenueCollector, VenuePayload, parse_venue_snapshot};\n",
        "use crate::v04_provider::{MultiVenueCollector, parse_venue_snapshot};\n",
        "remove unused v04 cycle VenuePayload import",
    )
    replace_once(
        "crates/crossalpha-state/src/v03/network.rs",
        "                if end.saturating_sub(start) + 1 <= minimum_span.max(1) {\n",
        "                if end.saturating_sub(start) < minimum_span.max(1) {\n",
        "apply Rust 1.98 int_plus_one equivalent",
    )
    replace_once(
        "crates/crossalpha-state/src/v04/provider.rs",
        "use serde_json::{Value, json};\n",
        "use serde_json::Value;\n",
        "remove test-only json macro from production import",
    )
    replace_once(
        "crates/crossalpha-state/src/v04/provider.rs",
        "    use chrono::TimeZone;\n\n    #[test]\n",
        "    use chrono::TimeZone;\n    use serde_json::json;\n\n    #[test]\n",
        "import json macro only in v04 provider tests",
    )

    provider = Path("crates/crossalpha-state/src/v04/provider.rs")
    text = provider.read_text()
    replacements = (
        (
            "            let source_times = [\n                payload.premium.get(\"time\").cloned(),\n",
            "            let source_times: Vec<Value> = [\n                payload.premium.get(\"time\").cloned(),\n",
            "annotate Binance source_times",
        ),
        (
            "            let source_times = [\n                spot.get(\"ts\").cloned(),\n",
            "            let source_times: Vec<Value> = [\n                spot.get(\"ts\").cloned(),\n",
            "annotate OKX source_times",
        ),
        (
            "            let source_times = [spot_time, perp_time].into_iter().flatten().collect();\n",
            "            let source_times: Vec<Value> =\n                [spot_time, perp_time].into_iter().flatten().collect();\n",
            "annotate Bybit source_times",
        ),
    )
    for old, new, label in replacements:
        count = text.count(old)
        if count != 1:
            raise SystemExit(f"{label}: expected exactly one match, got {count}")
        text = text.replace(old, new, 1)
        print(f"patched: {label}")
    provider.write_text(text)


if __name__ == "__main__":
    main()
