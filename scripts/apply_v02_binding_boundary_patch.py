from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"already_patched={label}")
        return
    if old not in text:
        raise SystemExit(f"patch marker not found: {label}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched={label}")


runtime = Path("crates/crossalpha-state/src/v02/runtime_binding.rs")
replace_once(
    runtime,
    '''    if freeze.get("protocol").and_then(Value::as_str) != Some(PROSPECTIVE_PROTOCOL) {
        bail!("State V0.2 freeze protocol mismatch");
    }
    let root = repo_root();
''',
    '''    if freeze.get("protocol").and_then(Value::as_str) != Some(PROSPECTIVE_PROTOCOL) {
        bail!("State V0.2 freeze protocol mismatch");
    }
    let legacy_attestation =
        crate::v02_legacy_binding::verify_attestation_for_binding(data_root, bound_at)?;
    let root = repo_root();
''',
    "runtime-binding-requires-legacy-attestation",
)
replace_once(
    runtime,
    '''        "legacy_freeze": {
            "path": freeze_path.to_string_lossy(),
            "file_sha256": sha256_file(&freeze_path)?,
            "record_sha256": freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        },
        "bound_at": bound_at.to_rfc3339_opts(SecondsFormat::Micros, false),
''',
    '''        "legacy_freeze": {
            "path": freeze_path.to_string_lossy(),
            "file_sha256": sha256_file(&freeze_path)?,
            "record_sha256": freeze.get("record_sha256").cloned().unwrap_or(Value::Null),
        },
        "legacy_python_integrity_attestation": legacy_attestation,
        "bound_at": bound_at.to_rfc3339_opts(SecondsFormat::Micros, false),
''',
    "runtime-binding-embeds-legacy-attestation",
)
replace_once(
    runtime,
    '''        (
            "state_v02_prospective",
            "crates/crossalpha-state/src/v02/prospective.rs",
        ),
        (
            "state_v02_runtime_binding",
''',
    '''        (
            "state_v02_prospective",
            "crates/crossalpha-state/src/v02/prospective.rs",
        ),
        (
            "state_v02_legacy_binding",
            "crates/crossalpha-state/src/v02/legacy_binding.rs",
        ),
        (
            "state_v02_runtime_binding",
''',
    "runtime-binding-source-hash-legacy-module",
)

prospective = Path("crates/crossalpha-state/src/v02/prospective.rs")
replace_once(
    prospective,
    '''    let binding_record_sha = binding
        .as_ref()
        .and_then(|value| value.get("record_sha256"));
    let first_eligible = parse_time(freeze.get("first_eligible_observed_at"))?;
''',
    '''    let binding_record_sha = binding
        .as_ref()
        .and_then(|value| value.get("record_sha256"));
    let binding_bound_at = binding
        .as_ref()
        .and_then(|value| value.get("bound_at"))
        .map(|value| parse_time(Some(value)))
        .transpose()?;
    let legacy_ledger_ok = match (binding.as_ref(), binding_bound_at) {
        (Some(binding), Some(bound_at)) =>
            crate::v02_legacy_binding::verify_bound_legacy_ledger(data_root, bound_at, binding)?,
        _ => false,
    };
    let first_eligible = parse_time(freeze.get("first_eligible_observed_at"))?;
''',
    "prospective-load-binding-boundary",
)
replace_once(
    prospective,
    '''    let mut observation_seals = true;
    let mut freeze_links = true;
    let mut runtime_binding_links = true;
''',
    '''    let mut native_observation_seals = true;
    let mut freeze_links = true;
    let mut runtime_binding_links = true;
''',
    "prospective-native-seal-counter",
)
replace_once(
    prospective,
    '''    for row in &rows {
        let computed = payload_hash(row)?;
        observation_seals &=
            row.get("record_sha256").and_then(Value::as_str) == Some(computed.as_str());
        freeze_links &= row.get("freeze_record_sha256") == freeze.get("record_sha256");
        if row.get("rust_runtime_binding_record_sha256").is_some() {
            native_binding_linked_count += 1;
            runtime_binding_links &=
                row.get("rust_runtime_binding_record_sha256") == binding_record_sha;
            runtime_binding_links &= row
                .get("rust_runtime_binding_file_sha256")
                .and_then(Value::as_str)
                == binding_file_sha.as_deref();
        }
        let ts = parse_time(row.get("generated_at"))?;
''',
    '''    for row in &rows {
        let ts = parse_time(row.get("generated_at"))?;
        let native = binding_bound_at.is_some_and(|bound_at| ts >= bound_at);
        if native {
            let computed = payload_hash(row)?;
            native_observation_seals &=
                row.get("record_sha256").and_then(Value::as_str) == Some(computed.as_str());
            native_binding_linked_count += 1;
            runtime_binding_links &=
                row.get("rust_runtime_binding_record_sha256") == binding_record_sha;
            runtime_binding_links &= row
                .get("rust_runtime_binding_file_sha256")
                .and_then(Value::as_str)
                == binding_file_sha.as_deref();
        } else {
            runtime_binding_links &= row.get("rust_runtime_binding_record_sha256").is_none()
                && row.get("rust_runtime_binding_file_sha256").is_none();
        }
        freeze_links &= row.get("freeze_record_sha256") == freeze.get("record_sha256");
''',
    "prospective-split-legacy-native-seals",
)
replace_once(
    prospective,
    '''    checks.insert(
        "observation_seals".to_owned(),
        Value::Bool(observation_seals),
    );
    checks.insert("freeze_links".to_owned(), Value::Bool(freeze_links));
''',
    '''    checks.insert(
        "legacy_python_ledger_immutable".to_owned(),
        Value::Bool(legacy_ledger_ok),
    );
    checks.insert(
        "native_observation_seals".to_owned(),
        Value::Bool(native_observation_seals),
    );
    checks.insert(
        "observation_seals".to_owned(),
        Value::Bool(legacy_ledger_ok && native_observation_seals),
    );
    checks.insert("freeze_links".to_owned(), Value::Bool(freeze_links));
''',
    "prospective-report-handoff-checks",
)

bind = Path("scripts/bind_native_state_runtime.sh")
replace_once(
    bind,
    '''# Bind predecessor State layers first because later freezes reference them.
bind_state v02
''',
    '''# Prove the immutable Python-era V0.2 ledger before the Rust handoff. The
# attestation snapshots file bytes; it never rewrites historical observations.
PYTHON="$REPO_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON=python3
"$PYTHON" scripts/attest_state_v02_legacy_ledger.py --data-root "$DATA_ROOT"

# Bind predecessor State layers first because later freezes reference them.
bind_state v02
''',
    "bind-script-attests-v02-legacy-ledger",
)
