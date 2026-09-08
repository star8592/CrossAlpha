from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"already_patched={label}")
        return
    if old not in text:
        raise SystemExit(f"patch marker not found: {label}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched={label}")


market = Path("crates/crossalpha-features/src/market_state.rs")
replace_once(
    market,
    """        for index in 0..intermediate.len() {\n""",
    """        let funding_z_24h = rolling_zscores(&intermediate, |row| row.source.funding_rate);\n        let basis_z_24h = rolling_zscores(&intermediate, |row| row.mark_oracle_basis_bps);\n        let oi_change_z_24h =\n            rolling_zscores(&intermediate, |row| row.open_interest_change_pct);\n        let spread_z_24h = rolling_zscores(&intermediate, |row| row.impact_spread_bps);\n\n        for index in 0..intermediate.len() {\n""",
    "precompute-pandas-rolling-zscores",
)
for label, old, new in [
    (
        "funding-zscore-vector",
        """                funding_z_24h: rolling_zscore(\n                    window.iter().map(|row| row.source.funding_rate),\n                    current.source.funding_rate,\n                ),\n""",
        """                funding_z_24h: funding_z_24h[index],\n""",
    ),
    (
        "basis-zscore-vector",
        """                basis_z_24h: rolling_zscore(\n                    window.iter().map(|row| row.mark_oracle_basis_bps),\n                    current.mark_oracle_basis_bps,\n                ),\n""",
        """                basis_z_24h: basis_z_24h[index],\n""",
    ),
    (
        "oi-zscore-vector",
        """                oi_change_z_24h: rolling_zscore(\n                    window.iter().map(|row| row.open_interest_change_pct),\n                    current.open_interest_change_pct,\n                ),\n""",
        """                oi_change_z_24h: oi_change_z_24h[index],\n""",
    ),
    (
        "spread-zscore-vector",
        """                spread_z_24h: rolling_zscore(\n                    window.iter().map(|row| row.impact_spread_bps),\n                    current.impact_spread_bps,\n                ),\n""",
        """                spread_z_24h: spread_z_24h[index],\n""",
    ),
]:
    replace_once(market, old, new, label)

text = market.read_text(encoding="utf-8")
start = text.index("fn rolling_zscore<I>")
end = text.index("fn positive(", start)
rolling_impl = r'''const PANDAS_INV_COND_TOL: f64 = f64::EPSILON * 1_000.0;

#[derive(Debug, Default, Clone)]
struct PandasRollingMean {
    nobs: usize,
    sum_x: f64,
    compensation_add: f64,
    compensation_remove: f64,
    negative_count: usize,
    consecutive_same: usize,
    previous_value: Option<f64>,
}

impl PandasRollingMean {
    fn reset(&mut self) {
        *self = Self::default();
    }

    fn add(&mut self, value: Option<f64>) {
        let Some(value) = value else { return };
        self.nobs += 1;
        let y = value - self.compensation_add;
        let t = self.sum_x + y;
        self.compensation_add = t - self.sum_x - y;
        self.sum_x = t;
        if value.is_sign_negative() {
            self.negative_count += 1;
        }
        if self.previous_value == Some(value) {
            self.consecutive_same += 1;
        } else {
            self.consecutive_same = 1;
        }
        self.previous_value = Some(value);
    }

    fn remove(&mut self, value: Option<f64>) {
        let Some(value) = value else { return };
        self.nobs -= 1;
        let y = -value - self.compensation_remove;
        let t = self.sum_x + y;
        self.compensation_remove = t - self.sum_x - y;
        self.sum_x = t;
        if value.is_sign_negative() {
            self.negative_count -= 1;
        }
    }

    fn value(&self) -> Option<f64> {
        if self.nobs < ROLLING_MIN_PERIODS || self.nobs == 0 {
            return None;
        }
        let mut result = self.sum_x / self.nobs as f64;
        if self.consecutive_same >= self.nobs {
            result = self.previous_value?;
        } else if self.negative_count == 0 && result < 0.0 {
            result = 0.0;
        } else if self.negative_count == self.nobs && result > 0.0 {
            result = 0.0;
        }
        Some(result)
    }
}

#[derive(Debug, Default, Clone)]
struct PandasRollingVariance {
    nobs: f64,
    mean_x: f64,
    ssqdm_x: f64,
    compensation_add: f64,
    compensation_remove: f64,
    numerically_unstable: bool,
}

impl PandasRollingVariance {
    fn reset(&mut self) {
        *self = Self::default();
    }

    fn add(&mut self, value: Option<f64>) {
        let Some(value) = value else { return };
        let previous_m2 = self.ssqdm_x;
        self.nobs += 1.0;
        let previous_mean = self.mean_x - self.compensation_add;
        let y = value - self.compensation_add;
        let t = y - self.mean_x;
        self.compensation_add = t + self.mean_x - y;
        let delta = t;
        self.mean_x += delta / self.nobs;
        self.ssqdm_x += (value - previous_mean) * (value - self.mean_x);
        if previous_m2 * PANDAS_INV_COND_TOL > self.ssqdm_x {
            self.numerically_unstable = true;
        }
    }

    fn remove(&mut self, value: Option<f64>) {
        let Some(value) = value else { return };
        let previous_m2 = self.ssqdm_x;
        self.nobs -= 1.0;
        if self.nobs != 0.0 {
            let previous_mean = self.mean_x - self.compensation_remove;
            let y = value - self.compensation_remove;
            let t = y - self.mean_x;
            self.compensation_remove = t + self.mean_x - y;
            let delta = t;
            self.mean_x -= delta / self.nobs;
            self.ssqdm_x -= (value - previous_mean) * (value - self.mean_x);
            if previous_m2 * PANDAS_INV_COND_TOL > self.ssqdm_x {
                self.numerically_unstable = true;
            }
        } else {
            self.mean_x = 0.0;
            self.ssqdm_x = 0.0;
            self.numerically_unstable = false;
        }
    }

    fn value(&self) -> Option<f64> {
        (self.nobs >= ROLLING_MIN_PERIODS as f64 && self.nobs > 0.0)
            .then_some(self.ssqdm_x / self.nobs)
    }
}

fn rolling_zscores<F>(rows: &[Intermediate], value_at: F) -> Vec<Option<f64>>
where
    F: Fn(&Intermediate) -> Option<f64>,
{
    let mut result = Vec::with_capacity(rows.len());
    let mut mean = PandasRollingMean::default();
    let mut variance = PandasRollingVariance::default();
    let mut start = 0_usize;

    for index in 0..rows.len() {
        let lower = rows[index].source.observed_at - Duration::hours(24);
        let mut new_start = start;
        while new_start <= index && rows[new_start].source.observed_at <= lower {
            new_start += 1;
        }
        let requires_recompute = index == 0 || new_start >= index;
        if requires_recompute {
            mean.reset();
            variance.reset();
            for row in &rows[new_start..=index] {
                let value = value_at(row);
                mean.add(value);
                variance.add(value);
            }
            variance.numerically_unstable = false;
        } else {
            for row in &rows[start..new_start] {
                let value = value_at(row);
                mean.remove(value);
                variance.remove(value);
            }
            let value = value_at(&rows[index]);
            mean.add(value);
            variance.add(value);
            if variance.numerically_unstable {
                variance.reset();
                for row in &rows[new_start..=index] {
                    variance.add(value_at(row));
                }
                variance.numerically_unstable = false;
            }
        }
        start = new_start;

        let zscore = match (value_at(&rows[index]), mean.value(), variance.value()) {
            (Some(current), Some(mean), Some(variance)) => {
                let std = variance.sqrt();
                if std == 0.0 || !std.is_finite() {
                    None
                } else {
                    Some((current - mean) / std)
                }
            }
            _ => None,
        };
        result.push(zscore);
    }
    result
}

'''
market.write_text(text[:start] + rolling_impl + text[end:], encoding="utf-8")
print("patched=pandas-rolling-state-machine")

# Add a regression proving a truly constant history remains undefined.
replace_once(
    market,
    """    #[test]\n    fn window_excludes_exactly_24_hours_old_row() {\n""",
    """    #[test]\n    fn exactly_constant_funding_history_has_undefined_zscore() {\n        let rows: Vec<_> = (0..30).map(|index| row(index, 1.25e-5)).collect();\n        let result = compute_hyperliquid_market_state(&rows);\n        assert!(result.last().unwrap().funding_z_24h.is_none());\n    }\n\n    #[test]\n    fn window_excludes_exactly_24_hours_old_row() {\n""",
    "constant-history-regression",
)

logs = Path("src/crossalpha/state/v03_logs.py")
replace_once(
    logs,
    'BLOCKSCOUT_MAX_LOG_RESULTS = 1000\n',
    'BLOCKSCOUT_MAX_LOG_RESULTS = 1000\nROUTESCAN_ETHEREUM_API_URL = "https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api"\nROUTESCAN_LOG_SOURCE = "ROUTESCAN_INDEXED_LOGS"\n',
    "routescan-constants",
)
replace_once(
    logs,
    '        self._blockscout = BlockscoutBorrowLogProvider(policy=self.policy)\n',
    '        self._blockscout = BlockscoutBorrowLogProvider(policy=self.policy)\n        self._routescan = BlockscoutBorrowLogProvider(\n            api_url=ROUTESCAN_ETHEREUM_API_URL, policy=self.policy\n        )\n',
    "routescan-provider",
)
replace_once(
    logs,
    '''        try:\n            rows = await self._blockscout.borrow_logs(int(from_block), int(to_block))\n            self.selected_source = BLOCKSCOUT_LOG_SOURCE\n            return rows\n        except Exception as exc:\n            self.candidate_failures[BLOCKSCOUT_LOG_SOURCE] = type(exc).__name__\n            raise RuntimeError(\n                "No V0.3 Borrow-log source completed the requested range"\n            ) from exc\n''',
    '''        try:\n            rows = await self._blockscout.borrow_logs(int(from_block), int(to_block))\n            self.selected_source = BLOCKSCOUT_LOG_SOURCE\n            return rows\n        except Exception as exc:\n            self.candidate_failures[BLOCKSCOUT_LOG_SOURCE] = type(exc).__name__\n\n        try:\n            rows = await self._routescan.borrow_logs(int(from_block), int(to_block))\n            self.selected_source = ROUTESCAN_LOG_SOURCE\n            return rows\n        except Exception as exc:\n            self.candidate_failures[ROUTESCAN_LOG_SOURCE] = type(exc).__name__\n            raise RuntimeError(\n                "No V0.3 Borrow-log source completed the requested range"\n            ) from exc\n''',
    "routescan-failover",
)

preflight = Path("crates/crossalpha-state/src/v03/preflight.rs")
replace_once(
    preflight,
    'pub const BLOCKSCOUT_STATE_RPC_SOURCE: &str = "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK";\n',
    'pub const BLOCKSCOUT_STATE_RPC_SOURCE: &str = "BLOCKSCOUT_ETH_RPC_ZERO_COST_FALLBACK";\npub const ROUTESCAN_ETHEREUM_API_URL: &str =\n    "https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api";\npub const ROUTESCAN_LOG_SOURCE: &str = "ROUTESCAN_INDEXED_LOGS";\n',
    "rust-routescan-constants",
)
replace_once(
    preflight,
    '''    let historical_logs = borrow_logs_complete(&client, historical_from, historical_to)\n        .await\n        .map_err(|error| {\n            anyhow::anyhow!(\n                "State V0.3 indexed Borrow-log source failed historical probe: {}",\n                error_category(&error)\n            )\n        })?;\n''',
    '''    let (_, historical_logs) = borrow_logs_with_failover(&client, historical_from, historical_to)\n        .await\n        .map_err(|error| {\n            anyhow::anyhow!(\n                "State V0.3 indexed Borrow-log sources failed historical probe: {}",\n                error_category(&error)\n            )\n        })?;\n''',
    "rust-historical-log-failover",
)
replace_once(
    preflight,
    '''                let recent_logs = borrow_logs_complete(&client, recent_from, probe.finalized_block)\n                    .await\n                    .context("State V0.3 indexed Borrow-log source failed recent probe")?;\n                return Ok(StateV03PreflightReport {\n''',
    '''                let (borrow_log_source, recent_logs) =\n                    borrow_logs_with_failover(&client, recent_from, probe.finalized_block)\n                        .await\n                        .context("State V0.3 indexed Borrow-log sources failed recent probe")?;\n                return Ok(StateV03PreflightReport {\n''',
    "rust-recent-log-failover",
)
replace_once(
    preflight,
    '                    borrow_log_source: BLOCKSCOUT_LOG_SOURCE.to_owned(),\n',
    '                    borrow_log_source: borrow_log_source.to_owned(),\n',
    "rust-report-selected-log-source",
)

# Generalize the Etherscan-compatible query so Routescan shares completeness semantics.
text = preflight.read_text(encoding="utf-8")
old_query = '''async fn query_blockscout_logs(\n    client: &Client,\n    from_block: u64,\n    to_block: u64,\n) -> Result<(usize, Vec<Value>)> {\n    let response = client\n        .get(BLOCKSCOUT_ETHEREUM_API_URL)\n'''
new_query = '''async fn query_indexed_logs(\n    client: &Client,\n    api_url: &str,\n    from_block: u64,\n    to_block: u64,\n) -> Result<(usize, Vec<Value>)> {\n    let response = client\n        .get(api_url)\n'''
if old_query in text:
    text = text.replace(old_query, new_query, 1)
    text = text.replace('.context("BlockscoutTransportError")?', '.context("IndexedLogTransportError")?', 1)
    text = text.replace('.context("BlockscoutHttpStatusError")?', '.context("IndexedLogHttpStatusError")?', 1)
    text = text.replace('response.json().await.context("BlockscoutJsonDecodeError")?', 'response.json().await.context("IndexedLogJsonDecodeError")?', 1)
    text = text.replace('.context("Blockscout indexed-log query failed")?;', '.context("indexed-log query failed")?;', 1)
    preflight.write_text(text, encoding="utf-8")
    print("patched=rust-generic-indexed-query")
else:
    print("already_patched=rust-generic-indexed-query")

text = preflight.read_text(encoding="utf-8")
start = text.index("fn borrow_logs_complete<'a>(")
end = text.index("fn error_category(", start)
new_complete = r'''fn indexed_logs_complete<'a>(
    client: &'a Client,
    api_url: &'a str,
    from_block: u64,
    to_block: u64,
) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<Vec<Value>>> + Send + 'a>> {
    Box::pin(async move {
        if to_block < from_block {
            bail!("invalid block range");
        }
        let (raw_len, rows) = query_indexed_logs(client, api_url, from_block, to_block).await?;
        if raw_len < BLOCKSCOUT_MAX_LOG_RESULTS as usize {
            return Ok(rows);
        }
        if from_block == to_block {
            bail!("indexed-log single-block Borrow count reached provider result limit");
        }
        let midpoint = (from_block + to_block) / 2;
        let mut left = indexed_logs_complete(client, api_url, from_block, midpoint).await?;
        let right = indexed_logs_complete(client, api_url, midpoint + 1, to_block).await?;
        left.extend(right);
        Ok(left)
    })
}

async fn borrow_logs_with_failover(
    client: &Client,
    from_block: u64,
    to_block: u64,
) -> Result<(&'static str, Vec<Value>)> {
    match indexed_logs_complete(client, BLOCKSCOUT_ETHEREUM_API_URL, from_block, to_block).await {
        Ok(rows) => return Ok((BLOCKSCOUT_LOG_SOURCE, rows)),
        Err(blockscout_error) => {
            match indexed_logs_complete(client, ROUTESCAN_ETHEREUM_API_URL, from_block, to_block).await {
                Ok(rows) => return Ok((ROUTESCAN_LOG_SOURCE, rows)),
                Err(routescan_error) => {
                    bail!(
                        "IndexedLogFailoverError: blockscout={} routescan={}",
                        error_category(&blockscout_error),
                        error_category(&routescan_error)
                    );
                }
            }
        }
    }
}

'''
preflight.write_text(text[:start] + new_complete + text[end:], encoding="utf-8")
print("patched=rust-indexed-log-failover")

# Error categories remain redacted and provider-neutral.
text = preflight.read_text(encoding="utf-8")
text = text.replace(
    '        "BlockscoutTransportError",\n        "BlockscoutHttpStatusError",\n        "BlockscoutJsonDecodeError",\n',
    '        "IndexedLogTransportError",\n        "IndexedLogHttpStatusError",\n        "IndexedLogJsonDecodeError",\n        "IndexedLogFailoverError",\n',
)
preflight.write_text(text, encoding="utf-8")
print("patched=rust-indexed-error-categories")
