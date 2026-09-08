from __future__ import annotations

from pathlib import Path

path = Path("src/crossalpha/state/v03_cycle.py")
text = path.read_text(encoding="utf-8")

old_import = '''from crossalpha.state.v03_logs import (
    BLOCKSCOUT_LOG_SOURCE,
    BlockscoutBorrowLogProvider,
    BorrowLogPolicy,
)
'''
new_import = '''from crossalpha.state.v03_logs import BorrowLogPolicy, FailoverBorrowLogProvider
'''
if new_import not in text:
    if old_import not in text:
        raise SystemExit("v03 cycle log-provider import marker not found")
    text = text.replace(old_import, new_import, 1)

old_init = '''    log_provider = BlockscoutBorrowLogProvider(
        policy=BorrowLogPolicy(timeout_seconds=settings.crossalpha_http_timeout)
    )
'''
new_init = '''    log_provider = FailoverBorrowLogProvider(
        settings.evm_rpc_url,
        policy=BorrowLogPolicy(timeout_seconds=settings.crossalpha_http_timeout),
    )
'''
if new_init not in text:
    if old_init not in text:
        raise SystemExit("v03 cycle log-provider init marker not found")
    text = text.replace(old_init, new_init, 1)

text = text.replace(
    '"State V0.3 indexed Borrow-log source unavailable: "',
    '"State V0.3 Borrow-log sources unavailable: "',
)
text = text.replace('"borrow_log_source": BLOCKSCOUT_LOG_SOURCE,', '"borrow_log_source": log_provider.selected_source,')
text = text.replace('state["borrow_log_source"] = BLOCKSCOUT_LOG_SOURCE', 'state["borrow_log_source"] = log_provider.selected_source')

probe_marker = '''    except Exception as exc:
        raise RuntimeError(
            "State V0.3 Borrow-log sources unavailable: "
            f"{type(exc).__name__}"
        ) from exc

    # The state RPC now needs only finalized-state capabilities, not archive logs.
'''
probe_new = '''    except Exception as exc:
        raise RuntimeError(
            "State V0.3 Borrow-log sources unavailable: "
            f"{type(exc).__name__}"
        ) from exc
    if log_provider.selected_source is None:
        raise RuntimeError("State V0.3 Borrow-log provider selected no auditable source")

    # The state RPC now needs only finalized-state capabilities, not archive logs.
'''
if probe_new not in text:
    if probe_marker not in text:
        raise SystemExit("v03 cycle selected-source marker not found")
    text = text.replace(probe_marker, probe_new, 1)

# Add source-failure audit metadata next to each selected source, but never endpoint URLs/messages.
needle = '"borrow_log_source": log_provider.selected_source,\n'
replacement = (
    '"borrow_log_source": log_provider.selected_source,\n'
    '                "borrow_log_candidate_failures_before_selection": dict(log_provider.candidate_failures),\n'
)
# Envelope block has 16-space indentation after opening quote; replace first occurrence only with exact indentation.
envelope_needle = '                "borrow_log_source": log_provider.selected_source,\n                "state_rpc_source": state_rpc_source,'
envelope_repl = '                "borrow_log_source": log_provider.selected_source,\n                "borrow_log_candidate_failures_before_selection": dict(log_provider.candidate_failures),\n                "state_rpc_source": state_rpc_source,'
if envelope_repl not in text:
    if envelope_needle not in text:
        raise SystemExit("v03 envelope source metadata marker not found")
    text = text.replace(envelope_needle, envelope_repl, 1)

common_needle = '        "borrow_log_source": log_provider.selected_source,\n        "state_rpc_source": state_rpc_source,'
common_repl = '        "borrow_log_source": log_provider.selected_source,\n        "borrow_log_candidate_failures_before_selection": dict(log_provider.candidate_failures),\n        "state_rpc_source": state_rpc_source,'
if common_repl not in text:
    if common_needle not in text:
        raise SystemExit("v03 common source metadata marker not found")
    text = text.replace(common_needle, common_repl, 1)

state_marker = '    state["borrow_log_source"] = log_provider.selected_source\n    state["state_rpc_source"] = state_rpc_source\n'
state_repl = '    state["borrow_log_source"] = log_provider.selected_source\n    state["borrow_log_candidate_failures_before_selection"] = dict(log_provider.candidate_failures)\n    state["state_rpc_source"] = state_rpc_source\n'
if state_repl not in text:
    if state_marker not in text:
        raise SystemExit("v03 state source metadata marker not found")
    text = text.replace(state_marker, state_repl, 1)

path.write_text(text, encoding="utf-8")
print("patched=src/crossalpha/state/v03_cycle.py")
