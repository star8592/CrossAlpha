from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from crossalpha.core import free_paper_guard


def _snapshot(day: str, sha: str = "snap") -> dict[str, object]:
    return {
        "effective_date": day,
        "record_sha256": sha,
        "weights": {},
    }


def _mark(day: str, active: str = "2026-09-07", sha: str = "snap") -> dict[str, object]:
    return {
        "date": day,
        "active_snapshot_effective_date": active,
        "active_snapshot_record_sha256": sha,
        "record_sha256": "mark",
    }


def test_strict_mark_returns_without_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(free_paper_guard, "_load_freeze", lambda root: {"ok": True})
    monkeypatch.setattr(free_paper_guard, "_load_snapshots", lambda root: [])
    monkeypatch.setattr(free_paper_guard, "_load_marks", lambda root: [])

    result = free_paper_guard.strict_mark_paper_forward(tmp_path, end="2026-09-08")
    assert result["status"] == "no_snapshots"
    assert result["created_marks"] == 0


def test_strict_mark_refuses_missed_first_prospective_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(free_paper_guard, "_load_freeze", lambda root: {"ok": True})
    monkeypatch.setattr(
        free_paper_guard,
        "_load_snapshots",
        lambda root: [_snapshot("2026-09-07")],
    )
    monkeypatch.setattr(free_paper_guard, "_load_marks", lambda root: [])

    with pytest.raises(RuntimeError, match="first prospective mark was missed"):
        free_paper_guard.strict_mark_paper_forward(tmp_path, end="2026-09-10")


def test_strict_mark_refuses_gap_after_existing_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(free_paper_guard, "_load_freeze", lambda root: {"ok": True})
    monkeypatch.setattr(
        free_paper_guard,
        "_load_snapshots",
        lambda root: [_snapshot("2026-09-07")],
    )
    monkeypatch.setattr(
        free_paper_guard,
        "_load_marks",
        lambda root: [_mark("2026-09-07")],
    )

    with pytest.raises(RuntimeError, match="PAPER_LEDGER_GAP"):
        free_paper_guard.strict_mark_paper_forward(tmp_path, end="2026-09-10")


def test_strict_mark_allows_exact_next_day_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(free_paper_guard, "_load_freeze", lambda root: {"ok": True})
    monkeypatch.setattr(
        free_paper_guard,
        "_load_snapshots",
        lambda root: [_snapshot("2026-09-07")],
    )
    monkeypatch.setattr(
        free_paper_guard,
        "_load_marks",
        lambda root: [_mark("2026-09-07")],
    )
    calls: list[date] = []

    def fake_mark(root: Path, *, end: date, research_start: str):
        calls.append(end)
        return {"created_marks": 1, "status": "marked"}

    monkeypatch.setattr(free_paper_guard, "_legacy_mark_paper_forward", fake_mark)
    result = free_paper_guard.strict_mark_paper_forward(tmp_path, end="2026-09-09")
    assert result["created_marks"] == 1
    assert result["retrospective_backfill_allowed"] is False
    assert calls == [date(2026, 9, 9)]


def test_integrity_report_detects_missing_mark_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        free_paper_guard,
        "_load_freeze",
        lambda root: {"first_eligible_effective_date": "2026-09-07"},
    )
    monkeypatch.setattr(
        free_paper_guard,
        "_load_snapshots",
        lambda root: [_snapshot("2026-09-07")],
    )
    monkeypatch.setattr(
        free_paper_guard,
        "_load_marks",
        lambda root: [
            _mark("2026-09-07"),
            _mark("2026-09-09"),
        ],
    )
    report = free_paper_guard.paper_integrity_report(tmp_path)
    assert report["ok"] is False
    assert report["checks"]["mark_dates_contiguous"] is False
    assert report["missing_mark_dates"] == ["2026-09-08"]
