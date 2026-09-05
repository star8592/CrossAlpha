from __future__ import annotations

from pathlib import Path


def test_grand_finalizer_is_single_pass_and_non_nested() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "finalize_crossalpha_research_stack_once.sh").read_text(
        encoding="utf-8"
    )

    # The expensive full suite is intentionally run exactly once for the entire stack.
    assert text.count('pytest -q') == 1

    # Never regain the old nested-finalizer pattern, which repeatedly re-ran tests/setup.
    for legacy in (
        "finalize_current_milestone_once.sh",
        "finalize_state_ab_milestone_once.sh",
        "finalize_state_v02_milestone_once.sh",
        "finalize_state_v03_milestone_once.sh",
    ):
        assert legacy not in text

    # One post-freeze V0.4 data cycle both writes the observation and its health report.
    assert text.count("python scripts/run_state_v04_cycle.py") == 1
    assert "crossalpha-state-v04-cycle" not in text

    # One Outcome cycle performs deterministic materialization + health/catalog.
    assert text.count("python scripts/run_outcome_linkage_cycle.py") == 1
    assert "crossalpha-outcome-materialize" not in text

    # V0.3/V0.4 freeze commands own their preflight; the finalizer must not duplicate them.
    assert "crossalpha-state-v03-preflight" not in text
    assert "crossalpha-state-v04-preflight" not in text

    # The finalizer is a safe child script: failures are summarized, not propagated to
    # an interactive shell through errexit.
    assert "set -e" not in text
    assert text.rstrip().endswith("exit 0")
