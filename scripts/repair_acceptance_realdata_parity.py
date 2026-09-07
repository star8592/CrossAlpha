from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Owner-authored exact-head validation trigger after verified self-hosted parity repair.


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
    freeze_verify = ROOT / "scripts/verify_rust_state_v03_freeze_parity.py"

    replace_once(
        parquet,
        "use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, StringBuilder};",
        "use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, LargeStringBuilder};",
        "canonical-large-string-import",
    )

    text = parquet.read_text(encoding="utf-8")
    old_builder_count = text.count("StringBuilder::new()")
    old_type_count = text.count("DataType::Utf8")
    if old_builder_count:
        text = text.replace("StringBuilder::new()", "LargeStringBuilder::new()")
    if old_type_count:
        text = text.replace("DataType::Utf8", "DataType::LargeUtf8")
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
        .or_else(|| duration.num_microseconds().map(|value| value as f64 / 1_000_000.0))
        .unwrap_or_else(|| duration.num_milliseconds() as f64 / 1_000.0)
}""",
        "feature-nanosecond-interval",
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

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
