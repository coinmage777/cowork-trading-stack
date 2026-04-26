"""
Live PAPER trader for the RSI(14) > 70 continuation strategy on ETHUSDT 4H
(Binance USDM perp). Logs signals + daily snapshots to a JSONL journal; does
NOT place any real orders.

Loop:
  * Every ~30 min (POLL_INTERVAL_SEC), fetch fresh 4H OHLCV.
  * Only ACT on confirmed bar closes (the last fully closed bar rolled forward
    since last processing).
  * Recompute RSI(14) from scratch each poll using the fetched history — avoids
    long-lived running-avg drift across restarts.
  * Entry: prev_RSI <= 70 AND last_RSI > 70   (cross-above).
  * Exit:  prev_RSI >= 70 AND last_RSI < 70   (cross-below).
  * Position: synthetic $500 LONG notional. Tracked in journal, restored on
    startup from last journal row.
  * Once per UTC day: write a {"event":"snapshot", ...} row.

Journal: ../DH_bithumb_arb-main/strategies_minara/data/paper_rsi70_eth_journal.jsonl
Log file: ../DH_bithumb_arb-main/strategies_minara/logs/paper_rsi70_eth.log
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib import request as _urlreq
from urllib import parse as _urlparse

import ccxt

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from rsi70_cont_btc_4h import Bar, Rsi70ContBtc4H, Rsi70ContBtc4HParams  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SYMBOL = "ETH/USDT:USDT"
TIMEFRAME = "4h"
MS_PER_BAR = 4 * 60 * 60 * 1000
NOTIONAL = 500.0             # paper bet per trade
RSI_PERIOD = 14
RSI_THRESHOLD = 70.0
POLL_INTERVAL_SEC = 30 * 60  # 30 min
HIST_BARS = 500              # plenty for RSI warmup + buffer
FEE_PER_SIDE = 0.0004        # 0.04%
SLIPPAGE_PER_SIDE = 0.0005   # 0.05%

DATA_DIR = HERE / "data"
LOG_DIR = HERE / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
JOURNAL_PATH = DATA_DIR / "paper_rsi70_eth_journal.jsonl"
LOG_PATH = LOG_DIR / "paper_rsi70_eth.log"

# Telegram (optional)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("paper_rsi70_eth")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
if not logger.handlers:
    logger.addHandler(_fh)
    logger.addHandler(_sh)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@dataclass
class Position:
    active: bool = False
    side: str = "long"
    entry_price: float = 0.0
    entry_ts: int = 0
    size_usd: float = 0.0


@dataclass
class Cumulative:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    wr: Optional[float] = None

    def update_after_exit(self, pnl: float) -> None:
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.net_pnl += pnl
        self.wr = round(self.wins / self.trades * 100.0, 2) if self.trades else None


@dataclass
class State:
    position: Position = field(default_factory=Position)
    cumulative: Cumulative = field(default_factory=Cumulative)
    last_processed_bar_ts: int = 0
    last_snapshot_date: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = _urlparse.urlencode({
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        req = _urlreq.Request(url, data=data)
        _urlreq.urlopen(req, timeout=10).read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("telegram send failed: %s", exc)


def write_journal(row: dict) -> None:
    row.setdefault("ts", int(time.time()))
    JOURNAL_PATH.parent.mkdir(exist_ok=True)
    with JOURNAL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_last_journal_line() -> Optional[dict]:
    if not JOURNAL_PATH.exists():
        return None
    last: Optional[dict] = None
    try:
        with JOURNAL_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("journal read failed: %s", exc)
    return last


def restore_state() -> State:
    st = State()
    last = read_last_journal_line()
    if not last:
        return st
    pos = last.get("position") or {}
    if pos.get("active"):
        st.position = Position(
            active=True,
            side=pos.get("side", "long"),
            entry_price=float(pos.get("entry_price") or 0.0),
            entry_ts=int(pos.get("entry_ts") or 0),
            size_usd=float(pos.get("size_usd") or NOTIONAL),
        )
    cum = last.get("cumulative") or {}
    st.cumulative = Cumulative(
        trades=int(cum.get("trades") or 0),
        wins=int(cum.get("wins") or 0),
        losses=int(cum.get("losses") or 0),
        net_pnl=float(cum.get("net_pnl") or 0.0),
        wr=cum.get("wr"),
    )
    st.last_processed_bar_ts = int(last.get("bar_close_ts") or 0)
    # last_snapshot_date is a UTC date string
    st.last_snapshot_date = str(last.get("snapshot_date") or "")
    return st


def fetch_ohlcv(ex: ccxt.Exchange, bars: int) -> list[list]:
    """Fetch last `bars` 4H candles. Retries a few times."""
    last_exc: Optional[Exception] = None
    for attempt in range(5):
        try:
            return ex.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=bars)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("fetch_ohlcv attempt %d failed: %s", attempt + 1, exc)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch_ohlcv exhausted retries: {last_exc}")


def compute_rsi_series(closes: list[float], period: int) -> list[Optional[float]]:
    """Wilder's RSI — returns a list aligned with `closes`, Nones before warmup."""
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # first RSI value sits at index = period
    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def maybe_snapshot(state: State, last_close: float, last_rsi: Optional[float], bar_ts: int) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    if state.last_snapshot_date == today:
        return
    row = {
        "event": "snapshot",
        "snapshot_date": today,
        "bar_close_ts": bar_ts,
        "close": round(float(last_close), 2),
        "rsi": round(float(last_rsi), 3) if last_rsi is not None else None,
        "position": asdict(state.position),
        "cumulative": asdict(state.cumulative),
    }
    write_journal(row)
    state.last_snapshot_date = today
    logger.info("SNAPSHOT %s close=%.2f rsi=%s pos=%s cum=%s",
                today, last_close, last_rsi, state.position.active, state.cumulative.trades)


def handle_entry(state: State, bar_ts: int, close: float, rsi: float) -> None:
    fill = close * (1 + SLIPPAGE_PER_SIDE)
    state.position = Position(
        active=True,
        side="long",
        entry_price=round(fill, 2),
        entry_ts=bar_ts,
        size_usd=NOTIONAL,
    )
    row = {
        "event": "entry",
        "bar_close_ts": bar_ts,
        "close": round(close, 2),
        "rsi": round(rsi, 3),
        "position": asdict(state.position),
        "cumulative": asdict(state.cumulative),
    }
    write_journal(row)
    bar_iso = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (f"[RSI-ETH] ENTRY LONG ${NOTIONAL:.0f}\n"
           f"bar {bar_iso}  close ${close:,.2f}  rsi {rsi:.2f}")
    logger.info(msg.replace("\n", " | "))
    tg_send(msg)


def handle_exit(state: State, bar_ts: int, close: float, rsi: float) -> None:
    if not state.position.active:
        return
    fill = close * (1 - SLIPPAGE_PER_SIDE)
    qty = state.position.size_usd / state.position.entry_price if state.position.entry_price else 0.0
    proceeds = fill * qty
    entry_fee = state.position.size_usd * FEE_PER_SIDE
    exit_fee = proceeds * FEE_PER_SIDE
    gross = proceeds - state.position.size_usd
    net = gross - entry_fee - exit_fee
    pct = (net / state.position.size_usd) * 100.0 if state.position.size_usd else 0.0
    state.cumulative.update_after_exit(net)

    entry_snap = asdict(state.position)
    state.position = Position()  # flat
    row = {
        "event": "exit",
        "bar_close_ts": bar_ts,
        "close": round(close, 2),
        "rsi": round(rsi, 3),
        "trade": {
            "entry_price": entry_snap["entry_price"],
            "entry_ts": entry_snap["entry_ts"],
            "exit_price": round(fill, 2),
            "exit_ts": bar_ts,
            "size_usd": entry_snap["size_usd"],
            "gross_pnl": round(gross, 2),
            "fees": round(entry_fee + exit_fee, 2),
            "net_pnl": round(net, 2),
            "pnl_pct": round(pct, 3),
        },
        "position": asdict(state.position),
        "cumulative": asdict(state.cumulative),
    }
    write_journal(row)
    bar_iso = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = (f"[RSI-ETH] EXIT\n"
           f"bar {bar_iso}  close ${close:,.2f}  rsi {rsi:.2f}\n"
           f"net ${net:+.2f} ({pct:+.2f}%)  cum n={state.cumulative.trades} wr={state.cumulative.wr} pnl=${state.cumulative.net_pnl:.2f}")
    logger.info(msg.replace("\n", " | "))
    tg_send(msg)


def process_bar_close(state: State, rows: list[list]) -> None:
    """Act on the most recent CLOSED bar (rows[-2]) once per bar close."""
    if len(rows) < RSI_PERIOD + 3:
        logger.info("not enough bars yet (%d)", len(rows))
        return
    # Binance returns the in-progress bar last. Use the one before it.
    closed = rows[-2]
    closed_ts = int(closed[0])
    if closed_ts <= state.last_processed_bar_ts:
        return  # already processed

    closes = [float(r[4]) for r in rows[:-1]]  # include only closed bars
    rsi_series = compute_rsi_series(closes, RSI_PERIOD)
    last_rsi = rsi_series[-1]
    prev_rsi = rsi_series[-2] if len(rsi_series) >= 2 else None
    last_close = closes[-1]

    if last_rsi is None or prev_rsi is None:
        logger.info("rsi warmup incomplete, skipping bar %s",
                    datetime.fromtimestamp(closed_ts / 1000, tz=timezone.utc).isoformat())
        state.last_processed_bar_ts = closed_ts
        return

    crossed_above = prev_rsi <= RSI_THRESHOLD and last_rsi > RSI_THRESHOLD
    crossed_below = prev_rsi >= RSI_THRESHOLD and last_rsi < RSI_THRESHOLD

    bar_iso = datetime.fromtimestamp(closed_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logger.info("bar_close %s  close=%.2f  prev_rsi=%.2f  last_rsi=%.2f  pos_active=%s",
                bar_iso, last_close, prev_rsi, last_rsi, state.position.active)

    if crossed_above and not state.position.active:
        handle_entry(state, closed_ts, last_close, last_rsi)
    elif crossed_below and state.position.active:
        handle_exit(state, closed_ts, last_close, last_rsi)

    state.last_processed_bar_ts = closed_ts
    maybe_snapshot(state, last_close, last_rsi, closed_ts)


def startup_row(state: State, ccxt_version: str) -> None:
    row = {
        "event": "startup",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "rsi_period": RSI_PERIOD,
        "rsi_threshold": RSI_THRESHOLD,
        "notional_usd": NOTIONAL,
        "poll_interval_sec": POLL_INTERVAL_SEC,
        "ccxt_version": ccxt_version,
        "pid": os.getpid(),
        "position": asdict(state.position),
        "cumulative": asdict(state.cumulative),
    }
    write_journal(row)
    logger.info("startup pid=%s pos_active=%s cum=%s",
                os.getpid(), state.position.active, state.cumulative)
    tg_send(f"[RSI-ETH] startup pid={os.getpid()} pos_active={state.position.active} trades={state.cumulative.trades}")


def main() -> None:
    logger.info("paper_rsi70_eth starting - journal=%s log=%s", JOURNAL_PATH, LOG_PATH)
    state = restore_state()

    ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})
    startup_row(state, ccxt.__version__)

    # Immediate first poll so we leave at least one OHLCV-based row behind.
    first = True
    while True:
        try:
            rows = fetch_ohlcv(ex, HIST_BARS)
            if rows:
                process_bar_close(state, rows)
                if first:
                    # Always drop an initial snapshot so the journal has OHLCV evidence.
                    last_closed = rows[-2] if len(rows) >= 2 else rows[-1]
                    closes_only = [float(r[4]) for r in rows[:-1]] if len(rows) >= 2 else [float(rows[-1][4])]
                    rsi_series = compute_rsi_series(closes_only, RSI_PERIOD)
                    last_rsi = rsi_series[-1] if rsi_series else None
                    # Force a first-poll snapshot regardless of date bookkeeping
                    row = {
                        "event": "snapshot",
                        "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
                        "bar_close_ts": int(last_closed[0]),
                        "close": round(float(last_closed[4]), 2),
                        "rsi": round(float(last_rsi), 3) if last_rsi is not None else None,
                        "position": asdict(state.position),
                        "cumulative": asdict(state.cumulative),
                        "note": "first_poll",
                    }
                    write_journal(row)
                    state.last_snapshot_date = row["snapshot_date"]
                    logger.info("first-poll snapshot written: close=%.2f rsi=%s",
                                float(last_closed[4]), last_rsi)
                    first = False
        except KeyboardInterrupt:
            logger.info("ctrl-c — exiting")
            break
        except Exception as exc:  # noqa: BLE001
            logger.exception("poll loop error: %s", exc)
        # sleep with light interruption tolerance
        sleep_remaining = POLL_INTERVAL_SEC
        while sleep_remaining > 0:
            chunk = min(60, sleep_remaining)
            time.sleep(chunk)
            sleep_remaining -= chunk


if __name__ == "__main__":
    main()
