from __future__ import annotations

import json

from crossalpha.catalog import build_catalog
from crossalpha.cli import materialize_observatory
from crossalpha.settings import Settings
from crossalpha.state.shadow import build_latest_shadow_state


def main() -> None:
    settings = Settings()
    settings.ensure_dirs()

    observatory = materialize_observatory(settings)
    try:
        state_shadow = build_latest_shadow_state(settings.crossalpha_data_dir, write=True)
        state_error = None
    except Exception as exc:  # fault isolation: never break the Observatory materializer
        state_shadow = {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "shadow_only": True,
        }
        state_error = str(exc)

    catalog = build_catalog(settings.crossalpha_data_dir)
    report = {
        "observatory": observatory,
        "state_shadow": state_shadow,
        "state_shadow_error": state_error,
        "catalog": catalog,
        "fault_isolation": "state_shadow_failure_does_not_fail_observatory_materialization",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
