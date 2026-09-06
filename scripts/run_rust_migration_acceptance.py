from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "command": command,
        "stdout_tail": completed.stdout.strip()[-8000:],
        "stderr_tail": completed.stderr.strip()[-8000:],
    }


def _json_run(command: list[str], env: dict[str, str]) -> dict[str, Any]:
    result = _run(command, env)
    if not result["ok"]:
        return result
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        result["ok"] = False
        result["returncode"] = completed.returncode
        result["stderr_tail"] = completed.stderr.strip()[-8000:]
        return result
    try:
        result["value"] = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        result["ok"] = False
        result["parse_error"] = str(exc)
    return result


def _cargo_lock_gate() -> dict[str, Any]:
    path = REPO_ROOT / "Cargo.lock"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "Cargo.lock"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    ).returncode == 0
    ok = path.is_file() and tracked
    return {
        "ok": ok,
        "present": path.is_file(),
        "tracked": tracked,
        "path": str(path),
        "remediation": None if ok else (
            "Generate Cargo.lock with the real local Cargo resolver, commit it, then rerun acceptance. "
            "Do not hand-author a lockfile."
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


def _production_state_integrity(data_root: Path, env: dict[str, str]) -> dict[str, Any]:
    binary = REPO_ROOT / "target/release/crossalpha-state-rs"
    result: dict[str, Any] = {}
    for version in ("v02", "v03", "v04"):
        row = _json_run(
            [str(binary), version, "integrity", "--data-root", str(data_root)], env
        )
        value = row.get("value", {})
        row["cycle_enabled"] = value.get("cycle_enabled") is True
        row["ok"] = row.get("ok") is True and row["cycle_enabled"]
        result[version] = row
    result["ok"] = all(result[version]["ok"] for version in ("v02", "v03", "v04"))
    return result


def _health_file(path: Path, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "ok": False, "reason": "missing"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        checked_at = value.get("checked_at")
        fresh = True
        if checked_at:
            stamp = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            fresh = (now - stamp.astimezone(timezone.utc)).total_seconds() <= 1200
        return {
            "path": str(path),
            "ok": value.get("ok") is True and fresh,
            "fresh": fresh,
            "checked_at": checked_at,
        }
    except Exception as exc:  # noqa: BLE001
        return {"path": str(path), "ok": False, "reason": type(exc).__name__}


def _post_cutover_gate(data_root: Path, minimum_soak_seconds: int) -> dict[str, Any]:
    status_path = data_root / "manifests/crossalpha_daemon_health.json"
    if not status_path.exists():
        return {"ok": False, "reason": "unified daemon health file missing", "path": str(status_path)}
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"invalid daemon health JSON: {type(exc).__name__}"}
    if status.get("status") != "running":
        return {"ok": False, "reason": f"daemon status is {status.get('status')!r}", "daemon": status}

    checked = datetime.fromisoformat(str(status["checked_at"]).replace("Z", "+00:00"))
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    soak_seconds = max(0.0, (now - checked.astimezone(timezone.utc)).total_seconds())
    components = set(status.get("components") or [])
    expected = {"Observatory", "Materializer", "StateV02", "StateV03", "StateV04"}
    health_paths = {
        "Observatory": data_root / "manifests/observatory_health.json",
        "Materializer": data_root / "manifests/materializer_health.json",
        "StateV02": data_root / "manifests/state_v02_daemon_health.json",
        "StateV03": data_root / "manifests/state_v03_daemon_health.json",
        "StateV04": data_root / "manifests/state_v04_daemon_health.json",
    }
    health = {
        component: _health_file(path, now)
        for component, path in health_paths.items()
        if component in components
    }
    full_component_set = expected.issubset(components)
    all_healthy = full_component_set and all(health.get(name, {}).get("ok") is True for name in expected)
    return {
        "ok": soak_seconds >= minimum_soak_seconds and all_healthy,
        "full_component_set": full_component_set,
        "components": sorted(components),
        "soak_seconds": soak_seconds,
        "minimum_soak_seconds": minimum_soak_seconds,
        "component_health": health,
        "daemon": status,
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _gates(py: str, data_root: Path, include_live: bool) -> list[Gate]:
    debug = REPO_ROOT / "target/debug"
    release = REPO_ROOT / "target/release"
    gates = [
        Gate("cargo_fmt", ("cargo", "fmt", "--all", "--", "--check"), "build"),
        Gate("cargo_clippy", ("cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"), "build"),
        Gate("cargo_test", ("cargo", "test", "--workspace"), "build"),
        Gate("cargo_build_debug", ("cargo", "build", "--workspace"), "build"),
        Gate("cargo_build_release", ("cargo", "build", "--workspace", "--release"), "build"),
        Gate("raw_envelope_v2", (py, "scripts/verify_rust_raw_envelope_v2.py"), "deterministic"),
        Gate("free_core_parity", (py, "scripts/verify_rust_free_core_parity.py"), "deterministic"),
        Gate("paper_ab_parity", (py, "scripts/verify_rust_paper_ab_parity.py"), "deterministic"),
        Gate("state_v02_parity", (py, "scripts/verify_rust_state_v02_parity.py"), "deterministic"),
        Gate("state_kernel_parity", (py, "scripts/verify_rust_state_kernel_parity.py"), "deterministic"),
        Gate("state_v03_config_parity", (py, "scripts/verify_rust_state_v03_config_parity.py"), "deterministic"),
        Gate("state_v03_freeze_parity", (py, "scripts/verify_rust_state_v03_freeze_parity.py"), "deterministic"),
        Gate("state_v03_runtime_binding", (py, "scripts/verify_rust_state_v03_runtime_binding.py"), "deterministic"),
        Gate("state_v04_parity", (py, "scripts/verify_rust_state_v04_parity.py"), "deterministic"),
        Gate("research_kernel_parity", (py, "scripts/verify_rust_research_kernel_parity.py"), "deterministic"),
        Gate("manifest_parity", (str(debug / "crossalpha-rs"), "manifest-parity", str(data_root)), "real_data"),
        Gate("observatory_health_parity", (py, "scripts/verify_rust_observatory_health_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("observatory_live_health_parity", (py, "scripts/verify_rust_observatory_live_health_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("canonical_parser_parity", (py, "scripts/verify_rust_canonical_parser_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("canonical_parquet_parity", (py, "scripts/verify_rust_canonical_parquet_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("canonical_materializer_parity", (py, "scripts/verify_rust_canonical_materializer.py", "--data-root", str(data_root)), "real_data"),
        Gate("feature_parity", (py, "scripts/verify_rust_feature_parity.py", "--data-root", str(data_root)), "real_data"),
        Gate("shadow_v01_parity", (py, "scripts/verify_rust_shadow_v01_parity.py", "--data-root", str(data_root)), "real_data"),
    ]
    if include_live:
        gates.extend([
            Gate("observatory_collectors_live", (py, "scripts/verify_rust_observatory_collectors.py"), "live"),
            Gate("state_v02_preflight_live", (str(release / "crossalpha-state-rs"), "v02", "preflight", "--data-root", str(data_root)), "live"),
            Gate("state_v03_preflight_live", (py, "scripts/verify_rust_state_v03_preflight_parity.py"), "live"),
            Gate("state_v04_preflight_live", (str(release / "crossalpha-state-rs"), "v04", "preflight", "--data-root", str(data_root)), "live"),
        ])
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the consolidated local-first R7 Rust migration acceptance suite. "
            "This command never changes production services or creates legacy freezes."
        )
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--post-cutover", action="store_true")
    parser.add_argument("--minimum-soak-seconds", type=int, default=1800)
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    data_root = args.data_root.resolve()
    output = args.output or data_root / "manifests/rust_migration_acceptance.json"
    env = os.environ.copy()
    env["CROSSALPHA_DATA_DIR"] = str(data_root)
    py = str(REPO_ROOT / ".venv/bin/python")

    results: dict[str, Any] = {}
    build_failed = False
    for gate in _gates(py, data_root, include_live=not args.skip_live):
        if build_failed and gate.category != "build":
            results[gate.name] = {
                "ok": False,
                "skipped": True,
                "reason": "build gate failed",
                "category": gate.category,
                "required_for_cutover": gate.required_for_cutover,
                "required_for_retirement": gate.required_for_retirement,
            }
            continue
        print(f"▶ {gate.name}", flush=True)
        result = _run(list(gate.command), env)
        result.update({
            "category": gate.category,
            "required_for_cutover": gate.required_for_cutover,
            "required_for_retirement": gate.required_for_retirement,
        })
        results[gate.name] = result
        print(f"  {'✓' if result['ok'] else '✗'} rc={result['returncode']}", flush=True)
        if gate.category == "build" and not result["ok"]:
            build_failed = True

    lock_gate = _cargo_lock_gate()
    clean_gate = _git_clean_gate()
    production_state = (
        _production_state_integrity(data_root, env)
        if not build_failed
        else {"ok": False, "reason": "build gate failed"}
    )
    required_cutover = [
        row.get("ok") is True
        for row in results.values()
        if row.get("required_for_cutover") is True
    ]
    gates_green = bool(required_cutover) and all(required_cutover)
    daemon_cutover_allowed = gates_green and lock_gate["ok"] and clean_gate["ok"] and not args.skip_live

    post_cutover = (
        _post_cutover_gate(data_root, args.minimum_soak_seconds)
        if args.post_cutover
        else {
            "ok": False,
            "required": True,
            "full_component_set": False,
            "reason": "rerun with --post-cutover after full unified daemon cutover and soak",
        }
    )
    production_runtime = (
        _json_run([py, "scripts/verify_rust_production_runtime.py", "--data-root", str(data_root)], env)
        if args.post_cutover and not build_failed
        else {"ok": False, "required": True, "reason": "installed-runtime audit requires --post-cutover"}
    )
    runtime_value = production_runtime.get("value", {})
    runtime_integrity = runtime_value.get("integrity", {})
    installed_runtime_green = production_runtime.get("ok") is True and runtime_value.get("ok") is True
    running = set(post_cutover.get("components") or [])

    obs_retire = daemon_cutover_allowed and post_cutover.get("ok") is True and "Observatory" in running
    materializer_retire = daemon_cutover_allowed and post_cutover.get("ok") is True and "Materializer" in running
    state_v02_retire = obs_retire and production_state.get("v02", {}).get("ok") is True and "StateV02" in running
    state_v03_retire = obs_retire and production_state.get("v03", {}).get("ok") is True and "StateV03" in running
    state_v04_retire = obs_retire and production_state.get("v04", {}).get("ok") is True and "StateV04" in running
    paper_retire = installed_runtime_green and runtime_integrity.get("paper", {}).get("ok") is True
    ab_retire = installed_runtime_green and runtime_integrity.get("state_ab", {}).get("ok") is True
    outcome_retire = installed_runtime_green and runtime_integrity.get("outcome", {}).get("ok") is True

    python_retirement_allowed = (
        all((
            obs_retire,
            materializer_retire,
            state_v02_retire,
            state_v03_retire,
            state_v04_retire,
            paper_retire,
            ab_retire,
            outcome_retire,
        ))
        and post_cutover.get("full_component_set") is True
        and runtime_value.get("python_production_writer_detected") is False
    )

    decision = (
        "PYTHON_RETIREMENT_ALLOWED"
        if python_retirement_allowed
        else "DAEMON_CUTOVER_ALLOWED"
        if daemon_cutover_allowed
        else "RUST_MIGRATION_GATED"
    )
    report = {
        "schema_version": 2,
        "protocol": "CROSSALPHA_RUST_MIGRATION_ACCEPTANCE_V2",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
        "data_root": str(data_root),
        "local_first": True,
        "changes_production_services": False,
        "creates_legacy_freezes": False,
        "skip_live": args.skip_live,
        "gates": results,
        "cargo_lock": lock_gate,
        "git_clean": clean_gate,
        "production_state_integrity": production_state,
        "post_cutover_soak": post_cutover,
        "production_runtime_audit": production_runtime,
        "daemon_cutover_allowed": daemon_cutover_allowed,
        "python_retirement_allowed": python_retirement_allowed,
        "subsystems": {
            "observatory_python_retirement_allowed": obs_retire,
            "materializer_python_retirement_allowed": materializer_retire,
            "state_v02_python_retirement_allowed": state_v02_retire,
            "state_v03_python_retirement_allowed": state_v03_retire,
            "state_v04_python_retirement_allowed": state_v04_retire,
            "paper_python_retirement_allowed": paper_retire,
            "state_ab_python_retirement_allowed": ab_retire,
            "outcome_linkage_python_retirement_allowed": outcome_retire,
            "research_python_runtime_required_for_production": False if python_retirement_allowed else None,
        },
        "decision": decision,
    }
    _atomic_write(output, report)
    print(
        "acceptance={} daemon_cutover_allowed={} python_retirement_allowed={} output={}".format(
            decision,
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
