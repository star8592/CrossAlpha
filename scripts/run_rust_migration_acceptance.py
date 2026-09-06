from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Gate:
    name: str
    command: tuple[str, ...]
    category: str
    required_for_cutover: bool = True
    required_for_retirement: bool = True


def _run(command: list[str], env: dict[str, str]) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    finished = datetime.now(timezone.utc)
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "command": command,
        "stdout_tail": stdout[-8000:],
        "stderr_tail": stderr[-8000:],
    }


def _cargo_lock_gate() -> dict[str, Any]:
    lock = REPO_ROOT / "Cargo.lock"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "Cargo.lock"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0
    return {
        "ok": lock.is_file() and tracked,
        "present": lock.is_file(),
        "tracked": tracked,
        "path": str(lock),
        "remediation": (
            None
            if lock.is_file() and tracked
            else "Run cargo generate-lockfile (or cargo build), then git add Cargo.lock && git commit/push it before State freeze/cutover."
        ),
    }


def _git_clean_gate() -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    dirty = [line for line in completed.stdout.splitlines() if line.strip()]
    return {
        "ok": completed.returncode == 0 and not dirty,
        "returncode": completed.returncode,
        "dirty_entries": dirty[:100],
    }


def _post_cutover_gate(data_root: Path, min_soak_seconds: int) -> dict[str, Any]:
    status_path = data_root / "manifests" / "crossalpha_daemon_health.json"
    required_health = [
        data_root / "manifests" / "observatory_health.json",
        data_root / "manifests" / "materializer_health.json",
    ]
    if not status_path.exists():
        return {
            "ok": False,
            "reason": "unified daemon health file missing",
            "path": str(status_path),
        }
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"invalid daemon health JSON: {type(exc).__name__}"}
    if status.get("status") != "running":
        return {"ok": False, "reason": f"daemon status is {status.get('status')!r}", "status": status}
    try:
        started = datetime.fromisoformat(str(status["checked_at"]).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"invalid daemon checked_at: {type(exc).__name__}"}
    now = datetime.now(timezone.utc)
    soak_seconds = max(0.0, (now - started.astimezone(timezone.utc)).total_seconds())
    health_results: list[dict[str, Any]] = []
    all_health_ok = True
    for path in required_health:
        if not path.exists():
            all_health_ok = False
            health_results.append({"path": str(path), "ok": False, "reason": "missing"})
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            ok = value.get("ok") is True
            checked = value.get("checked_at")
            fresh = True
            if checked:
                stamp = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                fresh = (now - stamp.astimezone(timezone.utc)).total_seconds() <= 1200
            ok = ok and fresh
            all_health_ok = all_health_ok and ok
            health_results.append({"path": str(path), "ok": ok, "fresh": fresh})
        except Exception as exc:  # noqa: BLE001
            all_health_ok = False
            health_results.append({"path": str(path), "ok": False, "reason": type(exc).__name__})
    return {
        "ok": soak_seconds >= min_soak_seconds and all_health_ok,
        "soak_seconds": soak_seconds,
        "minimum_soak_seconds": min_soak_seconds,
        "component_health": health_results,
        "daemon": status,
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the consolidated local-first R7 Rust migration acceptance suite. "
            "No production service is changed by this command."
        )
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--post-cutover", action="store_true")
    parser.add_argument("--minimum-soak-seconds", type=int, default=1800)
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Default: <data-root>/manifests/rust_migration_acceptance.json",
    )
    args = parser.parse_args()
    data_root = args.data_root.resolve()
    output = args.output or data_root / "manifests" / "rust_migration_acceptance.json"
    env = os.environ.copy()
    env["CROSSALPHA_DATA_DIR"] = str(data_root)

    py = str(REPO_ROOT / ".venv" / "bin" / "python")
    gates: list[Gate] = [
        Gate("cargo_fmt", ("cargo", "fmt", "--all", "--", "--check"), "build"),
        Gate("cargo_clippy", ("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"), "build"),
        Gate("cargo_test", ("cargo", "test", "--workspace"), "build"),
        Gate("cargo_build_debug", ("cargo", "build", "--workspace"), "build"),
        Gate("cargo_build_release", ("cargo", "build", "--workspace", "--release"), "build"),
        Gate("manifest_parity", (str(REPO_ROOT / "target/debug/crossalpha-rs"), "manifest-parity", str(data_root)), "real_data"),
        Gate("observatory_health_parity", (py, "scripts/verify_rust_observatory_health_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("observatory_live_health_parity", (py, "scripts/verify_rust_observatory_live_health_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("canonical_parser_parity", (py, "scripts/verify_rust_canonical_parser_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("canonical_parquet_parity", (py, "scripts/verify_rust_canonical_parquet_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("canonical_materializer_parity", (py, "scripts/verify_rust_canonical_materializer.py", "--data-root", str(data_root)), "real_data"),
        Gate("feature_parity", (py, "scripts/verify_rust_feature_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("state_kernel_parity", (py, "scripts/verify_rust_state_kernel_parity.py"), "deterministic"),
        Gate("state_v03_config_parity", (py, "scripts/verify_rust_state_v03_config_parity.py"), "deterministic"),
        Gate("state_v03_freeze_parity", (py, "scripts/verify_rust_state_v03_freeze_parity.py"), "deterministic"),
        Gate("state_v03_runtime_binding", (py, "scripts/verify_rust_state_v03_runtime_binding.py"), "deterministic"),
        Gate("state_v04_parity", (py, "scripts/verify_rust_state_v04_parity.py"), "deterministic"),
        Gate("research_kernel_parity", (py, "scripts/verify_rust_research_kernel_parity.py"), "deterministic"),
    ]
    live_gates = [
        Gate("observatory_collectors_live", (py, "scripts/verify_rust_observatory_collectors.py"), "live"),
        Gate("state_v03_preflight_live", (py, "scripts/verify_rust_state_v03_preflight_parity.py"), "live"),
        Gate(
            "state_v04_preflight_live",
            (
                str(REPO_ROOT / "target/release/crossalpha-state-rs"),
                "v04",
                "preflight",
                "--data-root",
                str(data_root),
            ),
            "live",
        ),
    ]
    if not args.skip_live:
        gates.extend(live_gates)

    results: dict[str, Any] = {}
    build_failed = False
    for gate in gates:
        if build_failed and gate.category != "build":
            results[gate.name] = {
                "ok": False,
                "skipped": True,
                "reason": "build gate failed",
                "category": gate.category,
            }
            continue
        print(f"▶ {gate.name}", flush=True)
        result = _run(list(gate.command), env)
        result["category"] = gate.category
        result["required_for_cutover"] = gate.required_for_cutover
        result["required_for_retirement"] = gate.required_for_retirement
        results[gate.name] = result
        print(f"  {'✓' if result['ok'] else '✗'} rc={result['returncode']}", flush=True)
        if gate.category == "build" and not result["ok"]:
            build_failed = True

    lock_gate = _cargo_lock_gate()
    clean_gate = _git_clean_gate()
    required_cutover = [
        result.get("ok") is True
        for result in results.values()
        if result.get("required_for_cutover") is True
    ]
    gates_green = bool(required_cutover) and all(required_cutover)
    deterministic_and_live_complete = not args.skip_live
    daemon_cutover_allowed = gates_green and lock_gate["ok"] and clean_gate["ok"] and deterministic_and_live_complete

    post_cutover = (
        _post_cutover_gate(data_root, args.minimum_soak_seconds)
        if args.post_cutover
        else {"ok": False, "required": True, "reason": "rerun with --post-cutover after unified daemon cutover and soak"}
    )
    python_retirement_allowed = daemon_cutover_allowed and post_cutover.get("ok") is True

    report = {
        "schema_version": 1,
        "protocol": "CROSSALPHA_RUST_MIGRATION_ACCEPTANCE_V1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "data_root": str(data_root),
        "local_first": True,
        "changes_production_services": False,
        "skip_live": args.skip_live,
        "gates": results,
        "cargo_lock": lock_gate,
        "git_clean": clean_gate,
        "post_cutover_soak": post_cutover,
        "daemon_cutover_allowed": daemon_cutover_allowed,
        "python_retirement_allowed": python_retirement_allowed,
        "subsystems": {
            "observatory_python_retirement_allowed": python_retirement_allowed,
            "materializer_python_retirement_allowed": python_retirement_allowed,
            "state_v03_python_retirement_allowed": python_retirement_allowed,
            "state_v04_python_retirement_allowed": python_retirement_allowed,
            "research_python_runtime_required_for_production": False if python_retirement_allowed else None,
        },
        "decision": (
            "PYTHON_RETIREMENT_ALLOWED"
            if python_retirement_allowed
            else "DAEMON_CUTOVER_ALLOWED"
            if daemon_cutover_allowed
            else "RUST_MIGRATION_GATED"
        ),
    }
    _atomic_write(output, report)
    print(
        "acceptance={} daemon_cutover_allowed={} python_retirement_allowed={} output={}".format(
            report["decision"],
            str(daemon_cutover_allowed).lower(),
            str(python_retirement_allowed).lower(),
            output,
        )
    )
    if not lock_gate["ok"]:
        print(f"cargo_lock_remediation={lock_gate['remediation']}")
    return 0 if daemon_cutover_allowed or (args.skip_live and gates_green) else 1


if __name__ == "__main__":
    raise SystemExit(main())
