from pathlib import Path


def test_d2_resume_finalizer_is_narrow_and_does_not_repeat_full_pytest() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "resume_crossalpha_after_v03_rpc_fix_once.sh").read_text(
        encoding="utf-8"
    )

    assert 'expected = ["D2. preflight + freeze State V0.3::1"]' in script
    assert 'row.get("V03_hash_final") == "MISSING"' in script
    assert 'row.get("V04_hash_final") == "MISSING"' in script
    assert 'row.get("OUTCOME_hash_final") == "MISSING"' in script

    assert 'run_critical "R3. targeted State V0.3 regression suite" pytest -q' in script
    assert 'pytest -q\n' not in script
    assert "tests/test_state_v03_rpc.py" in script
    assert "tests/test_state_v03_preflight.py" in script
    assert "tests/test_state_v03_config.py" in script
    assert "tests/test_state_v03_cycle.py" in script
    assert "tests/test_state_v03_prospective.py" in script

    assert "finalize_crossalpha_research_stack_once.sh" not in script
    assert "finalize_state_v03_milestone_once.sh" not in script
    assert "FINAL_RESULT=PASS" in script
    assert "full_pytest_repeated" in script
