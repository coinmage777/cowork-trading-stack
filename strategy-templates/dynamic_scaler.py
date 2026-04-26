"""Dynamic Scaler — 거래소별 manual_equity 자동 조정.

매 1시간마다 trading_data.db 최근 24h를 조회해서 거래소별 PnL/WR을 계산,
규칙에 따라 config.yaml의 scaling.manual_equity를 증감시킨다.

규칙:
- PnL > +$5 AND WR > 55% → equity 20% 증가 (cap: MAX_EQUITY_USD=300)
- PnL < -$3 AND WR < 40% → equity 20% 감소 (floor: MIN_EQUITY_USD=10)
- 최소 20건 (min_trades) 미만이면 skip
- 거래소별 6시간 쿨다운

안전장치:
- DYNAMIC_SCALER_ENABLED=false 기본 (opt-in)
- DYNAMIC_SCALER_DRY_RUN=true 기본 (로그만, config 변경 X)
- history_path 기록: data/dynamic_scaling_log.jsonl

통합:
- strategies/multi_runner.py에서 DynamicScaler()를 tasks 리스트에 추가
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_MIN_EQUITY = 10.0
DEFAULT_MAX_EQUITY = 300.0
DEFAULT_ADJUST_PCT = 0.20
DEFAULT_MIN_TRADES = 20
DEFAULT_COOLDOWN_SECONDS = 6 * 3600  # 6h
DEFAULT_SCAN_INTERVAL = 3600  # 1h


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def update_yaml_key(path: Path, dotted_key: str, new_value: float, *, create_backup: bool = True) -> bool:
    """scaling.manual_equity.<exchange> 같은 키의 값을 라인-단위 regex 치환으로 안전하게 덮어쓴다.

    주석/순서/다른 키를 보존한다. 반드시 4-space 들여쓰기 규약의 manual_equity 블록에서만 동작.
    """
    import re

    path = Path(path)
    if not path.exists():
        logger.error(f"[dynamic_scaler] config not found: {path}")
        return False

    text = path.read_text(encoding="utf-8")
    parts = dotted_key.split(".")
    if len(parts) < 2:
        logger.error(f"[dynamic_scaler] bad key: {dotted_key}")
        return False
    last = parts[-1]

    block_re = re.compile(r"^  manual_equity:\s*$", re.MULTILINE)
    bm = block_re.search(text)
    if not bm:
        logger.warning("[dynamic_scaler] manual_equity block not found")
        return False
    start = bm.end()
    # 블록 종료: 동일/상위 들여쓰기 (0 or 2 spaces) 섹션 헤더 등장
    end_re = re.compile(r"^(?:\S|  \S)", re.MULTILINE)
    em = end_re.search(text, pos=start + 1)
    end = em.start() if em else len(text)

    block = text[start:end]
    pattern = re.compile(
        rf"(^    {re.escape(last)}: )(-?\d+(?:\.\d+)?)(\s*(?:#.*)?)$",
        re.MULTILINE,
    )
    new_block, n = pattern.subn(
        lambda m: f"{m.group(1)}{new_value}{m.group(3)}", block
    )
    if n == 0:
        logger.warning(f"[dynamic_scaler] {last} not in manual_equity block")
        return False

    if create_backup:
        bak = path.with_suffix(path.suffix + f".bak_dyn_{int(time.time())}")
        try:
            bak.write_text(text, encoding="utf-8")
        except Exception as e:
            logger.debug(f"[dynamic_scaler] backup failed: {e}")

    new_text = text[:start] + new_block + text[end:]
    path.write_text(new_text, encoding="utf-8")
    return True


class DynamicScaler:
    def __init__(
        self,
        *,
        base_dir: Path,
        config_path: Optional[Path] = None,
        db_path: Optional[Path] = None,
        min_equity: float = DEFAULT_MIN_EQUITY,
        max_equity: float = DEFAULT_MAX_EQUITY,
        adjust_pct: float = DEFAULT_ADJUST_PCT,
        min_trades: int = DEFAULT_MIN_TRADES,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        win_pnl_threshold: float = 5.0,
        win_wr_threshold: float = 55.0,
        loss_pnl_threshold: float = -3.0,
        loss_wr_threshold: float = 40.0,
        notifier=None,
    ):
        self.base_dir = Path(base_dir)
        self.config_path = Path(config_path or self.base_dir / "config.yaml")
        self.db_path = Path(db_path or self.base_dir / "trading_data.db")
        self.min_equity = min_equity
        self.max_equity = max_equity
        self.adjust_pct = adjust_pct
        self.min_trades = min_trades
        self.cooldown_seconds = cooldown_seconds
        self.scan_interval = scan_interval
        self.win_pnl_threshold = win_pnl_threshold
        self.win_wr_threshold = win_wr_threshold
        self.loss_pnl_threshold = loss_pnl_threshold
        self.loss_wr_threshold = loss_wr_threshold
        self.notifier = notifier

        self.history_path = self.base_dir / "data" / "dynamic_scaling_log.jsonl"
        self.history_path.parent.mkdir(parents=True, exist_ok=True)

        self.enabled = _env_bool("DYNAMIC_SCALER_ENABLED", False)
        self.dry_run = _env_bool("DYNAMIC_SCALER_DRY_RUN", True)

        self._last_adjust: Dict[str, float] = {}

    def _load_last_adjust(self) -> None:
        if not self.history_path.exists():
            return
        try:
            with self.history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        ex = d.get("exchange")
                        ts = float(d.get("ts", 0))
                        applied = not bool(d.get("dry_run", False))
                        if ex and ts and applied:
                            self._last_adjust[ex] = max(self._last_adjust.get(ex, 0.0), ts)
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"[dynamic_scaler] load history failed: {e}")

    def _current_equity_map(self) -> Dict[str, float]:
        import yaml
        try:
            cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            return dict(cfg.get("scaling", {}).get("manual_equity", {}) or {})
        except Exception as e:
            logger.error(f"[dynamic_scaler] config read failed: {e}")
            return {}

    def _scan_stats(self, hours: int = 24) -> Dict[str, Dict]:
        try:
            con = sqlite3.connect(str(self.db_path), timeout=5)
            cur = con.cursor()
            cur.execute(
                f"""SELECT exchange, COUNT(*) as n,
                    SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) as w,
                    COALESCE(SUM(pnl_usd), 0) as pnl
                FROM trades
                WHERE entry_time > datetime('now','-{int(hours)} hours')
                  AND status='closed'
                GROUP BY exchange"""
            )
            rows = cur.fetchall()
            con.close()
        except Exception as e:
            logger.error(f"[dynamic_scaler] db scan failed: {e}")
            return {}
        out: Dict[str, Dict] = {}
        for ex, n, w, pnl in rows:
            n = int(n or 0)
            w = int(w or 0)
            pnl = float(pnl or 0)
            wr = (100.0 * w / n) if n else 0.0
            out[ex] = {"n": n, "w": w, "pnl": pnl, "wr": wr}
        return out

    def _log_event(self, event: dict) -> None:
        try:
            event = dict(event)
            event.setdefault("ts", time.time())
            event.setdefault("iso", datetime.now(timezone.utc).isoformat())
            with self.history_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[dynamic_scaler] log write failed: {e}")

    async def _notify(self, msg: str, dedup_key: Optional[str] = None) -> None:
        if not self.notifier:
            return
        try:
            if getattr(self.notifier, "is_enabled", lambda: False)():
                await self.notifier.notify(msg, dedup_key=dedup_key, dedup_seconds=1800)
        except Exception as e:
            logger.debug(f"[dynamic_scaler] notify fail: {e}")

    def decide(self, exchange: str, stats: dict, current: float) -> Optional[dict]:
        n = stats.get("n", 0)
        pnl = stats.get("pnl", 0.0)
        wr = stats.get("wr", 0.0)
        if n < self.min_trades:
            return None
        last = self._last_adjust.get(exchange, 0.0)
        if (time.time() - last) < self.cooldown_seconds:
            return None
        if pnl > self.win_pnl_threshold and wr > self.win_wr_threshold:
            new_eq = min(self.max_equity, round(current * (1.0 + self.adjust_pct), 2))
            if new_eq <= current:
                return None
            return {"direction": "up", "old": current, "new": new_eq, "stats": stats}
        if pnl < self.loss_pnl_threshold and wr < self.loss_wr_threshold:
            new_eq = max(self.min_equity, round(current * (1.0 - self.adjust_pct), 2))
            if new_eq >= current:
                return None
            return {"direction": "down", "old": current, "new": new_eq, "stats": stats}
        return None

    async def run_once(self) -> int:
        self._load_last_adjust()
        equity_map = self._current_equity_map()
        if not equity_map:
            logger.info("[dynamic_scaler] no manual_equity keys to manage")
            return 0
        stats_map = self._scan_stats(24)
        applied = 0
        for ex, current in equity_map.items():
            try:
                current_f = float(current)
            except Exception:
                continue
            stats = stats_map.get(ex)
            if not stats:
                continue
            decision = self.decide(ex, stats, current_f)
            if not decision:
                continue

            event = {
                "exchange": ex,
                "decision": decision["direction"],
                "old": decision["old"],
                "new": decision["new"],
                "n": stats["n"],
                "wr": round(stats["wr"], 2),
                "pnl": round(stats["pnl"], 3),
                "dry_run": self.dry_run,
            }
            if self.dry_run:
                logger.info(
                    f"[dynamic_scaler][DRY] {ex}: {decision['direction']} "
                    f"{decision['old']} -> {decision['new']} "
                    f"(n={stats['n']} wr={stats['wr']:.1f}% pnl={stats['pnl']:+.2f})"
                )
                self._log_event(event)
                continue
            ok = update_yaml_key(self.config_path, f"scaling.manual_equity.{ex}", decision["new"])
            if not ok:
                logger.warning(f"[dynamic_scaler] yaml update failed for {ex}")
                continue
            self._last_adjust[ex] = time.time()
            applied += 1
            logger.info(
                f"[dynamic_scaler] {ex}: {decision['direction']} "
                f"{decision['old']} -> {decision['new']} "
                f"(n={stats['n']} wr={stats['wr']:.1f}% pnl={stats['pnl']:+.2f})"
            )
            self._log_event(event)
            arrow = "UP" if decision["direction"] == "up" else "DOWN"
            await self._notify(
                f"<b>DynamicScaler {arrow}</b> {ex}\n"
                f"{decision['old']} -> {decision['new']} USD\n"
                f"24h: n={stats['n']} WR={stats['wr']:.1f}% PnL={stats['pnl']:+.2f}",
                dedup_key=f"dyn_scaler_{ex}_{decision['direction']}",
            )
        return applied

    async def run(self) -> None:
        if not self.enabled:
            logger.info("[dynamic_scaler] disabled via env; task exiting")
            return
        logger.info(
            f"[dynamic_scaler] started — dry_run={self.dry_run} "
            f"scan={self.scan_interval}s cooldown={self.cooldown_seconds}s "
            f"min_trades={self.min_trades}"
        )
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"[dynamic_scaler] loop error: {e}", exc_info=True)
            await asyncio.sleep(self.scan_interval)


# python -m strategies.dynamic_scaler --once [--force-live]
if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one scan then exit")
    ap.add_argument("--base", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--force-live", action="store_true", help="enable + disable dry_run via env override")
    args = ap.parse_args()
    if args.force_live:
        os.environ["DYNAMIC_SCALER_ENABLED"] = "true"
        os.environ["DYNAMIC_SCALER_DRY_RUN"] = "false"
    sc = DynamicScaler(base_dir=Path(args.base))
    if args.once:
        asyncio.run(sc.run_once())
    else:
        asyncio.run(sc.run())
