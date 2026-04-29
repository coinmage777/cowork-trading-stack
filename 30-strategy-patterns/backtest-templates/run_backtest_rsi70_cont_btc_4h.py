"""
Backtest RSI>70 Continuation BTC/USDT 4H on Binance USDM futures.
Reproduces Minara's claim: Trades 142, WR 35.2%, Sharpe 1.85, MDD 14.8%.

Strategy: enter LONG on RSI(14) cross above 70, exit on cross back below 70.
Long-only, no SL (pure RSI-flip). ~6 years of 4H bars targeted.
"""
from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import ccxt

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from rsi70_cont_btc_4h import Bar, Rsi70ContBtc4H, Rsi70ContBtc4HParams  # noqa: E402


SYMBOL = "BTC/USDT:USDT"  # Binance USDM perp
TIMEFRAME = "4h"
YEARS = 6
BARS_PER_DAY = 6  # 24h / 4h
BARS_TARGET = 365 * YEARS * BARS_PER_DAY + 100  # ~13,240 bars w/ RSI warmup buffer
NOTIONAL = 10_000.0
FEE_PER_SIDE = 0.0004  # 0.04% taker
SLIPPAGE_PER_SIDE = 0.0005  # 0.05%
MS_PER_BAR = 4 * 60 * 60 * 1000  # 4h in ms


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
    rows = sorted(seen.values(), key=lambda r: r[0])
    return rows


def run_backtest(rows: list[list], params: Rsi70ContBtc4HParams) -> dict:
    strat = Rsi70ContBtc4H(params)
    trades: list[dict] = []
    open_trade: dict | None = None
    # Record equity after each bar for drawdown / daily sharpe calculation
    equity_series: list[tuple[int, float]] = []
    equity = 0.0

    for row in rows:
        ts, o, h, l, c, v = row
        bar = Bar(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0))
        sig = strat.on_bar(bar)
        price = bar.close

        if sig is not None:
            if sig["side"] == "buy" and open_trade is None:
                fill_price = price * (1 + SLIPPAGE_PER_SIDE)
                qty = NOTIONAL / fill_price
                entry_fee = NOTIONAL * FEE_PER_SIDE
                open_trade = {
                    "entry_ts": ts,
                    "entry_price": fill_price,
                    "entry_close": price,
                    "entry_rsi": sig.get("rsi"),
                    "qty": qty,
                    "entry_fee": entry_fee,
                }
            elif sig["side"] == "exit" and open_trade is not None:
                fill_price = price * (1 - SLIPPAGE_PER_SIDE)
                proceeds = fill_price * open_trade["qty"]
                exit_fee = proceeds * FEE_PER_SIDE
                gross_pnl = proceeds - NOTIONAL
                net_pnl = gross_pnl - open_trade["entry_fee"] - exit_fee
                pct = net_pnl / NOTIONAL * 100.0
                equity += net_pnl
                trades.append({
                    "entry_ts": open_trade["entry_ts"],
                    "entry_date": datetime.fromtimestamp(open_trade["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "entry_price": round(open_trade["entry_price"], 2),
                    "entry_rsi": round(open_trade["entry_rsi"], 2) if open_trade["entry_rsi"] else None,
                    "exit_ts": ts,
                    "exit_date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "exit_price": round(fill_price, 2),
                    "exit_rsi": round(sig.get("rsi", 0.0), 2),
                    "qty": round(open_trade["qty"], 6),
                    "gross_pnl": round(gross_pnl, 2),
                    "fees": round(open_trade["entry_fee"] + exit_fee, 2),
                    "net_pnl": round(net_pnl, 2),
                    "pnl_pct": round(pct, 3),
                    "bars_held": int((ts - open_trade["entry_ts"]) / MS_PER_BAR),
                })
                open_trade = None

        # Record equity for each bar (closed trades only; MtM of open pos not counted here)
        equity_series.append((ts, equity))

    # If still in position, mark-to-market at last close
    last_row = rows[-1]
    last_price = float(last_row[4])
    open_mtm = None
    if open_trade is not None:
        fill_price = last_price * (1 - SLIPPAGE_PER_SIDE)
        proceeds = fill_price * open_trade["qty"]
        exit_fee = proceeds * FEE_PER_SIDE
        gross_pnl = proceeds - NOTIONAL
        net_pnl = gross_pnl - open_trade["entry_fee"] - exit_fee
        open_mtm = {
            "entry_date": datetime.fromtimestamp(open_trade["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "entry_price": round(open_trade["entry_price"], 2),
            "current_price": round(last_price, 2),
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

    # MDD on bar-level equity series (closed-trade equity)
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

    # Compute daily returns from equity series for Sharpe
    # Resample to daily by taking last equity value of each day
    from collections import OrderedDict
    daily_eq = OrderedDict()
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
    }


def subset_4y_stats(trades: list[dict], equity_series: list[tuple[int, float]], end_ts: int) -> dict:
    """Recompute stats on the last 1460 days to match Minara's reported window."""
    cutoff_ts = end_ts - 1460 * 86400 * 1000
    sub = [t for t in trades if t["entry_ts"] >= cutoff_ts]
    if not sub:
        return {}
    # Re-seed equity series from 0 at cutoff for MDD on this window
    sub_eq_series: list[tuple[int, float]] = []
    cum = 0.0
    # Use entry_ts→exit_ts of trades; interpolate with flat equity between trades
    # Simpler approach: compute per-trade running equity
    peak = 0.0
    mdd = 0.0
    peak_ts = None
    trough_ts = None
    cur_peak_ts = sub[0]["entry_ts"]
    for t in sub:
        cum += t["net_pnl"]
        sub_eq_series.append((t["exit_ts"], cum))
        if cum > peak:
            peak = cum
            cur_peak_ts = t["exit_ts"]
        dd = peak - cum
        if dd > mdd:
            mdd = dd
            peak_ts = cur_peak_ts
            trough_ts = t["exit_ts"]

    n = len(sub)
    wins = [t for t in sub if t["net_pnl"] > 0]
    losses = [t for t in sub if t["net_pnl"] <= 0]
    gw = sum(t["net_pnl"] for t in wins)
    gl = -sum(t["net_pnl"] for t in losses)
    pf = gw / gl if gl > 0 else float("inf")
    wr = len(wins) / n * 100
    total = sum(t["net_pnl"] for t in sub)

    # Daily Sharpe from sub-window equity series
    from collections import OrderedDict
    daily_eq = OrderedDict()
    # Seed day before first trade at 0
    first_day = datetime.fromtimestamp(cutoff_ts / 1000, tz=timezone.utc).date()
    daily_eq[first_day] = 0.0
    for ts, eq in sub_eq_series:
        d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        daily_eq[d] = eq
    # Fill in non-trading days with prior equity
    daily_values = list(daily_eq.values())
    rets = []
    for i in range(1, len(daily_values)):
        rets.append((daily_values[i] - daily_values[i - 1]) / NOTIONAL)
    mean_r = sum(rets) / len(rets) if rets else 0
    var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets) if rets else 0
    std_r = math.sqrt(var_r)
    sharpe = (mean_r / std_r) * math.sqrt(365) if std_r > 0 else 0.0

    return {
        "window_days": 1460,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(wr, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "gross_win": round(gw, 2),
        "gross_loss": round(gl, 2),
        "total_net_pnl": round(total, 2),
        "total_pct_on_notional": round(total / NOTIONAL * 100, 2),
        "mdd_dollar": round(mdd, 2),
        "mdd_pct_of_notional": round(mdd / NOTIONAL * 100, 2),
        "mdd_peak_date": datetime.fromtimestamp(peak_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if peak_ts else None,
        "mdd_trough_date": datetime.fromtimestamp(trough_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if trough_ts else None,
        "sharpe_daily_ann": round(sharpe, 3),
    }


def yearly_breakdown(trades: list[dict]) -> dict:
    by_year: dict[int, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for t in trades:
        y = int(t["entry_date"][:4])
        by_year[y]["trades"] += 1
        if t["net_pnl"] > 0:
            by_year[y]["wins"] += 1
        by_year[y]["pnl"] += t["net_pnl"]
    out = {}
    for y in sorted(by_year):
        d = by_year[y]
        out[y] = {
            "trades": d["trades"],
            "wins": d["wins"],
            "wr_pct": round(d["wins"] / d["trades"] * 100, 2) if d["trades"] else 0.0,
            "net_pnl": round(d["pnl"], 2),
            "pct_on_notional": round(d["pnl"] / NOTIONAL * 100, 2),
        }
    return out


def fmt_markdown(stats: dict, trades: list[dict], open_mtm: dict | None, rows: list[list], yearly: dict, sub4y: dict) -> str:
    start_date = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    minara = {
        "trades": 142,
        "wr_pct": 35.2,
        "sharpe": 1.85,
        "mdd_pct": 14.8,
        "apr_pct": 24.3,
        "total_return_pct": 99.7,
    }

    # Use 4Y subset for apples-to-apples comparison against Minara's 1460-day claim
    actual_sharpe = sub4y.get("sharpe_daily_ann", 0)
    actual_trades = sub4y.get("trades", 0)
    actual_wr = sub4y.get("wr_pct", 0)
    actual_mdd = sub4y.get("mdd_pct_of_notional", 0)
    actual_ret = sub4y.get("total_pct_on_notional", 0)

    trades_match = abs(actual_trades - minara["trades"]) <= 15
    wr_match = abs(actual_wr - minara["wr_pct"]) < 8
    sharpe_match = abs(actual_sharpe - minara["sharpe"]) < 0.5
    mdd_match = abs(actual_mdd - minara["mdd_pct"]) < 5

    matches = sum([trades_match, wr_match, sharpe_match, mdd_match])
    if matches >= 3:
        verdict = "REPLICATES"
    elif matches >= 2:
        verdict = "PARTIAL"
    else:
        verdict = "DOES NOT REPLICATE"

    md = []
    md.append("# RSI>70 Continuation BTC 4H — Backtest vs Minara Claim")
    md.append("")
    md.append(f"**Data**: Binance USDM `{SYMBOL}` 4H  |  **Range**: {start_date} → {end_date}  |  **Bars**: {len(rows)}")
    md.append("**Strategy**: enter LONG on RSI(14) cross above 70, exit on cross back below 70. Long-only, no SL.")
    md.append(f"**Costs**: fee {FEE_PER_SIDE*100:.3f}%/side, slippage {SLIPPAGE_PER_SIDE*100:.3f}%/side → 0.18% round-trip drag, notional ${NOTIONAL:,.0f}")
    md.append("")
    md.append(f"## Verdict: **{verdict}** ({matches}/4 metrics within tolerance)")
    md.append("")
    md.append("## Minara Claim vs Actual (1460-day window — apples-to-apples)")
    md.append("")
    md.append(f"_Subset window: {sub4y.get('mdd_peak_date','?')[:10]} trades → last 1460 days_")
    md.append("")
    md.append("| Metric | Minara | Actual (4Y) | Match | Full (6Y) |")
    md.append("|---|---|---|---|---|")
    md.append(f"| Trades | {minara['trades']} | {actual_trades} | {'OK' if trades_match else 'DIFF'} | {stats['trades']} |")
    md.append(f"| Win Rate | {minara['wr_pct']}% | {actual_wr}% | {'OK' if wr_match else 'DIFF'} | {stats['wr_pct']}% |")
    md.append(f"| Sharpe (daily ann.) | {minara['sharpe']} | {actual_sharpe} | {'OK' if sharpe_match else 'DIFF'} | {stats['sharpe_daily_ann']} |")
    md.append(f"| MDD | {minara['mdd_pct']}% | {actual_mdd}% | {'OK' if mdd_match else 'DIFF'} | {stats['mdd_pct_of_notional']}% |")
    md.append(f"| Total Return | {minara['total_return_pct']}% | {actual_ret}% | n/a | {stats['total_pct_on_notional']}% |")
    md.append(f"| Profit Factor | n/a | {sub4y.get('profit_factor','?')} | n/a | {stats['profit_factor']} |")
    md.append("")
    md.append(f"_Per-trade Sharpe (full, alt)_: {stats['sharpe_per_trade']}")
    md.append("")
    md.append("## Yearly Breakdown")
    md.append("")
    md.append("| Year | Trades | Wins | WR | Net PnL | % on Notional |")
    md.append("|---|---|---|---|---|---|")
    for y, d in yearly.items():
        md.append(f"| {y} | {d['trades']} | {d['wins']} | {d['wr_pct']}% | ${d['net_pnl']:,.2f} | {d['pct_on_notional']:+.2f}% |")
    md.append("")
    md.append("## Aggregate Stats")
    md.append("")
    for k, v in stats.items():
        md.append(f"- **{k}**: {v}")
    md.append("")
    md.append("## Per-Trade Log")
    md.append("")
    if trades:
        md.append("| # | Entry | Entry $ | RSI | Exit | Exit $ | RSI | Bars | Net PnL | % |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades, 1):
            md.append(
                f"| {i} | {t['entry_date']} | {t['entry_price']:,.2f} | {t['entry_rsi']} | "
                f"{t['exit_date']} | {t['exit_price']:,.2f} | {t['exit_rsi']} | "
                f"{t['bars_held']} | ${t['net_pnl']:,.2f} | {t['pnl_pct']:+.2f}% |"
            )
    else:
        md.append("_no closed trades_")
    if open_mtm:
        md.append("")
        md.append("**Open position (mark-to-market at last close)**")
        md.append("")
        md.append(f"- Entry {open_mtm['entry_date']} @ ${open_mtm['entry_price']:,.2f}")
        md.append(f"- Current ${open_mtm['current_price']:,.2f}")
        md.append(f"- Unrealized net PnL ${open_mtm['unrealized_net_pnl']:,.2f} ({open_mtm['unrealized_pct']:+.2f}%)")
    md.append("")
    return "\n".join(md)


def main():
    print(f"Fetching {BARS_TARGET} bars of {SYMBOL} {TIMEFRAME} from Binance USDM...", flush=True)
    rows = fetch_ohlcv(SYMBOL, TIMEFRAME, BARS_TARGET)
    if len(rows) > BARS_TARGET:
        rows = rows[-BARS_TARGET:]
    print(f"Got {len(rows)} bars: {datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).date()} -> {datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).date()}", flush=True)

    params = Rsi70ContBtc4HParams(rsi_period=14, rsi_threshold=70.0, long_only=True)
    result = run_backtest(rows, params)
    stats = compute_stats(result["trades"], result["equity_series"])
    yearly = yearly_breakdown(result["trades"])
    end_ts = rows[-1][0]
    sub4y = subset_4y_stats(result["trades"], result["equity_series"], end_ts)

    print("=" * 60, flush=True)
    print("FULL 6Y:", json.dumps(stats, indent=2), flush=True)
    print("=" * 60, flush=True)
    print("4Y SUBSET:", json.dumps(sub4y, indent=2), flush=True)
    print("=" * 60, flush=True)
    print(f"Yearly: {json.dumps(yearly, indent=2)}", flush=True)

    md = fmt_markdown(stats, result["trades"], result["open_mtm"], rows, yearly, sub4y)
    out_md = HERE / "backtest_rsi70_cont_btc_4h.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"Wrote {out_md}", flush=True)

    out_json = HERE / "backtest_rsi70_cont_btc_4h.json"
    out_json.write_text(json.dumps({
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bars": len(rows),
        "start": datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).isoformat(),
        "params": {"rsi_period": 14, "rsi_threshold": 70.0, "long_only": True},
        "costs": {"fee_per_side": FEE_PER_SIDE, "slippage_per_side": SLIPPAGE_PER_SIDE, "notional": NOTIONAL},
        "stats": stats,
        "stats_4y_subset": sub4y,
        "yearly": yearly,
        "trades": result["trades"],
        "open_mtm": result["open_mtm"],
    }, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_json}", flush=True)


if __name__ == "__main__":
    main()
