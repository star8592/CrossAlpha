use crate::free_core::{CashRateRow, FreeCoreRange, ProxyDailyRow, FRED_CASH_SERIES};
use anyhow::{Context, Result, bail};
use arrow_array::builder::{Float64Builder, StringBuilder};
use arrow_array::{Array, ArrayRef, Float64Array, Int64Array, RecordBatch, StringArray, TimestampMicrosecondArray, TimestampMillisecondArray, TimestampNanosecondArray, TimestampSecondArray};
use arrow_schema::{DataType, Field, Schema};
use chrono::{DateTime, Duration, NaiveDate, TimeZone, Utc};
use parquet::arrow::ArrowWriter;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
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
        ("tradfi", data_root.join("canonical/core/free_proxy_daily").join(&slug).join("tradfi.parquet")),
        ("crypto", data_root.join("canonical/core/free_proxy_daily").join(&slug).join("crypto.parquet")),
        ("cash", data_root.join("canonical/core/cash_rate").join(&slug).join(format!("{FRED_CASH_SERIES}.parquet"))),
        ("returns", data_root.join("derived/core/free_v01").join(&slug).join("asset_returns.parquet")),
        ("quality", data_root.join("manifests/free_core_quality.json")),
    ])
}

pub fn build_free_core_returns(data_root: &Path, range: &FreeCoreRange) -> Result<Value> {
    let paths = canonical_paths(data_root, range);
    for key in ["tradfi", "crypto", "cash"] {
        let path = &paths[key];
        if !path.exists() {
            bail!("free Core canonical input missing: {}", path.display());
        }
    }

    let mut proxy_rows = read_proxy_parquet(&paths["tradfi"])?;
    proxy_rows.extend(read_proxy_parquet(&paths["crypto"])?);
    let cash = read_cash_parquet(&paths["cash"])?;
    let start = Utc.from_utc_datetime(&range.start.and_hms_opt(0, 0, 0).unwrap());
    let end = Utc.from_utc_datetime(&range.end.and_hms_opt(0, 0, 0).unwrap());
    proxy_rows.retain(|row| row.date >= start && row.date < end);
    let cash: Vec<CashRateRow> = cash.into_iter().filter(|row| row.date >= start && row.date < end).collect();

    let mut by_asset = BTreeMap::<String, Vec<ProxyDailyRow>>::new();
    for row in proxy_rows {
        by_asset.entry(row.economic_asset.clone()).or_default().push(row);
    }
    let mut output = Vec::<AssetReturnRow>::new();
    for rows in by_asset.values_mut() {
        rows.sort_by_key(|row| row.date);
        let mut previous = None::<f64>;
        for row in rows.iter() {
            let price = if row.source == "tiingo_eod" { row.adj_close } else { row.close };
            let daily_return = match (previous, price) {
                (Some(prev), Some(current)) if prev > 0.0 && current > 0.0 => Some(current / prev - 1.0),
                _ => None,
            };
            if let Some(price) = price { previous = Some(price); }
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

    let mut known_rates: Vec<(DateTime<Utc>, f64)> = cash
        .iter()
        .filter_map(|row| row.rate_percent.map(|value| (row.date, value)))
        .collect();
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

    output.sort_by(|left, right| left.date.cmp(&right.date).then(left.economic_asset.cmp(&right.economic_asset)));
    let mut seen = BTreeSet::new();
    for row in &output {
        if row.date < start || row.date >= end {
            bail!("free Core derived returns violate [start, end) semantics");
        }
        if !seen.insert((row.date, row.economic_asset.clone())) {
            bail!("free Core returns contain duplicate date/economic_asset rows");
        }
    }

    let returns_path = &paths["returns"];
    write_returns_parquet(returns_path, &output)?;
    let mut coverage = serde_json::Map::new();
    let mut grouped = BTreeMap::<String, Vec<&AssetReturnRow>>::new();
    for row in &output { grouped.entry(row.economic_asset.clone()).or_default().push(row); }
    for (asset, rows) in grouped {
        let valid: Vec<_> = rows.iter().filter(|row| row.daily_return.is_some()).collect();
        coverage.insert(asset, json!({
            "rows": rows.len(),
            "valid_return_rows": valid.len(),
            "first_return": valid.first().map(|row| row.date.to_rfc3339()),
            "last_return": valid.last().map(|row| row.date.to_rfc3339()),
        }));
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
                economic_asset: string_at(batch.column(asset).as_ref(), index)?.context("economic_asset is null")?,
                source: string_at(batch.column(source).as_ref(), index)?.context("source is null")?,
                symbol: string_at(batch.column(symbol).as_ref(), index)?.context("symbol is null")?,
                price: f64_at(batch.column(price).as_ref(), index)?,
                daily_return: f64_at(batch.column(ret).as_ref(), index)?,
            });
        }
    }
    Ok(rows)
}

fn read_proxy_parquet(path: &Path) -> Result<Vec<ProxyDailyRow>> {
    let batches = read_batches(path)?;
    let mut rows = Vec::new();
    for batch in batches {
        let schema = batch.schema();
        let idx = |name: &str| column_index(schema.as_ref(), name);
        let date = idx("date")?; let economic_asset = idx("economic_asset")?; let source = idx("source")?; let symbol = idx("symbol")?;
        let open = idx("open")?; let high = idx("high")?; let low = idx("low")?; let close = idx("close")?; let volume = idx("volume")?;
        let adj_open = idx("adj_open").ok(); let adj_high = idx("adj_high").ok(); let adj_low = idx("adj_low").ok(); let adj_close = idx("adj_close").ok(); let adj_volume = idx("adj_volume").ok(); let div_cash = idx("div_cash").ok(); let split_factor = idx("split_factor").ok();
        let quote_volume = idx("quote_volume").ok(); let trade_count = idx("trade_count").ok(); let taker_buy_base_volume = idx("taker_buy_base_volume").ok(); let taker_buy_quote_volume = idx("taker_buy_quote_volume").ok();
        for i in 0..batch.num_rows() {
            rows.push(ProxyDailyRow {
                date: timestamp_at(batch.column(date).as_ref(), i)?,
                economic_asset: string_at(batch.column(economic_asset).as_ref(), i)?.context("economic_asset null")?,
                source: string_at(batch.column(source).as_ref(), i)?.context("source null")?,
                symbol: string_at(batch.column(symbol).as_ref(), i)?.context("symbol null")?,
                open: f64_at(batch.column(open).as_ref(), i)?, high: f64_at(batch.column(high).as_ref(), i)?, low: f64_at(batch.column(low).as_ref(), i)?, close: f64_at(batch.column(close).as_ref(), i)?, volume: f64_at(batch.column(volume).as_ref(), i)?,
                adj_open: optional_f64(&batch, adj_open, i)?, adj_high: optional_f64(&batch, adj_high, i)?, adj_low: optional_f64(&batch, adj_low, i)?, adj_close: optional_f64(&batch, adj_close, i)?, adj_volume: optional_f64(&batch, adj_volume, i)?, div_cash: optional_f64(&batch, div_cash, i)?, split_factor: optional_f64(&batch, split_factor, i)?,
                quote_volume: optional_f64(&batch, quote_volume, i)?, trade_count: optional_i64(&batch, trade_count, i)?, taker_buy_base_volume: optional_f64(&batch, taker_buy_base_volume, i)?, taker_buy_quote_volume: optional_f64(&batch, taker_buy_quote_volume, i)?,
            });
        }
    }
    Ok(rows)
}

fn read_cash_parquet(path: &Path) -> Result<Vec<CashRateRow>> {
    let batches = read_batches(path)?; let mut rows=Vec::new();
    for batch in batches { let schema=batch.schema(); let date=column_index(schema.as_ref(),"date")?; let series=column_index(schema.as_ref(),"series_id")?; let rate=column_index(schema.as_ref(),"rate_percent")?; for i in 0..batch.num_rows(){rows.push(CashRateRow{date:timestamp_at(batch.column(date).as_ref(),i)?,series_id:string_at(batch.column(series).as_ref(),i)?.context("series_id null")?,rate_percent:f64_at(batch.column(rate).as_ref(),i)?});}}
    Ok(rows)
}

fn read_batches(path:&Path)->Result<Vec<RecordBatch>> { let file=File::open(path).with_context(||format!("open {}",path.display()))?; let builder=ParquetRecordBatchReaderBuilder::try_new(file)?; Ok(builder.build()?.collect::<std::result::Result<Vec<_>,_>>()?) }
fn column_index(schema:&Schema,name:&str)->Result<usize>{schema.index_of(name).with_context(||format!("missing parquet column {name}"))}
fn optional_f64(batch:&RecordBatch,index:Option<usize>,row:usize)->Result<Option<f64>>{match index{Some(index)=>f64_at(batch.column(index).as_ref(),row),None=>Ok(None)}}
fn optional_i64(batch:&RecordBatch,index:Option<usize>,row:usize)->Result<Option<i64>>{match index{Some(index)=>i64_at(batch.column(index).as_ref(),row),None=>Ok(None)}}
fn f64_at(array:&dyn Array,index:usize)->Result<Option<f64>>{if array.is_null(index){return Ok(None);} if let Some(values)=array.as_any().downcast_ref::<Float64Array>(){return Ok(Some(values.value(index)));} if let Some(values)=array.as_any().downcast_ref::<Int64Array>(){return Ok(Some(values.value(index) as f64));} bail!("unsupported numeric parquet type: {:?}",array.data_type())}
fn i64_at(array:&dyn Array,index:usize)->Result<Option<i64>>{if array.is_null(index){return Ok(None);} if let Some(values)=array.as_any().downcast_ref::<Int64Array>(){return Ok(Some(values.value(index)));} bail!("unsupported integer parquet type: {:?}",array.data_type())}
fn string_at(array:&dyn Array,index:usize)->Result<Option<String>>{if array.is_null(index){return Ok(None);} if let Some(values)=array.as_any().downcast_ref::<StringArray>(){return Ok(Some(values.value(index).to_owned()));} bail!("unsupported string parquet type: {:?}",array.data_type())}
fn timestamp_at(array:&dyn Array,index:usize)->Result<DateTime<Utc>>{if array.is_null(index){bail!("timestamp is null");} if let Some(values)=array.as_any().downcast_ref::<StringArray>(){return Ok(DateTime::parse_from_rfc3339(values.value(index))?.with_timezone(&Utc));} macro_rules! ts {($ty:ty,$div:expr)=>{if let Some(values)=array.as_any().downcast_ref::<$ty>(){let raw=values.value(index);return Utc.timestamp_nanos(raw*$div).single().context("invalid timestamp");}}} ts!(TimestampNanosecondArray,1_i64); ts!(TimestampMicrosecondArray,1_000_i64); ts!(TimestampMillisecondArray,1_000_000_i64); ts!(TimestampSecondArray,1_000_000_000_i64); bail!("unsupported timestamp parquet type: {:?}",array.data_type())}

fn write_returns_parquet(path:&Path,rows:&[AssetReturnRow])->Result<()> { let mut fields=Vec::new(); let mut arrays=Vec::<ArrayRef>::new(); string_col(&mut fields,&mut arrays,"date",rows.iter().map(|r|Some(r.date.to_rfc3339()))); string_col(&mut fields,&mut arrays,"economic_asset",rows.iter().map(|r|Some(r.economic_asset.clone()))); string_col(&mut fields,&mut arrays,"source",rows.iter().map(|r|Some(r.source.clone()))); string_col(&mut fields,&mut arrays,"symbol",rows.iter().map(|r|Some(r.symbol.clone()))); f64_col(&mut fields,&mut arrays,"price",rows.iter().map(|r|r.price)); f64_col(&mut fields,&mut arrays,"return",rows.iter().map(|r|r.daily_return)); if let Some(parent)=path.parent(){fs::create_dir_all(parent)?;} let schema=Arc::new(Schema::new(fields)); let batch=RecordBatch::try_new(schema.clone(),arrays)?; let tmp=path.with_extension("parquet.tmp"); let file=File::create(&tmp)?; let mut writer=ArrowWriter::try_new(file,schema,None)?; writer.write(&batch)?; writer.close()?; OpenOptions::new().write(true).open(&tmp)?.sync_all()?; fs::rename(&tmp,path)?; if let Some(parent)=path.parent(){File::open(parent)?.sync_all()?;} Ok(()) }
fn string_col<I:Iterator<Item=Option<String>>>(fields:&mut Vec<Field>,arrays:&mut Vec<ArrayRef>,name:&str,values:I){fields.push(Field::new(name,DataType::Utf8,true));let mut b=StringBuilder::new();for v in values{b.append_option(v.as_deref());}arrays.push(Arc::new(b.finish()));}
fn f64_col<I:Iterator<Item=Option<f64>>>(fields:&mut Vec<Field>,arrays:&mut Vec<ArrayRef>,name:&str,values:I){fields.push(Field::new(name,DataType::Float64,true));let mut b=Float64Builder::new();for v in values{b.append_option(v);}arrays.push(Arc::new(b.finish()));}
