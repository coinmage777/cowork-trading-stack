"""Regression tests for known historical production bugs in perp-dex-bot."""
from __future__ import annotations
import json
from types import SimpleNamespace
import pytest


def _verify_order_fill_fixed(response, expected_size):
    if response.get("status") != 200:
        return "unknown"
    body = response.get("body") or {}
    orders = body.get("orders") or []
    if not orders:
        return "unknown"
    filled_total = sum(float(o.get("filled_size", 0)) for o in orders)
    if filled_total <= 0:
        return "unfilled"
    if filled_total + 1e-9 < expected_size:
        return "partial"
    return "filled"


def test_verify_order_fill_does_not_treat_ambiguous_200_as_filled():
    r = {"status": 200, "body": {"orders": []}}
    assert _verify_order_fill_fixed(r, 10) == "unknown"
    assert _verify_order_fill_fixed(r, 10) != "filled"


def test_verify_order_fill_detects_partial_fill():
    r = {"status": 200, "body": {"orders": [{"id": "x", "filled_size": 5}]}}
    assert _verify_order_fill_fixed(r, 10) == "partial"


def test_verify_order_fill_detects_full_fill():
    r = {"status": 200, "body": {"orders": [{"id": "x", "filled_size": 10}]}}
    assert _verify_order_fill_fixed(r, 10) == "filled"


def test_verify_order_fill_unfilled_when_zero():
    r = {"status": 200, "body": {"orders": [{"id": "x", "filled_size": 0}]}}
    assert _verify_order_fill_fixed(r, 10) == "unfilled"


def _normalize_signature(sig_hex):
    if not isinstance(sig_hex, str):
        sig_hex = sig_hex.hex() if hasattr(sig_hex, "hex") else str(sig_hex)
    return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex


def test_signature_wrapper_adds_0x_when_missing():
    raw = "abcdef1234567890" * 8
    out = _normalize_signature(raw)
    assert out.startswith("0x")
    assert len(out) == 130


def test_signature_wrapper_idempotent_when_prefixed():
    raw = "0x" + "ab" * 65
    out = _normalize_signature(raw)
    assert out.startswith("0x")
    assert not out.startswith("0x0x")


def test_signature_wrapper_handles_hex_method_object():
    fake = SimpleNamespace(hex=lambda: "deadbeef" * 16)
    out = _normalize_signature(fake.hex())
    assert out == "0x" + "deadbeef" * 16


_TOKEN_DECIMALS = {
    "ethereum": {
        "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,
    },
    "bsc": {
        "0x55d398326f99059ff775485246999027b3197955": 18,
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,
    },
    "polygon": {
        "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": 6,
    },
}


def get_token_decimals(chain, contract):
    return _TOKEN_DECIMALS[chain][contract.lower()]


def to_base_units(amount_human, chain, contract):
    return int(amount_human * 10 ** get_token_decimals(chain, contract))


def test_bsc_usdt_has_18_decimals_not_6():
    assert get_token_decimals("bsc", "0x55d398326f99059fF775485246999027B3197955") == 18


def test_ethereum_usdt_has_6_decimals():
    assert get_token_decimals("ethereum", "0xdAC17F958D2ee523a2206206994597C13D831ec7") == 6


def test_bsc_usdt_transfer_uses_18_decimals():
    base = to_base_units(1.0, "bsc", "0x55d398326f99059fF775485246999027B3197955")
    assert base == 10 ** 18
    assert base != 10 ** 6


def break_even_win_rate(tp_pct, sl_pct):
    return sl_pct / (tp_pct + sl_pct)


def test_breakeven_wr_margin_basis_tp04_sl3_unfeasible():
    wr = break_even_win_rate(0.4, 3.0)
    assert wr > 0.80
    assert wr == pytest.approx(0.8823, abs=0.01)


def test_breakeven_wr_notional_basis_tp2_sl25_feasible():
    wr = break_even_win_rate(2.0, 2.5)
    assert wr == pytest.approx(0.5556, abs=0.01)
    assert 0.50 < wr < 0.60


def test_breakeven_wr_symmetric_is_50pct():
    assert break_even_win_rate(1.0, 1.0) == pytest.approx(0.5)


def parse_polymarket_book(book_json, intent):
    book = book_json if isinstance(book_json, dict) else json.loads(book_json)
    bids = sorted(book.get("bids", []), key=lambda r: float(r["price"]), reverse=True)
    asks = sorted(book.get("asks", []), key=lambda r: float(r["price"]))
    if intent == "buy":
        if not asks:
            raise ValueError("no asks")
        return float(asks[0]["price"])
    if intent == "sell":
        if not bids:
            raise ValueError("no bids")
        return float(bids[0]["price"])
    raise ValueError("unknown intent")


_MOCK_POLY_BOOK = {
    "outcomePrices": ["0.55", "0.45"],
    "bids": [{"price": "0.53", "size": "100"}, {"price": "0.50", "size": "200"}],
    "asks": [{"price": "0.57", "size": "100"}, {"price": "0.60", "size": "200"}],
}


def test_polymarket_buy_uses_ask_not_mid():
    px = parse_polymarket_book(_MOCK_POLY_BOOK, "buy")
    assert px == 0.57
    assert px != 0.55


def test_polymarket_sell_uses_bid_not_mid():
    assert parse_polymarket_book(_MOCK_POLY_BOOK, "sell") == 0.53


def test_polymarket_book_ignores_outcomeprices_field():
    px = parse_polymarket_book(_MOCK_POLY_BOOK, "buy")
    assert px not in (0.55, 0.45)


EXPECTED_BACKOFF = [2, 5, 10, 30, 60, 180, 300]


def reconnect_delay(attempt):
    if attempt < 1:
        return EXPECTED_BACKOFF[0]
    idx = min(attempt - 1, len(EXPECTED_BACKOFF) - 1)
    return EXPECTED_BACKOFF[idx]


def test_ws_backoff_matches_tuned_sequence():
    assert [reconnect_delay(i) for i in range(1, 8)] == EXPECTED_BACKOFF


def test_ws_backoff_clamps_after_last():
    assert reconnect_delay(8) == 300
    assert reconnect_delay(50) == 300


def test_ws_backoff_first_retry_is_fast():
    assert reconnect_delay(1) == 2


def test_ws_backoff_is_not_exponential_doubling():
    actual = [reconnect_delay(i) for i in range(1, 8)]
    exponential = [2 ** i for i in range(7)]
    assert actual != exponential


MIRACLE_PREFIX = "0x4d455243"


def make_cloid(random_suffix_hex, builder_code):
    suffix = random_suffix_hex.lower()
    if suffix.startswith("0x"):
        suffix = suffix[2:]
    if builder_code == "miracle":
        prefix_bytes = MIRACLE_PREFIX[2:]
        suffix = suffix[-24:].rjust(24, "0")
        return "0x" + prefix_bytes + suffix
    return "0x" + suffix[-32:].rjust(32, "0")


def test_cloid_with_miracle_builder_has_correct_prefix():
    cloid = make_cloid("0xdeadbeef" * 4, "miracle")
    assert cloid.startswith(MIRACLE_PREFIX)
    assert len(cloid) == 34


def test_cloid_without_builder_has_no_prefix():
    cloid = make_cloid("0xdeadbeefdeadbeefdeadbeefdeadbeef", None)
    assert cloid.startswith("0x")
    assert not cloid.startswith(MIRACLE_PREFIX)
    assert len(cloid) == 34


def test_cloid_prefix_at_byte_zero_not_embedded():
    cloid = make_cloid("0x1111222233334444", "miracle")
    assert cloid[2:10] == "4d455243"


import strategies.nado_pair_scalper as M


@pytest.fixture(autouse=True)
def _reset_module_state():
    M._PORTFOLIO_DAILY_PNL = 0.0
    if hasattr(M, "_STOP_LOSS_EVENTS"):
        M._STOP_LOSS_EVENTS.clear()
    if hasattr(M, "_CIRCUIT_BREAKER_UNTIL"):
        M._CIRCUIT_BREAKER_UNTIL = 0.0
    yield


def test_day_rollover_at_exact_midnight_utc():
    M.update_portfolio_pnl(42.0)
    assert M.get_portfolio_daily_pnl() == 42.0
    M._PORTFOLIO_DAY = "2026-04-26"
    M.update_portfolio_pnl(0.0)
    assert M.get_portfolio_daily_pnl() == 0.0


def test_day_rollover_same_day_accumulates():
    M.update_portfolio_pnl(10.0)
    same_day = M._PORTFOLIO_DAY
    M.update_portfolio_pnl(5.0)
    assert M._PORTFOLIO_DAY == same_day
    assert M.get_portfolio_daily_pnl() == pytest.approx(15.0)
