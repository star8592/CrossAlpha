use anyhow::{Context, Result};
use arrow_array::builder::{BooleanBuilder, Float64Builder, Int64Builder, StringBuilder};
use arrow_array::{ArrayRef, NullArray, RecordBatch};
use arrow_schema::{DataType, Field, Schema};
use parquet::arrow::ArrowWriter;
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::path::Path;
use std::sync::Arc;

pub fn write_state_snapshot(path: &Path, snapshot: &Value) -> Result<()> {
    let components = snapshot
        .get("components")
        .and_then(Value::as_object)
        .context("State V0.2 components missing")?;
    let aave = components
        .get("aave_market_stress")
        .cloned()
        .unwrap_or(Value::Null);
    let stable = components
        .get("stablecoin_flow_decomposition")
        .cloned()
        .unwrap_or(Value::Null);
    let basis = components
        .get("basis_dispersion")
        .cloned()
        .unwrap_or(Value::Null);
    let contagion = components
        .get("contagion_graph")
        .cloned()
        .unwrap_or(Value::Null);
    let liquidations = snapshot
        .get("aave_liquidation_activity")
        .cloned()
        .unwrap_or(Value::Null);
    let borrower = snapshot
        .get("borrower_health_factor_distribution")
        .cloned()
        .unwrap_or(Value::Null);
    let deployment = snapshot
        .get("deployment_activation")
        .cloned()
        .unwrap_or(Value::Null);

    let mut fields = Vec::<Field>::new();
    let mut arrays = Vec::<ArrayRef>::new();
    push_string(&mut fields, &mut arrays, "protocol", snapshot.get("protocol").and_then(Value::as_str));
    push_string(&mut fields, &mut arrays, "mode", snapshot.get("mode").and_then(Value::as_str));
    push_string(&mut fields, &mut arrays, "actionability", snapshot.get("actionability").and_then(Value::as_str));
    push_null(&mut fields, &mut arrays, "risk_multiplier");
    push_bool(&mut fields, &mut arrays, "mutates_frozen_core", snapshot.get("mutates_frozen_core").and_then(Value::as_bool));
    push_bool(&mut fields, &mut arrays, "mutates_state_v01", snapshot.get("mutates_state_v01").and_then(Value::as_bool));
    push_bool(&mut fields, &mut arrays, "mutates_state_ab_v01", snapshot.get("mutates_state_ab_v01").and_then(Value::as_bool));
    push_string(&mut fields, &mut arrays, "as_of", snapshot.get("as_of").and_then(Value::as_str));
    push_string(&mut fields, &mut arrays, "generated_at", snapshot.get("generated_at").and_then(Value::as_str));
    push_string(&mut fields, &mut arrays, "data_confidence", snapshot.get("data_confidence").and_then(Value::as_str));
    push_f64(&mut fields, &mut arrays, "descriptive_stress_score", snapshot.get("descriptive_stress_score").and_then(Value::as_f64));
    push_i64(&mut fields, &mut arrays, "valid_pressure_component_count", snapshot.get("valid_pressure_component_count").and_then(Value::as_i64));
    push_bool(&mut fields, &mut arrays, "aave_valid", aave.get("valid").and_then(Value::as_bool));
    push_f64(&mut fields, &mut arrays, "aave_pressure", aave.get("pressure").and_then(Value::as_f64));
    push_bool(&mut fields, &mut arrays, "stablecoin_flow_valid", stable.get("valid").and_then(Value::as_bool));
    push_f64(&mut fields, &mut arrays, "stablecoin_flow_pressure", stable.get("pressure").and_then(Value::as_f64));
    push_f64(&mut fields, &mut arrays, "stablecoin_net_change_ratio", stable.get("net_system_change_ratio").and_then(Value::as_f64));
    push_f64(&mut fields, &mut arrays, "stablecoin_migration_ratio", stable.get("migration_ratio").and_then(Value::as_f64));
    push_bool(&mut fields, &mut arrays, "basis_dispersion_valid", basis.get("valid").and_then(Value::as_bool));
    push_f64(&mut fields, &mut arrays, "basis_dispersion_pressure", basis.get("pressure").and_then(Value::as_f64));
    push_f64(&mut fields, &mut arrays, "basis_z_dispersion", basis.get("basis_z_dispersion").and_then(Value::as_f64));
    push_bool(&mut fields, &mut arrays, "contagion_valid", contagion.get("valid").and_then(Value::as_bool));
    push_f64(&mut fields, &mut arrays, "contagion_pressure", contagion.get("pressure").and_then(Value::as_f64));
    push_f64(&mut fields, &mut arrays, "contagion_connectivity", contagion.get("weighted_chain_composition_cosine_overlap").and_then(Value::as_f64));
    push_i64(&mut fields, &mut arrays, "liquidation_events_24h", liquidations.get("events_24h").and_then(Value::as_i64));
    push_i64(&mut fields, &mut arrays, "liquidation_events_7d", liquidations.get("events_7d").and_then(Value::as_i64));
    push_bool(&mut fields, &mut arrays, "borrower_health_factor_distribution_valid", borrower.get("valid").and_then(Value::as_bool));
    push_bool(&mut fields, &mut arrays, "deployment_activation_proxy", deployment.get("coincident_activation_proxy").and_then(Value::as_bool));
    let details = serde_json::to_string(snapshot)?;
    push_string(&mut fields, &mut arrays, "details_json", Some(&details));

    let schema = Arc::new(Schema::new(fields));
    let batch = RecordBatch::try_new(schema.clone(), arrays)?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
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

fn push_string(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, value: Option<&str>) {
    fields.push(Field::new(name, DataType::Utf8, true));
    let mut builder = StringBuilder::new();
    builder.append_option(value);
    arrays.push(Arc::new(builder.finish()));
}

fn push_f64(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, value: Option<f64>) {
    fields.push(Field::new(name, DataType::Float64, true));
    let mut builder = Float64Builder::new();
    builder.append_option(value);
    arrays.push(Arc::new(builder.finish()));
}

fn push_i64(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, value: Option<i64>) {
    fields.push(Field::new(name, DataType::Int64, true));
    let mut builder = Int64Builder::new();
    builder.append_option(value);
    arrays.push(Arc::new(builder.finish()));
}

fn push_bool(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str, value: Option<bool>) {
    fields.push(Field::new(name, DataType::Boolean, true));
    let mut builder = BooleanBuilder::new();
    builder.append_option(value);
    arrays.push(Arc::new(builder.finish()));
}

fn push_null(fields: &mut Vec<Field>, arrays: &mut Vec<ArrayRef>, name: &str) {
    fields.push(Field::new(name, DataType::Null, true));
    arrays.push(Arc::new(NullArray::new(1)));
}
