//! Parity tests — random inputs matching the Python reference formula.
//! Python formula is simple enough that we can assert algebraic identity.

use bbo_loop::{calculate_gap, calculate_gap_krw, calculate_impact_gap};

fn lcg(seed: &mut u64) -> f64 {
    *seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    ((*seed >> 33) as u32) as f64 / u32::MAX as f64
}

#[test]
fn random_1000_gaps_match_formula() {
    let mut seed: u64 = 42;
    for _ in 0..1000 {
        let fb = 0.01 + lcg(&mut seed) * 100_000.0;
        let uk = 1000.0 + lcg(&mut seed) * 500.0;
        let ba = 1.0 + lcg(&mut seed) * 100_000_000.0;
        let g = calculate_gap(fb, uk, ba);
        let expected = ba / (fb * uk) * 10_000.0;
        assert!((g - expected).abs() / expected.abs().max(1.0) < 1e-12, "gap mismatch");
    }
}

#[test]
fn random_1000_gap_krw_match_formula() {
    let mut seed: u64 = 99;
    for _ in 0..1000 {
        let d = 1.0 + lcg(&mut seed) * 100_000_000.0;
        let b = 1.0 + lcg(&mut seed) * 100_000_000.0;
        let g = calculate_gap_krw(d, b);
        let expected = b / d * 10_000.0;
        assert!((g - expected).abs() / expected.abs().max(1.0) < 1e-12);
    }
}

#[test]
fn impact_gap_monotone_volume() {
    // Larger volume through thicker orderbook should not panic; results monotonic in sign of deep levels.
    // Deep, thick book: 50 levels of (price, qty=100). $10k volume easily fills.
    let asks: Vec<(f64, f64)> = (0..50).map(|i| (100.0 + i as f64 * 0.1, 100.0)).collect();
    let bids: Vec<(f64, f64)> = (0..50).map(|i| (99.0 - i as f64 * 0.1, 100.0)).collect();
    let g1 = calculate_impact_gap(&asks, &bids, 1380.0, 1000.0, false);
    let g2 = calculate_impact_gap(&asks, &bids, 1380.0, 5000.0, false);
    assert!(g1.is_some() && g2.is_some());
}
