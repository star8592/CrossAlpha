use crate::v04::{ASSETS, FUNDING_SEMANTICS, NormalizedVenueRow, PROTOCOL, VENUES};
use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Duration;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VenuePayload {
    pub venue: String,
    pub asset: String,
    pub spot_symbol: String,
    pub perp_symbol: String,
    #[serde(default)]
    pub spot: Value,
    #[serde(default)]
    pub perp: Value,
    #[serde(default)]
    pub perp_depth: Value,
    #[serde(default)]
    pub premium: Value,
    #[serde(default)]
    pub open_interest: Value,
    #[serde(default)]
    pub funding_history: Value,
    #[serde(default)]
    pub collection_error: Option<String>,
}

#[derive(Clone)]
pub struct MultiVenueCollector {
    client: Client,
}

impl MultiVenueCollector {
    pub fn new(timeout: Duration) -> Result<Self> {
        if timeout.is_zero() {
            bail!("State V0.4 HTTP timeout must be positive");
        }
        let client = Client::builder()
            .timeout(timeout)
            .build()
            .context("build State V0.4 HTTP client")?;
        Ok(Self { client })
    }

    pub async fn collect(&self) -> Result<Vec<VenuePayload>> {
        let mut tasks = Vec::new();
        for asset in ASSETS {
            tasks.push(self.collect_safe("binance", asset));
            tasks.push(self.collect_safe("okx", asset));
            tasks.push(self.collect_safe("bybit", asset));
        }
        let rows = futures_util::future::join_all(tasks).await;
        let expected: std::collections::BTreeSet<(String, String)> = ASSETS
            .iter()
            .flat_map(|asset| {
                VENUES
                    .iter()
                    .map(move |venue| ((*asset).to_owned(), (*venue).to_owned()))
            })
            .collect();
        let actual: std::collections::BTreeSet<(String, String)> = rows
            .iter()
            .map(|row| (row.asset.clone(), row.venue.clone()))
            .collect();
        if rows.len() != 6 || actual != expected {
            bail!("State V0.4 slot preservation failed: {actual:?}");
        }
        Ok(rows)
    }

    async fn collect_safe(&self, venue: &str, asset: &str) -> VenuePayload {
        let result = match venue {
            "binance" => self.binance(asset).await,
            "okx" => self.okx(asset).await,
            "bybit" => self.bybit(asset).await,
            _ => Err(anyhow::anyhow!("unsupported venue")),
        };
        match result {
            Ok(payload) => payload,
            Err(error) => VenuePayload {
                venue: venue.to_owned(),
                asset: asset.to_owned(),
                spot_symbol: format!("{asset}USDT"),
                perp_symbol: format!("{asset}USDT"),
                spot: Value::Null,
                perp: Value::Null,
                perp_depth: Value::Null,
                premium: Value::Null,
                open_interest: Value::Null,
                funding_history: Value::Null,
                collection_error: Some(error_category(&error)),
            },
        }
    }

    async fn binance(&self, asset: &str) -> Result<VenuePayload> {
        let symbol = format!("{asset}USDT");
        let spot_f = self.get(
            "https://api.binance.com/api/v3/ticker/bookTicker",
            &[(&"symbol", symbol.as_str())],
        );
        let depth_f = self.get(
            "https://fapi.binance.com/fapi/v1/depth",
            &[(&"symbol", symbol.as_str()), (&"limit", "5")],
        );
        let premium_f = self.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            &[(&"symbol", symbol.as_str())],
        );
        let oi_f = self.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            &[(&"symbol", symbol.as_str())],
        );
        let funding_f = self.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            &[(&"symbol", symbol.as_str()), (&"limit", "2")],
        );
        let (spot, depth, premium, oi, funding) =
            tokio::try_join!(spot_f, depth_f, premium_f, oi_f, funding_f)?;
        Ok(VenuePayload {
            venue: "binance".to_owned(),
            asset: asset.to_owned(),
            spot_symbol: symbol.clone(),
            perp_symbol: symbol,
            spot,
            perp: Value::Null,
            perp_depth: depth,
            premium,
            open_interest: oi,
            funding_history: funding,
            collection_error: None,
        })
    }

    async fn okx(&self, asset: &str) -> Result<VenuePayload> {
        let spot_id = format!("{asset}-USDT");
        let swap_id = format!("{asset}-USDT-SWAP");
        let spot_f = self.get(
            "https://www.okx.com/api/v5/market/ticker",
            &[(&"instId", spot_id.as_str())],
        );
        let perp_f = self.get(
            "https://www.okx.com/api/v5/market/ticker",
            &[(&"instId", swap_id.as_str())],
        );
        let funding_f = self.get(
            "https://www.okx.com/api/v5/public/funding-rate-history",
            &[(&"instId", swap_id.as_str()), (&"limit", "2")],
        );
        let oi_f = self.get(
            "https://www.okx.com/api/v5/public/open-interest",
            &[(&"instType", "SWAP"), (&"instId", swap_id.as_str())],
        );
        let (spot, perp, funding, oi) = tokio::try_join!(spot_f, perp_f, funding_f, oi_f)?;
        Ok(VenuePayload {
            venue: "okx".to_owned(),
            asset: asset.to_owned(),
            spot_symbol: spot_id,
            perp_symbol: swap_id,
            spot,
            perp,
            perp_depth: Value::Null,
            premium: Value::Null,
            open_interest: oi,
            funding_history: funding,
            collection_error: None,
        })
    }

    async fn bybit(&self, asset: &str) -> Result<VenuePayload> {
        let symbol = format!("{asset}USDT");
        let spot_f = self.get(
            "https://api.bybit.com/v5/market/tickers",
            &[(&"category", "spot"), (&"symbol", symbol.as_str())],
        );
        let perp_f = self.get(
            "https://api.bybit.com/v5/market/tickers",
            &[(&"category", "linear"), (&"symbol", symbol.as_str())],
        );
        let funding_f = self.get(
            "https://api.bybit.com/v5/market/funding/history",
            &[
                (&"category", "linear"),
                (&"symbol", symbol.as_str()),
                (&"limit", "2"),
            ],
        );
        let (spot, perp, funding) = tokio::try_join!(spot_f, perp_f, funding_f)?;
        Ok(VenuePayload {
            venue: "bybit".to_owned(),
            asset: asset.to_owned(),
            spot_symbol: symbol.clone(),
            perp_symbol: symbol,
            spot,
            perp,
            perp_depth: Value::Null,
            premium: Value::Null,
            open_interest: Value::Null,
            funding_history: funding,
            collection_error: None,
        })
    }

    fn get(
        &self,
        url: &str,
        params: &[(&&str, &str)],
    ) -> impl std::future::Future<Output = Result<Value>> + Send + 'static {
        let client = self.client.clone();
        let url = url.to_owned();
        let pairs: Vec<(String, String)> = params
            .iter()
            .map(|(key, value)| ((**key).to_owned(), (*value).to_owned()))
            .collect();
        async move {
            let response = client
                .get(&url)
                .query(&pairs)
                .send()
                .await
                .context("VenueTransportError")?
                .error_for_status()
                .context("VenueHttpStatusError")?;
            response.json().await.context("VenueJsonDecodeError")
        }
    }
}

pub fn parse_venue_snapshot(
    payload: &VenuePayload,
    known_at: DateTime<Utc>,
) -> Result<NormalizedVenueRow> {
    if !VENUES.contains(&payload.venue.as_str()) || !ASSETS.contains(&payload.asset.as_str()) {
        bail!("unsupported State V0.4 venue/asset");
    }
    let (
        spot_bid,
        spot_ask,
        perp_bid,
        perp_ask,
        mark,
        index,
        oi_usd,
        settled_rate,
        settled_interval,
        settled_time,
        mut source_times,
    ) = match payload.venue.as_str() {
        "binance" => {
            let spot_bid = number(payload.spot.get("bidPrice"));
            let spot_ask = number(payload.spot.get("askPrice"));
            let perp_bid = first_book_price(payload.perp_depth.get("bids"));
            let perp_ask = first_book_price(payload.perp_depth.get("asks"));
            let mark = number(payload.premium.get("markPrice"));
            let index = number(payload.premium.get("indexPrice"));
            let funding = funding_rows(&payload.funding_history);
            let (settled_rate, settled_interval, settled_time) =
                settled_funding(&funding, "fundingRate", "fundingTime");
            let perp_mid = mid(perp_bid, perp_ask);
            let oi_usd = number(payload.open_interest.get("openInterest"))
                .zip(perp_mid)
                .map(|(base, price)| base * price);
            let source_times = [
                payload.premium.get("time").cloned(),
                payload.open_interest.get("time").cloned(),
                payload.perp_depth.get("E").cloned(),
                payload.perp_depth.get("T").cloned(),
            ]
            .into_iter()
            .flatten()
            .collect();
            (
                spot_bid,
                spot_ask,
                perp_bid,
                perp_ask,
                mark,
                index,
                oi_usd,
                settled_rate,
                settled_interval,
                settled_time,
                source_times,
            )
        }
        "okx" => {
            let spot = okx_item(&payload.spot);
            let perp = okx_item(&payload.perp);
            let oi = okx_item(&payload.open_interest);
            let spot_bid = number(spot.get("bidPx"));
            let spot_ask = number(spot.get("askPx"));
            let perp_bid = number(perp.get("bidPx"));
            let perp_ask = number(perp.get("askPx"));
            let funding = okx_rows(&payload.funding_history);
            let (settled_rate, settled_interval, settled_time) =
                settled_funding(&funding, "realizedRate", "fundingTime");
            let oi_usd = number(oi.get("oiUsd"));
            let source_times = [
                spot.get("ts").cloned(),
                perp.get("ts").cloned(),
                oi.get("ts").cloned(),
            ]
            .into_iter()
            .flatten()
            .collect();
            (
                spot_bid,
                spot_ask,
                perp_bid,
                perp_ask,
                None,
                None,
                oi_usd,
                settled_rate,
                settled_interval,
                settled_time,
                source_times,
            )
        }
        "bybit" => {
            let (spot, spot_time) = bybit_item(&payload.spot);
            let (perp, perp_time) = bybit_item(&payload.perp);
            let spot_bid = number(spot.get("bid1Price"));
            let spot_ask = number(spot.get("ask1Price"));
            let perp_bid = number(perp.get("bid1Price"));
            let perp_ask = number(perp.get("ask1Price"));
            let mark = number(perp.get("markPrice"));
            let index = number(perp.get("indexPrice"));
            let funding = bybit_rows(&payload.funding_history);
            let (settled_rate, settled_interval, settled_time) =
                settled_funding(&funding, "fundingRate", "fundingRateTimestamp");
            let oi_usd = number(perp.get("openInterestValue"));
            let source_times = [spot_time, perp_time].into_iter().flatten().collect();
            (
                spot_bid,
                spot_ask,
                perp_bid,
                perp_ask,
                mark,
                index,
                oi_usd,
                settled_rate,
                settled_interval,
                settled_time,
                source_times,
            )
        }
        _ => unreachable!(),
    };
    if let Some(time) = settled_time.as_deref() {
        source_times.push(Value::String(time.to_owned()));
    }
    let observed_at = latest_source_time(&source_times).unwrap_or(known_at);
    let spot_mid = mid(spot_bid, spot_ask);
    let perp_mid = mid(perp_bid, perp_ask);
    Ok(NormalizedVenueRow {
        protocol: PROTOCOL.to_owned(),
        observed_at,
        known_at,
        venue: payload.venue.clone(),
        asset: payload.asset.clone(),
        spot_symbol: Some(payload.spot_symbol.clone()),
        perp_symbol: Some(payload.perp_symbol.clone()),
        spot_bid,
        spot_ask,
        spot_mid,
        spot_spread_bps: spread_bps(spot_bid, spot_ask),
        perp_bid,
        perp_ask,
        perp_mid,
        perp_spread_bps: spread_bps(perp_bid, perp_ask),
        mark_price: mark,
        index_price: index,
        basis_bps: basis_bps(perp_mid, spot_mid),
        mark_index_basis_bps: basis_bps(mark, index),
        funding_semantics: FUNDING_SEMANTICS.to_owned(),
        funding_rate_settled_raw: settled_rate,
        funding_settlement_time: settled_time,
        funding_interval_hours: settled_interval,
        funding_rate_8h: funding_8h(settled_rate, settled_interval),
        open_interest_usd: oi_usd,
        data_cost_usd: 0,
        collection_error: payload.collection_error.clone(),
        raw_sha256: None,
        raw_compressed_file_sha256: None,
        raw_path: None,
    })
}

fn number(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let parsed = match value {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.parse::<f64>().ok(),
        _ => None,
    }?;
    parsed.is_finite().then_some(parsed)
}

fn mid(bid: Option<f64>, ask: Option<f64>) -> Option<f64> {
    bid.zip(ask)
        .and_then(|(bid, ask)| (bid > 0.0 && ask > 0.0 && ask >= bid).then_some((bid + ask) / 2.0))
}

fn spread_bps(bid: Option<f64>, ask: Option<f64>) -> Option<f64> {
    let midpoint = mid(bid, ask)?;
    let (bid, ask) = bid.zip(ask)?;
    Some((ask - bid) / midpoint * 10_000.0)
}

fn basis_bps(perp_mid: Option<f64>, spot_mid: Option<f64>) -> Option<f64> {
    let (perp, spot) = perp_mid.zip(spot_mid)?;
    (spot > 0.0).then_some((perp / spot - 1.0) * 10_000.0)
}

fn funding_8h(rate: Option<f64>, interval_hours: Option<f64>) -> Option<f64> {
    let (rate, hours) = rate.zip(interval_hours)?;
    (hours > 0.0).then_some(rate * 8.0 / hours)
}

fn first_book_price(value: Option<&Value>) -> Option<f64> {
    let row = value?.as_array()?.first()?.as_array()?;
    number(row.first())
}

fn funding_rows(value: &Value) -> Vec<Value> {
    value.as_array().cloned().unwrap_or_default()
}

fn settled_funding(
    rows: &[Value],
    rate_field: &str,
    time_field: &str,
) -> (Option<f64>, Option<f64>, Option<String>) {
    let mut clean: Vec<(i64, &Value)> = rows
        .iter()
        .filter_map(|row| {
            let stamp = row.get(time_field).and_then(as_i64)?;
            (stamp > 0).then_some((stamp, row))
        })
        .collect();
    clean.sort_by_key(|(stamp, _)| *stamp);
    let Some((latest_time, latest_row)) = clean.last().copied() else {
        return (None, None, None);
    };
    let rate = number(latest_row.get(rate_field));
    let interval = if clean.len() >= 2 {
        let previous = clean[clean.len() - 2].0;
        let delta = latest_time - previous;
        (delta > 0).then_some(delta as f64 / 3_600_000.0)
    } else {
        None
    };
    let time = DateTime::<Utc>::from_timestamp_millis(latest_time).map(|value| value.to_rfc3339());
    (rate, interval, time)
}

fn latest_source_time(values: &[Value]) -> Option<DateTime<Utc>> {
    values.iter().filter_map(parse_source_time).max()
}

fn parse_source_time(value: &Value) -> Option<DateTime<Utc>> {
    match value {
        Value::Number(number) => DateTime::<Utc>::from_timestamp_millis(number.as_i64()?),
        Value::String(text) => {
            if let Ok(milliseconds) = text.parse::<i64>() {
                DateTime::<Utc>::from_timestamp_millis(milliseconds)
            } else {
                DateTime::parse_from_rfc3339(text)
                    .ok()
                    .map(|value| value.with_timezone(&Utc))
            }
        }
        _ => None,
    }
}

fn okx_item(value: &Value) -> &serde_json::Map<String, Value> {
    static EMPTY: std::sync::OnceLock<serde_json::Map<String, Value>> = std::sync::OnceLock::new();
    let empty = EMPTY.get_or_init(Default::default);
    if value.get("code").and_then(Value::as_str) != Some("0") {
        return empty;
    }
    value
        .get("data")
        .and_then(Value::as_array)
        .and_then(|rows| rows.first())
        .and_then(Value::as_object)
        .unwrap_or(empty)
}

fn okx_rows(value: &Value) -> Vec<Value> {
    if value.get("code").and_then(Value::as_str) != Some("0") {
        return Vec::new();
    }
    value
        .get("data")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn bybit_item(value: &Value) -> (&serde_json::Map<String, Value>, Option<Value>) {
    static EMPTY: std::sync::OnceLock<serde_json::Map<String, Value>> = std::sync::OnceLock::new();
    let empty = EMPTY.get_or_init(Default::default);
    if value.get("retCode").and_then(as_i64) != Some(0) {
        return (empty, None);
    }
    let item = value
        .get("result")
        .and_then(|result| result.get("list"))
        .and_then(Value::as_array)
        .and_then(|rows| rows.first())
        .and_then(Value::as_object)
        .unwrap_or(empty);
    (item, value.get("time").cloned())
}

fn bybit_rows(value: &Value) -> Vec<Value> {
    if value.get("retCode").and_then(as_i64) != Some(0) {
        return Vec::new();
    }
    value
        .get("result")
        .and_then(|result| result.get("list"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn as_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64(),
        Value::String(text) => text.parse().ok(),
        _ => None,
    }
}

fn error_category(error: &anyhow::Error) -> String {
    let text = format!("{error:#}");
    for category in [
        "VenueTransportError",
        "VenueHttpStatusError",
        "VenueJsonDecodeError",
    ] {
        if text.contains(category) {
            return category.to_owned();
        }
    }
    "RuntimeError".to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn settled_funding_normalizes_latest_two_timestamps() {
        let rows = vec![
            json!({"fundingRate": "0.0001", "fundingTime": "1000"}),
            json!({"fundingRate": "0.0002", "fundingTime": "28801000"}),
        ];
        let (rate, interval, _) = settled_funding(&rows, "fundingRate", "fundingTime");
        assert_eq!(rate, Some(0.0002));
        assert_eq!(interval, Some(8.0));
        assert_eq!(funding_8h(rate, interval), Some(0.0002));
    }

    #[test]
    fn parser_preserves_failed_slot_shape() {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let payload = VenuePayload {
            venue: "binance".to_owned(),
            asset: "BTC".to_owned(),
            spot_symbol: "BTCUSDT".to_owned(),
            perp_symbol: "BTCUSDT".to_owned(),
            spot: Value::Null,
            perp: Value::Null,
            perp_depth: Value::Null,
            premium: Value::Null,
            open_interest: Value::Null,
            funding_history: Value::Null,
            collection_error: Some("VenueTransportError".to_owned()),
        };
        let row = parse_venue_snapshot(&payload, now).unwrap();
        assert_eq!(row.collection_error.as_deref(), Some("VenueTransportError"));
        assert!(row.spot_mid.is_none());
    }
}
