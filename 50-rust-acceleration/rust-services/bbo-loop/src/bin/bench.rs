//! Rust benchmark — mirrors `profile_python.py`.
//!
//! Run from workspace root:
//!   cargo run -p bbo-loop --release --bin bbo-bench

use bbo_loop::{
    build_gap_result, calculate_impact_gap, parse_binance_booktickers, Bbo, BuildInput,
    ExchangeBbos, OrderbookLevel,
};
use std::time::Instant;

const ALL_EXCHANGES: &[(&str, bool)] = &[
    ("binance", false),
    ("bybit", false),
    ("okx", false),
    ("bitget", false),
    ("gate", false),
    ("htx", false),
    ("upbit", true),
    ("coinone", true),
];
const N_TICKERS: usize = 500;

fn prng(seed: u64, i: usize) -> f64 {
    // cheap LCG for deterministic noise
    let mut x = seed.wrapping_add(i as u64);
    x = x.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    ((x >> 33) as u32) as f64 / u32::MAX as f64
}

fn gen_binance_json() -> (String, String) {
    let mut spot = String::from("[");
    for i in 0..N_TICKERS {
        if i > 0 {
            spot.push(',');
        }
        let bid = 0.01 + prng(1, i) * 100_000.0;
        let ask = bid * (1.0001 + prng(2, i) * 0.001);
        spot.push_str(&format!(
            r#"{{"symbol":"C{i:04}USDT","bidPrice":"{:.8}","askPrice":"{:.8}"}}"#,
            bid, ask
        ));
    }
    spot.push(']');
    let mut fut = String::from("[");
    let half = N_TICKERS / 2;
    for i in 0..half {
        if i > 0 {
            fut.push(',');
        }
        let bid = 0.01 + prng(3, i) * 100_000.0;
        let ask = bid * (1.0001 + prng(4, i) * 0.001);
        fut.push_str(&format!(
            r#"{{"symbol":"C{i:04}USDT","bidPrice":"{:.8}","askPrice":"{:.8}"}}"#,
            bid, ask
        ));
    }
    fut.push(']');
    (spot, fut)
}

fn gen_exchange_bbos(seed: u64) -> Vec<(String, ExchangeBbos)> {
    let mut v = Vec::with_capacity(N_TICKERS);
    for i in 0..N_TICKERS {
        let base = format!("C{i:04}");
        let bid = 0.01 + prng(seed, i) * 100_000.0;
        let ask = bid * (1.0001 + prng(seed + 1, i) * 0.001);
        v.push((
            base,
            ExchangeBbos {
                spot: Some(Bbo::new(bid, ask)),
                futures: Some(Bbo::new(bid * 1.001, ask * 1.001)),
            },
        ));
    }
    v
}

fn gen_orderbook(n: usize, base_price: f64, seed: u64) -> Vec<OrderbookLevel> {
    let mut p = base_price;
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        p *= 0.9999 + prng(seed, i) * 0.0006;
        let q = 0.01 + prng(seed + 1, i) * 5.0;
        out.push((p, q));
    }
    out
}

fn bench<F: FnMut()>(name: &str, warmup: usize, runs: usize, mut f: F) -> f64 {
    for _ in 0..warmup {
        f();
    }
    let mut best = f64::MAX;
    let mut times = Vec::with_capacity(runs);
    for _ in 0..runs {
        let t0 = Instant::now();
        f();
        let dt = t0.elapsed().as_secs_f64();
        if dt < best {
            best = dt;
        }
        times.push(dt);
    }
    times.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let median = times[runs / 2];
    println!(
        "  {:<42} median={:>9.3} ms   min={:>9.3} ms",
        name,
        median * 1000.0,
        best * 1000.0
    );
    median
}

fn main() {
    println!("=== BBO loop Rust hot-path bench ===\n");

    let (spot_json, futures_json) = gen_binance_json();

    println!("[1] Binance bookTicker JSON → BBO dict:");
    let t_json = bench(
        "serde_json parse + dict assembly (500+250)",
        2,
        5,
        || {
            let _ = parse_binance_booktickers(&spot_json, &futures_json);
        },
    );

    // Per-exchange BBO tables (pre-built, not part of inner loop cost).
    let exchange_tables: Vec<Vec<(String, ExchangeBbos)>> = ALL_EXCHANGES
        .iter()
        .enumerate()
        .map(|(i, _)| gen_exchange_bbos(10 + i as u64))
        .collect();

    println!("\n[2] build_gap_result for all tickers × all exchanges:");
    let t_build = bench(
        "500 tickers × 8 ex build_gap_result       ",
        2,
        5,
        || {
            for tidx in 0..N_TICKERS {
                let ticker = format!("C{tidx:04}");
                // assemble per-ticker slice referencing exchange tables
                let mut per_ex: Vec<(&str, bool, ExchangeBbos)> =
                    Vec::with_capacity(ALL_EXCHANGES.len());
                for (eidx, &(name, is_krw)) in ALL_EXCHANGES.iter().enumerate() {
                    // linear lookup — fine for N=500, deterministic
                    let ex_row = exchange_tables[eidx]
                        .iter()
                        .find(|(t, _)| t == &ticker)
                        .map(|(_, b)| *b)
                        .unwrap_or_default();
                    per_ex.push((name, is_krw, ex_row));
                }
                let inp = BuildInput {
                    bithumb_ask: Some(30_000_000.0 + tidx as f64),
                    usdt_krw: Some(1380.5),
                    exchanges: &per_ex,
                };
                let _ = build_gap_result(&inp);
            }
        },
    );

    println!("\n[3] calculate_impact_gap (on-demand):");
    let b_asks = gen_orderbook(20, 30_000.0, 100);
    let f_bids = gen_orderbook(20, 30_000.0, 101);
    let t_impact = bench(
        "1000 calls (depth=20)                     ",
        2,
        5,
        || {
            for _ in 0..1000 {
                let _ = calculate_impact_gap(&b_asks, &f_bids, 1380.5, 10_000.0, false);
            }
        },
    );

    println!("\n--- Rust timings summary ---");
    println!("  t_json   = {:.3} ms", t_json * 1000.0);
    println!("  t_build  = {:.3} ms", t_build * 1000.0);
    println!("  t_impact = {:.3} ms (1000 calls)", t_impact * 1000.0);
}
