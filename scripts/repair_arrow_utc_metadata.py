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
        "crates/crossalpha-features/src/feature_parquet.rs",
        "TimestampNanosecondArray::from(nanos).with_timezone_utc(),",
        "TimestampNanosecondArray::from(nanos).with_timezone(\"UTC\"),",
        "feature parquet canonical UTC timezone metadata",
    )
    replace_once(
        "crates/crossalpha-data/src/free_core.rs",
        "TimestampNanosecondArray::from(values).with_timezone_utc(),",
        "TimestampNanosecondArray::from(values).with_timezone(\"UTC\"),",
        "free-core canonical UTC timezone metadata",
    )


if __name__ == "__main__":
    main()
