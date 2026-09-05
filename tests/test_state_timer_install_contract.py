from pathlib import Path


def test_state_v03_v04_timers_do_not_immediately_repeat_finalizer_cycles() -> None:
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
