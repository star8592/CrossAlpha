use anyhow::{Result, bail};
use chrono::{DateTime, Duration, Utc};
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct VenueQuality {
    pub venue: String,
    pub asset: String,
    pub observed_at: DateTime<Utc>,
    pub known_at: DateTime<Utc>,
    pub spread_bps: Option<f64>,
    pub basis_bps: Option<f64>,
    pub funding_8h: Option<f64>,
    pub open_interest_usd: Option<f64>,
    #[serde(default)]
    pub collection_error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct VenueRank {
    pub venue: String,
    pub asset: String,
    pub spread_bps: f64,
    pub basis_bps: Option<f64>,
    pub funding_8h: Option<f64>,
    pub open_interest_usd: Option<f64>,
    pub rank: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RoutingAssessment {
    pub generated_at: DateTime<Utc>,
    pub asset: String,
    pub valid_venue_count: usize,
    pub data_confidence: String,
    pub automatic_execution_allowed: bool,
    pub ranked_venues: Vec<VenueRank>,
}

pub fn assess_venue_quality(
    rows: &[VenueQuality],
    asset: &str,
    generated_at: DateTime<Utc>,
    maximum_age_seconds: i64,
    minimum_venues: usize,
) -> Result<RoutingAssessment> {
    if maximum_age_seconds < 0 {
        bail!("maximum_age_seconds must be non-negative");
    }
    let mut valid: Vec<&VenueQuality> = rows
        .iter()
        .filter(|row| row.asset == asset)
        .filter(|row| row.collection_error.is_none())
        .filter(|row| row.known_at <= generated_at && row.observed_at <= row.known_at)
        .filter(|row| generated_at - row.observed_at <= Duration::seconds(maximum_age_seconds))
        .filter(|row| row.spread_bps.is_some_and(|value| value.is_finite() && value >= 0.0))
        .collect();
    valid.sort_by(|left, right| {
        left.spread_bps
            .partial_cmp(&right.spread_bps)
            .unwrap_or(Ordering::Equal)
            .then_with(|| {
                right
                    .open_interest_usd
                    .partial_cmp(&left.open_interest_usd)
                    .unwrap_or(Ordering::Equal)
            })
            .then(left.venue.cmp(&right.venue))
    });
    let ranked_venues = valid
        .iter()
        .enumerate()
        .map(|(index, row)| VenueRank {
            venue: row.venue.clone(),
            asset: row.asset.clone(),
            spread_bps: row.spread_bps.unwrap_or_default(),
            basis_bps: row.basis_bps,
            funding_8h: row.funding_8h,
            open_interest_usd: row.open_interest_usd,
            rank: index + 1,
        })
        .collect::<Vec<_>>();
    let count = ranked_venues.len();
    Ok(RoutingAssessment {
        generated_at,
        asset: asset.to_owned(),
        valid_venue_count: count,
        data_confidence: if count >= minimum_venues {
            "SUFFICIENT".to_owned()
        } else {
            "INSUFFICIENT".to_owned()
        },
        // R5 market layer is descriptive/routing research only. Execution enablement
        // remains a separate, explicit production control-plane decision.
        automatic_execution_allowed: false,
        ranked_venues,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn descriptive_ranking_never_enables_execution() {
        let now = Utc.timestamp_opt(1_700_000_000, 0).unwrap();
        let rows = vec![VenueQuality {
            venue: "x".into(),
            asset: "BTC".into(),
            observed_at: now,
            known_at: now,
            spread_bps: Some(1.0),
            basis_bps: None,
            funding_8h: None,
            open_interest_usd: Some(1.0),
            collection_error: None,
        }];
        let report = assess_venue_quality(&rows, "BTC", now, 90, 1).unwrap();
        assert_eq!(report.data_confidence, "SUFFICIENT");
        assert!(!report.automatic_execution_allowed);
    }
}
