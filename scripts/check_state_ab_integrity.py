from __future__ import annotations

import json

from crossalpha.settings import Settings
from crossalpha.state.ab_paper import state_ab_integrity_report


def main() -> None:
    settings = Settings()
    settings.ensure_dirs()
    report = state_ab_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("STATE A/B INTEGRITY FAILED")


if __name__ == "__main__":
    main()
