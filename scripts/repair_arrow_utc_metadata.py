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


def canonical_timestamp_col() -> str:
    return '''fn timestamp_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = DateTime<Utc>>,\n{\n    let data_type = DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));\n    fields.push(Field::new(name, data_type.clone(), true));\n    let values = values\n        .map(|value| value.timestamp_micros().saturating_mul(1_000))\n        .collect::<Vec<_>>();\n    arrays.push(Arc::new(\n        TimestampNanosecondArray::from(values).with_data_type(data_type),\n    ));\n}\n'''


def original_timestamp_col() -> str:
    return '''fn timestamp_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)\nwhere\n    I: Iterator<Item = DateTime<Utc>>,\n{\n    fields.push(Field::new(\n        name,\n        DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into())),\n        true,\n    ));\n    let values = values\n        .map(|value| value.timestamp_micros().saturating_mul(1_000))\n        .collect::<Vec<_>>();\n    arrays.push(Arc::new(\n        TimestampNanosecondArray::from(values).with_timezone_utc(),\n    ));\n}\n'''


def main() -> None:
    replace_once(
        "crates/crossalpha-features/src/feature_parquet.rs",
        "TimestampNanosecondArray::from(nanos).with_timezone_utc(),",
        "TimestampNanosecondArray::from(nanos).with_timezone(\"UTC\"),",
        "feature parquet canonical UTC timezone metadata",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        original_timestamp_col(),
        canonical_timestamp_col(),
        "free-core canonical field/array shared UTC datatype",
    )
    replace_once(
        "crates/crossalpha-data/src/free_returns.rs",
        original_timestamp_col(),
        canonical_timestamp_col(),
        "free-core derived returns field/array shared UTC datatype",
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


if __name__ == "__main__":
    main()
