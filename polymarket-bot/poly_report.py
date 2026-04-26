"""
Polymarket/Predict.fun 일일 성과 리포트

사용법:
  python poly_report.py              # 어제 보고
  python poly_report.py --live       # 오늘(실시간) 보고
  python poly_report.py --days 7     # 최근 7일 보고
  python poly_report.py --date 2026-04-01  # 특정 날짜

데이터 소스:
  1. balance_snapshots.json — USDC 잔고 (진실의 원천)
  2. predict_trades.json — Predict.fun 거래 기록
  3. trades_v2.db — Polymarket 거래 DB (참고용, 정확하지 않을 수 있음)
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_FILE = BASE_DIR / "balance_snapshots.json"
PREDICT_FILE = BASE_DIR / "predict_trades.json"
DB_FILE = BASE_DIR / "trades_v2.db"


def load_snapshots() -> list[dict]:
    if not SNAPSHOT_FILE.exists():
        return []
    try:
        data = json.loads(SNAPSHOT_FILE.read_text())
        return [s for s in data if s.get("usdc", 0) > 0]
    except Exception:
        # Handle truncated JSON
        raw = SNAPSHOT_FILE.read_text()
        idx = raw.rfind("}")
        if idx > 0:
            cleaned = raw[: idx + 1] + "]"
            if not cleaned.startswith("["):
                cleaned = "[" + cleaned
            data = json.loads(cleaned)
            return [s for s in data if s.get("usdc", 0) > 0]
    return []


def load_predict_trades() -> list[dict]:
    if not PREDICT_FILE.exists():
        return []
    try:
        return json.loads(PREDICT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def load_db_trades(date_str: str | None = None) -> list[dict]:
    if not DB_FILE.exists():
        return []
    try:
        db = sqlite3.connect(str(DB_FILE))
        db.row_factory = sqlite3.Row
        cur = db.cursor()
        if date_str:
            cur.execute(
                "SELECT * FROM trades WHERE timestamp LIKE ? AND status='closed' ORDER BY timestamp",
                (f"{date_str}%",),
            )
        else:
            cur.execute("SELECT * FROM trades WHERE status='closed' ORDER BY timestamp DESC LIMIT 50")
        rows = [dict(r) for r in cur.fetchall()]
        db.close()
        return rows
    except Exception:
        return []


def daily_balance_report(snapshots: list[dict], target_date: str) -> dict:
    """특정 날짜의 잔고 변화 분석"""
    day_snaps = [s for s in snapshots if s["ts"].startswith(target_date)]

    if not day_snaps:
        return {"date": target_date, "start": 0, "end": 0, "pnl": 0, "pnl_pct": 0, "snapshots": 0}

    start = day_snaps[0]["usdc"]
    end = day_snaps[-1]["usdc"]
    pnl = end - start
    pnl_pct = (pnl / start * 100) if start > 0 else 0

    return {
        "date": target_date,
        "start": start,
        "end": end,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "min": min(s["usdc"] for s in day_snaps),
        "max": max(s["usdc"] for s in day_snaps),
        "snapshots": len(day_snaps),
    }


def predict_trades_report(trades: list[dict], target_date: str | None = None) -> dict:
    """Predict.fun 거래 분석"""
    if target_date:
        trades = [t for t in trades if t.get("ts", "").startswith(target_date)]

    if not trades:
        return {"total": 0}

    wins = [t for t in trades if t.get("result") == "win"]
    losses = [t for t in trades if t.get("result") == "loss"]
    pending = [t for t in trades if t.get("result", "pending") == "pending"]

    # Asset breakdown
    by_asset = defaultdict(lambda: {"total": 0, "wins": 0, "cost": 0.0})
    for t in trades:
        asset = t.get("asset", "?")
        by_asset[asset]["total"] += 1
        if t.get("result") == "win":
            by_asset[asset]["wins"] += 1
        by_asset[asset]["cost"] += t.get("cost", 0)

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "pending": len(pending),
        "wr": len(wins) / (len(wins) + len(losses)) * 100 if (len(wins) + len(losses)) > 0 else 0,
        "total_cost": sum(t.get("cost", 0) for t in trades),
        "avg_edge": sum(t.get("edge", 0) for t in trades) / len(trades) * 100 if trades else 0,
        "by_asset": dict(by_asset),
    }


def print_report(target_date: str, snapshots: list[dict], predict_trades: list[dict], is_live: bool = False):
    """보고서 출력"""
    bal = daily_balance_report(snapshots, target_date)
    pred = predict_trades_report(predict_trades, target_date)

    label = "실시간" if is_live else "일일"
    print(f"\n{'='*60}")
    print(f"  Polymarket/Predict.fun {label} 성과 ({target_date})")
    print(f"{'='*60}")

    # Balance section
    if bal["snapshots"] > 0:
        bar = "+" * int(abs(bal["pnl_pct"])) if bal["pnl"] >= 0 else "-" * int(abs(bal["pnl_pct"]))
        print(f"\n  USDC 잔고 (balance_snapshots.json 기준):")
        print(f"    시작: ${bal['start']:.2f}  →  종료: ${bal['end']:.2f}")
        print(f"    PnL:  ${bal['pnl']:+.2f} ({bal['pnl_pct']:+.1f}%)  {bar}")
        print(f"    범위: ${bal['min']:.2f} ~ ${bal['max']:.2f}  ({bal['snapshots']} snapshots)")
    else:
        print(f"\n  USDC 잔고: 데이터 없음 ({target_date})")

    # Predict.fun section
    if pred["total"] > 0:
        print(f"\n  Predict.fun 거래:")
        print(f"    총 {pred['total']}건 | W={pred['wins']} L={pred['losses']} P={pred['pending']} | WR={pred['wr']:.0f}%")
        print(f"    총 비용: ${pred['total_cost']:.2f} | 평균 edge: {pred['avg_edge']:.1f}%")
        if pred["by_asset"]:
            print(f"    자산별:")
            for asset, data in sorted(pred["by_asset"].items()):
                awr = data["wins"] / data["total"] * 100 if data["total"] > 0 else 0
                print(f"      {asset}: {data['total']}건 W={data['wins']} WR={awr:.0f}% cost=${data['cost']:.2f}")
    else:
        print(f"\n  Predict.fun 거래: 없음")

    # DB trades section (참고용)
    db_trades = load_db_trades(target_date)
    if db_trades:
        by_strat = defaultdict(lambda: {"n": 0, "w": 0, "pnl": 0.0})
        for t in db_trades:
            s = t.get("strategy_name") or "unknown"
            by_strat[s]["n"] += 1
            if (t.get("pnl") or 0) > 0:
                by_strat[s]["w"] += 1
            by_strat[s]["pnl"] += t.get("pnl") or 0

        print(f"\n  DB 거래 (참고용, 실제 PnL과 다를 수 있음):")
        for strat, data in sorted(by_strat.items()):
            wr = data["w"] / data["n"] * 100 if data["n"] else 0
            print(f"    {strat}: {data['n']}건 WR={wr:.0f}% PnL=${data['pnl']:.2f}")

    print(f"\n{'='*60}\n")


def print_multi_day_report(days: int, snapshots: list[dict], predict_trades: list[dict]):
    """다일 추이 리포트"""
    # Get unique dates from snapshots
    all_dates = sorted(set(s["ts"][:10] for s in snapshots))
    target_dates = all_dates[-days:] if len(all_dates) >= days else all_dates

    print(f"\n{'='*60}")
    print(f"  최근 {len(target_dates)}일 추이")
    print(f"{'='*60}")

    total_pnl = 0
    win_days = 0

    for date in target_dates:
        bal = daily_balance_report(snapshots, date)
        if bal["snapshots"] == 0:
            continue
        total_pnl += bal["pnl"]
        if bal["pnl"] > 0:
            win_days += 1
        bar = "+" * min(20, int(abs(bal["pnl"]))) if bal["pnl"] >= 0 else "-" * min(20, int(abs(bal["pnl"])))
        print(f"  {date}  ${bal['pnl']:+7.2f} ({bal['pnl_pct']:+5.1f}%)  ${bal['end']:>7.2f}  {bar}")

    if target_dates:
        first_bal = daily_balance_report(snapshots, target_dates[0])
        last_bal = daily_balance_report(snapshots, target_dates[-1])
        print(f"\n  누적 PnL: ${total_pnl:+.2f}")
        print(f"  승일/패일: {win_days}W/{len(target_dates)-win_days}L")
        if first_bal["start"] > 0:
            overall_pct = (last_bal["end"] - first_bal["start"]) / first_bal["start"] * 100
            print(f"  기간 수익률: {overall_pct:+.1f}% (${first_bal['start']:.2f} → ${last_bal['end']:.2f})")

    # Predict.fun summary for period
    pred = predict_trades_report(predict_trades)
    if pred["total"] > 0:
        print(f"\n  Predict.fun 전체: {pred['total']}건 WR={pred['wr']:.0f}% cost=${pred['total_cost']:.2f}")

    print(f"\n{'='*60}\n")


def main():
    parser = argparse.ArgumentParser(description="Polymarket/Predict.fun 성과 리포트")
    parser.add_argument("--live", action="store_true", help="오늘(실시간) 보고")
    parser.add_argument("--date", type=str, help="특정 날짜 (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="최근 N일 추이")
    args = parser.parse_args()

    snapshots = load_snapshots()
    predict_trades = load_predict_trades()

    if not snapshots:
        print("balance_snapshots.json이 비어있거나 없습니다.")
        sys.exit(1)

    if args.days:
        print_multi_day_report(args.days, snapshots, predict_trades)
    elif args.live:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        print_report(today, snapshots, predict_trades, is_live=True)
    elif args.date:
        print_report(args.date, snapshots, predict_trades)
    else:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        print_report(yesterday, snapshots, predict_trades)

    # Always show current balance
    if snapshots:
        latest = snapshots[-1]
        print(f"  현재 USDC 잔고: ${latest['usdc']:.2f} (as of {latest['ts'][:19]})")


if __name__ == "__main__":
    main()
