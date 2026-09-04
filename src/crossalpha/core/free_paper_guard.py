from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from crossalpha.core.free_paper import (
    HISTORICAL_START,
    PAPER_PROTOCOL,
    _load_freeze,
    _load_marks,
    _load_snapshots,
    _parse_date,
    mark_paper_forward as _legacy_mark_paper_forward,
)


def _dates_are_contiguous(values: list[date]) -> bool:
    if len(values) < 2:
        return True
    return all(right - left == timedelta(days=1) for left, right in zip(values, values[1:]))


def paper_integrity_report(data_root: Path) -> dict[str, Any]:
    """Audit the immutable prospective paper ledger without modifying it.

    A missing prospective day is evidence and must remain missing.  This report
    therefore treats gaps as an integrity failure; it never repairs or backfills
    them from later vendor data.
    """
    try:
        freeze = _load_freeze(data_root)
    except FileNotFoundError:
        return {
            "paper_protocol": PAPER_PROTOCOL,
            "frozen": False,
            "ok": False,
            "error": "paper protocol is not frozen",
        }

    snapshots = _load_snapshots(data_root)
    marks = _load_marks(data_root)
    first_eligible = _parse_date(freeze["first_eligible_effective_date"])

    snapshot_dates = [_parse_date(row["effective_date"]) for row in snapshots]
    mark_dates = [_parse_date(row["date"]) for row in marks]

    snapshot_unique = len(snapshot_dates) == len(set(snapshot_dates))
    snapshot_mondays = all(value.weekday() == 0 for value in snapshot_dates)
    snapshot_after_freeze = all(value >= first_eligible for value in snapshot_dates)
    marks_unique = len(mark_dates) == len(set(mark_dates))
    mark_contiguous = _dates_are_contiguous(mark_dates)

    snapshot_hash_by_date = {
        _parse_date(row["effective_date"]): row["record_sha256"] for row in snapshots
    }
    mark_snapshot_links_ok = True
    marks_not_before_snapshot = True
    for row in marks:
        mark_date = _parse_date(row["date"])
        active = _parse_date(row["active_snapshot_effective_date"])
        if active > mark_date:
            marks_not_before_snapshot = False
        expected_hash = snapshot_hash_by_date.get(active)
        if expected_hash is None or expected_hash != row.get("active_snapshot_record_sha256"):
            mark_snapshot_links_ok = False

    if marks and snapshots:
        marks_not_before_snapshot = (
            marks_not_before_snapshot and min(mark_dates) >= min(snapshot_dates)
        )
    elif marks and not snapshots:
        marks_not_before_snapshot = False
        mark_snapshot_links_ok = False

    missing_mark_dates: list[str] = []
    if len(mark_dates) >= 2:
        expected = mark_dates[0]
        actual = set(mark_dates)
        while expected <= mark_dates[-1]:
            if expected not in actual:
                missing_mark_dates.append(expected.isoformat())
                if len(missing_mark_dates) >= 50:
                    break
            expected += timedelta(days=1)

    checks = {
        "snapshot_dates_unique": snapshot_unique,
        "snapshots_are_mondays": snapshot_mondays,
        "snapshots_not_before_first_eligible": snapshot_after_freeze,
        "mark_dates_unique": marks_unique,
        "mark_dates_contiguous": mark_contiguous,
        "marks_not_before_first_snapshot": marks_not_before_snapshot,
        "mark_snapshot_hash_links": mark_snapshot_links_ok,
    }
    return {
        "paper_protocol": PAPER_PROTOCOL,
        "frozen": True,
        "ok": all(checks.values()),
        "first_eligible_effective_date": first_eligible.isoformat(),
        "snapshot_count": len(snapshots),
        "mark_count": len(marks),
        "first_mark_date": mark_dates[0].isoformat() if mark_dates else None,
        "last_mark_date": mark_dates[-1].isoformat() if mark_dates else None,
        "missing_mark_dates": missing_mark_dates,
        "checks": checks,
        "policy": "NO_RETROSPECTIVE_MARK_BACKFILL",
    }


def strict_mark_paper_forward(
    data_root: Path,
    *,
    end: str | date,
    research_start: str = HISTORICAL_START,
) -> dict[str, Any]:
    """Create at most the immediately preceding UTC day's mark.

    The historical implementation iterates every unmarked day.  That is useful
    for backtests but unacceptable for a prospective ledger because a missed day
    could later be reconstructed with revised vendor data.  This guard refuses
    such retrospective repair.  A detected gap locks V0.1 and requires an
    explicit new prospective protocol version.
    """
    _load_freeze(data_root)
    end_date = _parse_date(end)
    target = end_date - timedelta(days=1)
    snapshots = _load_snapshots(data_root)
    if not snapshots:
        return {
            "paper_protocol": PAPER_PROTOCOL,
            "status": "no_snapshots",
            "created_marks": 0,
            "skipped_existing_marks": 0,
            "end_exclusive": end_date.isoformat(),
        }

    snapshot_dates = sorted(_parse_date(row["effective_date"]) for row in snapshots)
    first_effective = snapshot_dates[0]
    if target < first_effective:
        return {
            "paper_protocol": PAPER_PROTOCOL,
            "status": "before_first_snapshot",
            "created_marks": 0,
            "skipped_existing_marks": 0,
            "end_exclusive": end_date.isoformat(),
            "target_mark_date": target.isoformat(),
        }

    marks = _load_marks(data_root)
    mark_dates = sorted(_parse_date(row["date"]) for row in marks)
    if target in set(mark_dates):
        return {
            "paper_protocol": PAPER_PROTOCOL,
            "status": "already_marked",
            "created_marks": 0,
            "skipped_existing_marks": 1,
            "end_exclusive": end_date.isoformat(),
            "target_mark_date": target.isoformat(),
        }

    if mark_dates:
        expected = mark_dates[-1] + timedelta(days=1)
        if target != expected:
            raise RuntimeError(
                "PAPER_LEDGER_GAP: refusing retrospective backfill. "
                f"last immutable mark={mark_dates[-1].isoformat()}, "
                f"next required={expected.isoformat()}, requested target={target.isoformat()}. "
                "Preserve the gap and start a new prospective protocol version."
            )
    elif target != first_effective:
        raise RuntimeError(
            "PAPER_LEDGER_GAP: first prospective mark was missed; refusing backfill. "
            f"first snapshot={first_effective.isoformat()}, requested target={target.isoformat()}. "
            "Preserve the gap and start a new prospective protocol version."
        )

    result = _legacy_mark_paper_forward(
        data_root,
        end=end_date,
        research_start=research_start,
    )
    if int(result.get("created_marks", 0)) != 1:
        raise RuntimeError(
            "strict prospective mark expected exactly one new immutable day; "
            f"got {result.get('created_marks')!r}"
        )
    return {**result, "retrospective_backfill_allowed": False}
