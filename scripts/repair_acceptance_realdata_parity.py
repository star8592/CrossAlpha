from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Owner-authored exact-head validation trigger after verified self-hosted parity repair.
# Full Acceptance V2 trigger after deterministic repair verification.


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    old_count = text.count(old)
    new_count = text.count(new)
    if old_count == 1:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"patched={label}")
        return
    if old_count == 0 and new_count == 1:
        print(f"already_patched={label}")
        return
    raise SystemExit(
        f"repair drift for {label}: old_count={old_count} new_count={new_count} path={path}"
    )


def main() -> int:
    parquet = ROOT / "crates/crossalpha-features/src/parquet.rs"
    market_state = ROOT / "crates/crossalpha-features/src/market_state.rs"
    stablecoin_state = ROOT / "crates/crossalpha-features/src/stablecoin_state.rs"
    freeze_verify = ROOT / "scripts/verify_rust_state_v03_freeze_parity.py"
    manifest_parity = ROOT / "crates/crossalpha-storage/src/parity.rs"

    replace_once(
        parquet,
        "use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, StringBuilder};",
        "use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, LargeStringBuilder};",
        "canonical-large-string-import",
    )

    text = parquet.read_text(encoding="utf-8")
    # Replace only a bare StringBuilder token. A plain str.replace would also match
    # the suffix inside LargeStringBuilder and produce LargeLargeStringBuilder.
    bare_builder_pattern = re.compile(r"(?<![A-Za-z0-9_])StringBuilder::new\(\)")
    old_builder_count = len(bare_builder_pattern.findall(text))
    old_type_count = text.count("DataType::Utf8")
    if old_builder_count:
        text = bare_builder_pattern.sub("LargeStringBuilder::new()", text)
    if old_type_count:
        text = text.replace("DataType::Utf8", "DataType::LargeUtf8")
    if "LargeLargeStringBuilder" in text:
        raise SystemExit("repair drift: invalid LargeLargeStringBuilder token detected")
    if old_builder_count == 0 and text.count("LargeStringBuilder::new()") < 2:
        raise SystemExit("repair drift for canonical LargeStringBuilder calls")
    if old_type_count == 0 and text.count("DataType::LargeUtf8") < 2:
        raise SystemExit("repair drift for canonical LargeUtf8 fields")
    parquet.write_text(text, encoding="utf-8")
    print(f"patched=canonical-large-strings builders={old_builder_count} fields={old_type_count}")

    replace_once(
        market_state,
        """fn duration_seconds(duration: chrono::Duration) -> f64 {
    duration
        .num_microseconds()
        .map(|value| value as f64 / 1_000_000.0)
        .unwrap_or_else(|| duration.num_milliseconds() as f64 / 1_000.0)
}""",
        """fn duration_seconds(duration: chrono::Duration) -> f64 {
    duration
        .num_nanoseconds()
        .map(|value| value as f64 / 1_000_000_000.0)
        .or_else(|| {
            duration
                .num_microseconds()
                .map(|value| value as f64 / 1_000_000.0)
        })
        .unwrap_or_else(|| duration.num_milliseconds() as f64 / 1_000.0)
}""",
        "feature-nanosecond-interval",
    )

    replace_once(
        market_state,
        """    if values.len() < ROLLING_MIN_PERIODS {
        return None;
    }
    let current = current?;
    let mean = values.iter().sum::<f64>() / values.len() as f64;
""",
        """    if values.len() < ROLLING_MIN_PERIODS {
        return None;
    }
    let current = current?;
    // pandas rolling.std(ddof=0) returns NaN for an exactly constant window.
    // Detect that contract before floating-point mean cancellation can manufacture
    // a tiny non-zero variance and a spurious z-score of +/-1.
    if values.windows(2).all(|pair| pair[0] == pair[1]) {
        return None;
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
""",
        "feature-constant-window-zscore",
    )

    replace_once(
        stablecoin_state,
        """fn sum_min_count_one(values: &[Option<f64>]) -> Option<f64> {
    known_sum(values.iter().copied())
}""",
        """fn sum_min_count_one(values: &[Option<f64>]) -> Option<f64> {
    // pandas groupby.sum() uses compensated accumulation for float64 groups.
    // Match that behavior so large stablecoin supplies retain the same tiny
    // accounting residuals instead of drifting by one or two ULPs.
    let mut count = 0_usize;
    let mut total = 0.0_f64;
    let mut compensation = 0.0_f64;
    for value in values.iter().copied().flatten() {
        count += 1;
        let y = value - compensation;
        let t = total + y;
        compensation = (t - total) - y;
        total = t;
    }
    (count > 0).then_some(total)
}""",
        "stablecoin-pandas-group-sum",
    )

    replace_once(
        stablecoin_state,
        """        let total_supply_native = part
            .iter()
            .map(|item| item.row.circulating_native.unwrap_or(0.0))
            .sum::<f64>();
        let chain_sum_native = part.iter().map(|item| item.chain_sum_native).sum::<f64>();
        let residual_native = chain_sum_native - total_supply_native;
""",
        """        // pandas Series.sum() delegates float64 reduction to NumPy, which uses
        // pairwise summation for sufficiently large contiguous vectors. Preserve the
        // frozen formula (chain total - asset total), but mirror that reduction order.
        let supply_values: Vec<f64> = part
            .iter()
            .map(|item| item.row.circulating_native.unwrap_or(0.0))
            .collect();
        let chain_values: Vec<f64> = part.iter().map(|item| item.chain_sum_native).collect();
        let total_supply_native = numpy_pairwise_sum(&supply_values);
        let chain_sum_native = numpy_pairwise_sum(&chain_values);
        let residual_native = chain_sum_native - total_supply_native;
""",
        "stablecoin-pandas-system-sum",
    )

    replace_once(
        stablecoin_state,
        """fn known_sum<I>(values: I) -> Option<f64>
where
    I: IntoIterator<Item = Option<f64>>,
{
""",
        """// Mirror NumPy's pairwise reduction shape closely enough for the frozen
// pandas Series.sum() contract. Small blocks remain left-to-right; larger blocks
// recursively split, reducing cancellation error without changing input order.
fn numpy_pairwise_sum(values: &[f64]) -> f64 {
    const BLOCK: usize = 128;
    if values.len() <= BLOCK {
        return values.iter().copied().sum();
    }
    let midpoint = values.len() / 2;
    numpy_pairwise_sum(&values[..midpoint]) + numpy_pairwise_sum(&values[midpoint..])
}

fn known_sum<I>(values: I) -> Option<f64>
where
    I: IntoIterator<Item = Option<f64>>,
{
""",
        "stablecoin-numpy-pairwise-helper",
    )

    replace_once(
        freeze_verify,
        """        python = freeze_state_v03(
            data_root,
            minimum_eligible_block=MINIMUM_BLOCK,
            now=FIXED_NOW,
        )
        if not verify_seal(python):
            raise RuntimeError("Python fixture freeze seal failed")
        expected = dict(python)
        expected.pop("status", None)
""",
        """        python = freeze_state_v03(
            data_root,
            minimum_eligible_block=MINIMUM_BLOCK,
            now=FIXED_NOW,
        )
        expected = dict(python)
        expected.pop("status", None)
        if not verify_seal(expected):
            raise RuntimeError("Python fixture freeze seal failed")
""",
        "state-v03-seal-wrapper",
    )

    replace_once(
        manifest_parity,
        """            mismatches.push(format!(
                \"daily content mismatch: {} python_records={} rust_records={} first_difference={}\",
                relative.display(),
                expected.len(),
                actual.len(),
                first_difference
                    .map(|index| (index + 1).to_string())
                    .unwrap_or_else(|| \"length-only\".to_string())
            ));
""",
        """            let detail = first_difference
                .map(|index| format!(\" python={:?} rust={:?}\", expected[index], actual[index]))
                .unwrap_or_default();
            mismatches.push(format!(
                \"daily content mismatch: {} python_records={} rust_records={} first_difference={}{}\",
                relative.display(),
                expected.len(),
                actual.len(),
                first_difference
                    .map(|index| (index + 1).to_string())
                    .unwrap_or_else(|| \"length-only\".to_string()),
                detail,
            ));
""",
        "manifest-daily-field-diagnostic",
    )

    replace_once(
        manifest_parity,
        """        if expected != actual {
            mismatches.push(format!(\"series content mismatch: {}\", relative.display()));
        }
""",
        """        if expected != actual {
            mismatches.push(format!(
                \"series content mismatch: {} python={} rust={}\",
                relative.display(),
                expected,
                actual,
            ));
        }
""",
        "manifest-series-field-diagnostic",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
