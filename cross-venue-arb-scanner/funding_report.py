"""
Funding Rate Report (v2)
========================
펀딩레이트 스프레드 분석 + 백테스트 결과 CLI 도구.

  python scripts/funding_report.py --live             # 현재 실시간
  python scripts/funding_report.py --days 7           # 최근 7일 분석
  python scripts/funding_report.py --symbol ETH       # ETH만
  python scripts/funding_report.py --min-spread 0.05  # 0.05%+ 스프레드만
  python scripts/funding_report.py --backtest         # 페이퍼 트레이드 결과
"""

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "funding_rates.db"


def get_db():
    if not DB_PATH.exists():
        print(f"DB 없음: {DB_PATH}")
        print("먼저: python -m strategies.funding_collector --config config.yaml --once")
        exit(1)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def report_live(db, symbol_filter=None):
    print("=" * 60)
    print("  실시간 펀딩레이트 스프레드")
    print("=" * 60)

    row = db.execute("SELECT MAX(timestamp) as ts FROM funding_rates").fetchone()
    if not row or not row["ts"]:
        print("  데이터 없음")
        return
    ts = row["ts"]
    print(f"  기준: {ts[:19]} UTC\n")

    where = f"AND symbol='{symbol_filter}'" if symbol_filter else ""
    rates = db.execute(f"""
        SELECT exchange, symbol, funding_rate_8h, funding_interval, source
        FROM funding_rates WHERE timestamp = ? {where}
        ORDER BY symbol, funding_rate_8h DESC
    """, (ts,)).fetchall()

    current_sym = None
    sym_rates = {}
    for r in rates:
        sym = r["symbol"]
        if sym != current_sym:
            if current_sym and sym_rates:
                _print_sym(current_sym, sym_rates)
            current_sym = sym
            sym_rates = {}
        sym_rates[r["exchange"]] = r["funding_rate_8h"]
    if current_sym and sym_rates:
        _print_sym(current_sym, sym_rates)

    # Spreads
    spreads = db.execute(f"""
        SELECT symbol, max_exchange, min_exchange, spread_8h, spread_pct, actionable
        FROM funding_spreads WHERE timestamp = ? {where}
    """, (ts,)).fetchall()
    if spreads:
        print("  스프레드:")
        for s in spreads:
            icon = " ***" if s["actionable"] else ""
            print(f"    {s['symbol']:4s} {s['max_exchange']}(S) <-> {s['min_exchange']}(L) "
                  f"= {s['spread_pct']:.4f}%{icon}")


def _print_sym(symbol, rates):
    print(f"  {symbol}:")
    for ex, r8h in sorted(rates.items(), key=lambda x: -x[1]):
        bar = "+" * min(20, max(0, int(r8h * 10000))) if r8h > 0 else "-" * min(20, max(0, int(-r8h * 10000)))
        print(f"    {ex:22s} {r8h*100:+.4f}% (8h)  {bar}")
    vals = list(rates.values())
    if len(vals) >= 2:
        spread = max(vals) - min(vals)
        print(f"    {'MAX SPREAD':22s} {spread*100:.4f}%")
    print()


def report_history(db, days=1, symbol_filter=None, min_spread=0.0):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where_sym = f"AND symbol='{symbol_filter}'" if symbol_filter else ""

    print("=" * 60)
    print(f"  펀딩레이트 스프레드 분석 (최근 {days}일)")
    print("=" * 60)

    symbols = [symbol_filter] if symbol_filter else ["BTC", "ETH", "SOL"]
    for sym in symbols:
        row = db.execute(f"""
            SELECT COUNT(*) cnt, AVG(spread_8h) avg, MAX(spread_8h) mx,
                   SUM(CASE WHEN actionable THEN 1 ELSE 0 END) act
            FROM funding_spreads WHERE timestamp > ? AND symbol = ?
        """, (cutoff, sym)).fetchone()
        if not row or row["cnt"] == 0:
            continue
        print(f"\n  {sym}:")
        print(f"    수집: {row['cnt']}회")
        print(f"    평균 스프레드 (8h): {row['avg']*100:.4f}%")
        print(f"    최대 스프레드 (8h): {row['mx']*100:.4f}%")
        print(f"    수수료 감안 기회: {row['act']}회")

    # Exchange bias
    print(f"\n  거래소별 평균 펀딩 (8h):")
    bias = db.execute(f"""
        SELECT exchange, symbol, AVG(funding_rate_8h) avg, COUNT(*) cnt
        FROM funding_rates WHERE timestamp > ? {where_sym}
        GROUP BY exchange, symbol ORDER BY symbol, avg DESC
    """, (cutoff,)).fetchall()
    cur = None
    for b in bias:
        if b["symbol"] != cur:
            cur = b["symbol"]
            print(f"\n    {cur}:")
        d = "S유리" if b["avg"] > 0 else "L유리"
        print(f"      {b['exchange']:22s} {b['avg']*100:+.4f}%  ({d}, {b['cnt']}건)")

    # Best pairs
    print(f"\n  최적 페어:")
    pairs = db.execute(f"""
        SELECT symbol, max_exchange, min_exchange,
               AVG(spread_8h) avg_s, COUNT(*) cnt,
               SUM(CASE WHEN actionable THEN 1 ELSE 0 END) act
        FROM funding_spreads WHERE timestamp > ? {where_sym}
        GROUP BY symbol, max_exchange, min_exchange
        HAVING avg_s > 0 ORDER BY avg_s DESC LIMIT 10
    """, (cutoff,)).fetchall()
    for i, p in enumerate(pairs, 1):
        apr = p["avg_s"] * 100 * 365 * 3
        print(f"    {i}. {p['max_exchange']}(S) <-> {p['min_exchange']}(L) "
              f"- {p['symbol']} avg {p['avg_s']*100:.4f}%, ~{apr:.0f}% APR "
              f"({p['act']}/{p['cnt']} actionable)")


def report_backtest(db):
    print("=" * 60)
    print("  페이퍼 트레이드 결과")
    print("=" * 60)

    trades = db.execute("""
        SELECT * FROM paper_trades ORDER BY open_time
    """).fetchall()

    if not trades:
        print("  데이터 없음. 먼저: python -m strategies.funding_simulator --mode backtest")
        return

    wins = [t for t in trades if (t["est_pnl"] or 0) > 0]
    total_pnl = sum(t["est_pnl"] or 0 for t in trades)
    total_fund = sum(t["est_funding_collected"] or 0 for t in trades)
    total_fee = sum(t["est_fee_cost"] or 0 for t in trades)

    print(f"\n  총 거래: {len(trades)}회")
    print(f"  승률: {len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.1f}%)")
    print(f"  수취 펀딩: ${total_fund:.2f}")
    print(f"  수수료: -${total_fee:.2f}")
    print(f"  순 PnL: ${total_pnl:+.2f}")

    print(f"\n  최근 거래:")
    for t in trades[-10:]:
        print(f"    {t['open_time'][:16]} {t['symbol']:4s} "
              f"{t['short_exchange']}(S)<->{t['long_exchange']}(L) "
              f"${t['est_pnl'] or 0:+.2f} ({t['close_reason']})")


def report_price_gaps(db, days=1, symbol_filter=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where_sym = f"AND symbol='{symbol_filter}'" if symbol_filter else ""

    print("=" * 60)
    print(f"  선물 가격 갭 분석 (최근 {days}일)")
    print("=" * 60)

    # Latest snapshot
    row = db.execute("SELECT MAX(timestamp) ts FROM price_gaps").fetchone()
    if not row or not row["ts"]:
        print("  데이터 없음")
        return

    print(f"\n  최신 ({row['ts'][:19]} UTC):")
    for r in db.execute(f"SELECT * FROM price_gaps WHERE timestamp = ? {where_sym}", (row["ts"],)):
        all_p = json.loads(r["all_prices"]) if r["all_prices"] else {}
        icon = " ***" if r["actionable"] else ""
        print(f"    {r['symbol']:4s} {r['max_exchange']}(${r['max_price']:.2f}) <-> "
              f"{r['min_exchange']}(${r['min_price']:.2f}) gap=${r['gap_usd']:.2f} ({r['gap_pct']*100:.3f}%){icon}")

    # Stats
    symbols = [symbol_filter] if symbol_filter else ["BTC", "ETH", "SOL"]
    for sym in symbols:
        row = db.execute(f"""
            SELECT COUNT(*) cnt, AVG(gap_pct) avg_gap, MAX(gap_pct) max_gap,
                   AVG(gap_usd) avg_usd, MAX(gap_usd) max_usd,
                   SUM(CASE WHEN actionable THEN 1 ELSE 0 END) act
            FROM price_gaps WHERE timestamp > ? AND symbol = ?
        """, (cutoff, sym)).fetchone()
        if not row or row["cnt"] == 0:
            continue
        print(f"\n  {sym} ({row['cnt']}건):")
        print(f"    평균 갭: ${row['avg_usd']:.2f} ({row['avg_gap']*100:.3f}%)")
        print(f"    최대 갭: ${row['max_usd']:.2f} ({row['max_gap']*100:.3f}%)")
        print(f"    수수료 감안 기회: {row['act']}회 ({row['act']/row['cnt']*100:.0f}%)")

    # Best pair combos
    print(f"\n  최적 갭 페어:")
    pairs = db.execute(f"""
        SELECT symbol, max_exchange, min_exchange,
               AVG(gap_pct) avg_gap, MAX(gap_pct) max_gap, COUNT(*) cnt,
               SUM(CASE WHEN actionable THEN 1 ELSE 0 END) act
        FROM price_gaps WHERE timestamp > ? {where_sym}
        GROUP BY symbol, max_exchange, min_exchange
        HAVING avg_gap > 0 ORDER BY avg_gap DESC LIMIT 10
    """, (cutoff,)).fetchall()
    for i, p in enumerate(pairs, 1):
        print(f"    {i}. {p['max_exchange']}(S) <-> {p['min_exchange']}(L) "
              f"- {p['symbol']} avg gap {p['avg_gap']*100:.3f}% "
              f"(max {p['max_gap']*100:.3f}%, {p['act']}/{p['cnt']} actionable)")


def main():
    parser = argparse.ArgumentParser(description="Funding Rate Report")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--min-spread", type=float, default=0.0)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--pairs", action="store_true")
    parser.add_argument("--gaps", action="store_true")
    args = parser.parse_args()

    db = get_db()
    if args.live:
        report_live(db, args.symbol)
    elif args.backtest:
        report_backtest(db)
    elif args.gaps:
        report_price_gaps(db, args.days, args.symbol)
    else:
        report_history(db, args.days, args.symbol, args.min_spread)
    db.close()


if __name__ == "__main__":
    main()
