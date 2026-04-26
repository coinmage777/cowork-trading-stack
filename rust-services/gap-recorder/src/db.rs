//! SQLite persistence for gap_history.
//!
//! Schema matches the existing Python `gap_recorder.py` exactly — no breaking
//! changes. Connection is opened once, pragmas set, and reused for the whole
//! process lifetime. Writes are wrapped in `BEGIN IMMEDIATE` transactions so
//! the rollback journal is skipped and WAL lets concurrent readers in.

use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{Context, Result};
use rusqlite::{params, Connection, OpenFlags};

use crate::ipc::GapRow;

pub struct Db {
    conn: Connection,
    path: PathBuf,
    total_inserts: u64,
}

impl Db {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).with_context(|| {
                    format!("create parent dir for {}", path.display())
                })?;
            }
        }

        let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX;
        let conn = Connection::open_with_flags(&path, flags)
            .with_context(|| format!("open sqlite {}", path.display()))?;

        // Pragmas — applied before any schema ops so WAL files are created up-front.
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "synchronous", "NORMAL")?;
        conn.pragma_update(None, "temp_store", "MEMORY")?;
        conn.pragma_update(None, "cache_size", -65536)?; // 64 MiB
        conn.pragma_update(None, "busy_timeout", 5000)?;

        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS gap_history (
                ts INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                exchange TEXT NOT NULL,
                spot_gap REAL,
                futures_gap REAL,
                bithumb_ask REAL,
                futures_bid_usdt REAL,
                usdt_krw REAL
            );
            CREATE INDEX IF NOT EXISTS idx_gap_ticker_ts ON gap_history (ticker, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_gap_ts ON gap_history (ts DESC);
            "#,
        )
        .context("init schema")?;

        Ok(Self {
            conn,
            path,
            total_inserts: 0,
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Batch-insert rows inside a single IMMEDIATE transaction. Returns
    /// (rows_inserted, wall_time_micros) so callers can feed the timing into
    /// benchmarks or the /stats endpoint.
    pub fn insert_batch(&mut self, rows: &[GapRow]) -> Result<(usize, u128)> {
        if rows.is_empty() {
            return Ok((0, 0));
        }
        let start = Instant::now();
        let tx = self.conn.transaction_with_behavior(
            rusqlite::TransactionBehavior::Immediate,
        )?;
        {
            let mut stmt = tx.prepare_cached(
                "INSERT INTO gap_history
                   (ts, ticker, exchange, spot_gap, futures_gap,
                    bithumb_ask, futures_bid_usdt, usdt_krw)
                 VALUES (?,?,?,?,?,?,?,?)",
            )?;
            for r in rows {
                stmt.execute(params![
                    r.ts,
                    r.ticker,
                    r.exchange,
                    r.spot_gap,
                    r.futures_gap,
                    r.bithumb_ask,
                    r.futures_bid_usdt,
                    r.usdt_krw,
                ])?;
            }
        }
        tx.commit()?;
        self.total_inserts += rows.len() as u64;
        Ok((rows.len(), start.elapsed().as_micros()))
    }

    /// Delete rows older than `cutoff_ts`. Returns number of rows removed.
    pub fn prune(&mut self, cutoff_ts: i64) -> Result<usize> {
        let removed = self
            .conn
            .execute("DELETE FROM gap_history WHERE ts < ?1", params![cutoff_ts])?;
        Ok(removed)
    }

    /// Run `VACUUM`. Expensive — only called when the file balloons.
    pub fn vacuum(&mut self) -> Result<()> {
        self.conn.execute_batch("VACUUM")?;
        Ok(())
    }

    pub fn file_size_bytes(&self) -> u64 {
        std::fs::metadata(&self.path)
            .map(|m| m.len())
            .unwrap_or(0)
    }

    pub fn total_inserts(&self) -> u64 {
        self.total_inserts
    }

    /// Snapshot ts range for /stats.
    pub fn ts_range(&self) -> Result<(Option<i64>, Option<i64>, i64)> {
        let mut stmt = self
            .conn
            .prepare_cached("SELECT MIN(ts), MAX(ts), COUNT(*) FROM gap_history")?;
        let (oldest, newest, count): (Option<i64>, Option<i64>, i64) = stmt
            .query_row([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?;
        Ok((oldest, newest, count))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn sample_row(ts: i64, ticker: &str) -> GapRow {
        GapRow {
            ts,
            ticker: ticker.into(),
            exchange: "binance".into(),
            spot_gap: Some(10010.0),
            futures_gap: Some(9980.0),
            bithumb_ask: Some(100.5),
            futures_bid_usdt: Some(0.99),
            usdt_krw: Some(1350.0),
        }
    }

    #[test]
    fn init_creates_schema() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("t.db");
        let db = Db::open(&path).unwrap();
        let (_, _, n) = db.ts_range().unwrap();
        assert_eq!(n, 0);
    }

    #[test]
    fn batch_insert_and_range() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("t.db");
        let mut db = Db::open(&path).unwrap();
        let rows: Vec<_> = (1..=5).map(|i| sample_row(1_700_000_000 + i, "BTC")).collect();
        let (n, _us) = db.insert_batch(&rows).unwrap();
        assert_eq!(n, 5);
        let (oldest, newest, count) = db.ts_range().unwrap();
        assert_eq!(count, 5);
        assert_eq!(oldest, Some(1_700_000_001));
        assert_eq!(newest, Some(1_700_000_005));
    }

    #[test]
    fn prune_deletes_old_rows() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("t.db");
        let mut db = Db::open(&path).unwrap();
        let rows = vec![
            sample_row(1000, "A"),
            sample_row(2000, "B"),
            sample_row(3000, "C"),
        ];
        db.insert_batch(&rows).unwrap();
        let removed = db.prune(2500).unwrap();
        assert_eq!(removed, 2);
        let (_, _, n) = db.ts_range().unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn empty_batch_is_noop() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("t.db");
        let mut db = Db::open(&path).unwrap();
        let (n, us) = db.insert_batch(&[]).unwrap();
        assert_eq!(n, 0);
        assert_eq!(us, 0);
    }

    #[test]
    fn handles_null_fields() {
        let dir = tempdir().unwrap();
        let path = dir.path().join("t.db");
        let mut db = Db::open(&path).unwrap();
        let row = GapRow {
            ts: 1,
            ticker: "X".into(),
            exchange: "ex".into(),
            spot_gap: None,
            futures_gap: None,
            bithumb_ask: None,
            futures_bid_usdt: None,
            usdt_krw: None,
        };
        let (n, _) = db.insert_batch(&[row]).unwrap();
        assert_eq!(n, 1);
    }
}
