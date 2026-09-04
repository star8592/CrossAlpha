from __future__ import annotations

import argparse
import asyncio
import json

from crossalpha.core.databento_provider import DatabentoCoreProvider, DatabentoRequest
from crossalpha.data.quality import validate_ohlcv_parquet
from crossalpha.observatory.providers.defillama import DefiLlamaStablecoinProvider
from crossalpha.observatory.providers.hyperliquid import HyperliquidProvider
from crossalpha.settings import Settings
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


def fetch_core(settings: Settings, start: str, end: str | None) -> None:
    if not settings.databento_api_key:
        raise SystemExit("DATABENTO_API_KEY is missing in .env")
    provider = DatabentoCoreProvider(settings.databento_api_key)
    request = DatabentoRequest(symbols=("ES.v.0", "NQ.v.0", "GC.v.0", "SI.v.0", "HG.v.0", "CL.v.0", "BTC.v.0", "ETH.v.0"), start=start, end=end)
    out = provider.fetch_continuous_daily(request, settings.crossalpha_data_dir / "raw" / "databento" / "GLBX.MDP3")
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
    core = sub.add_parser("fetch-core")
    core.add_argument("--start", default="2010-06-01")
    core.add_argument("--end", default=None)
    args = parser.parse_args()
    settings = Settings()
    settings.ensure_dirs()
    if args.command == "collect-observatory":
        asyncio.run(collect_observatory(settings, args.sources or ["hyperliquid", "defillama"]))
    elif args.command == "fetch-core":
        fetch_core(settings, args.start, args.end)


if __name__ == "__main__":
    main()
