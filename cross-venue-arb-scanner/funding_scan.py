"""펀딩레이트 스프레드 스캐너 — 라이브 전환 전 기회 확인.

funding_rates.db에서 최근 24h 거래소 간 펀딩 스프레드를 분석하여
수익 기회 빈도 + 평균 스프레드 + 활용 가능 자본 권장량 리포트.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass


def analyze():
    db = ROOT / "funding_rates.db"
    if not db.exists():
        return {"error": "funding_rates.db 없음 — funding_collector 먼저 실행"}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # 스키마 확인
    cols = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = [c["name"] for c in cols]
    out = {"tables": tables}

    if "funding_rates" in tables:
        # 최근 24h 스프레드 기회
        try:
            rows = conn.execute("""
                SELECT exchange, symbol, rate, timestamp
                FROM funding_rates
                WHERE timestamp > datetime('now', '-24 hours')
                ORDER BY timestamp DESC
                LIMIT 10000
            """).fetchall()
            by_symbol = defaultdict(dict)  # symbol -> {exchange: rate}
            for r in rows:
                by_symbol[r["symbol"]][r["exchange"]] = r["rate"]
            spreads = []
            for sym, rates in by_symbol.items():
                if len(rates) < 2:
                    continue
                vals = sorted(rates.values())
                spread = vals[-1] - vals[0]
                if spread > 0.0005:  # 0.05% 이상
                    spreads.append((sym, spread, rates))
            spreads.sort(key=lambda x: -x[1])
            out["top_spreads"] = spreads[:10]
            out["total_opportunities_24h"] = len(spreads)
        except Exception as e:
            out["spread_err"] = str(e)
    return out


def format_report(data: dict, with_html: bool = False):
    B = ("<b>", "</b>") if with_html else ("", "")
    C = ("<code>", "</code>") if with_html else ("", "")
    lines = [f"{B[0]}💱 펀딩 스프레드 스캔 (24h){B[1]}", ""]
    if "error" in data:
        lines.append(f"⚠ {data['error']}")
        return "\n".join(lines)
    opps = data.get("total_opportunities_24h", 0)
    lines.append(f"0.05%+ 스프레드 기회: <b>{opps}</b>건")
    if opps == 0:
        lines.append("→ 현재 아비트라지 여지 낮음")
    top = data.get("top_spreads", [])
    if top:
        lines.append("")
        lines.append(f"{B[0]}TOP 스프레드{B[1]}")
        for sym, spread, rates in top[:5]:
            lines.append(f"  {sym}: {spread*100:.4f}% spread")
            for ex, r in sorted(rates.items(), key=lambda x: x[1])[:3]:
                lines.append(f"    {ex}: {r*100:+.4f}%")
    lines.append("")
    lines.append(f"{B[0]}라이브 전환 가이드{B[1]}")
    lines.append("  1. config.yaml funding_arb.mode: paper → live")
    lines.append("  2. 자본 분리 필수: 기존 페어 트레이딩과 별도 지갑 권장")
    lines.append("  3. 최소 $500 per pair (HL + 바이낸스 헤지)")
    lines.append("  4. 펀딩 수수료 타이밍 (8h/4h) 맞춰 진입/청산")
    return "\n".join(lines)


async def send_telegram(msg: str):
    import aiohttp
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print(msg)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        await s.post(url, json={"chat_id": chat, "text": msg[:3900], "parse_mode": "HTML"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args()
    data = analyze()
    report = format_report(data, with_html=args.telegram)
    if args.telegram:
        asyncio.run(send_telegram(report))
    else:
        print(report)


if __name__ == "__main__":
    main()
