from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # V0.1 required data path: free-only.
    tiingo_api_token: str | None = None
    fred_api_key: str | None = None

    # Optional paid validation adapter only; never required for V0.1.
    databento_api_key: str | None = None

    evm_rpc_url: str | None = None
    crossalpha_data_dir: Path = Path("./data")
    crossalpha_http_timeout: float = 30.0

    def ensure_dirs(self) -> None:
        for name in ("raw", "canonical", "derived", "manifests", "research", "archive", "catalog"):
            (self.crossalpha_data_dir / name).mkdir(parents=True, exist_ok=True)
