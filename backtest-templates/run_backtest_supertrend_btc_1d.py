"""
Backtest SuperTrend BTC/USDT 1D (ATR=10, mult=8.5) on Binance USDM futures.
Reproduces Minara's claim: PF 8.98, WR 75%, Sharpe 1.24, 4 trades over 4 years.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from supertrend_btc_1d import Bar, SuperTrendBtc1D, SuperTrendBtc1DParams  # noqa: E402


SYMBOL = "BTC/USDT:USDT"  # Binance USDM perp
TIMEFRAME = "1d"
YEARS = 4
BARS_TARGET = 365 * YEARS + 40  # buffer for ATR warmup
NOTIONAL = 10_000.0
FEE_PER_SIDE = 0.0004  # 0.04% taker (Binance USDM futures)
SLIPPAGE_PER_SIDE = 0.0005  # 0.05%


def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> list[list]:
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    # Paginate backwards: Binance USDM returns max 1500/req for 1d.
    # Compute `since` to get ~limit bars ending now.
    ms_per_day = 86400 * 1000
    since = ex.milliseconds() - ms_per_day * (limit + 5)
    all_rows: list[list] = []
    fetch_since = since
    while True:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=1500)
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts >= ex.milliseconds() - ms_per_day:
            break
        fetch_since = last_ts + ms_per_day
        time.sleep(ex.rateLimit / 1000)
        if len(all_rows) > limit + 100:
            break
    # Dedup by ts
    seen = {}
    for r in all_rows:
        seen[r[0]] = r
    rows = sorted(seen.values(), key=lambda r: r[0])
    return rows


def run_backtest(rows: list[list], params: SuperTrendBtc1DParams) -> dict:
    st = SuperTrendBtc1D(params)
    trades: list[dict] = []
    open_trade: dict | None = None
    daily_equity: list[tuple[int, float]] = []
    equity = 0.0

    for row in rows:
        ts, o, h, l, c, v = row
        bar = Bar(ts=ts, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v or 0))
        sig = st.on_bar(bar)
        if sig is None:
            daily_equity.append((ts, equity))
            continue

        price = bar.close
        if sig["side"] == "buy" and open_trade is None:
            # Slippage on entry: pay more
            fill_price = price * (1 + SLIPPAGE_PER_SIDE)
            qty = NOTIONAL / fill_price
            entry_fee = NOTIONAL * FEE_PER_SIDE
            open_trade = {
                "entry_ts": ts,
                "entry_price": fill_price,
                "entry_close": price,
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
                "entry_date": datetime.fromtimestamp(open_trade["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "entry_price": round(open_trade["entry_price"], 2),
                "exit_ts": ts,
                "exit_date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "exit_price": round(fill_price, 2),
                "qty": round(open_trade["qty"], 6),
                "gross_pnl": round(gross_pnl, 2),
                "fees": round(open_trade["entry_fee"] + exit_fee, 2),
                "net_pnl": round(net_pnl, 2),
                "pnl_pct": round(pct, 3),
                "bars_held": int((ts - open_trade["entry_ts"]) / 86400000),
            })
            open_trade = None
        daily_equity.append((ts, equity))

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
            "entry_date": datetime.fromtimestamp(open_trade["entry_ts"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "entry_price": round(open_trade["entry_price"], 2),
            "current_price": round(last_price, 2),
            "unrealized_net_pnl": round(net_pnl, 2),
            "unrealized_pct": round(net_pnl / NOTIONAL * 100, 3),
        }

    return {"trades": trades, "daily_equity": daily_equity, "open_mtm": open_mtm}


def compute_stats(trades: list[dict], daily_equity: list[tuple[int, float]]) -> dict:
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

    # Max winning / losing streak
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

    # MDD on closed-trade equity curve
    peak = 0.0
    mdd_dollar = 0.0
    for _, eq in daily_equity:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > mdd_dollar:
            mdd_dollar = dd
    mdd_pct = mdd_dollar / NOTIONAL * 100 if mdd_dollar > 0 else 0.0

    # Sharpe from daily equity returns (rough proxy). Daily delta.
    daily_rets: list[float] = []
    prev_eq = 0.0
    for _, eq in daily_equity:
        daily_rets.append(eq - prev_eq)
        prev_eq = eq
    # Normalize by notional to get % returns
    pct_rets = [r / NOTIONAL for r in daily_rets]
    mean_r = sum(pct_rets) / len(pct_rets) if pct_rets else 0
    var_r = sum((r - mean_r) ** 2 for r in pct_rets) / len(pct_rets) if pct_rets else 0
    std_r = math.sqrt(var_r)
    sharpe = (mean_r / std_r) * math.sqrt(365) if std_r > 0 else 0.0

    # Per-trade Sharpe (alternate, matches Pine-style)
    trade_rets = [t["pnl_pct"] / 100 for t in trades]
    m2 = sum(trade_rets) / len(trade_rets)
    v2 = sum((r - m2) ** 2 for r in trade_rets) / len(trade_rets)
    s2 = math.sqrt(v2)
    trade_sharpe = (m2 / s2) if s2 > 0 else 0.0

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "wr_pct": round(wr, 2),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
        "total_net_pnl": round(total_pnl, 2),
        "total_pct_on_notional": round(total_pct, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "mdd_dollar": round(mdd_dollar, 2),
        "mdd_pct_of_notional": round(mdd_pct, 2),
        "sharpe_daily_ann": round(sharpe, 3),
        "sharpe_per_trade": round(trade_sharpe, 3),
    }


def fmt_markdown(stats: dict, trades: list[dict], open_mtm: dict | None, rows: list[list]) -> str:
    start_date = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    minara = {
        "trades": 4,
        "wr_pct": 75.0,
        "profit_factor": 8.98,
        "sharpe": 1.24,
        "mdd_pct": 46.1,
        "total_return_pct": 292.4,
        "apr_pct": 35.6,
    }

    md = []
    md.append("# SuperTrend BTC 1D — Backtest vs Minara Claim")
    md.append("")
    md.append(f"**Data**: Binance USDM `{SYMBOL}` 1D  |  **Range**: {start_date} → {end_date}  |  **Bars**: {len(rows)}")
    md.append(f"**Params**: ATR=10, multiplier=8.5, long-only, exit-on-flip")
    md.append(f"**Costs**: fee {FEE_PER_SIDE*100:.3f}%/side, slippage {SLIPPAGE_PER_SIDE*100:.3f}%/side, notional ${NOTIONAL:,.0f}")
    md.append("")
    md.append("## Minara Claim vs Actual")
    md.append("")
    md.append("| Metric | Minara | Actual | Match |")
    md.append("|---|---|---|---|")
    md.append(f"| Trades | {minara['trades']} | {stats['trades']} | {'OK' if stats['trades'] == minara['trades'] else 'DIFF'} |")
    md.append(f"| Win Rate | {minara['wr_pct']}% | {stats['wr_pct']}% | {'OK' if abs(stats['wr_pct'] - minara['wr_pct']) < 10 else 'DIFF'} |")
    md.append(f"| Profit Factor | {minara['profit_factor']} | {stats['profit_factor']} | {'OK' if isinstance(stats['profit_factor'], (int, float)) and abs(stats['profit_factor'] - minara['profit_factor']) < 2 else 'DIFF'} |")
    md.append(f"| Sharpe | {minara['sharpe']} | {stats['sharpe_per_trade']} (per-trade) / {stats['sharpe_daily_ann']} (daily ann.) | n/a |")
    md.append(f"| MDD | {minara['mdd_pct']}% | {stats['mdd_pct_of_notional']}% | n/a |")
    md.append(f"| Total Return | {minara['total_return_pct']}% | {stats['total_pct_on_notional']}% | n/a |")
    md.append("")
    md.append("## Per-Trade Log")
    md.append("")
    if trades:
        md.append("| # | Entry Date | Entry $ | Exit Date | Exit $ | Bars | Gross PnL | Fees | Net PnL | % |")
        md.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, t in enumerate(trades, 1):
            md.append(
                f"| {i} | {t['entry_date']} | {t['entry_price']:,.2f} | {t['exit_date']} | {t['exit_price']:,.2f} | "
                f"{t['bars_held']} | ${t['gross_pnl']:,.2f} | ${t['fees']:,.2f} | ${t['net_pnl']:,.2f} | {t['pnl_pct']:+.2f}% |"
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
    md.append("## Aggregate Stats")
    md.append("")
    for k, v in stats.items():
        md.append(f"- **{k}**: {v}")
    md.append("")
    return "\n".join(md)


def main():
    print(f"Fetching {BARS_TARGET} bars of {SYMBOL} {TIMEFRAME} from Binance USDM...")
    rows = fetch_ohlcv(SYMBOL, TIMEFRAME, BARS_TARGET)
    # Trim to last BARS_TARGET bars
    if len(rows) > BARS_TARGET:
        rows = rows[-BARS_TARGET:]
    print(f"Got {len(rows)} bars: {datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).date()} -> {datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).date()}")

    params = SuperTrendBtc1DParams(atr_period=10, multiplier=8.5, long_only=True)
    result = run_backtest(rows, params)
    stats = compute_stats(result["trades"], result["daily_equity"])
    print(json.dumps(stats, indent=2))
    print(f"Trades: {len(result['trades'])}")
    for t in result["trades"]:
        print(f"  {t['entry_date']} -> {t['exit_date']}  net ${t['net_pnl']:+.2f}  ({t['pnl_pct']:+.2f}%)")
    if result["open_mtm"]:
        m = result["open_mtm"]
        print(f"  OPEN: {m['entry_date']} -> now  unreal ${m['unrealized_net_pnl']:+.2f}  ({m['unrealized_pct']:+.2f}%)")

    md = fmt_markdown(stats, result["trades"], result["open_mtm"], rows)
    out_md = HERE / "backtest_supertrend_btc_1d.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"Wrote {out_md}")

    out_json = HERE / "backtest_supertrend_btc_1d.json"
    out_json.write_text(json.dumps({
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "bars": len(rows),
        "start": datetime.fromtimestamp(rows[0][0]/1000, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(rows[-1][0]/1000, tz=timezone.utc).isoformat(),
        "params": {"atr_period": 10, "multiplier": 8.5},
        "costs": {"fee_per_side": FEE_PER_SIDE, "slippage_per_side": SLIPPAGE_PER_SIDE, "notional": NOTIONAL},
        "stats": stats,
        "trades": result["trades"],
        "open_mtm": result["open_mtm"],
    }, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_json}")


if __name__ == "__main__":
    main()
