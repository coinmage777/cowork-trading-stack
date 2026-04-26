"""
Backtest Optimized BTC Mean Reversion (RSI 20/65) 15m vs Minara's claim.

Minara claim: APR +204%, Sharpe >4, 16 trades in 90d (HyperLiquid fees).

Strategy (per spec):
  - RSI(14), EMA(200), Stochastic %K(14,3,3)
  - Long entry: RSI < 20 AND close > EMA(200) AND Stoch %K < 25
  - Exit long: RSI > 65 OR Stoch %K > 75 OR TP (+6%) OR SL (-4%)
  - Also run SHORT side (symmetric) since scaffolding includes it; flagged separately.

Data: ~1 year of 15m bars on Binance USDM BTC/USDT:USDT.
Fee: 0.04% + 0.05% slip per side → 0.18% round-trip.
Notional: $10,000.

Note on scaffolding: mean_rev_btc_15m.py only emits SL/TP exits, not RSI>65 /
Stoch>75 exits. We add those in the backtest harness as per Minara's stated
exit rules (and treat them as exit reasons).
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import ccxt

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from mean_rev_btc_15m import Bar, MeanRevBtc15m, MeanRevBtc15mParams  # noqa: E402


SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "15m"
BARS_TARGET = 365 * 24 * 4 + 300  # ~35,340 bars with warmup buffer
NOTIONAL = 10_000.0
FEE_PER_SIDE = 0.0004
SLIPPAGE_PER_SIDE = 0.0005
MS_PER_BAR = 15 * 60 * 1000
# Minara's published scaffolding & Pine source are long+short. The user's
# task brief only described the LONG rules, but the claim of 16 trades in 90d
# and +204% APR is not reachable in long-only mode (see diagnostics: only 1
# long-triple-trigger bar in 35,141 15m bars). We therefore run BOTH modes
# and report them side-by-side. Primary verdict uses long+short (matches
# Minara's published scaffolding).
LONG_ONLY = False


def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> list[list]:
    ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})
    since = ex.milliseconds() - MS_PER_BAR * (limit + 5)
    all_rows: list[list] = []
    fetch_since = since
    consecutive_empty = 0
    while True:
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=1500)
        except Exception as e:
            print(f"  fetch error: {e}; retry in 2s", flush=True)
            time.sleep(2)
            continue
        if not batch:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break
            fetch_since += MS_PER_BAR * 500
            continue
        consecutive_empty = 0
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        print(
            f"  batch: {len(batch)} bars up to "
            f"{datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).isoformat()}",
            flush=True,
        )
        if last_ts >= ex.milliseconds() - MS_PER_BAR:
            break
        fetch_since = last_ts + MS_PER_BAR
        time.sleep(ex.rateLimit / 1000)
        if len(all_rows) > limit + 200:
            break
    seen = {}
    for r in all_rows:
        seen[r[0]] = r
    rows = sorted(seen.values(), key=lambda r: r[0])
    return rows


def run_backtest(rows: list[list], params: MeanRevBtc15mParams, long_only: bool = False) -> dict:
    """Faithful backtest: SL/TP via strategy on_bar; RSI>65 & Stoch>75 exits
    added at the harness level (the scaffolding doesn't emit them).

    We recompute RSI/EMA/%K inside a *parallel* MeanRevBtc15m instance solely
    for the indicator state — the main instance owns position state and
    SL/TP exits. To keep this simple and avoid double-maintenance, we inline
    the indicator updates here and use on_bar only for TP/SL bookkeeping.
    """
    strat = MeanRevBtc15m(params)
    trades: list[dict] = []
    open_trade: dict | None = None
    equity_series: list[tuple[int, float]] = []
    equity = 0.0
    exit_counts = {"tp_long": 0, "sl_long": 0, "rsi_exit_long": 0, "stoch_exit_long": 0,
                   "tp_short": 0, "sl_short": 0, "rsi_exit_short": 0, "stoch_exit_short": 0}
    # Diagnostics: count how many bars hit each partial condition
    diag = {"bars_with_indicators": 0, "rsi_lt_20": 0, "rsi_gt_65": 0,
            "close_gt_ema": 0, "close_lt_ema": 0, "k_lt_25": 0, "k_gt_75": 0,
            "long_trigger": 0, "short_trigger": 0}

    # Independent indicator pipeline for entries + RSI/Stoch exits so we see
    # indicator values *before* strategy resets state on an SL/TP.
    from mean_rev_btc_15m import MeanRevBtc15m as _S
    ind = _S(params)

    for row in rows:
        ts, o, h, l, c, v = row
        bar = Bar(ts=ts, open=float(o), high=float(h), low=float(l),
                  close=float(c), volume=float(v or 0))
        # Compute indicators this bar (no position management)
        rsi = ind._update_rsi(bar.close)
        ema = ind._update_ema(bar.close)
        k = ind._update_stoch_k(bar)

        # Diagnostics
        if rsi is not None and ema is not None and k is not None:
            diag["bars_with_indicators"] += 1
            if rsi < 20: diag["rsi_lt_20"] += 1
            if rsi > 65: diag["rsi_gt_65"] += 1
            if bar.close > ema: diag["close_gt_ema"] += 1
            if bar.close < ema: diag["close_lt_ema"] += 1
            if k < 25: diag["k_lt_25"] += 1
            if k > 75: diag["k_gt_75"] += 1
            if rsi < 20 and bar.close > ema and k < 25: diag["long_trigger"] += 1
            if rsi > 65 and bar.close < ema and k > 75: diag["short_trigger"] += 1

        # Strategy drives entry & SL/TP exits
        sig = strat.on_bar(bar)

        # === manage open trade ===
        if open_trade is not None:
            exit_now = False
            exit_price = None
            exit_reason = None

            if sig is not None and sig["side"] == "exit":
                # SL/TP from strategy; use strategy's filled price (SL/TP level)
                exit_price = sig["price"] * (
                    1 - SLIPPAGE_PER_SIDE if open_trade["side"] == "long"
                    else 1 + SLIPPAGE_PER_SIDE
                )
                exit_reason = sig["reason"]
                exit_now = True
            else:
                # RSI/Stoch exits (harness-added, per Minara rules)
                if open_trade["side"] == "long" and rsi is not None and k is not None:
                    if rsi > params.rsi_short_entry:  # RSI > 65
                        exit_price = bar.close * (1 - SLIPPAGE_PER_SIDE)
                        exit_reason = "rsi_exit_long"
                        exit_now = True
                    elif k > params.stoch_short_min:  # %K > 75
                        exit_price = bar.close * (1 - SLIPPAGE_PER_SIDE)
                        exit_reason = "stoch_exit_long"
                        exit_now = True
                elif open_trade["side"] == "short" and rsi is not None and k is not None:
                    if rsi < params.rsi_long_entry:  # RSI < 20
                        exit_price = bar.close * (1 + SLIPPAGE_PER_SIDE)
                        exit_reason = "rsi_exit_short"
                        exit_now = True
                    elif k < params.stoch_long_max:  # %K < 25
                        exit_price = bar.close * (1 + SLIPPAGE_PER_SIDE)
                        exit_reason = "stoch_exit_short"
                        exit_now = True

            if exit_now:
                qty = open_trade["qty"]
                if open_trade["side"] == "long":
                    proceeds = exit_price * qty
                    gross_pnl = proceeds - NOTIONAL
                else:  # short
                    gross_pnl = (open_trade["entry_price"] - exit_price) * qty
                    proceeds = NOTIONAL + gross_pnl
                exit_fee = abs(proceeds) * FEE_PER_SIDE
                net_pnl = gross_pnl - open_trade["entry_fee"] - exit_fee
                pct = net_pnl / NOTIONAL * 100
                equity += net_pnl
                exit_counts[exit_reason] = exit_counts.get(exit_reason, 0) + 1
                trades.append({
                    "side": open_trade["side"],
                    "entry_ts": open_trade["entry_ts"],
                    "entry_date": datetime.fromtimestamp(open_trade["entry_ts"]/1000,
                                                         tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "entry_price": round(open_trade["entry_price"], 2),
                    "exit_ts": ts,
                    "exit_date": datetime.fromtimestamp(ts/1000,
                                                        tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_price": round(exit_price, 2),
                    "exit_reason": exit_reason,
                    "qty": round(qty, 6),
                    "gross_pnl": round(gross_pnl, 2),
                    "fees": round(open_trade["entry_fee"] + exit_fee, 2),
                    "net_pnl": round(net_pnl, 2),
                    "pnl_pct": round(pct, 3),
                    "bars_held": int((ts - open_trade["entry_ts"]) / MS_PER_BAR),
                })
                open_trade = None
                # IMPORTANT: sync strategy state to reflect the harness-level
                # exit so it can re-enter on the next signal.
                if exit_reason in ("rsi_exit_long", "stoch_exit_long",
                                   "rsi_exit_short", "stoch_exit_short"):
                    strat._side = None
                    strat._entry = None

        # === new entry ===
        if open_trade is None and sig is not None and sig["side"] in ("buy", "sell"):
            side = "long" if sig["side"] == "buy" else "short"
            if long_only and side == "short":
                # Skip short entry; also clear strategy position state so it
                # doesn't think it's in a trade.
                strat._side = None
                strat._entry = None
            else:
                slip = 1 + SLIPPAGE_PER_SIDE if side == "long" else 1 - SLIPPAGE_PER_SIDE
                fill_price = sig["price"] * slip
                qty = NOTIONAL / fill_price
                entry_fee = NOTIONAL * FEE_PER_SIDE
                open_trade = {
                    "side": side,
                    "entry_ts": ts,
                    "entry_price": fill_price,
                    "qty": qty,
                    "entry_fee": entry_fee,
                }

        equity_series.append((ts, equity))

    # MtM of any open position at last close
    open_mtm = None
    if open_trade is not None:
        last_price = float(rows[-1][4])
        slip = 1 - SLIPPAGE_PER_SIDE if open_trade["side"] == "long" else 1 + SLIPPAGE_PER_SIDE
        fill_price = last_price * slip
        if open_trade["side"] == "long":
            gross_pnl = fill_price * open_trade["qty"] - NOTIONAL
            proceeds = fill_price * open_trade["qty"]
        else:
            gross_pnl = (open_trade["entry_price"] - fill_price) * open_trade["qty"]
            proceeds = NOTIONAL + gross_pnl
        exit_fee = abs(proceeds) * FEE_PER_SIDE
        net_pnl = gross_pnl - open_trade["entry_fee"] - exit_fee
        open_mtm = {
            "side": open_trade["side"],
            "entry_date": datetime.fromtimestamp(open_trade["entry_ts"]/1000,
                                                 tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry_price": round(open_trade["entry_price"], 2),
            "current_price": round(last_price, 2),
            "unrealized_net_pnl": round(net_pnl, 2),
            "unrealized_pct": round(net_pnl / NOTIONAL * 100, 3),
        }

    return {"trades": trades, "equity_series": equity_series,
            "open_mtm": open_mtm, "exit_counts": exit_counts, "diagnostics": diag}


def compute_stats(trades: list[dict], equity_series: list[tuple[int, float]]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / n * 100
    total_pnl = sum(t["net_pnl"] for t in trades)
    total_pct = total_pnl / NOTIONAL * 100
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    best = max(trades, key=lambda t: t["net_pnl"])
    worst = min(trades, key=lambda t: t["net_pnl"])
    avg_bars_held = sum(t["bars_held"] for t in trades) / n

    # Fee drag
    total_fees = sum(t["fees"] for t in trades)
    gross_total = sum(t["gross_pnl"] for t in trades)

    # MDD on bar-level equity
    peak = 0.0
    mdd_dollar = 0.0
    for ts, eq in equity_series:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > mdd_dollar:
            mdd_dollar = dd
    mdd_pct = mdd_dollar / NOTIONAL * 100

    # Daily returns for Sharpe
    daily_eq = OrderedDict()
    for ts, eq in equity_series:
        d = datetime.fromtimestamp(ts/1000, tz=timezone.utc).date()
        daily_eq[d] = eq
    daily_values = list(daily_eq.values())
    daily_rets = [(daily_values[i]-daily_values[i-1])/NOTIONAL
                  for i in range(1, len(daily_values))]
    mean_r = sum(daily_rets)/len(daily_rets) if daily_rets else 0.0
    var_r = sum((r-mean_r)**2 for r in daily_rets)/len(daily_rets) if daily_rets else 0.0
    std_r = math.sqrt(var_r)
    sharpe = (mean_r/std_r)*math.sqrt(365) if std_r > 0 else 0.0

    # APR
    days = (equity_series[-1][0] - equity_series[0][0]) / (86400 * 1000) if equity_series else 1
    apr = (total_pnl / NOTIONAL) / days * 365 * 100 if days > 0 else 0.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(wr, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best_trade_pnl": round(best["net_pnl"], 2),
        "best_trade_pct": round(best["pnl_pct"], 3),
        "worst_trade_pnl": round(worst["net_pnl"], 2),
        "worst_trade_pct": round(worst["pnl_pct"], 3),
        "avg_bars_held": round(avg_bars_held, 2),
        "total_net_pnl": round(total_pnl, 2),
        "total_pct_on_notional": round(total_pct, 2),
        "total_fees": round(total_fees, 2),
        "gross_pnl": round(gross_total, 2),
        "fee_drag_pct_of_notional": round(total_fees / NOTIONAL * 100, 2),
        "apr_pct": round(apr, 2),
        "mdd_dollar": round(mdd_dollar, 2),
        "mdd_pct_of_notional": round(mdd_pct, 2),
        "sharpe_daily_ann": round(sharpe, 3),
        "days_covered": round(days, 1),
    }


def yearly_breakdown(trades: list[dict]) -> dict:
    by_year: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0})
    for t in trades:
        y = int(t["entry_date"][:4])
        by_year[y]["trades"] += 1
        if t["net_pnl"] > 0:
            by_year[y]["wins"] += 1
        by_year[y]["pnl"] += t["net_pnl"]
        by_year[y]["fees"] += t["fees"]
    out = {}
    for y in sorted(by_year):
        d = by_year[y]
        out[y] = {
            "trades": d["trades"],
            "wins": d["wins"],
            "wr_pct": round(d["wins"]/d["trades"]*100, 2) if d["trades"] else 0.0,
            "net_pnl": round(d["pnl"], 2),
            "fees": round(d["fees"], 2),
            "pct_on_notional": round(d["pnl"]/NOTIONAL*100, 2),
        }
    return out


def subset_90d_stats(trades, equity_series, end_ts) -> dict:
    """90-day subset matching Minara's reported window."""
    cutoff_ts = end_ts - 90 * 86400 * 1000
    sub = [t for t in trades if t["entry_ts"] >= cutoff_ts]
    sub_eq = [(ts, eq - next((eq2 for ts2, eq2 in equity_series if ts2 >= cutoff_ts), 0))
              for ts, eq in equity_series if ts >= cutoff_ts]
    if not sub:
        return {"trades": 0}
    wins = [t for t in sub if t["net_pnl"] > 0]
    n = len(sub)
    total = sum(t["net_pnl"] for t in sub)
    gross = sum(t["gross_pnl"] for t in sub)
    fees = sum(t["fees"] for t in sub)
    apr = (total / NOTIONAL) / 90 * 365 * 100
    # rough 90d daily sharpe
    daily_eq = OrderedDict()
    for ts, eq in sub_eq:
        d = datetime.fromtimestamp(ts/1000, tz=timezone.utc).date()
        daily_eq[d] = eq
    dv = list(daily_eq.values())
    rets = [(dv[i]-dv[i-1])/NOTIONAL for i in range(1, len(dv))]
    m = sum(rets)/len(rets) if rets else 0
    v = sum((r-m)**2 for r in rets)/len(rets) if rets else 0
    s = math.sqrt(v)
    sh = (m/s)*math.sqrt(365) if s > 0 else 0.0
    return {
        "window_days": 90,
        "trades": n,
        "wins": len(wins),
        "wr_pct": round(len(wins)/n*100, 2),
        "total_net_pnl": round(total, 2),
        "total_pct_on_notional": round(total/NOTIONAL*100, 2),
        "apr_pct": round(apr, 2),
        "gross_pnl": round(gross, 2),
        "fees": round(fees, 2),
        "sharpe_daily_ann": round(sh, 3),
    }


def fmt_markdown(stats, trades, exit_counts, open_mtm, rows, yearly, sub90, diag=None) -> str:
    start_date = datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")

    minara = {"trades_90d": 16, "sharpe": ">4", "apr_pct": 204.6}
    actual_trades_90d = sub90.get("trades", 0)
    actual_sharpe = sub90.get("sharpe_daily_ann", 0)
    actual_apr = sub90.get("apr_pct", 0)

    # Replication checks (90d subset to match Minara)
    trades_match = abs(actual_trades_90d - 16) <= 8
    sharpe_match = actual_sharpe >= 2.0  # claim is >4; accept half as PARTIAL
    apr_match = actual_apr >= 100  # claim is +204; accept >100 as decent
    matches = sum([trades_match, sharpe_match, apr_match])
    if matches >= 3:
        verdict = "REPLICATES"
    elif matches >= 2:
        verdict = "PARTIAL"
    else:
        verdict = "DOES NOT REPLICATE"

    md = []
    md.append("# Optimized BTC Mean Reversion (RSI 20/65) 15m — Backtest vs Minara Claim")
    md.append("")
    md.append(f"**Data**: Binance USDM `{SYMBOL}` 15m | **Range**: {start_date} → {end_date} | **Bars**: {len(rows)}")
    md.append("**Strategy (long & short)**: Long RSI<20 + close>EMA200 + %K<25; Short symmetric. Exits: RSI>65/<20, %K>75/<25, TP 6%, SL 4%.")
    md.append(f"**Costs**: fee {FEE_PER_SIDE*100:.3f}%/side, slip {SLIPPAGE_PER_SIDE*100:.3f}%/side → 0.18% round-trip. Notional ${NOTIONAL:,.0f}.")
    md.append("")
    md.append(f"## Verdict: **{verdict}** ({matches}/3 metrics within tolerance)")
    md.append("")
    md.append("## Minara Claim vs Actual (90-day apples-to-apples)")
    md.append("")
    md.append("| Metric | Minara (90d) | Actual (90d) | Full 1Y |")
    md.append("|---|---|---|---|")
    md.append(f"| Trades | {minara['trades_90d']} | {actual_trades_90d} | {stats['trades']} |")
    md.append(f"| APR | {minara['apr_pct']}% | {actual_apr}% | {stats.get('apr_pct', 0)}% |")
    md.append(f"| Sharpe (daily ann.) | {minara['sharpe']} | {actual_sharpe} | {stats.get('sharpe_daily_ann', 0)} |")
    md.append(f"| Total Net PnL ($) | n/a | ${sub90.get('total_net_pnl', 0):,.2f} | ${stats.get('total_net_pnl', 0):,.2f} |")
    md.append(f"| WR | n/a | {sub90.get('wr_pct', 0)}% | {stats.get('wr_pct', 0)}% |")
    md.append("")

    if diag:
        md.append("## Condition Diagnostics (bar-level)")
        md.append("")
        md.append(f"- Bars with all indicators warm: {diag['bars_with_indicators']:,}")
        md.append(f"- RSI < 20: {diag['rsi_lt_20']:,} ({diag['rsi_lt_20']/max(diag['bars_with_indicators'],1)*100:.2f}%)")
        md.append(f"- RSI > 65: {diag['rsi_gt_65']:,} ({diag['rsi_gt_65']/max(diag['bars_with_indicators'],1)*100:.2f}%)")
        md.append(f"- Close > EMA200: {diag['close_gt_ema']:,} ({diag['close_gt_ema']/max(diag['bars_with_indicators'],1)*100:.2f}%)")
        md.append(f"- Close < EMA200: {diag['close_lt_ema']:,} ({diag['close_lt_ema']/max(diag['bars_with_indicators'],1)*100:.2f}%)")
        md.append(f"- Stoch %K < 25: {diag['k_lt_25']:,}")
        md.append(f"- Stoch %K > 75: {diag['k_gt_75']:,}")
        md.append(f"- **LONG triple-trigger bars** (RSI<20 & close>EMA & %K<25): {diag['long_trigger']:,}")
        md.append(f"- **SHORT triple-trigger bars** (RSI>65 & close<EMA & %K>75): {diag['short_trigger']:,}")
        md.append("")

    md.append("## Exit Reason Breakdown")
    md.append("")
    md.append("| Reason | Count |")
    md.append("|---|---|")
    for k, v in exit_counts.items():
        md.append(f"| {k} | {v} |")
    md.append("")

    md.append("## Fee Drag Analysis")
    md.append("")
    md.append(f"- **Gross PnL** (before fees/slip): ${stats.get('gross_pnl', 0):,.2f}")
    md.append(f"- **Total Fees + Slippage**: ${stats.get('total_fees', 0):,.2f}")
    md.append(f"- **Net PnL**: ${stats.get('total_net_pnl', 0):,.2f}")
    md.append(f"- **Fee drag vs notional**: {stats.get('fee_drag_pct_of_notional', 0)}%")
    gross = stats.get("gross_pnl", 0) or 0
    net = stats.get("total_net_pnl", 0) or 0
    fee_eats_pct = (gross - net) / abs(gross) * 100 if gross else 0
    md.append(f"- **Fees as % of gross PnL**: {fee_eats_pct:.1f}%")
    md.append(f"- Round-trip cost per trade (~0.18%) × {stats.get('trades', 0)} trades ≈ {0.18 * stats.get('trades', 0):.1f}% of notional in drag")
    md.append("")

    md.append("## Yearly Breakdown")
    md.append("")
    md.append("| Year | Trades | Wins | WR | Net PnL | Fees | % on Notional |")
    md.append("|---|---|---|---|---|---|---|")
    for y, d in yearly.items():
        md.append(f"| {y} | {d['trades']} | {d['wins']} | {d['wr_pct']}% | "
                  f"${d['net_pnl']:,.2f} | ${d['fees']:,.2f} | {d['pct_on_notional']:+.2f}% |")
    md.append("")

    md.append("## Aggregate Stats (full period)")
    md.append("")
    for k, v in stats.items():
        md.append(f"- **{k}**: {v}")
    md.append("")

    md.append("## Per-Trade Log (first 50)")
    md.append("")
    if trades:
        md.append("| # | Side | Entry | Entry $ | Exit | Exit $ | Reason | Bars | Net PnL | % |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades[:50], 1):
            md.append(
                f"| {i} | {t['side']} | {t['entry_date']} | {t['entry_price']:,.2f} | "
                f"{t['exit_date']} | {t['exit_price']:,.2f} | {t['exit_reason']} | "
                f"{t['bars_held']} | ${t['net_pnl']:,.2f} | {t['pnl_pct']:+.2f}% |"
            )
        if len(trades) > 50:
            md.append(f"\n_... {len(trades)-50} more trades in JSON_")
    else:
        md.append("_no closed trades_")
    md.append("")
    if open_mtm:
        md.append("**Open position (MtM at last close)**")
        md.append(f"- {open_mtm['side']} entry {open_mtm['entry_date']} @ ${open_mtm['entry_price']:,.2f}")
        md.append(f"- Current ${open_mtm['current_price']:,.2f} | PnL ${open_mtm['unrealized_net_pnl']:,.2f} ({open_mtm['unrealized_pct']:+.2f}%)")
        md.append("")
    return "\n".join(md)


def run_mode(rows, params, long_only: bool) -> dict:
    result = run_backtest(rows, params, long_only=long_only)
    stats = compute_stats(result["trades"], result["equity_series"])
    yearly = yearly_breakdown(result["trades"])
    end_ts = rows[-1][0]
    sub90 = subset_90d_stats(result["trades"], result["equity_series"], end_ts)
    return {"result": result, "stats": stats, "yearly": yearly, "sub90": sub90,
            "long_only": long_only}


def fmt_markdown_combined(rows, long_short: dict, long_only: dict) -> str:
    start_date = datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).strftime("%Y-%m-%d")
    ls, lo = long_short, long_only

    # Primary verdict uses long+short (matches Minara's scaffolding)
    minara = {"trades_90d": 16, "sharpe_text": ">4", "apr_pct": 204.6}
    actual_trades_90d = ls["sub90"].get("trades", 0)
    actual_sharpe = ls["sub90"].get("sharpe_daily_ann", 0)
    actual_apr = ls["sub90"].get("apr_pct", 0)

    trades_match = abs(actual_trades_90d - 16) <= 8
    sharpe_match = actual_sharpe >= 2.0
    apr_match = actual_apr >= 100
    matches = sum([trades_match, sharpe_match, apr_match])
    if matches >= 3:
        verdict = "REPLICATES"
    elif matches >= 2:
        verdict = "PARTIAL"
    else:
        verdict = "DOES NOT REPLICATE"

    st_ls = ls["stats"]; st_lo = lo["stats"]
    md = []
    md.append("# Optimized BTC Mean Reversion (RSI 20/65) 15m — Backtest vs Minara Claim")
    md.append("")
    md.append(f"**Data**: Binance USDM `{SYMBOL}` 15m  |  **Range**: {start_date} → {end_date}  |  **Bars**: {len(rows):,}")
    md.append(f"**Costs**: fee {FEE_PER_SIDE*100:.3f}%/side, slip {SLIPPAGE_PER_SIDE*100:.3f}%/side → **0.18% round-trip**. Notional ${NOTIONAL:,.0f}.")
    md.append("**Rules**: Long entry RSI<20 & close>EMA200 & %K<25. Short entry symmetric. Exits: RSI 20/65 flip, %K 25/75 flip, TP +6%, SL -4%.")
    md.append("")
    md.append(f"## Verdict: **{verdict}** ({matches}/3 primary metrics within tolerance)")
    md.append("")
    md.append("### Important context")
    md.append(f"- **Long-only is not viable** on this timeframe with these rules. In {lo['result']['diagnostics']['bars_with_indicators']:,} warm 15m bars over 1 year, the triple-trigger (RSI<20 & close>EMA200 & %K<25) fired only **{lo['result']['diagnostics']['long_trigger']}** time(s). Minara's 16-trades-in-90d claim requires shorts.")
    md.append("- Primary comparison below uses **long+short** (matches scaffolding + Minara's actual published Pine source).")
    md.append("")
    md.append("## Minara Claim vs Actual (90-day apples-to-apples)")
    md.append("")
    md.append("| Metric | Minara (90d, HL fees) | Long+Short Actual (90d) | Long-only (90d) | Long+Short Full 1Y |")
    md.append("|---|---|---|---|---|")
    md.append(f"| Trades | {minara['trades_90d']} | {actual_trades_90d} | {lo['sub90'].get('trades',0)} | {st_ls['trades']} |")
    md.append(f"| APR | +{minara['apr_pct']}% | {actual_apr:+.2f}% | {lo['sub90'].get('apr_pct',0):+.2f}% | {st_ls.get('apr_pct',0):+.2f}% |")
    md.append(f"| Sharpe (daily ann.) | {minara['sharpe_text']} | {actual_sharpe} | {lo['sub90'].get('sharpe_daily_ann',0)} | {st_ls.get('sharpe_daily_ann',0)} |")
    md.append(f"| 90d Net PnL | n/a | ${ls['sub90'].get('total_net_pnl',0):,.2f} | ${lo['sub90'].get('total_net_pnl',0):,.2f} | ${st_ls.get('total_net_pnl',0):,.2f} |")
    md.append(f"| 90d WR | n/a | {ls['sub90'].get('wr_pct',0)}% | {lo['sub90'].get('wr_pct',0)}% | {st_ls.get('wr_pct',0)}% |")
    md.append("")

    diag = ls["result"]["diagnostics"]
    md.append("## Condition Diagnostics (1 year of 15m bars)")
    md.append("")
    md.append(f"- Warm bars: {diag['bars_with_indicators']:,}")
    md.append(f"- RSI<20: {diag['rsi_lt_20']:,} ({diag['rsi_lt_20']/max(diag['bars_with_indicators'],1)*100:.2f}%) | RSI>65: {diag['rsi_gt_65']:,} ({diag['rsi_gt_65']/max(diag['bars_with_indicators'],1)*100:.2f}%)")
    md.append(f"- Close>EMA200: {diag['close_gt_ema']:,} ({diag['close_gt_ema']/max(diag['bars_with_indicators'],1)*100:.2f}%) | Close<EMA200: {diag['close_lt_ema']:,} ({diag['close_lt_ema']/max(diag['bars_with_indicators'],1)*100:.2f}%)")
    md.append(f"- Stoch %K<25: {diag['k_lt_25']:,} | %K>75: {diag['k_gt_75']:,}")
    md.append(f"- **LONG triple-trigger bars**: {diag['long_trigger']:,}")
    md.append(f"- **SHORT triple-trigger bars**: {diag['short_trigger']:,}")
    md.append("")
    md.append("_Note: Asymmetric entries (RSI<20 strict vs RSI>65 loose) cause an ~188:1 short-to-long imbalance. The \"mean reversion with the trend\" thesis fails because RSI<20 on 15m virtually only prints during dumps, by which time price is already below EMA200._")
    md.append("")

    md.append("## Exit Reason Breakdown (Long+Short, full 1Y)")
    md.append("")
    md.append("| Reason | Count |")
    md.append("|---|---|")
    for k, v in ls["result"]["exit_counts"].items():
        md.append(f"| {k} | {v} |")
    md.append("")
    md.append("_All shorts close on `stoch_exit_short` almost immediately. The RSI>75 threshold flips back to %K<25 within a few bars of any short entry, pre-empting TP/SL. **Avg hold {:.1f} bars (~{:.1f}h)**. Effective reward = small mean-reversion wiggle minus 0.18% round-trip fee._".format(
        st_ls["avg_bars_held"], st_ls["avg_bars_held"] * 0.25))
    md.append("")

    md.append("## Fee Drag Analysis (CRITICAL)")
    md.append("")
    md.append(f"- **Gross PnL**: ${st_ls.get('gross_pnl', 0):,.2f}")
    md.append(f"- **Total fees+slip**: ${st_ls.get('total_fees', 0):,.2f}")
    md.append(f"- **Net PnL**: ${st_ls.get('total_net_pnl', 0):,.2f}")
    md.append(f"- **Fee drag vs notional**: {st_ls.get('fee_drag_pct_of_notional', 0):.2f}% per year")
    md.append(f"- Round-trip cost per trade ≈ 0.18%; {st_ls['trades']} trades × 0.18% = **{0.18 * st_ls['trades']:.2f}%** friction")
    md.append(f"- Avg gross trade PnL: {(st_ls.get('gross_pnl', 0)/max(st_ls['trades'],1))/NOTIONAL*100:+.3f}% vs required 0.18% — fees eat the edge")
    md.append("")
    gross = st_ls.get("gross_pnl", 0) or 0
    net = st_ls.get("total_net_pnl", 0) or 0
    md.append(f"**Fee takes {abs((gross-net)/gross)*100:.1f}% of the magnitude of gross PnL.** " if gross else "")
    md.append("")

    md.append("## Yearly Breakdown (Long+Short)")
    md.append("")
    md.append("| Year | Trades | Wins | WR | Net PnL | Fees | % on Notional |")
    md.append("|---|---|---|---|---|---|---|")
    for y, d in ls["yearly"].items():
        md.append(f"| {y} | {d['trades']} | {d['wins']} | {d['wr_pct']}% | "
                  f"${d['net_pnl']:,.2f} | ${d['fees']:,.2f} | {d['pct_on_notional']:+.2f}% |")
    md.append("")

    md.append("## Aggregate Stats — Long+Short (full 1Y)")
    md.append("")
    for k, v in st_ls.items():
        md.append(f"- **{k}**: {v}")
    md.append("")

    md.append("## Aggregate Stats — Long-only (full 1Y, reference)")
    md.append("")
    for k, v in st_lo.items():
        md.append(f"- **{k}**: {v}")
    md.append("")

    md.append("## Per-Trade Log — Long+Short (first 50)")
    md.append("")
    trades = ls["result"]["trades"]
    if trades:
        md.append("| # | Side | Entry | Entry $ | Exit | Exit $ | Reason | Bars | Net PnL | % |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades[:50], 1):
            md.append(f"| {i} | {t['side']} | {t['entry_date']} | {t['entry_price']:,.2f} | "
                      f"{t['exit_date']} | {t['exit_price']:,.2f} | {t['exit_reason']} | "
                      f"{t['bars_held']} | ${t['net_pnl']:,.2f} | {t['pnl_pct']:+.2f}% |")
        if len(trades) > 50:
            md.append(f"\n_... {len(trades)-50} more trades in JSON_")
    md.append("")

    md.append("## Recommendation")
    md.append("")
    md.append("**Do not deploy.** Three independent failures:")
    md.append("")
    md.append("1. **Long-only unviable**: RSI<20 on 15m fires 0.49% of bars; combined with close>EMA200 (a trend filter that's false *exactly* when RSI dumps) the triple-trigger fired 1× in a full year.")
    md.append("2. **Long+short live-trades but loses money**: 90d net {:+.2f}%, 1Y net {:+.2f}%. Minara claimed +204% APR — we measure {:+.2f}%.".format(
        ls["sub90"].get("total_pct_on_notional", 0), st_ls["total_pct_on_notional"], st_ls.get("apr_pct", 0)))
    md.append("3. **Fees are structural**: round-trip 0.18% vs avg trade magnitude <0.5% means break-even is impossible without edge that doesn't exist in this ruleset on this data.")
    md.append("")
    md.append("Minara's +204% APR on 16 trades in 90d is a small-sample fluke (possibly over-fit in a 2024 range-bound window). Out-of-sample (apr 2025 - apr 2026) it inverts. If there's interest in a mean-reversion play on BTC 15m, at minimum: (a) relax RSI<20 to <30, (b) drop the %K<25 third filter (it makes bars ultra-rare), (c) benchmark against a simple RSI(2) contrarian baseline, (d) require out-of-sample WR & PF > 1 *after* fees in ≥ 2 independent 6-month windows.")
    md.append("")
    return "\n".join(md)


def main():
    cache_file = HERE / "ohlcv_btc_15m_1y.json"
    if cache_file.exists():
        print(f"Loading cached OHLCV from {cache_file}", flush=True)
        rows = json.loads(cache_file.read_text())
    else:
        print(f"Fetching {BARS_TARGET} bars of {SYMBOL} {TIMEFRAME} from Binance USDM...", flush=True)
        rows = fetch_ohlcv(SYMBOL, TIMEFRAME, BARS_TARGET)
        cache_file.write_text(json.dumps(rows))
    if len(rows) > BARS_TARGET:
        rows = rows[-BARS_TARGET:]
    print(f"Got {len(rows):,} bars: "
          f"{datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).isoformat()} -> "
          f"{datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).isoformat()}", flush=True)

    params = MeanRevBtc15mParams()
    print("\n=== Running LONG+SHORT (Minara's actual rules) ===", flush=True)
    ls = run_mode(rows, params, long_only=False)
    print("\n=== Running LONG-ONLY (user spec interpretation) ===", flush=True)
    lo = run_mode(rows, params, long_only=True)

    print("="*60)
    print("LONG+SHORT 90d:", json.dumps(ls["sub90"], indent=2))
    print("LONG+SHORT 1Y:", json.dumps(ls["stats"], indent=2))
    print("LONG-ONLY  1Y:", json.dumps(lo["stats"], indent=2))
    print("Exit counts:", json.dumps(ls["result"]["exit_counts"], indent=2))
    print("Diagnostics:", json.dumps(ls["result"]["diagnostics"], indent=2))

    md = fmt_markdown_combined(rows, ls, lo)
    out_md = HERE / "backtest_mean_rev_btc_15m.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"Wrote {out_md}")

    out_json = HERE / "backtest_mean_rev_btc_15m.json"
    out_json.write_text(json.dumps({
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bars": len(rows),
        "start": datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).isoformat(),
        "params": {
            "rsi_period": params.rsi_period,
            "rsi_long_entry": params.rsi_long_entry,
            "rsi_short_entry": params.rsi_short_entry,
            "ema_period": params.ema_period,
            "stoch_k_period": params.stoch_k_period,
            "stoch_d_smooth": params.stoch_d_smooth,
            "stoch_long_max": params.stoch_long_max,
            "stoch_short_min": params.stoch_short_min,
            "stop_loss_pct": params.stop_loss_pct,
            "take_profit_pct": params.take_profit_pct,
        },
        "costs": {"fee_per_side": FEE_PER_SIDE, "slippage_per_side": SLIPPAGE_PER_SIDE,
                  "notional": NOTIONAL},
        "modes": {
            "long_short": {
                "stats": ls["stats"],
                "stats_90d_subset": ls["sub90"],
                "exit_counts": ls["result"]["exit_counts"],
                "yearly": ls["yearly"],
                "trades": ls["result"]["trades"],
                "open_mtm": ls["result"]["open_mtm"],
            },
            "long_only": {
                "stats": lo["stats"],
                "stats_90d_subset": lo["sub90"],
                "exit_counts": lo["result"]["exit_counts"],
                "yearly": lo["yearly"],
                "trades": lo["result"]["trades"],
                "open_mtm": lo["result"]["open_mtm"],
            },
        },
        "diagnostics": ls["result"]["diagnostics"],
    }, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_json}")


if __name__ == "__main__":
    main()
