// mpdex_gap_recorder
// Rust-backed batched SQLite recorder for price-gap / BBO-spread events.
//
// Drop-in producer for funding_rates.db `price_gaps` table (schema matched).
// Also exposes a lower-level `bbo_gaps` table for tick-level BBO snapshots.
//
// Python API:
//   from mpdex_gap_recorder import GapRecorder
//   rec = GapRecorder("/path/to/funding_rates.db", flush_threshold=1000)
//   rec.record_price_gap(ts_iso, symbol, max_ex, min_ex, max_price, min_price,
//                        gap_usd, gap_pct, all_prices_json, actionable)
//   rec.record_bbo(ts_unix, exchange, symbol, bid, ask, mid, spread_bps)
//   rec.flush()   # force flush both buffers
//   rec.prune_older_than(hours)  # retention
//   rec.close()

use parking_lot::Mutex;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use rusqlite::{params, Connection};
use std::sync::Arc;

const PRICE_GAP_SCHEMA: &str = "CREATE TABLE IF NOT EXISTS price_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    max_exchange TEXT NOT NULL,
    min_exchange TEXT NOT NULL,
    max_price REAL NOT NULL,
    min_price REAL NOT NULL,
    gap_usd REAL NOT NULL,
    gap_pct REAL NOT NULL,
    all_prices TEXT,
    actionable BOOLEAN DEFAULT 0
)";

const PRICE_GAP_INDEX: &str =
    "CREATE INDEX IF NOT EXISTS idx_pg_ts ON price_gaps(timestamp, symbol)";

const BBO_SCHEMA: &str = "CREATE TABLE IF NOT EXISTS bbo_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_unix REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    mid REAL NOT NULL,
    spread_bps REAL NOT NULL
)";

const BBO_INDEX_1: &str = "CREATE INDEX IF NOT EXISTS idx_bbo_ts ON bbo_gaps(ts_unix)";
const BBO_INDEX_2: &str =
    "CREATE INDEX IF NOT EXISTS idx_bbo_sym ON bbo_gaps(symbol, ts_unix)";

#[derive(Debug, Clone)]
struct PriceGapRow {
    timestamp: String,
    symbol: String,
    max_exchange: String,
    min_exchange: String,
    max_price: f64,
    min_price: f64,
    gap_usd: f64,
    gap_pct: f64,
    all_prices: Option<String>,
    actionable: bool,
}

#[derive(Debug, Clone)]
struct BboRow {
    ts_unix: f64,
    exchange: String,
    symbol: String,
    bid: f64,
    ask: f64,
    mid: f64,
    spread_bps: f64,
}

struct Inner {
    conn: Connection,
    price_buf: Vec<PriceGapRow>,
    bbo_buf: Vec<BboRow>,
    flush_threshold: usize,
    total_price_rows: u64,
    total_bbo_rows: u64,
    closed: bool,
}

impl Inner {
    fn new(db_path: &str, flush_threshold: usize) -> rusqlite::Result<Self> {
        let conn = Connection::open(db_path)?;
        // Durability vs throughput tradeoff: WAL + NORMAL sync is standard for
        // high-ingest tick recorders. A crash loses the last fsync window, not the DB.
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "busy_timeout", 5000)?;
        conn.pragma_update(None, "temp_store", "MEMORY")?;
        conn.pragma_update(None, "cache_size", -65536)?; // 64 MiB page cache
        conn.execute(PRICE_GAP_SCHEMA, [])?;
        conn.execute(PRICE_GAP_INDEX, [])?;
        conn.execute(BBO_SCHEMA, [])?;
        conn.execute(BBO_INDEX_1, [])?;
        conn.execute(BBO_INDEX_2, [])?;
        // Initial capacity is a hint only — cap it so a caller passing a huge
        // flush_threshold (e.g. 10**9 to disable auto-flush) doesn't trigger
        // a multi-GB up-front allocation.
        let init_cap = flush_threshold.clamp(64, 16_384);
        Ok(Self {
            conn,
            price_buf: Vec::with_capacity(init_cap),
            bbo_buf: Vec::with_capacity(init_cap),
            flush_threshold,
            total_price_rows: 0,
            total_bbo_rows: 0,
            closed: false,
        })
    }

    fn flush(&mut self) -> rusqlite::Result<usize> {
        if self.closed {
            return Ok(0);
        }
        let mut written: usize = 0;
        let tx = self.conn.transaction()?;
        if !self.price_buf.is_empty() {
            let mut stmt = tx.prepare_cached(
                "INSERT INTO price_gaps (timestamp, symbol, max_exchange, min_exchange,
                                        max_price, min_price, gap_usd, gap_pct,
                                        all_prices, actionable)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            )?;
            for row in &self.price_buf {
                stmt.execute(params![
                    row.timestamp,
                    row.symbol,
                    row.max_exchange,
                    row.min_exchange,
                    row.max_price,
                    row.min_price,
                    row.gap_usd,
                    row.gap_pct,
                    row.all_prices,
                    row.actionable as i64,
                ])?;
                written += 1;
            }
            drop(stmt);
        }
        if !self.bbo_buf.is_empty() {
            let mut stmt = tx.prepare_cached(
                "INSERT INTO bbo_gaps (ts_unix, exchange, symbol, bid, ask, mid, spread_bps)
                 VALUES (?, ?, ?, ?, ?, ?, ?)",
            )?;
            for row in &self.bbo_buf {
                stmt.execute(params![
                    row.ts_unix,
                    row.exchange,
                    row.symbol,
                    row.bid,
                    row.ask,
                    row.mid,
                    row.spread_bps,
                ])?;
                written += 1;
            }
            drop(stmt);
        }
        tx.commit()?;
        self.total_price_rows += self.price_buf.len() as u64;
        self.total_bbo_rows += self.bbo_buf.len() as u64;
        self.price_buf.clear();
        self.bbo_buf.clear();
        Ok(written)
    }

    fn maybe_flush(&mut self) -> rusqlite::Result<()> {
        let total_buffered = self.price_buf.len() + self.bbo_buf.len();
        if total_buffered >= self.flush_threshold {
            self.flush()?;
        }
        Ok(())
    }

    fn prune(&mut self, hours: f64) -> rusqlite::Result<usize> {
        // funding_collector writes ISO-8601 UTC to price_gaps.timestamp.
        let mut affected = 0usize;
        // Use datetime() around the column so ISO-8601 strings with a 'T'
        // separator and timezone suffix (e.g. "2026-04-25T10:00:00+00:00",
        // which is what funding_collector writes) compare correctly against
        // the SQLite-native "YYYY-MM-DD HH:MM:SS" output of datetime('now').
        affected += self.conn.execute(
            "DELETE FROM price_gaps
             WHERE datetime(timestamp) < datetime('now', ?1)",
            params![format!("-{} hours", hours)],
        )?;
        let cutoff_unix = unix_now() - hours * 3600.0;
        affected += self.conn.execute(
            "DELETE FROM bbo_gaps WHERE ts_unix < ?1",
            params![cutoff_unix],
        )?;
        Ok(affected)
    }
}

fn unix_now() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[pyclass]
struct GapRecorder {
    inner: Arc<Mutex<Inner>>,
}

#[pymethods]
impl GapRecorder {
    #[new]
    #[pyo3(signature = (db_path, flush_threshold=1000))]
    fn new(db_path: &str, flush_threshold: usize) -> PyResult<Self> {
        let inner = Inner::new(db_path, flush_threshold)
            .map_err(|e| PyRuntimeError::new_err(format!("open db failed: {e}")))?;
        Ok(Self {
            inner: Arc::new(Mutex::new(inner)),
        })
    }

    #[pyo3(signature = (
        timestamp, symbol, max_exchange, min_exchange, max_price, min_price,
        gap_usd, gap_pct, all_prices=None, actionable=false
    ))]
    fn record_price_gap(
        &self,
        timestamp: &str,
        symbol: &str,
        max_exchange: &str,
        min_exchange: &str,
        max_price: f64,
        min_price: f64,
        gap_usd: f64,
        gap_pct: f64,
        all_prices: Option<&str>,
        actionable: bool,
    ) -> PyResult<()> {
        let mut g = self.inner.lock();
        g.price_buf.push(PriceGapRow {
            timestamp: timestamp.to_owned(),
            symbol: symbol.to_owned(),
            max_exchange: max_exchange.to_owned(),
            min_exchange: min_exchange.to_owned(),
            max_price,
            min_price,
            gap_usd,
            gap_pct,
            all_prices: all_prices.map(|s| s.to_owned()),
            actionable,
        });
        g.maybe_flush()
            .map_err(|e| PyRuntimeError::new_err(format!("maybe_flush: {e}")))
    }

    #[pyo3(signature = (ts_unix, exchange, symbol, bid, ask, mid=None, spread_bps=None))]
    fn record_bbo(
        &self,
        ts_unix: f64,
        exchange: &str,
        symbol: &str,
        bid: f64,
        ask: f64,
        mid: Option<f64>,
        spread_bps: Option<f64>,
    ) -> PyResult<()> {
        if !bid.is_finite() || !ask.is_finite() || bid <= 0.0 || ask <= 0.0 {
            return Err(PyRuntimeError::new_err(
                "bid/ask must be finite and positive",
            ));
        }
        let mid_v = mid.unwrap_or_else(|| (bid + ask) / 2.0);
        let spread_v = spread_bps.unwrap_or_else(|| {
            if mid_v > 0.0 {
                (ask - bid) / mid_v * 10_000.0
            } else {
                0.0
            }
        });
        let mut g = self.inner.lock();
        g.bbo_buf.push(BboRow {
            ts_unix,
            exchange: exchange.to_owned(),
            symbol: symbol.to_owned(),
            bid,
            ask,
            mid: mid_v,
            spread_bps: spread_v,
        });
        g.maybe_flush()
            .map_err(|e| PyRuntimeError::new_err(format!("maybe_flush: {e}")))
    }

    fn flush(&self) -> PyResult<usize> {
        self.inner
            .lock()
            .flush()
            .map_err(|e| PyRuntimeError::new_err(format!("flush: {e}")))
    }

    #[pyo3(signature = (hours))]
    fn prune_older_than(&self, hours: f64) -> PyResult<usize> {
        self.inner
            .lock()
            .prune(hours)
            .map_err(|e| PyRuntimeError::new_err(format!("prune: {e}")))
    }

    fn stats(&self) -> (u64, u64, usize, usize) {
        let g = self.inner.lock();
        (
            g.total_price_rows,
            g.total_bbo_rows,
            g.price_buf.len(),
            g.bbo_buf.len(),
        )
    }

    fn close(&self) -> PyResult<()> {
        let mut g = self.inner.lock();
        if g.closed {
            return Ok(());
        }
        let _ = g.flush();
        g.closed = true;
        Ok(())
    }
}

#[pymodule]
fn mpdex_gap_recorder(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<GapRecorder>()?;
    m.add("__version__", "0.1.0")?;
    Ok(())
}
