from __future__ import annotations

import pandas as pd
import pytest

from crossalpha.core.roll_map import build_previous_volume_roll_map


def _bars(volumes: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(volumes, columns=["date", "contract", "volume"])


def test_roll_map_uses_previous_day_volume_and_never_rolls_backward() -> None:
    bars = _bars(
        [
            ("2026-09-01", "F1", 100),
            ("2026-09-01", "F2", 50),
            ("2026-09-02", "F1", 80),
            ("2026-09-02", "F2", 120),
            ("2026-09-03", "F1", 200),
            ("2026-09-03", "F2", 100),
            ("2026-09-04", "F1", 300),
            ("2026-09-04", "F2", 90),
        ]
    )
    meta = pd.DataFrame(
        {
            "contract": ["F1", "F2"],
            "expiration_date": ["2026-09-30", "2026-10-31"],
        }
    )

    result = build_previous_volume_roll_map(bars, meta, safety_days=2)
    assert list(result["contract"]) == ["F1", "F2", "F2"]
    assert list(result["rolled"]) == [False, True, False]
    assert result.loc[1, "decision_volume_date"] == pd.Timestamp("2026-09-02", tz="UTC")


def test_current_day_volume_cannot_change_same_day_selection() -> None:
    base = _bars(
        [
            ("2026-09-01", "F1", 100),
            ("2026-09-01", "F2", 50),
            ("2026-09-02", "F1", 1),
            ("2026-09-02", "F2", 1000),
            ("2026-09-03", "F1", 1),
            ("2026-09-03", "F2", 1000),
        ]
    )
    changed = base.copy()
    changed.loc[changed["date"] == "2026-09-02", "volume"] = [9999, 1]
    meta = pd.DataFrame(
        {
            "contract": ["F1", "F2"],
            "expiration_date": ["2026-09-30", "2026-10-31"],
        }
    )

    first = build_previous_volume_roll_map(base, meta, safety_days=2)
    second = build_previous_volume_roll_map(changed, meta, safety_days=2)
    # Sep-02 selection is based on Sep-01 volume only and must be identical.
    assert first.loc[0, "contract"] == "F1"
    assert second.loc[0, "contract"] == "F1"
    # Sep-03 is allowed to differ because Sep-02 volume is then known.
    assert first.loc[1, "contract"] == "F2"
    assert second.loc[1, "contract"] == "F1"


def test_expiry_safety_forces_forward_roll_even_when_front_volume_is_higher() -> None:
    rows: list[tuple[str, str, float]] = []
    for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
        rows.extend([(day, "F1", 1000), (day, "F2", 10)])
    bars = _bars(rows)
    meta = pd.DataFrame(
        {
            "contract": ["F1", "F2"],
            "expiration_date": ["2026-09-06", "2026-10-31"],
        }
    )

    result = build_previous_volume_roll_map(bars, meta, safety_days=2)
    assert list(result["contract"]) == ["F1", "F1", "F2"]
    final = result.iloc[-1]
    assert bool(final["rolled"]) is True
    assert bool(final["forced_roll"]) is True
    assert final["decision_reason"] == "expiry_safety"


def test_roll_map_rejects_missing_contract_metadata() -> None:
    bars = _bars(
        [
            ("2026-09-01", "F1", 100),
            ("2026-09-01", "F2", 50),
            ("2026-09-02", "F1", 100),
            ("2026-09-02", "F2", 50),
        ]
    )
    meta = pd.DataFrame({"contract": ["F1"], "expiration_date": ["2026-09-30"]})

    with pytest.raises(ValueError, match="missing contract metadata"):
        build_previous_volume_roll_map(bars, meta)
