from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text()
    old_count = text.count(old)
    new_count = text.count(new)
    if old_count == 1:
        file.write_text(text.replace(old, new, 1))
        print(f"patched: {label}")
        return
    if old_count == 0 and new_count == 1:
        print(f"already patched: {label}")
        return
    raise SystemExit(
        f"{label}: expected exactly one old or one already-patched match in {path}; "
        f"old={old_count} new={new_count}"
    )


def original_timestamp_col() -> str:
    return '''fn timestamp_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = DateTime<Utc>>,\n{\n    fields.push(Field::new(\n        name,\n        DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into())),\n        true,\n    ));\n    let values = values\n        .map(|value| value.timestamp_micros().saturating_mul(1_000))\n        .collect::<Vec<_>>();\n    arrays.push(Arc::new(\n        TimestampNanosecondArray::from(values).with_timezone_utc(),\n    ));\n}\n'''


def free_core_timestamp_cols() -> str:
    return '''fn timestamp_us_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = DateTime<Utc>>,\n{\n    let data_type = DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into()));\n    fields.push(Field::new(name, data_type.clone(), true));\n    let values = values.map(|value| value.timestamp_micros()).collect::<Vec<_>>();\n    arrays.push(Arc::new(\n        TimestampMicrosecondArray::from(values).with_data_type(data_type),\n    ));\n}\n\nfn timestamp_ms_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = DateTime<Utc>>,\n{\n    let data_type = DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".into()));\n    fields.push(Field::new(name, data_type.clone(), true));\n    let values = values.map(|value| value.timestamp_millis()).collect::<Vec<_>>();\n    arrays.push(Arc::new(\n        TimestampMillisecondArray::from(values).with_data_type(data_type),\n    ));\n}\n'''


def returns_timestamp_col() -> str:
    return '''fn timestamp_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = DateTime<Utc>>,\n{\n    let data_type = DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into()));\n    fields.push(Field::new(name, data_type.clone(), true));\n    let values = values.map(|value| value.timestamp_micros()).collect::<Vec<_>>();\n    arrays.push(Arc::new(\n        TimestampMicrosecondArray::from(values).with_data_type(data_type),\n    ));\n}\n'''


def reference_string_col() -> str:
    return '''fn string_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = Option<String>>,\n{\n    fields.push(Field::new(name, DataType::LargeUtf8, true));\n    let mut builder = LargeStringBuilder::new();\n    for value in values {\n        builder.append_option(value.as_deref());\n    }\n    arrays.push(Arc::new(builder.finish()));\n}\n'''


def original_string_col() -> str:
    return '''fn string_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = Option<String>>,\n{\n    fields.push(Field::new(name, DataType::Utf8, true));\n    let mut builder = StringBuilder::new();\n    for value in values {\n        builder.append_option(value.as_deref());\n    }\n    arrays.push(Arc::new(builder.finish()));\n}\n'''


def main() -> None:
    # Feature parquet is an independent frozen contract: ns-resolution with
    # exact "UTC" Arrow timezone metadata.
    replace_once(
        "crates/crossalpha-features/src/feature_parquet.rs",
        "TimestampNanosecondArray::from(nanos).with_timezone_utc(),",
        "TimestampNanosecondArray::from(nanos).with_timezone(\"UTC\"),",
        "feature parquet exact UTC timezone metadata",
    )

    # Free Core canonical Parquet preserves the Python producer's source-level
    # timestamp precision: Tiingo/FRED -> us, Binance -> ms. Strings are
    # pandas/pyarrow large_string. Do not normalize these frozen schemas.
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        "use arrow_array::builder::{Float64Builder, Int64Builder, StringBuilder};",
        "use arrow_array::builder::{Float64Builder, Int64Builder, LargeStringBuilder};",
        "free-core large-string builder import",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        "use arrow_array::{ArrayRef, RecordBatch, TimestampNanosecondArray};",
        "use arrow_array::{ArrayRef, RecordBatch, TimestampMicrosecondArray, TimestampMillisecondArray};",
        "free-core source-precision timestamp imports",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        original_timestamp_col(),
        free_core_timestamp_cols(),
        "free-core source-specific timestamp helpers",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        original_string_col(),
        reference_string_col(),
        "free-core canonical large_string",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        '''fn write_tradfi_parquet(path: &Path, rows: &[ProxyDailyRow]) -> Result<()> {\n    let mut fields = Vec::new();\n    let mut arrays = Vec::<ArrayRef>::new();\n    timestamp_col(\n''',
        '''fn write_tradfi_parquet(path: &Path, rows: &[ProxyDailyRow]) -> Result<()> {\n    let mut fields = Vec::new();\n    let mut arrays = Vec::<ArrayRef>::new();\n    timestamp_us_col(\n''',
        "tradfi canonical timestamp[us, UTC]",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        '''fn write_crypto_parquet(path: &Path, rows: &[ProxyDailyRow]) -> Result<()> {\n    let mut fields = Vec::new();\n    let mut arrays = Vec::<ArrayRef>::new();\n    timestamp_col(\n''',
        '''fn write_crypto_parquet(path: &Path, rows: &[ProxyDailyRow]) -> Result<()> {\n    let mut fields = Vec::new();\n    let mut arrays = Vec::<ArrayRef>::new();\n    timestamp_ms_col(\n''',
        "crypto canonical timestamp[ms, UTC]",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        '''fn write_cash_parquet(path: &Path, rows: &[CashRateRow]) -> Result<()> {\n    let mut fields = Vec::new();\n    let mut arrays = Vec::<ArrayRef>::new();\n    timestamp_col(\n''',
        '''fn write_cash_parquet(path: &Path, rows: &[CashRateRow]) -> Result<()> {\n    let mut fields = Vec::new();\n    let mut arrays = Vec::<ArrayRef>::new();\n    timestamp_us_col(\n''',
        "cash canonical timestamp[us, UTC]",
    )

    # Derived returns are produced by pandas after combining source frames;
    # the frozen reference currently materializes them at us precision.
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        "use arrow_array::builder::{Float64Builder, StringBuilder};",
        "use arrow_array::builder::{Float64Builder, LargeStringBuilder};",
        "free-returns large-string builder import",
    )
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        "Array, ArrayRef, Float64Array, Int64Array, RecordBatch, StringArray, TimestampMicrosecondArray,",
        "Array, ArrayRef, Float64Array, Int64Array, LargeStringArray, RecordBatch, StringArray, TimestampMicrosecondArray,",
        "free-returns LargeStringArray reader import",
    )
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        original_timestamp_col(),
        returns_timestamp_col(),
        "free-returns canonical timestamp[us, UTC]",
    )
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        original_string_col(),
        reference_string_col(),
        "free-returns canonical large_string",
    )
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        '''    if let Some(values) = array.as_any().downcast_ref::<StringArray>() {\n        return Ok(Some(values.value(index).to_owned()));\n    }\n''',
        '''    if let Some(values) = array.as_any().downcast_ref::<StringArray>() {\n        return Ok(Some(values.value(index).to_owned()));\n    }\n    if let Some(values) = array.as_any().downcast_ref::<LargeStringArray>() {\n        return Ok(Some(values.value(index).to_owned()));\n    }\n''',
        "free-returns read large_string canonical columns",
    )

    replace_once(
        "crates/crossalpha-cli/src/bin/free_core_fixture.rs",
        '''    }\n\n    let mut binance_rows = Vec::new();\n''',
        '''    }\n    tiingo_rows.sort_by(|left, right| {\n        left.date\n            .cmp(&right.date)\n            .then(left.economic_asset.cmp(&right.economic_asset))\n    });\n\n    let mut binance_rows = Vec::new();\n''',
        "sort Tiingo fixture rows by date then asset",
    )
    replace_once(
        "crates/crossalpha-cli/src/bin/free_core_fixture.rs",
        '''    }\n\n    let fred = fixture.get("fred").context("fred fixture missing")?;\n''',
        '''    }\n    binance_rows.sort_by(|left, right| {\n        left.date\n            .cmp(&right.date)\n            .then(left.economic_asset.cmp(&right.economic_asset))\n    });\n\n    let fred = fixture.get("fred").context("fred fixture missing")?;\n''',
        "sort Binance fixture rows by date then asset",
    )
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        "let gap = (row.date - previous).num_seconds() as f64 / 86_400.0;",
        "let gap = row.date.signed_duration_since(previous).num_seconds() as f64 / 86_400.0;",
        "make free-core audit chrono duration inference explicit",
    )


if __name__ == "__main__":
    main()
