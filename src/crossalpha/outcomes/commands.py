from __future__ import annotations

import argparse
import json
from pathlib import Path

from crossalpha.settings import Settings
from crossalpha.outcomes.integrity import (
    outcome_linkage_status,
    strict_outcome_linkage_integrity_report,
)
from crossalpha.outcomes.linkage import materialize_outcome_links
from crossalpha.outcomes.prospective import (
    config_consistency_report,
    freeze_outcome_linkage,
)
from crossalpha.state.ab_integrity import strict_state_ab_integrity_report
from crossalpha.state.v02_integrity import strict_state_v02_integrity_report
from crossalpha.state.v03_integrity import strict_state_v03_integrity_report
from crossalpha.state.v04_config import strict_v04_config_report
from crossalpha.state.v04_integrity import strict_state_v04_integrity_report


def config_check_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-outcome-config-check")
    parser.parse_args()
    report = config_consistency_report()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("OUTCOME LINKAGE STRICT CONFIG CONSISTENCY FAILED")


def freeze_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-outcome-freeze")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    v04_config = strict_v04_config_report(Path("config/state_v04.yaml"))
    if not v04_config.get("ok"):
        raise SystemExit("OUTCOME LINKAGE FREEZE REFUSED: State V0.4 runtime config/hash drift")
    predecessors = {
        "state_ab_v01": strict_state_ab_integrity_report(settings.crossalpha_data_dir),
        "state_v02": strict_state_v02_integrity_report(settings.crossalpha_data_dir),
        "state_v03": strict_state_v03_integrity_report(settings.crossalpha_data_dir),
        "state_v04": strict_state_v04_integrity_report(settings.crossalpha_data_dir),
    }
    bad = [name for name, report in predecessors.items() if not report.get("frozen") or not report.get("ok")]
    if bad:
        raise SystemExit(f"OUTCOME LINKAGE FREEZE REFUSED: predecessor integrity failed: {bad}")
    report = freeze_outcome_linkage(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def materialize_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-outcome-materialize")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = materialize_outcome_links(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


def integrity_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-outcome-integrity")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = strict_outcome_linkage_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("OUTCOME LINKAGE INTEGRITY FAILED")


def status_main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha-outcome-status")
    parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    report = outcome_linkage_status(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
