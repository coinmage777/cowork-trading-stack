//! BBO loop — parse + gap compute hot path (candidate for Python offload).
//!
//! Pure-function scaffold. Parity with `backend/services/gap_calculator.py` +
//! `backend/exchanges/manager.py::_fetch_all_binance_bbos` parse.

use ahash::AHashMap;
use serde::Deserialize;

// ===================================================================
// Types
// ===================================================================

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Bbo {
    pub bid: f64,
    pub ask: f64,
    pub ts: Option<i64>,
}

impl Bbo {
    #[inline]
    pub fn new(bid: f64, ask: f64) -> Self {
        Self { bid, ask, ts: None }
    }
}

#[derive(Debug, Default, Clone, Copy)]
pub struct ExchangeBbos {
    pub spot: Option<Bbo>,
    pub futures: Option<Bbo>,
}

#[derive(Debug, Clone, Copy)]
pub struct ExchangeGap {
    pub spot_gap: Option<f64>,
    pub futures_gap: Option<f64>,
}

// ===================================================================
// Binance bookTicker parse
// ===================================================================

#[derive(Deserialize)]
struct BookTickerItem<'a> {
    symbol: &'a str,
    #[serde(rename = "bidPrice")]
    bid_price: &'a str,
    #[serde(rename = "askPrice")]
    ask_price: &'a str,
}

/// Parse Binance `/api/v3/ticker/bookTicker` response (spot) + fapi equivalent.
/// Mirrors `_fetch_all_binance_bbos` parse loop in manager.py.
pub fn parse_binance_booktickers(
    spot_json: &str,
    futures_json: &str,
) -> AHashMap<String, ExchangeBbos> {
    let mut out: AHashMap<String, ExchangeBbos> = AHashMap::with_capacity(2048);

    let spot: Vec<BookTickerItem> = serde_json::from_str(spot_json).unwrap_or_default();
    for item in spot {
        if !item.symbol.ends_with("USDT") {
            continue;
        }
        let base = &item.symbol[..item.symbol.len() - 4];
        let bid: f64 = item.bid_price.parse().unwrap_or(0.0);
        let ask: f64 = item.ask_price.parse().unwrap_or(0.0);
        if bid > 0.0 && ask > 0.0 {
            out.entry(base.to_string())
                .or_default()
                .spot = Some(Bbo::new(bid, ask));
        }
    }

    let futures: Vec<BookTickerItem> = serde_json::from_str(futures_json).unwrap_or_default();
    for item in futures {
        if !item.symbol.ends_with("USDT") {
            continue;
        }
        let base = &item.symbol[..item.symbol.len() - 4];
        let bid: f64 = item.bid_price.parse().unwrap_or(0.0);
        let ask: f64 = item.ask_price.parse().unwrap_or(0.0);
        if bid > 0.0 && ask > 0.0 {
            out.entry(base.to_string())
                .or_default()
                .futures = Some(Bbo::new(bid, ask));
        }
    }

    out
}

// ===================================================================
// Gap calc (pure arithmetic)
// ===================================================================

#[inline]
pub fn calculate_gap(foreign_bid_usdt: f64, usdt_krw: f64, bithumb_ask_krw: f64) -> f64 {
    bithumb_ask_krw / (foreign_bid_usdt * usdt_krw) * 10_000.0
}

#[inline]
pub fn calculate_gap_krw(domestic_bid_krw: f64, bithumb_ask_krw: f64) -> f64 {
    bithumb_ask_krw / domestic_bid_krw * 10_000.0
}

/// Orderbook row: [price, qty].
pub type OrderbookLevel = (f64, f64);

/// Mirrors `calculate_impact_gap`.
pub fn calculate_impact_gap(
    bithumb_asks: &[OrderbookLevel],
    foreign_bids: &[OrderbookLevel],
    usdt_krw: f64,
    volume_usd: f64,
    is_krw_exchange: bool,
) -> Option<f64> {
    if bithumb_asks.is_empty() || foreign_bids.is_empty() {
        return None;
    }

    let mut remaining_value = if is_krw_exchange {
        volume_usd * usdt_krw
    } else {
        volume_usd
    };
    let mut total_cost_foreign = 0.0_f64;
    let mut total_qty = 0.0_f64;

    for &(price, qty) in foreign_bids {
        if price <= 0.0 {
            continue;
        }
        let level_value = price * qty;
        if level_value >= remaining_value {
            let filled_qty = remaining_value / price;
            total_cost_foreign += remaining_value;
            total_qty += filled_qty;
            remaining_value = 0.0;
            break;
        } else {
            total_cost_foreign += level_value;
            total_qty += qty;
            remaining_value -= level_value;
        }
    }

    if remaining_value > 0.0 || total_qty <= 0.0 {
        return None;
    }

    let foreign_bid_vwap = total_cost_foreign / total_qty;

    let mut remaining_qty = total_qty;
    let mut total_cost_bithumb = 0.0_f64;
    for &(price, qty) in bithumb_asks {
        if price <= 0.0 {
            continue;
        }
        if qty >= remaining_qty {
            total_cost_bithumb += price * remaining_qty;
            remaining_qty = 0.0;
            break;
        } else {
            total_cost_bithumb += price * qty;
            remaining_qty -= qty;
        }
    }

    if remaining_qty > 0.0 {
        return None;
    }

    let bithumb_ask_vwap = total_cost_bithumb / total_qty;

    Some(if is_krw_exchange {
        bithumb_ask_vwap / foreign_bid_vwap * 10_000.0
    } else {
        bithumb_ask_vwap / (foreign_bid_vwap * usdt_krw) * 10_000.0
    })
}

// ===================================================================
// Build gap result (the per-iteration hot loop)
// ===================================================================

pub struct BuildInput<'a> {
    pub bithumb_ask: Option<f64>,
    pub usdt_krw: Option<f64>,
    /// exchange_name → (is_krw, ExchangeBbos)
    pub exchanges: &'a [(&'a str, bool, ExchangeBbos)],
}

pub fn build_gap_result(inp: &BuildInput) -> Vec<(String, ExchangeGap)> {
    let bithumb_ask = inp.bithumb_ask.filter(|&v| v > 0.0);
    let usdt_krw = inp.usdt_krw.filter(|&v| v > 0.0);
    let can_calc_foreign = bithumb_ask.is_some() && usdt_krw.is_some();

    let mut out = Vec::with_capacity(inp.exchanges.len());
    for &(name, is_krw, ref bbos) in inp.exchanges {
        let mut spot_gap = None;
        let mut futures_gap = None;
        let can_calc_this = if is_krw {
            bithumb_ask.is_some()
        } else {
            can_calc_foreign
        };
        if can_calc_this {
            if let Some(spot) = bbos.spot {
                if spot.bid > 0.0 {
                    spot_gap = Some(if is_krw {
                        calculate_gap_krw(spot.bid, bithumb_ask.unwrap())
                    } else {
                        calculate_gap(spot.bid, usdt_krw.unwrap(), bithumb_ask.unwrap())
                    });
                }
            }
            if let Some(fut) = bbos.futures {
                if fut.bid > 0.0 && !is_krw {
                    futures_gap = Some(calculate_gap(
                        fut.bid,
                        usdt_krw.unwrap(),
                        bithumb_ask.unwrap(),
                    ));
                }
            }
        }
        out.push((
            name.to_string(),
            ExchangeGap {
                spot_gap,
                futures_gap,
            },
        ));
    }
    out
}

// ===================================================================
// Parity tests
// ===================================================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gap_matches_reference() {
        // Python: 30_000_000 / (30_000 * 1380) * 10_000 ≈ 7246.376811...
        let g = calculate_gap(30_000.0, 1380.0, 30_000_000.0);
        assert!((g - 7246.376811_f64).abs() < 1e-3);
    }

    #[test]
    fn gap_krw_matches_reference() {
        let g = calculate_gap_krw(30_000_000.0, 30_100_000.0);
        assert!((g - 10_033.333333_f64).abs() < 1e-3);
    }

    #[test]
    fn impact_gap_none_on_empty() {
        assert!(calculate_impact_gap(&[], &[(1.0, 1.0)], 1380.0, 100.0, false).is_none());
        assert!(calculate_impact_gap(&[(1.0, 1.0)], &[], 1380.0, 100.0, false).is_none());
    }

    #[test]
    fn impact_gap_stops_when_foreign_short() {
        // only enough for $10 worth
        let foreign = [(1.0_f64, 10.0_f64)];
        let bithumb = [(2.0_f64, 1_000.0_f64)];
        let g = calculate_impact_gap(&bithumb, &foreign, 1380.0, 100.0, false);
        assert!(g.is_none());
    }

    #[test]
    fn impact_gap_computes_happy_path() {
        // foreign bid 1.0 qty 200 → sell $100 at price 1 ⇒ vwap = 1.0, qty=100
        // bithumb ask 2.0 qty 200 → buy 100 coins at 2.0 ⇒ vwap = 2.0
        // gap = 2.0 / (1.0 * 1380) * 10_000 ≈ 14.492...
        let foreign = [(1.0, 200.0)];
        let bithumb = [(2.0, 200.0)];
        let g = calculate_impact_gap(&bithumb, &foreign, 1380.0, 100.0, false)
            .expect("should compute");
        let expected = 2.0 / (1.0 * 1380.0) * 10_000.0;
        assert!((g - expected).abs() < 1e-6);
    }

    #[test]
    fn parse_binance_happy() {
        let spot = r#"[{"symbol":"BTCUSDT","bidPrice":"30000.00","askPrice":"30010.00"},{"symbol":"ETHUSDT","bidPrice":"2000.00","askPrice":"2001.00"},{"symbol":"NONUSDT","bidPrice":"0","askPrice":"0"}]"#;
        let fut = r#"[{"symbol":"BTCUSDT","bidPrice":"30001.00","askPrice":"30011.00"}]"#;
        let m = parse_binance_booktickers(spot, fut);
        assert!(m.contains_key("BTC"));
        assert!(m.contains_key("ETH"));
        assert_eq!(m["BTC"].spot.unwrap().bid, 30_000.0);
        assert_eq!(m["BTC"].futures.unwrap().bid, 30_001.0);
        assert!(m["ETH"].futures.is_none());
        // zero-priced skipped
        assert!(!m.contains_key("NON"));
    }

    #[test]
    fn build_gap_result_foreign_and_krw() {
        let exchanges = vec![
            (
                "binance",
                false,
                ExchangeBbos {
                    spot: Some(Bbo::new(30_000.0, 30_010.0)),
                    futures: Some(Bbo::new(30_001.0, 30_011.0)),
                },
            ),
            (
                "upbit",
                true,
                ExchangeBbos {
                    spot: Some(Bbo::new(30_050_000.0, 30_060_000.0)),
                    futures: None,
                },
            ),
        ];
        let inp = BuildInput {
            bithumb_ask: Some(30_100_000.0),
            usdt_krw: Some(1380.0),
            exchanges: &exchanges,
        };
        let out = build_gap_result(&inp);
        assert_eq!(out.len(), 2);
        let binance_gap = out[0].1.spot_gap.unwrap();
        let expected = 30_100_000.0 / (30_000.0 * 1380.0) * 10_000.0;
        assert!((binance_gap - expected).abs() < 1e-6);
        let upbit_gap = out[1].1.spot_gap.unwrap();
        let upbit_expected = 30_100_000.0 / 30_050_000.0 * 10_000.0;
        assert!((upbit_gap - upbit_expected).abs() < 1e-6);
        // Upbit has no futures
        assert!(out[1].1.futures_gap.is_none());
    }
}
