//! gap-recorder — standalone SQLite recorder for DH arbitrage gap ticks.
//!
//! Architecture:
//!   stdin (LDJSON) ──► ipc::run_stdin_loop ──► mpsc<Vec<GapRow>> ──► db task
//!                                                         │
//!                   /stats (TCP 127.0.0.1:38744) ◄────────┘ SharedState
//!
//! Graceful shutdown: on SIGINT/SIGTERM/Ctrl-C or EOF on stdin we drain the
//! channel, commit the final batch, close the db, exit 0.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use tokio::sync::{mpsc, Mutex};
use tracing::{error, info, warn};

mod db;
mod http;
mod ipc;
mod state;

use crate::db::Db;
use crate::ipc::{BatchConfig, GapRow};
use crate::state::SharedState;

fn env_or<T: std::str::FromStr>(key: &str, default: T) -> T {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse::<T>().ok())
        .unwrap_or(default)
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let default_filter =
        std::env::var("RUST_LOG").unwrap_or_else(|_| "info,gap_recorder=info".into());
    tracing_subscriber::fmt()
        .with_env_filter(default_filter)
        .with_target(false)
        .with_writer(std::io::stderr) // keep stdout clean for any future data-out use
        .init();

    // --- config ---
    let db_path = std::env::var("GAP_RECORDER_DB")
        .unwrap_or_else(|_| "data/gap_history.db".into());
    let retention_hours: i64 = env_or("GAP_RECORDER_RETENTION_HOURS", 48);
    let prune_interval_sec: u64 = env_or("GAP_RECORDER_PRUNE_SEC", 3600);
    let vacuum_threshold_mb: u64 = env_or("GAP_RECORDER_VACUUM_MB", 200);
    let flush_rows: usize = env_or("GAP_RECORDER_FLUSH_ROWS", 100);
    let flush_interval_ms: u64 = env_or("GAP_RECORDER_FLUSH_MS", 1000);
    let http_bind = std::env::var("GAP_RECORDER_STATS_BIND")
        .unwrap_or_else(|_| "127.0.0.1:38744".into());

    info!(
        db = %db_path,
        retention_hours,
        prune_interval_sec,
        vacuum_threshold_mb,
        flush_rows,
        flush_interval_ms,
        http_bind = %http_bind,
        "gap-recorder starting"
    );

    // --- shared state ---
    let state = SharedState::new();
    let db = Arc::new(Mutex::new(
        Db::open(&db_path).with_context(|| format!("open db {db_path}"))?,
    ));

    // --- channels ---
    let (tx, rx) = mpsc::channel::<Vec<GapRow>>(256);

    // --- tasks ---
    let state_db = state.clone();
    let db_clone = db.clone();
    let db_task = tokio::spawn(async move {
        db_writer_loop(db_clone, rx, state_db).await;
    });

    let state_prune = state.clone();
    let db_prune = db.clone();
    let prune_task = tokio::spawn(async move {
        pruner_loop(
            db_prune,
            state_prune,
            retention_hours,
            prune_interval_sec,
            vacuum_threshold_mb,
        )
        .await;
    });

    let http_state = state.clone();
    let http_db = db.clone();
    let http_task = tokio::spawn(async move {
        if let Err(e) = http::run_stats_server(&http_bind, http_state, http_db).await {
            error!(error = ?e, "stats server crashed");
        }
    });

    let cfg = BatchConfig {
        flush_rows,
        flush_interval: Duration::from_millis(flush_interval_ms),
    };
    let stdin = tokio::io::stdin();
    let ipc_task = tokio::spawn(async move {
        if let Err(e) = ipc::run_stdin_loop(stdin, tx, cfg).await {
            error!(error = ?e, "stdin loop exited with error");
        } else {
            info!("stdin EOF — initiating shutdown");
        }
    });

    // --- shutdown ---
    shutdown_signal().await;
    info!("shutdown signal received");

    // ipc_task will drain on stdin close; we also abort prune/http.
    // Wait briefly for pending writes.
    let _ = tokio::time::timeout(Duration::from_secs(5), ipc_task).await;
    let _ = tokio::time::timeout(Duration::from_secs(5), db_task).await;
    prune_task.abort();
    http_task.abort();

    info!("gap-recorder stopped cleanly");
    Ok(())
}

async fn db_writer_loop(
    db: Arc<Mutex<Db>>,
    mut rx: mpsc::Receiver<Vec<GapRow>>,
    state: SharedState,
) {
    while let Some(batch) = rx.recv().await {
        if batch.is_empty() {
            continue;
        }
        let mut guard = db.lock().await;
        match guard.insert_batch(&batch) {
            Ok((n, us)) => {
                drop(guard);
                state.record_batch(n, us);
            }
            Err(e) => {
                drop(guard);
                state.incr_insert_err();
                error!(error = ?e, rows = batch.len(), "insert batch failed");
                // Retry once with a short sleep — SQLITE_BUSY handling.
                tokio::time::sleep(Duration::from_millis(50)).await;
                let mut guard = db.lock().await;
                if let Err(e2) = guard.insert_batch(&batch) {
                    error!(error = ?e2, rows = batch.len(), "retry also failed — dropping batch");
                    state.incr_insert_err();
                }
            }
        }
    }
    info!("db writer loop exiting — channel closed");
}

async fn pruner_loop(
    db: Arc<Mutex<Db>>,
    state: SharedState,
    retention_hours: i64,
    interval_sec: u64,
    vacuum_threshold_mb: u64,
) {
    let mut ticker = tokio::time::interval(Duration::from_secs(interval_sec));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    ticker.tick().await; // fire immediately once
    loop {
        let now = chrono::Utc::now().timestamp();
        let cutoff = now - retention_hours * 3600;

        let (removed, size_mb) = {
            let mut guard = db.lock().await;
            let removed = match guard.prune(cutoff) {
                Ok(n) => n,
                Err(e) => {
                    warn!(error = ?e, "prune failed");
                    0
                }
            };
            let sz = guard.file_size_bytes();
            (removed, (sz as f64) / (1024.0 * 1024.0))
        };
        state.set_last_prune(now);

        info!(removed, size_mb, "prune complete");

        if size_mb as u64 > vacuum_threshold_mb {
            info!(size_mb, "running VACUUM");
            let mut guard = db.lock().await;
            if let Err(e) = guard.vacuum() {
                warn!(error = ?e, "vacuum failed");
            }
        }

        ticker.tick().await;
    }
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate()).expect("install SIGTERM");
        let mut int = signal(SignalKind::interrupt()).expect("install SIGINT");
        tokio::select! {
            _ = term.recv() => {}
            _ = int.recv() => {}
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
