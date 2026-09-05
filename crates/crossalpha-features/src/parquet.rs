use crate::{HyperliquidAssetContextRow, StablecoinCanonicalSnapshot};
use anyhow::{Result, bail};
use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, StringBuilder};
use arrow_array::{ArrayRef, NullArray, RecordBatch};
use arrow_schema::{DataType, Field, Schema, SchemaRef};
use chrono::SecondsFormat;
use parquet::arrow::ArrowWriter;
use serde_json::Value;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub fn write_hyperliquid_parquet(
    rows: &[HyperliquidAssetContextRow],
    path: &Path,
) -> Result<()> {
    if rows.is_empty() {
        bail!("cannot write empty Hyperliquid canonical parquet");
    }

    let mut fields = Vec::new();
    let mut arrays = Vec::new();

    push_string_column(
        &mut fields,
        &mut arrays,
        "observed_at",
        rows.iter().map(|row| {
            Some(
                row.observed_at
                    .to_rfc3339_opts(SecondsFormat::AutoSi, true),
            )
        }),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "known_at",
        rows.iter().map(|row| {
            Some(row.known_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))
        }),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "asset",
        rows.iter().map(|row| Some(row.asset.clone())),
    );
    push_value_column(
        &mut fields,
        &mut arrays,
        "sz_decimals",
        rows.iter().map(|row| &row.sz_decimals),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "max_leverage",
        rows.iter().map(|row| &row.max_leverage),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "only_isolated",
        rows.iter().map(|row| &row.only_isolated),
    )?;
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "mark_price",
        rows.iter().map(|row| row.mark_price),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "oracle_price",
        rows.iter().map(|row| row.oracle_price),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "mid_price",
        rows.iter().map(|row| row.mid_price),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "prev_day_price",
        rows.iter().map(|row| row.prev_day_price),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "premium",
        rows.iter().map(|row| row.premium),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "funding_rate",
        rows.iter().map(|row| row.funding_rate),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "open_interest",
        rows.iter().map(|row| row.open_interest),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "day_notional_volume",
        rows.iter().map(|row| row.day_notional_volume),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "day_base_volume",
        rows.iter().map(|row| row.day_base_volume),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "impact_bid",
        rows.iter().map(|row| row.impact_bid),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "impact_ask",
        rows.iter().map(|row| row.impact_ask),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "raw_sha256",
        rows.iter().map(|row| Some(row.raw_sha256.clone())),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "raw_path",
        rows.iter().map(|row| Some(row.raw_path.clone())),
    );

    write_batch_atomic(path, fields, arrays)
}

pub fn write_stablecoin_parquet(
    snapshot: &StablecoinCanonicalSnapshot,
    asset_path: &Path,
    chain_path: &Path,
) -> Result<()> {
    if snapshot.assets.is_empty() {
        bail!("cannot write empty stablecoin asset parquet");
    }

    let asset_rows = &snapshot.assets;
    let mut fields = Vec::new();
    let mut arrays = Vec::new();
    push_i64_column(
        &mut fields,
        &mut arrays,
        "canonical_schema_version",
        asset_rows
            .iter()
            .map(|row| i64::from(row.canonical_schema_version)),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "observed_at",
        asset_rows.iter().map(|row| {
            Some(
                row.observed_at
                    .to_rfc3339_opts(SecondsFormat::AutoSi, true),
            )
        }),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "known_at",
        asset_rows.iter().map(|row| {
            Some(row.known_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))
        }),
    );
    push_value_column(
        &mut fields,
        &mut arrays,
        "stablecoin_id",
        asset_rows.iter().map(|row| &row.stablecoin_id),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "name",
        asset_rows.iter().map(|row| &row.name),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "symbol",
        asset_rows.iter().map(|row| &row.symbol),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "peg_type",
        asset_rows.iter().map(|row| &row.peg_type),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "peg_mechanism",
        asset_rows.iter().map(|row| &row.peg_mechanism),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "price_source",
        asset_rows.iter().map(|row| &row.price_source),
    )?;
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "price_usd",
        asset_rows.iter().map(|row| row.price_usd),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_native",
        asset_rows.iter().map(|row| row.circulating_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_prev_day_native",
        asset_rows
            .iter()
            .map(|row| row.circulating_prev_day_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_prev_week_native",
        asset_rows
            .iter()
            .map(|row| row.circulating_prev_week_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_prev_month_native",
        asset_rows
            .iter()
            .map(|row| row.circulating_prev_month_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "delta_1d_native",
        asset_rows.iter().map(|row| row.delta_1d_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "delta_7d_native",
        asset_rows.iter().map(|row| row.delta_7d_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "delta_30d_native",
        asset_rows.iter().map(|row| row.delta_30d_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "market_value_usd",
        asset_rows.iter().map(|row| row.market_value_usd),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "peg_deviation_bps",
        asset_rows.iter().map(|row| row.peg_deviation_bps),
    );
    push_i64_column(
        &mut fields,
        &mut arrays,
        "chain_count",
        asset_rows.iter().map(|row| row.chain_count as i64),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "raw_sha256",
        asset_rows.iter().map(|row| Some(row.raw_sha256.clone())),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "raw_path",
        asset_rows.iter().map(|row| Some(row.raw_path.clone())),
    );
    write_batch_atomic(asset_path, fields, arrays)?;

    let chain_rows = &snapshot.chains;
    let mut fields = Vec::new();
    let mut arrays = Vec::new();
    push_i64_column(
        &mut fields,
        &mut arrays,
        "canonical_schema_version",
        chain_rows
            .iter()
            .map(|row| i64::from(row.canonical_schema_version)),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "observed_at",
        chain_rows.iter().map(|row| {
            Some(
                row.observed_at
                    .to_rfc3339_opts(SecondsFormat::AutoSi, true),
            )
        }),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "known_at",
        chain_rows.iter().map(|row| {
            Some(row.known_at.to_rfc3339_opts(SecondsFormat::AutoSi, true))
        }),
    );
    push_value_column(
        &mut fields,
        &mut arrays,
        "stablecoin_id",
        chain_rows.iter().map(|row| &row.stablecoin_id),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "name",
        chain_rows.iter().map(|row| &row.name),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "symbol",
        chain_rows.iter().map(|row| &row.symbol),
    )?;
    push_value_column(
        &mut fields,
        &mut arrays,
        "peg_type",
        chain_rows.iter().map(|row| &row.peg_type),
    )?;
    push_string_column(
        &mut fields,
        &mut arrays,
        "chain",
        chain_rows.iter().map(|row| Some(row.chain.clone())),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_native",
        chain_rows.iter().map(|row| row.circulating_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_prev_day_native",
        chain_rows
            .iter()
            .map(|row| row.circulating_prev_day_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_prev_week_native",
        chain_rows
            .iter()
            .map(|row| row.circulating_prev_week_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "circulating_prev_month_native",
        chain_rows
            .iter()
            .map(|row| row.circulating_prev_month_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "delta_1d_native",
        chain_rows.iter().map(|row| row.delta_1d_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "delta_7d_native",
        chain_rows.iter().map(|row| row.delta_7d_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "delta_30d_native",
        chain_rows.iter().map(|row| row.delta_30d_native),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "market_value_usd",
        chain_rows.iter().map(|row| row.market_value_usd),
    );
    push_optional_f64_column(
        &mut fields,
        &mut arrays,
        "price_usd",
        chain_rows.iter().map(|row| row.price_usd),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "raw_sha256",
        chain_rows.iter().map(|row| Some(row.raw_sha256.clone())),
    );
    push_string_column(
        &mut fields,
        &mut arrays,
        "raw_path",
        chain_rows.iter().map(|row| Some(row.raw_path.clone())),
    );
    write_batch_atomic(chain_path, fields, arrays)
}

fn write_batch_atomic(path: &Path, fields: Vec<Field>, arrays: Vec<ArrayRef>) -> Result<()> {
    if fields.len() != arrays.len() {
        bail!("parquet field/array length mismatch");
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
    File::open(&tmp)?.sync_all()?;
    fs::rename(&tmp, path)?;
    Ok(())
}

fn temp_path(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".tmp");
    PathBuf::from(value)
}

fn push_string_column<I>(
    fields: &mut Vec<Field>,
    arrays: &mut Vec<ArrayRef>,
    name: &str,
    values: I,
) where
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

fn push_i64_column<I>(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, values: I)
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

fn push_optional_f64_column<I>(
    fields: &mut Vec<Field>,
    arrays: &mut Vec<ArrayRef>,
    name: &str,
    values: I,
) where
    I: IntoIterator<Item = Option<f64>>,
{
    let values: Vec<Option<f64>> = values.into_iter().collect();
    if values.iter().all(Option::is_none) {
        fields.push(Field::new(name, DataType::Null, true));
        arrays.push(Arc::new(NullArray::new(values.len())));
        return;
    }

    let mut builder = Float64Builder::new();
    for value in values {
        match value {
            Some(value) => builder.append_value(value),
            None => builder.append_null(),
        }
    }
    fields.push(Field::new(name, DataType::Float64, true));
    arrays.push(Arc::new(builder.finish()));
}

fn push_value_column<'a, I>(
    fields: &mut Vec<Field>,
    arrays: &mut Vec<ArrayRef>,
    name: &str,
    values: I,
) -> Result<()>
where
    I: IntoIterator<Item = &'a Value>,
{
    let values: Vec<&Value> = values.into_iter().collect();
    let non_null: Vec<&Value> = values
        .iter()
        .copied()
        .filter(|value| !value.is_null())
        .collect();

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
                None if value.is_null() => builder.append_null(),
                None => bail!("mixed value types in canonical column {name}"),
            }
        }
        fields.push(Field::new(name, DataType::Boolean, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }

    if non_null.iter().all(|value| value.is_number()) {
        let has_null = values.iter().any(|value| value.is_null());
        let all_integer = non_null.iter().all(|value| value.as_i64().is_some());
        if all_integer && !has_null {
            let mut builder = Int64Builder::new();
            for value in values {
                builder.append_value(
                    value
                        .as_i64()
                        .ok_or_else(|| anyhow::anyhow!("non-integer value in {name}"))?,
                );
            }
            fields.push(Field::new(name, DataType::Int64, true));
            arrays.push(Arc::new(builder.finish()));
        } else {
            let mut builder = Float64Builder::new();
            for value in values {
                if value.is_null() {
                    builder.append_null();
                } else {
                    builder.append_value(
                        value
                            .as_f64()
                            .ok_or_else(|| anyhow::anyhow!("non-numeric value in {name}"))?,
                    );
                }
            }
            fields.push(Field::new(name, DataType::Float64, true));
            arrays.push(Arc::new(builder.finish()));
        }
        return Ok(());
    }

    if non_null.iter().all(|value| value.is_string()) {
        let mut builder = StringBuilder::new();
        for value in values {
            match value.as_str() {
                Some(value) => builder.append_value(value),
                None if value.is_null() => builder.append_null(),
                None => bail!("mixed value types in canonical column {name}"),
            }
        }
        fields.push(Field::new(name, DataType::Utf8, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }

    bail!("unsupported mixed value types in canonical column {name}")
}
