"""
Perp DEX Dashboard API — read-only FastAPI wrapper for mpdex state.

Exposes 5 endpoints used by the DH_bithumb_arb dashboard "PERP DEX" tab:
  - GET /exchanges       — latest per-exchange balance, status, disabled flag
  - GET /today_pnl       — per-exchange PnL for current UTC day (equity delta or DB pnl for HIP-3)
  - GET /funding         — latest funding snapshot (top spreads)
  - GET /positions       — currently open trades with entry time + direction
  - GET /volume_farming  — daily trade count, gross volume estimate

Runs on VPS at 0.0.0.0:38743. Read-only: no writes to any mpdex data file.
Install: pip install fastapi uvicorn
Launch:  nohup .../python -m uvicorn perp_dashboard_api:app \
              --host 0.0.0.0 --port 38743 >> logs/perp_dashboard_api.log 2>&1 &
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

BASE = Path(__file__).resolve().parent
EQUITY_PATH = BASE / "equity_tracker.json"
DISABLED_PATH = BASE / "auto_disabled_exchanges.json"
TRADING_DB = BASE / "trading_data.db"
FUNDING_DB = BASE / "funding_rates.db"

# Excluded from reporting (same rules as scripts/daily_report.py)
EXCLUDE = {"bulk", "dreamcash"}
# HIP-3 exchanges: USDe collateral is shared with hyperliquid_2, so equity delta
# is unreliable (shuffle events show as deposits). Use DB pnl_usd instead.
HIP3 = {"hyena", "hyena_2", "hl_c", "hyn2"}
DEPOSIT_THRESHOLD = 30.0

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)
log = logging.getLogger("perp_dashboard_api")

app = FastAPI(title="mpdex dashboard api", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

# --- tiny in-process cache so frontend polling does not stampede disk ---
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 20.0  # seconds


def _cached(key: str, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


# --- helpers ---------------------------------------------------------------
def _safe_json(path: Path, default: Any):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("read %s failed: %s", path.name, e)
        return default


def _equity_history() -> list[dict]:
    data = _safe_json(EQUITY_PATH, [])
    return data if isinstance(data, list) else []


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _midnight_snapshot(history: list[dict], date_str: str) -> dict | None:
    """First snapshot at-or-after midnight UTC of date_str."""
    prefix = f"{date_str}T00:00"
    prev = None
    for entry in history:
        ts = entry.get("timestamp", "")
        if ts >= prefix:
            return prev or entry
        prev = entry
    return prev


def _latest_snapshot(history: list[dict]) -> dict | None:
    return history[-1] if history else None


def _db_pnl_today(exchange: str) -> tuple[float, int]:
    """Sum of closed trade pnl_usd for `exchange` on today (UTC)."""
    if not TRADING_DB.exists():
        return 0.0, 0
    try:
        conn = sqlite3.connect(str(TRADING_DB), timeout=3.0)
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(SUM(pnl_usd),0), COUNT(*) FROM trades "
            "WHERE exchange=? AND status='closed' "
            "AND substr(close_time,1,10)=?",
            (exchange, _utc_today()),
        )
        row = c.fetchone()
        conn.close()
        return float(row[0] or 0.0), int(row[1] or 0)
    except Exception as e:
        log.warning("db pnl (%s): %s", exchange, e)
        return 0.0, 0


def _detect_deposits(snaps_today: list[dict], exchange: str) -> float:
    total = 0.0
    last_valid: float | None = None
    for s in snaps_today:
        v = s.get("exchanges", {}).get(exchange, 0) or 0
        if v <= 0:
            continue
        if last_valid is not None and abs(v - last_valid) >= DEPOSIT_THRESHOLD:
            total += v - last_valid
        last_valid = v
    return total


# --- endpoints -------------------------------------------------------------
@app.get("/exchanges")
def exchanges():
    def build():
        history = _equity_history()
        latest = _latest_snapshot(history)
        disabled = _safe_json(DISABLED_PATH, {}) or {}
        if not latest:
            return {"exchanges": [], "total": 0.0, "stamp": None}

        rows: list[dict] = []
        total = 0.0
        for name, bal in (latest.get("exchanges") or {}).items():
            if name in EXCLUDE:
                continue
            is_disabled = name in disabled
            bal_f = float(bal or 0.0)
            if bal_f > 0 and not is_disabled:
                total += bal_f
            rows.append(
                {
                    "name": name,
                    "balance": round(bal_f, 2),
                    "disabled": is_disabled,
                    "disabled_reason": (disabled.get(name) or {}).get("reason")
                    if isinstance(disabled.get(name), dict)
                    else None,
                    "status": "off"
                    if is_disabled
                    else ("idle" if bal_f <= 0 else "live"),
                    "hip3": name in HIP3,
                }
            )
        rows.sort(key=lambda r: r["balance"], reverse=True)
        return {
            "exchanges": rows,
            "total": round(total, 2),
            "stamp": latest.get("timestamp"),
        }

    return _cached("exchanges", build)


@app.get("/today_pnl")
def today_pnl():
    def build():
        history = _equity_history()
        if not history:
            return {"rows": [], "total_pnl": 0.0, "winners": 0, "losers": 0}
        today = _utc_today()
        start = _midnight_snapshot(history, today)
        latest = _latest_snapshot(history)
        # snapshots during today, for deposit detection
        today_prefix = f"{today}T"
        snaps_today = [s for s in history if s.get("timestamp", "").startswith(today_prefix)]

        rows: list[dict] = []
        total = 0.0
        winners = 0
        losers = 0
        names = set()
        if start:
            names.update((start.get("exchanges") or {}).keys())
        if latest:
            names.update((latest.get("exchanges") or {}).keys())

        for name in sorted(names):
            if name in EXCLUDE:
                continue
            start_bal = float((start.get("exchanges", {}) or {}).get(name, 0) or 0)
            end_bal = float((latest.get("exchanges", {}) or {}).get(name, 0) or 0)
            deposits = _detect_deposits(snaps_today, name)

            if name in HIP3:
                db_pnl, db_trades = _db_pnl_today(name)
                pnl = db_pnl
                source = "db"
            else:
                pnl = (end_bal - start_bal) - deposits
                _, db_trades = _db_pnl_today(name)
                source = "equity"

            if pnl > 0.5:
                winners += 1
            elif pnl < -0.5:
                losers += 1
            total += pnl
            rows.append(
                {
                    "name": name,
                    "start_balance": round(start_bal, 2),
                    "end_balance": round(end_bal, 2),
                    "pnl_usd": round(pnl, 2),
                    "pnl_pct": round(100 * pnl / start_bal, 2) if start_bal > 0 else 0.0,
                    "trades": db_trades,
                    "deposits": round(deposits, 2),
                    "source": source,
                }
            )
        rows.sort(key=lambda r: r["pnl_usd"])
        return {
            "rows": rows,
            "total_pnl": round(total, 2),
            "winners": winners,
            "losers": losers,
            "date": today,
        }

    return _cached("today_pnl", build)


@app.get("/funding")
def funding():
    def build():
        if not FUNDING_DB.exists():
            return {"spreads": [], "latest_ts": None}
        try:
            conn = sqlite3.connect(str(FUNDING_DB), timeout=3.0)
            c = conn.cursor()
            # latest timestamp — then pull spreads from that batch
            c.execute("SELECT MAX(timestamp) FROM funding_spreads")
            latest_row = c.fetchone()
            latest_ts = latest_row[0] if latest_row else None
            spreads: list[dict] = []
            if latest_ts:
                c.execute(
                    "SELECT symbol, max_exchange, min_exchange, max_rate_8h, "
                    "min_rate_8h, spread_8h, spread_pct "
                    "FROM funding_spreads WHERE timestamp=? "
                    "ORDER BY ABS(spread_8h) DESC LIMIT 10",
                    (latest_ts,),
                )
                for r in c.fetchall():
                    spreads.append(
                        {
                            "symbol": r[0],
                            "max_ex": r[1],
                            "min_ex": r[2],
                            "max_rate_8h": float(r[3] or 0),
                            "min_rate_8h": float(r[4] or 0),
                            "spread_8h": float(r[5] or 0),
                            "spread_pct": float(r[6] or 0),
                        }
                    )
            conn.close()
            return {"spreads": spreads, "latest_ts": latest_ts}
        except Exception as e:
            log.warning("funding query: %s", e)
            return {"spreads": [], "latest_ts": None, "error": str(e)}

    return _cached("funding", build)


@app.get("/positions")
def positions():
    def build():
        if not TRADING_DB.exists():
            return {"open": [], "count": 0}
        try:
            conn = sqlite3.connect(str(TRADING_DB), timeout=3.0)
            c = conn.cursor()
            c.execute(
                "SELECT id, exchange, direction, coin1, coin2, entry_time, "
                "entry_count, pnl_percent, pnl_usd "
                "FROM trades WHERE status='open' ORDER BY entry_time DESC LIMIT 50"
            )
            rows = []
            for r in c.fetchall():
                entry_time = r[5]
                age_min = None
                try:
                    dt = datetime.fromisoformat(entry_time.replace(" ", "T"))
                    age_min = max(0, int((datetime.utcnow() - dt).total_seconds() // 60))
                except Exception:
                    pass
                rows.append(
                    {
                        "id": r[0],
                        "exchange": r[1],
                        "direction": r[2],
                        "coin1": r[3],
                        "coin2": r[4],
                        "entry_time": entry_time,
                        "age_min": age_min,
                        "entries": r[6],
                        "pnl_percent": float(r[7] or 0),
                        "pnl_usd": float(r[8] or 0),
                    }
                )
            conn.close()
            return {"open": rows, "count": len(rows)}
        except Exception as e:
            log.warning("positions: %s", e)
            return {"open": [], "count": 0, "error": str(e)}

    return _cached("positions", build)


@app.get("/volume_farming")
def volume_farming():
    def build():
        if not TRADING_DB.exists():
            return {"rows": [], "total_trades": 0}
        try:
            conn = sqlite3.connect(str(TRADING_DB), timeout=3.0)
            c = conn.cursor()
            today = _utc_today()
            c.execute(
                "SELECT exchange, COUNT(*), "
                "COALESCE(SUM(entry_count),0), "
                "COALESCE(SUM(pnl_usd),0) "
                "FROM trades WHERE substr(entry_time,1,10)=? "
                "GROUP BY exchange",
                (today,),
            )
            rows = []
            total_trades = 0
            total_entries = 0
            for r in c.fetchall():
                rows.append(
                    {
                        "exchange": r[0],
                        "trades": int(r[1]),
                        "entries": int(r[2]),
                        "pnl_usd": round(float(r[3]), 2),
                    }
                )
                total_trades += int(r[1])
                total_entries += int(r[2])
            rows.sort(key=lambda x: x["entries"], reverse=True)
            conn.close()
            # rough vol estimate: each entry ≈ margin*leverage both legs
            # we do not know margin per exchange exactly; report entry count instead
            return {
                "rows": rows,
                "total_trades": total_trades,
                "total_entries": total_entries,
                "date": today,
            }
        except Exception as e:
            log.warning("volume_farming: %s", e)
            return {"rows": [], "total_trades": 0, "error": str(e)}

    return _cached("volume_farming", build)


@app.get("/")
def root():
    return {
        "service": "perp_dashboard_api",
        "endpoints": [
            "/exchanges",
            "/today_pnl",
            "/funding",
            "/positions",
            "/volume_farming",
        ],
    }


@app.on_event("startup")
async def _startup():
    log.info("[perp_dashboard_api] listening :38743  base=%s", BASE)
