use crate::v03_census::AccountDataRow;
use anyhow::{Context, Result};
use arrow_array::builder::{BooleanBuilder, Float64Builder, StringBuilder};
use arrow_array::{ArrayRef, RecordBatch};
use arrow_schema::{DataType, Field, Schema, SchemaRef};
use parquet::arrow::ArrowWriter;
use std::collections::BTreeSet;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub fn read_address_set(path: &Path) -> Result<BTreeSet<String>> {
    if !path.exists() {
        return Ok(BTreeSet::new());
    }
    let file = File::open(path).with_context(|| format!("open {}", path.display()))?;
    let builder = parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder::try_new(file)?;
    let mut reader = builder.with_batch_size(8_192).build()?;
    let mut result = BTreeSet::new();
    while let Some(batch) = reader.next() {
        let batch = batch?;
        let column = batch
            .column_by_name("address")
            .context("address parquet missing address column")?;
        let strings = column
            .as_any()
            .downcast_ref::<arrow_array::StringArray>()
            .context("address parquet address column is not utf8")?;
        for index in 0..strings.len() {
            if strings.is_null(index) {
                continue;
            }
            result.insert(strings.value(index).to_ascii_lowercase());
        }
    }
    Ok(result)
}

pub fn write_address_set(path: &Path, addresses: &BTreeSet<String>) -> Result<()> {
    let mut builder = StringBuilder::new();
    for address in addresses {
        builder.append_value(address);
    }
    let schema: SchemaRef = Arc::new(Schema::new(vec![Field::new(
        "address",
        DataType::Utf8,
        true,
    )]));
    let batch = RecordBatch::try_new(schema.clone(), vec![Arc::new(builder.finish())])?;
    write_batch_atomic(path, schema, batch)
}

pub fn write_account_rows(path: &Path, rows: &[AccountDataRow]) -> Result<()> {
    let mut address = StringBuilder::new();
    let mut success = BooleanBuilder::new();
    let mut error = StringBuilder::new();
    let mut total_collateral = Float64Builder::new();
    let mut total_debt = Float64Builder::new();
    let mut available_borrows = Float64Builder::new();
    let mut liquidation_threshold = Float64Builder::new();
    let mut ltv = Float64Builder::new();
    let mut health_factor = Float64Builder::new();

    for row in rows {
        address.append_value(&row.address);
        success.append_value(row.success);
        append_string(&mut error, row.error.as_deref());
        append_f64(&mut total_collateral, row.total_collateral_usd);
        append_f64(&mut total_debt, row.total_debt_usd);
        append_f64(&mut available_borrows, row.available_borrows_usd);
        append_f64(
            &mut liquidation_threshold,
            row.current_liquidation_threshold_pct,
        );
        append_f64(&mut ltv, row.ltv_pct);
        append_f64(&mut health_factor, row.health_factor);
    }

    let fields = vec![
        Field::new("address", DataType::Utf8, true),
        Field::new("success", DataType::Boolean, true),
        Field::new("error", DataType::Utf8, true),
        Field::new("total_collateral_usd", DataType::Float64, true),
        Field::new("total_debt_usd", DataType::Float64, true),
        Field::new("available_borrows_usd", DataType::Float64, true),
        Field::new(
            "current_liquidation_threshold_pct",
            DataType::Float64,
            true,
        ),
        Field::new("ltv_pct", DataType::Float64, true),
        Field::new("health_factor", DataType::Float64, true),
    ];
    let arrays: Vec<ArrayRef> = vec![
        Arc::new(address.finish()),
        Arc::new(success.finish()),
        Arc::new(error.finish()),
        Arc::new(total_collateral.finish()),
        Arc::new(total_debt.finish()),
        Arc::new(available_borrows.finish()),
        Arc::new(liquidation_threshold.finish()),
        Arc::new(ltv.finish()),
        Arc::new(health_factor.finish()),
    ];
    let schema: SchemaRef = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    write_batch_atomic(path, schema, batch)
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

fn append_string(builder: &mut StringBuilder, value: Option<&str>) {
    match value {
        Some(value) => builder.append_value(value),
        None => builder.append_null(),
    }
}

fn append_f64(builder: &mut Float64Builder, value: Option<f64>) {
    match value {
        Some(value) if value.is_finite() => builder.append_value(value),
        _ => builder.append_null(),
    }
}

fn temp_path(path: &Path) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".tmp");
    PathBuf::from(value)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn address_round_trip_is_sorted_and_lowercase_stable() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("addresses.parquet");
        let addresses = BTreeSet::from([
            "0x0000000000000000000000000000000000000001".to_owned(),
            "0x0000000000000000000000000000000000000002".to_owned(),
        ]);
        write_address_set(&path, &addresses).unwrap();
        assert_eq!(read_address_set(&path).unwrap(), addresses);
    }
}
