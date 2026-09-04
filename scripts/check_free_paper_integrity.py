from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from crossalpha.core.free_paper import _verify_sealed
from crossalpha.settings import Settings


def main() -> None:
    settings = Settings()
    root = settings.crossalpha_data_dir / "research" / "free_v01" / "paper" / "marks"
    if not root.exists():
        print(json.dumps({"ok": True, "mark_count": 0, "missing_dates": []}))
        return

    records: list[tuple[date, Path]] = []
    for path in sorted(root.glob("year=*/month=*/date=*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not _verify_sealed(payload):
            raise SystemExit(f"PAPER LEDGER INTEGRITY FAILED: invalid record hash: {path}")
        records.append((date.fromisoformat(payload["date"]), path))

    if not records:
        print(json.dumps({"ok": True, "mark_count": 0, "missing_dates": []}))
        return

    dates = [item[0] for item in records]
    if len(dates) != len(set(dates)):
        raise SystemExit("PAPER LEDGER INTEGRITY FAILED: duplicate mark dates")

    expected: list[date] = []
    cursor = min(dates)
    last = max(dates)
    while cursor <= last:
        expected.append(cursor)
        cursor += timedelta(days=1)
    missing = sorted(set(expected) - set(dates))
    if missing:
        rendered = ",".join(value.isoformat() for value in missing[:20])
        raise SystemExit(
            "PAPER LEDGER INTEGRITY FAILED: historical mark gaps cannot be backfilled: "
            + rendered
        )

    print(
        json.dumps(
            {
                "ok": True,
                "mark_count": len(records),
                "first_date": min(dates).isoformat(),
                "last_date": max(dates).isoformat(),
                "missing_dates": [],
            }
        )
    )


if __name__ == "__main__":
    main()
