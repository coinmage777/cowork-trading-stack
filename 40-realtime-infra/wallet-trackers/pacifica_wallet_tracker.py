"""
Pacifica Wallet Position Tracker
================================

Polls 3 target Solana wallets on pacifica.fi every ~10 minutes, snapshots
account equity + per-symbol positions + recent fills + fee tier, and emits
strategy-change ALERTS (new symbol, position flip, size doubling, tier
change, dormant wallet waking up).

Read-only. No orders placed. No private keys needed. Pacifica's REST API
is public for account-scoped queries.

Targets (from prior reverse-engineering, see CLAUDE.md Projects):
  W1 4TYE...kYLZ   - ~$244K, tier 1, Directional Grid + Funding Harvester
  W2 E8j5...WCiZ   - ~$116K, tier 4, 7-market concentrated
  W3 531e...TXTGG  - ~$126K, tier 0 DORMANT (first fill = new strategy launch)

Journal : data/pacifica_wallet_journal.jsonl
Log file: logs/pacifica_wallet_tracker.log

Telegram: uses TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env vars; no-op if unset.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
LOG_DIR = HERE / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
JOURNAL_PATH = DATA_DIR / "pacifica_wallet_journal.jsonl"
LOG_PATH = LOG_DIR / "pacifica_wallet_tracker.log"

API_BASE = "https://api.pacifica.fi/api/v1"
POLL_INTERVAL_SEC = int(os.environ.get("PACIFICA_TRACKER_POLL_SEC", str(10 * 60)))
HTTP_TIMEOUT = 15
USER_AGENT = "pacifica-wallet-tracker/1.0 (+coinmage)"

# Alert thresholds
SIZE_DOUBLE_WINDOW_SEC = 3600          # size doubled within 1h -> ALERT
SIZE_DOUBLE_MULT = 2.0
DORMANT_LOOKBACK_SEC = 7 * 24 * 3600   # last fill older than 7d = dormant baseline

TARGETS: list[dict[str, Any]] = [
    {
        "label": "W1_grid_harvester",
        "address": "4TYEjn9PSpxoBNBXufeuNDRbytzvyyZtEUgXYSk8kYLZ",
        "notes": "tier 1 VIP, Directional Grid + Funding Harvester, 40 markets",
    },
    {
        "label": "W2_concentrated",
        "address": "E8j5xSbGXEWtj7BQobPtiMAfh7CpqR8t1tXX7qtAWCiZ",
        "notes": "tier 4, 7 markets concentrated, likely same operator as W1",
    },
    {
        "label": "W3_dormant_tier0",
        "address": "531euoNtZMvciBcKPBvYgFJoWnUvtu4PjasDhbTTXTGG",
        "notes": "tier 0 DORMANT, first new fill = BIG ALERT (strategy launch)",
    },
]

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("pacifica_wallet_tracker")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
if not logger.handlers:
    _fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    logger.addHandler(_fh)
    logger.addHandler(_sh)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _http_get_json(path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict]:
    """GET against Pacifica; returns parsed JSON dict or None on failure."""
    if params:
        from urllib.parse import urlencode
        qs = urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{API_BASE}{path}?{qs}"
    else:
        url = f"{API_BASE}{path}"
    req = _urlreq.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(3):
        try:
            with _urlreq.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                raw = r.read()
            return json.loads(raw)
        except HTTPError as e:
            if e.code == 404:
                logger.warning("404 %s", url)
                return None
            logger.warning("HTTP %s on %s (attempt %d)", e.code, url, attempt + 1)
        except (URLError, TimeoutError, OSError) as e:
            logger.warning("net err %s on %s (attempt %d)", e, url, attempt + 1)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("json decode err %s on %s", e, url)
            return None
        time.sleep(2 ** attempt)
    return None


def _f(x: Any) -> float:
    """Safe float parse, returns 0.0 on junk."""
    try:
        if x is None:
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Snapshot + fetch
# ---------------------------------------------------------------------------
@dataclass
class WalletSnapshot:
    label: str
    address: str
    ts: int                                   # unix ms
    ok: bool
    equity: float = 0.0
    balance: float = 0.0
    fee_level: int = -1
    taker_fee: float = 0.0
    maker_fee: float = 0.0
    positions_count: int = 0
    orders_count: int = 0
    total_margin_used: float = 0.0
    # per-symbol: {symbol: {side, amount, entry_price, signed_amount}}
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_fill_ts: int = 0                     # unix ms of most recent fill seen
    recent_fill_symbols: list[str] = field(default_factory=list)  # since last snap
    recent_fill_count: int = 0
    error: Optional[str] = None


def fetch_wallet_snapshot(label: str, address: str, prev_last_fill_ts: int) -> WalletSnapshot:
    now_ms = int(time.time() * 1000)
    snap = WalletSnapshot(label=label, address=address, ts=now_ms, ok=False)

    acct = _http_get_json("/account", {"account": address})
    if not acct or not acct.get("success"):
        snap.error = f"account fetch failed: {acct}"
        return snap
    a = acct.get("data") or {}
    snap.equity = _f(a.get("account_equity"))
    snap.balance = _f(a.get("balance"))
    snap.fee_level = int(a.get("fee_level") or 0)
    snap.taker_fee = _f(a.get("taker_fee"))
    snap.maker_fee = _f(a.get("maker_fee"))
    snap.positions_count = int(a.get("positions_count") or 0)
    snap.orders_count = int(a.get("orders_count") or 0)
    snap.total_margin_used = _f(a.get("total_margin_used"))

    pos = _http_get_json("/positions", {"account": address})
    if pos and pos.get("success"):
        for p in pos.get("data") or []:
            sym = p.get("symbol")
            if not sym:
                continue
            side = p.get("side")  # bid=long, ask=short
            amt = _f(p.get("amount"))
            signed = amt if side == "bid" else -amt
            snap.positions[sym] = {
                "side": side,
                "amount": amt,
                "entry_price": _f(p.get("entry_price")),
                "signed_amount": signed,
                "funding": _f(p.get("funding")),
                "updated_at": int(p.get("updated_at") or 0),
            }

    # fills: Pacifica calls it /positions/history
    fills = _http_get_json("/positions/history", {"account": address, "limit": 100})
    if fills and fills.get("success"):
        rows = fills.get("data") or []
        # rows sorted newest-first (confirmed via probe)
        if rows:
            snap.last_fill_ts = int(rows[0].get("created_at") or 0)
        newer = [r for r in rows if int(r.get("created_at") or 0) > prev_last_fill_ts]
        snap.recent_fill_count = len(newer)
        syms_seen: list[str] = []
        for r in newer:
            s = r.get("symbol")
            if s and s not in syms_seen:
                syms_seen.append(s)
        snap.recent_fill_symbols = syms_seen

    snap.ok = True
    return snap


# ---------------------------------------------------------------------------
# Diffing / alerts
# ---------------------------------------------------------------------------
def diff_for_alerts(prev: Optional[dict], curr: WalletSnapshot) -> list[dict]:
    """
    prev: previous *snapshot dict* loaded from journal (or None if first run).
    curr: current WalletSnapshot.
    Returns a list of alert dicts {severity, kind, msg, ...}.
    """
    alerts: list[dict] = []
    label = curr.label

    if prev is None:
        # first sighting -- not really an alert, informational only
        alerts.append({
            "severity": "info",
            "kind": "first_observation",
            "msg": f"{label} first tracked. equity=${curr.equity:,.0f} tier={curr.fee_level} "
                   f"positions={len(curr.positions)}",
        })
        # dormant check: if label says dormant and there's *recent* fill activity,
        # flag it (covers the case where prev is None but wallet already active)
        now_ms = curr.ts
        if "dormant" in label.lower() and curr.last_fill_ts:
            age_sec = max(0, (now_ms - curr.last_fill_ts) / 1000)
            if age_sec < DORMANT_LOOKBACK_SEC:
                alerts.append({
                    "severity": "big",
                    "kind": "dormant_wallet_active",
                    "msg": f"BIG: {label} last fill {age_sec/3600:.1f}h ago (<7d)"
                           f" -- dormant baseline broken",
                })
        return alerts

    # ---- fee tier changed
    prev_tier = int(prev.get("fee_level", -1))
    if prev_tier != curr.fee_level and prev_tier != -1:
        alerts.append({
            "severity": "warn",
            "kind": "fee_tier_changed",
            "msg": f"{label} fee_level {prev_tier} -> {curr.fee_level}",
        })

    # ---- equity delta (informational warning if > 10% in one poll)
    prev_eq = _f(prev.get("equity"))
    if prev_eq > 0:
        pct = (curr.equity - prev_eq) / prev_eq
        if abs(pct) >= 0.10:
            alerts.append({
                "severity": "warn",
                "kind": "equity_jump",
                "msg": f"{label} equity {prev_eq:,.0f} -> {curr.equity:,.0f} ({pct:+.1%})",
            })

    prev_pos = prev.get("positions") or {}
    curr_pos = curr.positions

    # ---- new symbol appeared
    new_syms = set(curr_pos) - set(prev_pos)
    for s in new_syms:
        p = curr_pos[s]
        alerts.append({
            "severity": "alert",
            "kind": "new_symbol",
            "msg": f"{label} NEW symbol {s} {p['side']} amt={p['amount']:g} "
                   f"entry={p['entry_price']:g}",
        })

    # ---- symbol closed
    closed_syms = set(prev_pos) - set(curr_pos)
    for s in closed_syms:
        alerts.append({
            "severity": "info",
            "kind": "symbol_closed",
            "msg": f"{label} closed {s} (prev {prev_pos[s].get('side')} "
                   f"{prev_pos[s].get('amount')})",
        })

    # ---- flip / size doubling on retained symbols
    for s in set(prev_pos) & set(curr_pos):
        pp = prev_pos[s]
        cp = curr_pos[s]
        prev_signed = _f(pp.get("signed_amount"))
        curr_signed = _f(cp.get("signed_amount"))
        if prev_signed * curr_signed < 0:
            alerts.append({
                "severity": "alert",
                "kind": "position_flip",
                "msg": f"{label} FLIP {s} {prev_signed:+g} -> {curr_signed:+g}",
            })
            continue
        prev_abs = abs(prev_signed)
        curr_abs = abs(curr_signed)
        if prev_abs > 0 and curr_abs / prev_abs >= SIZE_DOUBLE_MULT:
            prev_ts = int(prev.get("ts") or 0)
            window = (curr.ts - prev_ts) / 1000 if prev_ts else SIZE_DOUBLE_WINDOW_SEC + 1
            if window <= SIZE_DOUBLE_WINDOW_SEC:
                alerts.append({
                    "severity": "alert",
                    "kind": "size_doubled",
                    "msg": f"{label} {s} size {prev_abs:g} -> {curr_abs:g} "
                           f"({curr_abs/prev_abs:.2f}x in {window/60:.0f}m)",
                })

    # ---- dormant wallet waking up: first fill after prev=none/stale
    if "dormant" in label.lower():
        prev_last_fill = int(prev.get("last_fill_ts") or 0)
        if curr.recent_fill_count > 0 and curr.last_fill_ts > prev_last_fill:
            alerts.append({
                "severity": "big",
                "kind": "dormant_wallet_active",
                "msg": f"BIG: {label} DORMANT wallet just filled "
                       f"({curr.recent_fill_count} new fills on "
                       f"{','.join(curr.recent_fill_symbols) or '?'})",
            })

    return alerts


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg_send(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        from urllib.parse import urlencode
        body = urlencode({
            "chat_id": TG_CHAT,
            "text": text[:3800],
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        req = _urlreq.Request(url, data=body, method="POST")
        with _urlreq.urlopen(req, timeout=10):
            pass
    except (HTTPError, URLError, OSError) as e:
        logger.warning("telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
def snapshot_to_row(s: WalletSnapshot) -> dict:
    return {
        "event": "snapshot",
        "ts": s.ts,
        "ts_iso": datetime.fromtimestamp(s.ts / 1000, tz=timezone.utc).isoformat(),
        "label": s.label,
        "address": s.address,
        "ok": s.ok,
        "equity": round(s.equity, 4),
        "balance": round(s.balance, 4),
        "fee_level": s.fee_level,
        "taker_fee": s.taker_fee,
        "maker_fee": s.maker_fee,
        "positions_count": s.positions_count,
        "orders_count": s.orders_count,
        "total_margin_used": round(s.total_margin_used, 4),
        "positions": s.positions,
        "last_fill_ts": s.last_fill_ts,
        "recent_fill_count": s.recent_fill_count,
        "recent_fill_symbols": s.recent_fill_symbols,
        "error": s.error,
    }


def write_row(row: dict) -> None:
    JOURNAL_PATH.parent.mkdir(exist_ok=True)
    with JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_last_snapshots() -> dict[str, dict]:
    """Return dict label -> most recent snapshot row from journal."""
    out: dict[str, dict] = {}
    if not JOURNAL_PATH.exists():
        return out
    # Stream through file; for the modest sizes we expect this is fine.
    with JOURNAL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "snapshot":
                continue
            label = row.get("label")
            if label:
                out[label] = row
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_once(prev_by_label: dict[str, dict]) -> dict[str, dict]:
    """One polling pass across all target wallets. Returns new prev map."""
    new_prev: dict[str, dict] = {}
    for t in TARGETS:
        label = t["label"]
        addr = t["address"]
        prev_row = prev_by_label.get(label)
        prev_last_fill = int(prev_row.get("last_fill_ts") or 0) if prev_row else 0

        snap = fetch_wallet_snapshot(label, addr, prev_last_fill)
        row = snapshot_to_row(snap)
        write_row(row)
        new_prev[label] = row

        if not snap.ok:
            logger.warning("%s fetch failed: %s", label, snap.error)
            continue

        logger.info(
            "snap %s equity=$%.0f tier=%d pos=%d orders=%d new_fills=%d last_fill=%s",
            label, snap.equity, snap.fee_level, len(snap.positions),
            snap.orders_count, snap.recent_fill_count,
            datetime.fromtimestamp(snap.last_fill_ts / 1000, tz=timezone.utc).isoformat()
            if snap.last_fill_ts else "none",
        )

        alerts = diff_for_alerts(prev_row, snap)
        for a in alerts:
            # write alert to journal
            alert_row = {
                "event": "alert",
                "ts": snap.ts,
                "ts_iso": datetime.fromtimestamp(snap.ts / 1000, tz=timezone.utc).isoformat(),
                "label": label,
                "severity": a["severity"],
                "kind": a["kind"],
                "msg": a["msg"],
            }
            write_row(alert_row)
            tag = {"big": "[BIG]", "alert": "[ALERT]", "warn": "[warn]", "info": "[info]"}.get(
                a["severity"], "[?]"
            )
            log_line = f"{tag} {a['msg']}"
            if a["severity"] == "big":
                logger.error(log_line)
            elif a["severity"] == "alert":
                logger.warning(log_line)
            else:
                logger.info(log_line)
            if a["severity"] in ("alert", "big"):
                tg_send(f"Pacifica Tracker {tag}\n{a['msg']}")
    return new_prev


def main() -> int:
    logger.info(
        "pacifica_wallet_tracker starting: %d wallets, poll=%ds, journal=%s",
        len(TARGETS), POLL_INTERVAL_SEC, JOURNAL_PATH,
    )
    prev_by_label = load_last_snapshots()
    if prev_by_label:
        logger.info("restored %d prev snapshots from journal", len(prev_by_label))
    else:
        logger.info("no prior journal; cold start")

    # startup banner row for audit
    write_row({
        "event": "boot",
        "ts": int(time.time() * 1000),
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "poll_interval_sec": POLL_INTERVAL_SEC,
        "targets": [{"label": t["label"], "address": t["address"]} for t in TARGETS],
    })

    while True:
        t0 = time.time()
        try:
            prev_by_label = run_once(prev_by_label)
        except KeyboardInterrupt:
            logger.info("keyboard interrupt; exit")
            return 0
        except Exception as e:
            logger.exception("run_once crashed: %s", e)
        dt = time.time() - t0
        sleep_for = max(5.0, POLL_INTERVAL_SEC - dt)
        logger.info("cycle done in %.1fs; sleep %.0fs", dt, sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            logger.info("keyboard interrupt during sleep; exit")
            return 0


if __name__ == "__main__":
    sys.exit(main())
