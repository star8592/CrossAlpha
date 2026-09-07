from __future__ import annotations

from pathlib import Path


# This repair has already landed in the branch.  The focused workflow reruns this
# helper on every owner-authored validation commit, so the helper must be robust
# to rustfmt changing line breaks/indentation.  Validate semantic sentinels rather
# than trying to match a previously formatted multi-line replacement verbatim.
EXPECTED = {
    Path("crates/crossalpha-state/src/v02/runtime_binding.rs"): (
        "verify_attestation_for_binding",
        '"legacy_python_integrity_attestation"',
        '"state_v02_legacy_binding"',
        "crates/crossalpha-state/src/v02/legacy_binding.rs",
    ),
    Path("crates/crossalpha-state/src/v02/prospective.rs"): (
        "binding_bound_at",
        "verify_bound_legacy_ledger",
        "legacy_python_ledger_immutable",
        "native_observation_seals",
        "rust_runtime_binding_record_sha256",
        "rust_runtime_binding_file_sha256",
    ),
    Path("scripts/bind_native_state_runtime.sh"): (
        "attest_state_v02_legacy_ledger.py",
        "bind_state v02",
    ),
}

missing: list[str] = []
for path, markers in EXPECTED.items():
    text = path.read_text(encoding="utf-8")
    for marker in markers:
        if marker not in text:
            missing.append(f"{path}:{marker}")

if missing:
    raise SystemExit(
        "V02 binding-boundary repair is incomplete; missing semantic markers:\n  - "
        + "\n  - ".join(missing)
    )

for path in EXPECTED:
    print(f"already_patched={path}")
print("v02_binding_boundary_patch=SEMANTICALLY_PRESENT")
