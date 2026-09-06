use crate::v04::NormalizedVenueRow;
use anyhow::Result;
use arrow_array::builder::{Float64Builder, Int64Builder, StringBuilder};
use arrow_array::{ArrayRef, RecordBatch};
use arrow_schema::{DataType, Field, Schema, SchemaRef};
use chrono::SecondsFormat;
use parquet::arrow::ArrowWriter;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub fn write_venue_rows(path: &Path, rows: &[NormalizedVenueRow]) -> Result<()> {
    let mut fields = Vec::new();
    let mut arrays: Vec<ArrayRef> = Vec::new();

    string_column(&mut fields, &mut arrays, "protocol", rows.iter().map(|r| Some(r.protocol.clone())));
    string_column(&mut fields, &mut arrays, "observed_at", rows.iter().map(|r| Some(r.observed_at.to_rfc3339_opts(SecondsFormat::Micros, false))));
    string_column(&mut fields, &mut arrays, "known_at", rows.iter().map(|r| Some(r.known_at.to_rfc3339_opts(SecondsFormat::Micros, false))));
    string_column(&mut fields, &mut arrays, "venue", rows.iter().map(|r| Some(r.venue.clone())));
    string_column(&mut fields, &mut arrays, "asset", rows.iter().map(|r| Some(r.asset.clone())));
    string_column(&mut fields, &mut arrays, "spot_symbol", rows.iter().map(|r| r.spot_symbol.clone()));
    string_column(&mut fields, &mut arrays, "perp_symbol", rows.iter().map(|r| r.perp_symbol.clone()));
    f64_column(&mut fields, &mut arrays, "spot_bid", rows.iter().map(|r| r.spot_bid));
    f64_column(&mut fields, &mut arrays, "spot_ask", rows.iter().map(|r| r.spot_ask));
    f64_column(&mut fields, &mut arrays, "spot_mid", rows.iter().map(|r| r.spot_mid));
    f64_column(&mut fields, &mut arrays, "spot_spread_bps", rows.iter().map(|r| r.spot_spread_bps));
    f64_column(&mut fields, &mut arrays, "perp_bid", rows.iter().map(|r| r.perp_bid));
    f64_column(&mut fields, &mut arrays, "perp_ask", rows.iter().map(|r| r.perp_ask));
    f64_column(&mut fields, &mut arrays, "perp_mid", rows.iter().map(|r| r.perp_mid));
    f64_column(&mut fields, &mut arrays, "perp_spread_bps", rows.iter().map(|r| r.perp_spread_bps));
    f64_column(&mut fields, &mut arrays, "mark_price", rows.iter().map(|r| r.mark_price));
    f64_column(&mut fields, &mut arrays, "index_price", rows.iter().map(|r| r.index_price));
    f64_column(&mut fields, &mut arrays, "basis_bps", rows.iter().map(|r| r.basis_bps));
    f64_column(&mut fields, &mut arrays, "mark_index_basis_bps", rows.iter().map(|r| r.mark_index_basis_bps));
    string_column(&mut fields, &mut arrays, "funding_semantics", rows.iter().map(|r| Some(r.funding_semantics.clone())));
    f64_column(&mut fields, &mut arrays, "funding_rate_settled_raw", rows.iter().map(|r| r.funding_rate_settled_raw));
    string_column(&mut fields, &mut arrays, "funding_settlement_time", rows.iter().map(|r| r.funding_settlement_time.clone()));
    f64_column(&mut fields, &mut arrays, "funding_interval_hours", rows.iter().map(|r| r.funding_interval_hours));
    f64_column(&mut fields, &mut arrays, "funding_rate_8h", rows.iter().map(|r| r.funding_rate_8h));
    f64_column(&mut fields, &mut arrays, "open_interest_usd", rows.iter().map(|r| r.open_interest_usd));
    i64_column(&mut fields, &mut arrays, "data_cost_usd", rows.iter().map(|r| r.data_cost_usd));
    string_column(&mut fields, &mut arrays, "collection_error", rows.iter().map(|r| r.collection_error.clone()));
    string_column(&mut fields, &mut arrays, "raw_sha256", rows.iter().map(|r| r.raw_sha256.clone()));
    string_column(&mut fields, &mut arrays, "raw_compressed_file_sha256", rows.iter().map(|r| r.raw_compressed_file_sha256.clone()));
    string_column(&mut fields, &mut arrays, "raw_path", rows.iter().map(|r| r.raw_path.clone()));

    let schema: SchemaRef = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    write_batch_atomic(path, schema, batch)
}

fn string_column<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: IntoIterator<Item = Option<String>>,
{
    let mut builder = StringBuilder::new();
    for value in values {
        match value {
            Some(value) => builder.append_value(value),
            None => builder.append_null(),
        }
    }
    fields.push(Field::new(name, DataType::Utf8, true));
    arrays.push(Arc::new(builder.finish()));
}

fn f64_column<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: IntoIterator<Item = Option<f64>>,
{
    let mut builder = Float64Builder::new();
    for value in values {
        match value {
            Some(value) if value.is_finite() => builder.append_value(value),
            _ => builder.append_null(),
        }
    }
    fields.push(Field::new(name, DataType::Float64, true));
    arrays.push(Arc::new(builder.finish()));
}

fn i64_column<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: IntoIterator<Item = i64>,
{
    let mut builder = Int64Builder::new();
    for value in values {
        builder.append_value(value);
    }
    fields.push(Field::new(name, DataType::Int64, true));
    arrays.push(Arc::new(builder.finish()));
}

fn write_batch_atomic(path: &Path, schema: SchemaRef, batch: RecordBatch) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = temp_path(path);
    if tmp.exists() {
        fs::remove_file(&tmp)?;
    }
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

fn temp_path(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".tmp");
    PathBuf::from(value)
}
