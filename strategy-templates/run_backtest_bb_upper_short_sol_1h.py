"""
Backtest BB Upper Short +2% SOL/USDT 1H on Binance USDM futures.

Reproduces Minara's claim: Trades 49, WR 100%, MDD 36.7% (730 days).

Strategy (per Minara / dr_Ziuber):
- Bollinger Bands (20, 2.0)
- Entry: SHORT when HIGH > BB_upper * 1.02 (price breaks above upper BB by +2%)
- Exit (TP): when price drops -2% from entry
- Original Pine has NO stop loss (catastrophic tail risk)

Overlay (added for safety — NOT in original):
- Hard stop: +10% adverse move (price rises 10% above entry)
- Time stop: 24 bars (24 hours) max hold, exit at market

Notional $10,000 / fee 0.04% taker / slippage 0.05% per side.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ccxt

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))


SYMBOL = "SOL/USDT:USDT"
TIMEFRAME = "1h"
DAYS = 730
BARS_PER_DAY = 24
BARS_TARGET = DAYS * BARS_PER_DAY + 100  # warmup buffer
NOTIONAL = 10_000.0
FEE_PER_SIDE = 0.0004
SLIPPAGE_PER_SIDE = 0.0005
MS_PER_BAR = 60 * 60 * 1000  # 1h in ms

BB_PERIOD = 20
BB_STDDEV = 2.0
BREAKOUT_PCT = 0.02
TP_PCT = 0.02
HARD_STOP_PCT = 0.10
TIME_STOP_BARS = 24


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


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
            f"  batch: {len(batch)} bars up to {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).date()}",
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


def bb_upper_from_closes(closes: list[float]) -> Optional[float]:
    if len(closes) < BB_PERIOD:
        return None
    window = closes[-BB_PERIOD:]
    mean = sum(window) / BB_PERIOD
    var = sum((x - mean) ** 2 for x in window) / BB_PERIOD
    sd = math.sqrt(var)
    return mean + BB_STDDEV * sd


def run_backtest(rows: list[list]) -> dict:
    """
    Entry rule (HIGH-based, per task spec):
      On bar close, compute BB upper from closes through this bar.
      If bar.HIGH > BB_upper * (1 + BREAKOUT_PCT) AND no open position:
        → enter SHORT at bar.close (with slippage).
      Intrabar TP/SL check (next bar onward) uses bar.low / bar.high.

    Exit priority on each subsequent bar:
      1. Hard stop: bar.high >= entry * (1 + HARD_STOP_PCT)  → SL at that price
      2. Take profit: bar.low <= entry * (1 - TP_PCT)        → TP at that price
      3. Time stop: held >= TIME_STOP_BARS                    → close at bar.close

    If hard-stop and TP trigger on the SAME bar, we assume worst-case (SL first)
    because we cannot know intra-bar path without tick data. This is a
    conservative assumption for a safety overlay.
    """
    closes: list[float] = []
    trades: list[dict] = []
    open_trade: dict | None = None
    equity_series: list[tuple[int, float]] = []
    equity = 0.0

    for i, row in enumerate(rows):
        ts, o, h, l, c, v = row
        bar = Bar(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0))
        closes.append(bar.close)

        # Manage open position first (exits resolved on THIS bar after entry bar)
        if open_trade is not None and i > open_trade["entry_idx"]:
            entry = open_trade["entry_ref"]  # unadjusted reference price for TP/SL levels
            sl_level = entry * (1 + HARD_STOP_PCT)
            tp_level = entry * (1 - TP_PCT)
            bars_held = i - open_trade["entry_idx"]

            exit_reason = None
            exit_ref_price = None
            # Conservative: if both triggered on same bar, SL wins
            hit_sl = bar.high >= sl_level
            hit_tp = bar.low <= tp_level
            if hit_sl and hit_tp:
                exit_reason = "hard_stop"
                exit_ref_price = sl_level
            elif hit_sl:
                exit_reason = "hard_stop"
                exit_ref_price = sl_level
            elif hit_tp:
                exit_reason = "tp_2pct"
                exit_ref_price = tp_level
            elif bars_held >= TIME_STOP_BARS:
                exit_reason = "time_stop"
                exit_ref_price = bar.close

            if exit_reason is not None:
                # SHORT close = buy back. Slippage hurts buyer (price up).
                fill_price = exit_ref_price * (1 + SLIPPAGE_PER_SIDE)
                qty = open_trade["qty"]
                # SHORT pnl = (entry_fill - exit_fill) * qty
                gross_pnl = (open_trade["entry_fill"] - fill_price) * qty
                exit_fee = fill_price * qty * FEE_PER_SIDE
                net_pnl = gross_pnl - open_trade["entry_fee"] - exit_fee
                pct = net_pnl / NOTIONAL * 100.0
                equity += net_pnl
                trades.append({
                    "entry_ts": open_trade["entry_ts"],
                    "entry_date": datetime.fromtimestamp(open_trade["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "entry_close": round(open_trade["entry_ref"], 4),
                    "entry_fill": round(open_trade["entry_fill"], 4),
                    "entry_bb_upper": round(open_trade["bb_upper"], 4),
                    "entry_trigger": round(open_trade["trigger"], 4),
                    "entry_high": round(open_trade["entry_high"], 4),
                    "exit_ts": ts,
                    "exit_date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_reason": exit_reason,
                    "exit_ref_price": round(exit_ref_price, 4),
                    "exit_fill": round(fill_price, 4),
                    "qty": round(qty, 6),
                    "bars_held": bars_held,
                    "gross_pnl": round(gross_pnl, 2),
                    "fees": round(open_trade["entry_fee"] + exit_fee, 2),
                    "net_pnl": round(net_pnl, 2),
                    "pnl_pct": round(pct, 3),
                })
                open_trade = None

        # Entry check (only if flat, and after BB warmup)
        if open_trade is None:
            upper = bb_upper_from_closes(closes)
            if upper is not None:
                trigger = upper * (1 + BREAKOUT_PCT)
                # HIGH-based breakout trigger (per task spec)
                if bar.high > trigger:
                    # SHORT entry at bar.close. Seller receives slippage-adjusted price (down).
                    entry_ref = bar.close
                    entry_fill = entry_ref * (1 - SLIPPAGE_PER_SIDE)
                    qty = NOTIONAL / entry_fill
                    entry_fee = NOTIONAL * FEE_PER_SIDE
                    open_trade = {
                        "entry_ts": ts,
                        "entry_idx": i,
                        "entry_ref": entry_ref,       # close reference for TP/SL level math
                        "entry_fill": entry_fill,     # actual fill price (for PnL)
                        "entry_high": bar.high,
                        "bb_upper": upper,
                        "trigger": trigger,
                        "qty": qty,
                        "entry_fee": entry_fee,
                    }

        equity_series.append((ts, equity))

    # Mark-to-market final open pos
    open_mtm = None
    if open_trade is not None:
        last_price = float(rows[-1][4])
        fill_price = last_price * (1 + SLIPPAGE_PER_SIDE)
        qty = open_trade["qty"]
        gross_pnl = (open_trade["entry_fill"] - fill_price) * qty
        exit_fee = fill_price * qty * FEE_PER_SIDE
        net_pnl = gross_pnl - open_trade["entry_fee"] - exit_fee
        open_mtm = {
            "entry_date": datetime.fromtimestamp(open_trade["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry_fill": round(open_trade["entry_fill"], 4),
            "current_price": round(last_price, 4),
            "unrealized_net_pnl": round(net_pnl, 2),
            "unrealized_pct": round(net_pnl / NOTIONAL * 100, 3),
        }

    return {"trades": trades, "equity_series": equity_series, "open_mtm": open_mtm}


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

    reasons: dict[str, int] = defaultdict(int)
    reason_pnl: dict[str, float] = defaultdict(float)
    for t in trades:
        reasons[t["exit_reason"]] += 1
        reason_pnl[t["exit_reason"]] += t["net_pnl"]

    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for t in trades:
        if t["net_pnl"] > 0:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)

    # MDD on bar-level equity (closed-trade equity)
    peak = 0.0
    mdd_dollar = 0.0
    mdd_peak_ts = None
    mdd_trough_ts = None
    cur_peak_ts = equity_series[0][0] if equity_series else None
    for ts, eq in equity_series:
        if eq > peak:
            peak = eq
            cur_peak_ts = ts
        dd = peak - eq
        if dd > mdd_dollar:
            mdd_dollar = dd
            mdd_peak_ts = cur_peak_ts
            mdd_trough_ts = ts
    mdd_pct = mdd_dollar / NOTIONAL * 100 if mdd_dollar > 0 else 0.0

    # Daily Sharpe
    daily_eq: "OrderedDict[object, float]" = OrderedDict()
    for ts, eq in equity_series:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        daily_eq[d] = eq
    daily_values = list(daily_eq.values())
    daily_rets = []
    for i in range(1, len(daily_values)):
        daily_rets.append((daily_values[i] - daily_values[i - 1]) / NOTIONAL)
    mean_r = sum(daily_rets) / len(daily_rets) if daily_rets else 0.0
    var_r = sum((r - mean_r) ** 2 for r in daily_rets) / len(daily_rets) if daily_rets else 0.0
    std_r = math.sqrt(var_r)
    sharpe = (mean_r / std_r) * math.sqrt(365) if std_r > 0 else 0.0

    trade_rets = [t["pnl_pct"] / 100 for t in trades]
    m2 = sum(trade_rets) / len(trade_rets)
    v2 = sum((r - m2) ** 2 for r in trade_rets) / len(trade_rets)
    s2 = math.sqrt(v2)
    trade_sharpe = (m2 / s2) if s2 > 0 else 0.0

    def fmt_ts(ts):
        if ts is None:
            return None
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

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
        "best_trade_date": best["entry_date"],
        "worst_trade_pnl": round(worst["net_pnl"], 2),
        "worst_trade_pct": round(worst["pnl_pct"], 3),
        "worst_trade_date": worst["entry_date"],
        "avg_bars_held": round(avg_bars_held, 2),
        "total_net_pnl": round(total_pnl, 2),
        "total_pct_on_notional": round(total_pct, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "mdd_dollar": round(mdd_dollar, 2),
        "mdd_pct_of_notional": round(mdd_pct, 2),
        "mdd_peak_date": fmt_ts(mdd_peak_ts),
        "mdd_trough_date": fmt_ts(mdd_trough_ts),
        "sharpe_daily_ann": round(sharpe, 3),
        "sharpe_per_trade": round(trade_sharpe, 3),
        "exit_reason_counts": dict(reasons),
        "exit_reason_pnl": {k: round(v, 2) for k, v in reason_pnl.items()},
    }


def yearly_breakdown(trades: list[dict]) -> dict:
    by_year: dict[int, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "sl": 0, "tp": 0, "timeout": 0})
    for t in trades:
        y = int(t["entry_date"][:4])
        by_year[y]["trades"] += 1
        if t["net_pnl"] > 0:
            by_year[y]["wins"] += 1
        by_year[y]["pnl"] += t["net_pnl"]
        r = t["exit_reason"]
        if r == "hard_stop":
            by_year[y]["sl"] += 1
        elif r == "tp_2pct":
            by_year[y]["tp"] += 1
        elif r == "time_stop":
            by_year[y]["timeout"] += 1
    out = {}
    for y in sorted(by_year):
        d = by_year[y]
        out[y] = {
            "trades": d["trades"],
            "wins": d["wins"],
            "wr_pct": round(d["wins"] / d["trades"] * 100, 2) if d["trades"] else 0.0,
            "tp": d["tp"],
            "sl": d["sl"],
            "timeout": d["timeout"],
            "net_pnl": round(d["pnl"], 2),
            "pct_on_notional": round(d["pnl"] / NOTIONAL * 100, 2),
        }
    return out


def fmt_markdown(stats: dict, trades: list[dict], open_mtm: dict | None, rows: list[list], yearly: dict) -> str:
    start_date = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    days_covered = (rows[-1][0] - rows[0][0]) / 1000 / 86400

    minara = {
        "trades": 49,
        "wr_pct": 100.0,
        "mdd_pct": 36.7,
        "apr_pct": 48.1,
        "total_return_pct": 96.3,
    }

    actual = stats
    trades_match = abs(actual["trades"] - minara["trades"]) <= 15
    wr_match = abs(actual["wr_pct"] - minara["wr_pct"]) < 10
    mdd_match = abs(actual["mdd_pct_of_notional"] - minara["mdd_pct"]) < 10

    matches = sum([trades_match, wr_match, mdd_match])
    if matches >= 3:
        verdict = "REPLICATES"
    elif matches >= 2:
        verdict = "PARTIAL"
    else:
        verdict = "DOES NOT REPLICATE"

    sl_count = stats.get("exit_reason_counts", {}).get("hard_stop", 0)
    sl_pct_of_trades = (sl_count / stats["trades"] * 100) if stats["trades"] else 0
    sl_pnl = stats.get("exit_reason_pnl", {}).get("hard_stop", 0)

    # Safety verdict
    if stats["mdd_pct_of_notional"] > 40 or sl_pct_of_trades > 5:
        live_ready = "NOT READY FOR LIVE"
    else:
        live_ready = "CAUTIOUSLY LIVE-READY"

    md = []
    md.append("# BB Upper Short +2% SOL 1H — Backtest vs Minara Claim (+ 10% SL overlay)")
    md.append("")
    md.append(f"**Data**: Binance USDM `{SYMBOL}` 1H  |  **Range**: {start_date} → {end_date}  |  **Bars**: {len(rows)}  |  **Days**: {days_covered:.0f}")
    md.append("")
    md.append("**Strategy**: enter SHORT when bar HIGH > BB_upper(20,2.0) × 1.02. Exit at TP (-2% from entry), HARD STOP (+10% from entry), or TIME STOP (24 bars).")
    md.append(f"**Costs**: fee {FEE_PER_SIDE*100:.3f}%/side, slippage {SLIPPAGE_PER_SIDE*100:.3f}%/side → 0.18% round-trip drag  |  notional ${NOTIONAL:,.0f}")
    md.append("**Note**: Original Minara Pine has NO stop. The 10% hard stop + 24-bar time stop are ADDED for live safety.")
    md.append("")
    md.append(f"## Verdict: **{verdict}** ({matches}/3 core metrics within tolerance)")
    md.append(f"## Live-readiness: **{live_ready}**")
    md.append("")
    md.append("## Minara Claim vs Actual (side-by-side)")
    md.append("")
    md.append("| Metric | Minara (no SL) | Actual (with 10% SL overlay) | Match |")
    md.append("|---|---|---|---|")
    md.append(f"| Trades | {minara['trades']} | {actual['trades']} | {'OK' if trades_match else 'DIFF'} |")
    md.append(f"| Win Rate | {minara['wr_pct']}% | {actual['wr_pct']}% | {'OK' if wr_match else 'DIFF'} |")
    md.append(f"| MDD | {minara['mdd_pct']}% | {actual['mdd_pct_of_notional']}% | {'OK' if mdd_match else 'DIFF'} |")
    md.append(f"| Total Return | {minara['total_return_pct']}% | {actual['total_pct_on_notional']}% | n/a |")
    md.append(f"| Profit Factor | n/a | {actual['profit_factor']} | n/a |")
    md.append(f"| Sharpe (daily ann.) | n/a | {actual['sharpe_daily_ann']} | n/a |")
    md.append(f"| Avg bars held | n/a | {actual['avg_bars_held']} | n/a |")
    md.append("")
    md.append("## Exit Reason Breakdown")
    md.append("")
    md.append("| Reason | Count | % of trades | Net PnL |")
    md.append("|---|---|---|---|")
    total_trades = stats["trades"]
    for r in ["tp_2pct", "hard_stop", "time_stop"]:
        cnt = stats.get("exit_reason_counts", {}).get(r, 0)
        pnl = stats.get("exit_reason_pnl", {}).get(r, 0)
        pct = cnt / total_trades * 100 if total_trades else 0
        md.append(f"| {r} | {cnt} | {pct:.1f}% | ${pnl:,.2f} |")
    md.append("")
    md.append("## 10% Hard Stop Diagnostic (critical safety check)")
    md.append("")
    md.append(f"- **SL hits**: {sl_count} / {total_trades} trades ({sl_pct_of_trades:.1f}%)")
    md.append(f"- **Capital lost on SL**: ${sl_pnl:,.2f}")
    if sl_count > 0:
        sl_trades = [t for t in trades if t["exit_reason"] == "hard_stop"]
        avg_sl_loss = sum(t["net_pnl"] for t in sl_trades) / len(sl_trades)
        md.append(f"- **Avg loss per SL**: ${avg_sl_loss:,.2f} ({avg_sl_loss/NOTIONAL*100:.2f}% of notional)")
        md.append("")
        md.append("**SL-hit trades**:")
        md.append("")
        md.append("| # | Entry | Entry $ | Exit | Exit $ | Bars | Net PnL |")
        md.append("|---|---|---|---|---|---|---|")
        for i, t in enumerate(sl_trades, 1):
            md.append(
                f"| {i} | {t['entry_date']} | {t['entry_fill']:.4f} | {t['exit_date']} | {t['exit_fill']:.4f} | {t['bars_held']} | ${t['net_pnl']:,.2f} |"
            )
    else:
        md.append("- SL never hit in this dataset.")
    md.append("")
    md.append("## Yearly Breakdown")
    md.append("")
    md.append("| Year | Trades | Wins | WR | TP | SL | Timeout | Net PnL | % on Notional |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for y, d in yearly.items():
        md.append(
            f"| {y} | {d['trades']} | {d['wins']} | {d['wr_pct']}% | {d['tp']} | {d['sl']} | {d['timeout']} | ${d['net_pnl']:,.2f} | {d['pct_on_notional']:+.2f}% |"
        )
    md.append("")
    md.append("## Aggregate Stats")
    md.append("")
    for k, v in stats.items():
        md.append(f"- **{k}**: {v}")
    md.append("")
    md.append("## Per-Trade Log")
    md.append("")
    if trades:
        md.append("| # | Entry | Entry fill | BB upper | Trigger | Entry HIGH | Exit | Exit fill | Reason | Bars | Net PnL | % |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades, 1):
            md.append(
                f"| {i} | {t['entry_date']} | {t['entry_fill']:.4f} | {t['entry_bb_upper']:.4f} | {t['entry_trigger']:.4f} | "
                f"{t['entry_high']:.4f} | {t['exit_date']} | {t['exit_fill']:.4f} | {t['exit_reason']} | "
                f"{t['bars_held']} | ${t['net_pnl']:,.2f} | {t['pnl_pct']:+.2f}% |"
            )
    else:
        md.append("_no closed trades_")
    if open_mtm:
        md.append("")
        md.append("**Open position (mark-to-market at last close)**")
        md.append("")
        md.append(f"- Entry {open_mtm['entry_date']} @ ${open_mtm['entry_fill']:.4f}")
        md.append(f"- Current ${open_mtm['current_price']:.4f}")
        md.append(f"- Unrealized net PnL ${open_mtm['unrealized_net_pnl']:,.2f} ({open_mtm['unrealized_pct']:+.2f}%)")
    md.append("")
    md.append("## Recommendation")
    md.append("")
    if stats["mdd_pct_of_notional"] > 40:
        md.append(f"- MDD {stats['mdd_pct_of_notional']:.1f}% exceeds 40% ceiling → **do not deploy live as-is**.")
    elif sl_pct_of_trades > 5:
        md.append(f"- SL hit rate {sl_pct_of_trades:.1f}% > 5% → overlay proves the 100% WR claim is fragile. Use smaller size and tighter stop.")
    else:
        md.append(f"- SL hit rate {sl_pct_of_trades:.1f}% within 5% safety band, MDD {stats['mdd_pct_of_notional']:.1f}% within 40%.")
        md.append("- Could test with tiny size ($500-1000), but the 10% SL overlay is non-negotiable.")
    md.append("")
    return "\n".join(md)


def main():
    print(f"Fetching {BARS_TARGET} bars of {SYMBOL} {TIMEFRAME} from Binance USDM...", flush=True)
    rows = fetch_ohlcv(SYMBOL, TIMEFRAME, BARS_TARGET)
    if len(rows) > BARS_TARGET:
        rows = rows[-BARS_TARGET:]
    print(
        f"Got {len(rows)} bars: {datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).date()} -> "
        f"{datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).date()}",
        flush=True,
    )

    result = run_backtest(rows)
    stats = compute_stats(result["trades"], result["equity_series"])
    yearly = yearly_breakdown(result["trades"])

    print("=" * 60, flush=True)
    print(json.dumps(stats, indent=2, default=str), flush=True)
    print("=" * 60, flush=True)
    print(f"Yearly: {json.dumps(yearly, indent=2)}", flush=True)

    md = fmt_markdown(stats, result["trades"], result["open_mtm"], rows, yearly)
    out_md = HERE / "backtest_bb_upper_short_sol_1h.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"Wrote {out_md}", flush=True)

    out_json = HERE / "backtest_bb_upper_short_sol_1h.json"
    out_json.write_text(json.dumps({
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bars": len(rows),
        "start": datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).isoformat(),
        "params": {
            "bb_period": BB_PERIOD,
            "bb_stddev": BB_STDDEV,
            "breakout_pct": BREAKOUT_PCT,
            "take_profit_pct": TP_PCT,
            "hard_stop_pct": HARD_STOP_PCT,
            "time_stop_bars": TIME_STOP_BARS,
        },
        "costs": {"fee_per_side": FEE_PER_SIDE, "slippage_per_side": SLIPPAGE_PER_SIDE, "notional": NOTIONAL},
        "stats": stats,
        "yearly": yearly,
        "trades": result["trades"],
        "open_mtm": result["open_mtm"],
    }, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_json}", flush=True)


if __name__ == "__main__":
    main()
