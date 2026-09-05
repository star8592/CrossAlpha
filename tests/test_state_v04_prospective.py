from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from crossalpha.state import v04
from crossalpha.state.v04_integrity import strict_state_v04_integrity_report
from crossalpha.state import v04_prospective as prospective


FREEZE_TIME = pd.Timestamp("2026-09-05T04:00:00Z")
GENERATED = FREEZE_TIME + pd.Timedelta(minutes=1)


def _references(root: Path) -> None:
    for name, path in prospective._reference_paths(root).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"protocol": name, "frozen": True}), encoding="utf-8")


def _venue_rows() -> pd.DataFrame:
    rows = []
    for asset, base in (("BTC", 100_000.0), ("ETH", 4_000.0)):
        for index, venue in enumerate(v04.VENUES):
            spot = base * (1 + index * 0.0001)
            perp = spot * (1 + (index - 1) * 0.0001)
            rows.append(
                {
                    "protocol": v04.PROTOCOL,
                    "observed_at": GENERATED - pd.Timedelta(seconds=20 + index),
                    "known_at": GENERATED - pd.Timedelta(seconds=5),
                    "venue": venue,
                    "asset": asset,
                    "spot_symbol": f"{asset}USDT",
                    "perp_symbol": f"{asset}USDT",
                    "spot_bid": spot - 1,
                    "spot_ask": spot + 1,
                    "spot_mid": spot,
                    "spot_spread_bps": 0.2,
                    "perp_bid": perp - 1,
                    "perp_ask": perp + 1,
                    "perp_mid": perp,
                    "perp_spread_bps": 0.3,
                    "mark_price": perp,
                    "index_price": spot,
                    "basis_bps": (perp / spot - 1) * 10_000,
                    "mark_index_basis_bps": (perp / spot - 1) * 10_000,
                    "funding_semantics": v04.FUNDING_SEMANTICS,
                    "funding_rate_settled_raw": 0.0001 * (index + 1),
                    "funding_settlement_time": GENERATED - pd.Timedelta(hours=8),
                    "funding_interval_hours": 8.0,
                    "funding_rate_8h": 0.0001 * (index + 1),
                    "open_interest_usd": 1_000_000.0 * (index + 1),
                    "data_cost_usd": 0,
                }
            )
    return pd.DataFrame(rows)


def _raw(root: Path, asset: str, venue: str) -> dict[str, str]:
    payload = json.dumps({"asset": asset, "venue": venue}, sort_keys=True).encode()
    path = root / "raw" / f"{asset}_{venue}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as handle:
        handle.write(payload)
    return {
        "asset": asset,
        "venue": venue,
        "raw_path": str(path),
        "raw_sha256": hashlib.sha256(payload).hexdigest(),
        "raw_compressed_file_sha256": prospective.sha256_file(path),
    }


def _artifacts(root: Path) -> tuple[Path, Path]:
    venues = _venue_rows()
    venue_path = root / "derived" / "state" / "v04" / "venues.parquet"
    venue_path.parent.mkdir(parents=True, exist_ok=True)
    venues.to_parquet(venue_path, index=False)
    state = v04.compute_market_mechanics(venues, generated_at=GENERATED)
    mechanics_path = venue_path.with_name("mechanics.json")
    mechanics_path.write_text(
        json.dumps(
            {
                **state,
                "venue_snapshot_path": str(venue_path),
                "raw_records": [
                    _raw(root, asset, venue) for asset in v04.ASSETS for venue in v04.VENUES
                ],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return mechanics_path, venue_path


def test_v04_freeze_and_valid_live_record_are_strictly_audited(tmp_path: Path) -> None:
    _references(tmp_path)
    freeze = prospective.freeze_state_v04(tmp_path, now=FREEZE_TIME)
    mechanics, venues = _artifacts(tmp_path)
    record = prospective.write_live_observation(
        tmp_path,
        mechanics_path=mechanics,
        venue_path=venues,
        now=GENERATED + pd.Timedelta(seconds=10),
    )
    assert freeze["funding_semantics"] == v04.FUNDING_SEMANTICS
    assert record["status"] == "written"
    assert record["funding_semantics"] == v04.FUNDING_SEMANTICS
    report = strict_state_v04_integrity_report(tmp_path)
    assert report["ok"] is True, report
    assert report["checks"]["raw_payload_hash_links"] is True
    assert report["checks"]["raw_compressed_file_hash_links"] is True
    assert report["checks"]["settled_funding_semantics"] is True


def test_v04_rejects_stale_retrospective_write(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_state_v04(tmp_path, now=FREEZE_TIME)
    mechanics, venues = _artifacts(tmp_path)
    with pytest.raises(ValueError, match="stale"):
        prospective.write_live_observation(
            tmp_path,
            mechanics_path=mechanics,
            venue_path=venues,
            now=GENERATED + pd.Timedelta(minutes=10),
        )


def test_v04_raw_compressed_tamper_is_detected(tmp_path: Path) -> None:
    _references(tmp_path)
    prospective.freeze_state_v04(tmp_path, now=FREEZE_TIME)
    mechanics, venues = _artifacts(tmp_path)
    record = prospective.write_live_observation(
        tmp_path,
        mechanics_path=mechanics,
        venue_path=venues,
        now=GENERATED + pd.Timedelta(seconds=10),
    )
    raw_path = Path(record["raw_links"][0]["raw_path"])
    raw_path.write_bytes(raw_path.read_bytes() + b"tamper")
    report = strict_state_v04_integrity_report(tmp_path)
    assert report["ok"] is False
    assert report["checks"]["raw_compressed_file_hash_links"] is False
