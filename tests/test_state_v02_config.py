from __future__ import annotations

from pathlib import Path

import yaml

from crossalpha.state.v02_config import strict_v02_config_consistency_report


def test_state_v02_repository_config_matches_frozen_implementation() -> None:
    root = Path(__file__).resolve().parents[1]
    report = strict_v02_config_consistency_report(root / "config" / "state_v02.yaml")
    assert report["ok"] is True
    assert report["audit_level"] == "STRICT_CONFIG_IMPLEMENTATION_CONSISTENCY"
    assert all(report["checks"].values())


def test_state_v02_strict_config_detects_threshold_drift(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    raw = yaml.safe_load((root / "config" / "state_v02.yaml").read_text(encoding="utf-8"))
    raw["components"]["stablecoin_flow_decomposition"]["contraction_full_stress_ratio"] = 0.99
    path = tmp_path / "state_v02.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    report = strict_v02_config_consistency_report(path)
    assert report["ok"] is False
    assert report["checks"]["stable_contraction_threshold"] is False
