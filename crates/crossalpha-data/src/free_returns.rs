use crate::free_core::{
    CashRateRow, FRED_CASH_SERIES, FREE_CRYPTO_PROXIES, FREE_TRADFI_PROXIES, FreeCoreRange,
    ProxyDailyRow,
};
use anyhow::{Context, Result, bail};
use arrow_array::builder::{Float64Builder, LargeStringBuilder};
use arrow_array::{
    Array, ArrayRef, Float64Array, Int64Array, LargeStringArray, RecordBatch, StringArray,
    TimestampMicrosecondArray, TimestampMillisecondArray, TimestampNanosecondArray,
    TimestampSecondArray,
};
use arrow_schema::{DataType, Field, Schema, TimeUnit};
use chrono::{DateTime, Duration, TimeZone, Utc};
use parquet::arrow::ArrowWriter;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AssetReturnRow {
    pub date: DateTime<Utc>,
    pub economic_asset: String,
    pub source: String,
    pub symbol: String,
    pub price: Option<f64>,
    pub daily_return: Option<f64>,
}

pub fn canonical_paths(data_root: &Path, range: &FreeCoreRange) -> BTreeMap<&'static str, PathBuf> {
    let slug = range.slug();
    BTreeMap::from([
        (
            "tradfi",
            data_root
                .join("canonical/core/free_proxy_daily")
                .join(&slug)
                .join("tradfi.parquet"),
        ),
        (
            "crypto",
            data_root
                .join("canonical/core/free_proxy_daily")
                .join(&slug)
                .join("crypto.parquet"),
        ),
        (
            "cash",
            data_root
                .join("canonical/core/cash_rate")
                .join(&slug)
                .join(format!("{FRED_CASH_SERIES}.parquet")),
        ),
        (
            "returns",
            data_root
                .join("derived/core/free_v01")
                .join(&slug)
                .join("asset_returns.parquet"),
        ),
        (
            "quality",
            data_root.join("manifests/free_core_quality.json"),
        ),
    ])
}

pub fn audit_free_core(data_root: &Path, range: &FreeCoreRange) -> Result<Value> {
    let paths = canonical_paths(data_root, range);
    let missing_files = ["tradfi", "crypto", "cash"]
        .iter()
        .filter_map(|key| {
            let path = &paths[key];
            (!path.exists()).then(|| path.display().to_string())
        })
        .collect::<Vec<_>>();
    if !missing_files.is_empty() {
        let report = json!({
            "ok": false,
            "mode": "free_only",
            "data_cost_usd": 0,
            "range_semantics": "start_inclusive_end_exclusive",
            "canonical_vendor_boundary_policy": "preserve_exact_end_then_drop_in_derived",
            "start": range.start.to_string(),
            "end": range.end.to_string(),
            "missing_files": missing_files,
        });
        write_json_atomic(&paths["quality"], &report)?;
        return Ok(report);
    }

    let tradfi = read_proxy_parquet(&paths["tradfi"])?;
    let crypto = read_proxy_parquet(&paths["crypto"])?;
    let cash = read_cash_parquet(&paths["cash"])?;
    let start = Utc.from_utc_datetime(&range.start.and_hms_opt(0, 0, 0).unwrap());
    let end = Utc.from_utc_datetime(&range.end.and_hms_opt(0, 0, 0).unwrap());

    let tradfi_expected = FREE_TRADFI_PROXIES
        .iter()
        .map(|(asset, _)| (*asset).to_owned())
        .collect::<BTreeSet<_>>();
    let crypto_expected = FREE_CRYPTO_PROXIES
        .iter()
        .map(|(asset, _)| (*asset).to_owned())
        .collect::<BTreeSet<_>>();
    let tradfi_audit = proxy_audit(&tradfi, &tradfi_expected, "tiingo_eod", start, end, |row| {
        row.adj_close
    });
    let crypto_audit = proxy_audit(
        &crypto,
        &crypto_expected,
        "binance_spot_public",
        start,
        end,
        |row| row.close,
    );

    let mut dates = BTreeSet::new();
    let mut duplicate_dates = 0usize;
    let mut rows_before_start = 0usize;
    let mut rows_exact_end = 0usize;
    let mut rows_after_end = 0usize;
    let mut known = Vec::<&CashRateRow>::new();
    for row in &cash {
        if !dates.insert(row.date) {
            duplicate_dates += 1;
        }
        if row.date < start {
            rows_before_start += 1;
        } else if row.date == end {
            rows_exact_end += 1;
        } else if row.date > end {
            rows_after_end += 1;
        }
        if row.rate_percent.is_some_and(f64::is_finite) {
            known.push(row);
        }
    }
    known.sort_by_key(|row| row.date);
    let cash_ok = !cash.is_empty()
        && duplicate_dates == 0
        && rows_before_start == 0
        && rows_after_end == 0
        && !known.is_empty();
    let cash_audit = json!({
        "ok": cash_ok,
        "source": "fred",
        "series_id": FRED_CASH_SERIES,
        "rows": cash.len(),
        "known_rate_rows": known.len(),
        "missing_rate_rows": cash.iter().filter(|row| row.rate_percent.is_none()).count(),
        "duplicate_dates": duplicate_dates,
        "rows_before_start": rows_before_start,
        "rows_exactly_at_exclusive_end": rows_exact_end,
        "rows_strictly_after_exclusive_end": rows_after_end,
        "boundary_rows_normalized_in_derived": true,
        "start": cash.iter().map(|row| row.date).min().map(|value| value.to_rfc3339()),
        "end": cash.iter().map(|row| row.date).max().map(|value| value.to_rfc3339()),
        "first_known_rate": known.first().map(|row| row.date.to_rfc3339()),
        "last_known_rate": known.last().map(|row| row.date.to_rfc3339()),
    });

    let ok = tradfi_audit.get("ok").and_then(Value::as_bool) == Some(true)
        && crypto_audit.get("ok").and_then(Value::as_bool) == Some(true)
        && cash_ok;
    let report = json!({
        "ok": ok,
        "mode": "free_only",
        "data_cost_usd": 0,
        "range_semantics": "start_inclusive_end_exclusive",
        "canonical_vendor_boundary_policy": "preserve_exact_end_then_drop_in_derived",
        "start": range.start.to_string(),
        "end": range.end.to_string(),
        "missing_files": [],
        "tradfi": tradfi_audit,
        "crypto": crypto_audit,
        "cash": cash_audit,
    });
    write_json_atomic(&paths["quality"], &report)?;
    Ok(report)
}

pub fn build_free_core_returns(data_root: &Path, range: &FreeCoreRange) -> Result<Value> {
    let quality = audit_free_core(data_root, range)?;
    if quality.get("ok").and_then(Value::as_bool) != Some(true) {
        bail!("free Core quality gate failed; inspect manifests/free_core_quality.json");
    }

    let paths = canonical_paths(data_root, range);
    let mut proxy_rows = read_proxy_parquet(&paths["tradfi"])?;
    proxy_rows.extend(read_proxy_parquet(&paths["crypto"])?);
    let cash = read_cash_parquet(&paths["cash"])?;
    let start = Utc.from_utc_datetime(&range.start.and_hms_opt(0, 0, 0).unwrap());
    let end = Utc.from_utc_datetime(&range.end.and_hms_opt(0, 0, 0).unwrap());
    proxy_rows.retain(|row| row.date >= start && row.date < end);
    let cash = cash
        .into_iter()
        .filter(|row| row.date >= start && row.date < end)
        .collect::<Vec<_>>();

    let mut by_asset = BTreeMap::<String, Vec<ProxyDailyRow>>::new();
    for row in proxy_rows {
        by_asset
            .entry(row.economic_asset.clone())
            .or_default()
            .push(row);
    }
    let mut output = Vec::<AssetReturnRow>::new();
    for rows in by_asset.values_mut() {
        rows.sort_by_key(|row| row.date);
        let mut previous = None::<f64>;
        for row in rows.iter() {
            let price = if row.source == "tiingo_eod" {
                row.adj_close
            } else {
                row.close
            };
            let daily_return = match (previous, price) {
                (Some(previous), Some(current)) if previous > 0.0 && current > 0.0 => {
                    Some(current / previous - 1.0)
                }
                _ => None,
            };
            if let Some(price) = price {
                previous = Some(price);
            }
            output.push(AssetReturnRow {
                date: row.date,
                economic_asset: row.economic_asset.clone(),
                source: row.source.clone(),
                symbol: row.symbol.clone(),
                price,
                daily_return,
            });
        }
    }

    let mut known_rates = cash
        .iter()
        .filter_map(|row| row.rate_percent.map(|value| (row.date, value)))
        .collect::<Vec<_>>();
    known_rates.sort_by_key(|row| row.0);
    let mut day = range.start;
    while day < range.end {
        let at = Utc.from_utc_datetime(&day.and_hms_opt(0, 0, 0).unwrap());
        let index = known_rates.partition_point(|(known_at, _)| *known_at < at);
        let daily_return = if index == 0 {
            None
        } else {
            let annual_percent = known_rates[index - 1].1;
            Some((1.0 + annual_percent / 100.0).powf(1.0 / 365.0) - 1.0)
        };
        output.push(AssetReturnRow {
            date: at,
            economic_asset: "CASH".to_owned(),
            source: "fred".to_owned(),
            symbol: FRED_CASH_SERIES.to_owned(),
            price: None,
            daily_return,
        });
        day += Duration::days(1);
    }

    output.sort_by(|left, right| {
        left.date
            .cmp(&right.date)
            .then(left.economic_asset.cmp(&right.economic_asset))
    });
    let mut seen = BTreeSet::new();
    for row in &output {
        if row.date < start || row.date >= end {
            bail!("free Core derived returns violate [start, end) range semantics");
        }
        if !seen.insert((row.date, row.economic_asset.clone())) {
            bail!("free Core returns contain duplicate date/economic_asset rows");
        }
    }

    let returns_path = &paths["returns"];
    write_returns_parquet(returns_path, &output)?;
    let mut coverage = Map::new();
    let mut grouped = BTreeMap::<String, Vec<&AssetReturnRow>>::new();
    for row in &output {
        grouped
            .entry(row.economic_asset.clone())
            .or_default()
            .push(row);
    }
    for (asset, rows) in grouped {
        let valid = rows
            .iter()
            .filter(|row| row.daily_return.is_some())
            .copied()
            .collect::<Vec<_>>();
        coverage.insert(
            asset,
            json!({
                "rows": rows.len(),
                "valid_return_rows": valid.len(),
                "first_return": valid.first().map(|row| row.date.to_rfc3339()),
                "last_return": valid.last().map(|row| row.date.to_rfc3339()),
            }),
        );
    }
    Ok(json!({
        "mode":"free_only",
        "data_cost_usd":0,
        "range_semantics":"start_inclusive_end_exclusive",
        "canonical_vendor_boundary_policy":"preserve_exact_end_then_drop_in_derived",
        "rows":output.len(),
        "assets":coverage.keys().cloned().collect::<Vec<_>>(),
        "coverage":coverage,
        "output":returns_path,
        "quality_report":paths["quality"],
    }))
}

pub fn read_asset_returns(path: &Path) -> Result<Vec<AssetReturnRow>> {
    let batches = read_batches(path)?;
    let mut rows = Vec::new();
    for batch in batches {
        let date = column_index(batch.schema().as_ref(), "date")?;
        let asset = column_index(batch.schema().as_ref(), "economic_asset")?;
        let source = column_index(batch.schema().as_ref(), "source")?;
        let symbol = column_index(batch.schema().as_ref(), "symbol")?;
        let price = column_index(batch.schema().as_ref(), "price")?;
        let ret = column_index(batch.schema().as_ref(), "return")?;
        for index in 0..batch.num_rows() {
            rows.push(AssetReturnRow {
                date: timestamp_at(batch.column(date).as_ref(), index)?,
                economic_asset: string_at(batch.column(asset).as_ref(), index)?
                    .context("economic_asset is null")?,
                source: string_at(batch.column(source).as_ref(), index)?
                    .context("source is null")?,
                symbol: string_at(batch.column(symbol).as_ref(), index)?
                    .context("symbol is null")?,
                price: f64_at(batch.column(price).as_ref(), index)?,
                daily_return: f64_at(batch.column(ret).as_ref(), index)?,
            });
        }
    }
    Ok(rows)
}

fn proxy_audit<F>(
    rows: &[ProxyDailyRow],
    expected_assets: &BTreeSet<String>,
    source_name: &str,
    start: DateTime<Utc>,
    end: DateTime<Utc>,
    price: F,
) -> Value
where
    F: Fn(&ProxyDailyRow) -> Option<f64>,
{
    let found = rows
        .iter()
        .map(|row| row.economic_asset.clone())
        .collect::<BTreeSet<_>>();
    let assets = expected_assets
        .union(&found)
        .cloned()
        .collect::<BTreeSet<_>>();
    let missing_assets = expected_assets
        .difference(&found)
        .cloned()
        .collect::<Vec<_>>();
    let mut source_ok = true;
    let mut series = Map::new();
    for asset in assets {
        let mut part = rows
            .iter()
            .filter(|row| row.economic_asset == asset)
            .collect::<Vec<_>>();
        part.sort_by_key(|row| row.date);
        if part.is_empty() {
            source_ok = false;
            series.insert(
                asset,
                json!({
                    "ok":false,"rows":0,"start":Value::Null,"end":Value::Null,
                    "duplicate_dates":0,"null_price":0,"non_positive_price":0,
                    "rows_before_start":0,"rows_exactly_at_exclusive_end":0,
                    "rows_strictly_after_exclusive_end":0,
                    "boundary_rows_normalized_in_derived":true,"max_calendar_gap_days":Value::Null,
                }),
            );
            continue;
        }
        let mut seen = BTreeSet::new();
        let mut duplicate_dates = 0usize;
        let mut null_price = 0usize;
        let mut non_positive = 0usize;
        let mut before = 0usize;
        let mut exact_end = 0usize;
        let mut after = 0usize;
        let mut max_gap: Option<f64> = None;
        let mut previous_date: Option<DateTime<Utc>> = None;
        for row in &part {
            if !seen.insert(row.date) {
                duplicate_dates += 1;
            }
            match price(row) {
                None => null_price += 1,
                Some(value) if !value.is_finite() || value <= 0.0 => non_positive += 1,
                Some(_) => {}
            }
            if row.date < start {
                before += 1;
            } else if row.date == end {
                exact_end += 1;
            } else if row.date > end {
                after += 1;
            }
            if let Some(previous) = previous_date {
                let gap = row.date.signed_duration_since(previous).num_seconds() as f64 / 86_400.0;
                max_gap = Some(max_gap.map_or(gap, |current| current.max(gap)));
            }
            previous_date = Some(row.date);
        }
        let item_ok = duplicate_dates == 0
            && null_price == 0
            && non_positive == 0
            && before == 0
            && after == 0;
        source_ok &= item_ok;
        series.insert(
            asset,
            json!({
                "ok":item_ok,
                "rows":part.len(),
                "start":part.first().map(|row|row.date.to_rfc3339()),
                "end":part.last().map(|row|row.date.to_rfc3339()),
                "duplicate_dates":duplicate_dates,
                "null_price":null_price,
                "non_positive_price":non_positive,
                "rows_before_start":before,
                "rows_exactly_at_exclusive_end":exact_end,
                "rows_strictly_after_exclusive_end":after,
                "boundary_rows_normalized_in_derived":true,
                "max_calendar_gap_days":max_gap,
            }),
        );
    }
    json!({
        "ok": source_ok && missing_assets.is_empty(),
        "source": source_name,
        "missing_columns": [],
        "missing_assets": missing_assets,
        "series": series,
    })
}

fn read_proxy_parquet(path: &Path) -> Result<Vec<ProxyDailyRow>> {
    let batches = read_batches(path)?;
    let mut rows = Vec::new();
    for batch in batches {
        let schema = batch.schema();
        let idx = |name: &str| column_index(schema.as_ref(), name);
        let date = idx("date")?;
        let economic_asset = idx("economic_asset")?;
        let source = idx("source")?;
        let symbol = idx("symbol")?;
        let open = idx("open")?;
        let high = idx("high")?;
        let low = idx("low")?;
        let close = idx("close")?;
        let volume = idx("volume")?;
        let adj_open = idx("adj_open").ok();
        let adj_high = idx("adj_high").ok();
        let adj_low = idx("adj_low").ok();
        let adj_close = idx("adj_close").ok();
        let adj_volume = idx("adj_volume").ok();
        let div_cash = idx("div_cash").ok();
        let split_factor = idx("split_factor").ok();
        let quote_volume = idx("quote_volume").ok();
        let trade_count = idx("trade_count").ok();
        let taker_buy_base_volume = idx("taker_buy_base_volume").ok();
        let taker_buy_quote_volume = idx("taker_buy_quote_volume").ok();
        for row in 0..batch.num_rows() {
            rows.push(ProxyDailyRow {
                date: timestamp_at(batch.column(date).as_ref(), row)?,
                economic_asset: string_at(batch.column(economic_asset).as_ref(), row)?
                    .context("economic_asset null")?,
                source: string_at(batch.column(source).as_ref(), row)?.context("source null")?,
                symbol: string_at(batch.column(symbol).as_ref(), row)?.context("symbol null")?,
                open: f64_at(batch.column(open).as_ref(), row)?,
                high: f64_at(batch.column(high).as_ref(), row)?,
                low: f64_at(batch.column(low).as_ref(), row)?,
                close: f64_at(batch.column(close).as_ref(), row)?,
                volume: f64_at(batch.column(volume).as_ref(), row)?,
                adj_open: optional_f64(&batch, adj_open, row)?,
                adj_high: optional_f64(&batch, adj_high, row)?,
                adj_low: optional_f64(&batch, adj_low, row)?,
                adj_close: optional_f64(&batch, adj_close, row)?,
                adj_volume: optional_f64(&batch, adj_volume, row)?,
                div_cash: optional_f64(&batch, div_cash, row)?,
                split_factor: optional_f64(&batch, split_factor, row)?,
                quote_volume: optional_f64(&batch, quote_volume, row)?,
                trade_count: optional_i64(&batch, trade_count, row)?,
                taker_buy_base_volume: optional_f64(&batch, taker_buy_base_volume, row)?,
                taker_buy_quote_volume: optional_f64(&batch, taker_buy_quote_volume, row)?,
            });
        }
    }
    Ok(rows)
}

fn read_cash_parquet(path: &Path) -> Result<Vec<CashRateRow>> {
    let batches = read_batches(path)?;
    let mut rows = Vec::new();
    for batch in batches {
        let schema = batch.schema();
        let date = column_index(schema.as_ref(), "date")?;
        let series = column_index(schema.as_ref(), "series_id")?;
        let rate = column_index(schema.as_ref(), "rate_percent")?;
        for row in 0..batch.num_rows() {
            rows.push(CashRateRow {
                date: timestamp_at(batch.column(date).as_ref(), row)?,
                series_id: string_at(batch.column(series).as_ref(), row)?
                    .context("series_id null")?,
                rate_percent: f64_at(batch.column(rate).as_ref(), row)?,
            });
        }
    }
    Ok(rows)
}

fn read_batches(path: &Path) -> Result<Vec<RecordBatch>> {
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    Ok(builder
        .build()?
        .collect::<std::result::Result<Vec<_>, _>>()?)
}

fn column_index(schema: &Schema, name: &str) -> Result<usize> {
    schema
        .index_of(name)
        .with_context(|| format!("missing parquet column {name}"))
}

fn optional_f64(batch: &RecordBatch, index: Option<usize>, row: usize) -> Result<Option<f64>> {
    match index {
        Some(index) => f64_at(batch.column(index).as_ref(), row),
        None => Ok(None),
    }
}

fn optional_i64(batch: &RecordBatch, index: Option<usize>, row: usize) -> Result<Option<i64>> {
    match index {
        Some(index) => i64_at(batch.column(index).as_ref(), row),
        None => Ok(None),
    }
}

fn f64_at(array: &dyn Array, index: usize) -> Result<Option<f64>> {
    if array.is_null(index) {
        return Ok(None);
    }
    if let Some(values) = array.as_any().downcast_ref::<Float64Array>() {
        return Ok(Some(values.value(index)));
    }
    if let Some(values) = array.as_any().downcast_ref::<Int64Array>() {
        return Ok(Some(values.value(index) as f64));
    }
    bail!("unsupported numeric parquet type: {:?}", array.data_type())
}

fn i64_at(array: &dyn Array, index: usize) -> Result<Option<i64>> {
    if array.is_null(index) {
        return Ok(None);
    }
    if let Some(values) = array.as_any().downcast_ref::<Int64Array>() {
        return Ok(Some(values.value(index)));
    }
    bail!("unsupported integer parquet type: {:?}", array.data_type())
}

fn string_at(array: &dyn Array, index: usize) -> Result<Option<String>> {
    if array.is_null(index) {
        return Ok(None);
    }
    if let Some(values) = array.as_any().downcast_ref::<StringArray>() {
        return Ok(Some(values.value(index).to_owned()));
    }
    if let Some(values) = array.as_any().downcast_ref::<LargeStringArray>() {
        return Ok(Some(values.value(index).to_owned()));
    }
    bail!("unsupported string parquet type: {:?}", array.data_type())
}

fn timestamp_at(array: &dyn Array, index: usize) -> Result<DateTime<Utc>> {
    if array.is_null(index) {
        bail!("timestamp is null");
    }
    if let Some(values) = array.as_any().downcast_ref::<StringArray>() {
        return Ok(DateTime::parse_from_rfc3339(values.value(index))?.with_timezone(&Utc));
    }
    if let Some(values) = array.as_any().downcast_ref::<TimestampNanosecondArray>() {
        return datetime_from_nanos(values.value(index));
    }
    if let Some(values) = array.as_any().downcast_ref::<TimestampMicrosecondArray>() {
        return datetime_from_nanos(values.value(index).saturating_mul(1_000));
    }
    if let Some(values) = array.as_any().downcast_ref::<TimestampMillisecondArray>() {
        return datetime_from_nanos(values.value(index).saturating_mul(1_000_000));
    }
    if let Some(values) = array.as_any().downcast_ref::<TimestampSecondArray>() {
        return datetime_from_nanos(values.value(index).saturating_mul(1_000_000_000));
    }
    bail!(
        "unsupported timestamp parquet type: {:?}",
        array.data_type()
    )
}

fn datetime_from_nanos(value: i64) -> Result<DateTime<Utc>> {
    let seconds = value.div_euclid(1_000_000_000);
    let nanos = value.rem_euclid(1_000_000_000) as u32;
    DateTime::<Utc>::from_timestamp(seconds, nanos).context("invalid timestamp")
}

fn write_returns_parquet(path: &Path, rows: &[AssetReturnRow]) -> Result<()> {
    let mut fields = Vec::new();
    let mut arrays = Vec::<ArrayRef>::new();
    timestamp_col(
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
        "price",
        rows.iter().map(|row| row.price),
    );
    f64_col(
        &mut fields,
        &mut arrays,
        "return",
        rows.iter().map(|row| row.daily_return),
    );
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

fn timestamp_col<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
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

fn write_json_atomic(path: &Path, value: &Value) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_vec_pretty(value)?)?;
    OpenOptions::new().write(true).open(&tmp)?.sync_all()?;
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        File::open(parent)?.sync_all()?;
    }
    Ok(())
}
