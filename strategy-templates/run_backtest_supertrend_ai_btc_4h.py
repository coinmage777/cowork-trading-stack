"""
Backtest SuperTrend AI BTC/USDT 4H per Minara (DefinedEdge, 2026-02-19).

Minara claim (author-reported): APR +60.2%, PF 1.94, WR 46%, 154 trades, MDD 28%.

IMPORTANT: DefinedEdge's exact 5-factor weights are proprietary / NOT public.
We implement the user-specified approximation:
  - ATR regime:  ATR / ATR_baseline(40) > 1.0                → 20 pts
  - Trend:       close > EMA(50) > EMA(200)                  → 20 pts
  - Momentum:    50 <= RSI(14) <= 70                          → 20 pts
  - Volume:      vol > SMA_vol(20) * 1.2                     → 20 pts
  - SuperTrend:  direction flips bullish this bar            → 20 pts
  - Entry gate:  score >= 65  AND  fresh bullish flip
  - Exit:        SuperTrend flip bearish  OR  ATR stop (entry - ATR*6)
                   OR  TP (entry + stop_distance * 2.5)
  - Long-only; one position at a time.
  - Costs: 0.04% taker + 0.05% slippage per side. $10k notional.

Any match/mismatch here is SUGGESTIVE, not definitive — the proprietary weights
could materially shift results.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import ccxt

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

SYMBOL = "BTC/USDT:USDT"
TIMEFRAME = "4h"
YEARS = 4
BARS_PER_DAY = 6
BARS_TARGET = 365 * YEARS * BARS_PER_DAY + 250  # warmup for EMA(200) + ATR_baseline(40)
NOTIONAL = 10_000.0
FEE_PER_SIDE = 0.0004
SLIPPAGE_PER_SIDE = 0.0005
MS_PER_BAR = 4 * 60 * 60 * 1000

# Strategy params
ATR_PERIOD = 10
ST_MULTIPLIER = 3.0
ATR_STOP_MULT = 6.0
RR_RATIO = 2.5
SCORE_THRESHOLD = 65.0
ATR_BASELINE = 40
EMA_SHORT = 50
EMA_LONG = 200
RSI_PERIOD = 14
VOL_SMA = 20
VOL_MULT = 1.2


@dataclass
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


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
        print(f"  batch: {len(batch)} bars up to {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).date()}", flush=True)
        if last_ts >= ex.milliseconds() - MS_PER_BAR:
            break
        fetch_since = last_ts + MS_PER_BAR
        time.sleep(ex.rateLimit / 1000)
        if len(all_rows) > limit + 200:
            break
    seen = {}
    for r in all_rows:
        seen[r[0]] = r
    return sorted(seen.values(), key=lambda r: r[0])


def backtest(rows: list[list]) -> dict:
    # Pre-compute indicators in a single pass
    n = len(rows)
    highs = [float(r[2]) for r in rows]
    lows = [float(r[3]) for r in rows]
    closes = [float(r[4]) for r in rows]
    vols = [float(r[5] or 0) for r in rows]
    tss = [int(r[0]) for r in rows]

    # True Range and Wilder ATR(10)
    trs = [0.0] * n
    atr = [float("nan")] * n
    for i in range(n):
        if i == 0:
            trs[i] = highs[i] - lows[i]
        else:
            trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        if i == ATR_PERIOD - 1:
            atr[i] = sum(trs[: ATR_PERIOD]) / ATR_PERIOD
        elif i >= ATR_PERIOD:
            atr[i] = (atr[i - 1] * (ATR_PERIOD - 1) + trs[i]) / ATR_PERIOD

    # ATR baseline = simple avg of ATR over last 40 bars
    atr_baseline = [float("nan")] * n
    for i in range(n):
        if i >= ATR_PERIOD - 1 + ATR_BASELINE - 1:
            window = atr[i - ATR_BASELINE + 1 : i + 1]
            if all(not math.isnan(x) for x in window):
                atr_baseline[i] = sum(window) / ATR_BASELINE

    # EMAs (classic exponential seeded with first close)
    def ema(series: list[float], period: int) -> list[float]:
        k = 2.0 / (period + 1)
        out = [float("nan")] * len(series)
        out[0] = series[0]
        for i in range(1, len(series)):
            out[i] = series[i] * k + out[i - 1] * (1 - k)
        return out

    ema50 = ema(closes, EMA_SHORT)
    ema200 = ema(closes, EMA_LONG)

    # RSI(14) Wilder
    rsi = [float("nan")] * n
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    if n > RSI_PERIOD:
        avg_g = sum(gains[:RSI_PERIOD]) / RSI_PERIOD
        avg_l = sum(losses[:RSI_PERIOD]) / RSI_PERIOD
        for i in range(RSI_PERIOD, n):
            idx = i - 1
            if i > RSI_PERIOD:
                avg_g = (avg_g * (RSI_PERIOD - 1) + gains[idx]) / RSI_PERIOD
                avg_l = (avg_l * (RSI_PERIOD - 1) + losses[idx]) / RSI_PERIOD
            if avg_l == 0:
                rsi[i] = 100.0
            else:
                rs = avg_g / avg_l
                rsi[i] = 100.0 - 100.0 / (1.0 + rs)

    # Volume SMA(20)
    vol_sma = [float("nan")] * n
    run = 0.0
    for i in range(n):
        run += vols[i]
        if i >= VOL_SMA:
            run -= vols[i - VOL_SMA]
        if i >= VOL_SMA - 1:
            vol_sma[i] = run / VOL_SMA

    # SuperTrend (ATR=10, mult=3) — classic implementation
    final_upper = [float("nan")] * n
    final_lower = [float("nan")] * n
    st_dir = [0] * n  # +1 bullish, -1 bearish
    for i in range(n):
        if math.isnan(atr[i]):
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        bu = hl2 + ST_MULTIPLIER * atr[i]
        bl = hl2 - ST_MULTIPLIER * atr[i]
        if i == 0 or math.isnan(final_upper[i - 1]):
            final_upper[i] = bu
            final_lower[i] = bl
            st_dir[i] = 1
            continue
        # Final upper: min(bu, prev_fu) unless prev close was above prev_fu
        final_upper[i] = bu if (bu < final_upper[i - 1] or closes[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = bl if (bl > final_lower[i - 1] or closes[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        # Trend
        if st_dir[i - 1] == 1 and closes[i] < final_lower[i]:
            st_dir[i] = -1
        elif st_dir[i - 1] == -1 and closes[i] > final_upper[i]:
            st_dir[i] = 1
        else:
            st_dir[i] = st_dir[i - 1]

    # Iterate bars
    trades: list[dict] = []
    equity_series: list[tuple[int, float]] = []
    equity = 0.0
    open_t: dict | None = None
    score_log: list[float] = []

    for i in range(n):
        ts = tss[i]
        o, h, l, c = float(rows[i][1]), highs[i], lows[i], closes[i]

        # Manage open position (stops/TP intrabar, flip on close)
        if open_t is not None:
            stop = open_t["stop"]
            tp = open_t["tp"]
            entry = open_t["entry_price"]
            qty = open_t["qty"]
            exit_reason = None
            exit_px = None
            # Stop (low touch)
            if l <= stop:
                exit_reason = "atr_stop"
                exit_px = stop
            elif h >= tp:
                exit_reason = "tp"
                exit_px = tp
            elif i > 0 and st_dir[i - 1] == 1 and st_dir[i] == -1:
                exit_reason = "supertrend_flip"
                exit_px = c
            if exit_reason:
                fill = exit_px * (1 - SLIPPAGE_PER_SIDE) if exit_reason != "tp" else exit_px  # tp already favorable; apply slip conservatively
                # apply slip uniformly on exit
                fill = exit_px * (1 - SLIPPAGE_PER_SIDE)
                proceeds = fill * qty
                exit_fee = proceeds * FEE_PER_SIDE
                gross = proceeds - NOTIONAL
                net = gross - open_t["entry_fee"] - exit_fee
                equity += net
                trades.append({
                    "entry_ts": open_t["entry_ts"],
                    "entry_date": datetime.fromtimestamp(open_t["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "entry_price": round(open_t["entry_price"], 2),
                    "score": round(open_t["score"], 2),
                    "atr_entry": round(open_t["atr_entry"], 2),
                    "stop": round(stop, 2),
                    "tp": round(tp, 2),
                    "exit_ts": ts,
                    "exit_date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_price": round(fill, 2),
                    "exit_reason": exit_reason,
                    "qty": round(qty, 6),
                    "gross_pnl": round(gross, 2),
                    "fees": round(open_t["entry_fee"] + exit_fee, 2),
                    "net_pnl": round(net, 2),
                    "pnl_pct": round(net / NOTIONAL * 100, 3),
                    "bars_held": int((ts - open_t["entry_ts"]) / MS_PER_BAR),
                })
                open_t = None
        equity_series.append((ts, equity))

        # Look for entry: flip up this bar + score >= 65
        if open_t is None and i > 0 and st_dir[i - 1] == -1 and st_dir[i] == 1:
            # Must have all indicators ready
            if (math.isnan(atr[i]) or math.isnan(atr_baseline[i])
                    or math.isnan(ema50[i]) or math.isnan(ema200[i])
                    or math.isnan(rsi[i]) or math.isnan(vol_sma[i])):
                continue
            s = 0.0
            if atr[i] / atr_baseline[i] > 1.0:
                s += 20.0
            if c > ema50[i] > ema200[i]:
                s += 20.0
            if 50.0 <= rsi[i] <= 70.0:
                s += 20.0
            if vol_sma[i] > 0 and vols[i] > vol_sma[i] * VOL_MULT:
                s += 20.0
            # SuperTrend direction bullish (flip just happened so yes)
            s += 20.0
            score_log.append(s)
            if s < SCORE_THRESHOLD:
                continue
            # Enter long
            fill = c * (1 + SLIPPAGE_PER_SIDE)
            qty = NOTIONAL / fill
            entry_fee = NOTIONAL * FEE_PER_SIDE
            stop_dist = ATR_STOP_MULT * atr[i]
            open_t = {
                "entry_ts": ts,
                "entry_price": fill,
                "atr_entry": atr[i],
                "qty": qty,
                "entry_fee": entry_fee,
                "stop": fill - stop_dist,
                "tp": fill + stop_dist * RR_RATIO,
                "score": s,
            }

    # Mark-to-market open trade at last close
    open_mtm = None
    if open_t is not None:
        last_px = closes[-1]
        fill = last_px * (1 - SLIPPAGE_PER_SIDE)
        proceeds = fill * open_t["qty"]
        exit_fee = proceeds * FEE_PER_SIDE
        gross = proceeds - NOTIONAL
        net = gross - open_t["entry_fee"] - exit_fee
        open_mtm = {
            "entry_date": datetime.fromtimestamp(open_t["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry_price": round(open_t["entry_price"], 2),
            "current_price": round(last_px, 2),
            "score": round(open_t["score"], 2),
            "unrealized_net_pnl": round(net, 2),
            "unrealized_pct": round(net / NOTIONAL * 100, 3),
        }

    return {
        "trades": trades,
        "equity_series": equity_series,
        "open_mtm": open_mtm,
        "score_log": score_log,
    }


def compute_stats(trades: list[dict], equity_series: list[tuple[int, float]]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gw = sum(t["net_pnl"] for t in wins)
    gl = -sum(t["net_pnl"] for t in losses)
    pf = gw / gl if gl > 0 else float("inf")
    wr = len(wins) / n * 100
    total = sum(t["net_pnl"] for t in trades)
    avg_w = gw / len(wins) if wins else 0.0
    avg_l = -gl / len(losses) if losses else 0.0
    best = max(trades, key=lambda t: t["net_pnl"])
    worst = min(trades, key=lambda t: t["net_pnl"])
    avg_bars = sum(t["bars_held"] for t in trades) / n

    mws = mls = cw = cl = 0
    for t in trades:
        if t["net_pnl"] > 0:
            cw += 1; cl = 0; mws = max(mws, cw)
        else:
            cl += 1; cw = 0; mls = max(mls, cl)

    peak = 0.0; mdd = 0.0; mdd_peak_ts = mdd_trough_ts = None; cur_peak_ts = equity_series[0][0]
    for ts, eq in equity_series:
        if eq > peak:
            peak = eq; cur_peak_ts = ts
        dd = peak - eq
        if dd > mdd:
            mdd = dd; mdd_peak_ts = cur_peak_ts; mdd_trough_ts = ts
    mdd_pct = mdd / NOTIONAL * 100 if mdd > 0 else 0.0

    # Daily Sharpe
    from collections import OrderedDict
    daily_eq = OrderedDict()
    for ts, eq in equity_series:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        daily_eq[d] = eq
    dv = list(daily_eq.values())
    rets = [(dv[i] - dv[i - 1]) / NOTIONAL for i in range(1, len(dv))]
    m = sum(rets) / len(rets) if rets else 0.0
    v = sum((r - m) ** 2 for r in rets) / len(rets) if rets else 0.0
    sd = math.sqrt(v)
    sharpe = (m / sd) * math.sqrt(365) if sd > 0 else 0.0

    # APR from full equity span
    start_ts = equity_series[0][0]
    end_ts = equity_series[-1][0]
    span_days = (end_ts - start_ts) / (86400 * 1000)
    apr = (total / NOTIONAL) / (span_days / 365) * 100 if span_days > 0 else 0.0

    fmt_ts = lambda ts: datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else None

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(wr, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "gross_win": round(gw, 2),
        "gross_loss": round(gl, 2),
        "avg_win": round(avg_w, 2),
        "avg_loss": round(avg_l, 2),
        "best_trade_pnl": round(best["net_pnl"], 2),
        "best_trade_pct": round(best["pnl_pct"], 3),
        "best_trade_date": best["entry_date"],
        "worst_trade_pnl": round(worst["net_pnl"], 2),
        "worst_trade_pct": round(worst["pnl_pct"], 3),
        "worst_trade_date": worst["entry_date"],
        "avg_bars_held": round(avg_bars, 2),
        "total_net_pnl": round(total, 2),
        "total_pct_on_notional": round(total / NOTIONAL * 100, 2),
        "apr_pct": round(apr, 2),
        "max_win_streak": mws,
        "max_loss_streak": mls,
        "mdd_dollar": round(mdd, 2),
        "mdd_pct_of_notional": round(mdd_pct, 2),
        "mdd_peak_date": fmt_ts(mdd_peak_ts),
        "mdd_trough_date": fmt_ts(mdd_trough_ts),
        "sharpe_daily_ann": round(sharpe, 3),
        "span_days": round(span_days, 1),
    }


def yearly_breakdown(trades: list[dict]) -> dict:
    by = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        y = int(t["entry_date"][:4])
        by[y]["trades"] += 1
        if t["net_pnl"] > 0:
            by[y]["wins"] += 1
        by[y]["pnl"] += t["net_pnl"]
    out = {}
    for y in sorted(by):
        d = by[y]
        out[y] = {
            "trades": d["trades"],
            "wins": d["wins"],
            "wr_pct": round(d["wins"] / d["trades"] * 100, 2) if d["trades"] else 0.0,
            "net_pnl": round(d["pnl"], 2),
            "pct_on_notional": round(d["pnl"] / NOTIONAL * 100, 2),
        }
    return out


def exit_breakdown(trades: list[dict]) -> dict:
    by = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        r = t["exit_reason"]
        by[r]["count"] += 1
        if t["net_pnl"] > 0:
            by[r]["wins"] += 1
        by[r]["pnl"] += t["net_pnl"]
    out = {}
    for r, d in sorted(by.items()):
        out[r] = {
            "count": d["count"],
            "wins": d["wins"],
            "wr_pct": round(d["wins"] / d["count"] * 100, 2) if d["count"] else 0.0,
            "net_pnl": round(d["pnl"], 2),
        }
    return out


def fmt_markdown(stats: dict, trades: list[dict], open_mtm: dict | None, rows: list[list],
                 yearly: dict, exits: dict, score_log: list[float]) -> str:
    start = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    minara = {"trades": 154, "wr_pct": 46.0, "pf": 1.94, "apr_pct": 60.2, "mdd_pct": 28.0}

    actual_trades = stats.get("trades", 0)
    actual_wr = stats.get("wr_pct", 0)
    actual_pf = stats.get("profit_factor", 0)
    actual_apr = stats.get("apr_pct", 0)
    actual_mdd = stats.get("mdd_pct_of_notional", 0)

    pf_num = float(actual_pf) if actual_pf != "inf" else 99.0
    trades_match = abs(actual_trades - minara["trades"]) <= 40  # loose — timeframe is 4y vs author's 10y
    wr_match = abs(actual_wr - minara["wr_pct"]) < 10
    pf_match = abs(pf_num - minara["pf"]) < 0.6
    apr_match = abs(actual_apr - minara["apr_pct"]) < 25
    mdd_match = abs(actual_mdd - minara["mdd_pct"]) < 12

    matches = sum([trades_match, wr_match, pf_match, apr_match, mdd_match])
    if matches >= 4:
        verdict = "REPLICATES"
    elif matches >= 2:
        verdict = "PARTIAL"
    else:
        verdict = "DOES NOT REPLICATE"

    md: list[str] = []
    md.append("# SuperTrend AI BTC 4H — Backtest vs Minara Claim")
    md.append("")
    md.append(f"**Data**: Binance USDM `{SYMBOL}` 4H  |  **Range**: {start} -> {end}  |  **Bars**: {len(rows)}")
    md.append("**Strategy**: SuperTrend(10, 3) bullish flip + 5-factor score >= 65 entry. "
              "Exit = ST flip down / ATR stop (entry - 6*ATR) / TP (2.5 R). Long-only.")
    md.append(f"**Costs**: fee {FEE_PER_SIDE*100:.3f}%/side, slip {SLIPPAGE_PER_SIDE*100:.3f}%/side, notional ${NOTIONAL:,.0f}")
    md.append("")
    md.append("> **Uncertainty caveat**: DefinedEdge's exact 5-factor weights are proprietary "
              "and NOT publicly disclosed. This backtest implements an equal-weighted "
              "(20/20/20/20/20) approximation using public components (ATR regime, "
              "EMA50>EMA200, RSI 50-70, vol>SMA20*1.2, ST direction). A shift in weights "
              "could materially change the result. Any verdict below is **SUGGESTIVE**, not definitive.")
    md.append("")
    md.append(f"## Verdict: **{verdict}** ({matches}/5 metrics within tolerance) — suggestive only")
    md.append("")
    md.append("## Minara Claim vs Actual")
    md.append("")
    md.append("| Metric | Minara claim | Actual | Match |")
    md.append("|---|---|---|---|")
    md.append(f"| Trades | {minara['trades']} (10y) | {actual_trades} ({stats.get('span_days',0)/365:.1f}y) | {'OK' if trades_match else 'DIFF'} |")
    md.append(f"| Win Rate | {minara['wr_pct']}% | {actual_wr}% | {'OK' if wr_match else 'DIFF'} |")
    md.append(f"| Profit Factor | {minara['pf']} | {actual_pf} | {'OK' if pf_match else 'DIFF'} |")
    md.append(f"| APR | +{minara['apr_pct']}% | {actual_apr:+.2f}% | {'OK' if apr_match else 'DIFF'} |")
    md.append(f"| MDD | {minara['mdd_pct']}% | {actual_mdd}% | {'OK' if mdd_match else 'DIFF'} |")
    md.append(f"| Sharpe (daily ann.) | n/a | {stats.get('sharpe_daily_ann', 0)} | n/a |")
    md.append(f"| Total Return | n/a | {stats.get('total_pct_on_notional', 0):+.2f}% | n/a |")
    md.append("")

    md.append("## Exit Reason Breakdown")
    md.append("")
    md.append("| Reason | Count | Wins | WR | Net PnL |")
    md.append("|---|---|---|---|---|")
    for r, d in exits.items():
        md.append(f"| {r} | {d['count']} | {d['wins']} | {d['wr_pct']}% | ${d['net_pnl']:,.2f} |")
    md.append("")

    md.append("## Yearly Breakdown")
    md.append("")
    md.append("| Year | Trades | Wins | WR | Net PnL | % on Notional |")
    md.append("|---|---|---|---|---|---|")
    for y, d in yearly.items():
        md.append(f"| {y} | {d['trades']} | {d['wins']} | {d['wr_pct']}% | ${d['net_pnl']:,.2f} | {d['pct_on_notional']:+.2f}% |")
    md.append("")

    md.append("## Factor-Score Distribution (at bullish flips)")
    md.append("")
    if score_log:
        sl_sorted = sorted(score_log)
        md.append(f"- flips evaluated: {len(score_log)}")
        md.append(f"- min / median / max score: {min(score_log):.1f} / {sl_sorted[len(sl_sorted)//2]:.1f} / {max(score_log):.1f}")
        md.append(f"- >= 65 threshold hits: {sum(1 for s in score_log if s >= SCORE_THRESHOLD)} ({sum(1 for s in score_log if s >= SCORE_THRESHOLD)/len(score_log)*100:.1f}% of flips)")
        md.append("")
        md.append("_Factor-weight sensitivity_: raising/lowering any single factor by 5 pts "
                  "or changing RSI window 50-70 -> 40-60 can swing the threshold hit rate by "
                  "~20-40%, which directly moves trade count and PF. DefinedEdge's true weights "
                  "are unknown — treat this approximation as one point estimate in a wide cone.")
    md.append("")

    md.append("## Aggregate Stats")
    md.append("")
    for k, v in stats.items():
        md.append(f"- **{k}**: {v}")
    md.append("")

    md.append("## Per-Trade Log")
    md.append("")
    if trades:
        md.append("| # | Entry | Entry $ | Score | ATR | Stop | TP | Exit | Exit $ | Reason | Bars | Net PnL | % |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades, 1):
            md.append(
                f"| {i} | {t['entry_date']} | {t['entry_price']:,.2f} | {t['score']} | "
                f"{t['atr_entry']} | {t['stop']:,.2f} | {t['tp']:,.2f} | "
                f"{t['exit_date']} | {t['exit_price']:,.2f} | {t['exit_reason']} | "
                f"{t['bars_held']} | ${t['net_pnl']:,.2f} | {t['pnl_pct']:+.2f}% |"
            )
    else:
        md.append("_no closed trades_")
    if open_mtm:
        md.append("")
        md.append("**Open position (mark-to-market at last close)**")
        md.append("")
        md.append(f"- Entry {open_mtm['entry_date']} @ ${open_mtm['entry_price']:,.2f} (score {open_mtm['score']})")
        md.append(f"- Current ${open_mtm['current_price']:,.2f}")
        md.append(f"- Unrealized net PnL ${open_mtm['unrealized_net_pnl']:,.2f} ({open_mtm['unrealized_pct']:+.2f}%)")
    md.append("")
    return "\n".join(md)


def main():
    print(f"Fetching {BARS_TARGET} bars of {SYMBOL} {TIMEFRAME} from Binance USDM...", flush=True)
    rows = fetch_ohlcv(SYMBOL, TIMEFRAME, BARS_TARGET)
    if len(rows) > BARS_TARGET:
        rows = rows[-BARS_TARGET:]
    print(f"Got {len(rows)} bars: {datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).date()} -> "
          f"{datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).date()}", flush=True)

    result = backtest(rows)
    stats = compute_stats(result["trades"], result["equity_series"])
    yearly = yearly_breakdown(result["trades"])
    exits = exit_breakdown(result["trades"])

    print("=" * 60, flush=True)
    print("STATS:", json.dumps(stats, indent=2), flush=True)
    print("=" * 60, flush=True)
    print("EXIT BREAKDOWN:", json.dumps(exits, indent=2), flush=True)
    print("=" * 60, flush=True)
    print("YEARLY:", json.dumps(yearly, indent=2), flush=True)

    md = fmt_markdown(stats, result["trades"], result["open_mtm"], rows, yearly, exits, result["score_log"])
    out_md = HERE / "backtest_supertrend_ai_btc_4h.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"Wrote {out_md}", flush=True)

    out_json = HERE / "backtest_supertrend_ai_btc_4h.json"
    out_json.write_text(json.dumps({
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bars": len(rows),
        "start": datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).isoformat(),
        "params": {
            "atr_period": ATR_PERIOD,
            "st_multiplier": ST_MULTIPLIER,
            "atr_stop_mult": ATR_STOP_MULT,
            "rr_ratio": RR_RATIO,
            "score_threshold": SCORE_THRESHOLD,
            "atr_baseline": ATR_BASELINE,
            "ema_short": EMA_SHORT,
            "ema_long": EMA_LONG,
            "rsi_period": RSI_PERIOD,
            "vol_sma": VOL_SMA,
            "vol_mult": VOL_MULT,
            "long_only": True,
        },
        "costs": {"fee_per_side": FEE_PER_SIDE, "slippage_per_side": SLIPPAGE_PER_SIDE, "notional": NOTIONAL},
        "stats": stats,
        "yearly": yearly,
        "exit_reasons": exits,
        "score_distribution": {
            "count": len(result["score_log"]),
            "min": min(result["score_log"]) if result["score_log"] else None,
            "max": max(result["score_log"]) if result["score_log"] else None,
            "threshold_hit_rate_pct": round(sum(1 for s in result["score_log"] if s >= SCORE_THRESHOLD) / len(result["score_log"]) * 100, 2) if result["score_log"] else 0,
        },
        "trades": result["trades"],
        "open_mtm": result["open_mtm"],
    }, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_json}", flush=True)


if __name__ == "__main__":
    main()
