//! Integration test: spawn the release binary, pipe 1000 LDJSON lines to its
//! stdin, hit /stats over HTTP, verify row count + response shape.

use std::io::Write;
use std::net::TcpStream;
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;

use tempfile::tempdir;

/// Locate the binary built by cargo. Prefer release, fall back to debug.
fn binary_path() -> std::path::PathBuf {
    let exe = if cfg!(windows) {
        "gap-recorder.exe"
    } else {
        "gap-recorder"
    };
    let cwd = std::env::current_exe().unwrap();
    // target/{profile}/deps/integration-<hash> → target/{profile}/{exe}
    let mut p = cwd.clone();
    p.pop(); // deps
    p.pop(); // profile
    let rel = p.join("release").join(exe);
    if rel.exists() {
        return rel;
    }
    let dbg = p.join("debug").join(exe);
    if dbg.exists() {
        return dbg;
    }
    // Last resort: target/<profile>/<exe>
    p.push(exe);
    p
}

fn http_get(host: &str, path: &str) -> std::io::Result<String> {
    let mut s = TcpStream::connect(host)?;
    s.set_read_timeout(Some(Duration::from_secs(2)))?;
    s.set_write_timeout(Some(Duration::from_secs(2)))?;
    write!(
        s,
        "GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    )?;
    let mut buf = Vec::with_capacity(4096);
    use std::io::Read;
    s.read_to_end(&mut buf)?;
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

#[test]
fn end_to_end_1000_rows_and_stats() {
    let bin = binary_path();
    assert!(bin.exists(), "binary not found at {}", bin.display());

    let dir = tempdir().unwrap();
    let db_path = dir.path().join("gap.db");

    // Pick a port unlikely to clash with dev instance
    let port = 38899;
    let bind = format!("127.0.0.1:{port}");

    let mut child = Command::new(&bin)
        .env("GAP_RECORDER_DB", &db_path)
        .env("GAP_RECORDER_STATS_BIND", &bind)
        .env("GAP_RECORDER_FLUSH_ROWS", "100")
        .env("GAP_RECORDER_FLUSH_MS", "200")
        .env("GAP_RECORDER_PRUNE_SEC", "86400") // don't prune during test
        .env("RUST_LOG", "warn")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn gap-recorder");

    // Wait for HTTP server to bind.
    let mut attempts = 0;
    loop {
        if TcpStream::connect(&bind).is_ok() {
            break;
        }
        attempts += 1;
        assert!(attempts < 100, "server never started on {bind}");
        thread::sleep(Duration::from_millis(50));
    }

    // Feed 1000 rows
    {
        let mut stdin = child.stdin.take().expect("stdin");
        for i in 0..1000u32 {
            let line = format!(
                r#"{{"ts":{ts},"ticker":"T{m}","exchange":"binance","spot_gap":{sg},"futures_gap":{fg},"bithumb_ask":100.0,"futures_bid_usdt":0.99,"usdt_krw":1350.0}}
"#,
                ts = 1_700_000_000 + i as i64,
                m = i % 10,
                sg = 10000.0 + (i % 100) as f64,
                fg = 9900.0 + (i % 200) as f64,
            );
            stdin.write_all(line.as_bytes()).expect("write stdin");
        }
        drop(stdin);
    }

    // Give it a moment to flush final batches
    thread::sleep(Duration::from_millis(700));

    // Query /stats
    let resp = http_get(&bind, "/stats").expect("GET /stats");
    assert!(resp.contains("200 OK"), "bad response: {resp}");
    assert!(resp.contains("\"total_inserts\""));
    assert!(resp.contains("\"rows_in_db\""));
    let body = resp.split("\r\n\r\n").nth(1).unwrap_or("");
    let v: serde_json::Value = serde_json::from_str(body).expect("json body");
    let rows = v["rows_in_db"].as_i64().unwrap_or(-1);
    assert_eq!(rows, 1000, "expected 1000 rows, got {rows}. body={body}");
    let inserts = v["total_inserts"].as_u64().unwrap_or(0);
    assert_eq!(inserts, 1000);

    // Clean shutdown
    let _ = child.kill();
    let _ = child.wait();
}

#[test]
fn stats_endpoint_survives_bad_input() {
    let bin = binary_path();
    assert!(bin.exists());

    let dir = tempdir().unwrap();
    let db_path = dir.path().join("gap.db");
    let port = 38898;
    let bind = format!("127.0.0.1:{port}");

    let mut child = Command::new(&bin)
        .env("GAP_RECORDER_DB", &db_path)
        .env("GAP_RECORDER_STATS_BIND", &bind)
        .env("RUST_LOG", "error") // suppress expected parse-err warnings
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    for _ in 0..50 {
        if TcpStream::connect(&bind).is_ok() {
            break;
        }
        thread::sleep(Duration::from_millis(50));
    }

    {
        let mut stdin = child.stdin.take().unwrap();
        let _ = stdin.write_all(b"not json\n");
        let _ = stdin.write_all(b"{bad\n");
        let _ = stdin.write_all(
            b"{\"ts\":1,\"ticker\":\"OK\",\"exchange\":\"e\"}\n",
        );
        drop(stdin);
    }
    thread::sleep(Duration::from_millis(500));

    let resp = http_get(&bind, "/stats").expect("GET /stats");
    assert!(resp.contains("200 OK"));
    let body = resp.split("\r\n\r\n").nth(1).unwrap_or("");
    let v: serde_json::Value = serde_json::from_str(body).unwrap();
    assert_eq!(v["rows_in_db"].as_i64().unwrap_or(-1), 1);

    let _ = child.kill();
    let _ = child.wait();
}

#[test]
fn unknown_path_returns_404() {
    let bin = binary_path();
    assert!(bin.exists());

    let dir = tempdir().unwrap();
    let db_path = dir.path().join("gap.db");
    let port = 38897;
    let bind = format!("127.0.0.1:{port}");

    let mut child = Command::new(&bin)
        .env("GAP_RECORDER_DB", &db_path)
        .env("GAP_RECORDER_STATS_BIND", &bind)
        .env("RUST_LOG", "error")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    for _ in 0..50 {
        if TcpStream::connect(&bind).is_ok() {
            break;
        }
        thread::sleep(Duration::from_millis(50));
    }

    let resp = http_get(&bind, "/does-not-exist").expect("GET");
    assert!(resp.contains("404"));

    let _ = child.kill();
    let _ = child.wait();
}
