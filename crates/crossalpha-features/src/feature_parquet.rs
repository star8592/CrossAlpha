use anyhow::{Context, Result, bail};
use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, StringBuilder};
use arrow_array::{ArrayRef, NullArray, RecordBatch};
use arrow_schema::{DataType, Field, Schema, SchemaRef};
use parquet::arrow::ArrowWriter;
use serde::Serialize;
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::sync::Arc;

pub fn write_struct_rows<T: Serialize>(rows: &[T], columns: &[&str], path: &Path) -> Result<()> {
    if rows.is_empty() {
        bail!("cannot write empty feature parquet");
    }
    let objects: Vec<Value> = rows
        .iter()
        .map(serde_json::to_value)
        .collect::<std::result::Result<_, _>>()?;
    let mut fields = Vec::new();
    let mut arrays = Vec::new();
    for name in columns {
        let values: Vec<&Value> = objects
            .iter()
            .map(|row| {
                row.as_object()
                    .and_then(|object| object.get(*name))
                    .with_context(|| format!("feature row missing column {name}"))
            })
            .collect::<Result<_>>()?;
        push_values(&mut fields, &mut arrays, name, &values)?;
    }
    let schema: SchemaRef = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    write_batch_atomic(path, schema, batch)
}

fn push_values(
    fields: &mut Vec<Field>,
    arrays: &mut Vec<ArrayRef>,
    name: &str,
    values: &[&Value],
) -> Result<()> {
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
                None if value.is_null() => builder.append_null(),
                None => bail!("mixed feature types in {name}"),
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
                builder.append_value(value.as_i64().context("integer feature expected")?);
            }
            fields.push(Field::new(name, DataType::Int64, true));
            arrays.push(Arc::new(builder.finish()));
        } else {
            let mut builder = Float64Builder::new();
            for value in values {
                if value.is_null() {
                    builder.append_null();
                } else {
                    builder.append_value(value.as_f64().context("numeric feature expected")?);
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
                None => bail!("mixed feature types in {name}"),
            }
        }
        fields.push(Field::new(name, DataType::Utf8, true));
        arrays.push(Arc::new(builder.finish()));
        return Ok(());
    }
    bail!("unsupported feature type in column {name}")
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
