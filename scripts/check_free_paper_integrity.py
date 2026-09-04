from __future__ import annotations

import json

from crossalpha.core.free_paper_guard import paper_integrity_report
from crossalpha.settings import Settings


def main() -> None:
    settings = Settings()
    settings.ensure_dirs()
    report = paper_integrity_report(settings.crossalpha_data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit("PAPER LEDGER INTEGRITY FAILED")


if __name__ == "__main__":
    main()
