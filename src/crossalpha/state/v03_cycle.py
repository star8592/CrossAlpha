from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from crossalpha.domain.models import ObservationEnvelope, SourceType
from crossalpha.settings import Settings
from crossalpha.state.v03 import CensusPolicy, compute_borrower_census
from crossalpha.state.v03_prospective import freeze_path, write_full_census_observation
from crossalpha.state.v03_rpc import (
    AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    AAVE_V3_ETHEREUM_CORE_POOL,
    AaveBorrowerRpc,
    RpcPolicy,
    borrow_log_debtor,
    resolve_rpc_url,
)
from crossalpha.state.v03_watchlist import compute_watchlist_snapshot
from crossalpha.storage.raw import RawSnapshotStore


FINALITY_LAG_BLOCKS = 64
BOOTSTRAP_CHUNK_BLOCKS = 25_000
MAX_BOOTSTRAP_CHUNKS_PER_CYCLE = 8
ADAPTIVE_MINIMUM_SPAN_BLOCKS = 256
FULL_CENSUS_CADENCE_MINUTES = 360
WATCHLIST_CADENCE_MINUTES = 15


def _research_root(data_root: Path) -> Path:
    return data_root / "research" / "state_v03"


def _bootstrap_state_path(data_root: Path) -> Path:
    return _research_root(data_root) / "bootstrap_state.json"


def _universe_path(data_root: Path) -> Path:
    return data_root / "derived" / "state" / "v03" / "borrower_universe.parquet"


def _watchlist_path(data_root: Path) -> Path:
    return data_root / "derived" / "state" / "v03" / "watchlist.parquet"


def _snapshot_dir(data_root: Path, captured: pd.Timestamp, scope: str) -> Path:
    return (
        data_root
        / "derived"
        / "state"
        / "v03"
        / scope
        / f"year={captured:%Y}"
        / f"month={captured:%m}"
        / f"day={captured:%d}"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_state(data_root: Path) -> dict[str, Any]:
    path = _bootstrap_state_path(data_root)
    if not path.exists():
        return {
            "schema_version": 1,
            "next_block": AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
            "last_scanned_block": None,
            "bootstrap_complete": False,
            "last_valid_full_census_at": None,
            "last_valid_full_census_block": None,
            "pending_new_borrowers_since_full": [],
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("pending_new_borrowers_since_full", [])
    return state


def _write_state(data_root: Path, state: dict[str, Any]) -> None:
    path = _bootstrap_state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_addresses(path: Path) -> set[str]:
    if not path.exists():
        return set()
    frame = pd.read_parquet(path, columns=["address"])
    return set(frame["address"].dropna().astype(str).str.lower())


def _write_addresses(path: Path, addresses: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"address": sorted(addresses)})
    tmp = path.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


async def _adaptive_borrow_logs(
    rpc: AaveBorrowerRpc,
    start: int,
    end: int,
    *,
    minimum_span: int = ADAPTIVE_MINIMUM_SPAN_BLOCKS,
) -> list[dict[str, Any]]:
    try:
        return await rpc.borrow_logs(start, end)
    except Exception:
        if end - start + 1 <= minimum_span:
            raise
        midpoint = (start + end) // 2
        left = await _adaptive_borrow_logs(rpc, start, midpoint, minimum_span=minimum_span)
        right = await _adaptive_borrow_logs(rpc, midpoint + 1, end, minimum_span=minimum_span)
        return left + right


def _full_census_due(state: dict[str, Any], now: pd.Timestamp) -> bool:
    raw = state.get("last_valid_full_census_at")
    if not raw:
        return True
    previous = pd.Timestamp(raw)
    if previous.tzinfo is None:
        previous = previous.tz_localize("UTC")
    else:
        previous = previous.tz_convert("UTC")
    return now - previous >= pd.Timedelta(minutes=FULL_CENSUS_CADENCE_MINUTES)


def _write_census_artifacts(
    data_root: Path,
    rows: pd.DataFrame,
    summary: dict[str, Any],
    *,
    captured: pd.Timestamp,
    scope: str,
) -> dict[str, str]:
    directory = _snapshot_dir(data_root, captured, scope)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = captured.strftime("%H%M%S%f")
    detail_path = directory / f"accounts_at={stamp}.parquet"
    summary_path = directory / f"summary_at={stamp}.json"
    detail_tmp = detail_path.with_suffix(".parquet.tmp")
    rows.to_parquet(detail_tmp, index=False)
    detail_tmp.replace(detail_path)
    payload = {
        **summary,
        "detail_path": str(detail_path),
        "detail_sha256": _sha256(detail_path),
    }
    summary_tmp = summary_path.with_suffix(".json.tmp")
    summary_tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary_tmp.replace(summary_path)
    return {
        "detail": str(detail_path),
        "detail_sha256": payload["detail_sha256"],
        "summary": str(summary_path),
        "summary_sha256": _sha256(summary_path),
    }


async def run_state_v03_cycle(settings: Settings) -> dict[str, Any]:
    """Advance the borrower universe and record point-in-time borrower risk facts."""
    settings.ensure_dirs()
    data_root = settings.crossalpha_data_dir
    scan_started_at = pd.Timestamp(datetime.now(timezone.utc))
    rpc_url, rpc_source = resolve_rpc_url(settings.evm_rpc_url)
    rpc = AaveBorrowerRpc(
        rpc_url,
        policy=RpcPolicy(batch_size=100, timeout_seconds=settings.crossalpha_http_timeout),
    )
    latest_block = await rpc.latest_block()
    finalized_block = max(latest_block - FINALITY_LAG_BLOCKS, AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK)
    state = _load_state(data_root)
    universe_path = _universe_path(data_root)
    borrowers = _load_addresses(universe_path)
    pending_new_borrowers = set(
        str(value).lower() for value in state.get("pending_new_borrowers_since_full", [])
    )
    store = RawSnapshotStore(data_root)

    next_block = max(
        int(state.get("next_block", AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK)),
        AAVE_V3_ETHEREUM_DEPLOYMENT_BLOCK,
    )
    scanned_ranges: list[dict[str, Any]] = []
    for _ in range(MAX_BOOTSTRAP_CHUNKS_PER_CYCLE):
        if next_block > finalized_block:
            break
        end = min(next_block + BOOTSTRAP_CHUNK_BLOCKS - 1, finalized_block)
        logs = await _adaptive_borrow_logs(rpc, next_block, end)
        debtors = {address for row in logs if (address := borrow_log_debtor(row)) is not None}
        previously_known = set(borrowers)
        new_debtors = debtors - previously_known
        borrowers.update(debtors)
        if bool(state.get("bootstrap_complete")):
            pending_new_borrowers.update(new_debtors)
        observed = pd.Timestamp(datetime.now(timezone.utc))
        envelope = ObservationEnvelope(
            observed_at=observed.to_pydatetime(),
            known_at=observed.to_pydatetime(),
            source_type=SourceType.CHAIN,
            source_id="aave:v3:ethereum:borrowers",
            observation_type="borrow_logs_chunk",
            payload=logs,
            metadata={
                "pool_address": AAVE_V3_ETHEREUM_CORE_POOL,
                "from_block": next_block,
                "to_block": end,
                "log_count": len(logs),
                "new_candidate_count_in_chunk": len(new_debtors),
                "historical_bootstrap_is_evidence": False,
                "rpc_source": rpc_source,
                "data_cost_usd": 0,
            },
        )
        manifest = store.write(envelope)
        _write_addresses(universe_path, borrowers)
        scanned_ranges.append(
            {
                "from_block": next_block,
                "to_block": end,
                "log_count": len(logs),
                "new_candidate_count": len(new_debtors),
                "candidate_count": len(borrowers),
                "raw_sha256": manifest.sha256,
            }
        )
        state["last_scanned_block"] = end
        state["next_block"] = end + 1
        state["pending_new_borrowers_since_full"] = sorted(pending_new_borrowers)
        next_block = end + 1
        _write_state(data_root, state)

    caught_up = next_block > finalized_block
    state["bootstrap_complete"] = bool(caught_up)
    state["candidate_address_count"] = len(borrowers)
    state["latest_seen_block"] = latest_block
    state["latest_finalized_block"] = finalized_block
    state["rpc_source"] = rpc_source
    state["pending_new_borrowers_since_full"] = sorted(pending_new_borrowers)
    _write_state(data_root, state)

    common = {
        "protocol": "CROSSALPHA_STATE_V0_3_CYCLE",
        "actionability": "DESCRIPTIVE_ONLY",
        "risk_multiplier": None,
        "data_cost_usd": 0,
        "rpc_source": rpc_source,
        "latest_block": latest_block,
        "finalized_block": finalized_block,
        "candidate_address_count": len(borrowers),
        "pending_new_borrower_count": len(pending_new_borrowers),
        "mutates_v01_or_v02": False,
    }

    if not caught_up:
        return {
            **common,
            "status": "BORROWER_UNIVERSE_BOOTSTRAPPING",
            "next_block": next_block,
            "scanned_ranges": scanned_ranges,
            "historical_bootstrap_is_evidence": False,
        }

    decision_now = pd.Timestamp(datetime.now(timezone.utc))
    if _full_census_due(state, decision_now):
        accounts = await rpc.account_data(sorted(borrowers), block_number=finalized_block)
        census_captured = pd.Timestamp(datetime.now(timezone.utc))
        summary = compute_borrower_census(
            accounts,
            total_candidate_addresses=len(borrowers),
            bootstrap_complete=True,
            block_number=finalized_block,
            captured_at=census_captured,
            policy=CensusPolicy(),
        )
        artifacts = _write_census_artifacts(
            data_root,
            accounts,
            summary,
            captured=census_captured,
            scope="full_census",
        )
        prospective: dict[str, Any] = {"status": "not_frozen_no_prospective_write"}
        if summary.get("valid_full_census"):
            watchlist = set(str(value).lower() for value in summary.get("watchlist_addresses", []))
            _write_addresses(_watchlist_path(data_root), watchlist)
            if freeze_path(data_root).exists():
                prospective = write_full_census_observation(
                    data_root,
                    summary_path=Path(artifacts["summary"]),
                    detail_path=Path(artifacts["detail"]),
                    known_at=pd.Timestamp(datetime.now(timezone.utc)),
                )
            state["last_valid_full_census_at"] = census_captured.isoformat()
            state["last_valid_full_census_block"] = finalized_block
            state["last_valid_full_census_summary"] = artifacts["summary"]
            state["last_valid_full_census_summary_sha256"] = artifacts["summary_sha256"]
            state["pending_new_borrowers_since_full"] = []
            pending_new_borrowers.clear()
            _write_state(data_root, state)
        return {
            **common,
            "status": (
                "FULL_CENSUS_RECORDED"
                if summary.get("valid_full_census")
                else "FULL_CENSUS_PARTIAL_RETRY_REQUIRED"
            ),
            "scan_started_at": scan_started_at.isoformat(),
            "scanned_ranges": scanned_ranges,
            "census": summary,
            "artifacts": artifacts,
            "prospective": prospective,
        }

    watchlist = _load_addresses(_watchlist_path(data_root)) | pending_new_borrowers
    if not watchlist:
        return {**common, "status": "CAUGHT_UP_AWAITING_NEXT_FULL_CENSUS"}
    accounts = await rpc.account_data(sorted(watchlist), block_number=finalized_block)
    watch_captured = pd.Timestamp(datetime.now(timezone.utc))
    watch = compute_watchlist_snapshot(
        accounts,
        expected_addresses=len(watchlist),
        block_number=finalized_block,
        captured_at=watch_captured,
    )
    watch["includes_pending_new_borrowers"] = bool(pending_new_borrowers)
    watch["pending_new_borrower_count"] = len(pending_new_borrowers)
    artifacts = _write_census_artifacts(
        data_root,
        accounts,
        watch,
        captured=watch_captured,
        scope="watchlist",
    )
    return {
        **common,
        "status": "WATCHLIST_RECORDED",
        "watchlist": watch,
        "artifacts": artifacts,
    }
