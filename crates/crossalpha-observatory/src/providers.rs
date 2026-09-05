use anyhow::Context;
use anyhow::Result;
use anyhow::bail;
use chrono::Utc;
use crossalpha_storage::ObservationEnvelope;
use crossalpha_storage::RawSnapshotManifest;
use crossalpha_storage::RawSnapshotStore;
use reqwest::Client;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;
use std::path::Path;
use std::str::FromStr;
use std::time::Duration;
use tokio::time::sleep;

pub const HYPERLIQUID_URL: &str = "https://api.hyperliquid.xyz/info";
pub const DEFILLAMA_STABLECOINS_URL: &str =
    "https://stablecoins.llama.fi/stablecoins?includePrices=true";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderSource {
    Hyperliquid,
    DefiLlama,
}

impl FromStr for ProviderSource {
    type Err = anyhow::Error;

    fn from_str(value: &str) -> Result<Self> {
        match value {
            "hyperliquid" => Ok(Self::Hyperliquid),
            "defillama" => Ok(Self::DefiLlama),
            other => bail!("unsupported Observatory source: {other}"),
        }
    }
}

impl ProviderSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Hyperliquid => "hyperliquid",
            Self::DefiLlama => "defillama",
        }
    }
}

#[derive(Clone)]
pub struct ProviderClient {
    client: Client,
}

impl ProviderClient {
    pub fn new(timeout: Duration) -> Result<Self> {
        if timeout.is_zero() {
            bail!("HTTP timeout must be greater than zero");
        }
        let client = Client::builder()
            .timeout(timeout)
            .user_agent("crossalpha-rs/0.1")
            .build()
            .context("build Observatory HTTP client")?;
        Ok(Self { client })
    }

    pub async fn collect(&self, source: ProviderSource) -> Result<Vec<ObservationEnvelope>> {
        match source {
            ProviderSource::Hyperliquid => self.collect_hyperliquid().await,
            ProviderSource::DefiLlama => self.collect_defillama().await,
        }
    }

    pub async fn collect_many(
        &self,
        sources: &[ProviderSource],
    ) -> Result<Vec<ObservationEnvelope>> {
        let mut output = Vec::new();
        for source in sources {
            output.extend(self.collect(*source).await?);
        }
        Ok(output)
    }

    pub async fn collect_and_store(
        &self,
        sources: &[ProviderSource],
        data_root: &Path,
    ) -> Result<Vec<RawSnapshotManifest>> {
        let mut manifests = Vec::new();
        for source in sources {
            let envelopes = self
                .collect(*source)
                .await
                .with_context(|| format!("collect {}", source.as_str()))?;
            let root = data_root.to_path_buf();
            let written = tokio::task::spawn_blocking(move || {
                let store = RawSnapshotStore::new(root);
                let mut output = Vec::with_capacity(envelopes.len());
                for envelope in &envelopes {
                    output.push(store.write(envelope)?);
                }
                Ok::<_, anyhow::Error>(output)
            })
            .await
            .context("join Observatory snapshot writer")??;
            manifests.extend(written);
        }
        Ok(manifests)
    }

    async fn collect_hyperliquid(&self) -> Result<Vec<ObservationEnvelope>> {
        // Match the Python provider contract: both Hyperliquid observations from one
        // collection round share the same observed_at/known_at timestamp.
        let now = Utc::now();
        let requests = [
            ("metaAndAssetCtxs", json!({"type": "metaAndAssetCtxs"})),
            ("allMids", json!({"type": "allMids"})),
        ];
        let mut output = Vec::with_capacity(requests.len());

        for (observation_type, request) in requests {
            let payload = self
                .post_json_with_retry(HYPERLIQUID_URL, &request)
                .await?;
            let mut metadata = Map::new();
            metadata.insert("request".to_owned(), request);
            metadata.insert(
                "endpoint".to_owned(),
                Value::String(HYPERLIQUID_URL.to_owned()),
            );
            output.push(ObservationEnvelope {
                schema_version: 1,
                event_time: None,
                observed_at: now,
                known_at: now,
                source_type: "EXCHANGE".to_owned(),
                source_id: "hyperliquid".to_owned(),
                observation_type: observation_type.to_owned(),
                payload,
                metadata,
            });
        }
        Ok(output)
    }

    async fn collect_defillama(&self) -> Result<Vec<ObservationEnvelope>> {
        let payload = self.get_json_with_retry(DEFILLAMA_STABLECOINS_URL).await?;
        // Match Python: DefiLlama timestamps are captured after the request succeeds.
        let now = Utc::now();
        let mut metadata = Map::new();
        metadata.insert(
            "endpoint".to_owned(),
            Value::String(DEFILLAMA_STABLECOINS_URL.to_owned()),
        );
        Ok(vec![ObservationEnvelope {
            schema_version: 1,
            event_time: None,
            observed_at: now,
            known_at: now,
            source_type: "AGGREGATOR".to_owned(),
            source_id: "defillama".to_owned(),
            observation_type: "stablecoins_snapshot".to_owned(),
            payload,
            metadata,
        }])
    }

    async fn post_json_with_retry(&self, url: &str, body: &Value) -> Result<Value> {
        let mut last_error = None;
        for attempt in 0..3u32 {
            let result = async {
                let response = self
                    .client
                    .post(url)
                    .json(body)
                    .send()
                    .await
                    .with_context(|| format!("POST {url}"))?
                    .error_for_status()
                    .with_context(|| format!("POST {url} returned error status"))?;
                response
                    .json::<Value>()
                    .await
                    .with_context(|| format!("decode JSON from POST {url}"))
            }
            .await;

            match result {
                Ok(payload) => return Ok(payload),
                Err(error) => {
                    last_error = Some(error);
                    if attempt < 2 {
                        sleep(Duration::from_secs(1u64 << attempt)).await;
                    }
                }
            }
        }

        match last_error {
            Some(error) => Err(error.context("Hyperliquid request exhausted retries")),
            None => bail!("Hyperliquid request exhausted retries without an error"),
        }
    }

    async fn get_json_with_retry(&self, url: &str) -> Result<Value> {
        let mut last_error = None;
        for attempt in 0..3u32 {
            let result = async {
                let response = self
                    .client
                    .get(url)
                    .send()
                    .await
                    .with_context(|| format!("GET {url}"))?
                    .error_for_status()
                    .with_context(|| format!("GET {url} returned error status"))?;
                response
                    .json::<Value>()
                    .await
                    .with_context(|| format!("decode JSON from GET {url}"))
            }
            .await;

            match result {
                Ok(payload) => return Ok(payload),
                Err(error) => {
                    last_error = Some(error);
                    if attempt < 2 {
                        sleep(Duration::from_secs(1u64 << attempt)).await;
                    }
                }
            }
        }

        match last_error {
            Some(error) => Err(error.context("DefiLlama request exhausted retries")),
            None => bail!("DefiLlama request exhausted retries without an error"),
        }
    }
}

pub fn parse_sources(values: &[String]) -> Result<Vec<ProviderSource>> {
    if values.is_empty() {
        return Ok(vec![
            ProviderSource::Hyperliquid,
            ProviderSource::DefiLlama,
        ]);
    }
    values.iter().map(|value| value.parse()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_parser_matches_python_cli_names() {
        assert_eq!(
            "hyperliquid".parse::<ProviderSource>().unwrap(),
            ProviderSource::Hyperliquid
        );
        assert_eq!(
            "defillama".parse::<ProviderSource>().unwrap(),
            ProviderSource::DefiLlama
        );
        assert!("unknown".parse::<ProviderSource>().is_err());
    }

    #[test]
    fn empty_sources_default_to_both_python_sources() {
        assert_eq!(
            parse_sources(&[]).unwrap(),
            vec![ProviderSource::Hyperliquid, ProviderSource::DefiLlama]
        );
    }
}
