//! Minimal HTTP server for the `/stats` endpoint.
//!
//! We intentionally avoid hyper here — the payload is tiny, we have no
//! concurrent endpoints, and keeping the dependency surface small matters more
//! than HTTP/1.1 purity. One request → one response → connection close.

use std::sync::Arc;

use anyhow::Result;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::Mutex;
use tracing::{debug, warn};

use crate::state::SharedState;

pub async fn run_stats_server(
    bind: &str,
    state: SharedState,
    db: Arc<Mutex<crate::db::Db>>,
) -> Result<()> {
    let listener = TcpListener::bind(bind).await?;
    tracing::info!("stats server listening on http://{bind}/stats");

    loop {
        let (mut sock, peer) = match listener.accept().await {
            Ok(v) => v,
            Err(e) => {
                warn!(error = ?e, "accept failed");
                continue;
            }
        };
        let st = state.clone();
        let db_h = db.clone();
        tokio::spawn(async move {
            if let Err(e) = handle_conn(&mut sock, st, db_h).await {
                debug!(error = ?e, ?peer, "conn handler error");
            }
            let _ = sock.shutdown().await;
        });
    }
}

async fn handle_conn(
    sock: &mut tokio::net::TcpStream,
    state: SharedState,
    db: Arc<Mutex<crate::db::Db>>,
) -> Result<()> {
    // Read up to headers end. We don't care about the body.
    let mut buf = [0u8; 1024];
    let mut total = 0usize;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_millis(500);
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        if remaining.is_zero() {
            break;
        }
        let n = match tokio::time::timeout(remaining, sock.read(&mut buf[total..])).await {
            Ok(Ok(n)) => n,
            Ok(Err(e)) => return Err(e.into()),
            Err(_) => break,
        };
        if n == 0 {
            break;
        }
        total += n;
        if buf[..total].windows(4).any(|w| w == b"\r\n\r\n") {
            break;
        }
        if total >= buf.len() {
            break;
        }
    }

    let req_line = std::str::from_utf8(&buf[..total])
        .unwrap_or("")
        .lines()
        .next()
        .unwrap_or("");
    let mut parts = req_line.split_whitespace();
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("");

    if method != "GET" {
        return send(sock, 405, "text/plain", b"method not allowed").await;
    }

    match path {
        "/stats" => {
            let body = build_stats_json(&state, &db).await?;
            send(sock, 200, "application/json", body.as_bytes()).await
        }
        "/health" => send(sock, 200, "text/plain", b"ok\n").await,
        _ => send(sock, 404, "text/plain", b"not found\n").await,
    }
}

async fn build_stats_json(
    state: &SharedState,
    db: &Arc<Mutex<crate::db::Db>>,
) -> Result<String> {
    let snap = state.snapshot();
    let (oldest, newest, count, file_size) = {
        let guard = db.lock().await;
        let (o, n, c) = guard.ts_range().unwrap_or((None, None, 0));
        let sz = guard.file_size_bytes();
        (o, n, c, sz)
    };

    // recent_rows_per_sec: reported by state counter, which is ewma-smoothed.
    let json = serde_json::json!({
        "total_inserts": snap.total_inserts,
        "rows_in_db": count,
        "recent_rows_per_sec": snap.recent_rows_per_sec,
        "db_size_mb": (file_size as f64) / (1024.0 * 1024.0),
        "oldest_ts": oldest,
        "newest_ts": newest,
        "uptime_sec": snap.uptime_sec,
        "parse_errors": snap.parse_errors,
        "insert_errors": snap.insert_errors,
        "last_prune_ts": snap.last_prune_ts,
        "p50_insert_us": snap.p50_insert_us,
        "p99_insert_us": snap.p99_insert_us,
    });
    Ok(serde_json::to_string_pretty(&json)?)
}

async fn send(
    sock: &mut tokio::net::TcpStream,
    status: u16,
    content_type: &str,
    body: &[u8],
) -> Result<()> {
    let reason = match status {
        200 => "OK",
        404 => "Not Found",
        405 => "Method Not Allowed",
        _ => "Error",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    sock.write_all(head.as_bytes()).await?;
    sock.write_all(body).await?;
    sock.flush().await?;
    Ok(())
}
