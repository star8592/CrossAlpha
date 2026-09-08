use anyhow::{Context, Result, bail};
use arrow_array::builder::{Float64Builder, Int64Builder, LargeStringBuilder};
use arrow_array::{ArrayRef, RecordBatch, TimestampMicrosecondArray, TimestampMillisecondArray};
use arrow_schema::{DataType, Field, Schema, TimeUnit};
use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use parquet::arrow::ArrowWriter;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

pub const FRED_CASH_SERIES: &str = "DGS3MO";
pub const FREE_TRADFI_PROXIES: [(&str, &str); 6] = [
    ("US_EQUITY", "SPY"),
    ("US_GROWTH", "QQQ"),
    ("GOLD", "GLD"),
    ("SILVER", "SLV"),
    ("COPPER", "CPER"),
    ("WTI", "USO"),
];
pub const FREE_CRYPTO_PROXIES: [(&str, &str); 2] = [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT")];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FreeCoreRange {
    pub start: NaiveDate,
    pub end: NaiveDate,
}

impl FreeCoreRange {
    pub fn new(start: NaiveDate, end: NaiveDate) -> Result<Self> {
        if end <= start {
            bail!("end must be after start");
        }
        Ok(Self { start, end })
    }

    pub fn slug(&self) -> PathBuf {
        PathBuf::from(format!("start={}", self.start)).join(format!("end={}", self.end))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ProxyDailyRow {
    pub date: DateTime<Utc>,
    pub economic_asset: String,
    pub source: String,
    pub symbol: String,
    pub open: Option<f64>,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub close: Option<f64>,
    pub volume: Option<f64>,
    pub adj_open: Option<f64>,
    pub adj_high: Option<f64>,
    pub adj_low: Option<f64>,
    pub adj_close: Option<f64>,
    pub adj_volume: Option<f64>,
    pub div_cash: Option<f64>,
    pub split_factor: Option<f64>,
    pub quote_volume: Option<f64>,
    pub trade_count: Option<i64>,
    pub taker_buy_base_volume: Option<f64>,
    pub taker_buy_quote_volume: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CashRateRow {
    pub date: DateTime<Utc>,
    pub series_id: String,
    pub rate_percent: Option<f64>,
}

#[derive(Clone)]
pub struct FreeCoreProvider {
    client: Client,
    tiingo_token: String,
    fred_api_key: String,
}

impl FreeCoreProvider {
    pub fn new(tiingo_token: &str, fred_api_key: &str, timeout: Duration) -> Result<Self> {
        validate_tiingo_token(tiingo_token)?;
        validate_fred_key(fred_api_key)?;
        if timeout.is_zero() {
            bail!("Free Core HTTP timeout must be positive");
        }
        let client = Client::builder()
            .timeout(timeout)
            .redirect(reqwest::redirect::Policy::limited(5))
            .build()
            .context("build Free Core HTTP client")?;
        Ok(Self {
            client,
            tiingo_token: tiingo_token.trim().to_owned(),
            fred_api_key: fred_api_key.trim().to_owned(),
        })
    }

    pub async fn fetch_all(&self, range: &FreeCoreRange, data_root: &Path) -> Result<Value> {
        let tradfi = self.fetch_tradfi(range, data_root).await?;
        let crypto = self.fetch_crypto(range, data_root).await?;
        let cash = self.fetch_cash(range, data_root).await?;
        Ok(json!({
            "mode":"free_only",
            "data_cost_usd":0,
            "start":range.start.to_string(),
            "end":range.end.to_string(),
            "tradfi":tradfi,
            "crypto":crypto,
            "cash":cash,
            "generated_at":Utc::now().to_rfc3339(),
        }))
    }

    async fn fetch_tradfi(&self, range: &FreeCoreRange, data_root: &Path) -> Result<Value> {
        let raw_root = data_root.join("raw/free_core/tiingo").join(range.slug());
        fs::create_dir_all(&raw_root)?;
        let mut rows = Vec::new();
        for (economic_asset, ticker) in FREE_TRADFI_PROXIES {
            let response = self
                .client
                .get(format!(
                    "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
                ))
                .query(&[
                    ("startDate", range.start.to_string()),
                    ("endDate", range.end.to_string()),
                ])
                .header("Authorization", format!("Token {}", self.tiingo_token))
                .send()
                .await
                .context("TiingoTransportError")?
                .error_for_status()
                .context("TiingoHttpStatusError")?;
            let payload: Value = response.json().await.context("TiingoJsonDecodeError")?;
            write_raw_json(&raw_root.join(format!("{ticker}.json")), &payload)?;
            let parsed = parse_tiingo_payload(economic_asset, ticker, &payload)?;
            if parsed.is_empty() {
                bail!("Tiingo returned no data for {ticker}");
            }
            rows.extend(parsed);
        }
        rows.sort_by(|left, right| {
            left.date
                .cmp(&right.date)
                .then(left.economic_asset.cmp(&right.economic_asset))
        });
        let path = data_root
            .join("canonical/core/free_proxy_daily")
            .join(range.slug())
            .join("tradfi.parquet");
        write_tradfi_parquet(&path, &rows)?;
        Ok(json!({
            "source":"tiingo_eod",
            "data_cost_usd":0,
            "symbols":FREE_TRADFI_PROXIES.iter().map(|(_, ticker)| *ticker).collect::<Vec<_>>(),
            "rows":rows.len(),
            "canonical":path,
        }))
    }

    async fn fetch_crypto(&self, range: &FreeCoreRange, data_root: &Path) -> Result<Value> {
        let raw_root = data_root.join("raw/free_core/binance").join(range.slug());
        fs::create_dir_all(&raw_root)?;
        let start_ms = Utc
            .from_utc_datetime(&range.start.and_hms_opt(0, 0, 0).unwrap())
            .timestamp_millis();
        let end_ms = Utc
            .from_utc_datetime(&range.end.and_hms_opt(0, 0, 0).unwrap())
            .timestamp_millis()
            - 1;
        let mut rows = Vec::new();
        for (economic_asset, symbol) in FREE_CRYPTO_PROXIES {
            let mut cursor = start_ms;
            let mut page = 0_u32;
            let mut payload_rows = Vec::<Value>::new();
            while cursor <= end_ms {
                let payload: Value = self
                    .client
                    .get("https://api.binance.com/api/v3/klines")
                    .query(&[
                        ("symbol", symbol.to_owned()),
                        ("interval", "1d".to_owned()),
                        ("startTime", cursor.to_string()),
                        ("endTime", end_ms.to_string()),
                        ("limit", "1000".to_owned()),
                    ])
                    .send()
                    .await
                    .context("BinanceTransportError")?
                    .error_for_status()
                    .context("BinanceHttpStatusError")?
                    .json()
                    .await
                    .context("BinanceJsonDecodeError")?;
                let array = payload
                    .as_array()
                    .context("Binance kline payload must be array")?;
                if array.is_empty() {
                    break;
                }
                page += 1;
                write_raw_json(
                    &raw_root.join(format!("{symbol}_page={page:04}.json")),
                    &payload,
                )?;
                payload_rows.extend(array.iter().cloned());
                let last_open = array
                    .last()
                    .and_then(Value::as_array)
                    .and_then(|row| row.first())
                    .and_then(Value::as_i64)
                    .context("Binance kline missing open time")?;
                let next = last_open + 86_400_000;
                if next <= cursor {
                    bail!("Binance pagination did not advance for {symbol}");
                }
                cursor = next;
                if array.len() < 1000 {
                    break;
                }
            }
            let parsed = parse_binance_payload(economic_asset, symbol, &payload_rows)?;
            if parsed.is_empty() {
                bail!("Binance returned no data for {symbol}");
            }
            rows.extend(parsed);
        }
        rows.sort_by(|left, right| {
            left.date
                .cmp(&right.date)
                .then(left.economic_asset.cmp(&right.economic_asset))
        });
        let path = data_root
            .join("canonical/core/free_proxy_daily")
            .join(range.slug())
            .join("crypto.parquet");
        write_crypto_parquet(&path, &rows)?;
        Ok(json!({
            "source":"binance_spot_public",
            "data_cost_usd":0,
            "symbols":FREE_CRYPTO_PROXIES.iter().map(|(_, symbol)| *symbol).collect::<Vec<_>>(),
            "rows":rows.len(),
            "canonical":path,
        }))
    }

    async fn fetch_cash(&self, range: &FreeCoreRange, data_root: &Path) -> Result<Value> {
        let raw_root = data_root.join("raw/free_core/fred").join(range.slug());
        fs::create_dir_all(&raw_root)?;
        let payload: Value = self
            .client
            .get("https://api.stlouisfed.org/fred/series/observations")
            .query(&[
                ("series_id", FRED_CASH_SERIES.to_owned()),
                ("api_key", self.fred_api_key.clone()),
                ("file_type", "json".to_owned()),
                ("observation_start", range.start.to_string()),
                ("observation_end", range.end.to_string()),
            ])
            .send()
            .await
            .context("FredTransportError")?
            .error_for_status()
            .context("FredHttpStatusError")?
            .json()
            .await
            .context("FredJsonDecodeError")?;
        write_raw_json(&raw_root.join(format!("{FRED_CASH_SERIES}.json")), &payload)?;
        let rows = parse_fred_payload(FRED_CASH_SERIES, &payload)?;
        if rows.is_empty() {
            bail!("FRED returned no data for {FRED_CASH_SERIES}");
        }
        let path = data_root
            .join("canonical/core/cash_rate")
            .join(range.slug())
            .join(format!("{FRED_CASH_SERIES}.parquet"));
        write_cash_parquet(&path, &rows)?;
        Ok(json!({
            "source":"fred",
            "data_cost_usd":0,
            "series_id":FRED_CASH_SERIES,
            "rows":rows.len(),
            "canonical":path,
        }))
    }
}

pub fn validate_tiingo_token(value: &str) -> Result<()> {
    let token = value.trim();
    if token.is_empty() || !token.is_ascii() || token.chars().any(char::is_whitespace) {
        bail!("TIINGO_API_TOKEN is missing or invalid");
    }
    let lower = token.to_ascii_lowercase();
    if [
        "token",
        "yourtoken",
        "your_token",
        "your-api-token",
        "your_api_token",
        "changeme",
        "replace_me",
        "example",
    ]
    .contains(&lower.as_str())
        || lower.contains("yourtoken")
    {
        bail!("TIINGO_API_TOKEN still looks like a placeholder");
    }
    Ok(())
}

pub fn validate_fred_key(value: &str) -> Result<()> {
    let key = value.trim();
    if key.len() != 32
        || !key.is_ascii()
        || !key
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
    {
        bail!("FRED_API_KEY must be a 32-character lowercase alphanumeric key");
    }
    Ok(())
}

pub fn parse_tiingo_payload(
    economic_asset: &str,
    ticker: &str,
    payload: &Value,
) -> Result<Vec<ProxyDailyRow>> {
    let array = payload
        .as_array()
        .context("Tiingo EOD payload must be a list")?;
    let mut rows = Vec::new();
    for item in array {
        let date = parse_time(item.get("date"))?;
        let adj_close = number(item.get("adjClose"));
        if adj_close.is_none_or(|value| value <= 0.0) {
            bail!("invalid adjusted close for {ticker}");
        }
        rows.push(ProxyDailyRow {
            date,
            economic_asset: economic_asset.to_owned(),
            source: "tiingo_eod".to_owned(),
            symbol: ticker.to_owned(),
            open: number(item.get("open")),
            high: number(item.get("high")),
            low: number(item.get("low")),
            close: number(item.get("close")),
            volume: integer_number(item.get("volume")),
            adj_open: number(item.get("adjOpen")),
            adj_high: number(item.get("adjHigh")),
            adj_low: number(item.get("adjLow")),
            adj_close,
            adj_volume: integer_number(item.get("adjVolume")),
            div_cash: number(item.get("divCash")),
            split_factor: number(item.get("splitFactor")),
            quote_volume: None,
            trade_count: None,
            taker_buy_base_volume: None,
            taker_buy_quote_volume: None,
        });
    }
    ensure_unique_dates(&rows, ticker)?;
    Ok(rows)
}

pub fn parse_binance_payload(
    economic_asset: &str,
    symbol: &str,
    payload: &[Value],
) -> Result<Vec<ProxyDailyRow>> {
    let mut rows = Vec::new();
    for value in payload {
        let row = value.as_array().context("invalid Binance kline row")?;
        if row.len() < 11 {
            bail!("invalid Binance kline row for {symbol}");
        }
        let millis = row[0].as_i64().context("Binance open time missing")?;
        let date = Utc
            .timestamp_millis_opt(millis)
            .single()
            .context("invalid Binance open time")?;
        let parse = |index: usize| number(row.get(index)).context("Binance numeric field missing");
        let open = parse(1)?;
        let high = parse(2)?;
        let low = parse(3)?;
        let close = parse(4)?;
        if [open, high, low, close].iter().any(|value| *value <= 0.0) {
            bail!("invalid Binance OHLC for {symbol}");
        }
        rows.push(ProxyDailyRow {
            date,
            economic_asset: economic_asset.to_owned(),
            source: "binance_spot_public".to_owned(),
            symbol: symbol.to_owned(),
            open: Some(open),
            high: Some(high),
            low: Some(low),
            close: Some(close),
            volume: number(row.get(5)),
            adj_open: None,
            adj_high: None,
            adj_low: None,
            adj_close: None,
            adj_volume: None,
            div_cash: None,
            split_factor: None,
            quote_volume: number(row.get(7)),
            trade_count: row.get(8).and_then(value_i64),
            taker_buy_base_volume: number(row.get(9)),
            taker_buy_quote_volume: number(row.get(10)),
        });
    }
    ensure_unique_dates(&rows, symbol)?;
    Ok(rows)
}

pub fn parse_fred_payload(series_id: &str, payload: &Value) -> Result<Vec<CashRateRow>> {
    let observations = payload
        .get("observations")
        .and_then(Value::as_array)
        .context("FRED payload missing observations")?;
    let mut rows = Vec::new();
    for item in observations {
        let date_text = item
            .get("date")
            .and_then(Value::as_str)
            .context("FRED date missing")?;
        let date = NaiveDate::parse_from_str(date_text, "%Y-%m-%d")?;
        let date = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).unwrap());
        let rate = match item.get("value").and_then(Value::as_str) {
            Some(".") | None => None,
            Some(text) => text.parse::<f64>().ok(),
        };
        rows.push(CashRateRow {
            date,
            series_id: series_id.to_owned(),
            rate_percent: rate,
        });
    }
    Ok(rows)
}

pub fn write_free_core_fixture_canonical(
    data_root: &Path,
    range: &FreeCoreRange,
    tradfi_rows: &[ProxyDailyRow],
    crypto_rows: &[ProxyDailyRow],
    cash_rows: &[CashRateRow],
) -> Result<Value> {
    let mut tradfi = tradfi_rows.to_vec();
    let mut crypto = crypto_rows.to_vec();
    let mut cash = cash_rows.to_vec();
    tradfi.sort_by(|left, right| {
        left.date
            .cmp(&right.date)
            .then(left.economic_asset.cmp(&right.economic_asset))
    });
    crypto.sort_by(|left, right| {
        left.date
            .cmp(&right.date)
            .then(left.economic_asset.cmp(&right.economic_asset))
    });
    cash.sort_by_key(|row| row.date);

    let slug = range.slug();
    let proxy_root = data_root
        .join("canonical/core/free_proxy_daily")
        .join(&slug);
    let cash_root = data_root.join("canonical/core/cash_rate").join(&slug);
    let tradfi_path = proxy_root.join("tradfi.parquet");
    let crypto_path = proxy_root.join("crypto.parquet");
    let cash_path = cash_root.join(format!("{FRED_CASH_SERIES}.parquet"));
    write_tradfi_parquet(&tradfi_path, &tradfi)?;
    write_crypto_parquet(&crypto_path, &crypto)?;
    write_cash_parquet(&cash_path, &cash)?;
    Ok(json!({
        "tradfi": tradfi_path,
        "crypto": crypto_path,
        "cash": cash_path,
        "tradfi_rows": tradfi.len(),
        "crypto_rows": crypto.len(),
        "cash_rows": cash.len(),
    }))
}

fn write_tradfi_parquet(path: &Path, rows: &[ProxyDailyRow]) -> Result<()> {
    let mut fields = Vec::new();
    let mut arrays = Vec::<ArrayRef>::new();
    timestamp_us_col(
        &mut fields,
        &mut arrays,
        "date",
        rows.iter().map(|row| row.date),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "economic_asset",
        rows.iter().map(|row| Some(row.economic_asset.clone())),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "source",
        rows.iter().map(|row| Some(row.source.clone())),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "symbol",
        rows.iter().map(|row| Some(row.symbol.clone())),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "open",
        rows.iter().map(|row| row.open),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "high",
        rows.iter().map(|row| row.high),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "low",
        rows.iter().map(|row| row.low),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "close",
        rows.iter().map(|row| row.close),
    );
    i64_from_f64_col(
        &mut fields,
        &mut arrays,
        "volume",
        rows.iter().map(|row| row.volume),
    )?;
    f64_col(
        &mut fields,
        &mut arrays,
        "adj_open",
        rows.iter().map(|row| row.adj_open),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "adj_high",
        rows.iter().map(|row| row.adj_high),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "adj_low",
        rows.iter().map(|row| row.adj_low),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "adj_close",
        rows.iter().map(|row| row.adj_close),
    );
    i64_from_f64_col(
        &mut fields,
        &mut arrays,
        "adj_volume",
        rows.iter().map(|row| row.adj_volume),
    )?;
    f64_col(
        &mut fields,
        &mut arrays,
        "div_cash",
        rows.iter().map(|row| row.div_cash),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "split_factor",
        rows.iter().map(|row| row.split_factor),
    );
    write_batch(path, fields, arrays)
}

fn write_crypto_parquet(path: &Path, rows: &[ProxyDailyRow]) -> Result<()> {
    let mut fields = Vec::new();
    let mut arrays = Vec::<ArrayRef>::new();
    timestamp_ms_col(
        &mut fields,
        &mut arrays,
        "date",
        rows.iter().map(|row| row.date),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "economic_asset",
        rows.iter().map(|row| Some(row.economic_asset.clone())),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "source",
        rows.iter().map(|row| Some(row.source.clone())),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "symbol",
        rows.iter().map(|row| Some(row.symbol.clone())),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "open",
        rows.iter().map(|row| row.open),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "high",
        rows.iter().map(|row| row.high),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "low",
        rows.iter().map(|row| row.low),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "close",
        rows.iter().map(|row| row.close),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "volume",
        rows.iter().map(|row| row.volume),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "quote_volume",
        rows.iter().map(|row| row.quote_volume),
    );
    i64_col(
        &mut fields,
        &mut arrays,
        "trade_count",
        rows.iter().map(|row| row.trade_count),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "taker_buy_base_volume",
        rows.iter().map(|row| row.taker_buy_base_volume),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "taker_buy_quote_volume",
        rows.iter().map(|row| row.taker_buy_quote_volume),
    );
    write_batch(path, fields, arrays)
}

fn write_cash_parquet(path: &Path, rows: &[CashRateRow]) -> Result<()> {
    let mut fields = Vec::new();
    let mut arrays = Vec::<ArrayRef>::new();
    timestamp_us_col(
        &mut fields,
        &mut arrays,
        "date",
        rows.iter().map(|row| row.date),
    );
    string_col(
        &mut fields,
        &mut arrays,
        "series_id",
        rows.iter().map(|row| Some(row.series_id.clone())),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "rate_percent",
        rows.iter().map(|row| row.rate_percent),
    );
    write_batch(path, fields, arrays)
}

fn write_batch(path: &Path, fields: Vec<Field>, arrays: Vec<ArrayRef>) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let schema = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    let tmp = path.with_extension("parquet.tmp");
    let file = File::create(&tmp)?;
    let mut writer = ArrowWriter::try_new(file, schema, None)?;
    writer.write(&batch)?;
    writer.close()?;
    OpenOptions::new().write(true).open(&tmp)?.sync_all()?;
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn write_raw_json(path: &Path, value: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_vec(value)?)?;
    OpenOptions::new().write(true).open(&tmp)?.sync_all()?;
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(())
}

fn parse_time(value: Option<&Value>) -> Result<DateTime<Utc>> {
    let text = value.and_then(Value::as_str).context("timestamp missing")?;
    Ok(DateTime::parse_from_rfc3339(text)?.with_timezone(&Utc))
}

fn number(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let number = value
        .as_f64()
        .or_else(|| value.as_str()?.replace([',', '%'], "").parse().ok())?;
    number.is_finite().then_some(number)
}

fn integer_number(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    value.as_i64().map(|number| number as f64).or_else(|| {
        value
            .as_str()?
            .parse::<i64>()
            .ok()
            .map(|number| number as f64)
    })
}

fn value_i64(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
}

fn ensure_unique_dates(rows: &[ProxyDailyRow], symbol: &str) -> Result<()> {
    let mut dates = std::collections::BTreeSet::new();
    for row in rows {
        if !dates.insert(row.date) {
            bail!("duplicate dates for {symbol}");
        }
    }
    Ok(())
}

fn timestamp_us_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: Iterator<Item = DateTime<Utc>>,
{
    let data_type = DataType::Timestamp(TimeUnit::Microsecond, Some("UTC".into()));
    fields.push(Field::new(name, data_type.clone(), true));
    let values = values
        .map(|value| value.timestamp_micros())
        .collect::<Vec<_>>();
    arrays.push(Arc::new(
        TimestampMicrosecondArray::from(values).with_data_type(data_type),
    ));
}

fn timestamp_ms_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: Iterator<Item = DateTime<Utc>>,
{
    let data_type = DataType::Timestamp(TimeUnit::Millisecond, Some("UTC".into()));
    fields.push(Field::new(name, data_type.clone(), true));
    let values = values
        .map(|value| value.timestamp_millis())
        .collect::<Vec<_>>();
    arrays.push(Arc::new(
        TimestampMillisecondArray::from(values).with_data_type(data_type),
    ));
}

fn string_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: Iterator<Item = Option<String>>,
{
    fields.push(Field::new(name, DataType::LargeUtf8, true));
    let mut builder = LargeStringBuilder::new();
    for value in values {
        builder.append_option(value.as_deref());
    }
    arrays.push(Arc::new(builder.finish()));
}

fn f64_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: Iterator<Item = Option<f64>>,
{
    fields.push(Field::new(name, DataType::Float64, true));
    let mut builder = Float64Builder::new();
    for value in values {
        builder.append_option(value);
    }
    arrays.push(Arc::new(builder.finish()));
}

fn i64_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: Iterator<Item = Option<i64>>,
{
    fields.push(Field::new(name, DataType::Int64, true));
    let mut builder = Int64Builder::new();
    for value in values {
        builder.append_option(value);
    }
    arrays.push(Arc::new(builder.finish()));
}

fn i64_from_f64_col<I>(
    fields: &mut Vec<Field>,
    arrays: &mut Vec<ArrayRef>,
    name: &str,
    values: I,
) -> Result<()>
where
    I: Iterator<Item = Option<f64>>,
{
    fields.push(Field::new(name, DataType::Int64, true));
    let mut builder = Int64Builder::new();
    for value in values {
        match value {
            None => builder.append_null(),
            Some(value)
                if value.is_finite()
                    && value >= i64::MIN as f64
                    && value <= i64::MAX as f64
                    && value.fract() == 0.0 =>
            {
                builder.append_value(value as i64);
            }
            Some(value) => bail!("{name} must be an integer-compatible value, got {value}"),
        }
    }
    arrays.push(Arc::new(builder.finish()));
    Ok(())
}
