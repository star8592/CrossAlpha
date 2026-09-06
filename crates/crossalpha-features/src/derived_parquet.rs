use crate::{HyperliquidMarketStateRow, StablecoinChainStateRow, StablecoinSystemStateRow};
use anyhow::{Result, bail};
use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, StringBuilder};
use arrow_array::{ArrayRef, NullArray, RecordBatch};
use arrow_schema::{DataType, Field, Schema, SchemaRef};
use chrono::SecondsFormat;
use parquet::arrow::ArrowWriter;
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub fn write_market_state_parquet(rows: &[HyperliquidMarketStateRow], path: &Path) -> Result<()> {
    if rows.is_empty() {
        bail!("cannot write empty Hyperliquid market-state parquet");
    }
    let mut fields = Vec::new();
    let mut arrays = Vec::new();
    push_string(&mut fields, &mut arrays, "observed_at", rows.iter().map(|row| Some(row.observed_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))));
    push_string(&mut fields, &mut arrays, "known_at", rows.iter().map(|row| Some(row.known_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))));
    push_string(&mut fields, &mut arrays, "asset", rows.iter().map(|row| Some(row.asset.clone())));
    push_value(&mut fields, &mut arrays, "sz_decimals", rows.iter().map(|row| &row.sz_decimals))?;
    push_value(&mut fields, &mut arrays, "max_leverage", rows.iter().map(|row| &row.max_leverage))?;
    push_value(&mut fields, &mut arrays, "only_isolated", rows.iter().map(|row| &row.only_isolated))?;
    push_f64(&mut fields, &mut arrays, "mark_price", rows.iter().map(|row| row.mark_price));
    push_f64(&mut fields, &mut arrays, "oracle_price", rows.iter().map(|row| row.oracle_price));
    push_f64(&mut fields, &mut arrays, "mid_price", rows.iter().map(|row| row.mid_price));
    push_f64(&mut fields, &mut arrays, "prev_day_price", rows.iter().map(|row| row.prev_day_price));
    push_f64(&mut fields, &mut arrays, "premium", rows.iter().map(|row| row.premium));
    push_f64(&mut fields, &mut arrays, "funding_rate", rows.iter().map(|row| row.funding_rate));
    push_f64(&mut fields, &mut arrays, "open_interest", rows.iter().map(|row| row.open_interest));
    push_f64(&mut fields, &mut arrays, "day_notional_volume", rows.iter().map(|row| row.day_notional_volume));
    push_f64(&mut fields, &mut arrays, "day_base_volume", rows.iter().map(|row| row.day_base_volume));
    push_f64(&mut fields, &mut arrays, "impact_bid", rows.iter().map(|row| row.impact_bid));
    push_f64(&mut fields, &mut arrays, "impact_ask", rows.iter().map(|row| row.impact_ask));
    push_string(&mut fields, &mut arrays, "raw_sha256", rows.iter().map(|row| Some(row.raw_sha256.clone())));
    push_string(&mut fields, &mut arrays, "raw_path", rows.iter().map(|row| Some(row.raw_path.clone())));
    push_f64(&mut fields, &mut arrays, "mark_oracle_basis_bps", rows.iter().map(|row| row.mark_oracle_basis_bps));
    push_f64(&mut fields, &mut arrays, "impact_spread_bps", rows.iter().map(|row| row.impact_spread_bps));
    push_f64(&mut fields, &mut arrays, "day_return", rows.iter().map(|row| row.day_return));
    push_f64(&mut fields, &mut arrays, "funding_bps", rows.iter().map(|row| row.funding_bps));
    push_f64(&mut fields, &mut arrays, "premium_bps", rows.iter().map(|row| row.premium_bps));
    push_f64(&mut fields, &mut arrays, "open_interest_notional", rows.iter().map(|row| row.open_interest_notional));
    push_f64(&mut fields, &mut arrays, "observation_interval_seconds", rows.iter().map(|row| row.observation_interval_seconds));
    push_f64(&mut fields, &mut arrays, "open_interest_change_pct", rows.iter().map(|row| row.open_interest_change_pct));
    push_f64(&mut fields, &mut arrays, "open_interest_notional_change_pct", rows.iter().map(|row| row.open_interest_notional_change_pct));
    push_f64(&mut fields, &mut arrays, "funding_change", rows.iter().map(|row| row.funding_change));
    push_f64(&mut fields, &mut arrays, "basis_change_bps", rows.iter().map(|row| row.basis_change_bps));
    push_f64(&mut fields, &mut arrays, "funding_z_24h", rows.iter().map(|row| row.funding_z_24h));
    push_f64(&mut fields, &mut arrays, "basis_z_24h", rows.iter().map(|row| row.basis_z_24h));
    push_f64(&mut fields, &mut arrays, "oi_change_z_24h", rows.iter().map(|row| row.oi_change_z_24h));
    push_f64(&mut fields, &mut arrays, "spread_z_24h", rows.iter().map(|row| row.spread_z_24h));
    push_i64(&mut fields, &mut arrays, "rolling_observations_24h", rows.iter().map(|row| row.rolling_observations_24h));
    push_i64(&mut fields, &mut arrays, "feature_schema_version", rows.iter().map(|row| i64::from(row.feature_schema_version)));
    write_batch_atomic(path, fields, arrays)
}

pub fn write_stablecoin_system_state_parquet(rows: &[StablecoinSystemStateRow], path: &Path) -> Result<()> {
    if rows.is_empty() {
        bail!("cannot write empty stablecoin system-state parquet");
    }
    let mut fields = Vec::new();
    let mut arrays = Vec::new();
    push_string(&mut fields, &mut arrays, "observed_at", rows.iter().map(|row| Some(row.observed_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))));
    push_string(&mut fields, &mut arrays, "known_at", rows.iter().map(|row| Some(row.known_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))));
    push_i64(&mut fields, &mut arrays, "usd_stablecoin_count", rows.iter().map(|row| row.usd_stablecoin_count));
    push_required_f64(&mut fields, &mut arrays, "usd_supply_native", rows.iter().map(|row| row.usd_supply_native));
    push_required_f64(&mut fields, &mut arrays, "usd_market_value_usd", rows.iter().map(|row| row.usd_market_value_usd));
    push_f64(&mut fields, &mut arrays, "usd_delta_1d_native", rows.iter().map(|row| row.usd_delta_1d_native));
    push_f64(&mut fields, &mut arrays, "usd_delta_7d_native", rows.iter().map(|row| row.usd_delta_7d_native));
    push_f64(&mut fields, &mut arrays, "usd_delta_30d_native", rows.iter().map(|row| row.usd_delta_30d_native));
    push_f64(&mut fields, &mut arrays, "delta_1d_market_value_coverage", rows.iter().map(|row| row.delta_1d_market_value_coverage));
    push_f64(&mut fields, &mut arrays, "delta_7d_market_value_coverage", rows.iter().map(|row| row.delta_7d_market_value_coverage));
    push_f64(&mut fields, &mut arrays, "delta_30d_market_value_coverage", rows.iter().map(|row| row.delta_30d_market_value_coverage));
    push_f64(&mut fields, &mut arrays, "usdt_market_value_usd", rows.iter().map(|row| row.usdt_market_value_usd));
    push_f64(&mut fields, &mut arrays, "usdc_market_value_usd", rows.iter().map(|row| row.usdc_market_value_usd));
    push_f64(&mut fields, &mut arrays, "usdt_share", rows.iter().map(|row| row.usdt_share));
    push_f64(&mut fields, &mut arrays, "usdc_share", rows.iter().map(|row| row.usdc_share));
    push_f64(&mut fields, &mut arrays, "asset_hhi", rows.iter().map(|row| row.asset_hhi));
    push_f64(&mut fields, &mut arrays, "weighted_abs_peg_deviation_bps", rows.iter().map(|row| row.weighted_abs_peg_deviation_bps));
    push_f64(&mut fields, &mut arrays, "max_abs_peg_deviation_bps", rows.iter().map(|row| row.max_abs_peg_deviation_bps));
    push_required_f64(&mut fields, &mut arrays, "offpeg_50bps_market_value_usd", rows.iter().map(|row| row.offpeg_50bps_market_value_usd));
    push_required_f64(&mut fields, &mut arrays, "chain_sum_native", rows.iter().map(|row| row.chain_sum_native));
    push_f64(&mut fields, &mut arrays, "chain_coverage_ratio", rows.iter().map(|row| row.chain_coverage_ratio));
    push_required_f64(&mut fields, &mut arrays, "chain_residual_native", rows.iter().map(|row| row.chain_residual_native));
    push_required_f64(&mut fields, &mut arrays, "chain_abs_residual_native", rows.iter().map(|row| row.chain_abs_residual_native));
    push_f64(&mut fields, &mut arrays, "chain_abs_residual_ratio", rows.iter().map(|row| row.chain_abs_residual_ratio));
    push_i64(&mut fields, &mut arrays, "feature_schema_version", rows.iter().map(|row| i64::from(row.feature_schema_version)));
    write_batch_atomic(path, fields, arrays)
}

pub fn write_stablecoin_chain_state_parquet(rows: &[StablecoinChainStateRow], path: &Path) -> Result<()> {
    if rows.is_empty() {
        bail!("cannot write empty stablecoin chain-state parquet");
    }
    let mut fields = Vec::new();
    let mut arrays = Vec::new();
    push_string(&mut fields, &mut arrays, "observed_at", rows.iter().map(|row| Some(row.observed_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))));
    push_string(&mut fields, &mut arrays, "known_at", rows.iter().map(|row| Some(row.known_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))));
    push_string(&mut fields, &mut arrays, "chain", rows.iter().map(|row| Some(row.chain.clone())));
    push_f64(&mut fields, &mut arrays, "circulating_native", rows.iter().map(|row| row.circulating_native));
    push_f64(&mut fields, &mut arrays, "market_value_usd", rows.iter().map(|row| row.market_value_usd));
    push_f64(&mut fields, &mut arrays, "market_share", rows.iter().map(|row| row.market_share));
    push_i64(&mut fields, &mut arrays, "stablecoin_count", rows.iter().map(|row| row.stablecoin_count));
    push_f64(&mut fields, &mut arrays, "system_chain_hhi", rows.iter().map(|row| row.system_chain_hhi));
    push_i64(&mut fields, &mut arrays, "feature_schema_version", rows.iter().map(|row| i64::from(row.feature_schema_version)));
    write_batch_atomic(path, fields, arrays)
}

fn write_batch_atomic(path: &Path, fields: Vec<Field>, arrays: Vec<ArrayRef>) -> Result<()> {
    if fields.len() != arrays.len() {
        bail!("derived parquet field/array length mismatch");
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let tmp = temp_path(path);
    if tmp.exists() {
        fs::remove_file(&tmp)?;
    }
    let schema: SchemaRef = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
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

fn push_string<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
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

fn push_i64<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
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

fn push_required_f64<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: IntoIterator<Item = f64>,
{
    let mut builder = Float64Builder::new();
    for value in values {
        builder.append_value(value);
    }
    fields.push(Field::new(name, DataType::Float64, true));
    arrays.push(Arc::new(builder.finish()));
}

fn push_f64<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
where
    I: IntoIterator<Item = Option<f64>>,
{
    let mut builder = Float64Builder::new();
    for value in values {
        match value.filter(|value| value.is_finite()) {
            Some(value) => builder.append_value(value),
            None => builder.append_null(),
        }
    }
    fields.push(Field::new(name, DataType::Float64, true));
    arrays.push(Arc::new(builder.finish()));
}

fn push_value<'a, I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I) -> Result<()>
where
    I: IntoIterator<Item = &'a Value>,
{
    let values: Vec<&Value> = values.into_iter().collect();
    let non_null: Vec<&Value> = values.iter().copied().filter(|value| !value.is_null()).collect();
    if non_null.is_empty() {
        fields.push(Field::new(name, DataType::Null, true));
        arrays.push(Arc::new(NullArray::new(values.len())));
        return Ok(());
    }
    if non_null.iter().all(|value| value.is_boolean()) {
        let mut builder = BooleanBuilder::new();
        for value in values {
            match value.as_bool() {
                Some(value) => builder.append_value(value),
                None => builder.append_null(),
            }
        }
        fields.push(Field::new(name, DataType::Boolean, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }
    if non_null.iter().all(|value| value.as_i64().is_some()) {
        let mut builder = Int64Builder::new();
        for value in values {
            match value.as_i64() {
                Some(value) => builder.append_value(value),
                None => builder.append_null(),
            }
        }
        fields.push(Field::new(name, DataType::Int64, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }
    if non_null.iter().all(|value| value.as_f64().is_some()) {
        let mut builder = Float64Builder::new();
        for value in values {
            match value.as_f64().filter(|value| value.is_finite()) {
                Some(value) => builder.append_value(value),
                None => builder.append_null(),
            }
        }
        fields.push(Field::new(name, DataType::Float64, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }
    if non_null.iter().all(|value| value.as_str().is_some()) {
        let mut builder = StringBuilder::new();
        for value in values {
            match value.as_str() {
                Some(value) => builder.append_value(value),
                None => builder.append_null(),
            }
        }
        fields.push(Field::new(name, DataType::Utf8, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }
    bail!("derived column {name} contains mixed unsupported JSON types")
}
