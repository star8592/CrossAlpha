from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from crossalpha.catalog import build_catalog
from crossalpha.core.contracts import normalize_parent_futures_files
from crossalpha.core.databento_provider import (
    DatabentoCoreProvider,
    DatabentoRequest,
    ParentFuturesRequest,
)
from crossalpha.core.free_provider import FreeCoreProvider, FreeCoreRange
from crossalpha.data.quality import validate_ohlcv_parquet
from crossalpha.doctor import storage_report
from crossalpha.observatory.canonical.hyperliquid import canonicalize_hyperliquid
from crossalpha.observatory.canonical.stablecoins import canonicalize_stablecoins
from crossalpha.observatory.features.hyperliquid import build_hyperliquid_market_state
from crossalpha.observatory.features.stablecoins import build_stablecoin_state
from crossalpha.observatory.health import observatory_health, write_health_report
from crossalpha.observatory.live_health import observatory_live_health
from crossalpha.observatory.providers.defillama import DefiLlamaStablecoinProvider
from crossalpha.observatory.providers.hyperliquid import HyperliquidProvider
from crossalpha.observatory.query import latest_hyperliquid_market_state, latest_stablecoin_state
from crossalpha.settings import Settings
from crossalpha.storage.indexes import rebuild_manifest_indexes
from crossalpha.storage.raw import RawSnapshotStore


CORE_FUTURES_ROOTS = ("ES", "NQ", "GC", "SI", "HG", "CL", "BTC", "ETH")
CORE_CONTINUOUS_SYMBOLS = tuple(f"{root}.v.0" for root in CORE_FUTURES_ROOTS)


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
    stablecoin_state = build_stablecoin_state(settings.crossalpha_data_dir, recent_only=True)
    catalog = build_catalog(settings.crossalpha_data_dir)
    return {
        "mode": "incremental",
        "canonical_hyperliquid": canonical_hyperliquid,
        "canonical_stablecoins": canonical_stablecoins,
        "hyperliquid_market_state": market_state,
        "stablecoin_state": stablecoin_state,
        "catalog": catalog,
    }


def free_core_status(settings: Settings) -> dict[str, object]:
    return {
        "mode": "free_only",
        "required_data_cost_usd": 0,
        "tiingo_token_configured": bool(settings.tiingo_api_token),
        "fred_api_key_configured": bool(settings.fred_api_key),
        "binance_api_key_required": False,
        "ready": bool(settings.tiingo_api_token and settings.fred_api_key),
        "tradfi_source": "Tiingo Starter ($0 account)",
        "crypto_source": "Binance public market data (no API key)",
        "cash_source": "FRED (free API key)",
        "paid_vendor_required": False,
    }


def fetch_core_free(settings: Settings, start: str, end: str) -> dict[str, object]:
    if not settings.tiingo_api_token:
        raise SystemExit("TIINGO_API_TOKEN is missing in .env; Tiingo Starter is $0/month")
    if not settings.fred_api_key:
        raise SystemExit("FRED_API_KEY is missing in .env; FRED API keys are free")
    provider = FreeCoreProvider(
        tiingo_token=settings.tiingo_api_token,
        fred_api_key=settings.fred_api_key,
        timeout=settings.crossalpha_http_timeout,
    )
    return provider.fetch_all(
        FreeCoreRange(start=start, end=end),
        settings.crossalpha_data_dir,
    )


# --- Optional paid validation adapter below. Not required by V0.1. ---

def _require_databento(settings: Settings) -> DatabentoCoreProvider:
    if not settings.databento_api_key:
        raise SystemExit(
            "DATABENTO_API_KEY is missing in .env; Databento is optional paid validation only"
        )
    return DatabentoCoreProvider(settings.databento_api_key)


def _parent_request(start: str, end: str) -> ParentFuturesRequest:
    return ParentFuturesRequest(roots=CORE_FUTURES_ROOTS, start=start, end=end)


def _continuous_request(start: str, end: str) -> DatabentoRequest:
    return DatabentoRequest(symbols=CORE_CONTINUOUS_SYMBOLS, start=start, end=end)


def _safe_slug(value: str) -> str:
    return value.replace(":", "-").replace("/", "-").replace(" ", "_")


def _range_dir(start: str, end: str) -> Path:
    return Path(f"start={_safe_slug(start)}", f"end={_safe_slug(end)}")


def _parent_paths(data_root: Path, start: str, end: str) -> dict[str, Path]:
    range_dir = _range_dir(start, end)
    staging = data_root / "raw" / "databento" / "GLBX.MDP3" / "parent" / range_dir
    canonical = data_root / "canonical" / "core" / "futures_contract_daily" / range_dir
    return {
        "staging": staging,
        "definitions": staging / "parent_definitions.parquet",
        "bars": staging / "parent_ohlcv_1d.parquet",
        "canonical": canonical / "contracts_daily.parquet",
    }


def _continuous_paths(data_root: Path, start: str, end: str) -> dict[str, Path]:
    range_dir = _range_dir(start, end)
    staging = data_root / "raw" / "databento" / "GLBX.MDP3" / "continuous" / range_dir
    return {"staging": staging, "bars": staging / "continuous_daily.parquet"}


def estimate_core(settings: Settings, start: str, end: str) -> dict[str, object]:
    provider = _require_databento(settings)
    request = _continuous_request(start, end)
    cost = provider.estimate_cost(request)
    return {
        "mode": "optional_paid_validation",
        "dataset": request.dataset,
        "symbols": list(request.symbols),
        "start": start,
        "end": end,
        "schema": request.schema,
        "estimated_cost_usd": cost,
    }


def fetch_core(
    settings: Settings,
    start: str,
    end: str,
    *,
    max_cost_usd: float,
) -> dict[str, object]:
    if max_cost_usd < 0:
        raise SystemExit("--max-cost-usd must be non-negative")
    provider = _require_databento(settings)
    request = _continuous_request(start, end)
    paths = _continuous_paths(settings.crossalpha_data_dir, start, end)

    estimated_cost = 0.0
    fetched = False
    if not paths["bars"].exists():
        estimated_cost = provider.estimate_cost(request)
        if estimated_cost > max_cost_usd:
            raise SystemExit(
                f"estimated cost ${estimated_cost:.6f} exceeds --max-cost-usd ${max_cost_usd:.6f}; no paid download started"
            )
        provider.fetch_continuous_daily(request, paths["staging"])
        fetched = True

    quality = validate_ohlcv_parquet(paths["bars"])
    if not quality.ok:
        raise SystemExit("QUALITY GATE FAILED")
    return {
        "mode": "optional_paid_validation",
        "dataset": request.dataset,
        "symbols": list(request.symbols),
        "start": start,
        "end": end,
        "max_cost_usd": max_cost_usd,
        "estimated_missing_cost_usd": estimated_cost,
        "fetched": fetched,
        "bars": str(paths["bars"]),
        "quality_ok": True,
    }


def estimate_core_parent(settings: Settings, start: str, end: str) -> dict[str, object]:
    provider = _require_databento(settings)
    request = _parent_request(start, end)
    definition_cost = provider.estimate_parent_cost(request, schema="definition")
    daily_cost = provider.estimate_parent_cost(request, schema="ohlcv-1d")
    return {
        "mode": "optional_paid_validation",
        "dataset": request.dataset,
        "symbols": list(request.symbols),
        "start": start,
        "end": end,
        "definition_cost_usd": definition_cost,
        "ohlcv_1d_cost_usd": daily_cost,
        "total_estimated_cost_usd": definition_cost + daily_cost,
    }


def fetch_core_parent(
    settings: Settings,
    start: str,
    end: str,
    *,
    max_cost_usd: float,
) -> dict[str, object]:
    if max_cost_usd < 0:
        raise SystemExit("--max-cost-usd must be non-negative")
    provider = _require_databento(settings)
    request = _parent_request(start, end)
    paths = _parent_paths(settings.crossalpha_data_dir, start, end)

    missing: list[tuple[str, str]] = []
    if not paths["definitions"].exists():
        missing.append(("definition", "definitions"))
    if not paths["bars"].exists():
        missing.append(("ohlcv-1d", "bars"))

    estimates: dict[str, float] = {}
    for schema, label in missing:
        estimates[label] = provider.estimate_parent_cost(request, schema=schema)
    missing_cost = sum(estimates.values())
    if missing_cost > max_cost_usd:
        raise SystemExit(
            f"estimated missing-download cost ${missing_cost:.6f} exceeds --max-cost-usd ${max_cost_usd:.6f}; no paid download started"
        )

    fetched: list[str] = []
    if not paths["definitions"].exists():
        provider.fetch_parent_definitions(request, paths["staging"])
        fetched.append("definitions")
    if not paths["bars"].exists():
        provider.fetch_parent_daily(request, paths["staging"])
        fetched.append("ohlcv-1d")

    normalized = normalize_parent_futures_files(
        paths["definitions"],
        paths["bars"],
        paths["canonical"],
    )
    return {
        "mode": "optional_paid_validation",
        "dataset": request.dataset,
        "symbols": list(request.symbols),
        "start": start,
        "end": end,
        "max_cost_usd": max_cost_usd,
        "estimated_missing_cost_usd": missing_cost,
        "estimated_components_usd": estimates,
        "fetched": fetched,
        "definitions": str(paths["definitions"]),
        "bars": str(paths["bars"]),
        "canonical": str(normalized),
    }


def normalize_core_parent(settings: Settings, start: str, end: str) -> dict[str, str]:
    paths = _parent_paths(settings.crossalpha_data_dir, start, end)
    for key in ("definitions", "bars"):
        if not paths[key].exists():
            raise SystemExit(f"missing parent staging file: {paths[key]}")
    out = normalize_parent_futures_files(paths["definitions"], paths["bars"], paths["canonical"])
    return {"canonical": str(out)}


def _add_range(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", default="2010-06-01")
    parser.add_argument("--end", required=True, help="Explicit end date/timestamp for reproducibility.")


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
    sub.add_parser("build-stablecoin-state")
    sub.add_parser("materialize-observatory")
    sub.add_parser("build-catalog")

    state = sub.add_parser("market-state")
    state.add_argument("--asset", action="append", dest="assets")

    stable = sub.add_parser("stablecoin-state")
    stable.add_argument("--top-chains", type=int, default=10)

    sub.add_parser("free-core-status")

    free_core = sub.add_parser("fetch-core-free")
    _add_range(free_core)

    # Optional paid validation commands. They are not part of the free-only V0.1 path.
    estimate = sub.add_parser("estimate-core")
    _add_range(estimate)

    core = sub.add_parser("fetch-core")
    _add_range(core)
    core.add_argument("--max-cost-usd", type=float, required=True)

    estimate_parent = sub.add_parser("estimate-core-parent")
    _add_range(estimate_parent)

    fetch_parent = sub.add_parser("fetch-core-parent")
    _add_range(fetch_parent)
    fetch_parent.add_argument("--max-cost-usd", type=float, required=True)

    normalize_parent = sub.add_parser("normalize-core-parent")
    _add_range(normalize_parent)

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
    elif args.command == "build-stablecoin-state":
        print(json.dumps(build_stablecoin_state(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "materialize-observatory":
        print(json.dumps(materialize_observatory(settings), ensure_ascii=False, indent=2))
    elif args.command == "build-catalog":
        print(json.dumps(build_catalog(settings.crossalpha_data_dir), ensure_ascii=False, indent=2))
    elif args.command == "market-state":
        rows = latest_hyperliquid_market_state(settings.crossalpha_data_dir, args.assets)
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    elif args.command == "stablecoin-state":
        report = latest_stablecoin_state(settings.crossalpha_data_dir, top_chains=args.top_chains)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif args.command == "free-core-status":
        print(json.dumps(free_core_status(settings), ensure_ascii=False, indent=2))
    elif args.command == "fetch-core-free":
        print(json.dumps(fetch_core_free(settings, args.start, args.end), ensure_ascii=False, indent=2))
    elif args.command == "estimate-core":
        print(json.dumps(estimate_core(settings, args.start, args.end), ensure_ascii=False, indent=2))
    elif args.command == "fetch-core":
        print(
            json.dumps(
                fetch_core(settings, args.start, args.end, max_cost_usd=args.max_cost_usd),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "estimate-core-parent":
        print(json.dumps(estimate_core_parent(settings, args.start, args.end), ensure_ascii=False, indent=2))
    elif args.command == "fetch-core-parent":
        print(
            json.dumps(
                fetch_core_parent(
                    settings,
                    args.start,
                    args.end,
                    max_cost_usd=args.max_cost_usd,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "normalize-core-parent":
        print(json.dumps(normalize_core_parent(settings, args.start, args.end), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        report = storage_report(settings.crossalpha_data_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ok"]:
            raise SystemExit("STORAGE DOCTOR FAILED")


if __name__ == "__main__":
    main()
