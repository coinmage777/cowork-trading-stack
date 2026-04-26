//! Line-delimited JSON IPC over stdin.
//!
//! Python writes one JSON object per line. We deserialize, buffer until either
//! the batch hits `flush_rows` OR the oldest row is `flush_interval` old, then
//! push the batch to the DB task via a channel.

use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::sync::mpsc;
use tokio::time::{interval, Instant, MissedTickBehavior};
use tracing::{debug, warn};

/// One gap observation. All floats are optional to match the Python schema.
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct GapRow {
    pub ts: i64,
    pub ticker: String,
    pub exchange: String,
    #[serde(default)]
    pub spot_gap: Option<f64>,
    #[serde(default)]
    pub futures_gap: Option<f64>,
    #[serde(default)]
    pub bithumb_ask: Option<f64>,
    #[serde(default)]
    pub futures_bid_usdt: Option<f64>,
    #[serde(default)]
    pub usdt_krw: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct BatchConfig {
    pub flush_rows: usize,
    pub flush_interval: Duration,
}

impl Default for BatchConfig {
    fn default() -> Self {
        Self {
            flush_rows: 100,
            flush_interval: Duration::from_secs(1),
        }
    }
}

/// Parse one LDJSON line. Returns `None` for blank lines so the caller can
/// ignore them cheaply.
pub fn parse_line(line: &str) -> Result<Option<GapRow>> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    let row: GapRow =
        serde_json::from_str(trimmed).with_context(|| format!("parse ldjson: {trimmed}"))?;
    if row.ticker.is_empty() {
        return Err(anyhow!("ticker empty"));
    }
    if row.exchange.is_empty() {
        return Err(anyhow!("exchange empty"));
    }
    Ok(Some(row))
}

/// Stdin loop. Reads lines, parses, batches, emits `Vec<GapRow>` to `tx`.
/// Terminates when stdin closes.
pub async fn run_stdin_loop<R>(
    reader: R,
    tx: mpsc::Sender<Vec<GapRow>>,
    cfg: BatchConfig,
) -> Result<()>
where
    R: tokio::io::AsyncRead + Unpin,
{
    let mut lines = BufReader::with_capacity(64 * 1024, reader).lines();
    let mut buf: Vec<GapRow> = Vec::with_capacity(cfg.flush_rows * 2);
    let mut first_row_at: Option<Instant> = None;

    let mut tick = interval(Duration::from_millis(100));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);

    let mut parse_errors: u64 = 0;

    loop {
        tokio::select! {
            line_res = lines.next_line() => {
                match line_res {
                    Ok(Some(line)) => {
                        match parse_line(&line) {
                            Ok(Some(row)) => {
                                if buf.is_empty() {
                                    first_row_at = Some(Instant::now());
                                }
                                buf.push(row);
                                if buf.len() >= cfg.flush_rows {
                                    flush(&mut buf, &mut first_row_at, &tx).await?;
                                }
                            }
                            Ok(None) => {} // blank line
                            Err(e) => {
                                parse_errors += 1;
                                if parse_errors <= 10 || parse_errors % 1000 == 0 {
                                    warn!(error = ?e, total = parse_errors, "ipc parse error");
                                }
                            }
                        }
                    }
                    Ok(None) => {
                        debug!("stdin closed, flushing final batch");
                        if !buf.is_empty() {
                            flush(&mut buf, &mut first_row_at, &tx).await?;
                        }
                        return Ok(());
                    }
                    Err(e) => {
                        warn!(error = ?e, "stdin read error — stopping");
                        if !buf.is_empty() {
                            flush(&mut buf, &mut first_row_at, &tx).await?;
                        }
                        return Err(e.into());
                    }
                }
            }
            _ = tick.tick() => {
                if let Some(t0) = first_row_at {
                    if t0.elapsed() >= cfg.flush_interval && !buf.is_empty() {
                        flush(&mut buf, &mut first_row_at, &tx).await?;
                    }
                }
            }
        }
    }
}

async fn flush(
    buf: &mut Vec<GapRow>,
    first: &mut Option<Instant>,
    tx: &mpsc::Sender<Vec<GapRow>>,
) -> Result<()> {
    if buf.is_empty() {
        return Ok(());
    }
    let rows = std::mem::take(buf);
    *first = None;
    // If the DB task died, propagate — main will shut down.
    tx.send(rows)
        .await
        .map_err(|_| anyhow!("db channel closed"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_row() {
        let s = r#"{"ts":1700000000,"ticker":"BTC","exchange":"binance","spot_gap":10010.0,"futures_gap":9980.0,"bithumb_ask":100.5,"futures_bid_usdt":0.99,"usdt_krw":1350.0}"#;
        let r = parse_line(s).unwrap().unwrap();
        assert_eq!(r.ts, 1700000000);
        assert_eq!(r.ticker, "BTC");
        assert_eq!(r.spot_gap, Some(10010.0));
    }

    #[test]
    fn parses_with_nulls() {
        let s = r#"{"ts":1,"ticker":"X","exchange":"y","spot_gap":null,"futures_gap":null,"bithumb_ask":null,"futures_bid_usdt":null,"usdt_krw":null}"#;
        let r = parse_line(s).unwrap().unwrap();
        assert!(r.spot_gap.is_none());
    }

    #[test]
    fn parses_with_missing_optional_fields() {
        let s = r#"{"ts":1,"ticker":"X","exchange":"y"}"#;
        let r = parse_line(s).unwrap().unwrap();
        assert!(r.spot_gap.is_none());
        assert!(r.usdt_krw.is_none());
    }

    #[test]
    fn blank_line_is_none() {
        assert!(parse_line("").unwrap().is_none());
        assert!(parse_line("   \t  ").unwrap().is_none());
    }

    #[test]
    fn rejects_empty_ticker() {
        let s = r#"{"ts":1,"ticker":"","exchange":"y"}"#;
        assert!(parse_line(s).is_err());
    }

    #[test]
    fn rejects_malformed_json() {
        assert!(parse_line("not json").is_err());
        assert!(parse_line(r#"{"ts":"oops"}"#).is_err());
    }

    #[tokio::test]
    async fn stdin_loop_batches_by_count() {
        use tokio::io::AsyncWriteExt;
        let (tx, mut rx) = mpsc::channel(8);
        let (mut w, r) = tokio::io::duplex(16 * 1024);
        let cfg = BatchConfig {
            flush_rows: 3,
            flush_interval: Duration::from_secs(60),
        };
        let loop_task = tokio::spawn(run_stdin_loop(r, tx, cfg));
        for i in 0..3 {
            let line = format!(
                r#"{{"ts":{},"ticker":"T","exchange":"e"}}
"#,
                i
            );
            w.write_all(line.as_bytes()).await.unwrap();
        }
        let batch = rx.recv().await.unwrap();
        assert_eq!(batch.len(), 3);
        drop(w);
        loop_task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn stdin_loop_batches_by_time() {
        use tokio::io::AsyncWriteExt;
        let (tx, mut rx) = mpsc::channel(8);
        let (mut w, r) = tokio::io::duplex(16 * 1024);
        let cfg = BatchConfig {
            flush_rows: 1000,
            flush_interval: Duration::from_millis(200),
        };
        let loop_task = tokio::spawn(run_stdin_loop(r, tx, cfg));
        w.write_all(b"{\"ts\":1,\"ticker\":\"T\",\"exchange\":\"e\"}\n")
            .await
            .unwrap();
        let batch = tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(batch.len(), 1);
        drop(w);
        loop_task.await.unwrap().unwrap();
    }
}
