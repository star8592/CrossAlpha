# CrossAlpha Codex Instructions

## Mission

CrossAlpha is a cross-asset systematic macro research and point-in-time capital-state observatory. Treat this repository as research infrastructure with strict reproducibility and production-data boundaries.

When asked to repair CI, make the smallest evidence-backed code change that fixes the captured failure. Do not redesign unrelated subsystems.

## Mandatory preflight

Before editing files:

1. Read this file and the relevant source/tests/workflow files.
2. Inspect the current branch, diff, and failing CI evidence.
3. Confirm the working directory is not `/mnt/disk2/CrossAlpha` and do not access `/mnt/disk2/CrossAlphaData`.
4. Never assume network credentials are available.
5. Prefer deterministic/local fixtures and repository data over live external APIs.

## Hard safety boundaries

Never do any of the following unless a human explicitly authorizes that specific change:

- access, read, copy, mutate, or probe production data under `/mnt/disk2/CrossAlphaData`;
- use or expose production credentials, API tokens, passwords, private keys, exchange secrets, or wallet secrets;
- enable production writes, live trading, order placement, fund movement, or destructive data operations;
- weaken, delete, skip, or bypass tests, CI gates, assertions, invariants, frozen contracts, integrity checks, or safety checks merely to make CI green;
- loosen numeric tolerances, thresholds, strategy parameters, risk limits, research decision criteria, or data-quality gates without explicit human approval;
- change a failing test's expected result unless the existing expectation is proven wrong by repository evidence;
- auto-merge a pull request, force-push shared branches, rewrite history, or delete branches;
- fabricate missing market/research data or substitute unapproved data sources.

If a correct repair appears to require any prohibited action, stop and report the exact blocker instead of bypassing it.

## CI repair policy

For CI failures:

1. Identify the first root-cause failure from logs, not downstream cascade errors.
2. Reproduce locally when possible.
3. Patch only the evidence-backed defect.
4. Add or update a regression test when the defect is not already covered.
5. Run formatting/lint/build/tests relevant to the touched code.
6. Run the full deterministic qualification gates before declaring completion when the environment can support them.
7. Report remaining failures explicitly; never call a partial repair complete.

Do not change CI configuration to hide a source-code failure. CI changes are allowed only when the workflow itself is demonstrably defective, and the reason must be documented.

## Definition of done

The canonical deterministic gates are:

```bash
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
pytest -q
cargo build --workspace
python scripts/run_cloud_ci_parity.py
cargo build --workspace --release
```

A repair is complete only when all applicable gates pass, or when an external/environmental blocker is documented with exact evidence.

The self-hosted GitHub Actions workflow `.github/workflows/rust-ci.yml` is the authoritative integration qualification for PRs into `main`.

## Environment setup

For Codex Cloud, run:

```bash
bash scripts/codex_cloud_setup.sh
```

This setup installs only development dependencies and Rust tooling. It must not inject production secrets or mount production data.

## Code quality

- Rust: keep `cargo fmt` clean and Clippy warning-free under `-D warnings`.
- Python: keep tests deterministic; use existing project style and Ruff-compatible formatting conventions.
- Prefer explicit errors and typed boundaries over silent fallback behavior.
- Preserve point-in-time semantics, deterministic ordering, immutable/frozen artifacts, and provenance fields.
- Avoid opportunistic refactors in CI repair tasks.

## Pull-request behavior

When Codex proposes a change:

- summarize the root cause, files changed, and exact verification performed;
- state clearly whether all deterministic gates passed;
- call out any test/gate not executed;
- do not merge automatically;
- do not mark a task successful solely because compilation progressed further.
