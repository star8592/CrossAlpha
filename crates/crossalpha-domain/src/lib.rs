use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct AssetId(pub String);

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetClass {
    Equity,
    Commodity,
    Crypto,
    Cash,
    Other,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Observation<T> {
    pub asset: AssetId,
    pub observed_at: DateTime<Utc>,
    pub value: T,
    pub source: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PriceBar {
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PointInTimeState {
    pub as_of: DateTime<Utc>,
    pub schema_version: String,
    pub features: std::collections::BTreeMap<String, Option<f64>>,
}

#[derive(Debug, thiserror::Error)]
pub enum DomainError {
    #[error("invalid non-finite numeric value in {field}")]
    NonFinite { field: &'static str },
}

impl PriceBar {
    pub fn validate(&self) -> Result<(), DomainError> {
        for (field, value) in [
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ] {
            if !value.is_finite() {
                return Err(DomainError::NonFinite { field });
            }
        }
        Ok(())
    }
}
