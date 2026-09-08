#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _json_command(command: list[str]) -> dict[str, Any]:
    completed = _run(command)
    result: dict[str, Any] = {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip()[-4000:],
    }
    if completed.returncode != 0:
        return result
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {**result, "ok": False, "parse_error": str(exc)}
    result["value"] = value
    result["ok"] = value.get("ok") is True or value.get("cycle_enabled") is True
    return result


def _unit_text(unit: str) -> dict[str, Any]:
    completed = _run(["systemctl", "--user", "cat", unit])
    text = completed.stdout
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "text": text,
        "contains_python": "python" in text.lower() or ".venv" in text.lower(),
    }


def _is_active(unit: str) -> bool:
    return _run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def _is_enabled(unit: str) -> bool:
    return _run(["systemctl", "--user", "is-enabled", "--quiet", unit]).returncode == 0


def _script_is_rust_only(path: Path, required: tuple[str, ...]) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "path": str(path), "reason": "missing"}
    text = path.read_text(encoding="utf-8")
    forbidden = [token for token in ("python ", ".venv", "crossalpha-free-paper-") if token in text]
    missing = [token for token in required if token not in text]
    return {
        "ok": not forbidden and not missing,
        "path": str(path),
        "forbidden_tokens": forbidden,
        "missing_required_tokens": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify that installed CrossAlpha production writers are Rust-only."
    )
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    release = REPO_ROOT / "target" / "release"

    state = release / "crossalpha-state-rs"
    paper = release / "crossalpha-paper-rs"
    ab = release / "crossalpha-ab-rs"
    outcome = release / "crossalpha-outcome-rs"

    integrity: dict[str, Any] = {}
    for version in ("v02", "v03", "v04"):
        row = _json_command(
            [str(state), version, "integrity", "--data-root", str(data_root)]
        )
        value = row.get("value", {})
        row["ok"] = row.get("ok") is True and value.get("cycle_enabled") is True
        integrity[f"state_{version}"] = row
    integrity["paper"] = _json_command(
        [str(paper), "integrity", "--data-root", str(data_root)]
    )
    integrity["state_ab"] = _json_command(
        [str(ab), "integrity", "--data-root", str(data_root)]
    )
    integrity["outcome"] = _json_command(
        [str(outcome), "integrity", "--data-root", str(data_root)]
    )
    integrity_ok = all(row.get("ok") is True for row in integrity.values())

    units = {
        "daemon": _unit_text("crossalpha-daemon.service"),
        "paper_daily": _unit_text("crossalpha-free-paper-daily.service"),
        "paper_weekly": _unit_text("crossalpha-free-paper-weekly.service"),
        "outcome": _unit_text("crossalpha-outcome-linkage.service"),
    }
    expected_text = {
        "daemon": "crossalpha-daemon-rs",
        "paper_daily": "run_free_paper_daily.sh",
        "paper_weekly": "run_free_paper_weekly.sh",
        "outcome": "crossalpha-outcome-rs",
    }
    for name, row in units.items():
        row["expected_token"] = expected_text[name]
        row["expected_token_present"] = expected_text[name] in row.get("text", "")
        row["ok"] = (
            row.get("ok") is True
            and row["expected_token_present"]
            and not row["contains_python"]
        )
        row.pop("text", None)

    scripts = {
        "paper_daily": _script_is_rust_only(
            REPO_ROOT / "scripts" / "run_free_paper_daily.sh",
            ("crossalpha-paper-rs", "crossalpha-ab-rs"),
        ),
        "paper_weekly": _script_is_rust_only(
            REPO_ROOT / "scripts" / "run_free_paper_weekly.sh",
            ("crossalpha-paper-rs", "crossalpha-ab-rs"),
        ),
    }

    expected_active = {
        "crossalpha-daemon.service": _is_active("crossalpha-daemon.service"),
        "crossalpha-free-paper-daily.timer": _is_active("crossalpha-free-paper-daily.timer"),
        "crossalpha-free-paper-weekly.timer": _is_active("crossalpha-free-paper-weekly.timer"),
        "crossalpha-outcome-linkage.timer": _is_active("crossalpha-outcome-linkage.timer"),
    }
    expected_enabled = {
        "crossalpha-daemon.service": _is_enabled("crossalpha-daemon.service"),
        "crossalpha-free-paper-daily.timer": _is_enabled("crossalpha-free-paper-daily.timer"),
        "crossalpha-free-paper-weekly.timer": _is_enabled("crossalpha-free-paper-weekly.timer"),
        "crossalpha-outcome-linkage.timer": _is_enabled("crossalpha-outcome-linkage.timer"),
    }
    legacy_units = (
        "crossalpha-observatory.service",
        "crossalpha-materializer.timer",
        "crossalpha-state-v02.timer",
        "crossalpha-state-v03.timer",
        "crossalpha-state-v04.timer",
    )
    legacy_active = {unit: _is_active(unit) for unit in legacy_units}
    legacy_enabled = {unit: _is_enabled(unit) for unit in legacy_units}

    units_ok = all(row["ok"] for row in units.values())
    scripts_ok = all(row["ok"] for row in scripts.values())
    scheduling_ok = all(expected_active.values()) and all(expected_enabled.values())
    no_legacy_writer = not any(legacy_active.values()) and not any(legacy_enabled.values())
    ok = integrity_ok and units_ok and scripts_ok and scheduling_ok and no_legacy_writer

    report = {
        "schema_version": 1,
        "protocol": "CROSSALPHA_RUST_PRODUCTION_RUNTIME_AUDIT_V1",
        "ok": ok,
        "data_root": str(data_root),
        "integrity": integrity,
        "installed_units": units,
        "rust_only_scripts": scripts,
        "expected_active": expected_active,
        "expected_enabled": expected_enabled,
        "legacy_writer_active": legacy_active,
        "legacy_writer_enabled": legacy_enabled,
        "python_production_writer_detected": not (units_ok and scripts_ok and no_legacy_writer),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
