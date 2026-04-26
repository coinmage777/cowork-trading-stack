//! Shared, lock-free-ish stats counters used by both the DB task and the /stats
//! HTTP handler. A snapshot-at-read model keeps the critical section tiny.

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Instant;

use parking_lot_mutex::Mutex;

/// Light shim to keep zero external deps — we use std Mutex instead of
/// parking_lot for the latency ring buffer. The `parking_lot_mutex` alias is
/// just `std::sync::Mutex` aliased below.
mod parking_lot_mutex {
    pub use std::sync::Mutex;
}

pub struct InnerState {
    pub total_inserts: AtomicU64,
    pub parse_errors: AtomicU64,
    pub insert_errors: AtomicU64,
    pub last_prune_ts: AtomicI64,
    // ring buffer of recent per-row insert latencies (microseconds)
    latencies: Mutex<LatencyRing>,
    // rate tracker: (last_sample_instant, rows_since_last)
    rate: Mutex<RateTracker>,
    started_at: Mutex<Option<Instant>>,
}

struct LatencyRing {
    buf: Vec<u32>,
    idx: usize,
    filled: bool,
    cap: usize,
}

impl LatencyRing {
    fn new(cap: usize) -> Self {
        Self {
            buf: vec![0u32; cap],
            idx: 0,
            filled: false,
            cap,
        }
    }
    fn push(&mut self, v: u32) {
        self.buf[self.idx] = v;
        self.idx = (self.idx + 1) % self.cap;
        if self.idx == 0 {
            self.filled = true;
        }
    }
    fn percentiles(&self) -> (u32, u32) {
        let len = if self.filled { self.cap } else { self.idx };
        if len == 0 {
            return (0, 0);
        }
        let mut sorted = self.buf[..len].to_vec();
        sorted.sort_unstable();
        let p50 = sorted[len / 2];
        let p99_idx = ((len as f64) * 0.99) as usize;
        let p99 = sorted[p99_idx.min(len - 1)];
        (p50, p99)
    }
}

struct RateTracker {
    window_start: Instant,
    rows_in_window: u64,
    last_rate: f64,
}

impl RateTracker {
    fn new() -> Self {
        Self {
            window_start: Instant::now(),
            rows_in_window: 0,
            last_rate: 0.0,
        }
    }
    fn record(&mut self, n: u64) -> f64 {
        self.rows_in_window += n;
        let elapsed = self.window_start.elapsed().as_secs_f64();
        if elapsed >= 5.0 {
            self.last_rate = (self.rows_in_window as f64) / elapsed;
            self.window_start = Instant::now();
            self.rows_in_window = 0;
        }
        self.last_rate
    }
    fn current(&self) -> f64 {
        let elapsed = self.window_start.elapsed().as_secs_f64();
        if elapsed < 0.5 {
            // not enough data yet, return last known
            self.last_rate
        } else if self.rows_in_window == 0 {
            0.0
        } else {
            (self.rows_in_window as f64) / elapsed
        }
    }
}

pub struct Snapshot {
    pub total_inserts: u64,
    pub recent_rows_per_sec: f64,
    pub uptime_sec: f64,
    pub parse_errors: u64,
    pub insert_errors: u64,
    pub last_prune_ts: i64,
    pub p50_insert_us: u32,
    pub p99_insert_us: u32,
}

#[derive(Clone)]
pub struct SharedState(pub Arc<InnerState>);

impl SharedState {
    pub fn new() -> Self {
        let inner = InnerState {
            total_inserts: AtomicU64::new(0),
            parse_errors: AtomicU64::new(0),
            insert_errors: AtomicU64::new(0),
            last_prune_ts: AtomicI64::new(0),
            latencies: Mutex::new(LatencyRing::new(1024)),
            rate: Mutex::new(RateTracker::new()),
            started_at: Mutex::new(Some(Instant::now())),
        };
        Self(Arc::new(inner))
    }

    pub fn record_batch(&self, rows: usize, total_us: u128) {
        let n = rows as u64;
        self.0.total_inserts.fetch_add(n, Ordering::Relaxed);
        let per_row = if rows > 0 {
            (total_us / rows as u128).min(u32::MAX as u128) as u32
        } else {
            0
        };
        {
            let mut lat = self.0.latencies.lock().unwrap();
            // record per-row sample; if batch was huge, one representative sample is enough
            lat.push(per_row);
        }
        {
            let mut r = self.0.rate.lock().unwrap();
            r.record(n);
        }
    }

    pub fn incr_parse_err(&self) {
        self.0.parse_errors.fetch_add(1, Ordering::Relaxed);
    }

    pub fn incr_insert_err(&self) {
        self.0.insert_errors.fetch_add(1, Ordering::Relaxed);
    }

    pub fn set_last_prune(&self, ts: i64) {
        self.0.last_prune_ts.store(ts, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> Snapshot {
        let (p50, p99) = self.0.latencies.lock().unwrap().percentiles();
        let rate = self.0.rate.lock().unwrap().current();
        let uptime = self
            .0
            .started_at
            .lock()
            .unwrap()
            .map(|t| t.elapsed().as_secs_f64())
            .unwrap_or(0.0);
        Snapshot {
            total_inserts: self.0.total_inserts.load(Ordering::Relaxed),
            recent_rows_per_sec: rate,
            uptime_sec: uptime,
            parse_errors: self.0.parse_errors.load(Ordering::Relaxed),
            insert_errors: self.0.insert_errors.load(Ordering::Relaxed),
            last_prune_ts: self.0.last_prune_ts.load(Ordering::Relaxed),
            p50_insert_us: p50,
            p99_insert_us: p99,
        }
    }
}

impl Default for SharedState {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_and_reports() {
        let s = SharedState::new();
        s.record_batch(10, 1000);
        s.record_batch(20, 4000);
        let snap = s.snapshot();
        assert_eq!(snap.total_inserts, 30);
        assert!(snap.p50_insert_us > 0);
    }

    #[test]
    fn latency_ring_handles_zero() {
        let r = LatencyRing::new(4);
        let (p50, p99) = r.percentiles();
        assert_eq!(p50, 0);
        assert_eq!(p99, 0);
    }

    #[test]
    fn latency_ring_rotates() {
        let mut r = LatencyRing::new(3);
        for v in 1..=5u32 {
            r.push(v);
        }
        // after 5 pushes to cap=3, values should be [4,5,3] (idx cycled)
        let (p50, _p99) = r.percentiles();
        // sorted: [3,4,5] → p50=4
        assert_eq!(p50, 4);
    }
}
