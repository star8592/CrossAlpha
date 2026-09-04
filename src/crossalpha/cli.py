from __future__ import annotations

import argparse
import asyncio
import json

from crossalpha.catalog import build_catalog
from crossalpha.core.databento_provider import DatabentoCoreProvider, DatabentoRequest
from crossalpha.data.quality import validate_ohlcv_parquet
from crossalpha.doctor import storage_report
from crossalpha.observatory.canonical.hyperliquid import canonicalize_hyperliquid
from crossalpha.observatory.canonical.stablecoins import canonicalize_stablecoins
from crossalpha.observatory.features.hyperliquid import build_hyperliquid_market_state
from crossalpha.observatory.health import observatory_health, write_health_report
from crossalpha.observatory.live_health import observatory_live_health
from crossalpha.observatory.providers.defillama import DefiLlamaStablecoinProvider
from crossalpha.observatory.providers.hyperliquid import HyperliquidProvider
from crossalpha.observatory.query import latest_hyperliquid_market_state
from crossalpha.settings import Settings
from crossalpha.storage.indexes import rebuild_manifest_indexes
from crossalpha.storage.raw import RawSnapshotStore


async def collect_observatory(settings: Settings, sources: list[str]) -> None:
    store = RawSnapshotStore(settings.crossalpha_data_dir)
    providers = []
    if "hyperliquid" in sources:
        providers.append(HyperliquidProvider(settings.crossalpha_http_timeout))
    if "defillama" in sources:
        providers.append(DefiLlamaStablecoinProvider(settings.crossalpha_http_timeout))
    for provider in providers:
        for envelope in await provider.collect():
            manifest = store.write(envelope)
            print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False))


def materialize_observatory(settings: Settings) -> dict[str, object]:
    canonical_hyperliquid = canonicalize_hyperliquid(settings.crossalpha_data_dir, recent_days=2)
    canonical_stablecoins = canonicalize_stablecoins(settings.crossalpha_data_dir, recent_days=2)
    market_state = build_hyperliquid_market_state(settings.crossalpha_data_dir, recent_only=True)
    catalog = build_catalog(settings.crossalpha_data_dir)
    return {
        "mode": "incremental",
        "canonical_hyperliquid": canonical_hyperliquid,
        "canonical_stablecoins": canonical_stablecoins,
        "hyperliquid_market_state": market_state,
        "catalog": catalog,
    }


def fetch_core(settings: Settings, start: str, end: str | None) -> None:
    if not settings.databento_api_key:
        raise SystemExit("DATABENTO_API_KEY is missing in .env")
    provider = DatabentoCoreProvider(settings.databento_api_key)
    request = DatabentoRequest(
        symbols=("ES.v.0", "NQ.v.0", "GC.v.0", "SI.v.0", "HG.v.0", "CL.v.0", "BTC.v.0", "ETH.v.0"),
        start=start,
        end=end,
    )
    out = provider.fetch_continuous_daily(
        request,
        settings.crossalpha_data_dir / "raw" / "databento" / "GLBX.MDP3",
    )
    quality = validate_ohlcv_parquet(out)
    print(f"saved={out}")
    print(f"quality={quality}")
    if not quality.ok:
        raise SystemExit("QUALITY GATE FAILED")


def main() -> None:
    parser = argparse.ArgumentParser(prog="crossalpha")
    sub = parser.add_subparsers(dest="command", required=True)

    obs = sub.add_parser("collect-observatory")
    obs.add_argument("--source", action="append", choices=["hyperliquid", "defillama"], dest="sources")

    health = sub.add_parser("observatory-health")
    health.add_argument("--expected-interval", type=int, default=300)
    health.add_argument("--stale-after", type=int, default=900)
    health.add_argument("--no-verify-latest", action="store_true")

    live_health = sub.add_parser("observatory-live-health")
    live_health.add_argument("--stale-after", type=int, default=900)
    live_health.add_argument("--no-verify-latest", action="store_true")

    sub.add_parser("manifest-rebuild-indexes")
    sub.add_parser("canonicalize-hyperliquid")
    sub.add_parser("canonicalize-stablecoins")
    sub.add_parser("build-market-state")
    sub.add_parser("materialize-observatory")
    sub.add_parser("build-catalog")

    state = sub.add_parser("market-state")
    state.add_argument("--asset", action="append", dest="assets")

    core = sub.add_parser("fetch-core")
    core.add_argument("--start", default="2010-06-01")
    core.add_argument("--end", default=None)

    sub.add_parser("doctor")

    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()

    if args.command == "collect-observatory":
        asyncio.run(collect_observatory(settings, args.sources or ["hyperliquid", "defillama"]))
    elif args.command == "observatory-health":
        report = observatory_health(
            settings.crossalpha_data_dir,
            expected_interval_seconds=args.expected_interval,
            stale_after_seconds=args.stale_after,
            verify_latest=not args.no_verify_latest,
        )
        write_health_report(settings.crossalpha_data_dir, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit("OBSERVATORY HEALTH FAILED")
    elif args.command == "observatory-live-health":
        report = observatory_live_health(
            settings.crossalpha_data_dir,
            stale_after_seconds=args.stale_after,
            verify_latest=not args.no_verify_latest,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit("OBSERVATORY LIVE HEALTH FAILED")
    elif args.command == "manifest-rebuild-indexes":
        print(json.dumps(rebuild_manifest_indexes(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "canonicalize-hyperliquid":
        print(json.dumps(canonicalize_hyperliquid(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "canonicalize-stablecoins":
        print(json.dumps(canonicalize_stablecoins(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "build-market-state":
        print(json.dumps(build_hyperliquid_market_state(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "materialize-observatory":
        print(json.dumps(materialize_observatory(settings), ensure_ascii=False, indent=2))
    elif args.command == "build-catalog":
        print(json.dumps(build_catalog(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "market-state":
        rows = latest_hyperliquid_market_state(settings.crossalpha_data_dir, args.assets)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    elif args.command == "fetch-core":
        fetch_core(settings, args.start, args.end)
    elif args.command == "doctor":
        report = storage_report(settings.crossalpha_data_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit("STORAGE DOCTOR FAILED")


if __name__ == "__main__":
    main()
