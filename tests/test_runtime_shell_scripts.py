from __future__ import annotations

import subprocess
from pathlib import Path


RUNTIME_SCRIPTS = (
    "scripts/install_user_service.sh",
    "scripts/install_materializer_timer.sh",
    "scripts/install_all_user_services.sh",
    "scripts/install_free_paper_user_services.sh",
    "scripts/install_state_v02_user_service.sh",
    "scripts/install_state_v03_user_service.sh",
    "scripts/install_state_v04_user_service.sh",
    "scripts/install_outcome_linkage_user_service.sh",
    "scripts/run_free_paper_daily.sh",
    "scripts/run_free_paper_weekly.sh",
    "scripts/materialize_observatory_and_state.py",  # skipped by bash check below
    "scripts/finalize_current_milestone_once.sh",
    "scripts/finalize_state_ab_milestone_once.sh",
    "scripts/finalize_state_v02_milestone_once.sh",
    "scripts/finalize_state_v03_milestone_once.sh",
    "scripts/finalize_crossalpha_research_stack_once.sh",
    "scripts/resume_crossalpha_after_v03_rpc_fix_once.sh",
)


def test_runtime_bash_scripts_have_valid_syntax() -> None:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for relative in RUNTIME_SCRIPTS:
        path = root / relative
        assert path.exists(), relative
        if path.suffix != ".sh":
            continue
        proc = subprocess.run(
            ["bash", "-n", str(path)],
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            failures.append(f"{relative}: {proc.stderr.strip()}")
    assert not failures, "\n".join(failures)


def test_v03_v04_installers_delay_first_automatic_cycle() -> None:
    root = Path(__file__).resolve().parents[1]
    v03 = (root / "scripts" / "install_state_v03_user_service.sh").read_text(encoding="utf-8")
    v04 = (root / "scripts" / "install_state_v04_user_service.sh").read_text(encoding="utf-8")

    assert "OnBootSec=" not in v03
    assert "OnActiveSec=15min" in v03
    assert "OnUnitActiveSec=15min" in v03
    assert "resolve_rpc_candidates" in v03

    assert "OnBootSec=" not in v04
    assert "OnActiveSec=5min" in v04
    assert "OnUnitActiveSec=5min" in v04
