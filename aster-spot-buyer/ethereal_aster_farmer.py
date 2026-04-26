"""
Ethereal + Aster Volume Farmer — same-symbol delta-neutral hedge.

Ethereal LONG/SHORT  (wallet 0x49..., main WALLET_A_PK) — Hyperliquid-like clearinghouse
  +
Aster        SHORT/LONG (user 0xWALLA..., signer 0xSIGNR... API wallet) — Binance-style REST

Per cycle:
  1) safety checks (kill switches, daily PnL, kill file, consec fails, daily cap)
  2) fetch mark on both venues, abort if cross-venue spread > 0.5%
  3) asyncio.gather(open_eth, open_ast) near-simultaneous entry
  4) verify both legs have non-zero positions within 5s, else emergency-close + STOP
  5) hold interval_sec (jitter ±20%)
  6) asyncio.gather(close_eth, close_ast) reduce-only
  7) verify both flat, journal the cycle

Kill switches:
  - File data/KILL_ETH_AST_FARMER exists
  - Daily PnL  < ETH_AST_FARMER_DAILY_STOP_USD (default -$5)
  - Daily cap reached
  - 5 consecutive failures
  - Either venue collateral below floor

Symbol mapping:
  - asset "BTC" → Ethereal symbol "BTCUSD" | Aster symbol "BTCUSDT"
  - asset "ETH" → Ethereal symbol "ETHUSD" | Aster symbol "ETHUSDT"

Architecture note:
  Ethereal wrapper exposes async API but has no .close_position(); we close via reverse
  reduce-only market order. Aster wrapper has close_position via reduce-only market.

Usage:
  cd <INSTALL_DIR>/multi-perp-dex
  source main_venv/bin/activate
  python -m strategies.ethereal_aster_farmer --dry-run
  python -m strategies.ethereal_aster_farmer --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

# Allow "python -m strategies.var_aster_farmer" from the repo root,
# OR direct execution from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpdex.factory import create_exchange  # noqa: E402

try:
    from strategies.notifier import notify  # noqa: E402
except Exception:  # pragma: no cover
    async def notify(*args, **kwargs):  # type: ignore
        return False


logger = logging.getLogger("ethereal_aster_farmer")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Variational identity (config.yaml exchanges.variational_2)
ETH_ACCOUNT_NAME = "ethereal"

# Aster identity (config.yaml exchanges.aster)
AST_ACCOUNT_NAME = "aster"

# v2 jitter — rotate asset/size/hold to avoid exact-pattern fingerprinting.
# Ethereal + Aster ETH liquidity has been observed; BTC default for safety.
ASSET_POOL_DEFAULT = ("BTC",)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.environ.get(key, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(key: str, default: tuple) -> tuple:
    raw = os.environ.get(key)
    if not raw:
        return default
    items = tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return items or default


# Aster uses Binance-style integer-step lot sizes (BTC 0.001, ETH 0.001).
# Variational accepts up to 5-decimal qty for BTC, 4-decimal for ETH.
# We use the more conservative (Aster) precision to keep both venues happy.
ASSET_SIZE_DECIMALS = {"BTC": 3, "ETH": 3, "SOL": 1}
# Minimum notional per leg ($) — Aster has a $5+ floor; Variational has indicative quote min.
MIN_NOTIONAL_USD = 30.0


@dataclass
class FarmerConfig:
    position_size_usd: float = field(
        default_factory=lambda: _env_float("ETH_AST_FARMER_POSITION_SIZE_USD", 50.0)
    )
    leverage: int = field(default_factory=lambda: _env_int("ETH_AST_FARMER_LEVERAGE", 3))
    interval_sec: int = field(default_factory=lambda: _env_int("ETH_AST_FARMER_INTERVAL_SEC", 300))
    daily_cap: int = field(default_factory=lambda: _env_int("ETH_AST_FARMER_DAILY_CAP", 10))
    daily_stop_usd: float = field(
        default_factory=lambda: _env_float("ETH_AST_FARMER_DAILY_STOP_USD", -3.0)
    )
    kill_file: str = field(
        default_factory=lambda: os.environ.get(
            "ETH_AST_FARMER_KILL_FILE", "data/KILL_ETH_AST_FARMER"
        )
    )

    # Asset rotation
    asset_pool: tuple = field(
        default_factory=lambda: _env_list("ETH_AST_FARMER_ASSET_POOL", ASSET_POOL_DEFAULT)
    )
    asset_rotation: bool = field(default_factory=lambda: _env_bool("ETH_AST_FARMER_ASSET_ROTATION", True))
    direction_random: bool = field(default_factory=lambda: _env_bool("ETH_AST_FARMER_DIRECTION_RANDOM", True))

    # Funding-aware (Aster funding only — Variational has no per-symbol funding feed)
    funding_aware: bool = field(default_factory=lambda: _env_bool("ETH_AST_FARMER_FUNDING_AWARE", False))
    funding_diff_threshold: float = field(
        default_factory=lambda: _env_float("ETH_AST_FARMER_FUNDING_DIFF_THRESHOLD", 0.0005)
    )

    # Jitter
    size_jitter_pct: float = field(default_factory=lambda: _env_float("ETH_AST_FARMER_SIZE_JITTER_PCT", 0.10))
    hold_jitter_pct: float = field(default_factory=lambda: _env_float("ETH_AST_FARMER_HOLD_JITTER_PCT", 0.20))
    cycle_gap_min: float = field(default_factory=lambda: _env_float("ETH_AST_FARMER_CYCLE_GAP_MIN", 2.0))
    cycle_gap_max: float = field(default_factory=lambda: _env_float("ETH_AST_FARMER_CYCLE_GAP_MAX", 8.0))
    hold_min: int = field(default_factory=lambda: _env_int("ETH_AST_FARMER_HOLD_MIN", 180))
    hold_max: int = field(default_factory=lambda: _env_int("ETH_AST_FARMER_HOLD_MAX", 600))

    # Hard limits
    min_var_collateral: float = field(
        default_factory=lambda: _env_float("ETH_AST_FARMER_MIN_VAR_COLLATERAL", 80.0)
    )
    min_ast_collateral: float = field(
        default_factory=lambda: _env_float("ETH_AST_FARMER_MIN_AST_COLLATERAL", 50.0)
    )
    max_spread_pct: float = field(default_factory=lambda: _env_float("ETH_AST_FARMER_MAX_SPREAD_PCT", 0.005))
    fill_timeout_sec: float = field(default_factory=lambda: _env_float("ETH_AST_FARMER_FILL_TIMEOUT_SEC", 5.0))
    max_consecutive_failures: int = field(
        default_factory=lambda: _env_int("ETH_AST_FARMER_MAX_CONSEC_FAILS", 5)
    )
    close_verify_timeout: float = field(
        default_factory=lambda: _env_float("ETH_AST_FARMER_CLOSE_VERIFY_TIMEOUT", 6.0)
    )

    # Allow override of pre-flight reconcile-blocked behavior — STRONGLY discouraged
    skip_reconcile_block: bool = field(default_factory=lambda: _env_bool("ETH_AST_FARMER_SKIP_RECONCILE_BLOCK", False))



async def _ethereal_close(ex, symbol: str, position: Optional[Dict[str, Any]]):
    """Close ethereal position via reduce-only reverse market order."""
    if not position:
        return None
    try:
        size = float(position.get("size", 0))
        if abs(size) < 1e-9:
            return None
        side = position.get("side", "").lower()
        # if long -> sell; if short -> buy
        rev = "sell" if side == "long" else "buy"
        return await ex.create_order(symbol, rev, size, order_type="market", reduce_only=True)
    except Exception as e:
        logger.error("_ethereal_close err on %s: %s", symbol, e)
        return None


class EthAstFarmer:
    def __init__(self, cfg: FarmerConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.var = None
        self.ast = None
        self._running = False
        self._cycles = 0
        self._round_trips = 0
        self._consec_fails = 0
        self._day_key = self._today()
        self._day_start_var_collateral: Optional[float] = None
        self._day_start_ast_collateral: Optional[float] = None
        self._journal_path = DATA_DIR / "ethereal_aster_farmer.jsonl"
        self._asset_idx = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config_yaml() -> dict:
        import yaml
        from strategies.env_loader import resolve_env_vars  # late import (sys.path set)
        with open(REPO_ROOT / "config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return resolve_env_vars(data)

    async def connect(self):
        cfg_yaml = self._load_config_yaml()

        # ---- Ethereal in-process via factory --------------
        eth_keys = cfg_yaml["exchanges"][ETH_ACCOUNT_NAME]["keys"]
        eth_kp = SimpleNamespace(
            private_key=eth_keys["private_key"],
        )
        logger.info("connecting Ethereal (account=%s)...", ETH_ACCOUNT_NAME)
        self.var = await create_exchange("ethereal", eth_kp)

        # ---- Aster direct import (factory has no aster branch) -----
        ast_keys = cfg_yaml["exchanges"][AST_ACCOUNT_NAME]["keys"]
        from mpdex.exchanges.aster import AsterExchange
        logger.info("connecting Aster (user=%s)...", ast_keys["user_address"])
        self.ast = await AsterExchange(
            ast_keys["user_address"], ast_keys["signer_private_key"]
        ).init()
        logger.info("Ethereal + Aster connected")

    async def close(self):
        for name, ex in (("var", self.var), ("ast", self.ast)):
            if ex is None:
                continue
            try:
                await ex.close()
            except Exception as e:
                logger.debug("%s.close err: %s", name, e)

    # ------------------------------------------------------------------
    # asset rotation + helpers
    # ------------------------------------------------------------------

    def _pick_asset(self) -> str:
        if not self.cfg.asset_rotation or len(self.cfg.asset_pool) <= 1:
            return self.cfg.asset_pool[0] if self.cfg.asset_pool else "BTC"
        asset = self.cfg.asset_pool[self._asset_idx % len(self.cfg.asset_pool)]
        self._asset_idx += 1
        return asset

    async def _direction(self, asset: str) -> str:
        """Return 'var_long_ast_short' or 'var_short_ast_long'."""
        # Funding-aware tilt — only Aster has funding feed. Bias the SHORT to whichever
        # venue is paying us. If Aster funding < -threshold (longs are paid), put LONG on Aster.
        if self.cfg.funding_aware:
            try:
                ast_fund = await self.ast.get_funding_rate(self._ast_symbol(asset))
            except Exception as e:
                logger.warning("funding fetch err: %s", e)
                ast_fund = None
            if ast_fund is not None:
                if ast_fund < -self.cfg.funding_diff_threshold:
                    return "var_short_ast_long"   # longs paid on aster
                if ast_fund > self.cfg.funding_diff_threshold:
                    return "var_long_ast_short"   # shorts paid on aster
        # default: random when allowed, else var-long-ast-short
        if self.cfg.direction_random and random.random() < 0.5:
            return "var_short_ast_long"
        return "var_long_ast_short"

    @staticmethod
    def _var_symbol(asset: str) -> str:
        return f"{asset.upper()}USD"

    @staticmethod
    def _ast_symbol(asset: str) -> str:
        return f"{asset.upper()}USDT"

    @staticmethod
    def _round_size(asset: str, size: float) -> float:
        decimals = ASSET_SIZE_DECIMALS.get(asset.upper(), 3)
        factor = 10 ** decimals
        return int(size * factor) / factor

    @staticmethod
    def _scalar_collateral(col: Any) -> float:
        if col is None:
            return 0.0
        if isinstance(col, (int, float)):
            return float(col)
        if isinstance(col, dict):
            for key in ("total_collateral", "available_collateral", "balance"):
                v = col.get(key)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0
        try:
            return float(col)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _scalar_available(col: Any) -> Optional[float]:
        if isinstance(col, dict):
            v = col.get("available_collateral")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    # ------------------------------------------------------------------
    # pre-flight
    # ------------------------------------------------------------------

    async def reconcile(self) -> bool:
        """Return True if clean. False = abort start.

        IMPORTANT: This farmer must NOT collide with multi_runner. If multi_runner is
        currently holding a position on Ethereal / Aster for any of our pool assets,
        reconcile will abort. Override only with ETH_AST_FARMER_SKIP_RECONCILE_BLOCK=1
        AND only if you understand what you're doing.
        """
        clean = True
        per_asset: Dict[str, Dict[str, Any]] = {}
        for asset in self.cfg.asset_pool:
            try:
                vpos, apos = await asyncio.gather(
                    self.var.get_position(self._var_symbol(asset)),
                    self.ast.get_position(self._ast_symbol(asset)),
                    return_exceptions=True,
                )
            except Exception as e:
                logger.error("reconcile gather %s err: %s", asset, e)
                return False
            if isinstance(vpos, Exception):
                logger.warning("var.get_position(%s) err: %s", asset, vpos)
                vpos = None
            if isinstance(apos, Exception):
                logger.warning("ast.get_position(%s) err: %s", asset, apos)
                apos = None
            per_asset[asset] = {"var": vpos, "ast": apos}
            if vpos or apos:
                clean = False

        try:
            vcol = await self.var.get_collateral()
        except Exception as e:
            logger.error("var.get_collateral failed: %s", e)
            return False
        try:
            acol = await self.ast.get_collateral()
        except Exception as e:
            logger.error("ast.get_collateral failed: %s", e)
            return False

        var_total = self._scalar_collateral(vcol)
        ast_avail = self._scalar_available(acol)
        ast_total = ast_avail if ast_avail is not None else self._scalar_collateral(acol)

        logger.info(
            "reconcile: var_col=$%.2f ast_avail=$%.2f  per-asset=%s",
            var_total, ast_total, per_asset,
        )
        self._day_start_var_collateral = var_total
        self._day_start_ast_collateral = ast_total

        if not clean:
            msg_parts = [
                f"{a}: var={d['var']} ast={d['ast']}"
                for a, d in per_asset.items() if d["var"] or d["ast"]
            ]
            err = f"RECONCILE FAIL: pre-existing positions: {'; '.join(msg_parts)}"
            logger.error(err)
            await notify(
                f"<b>VarAsterFarmer RECONCILE FAIL</b>\n"
                f"Pre-existing positions:\n<code>{'; '.join(msg_parts)}</code>\n"
                f"This typically means multi_runner is using these venues. "
                f"Either disable variational_2/aster in multi_runner config, "
                f"or close positions manually.",
                dedup_key="var_aster_farmer_reconcile_fail",
            )
            if not self.cfg.skip_reconcile_block:
                return False
            logger.warning("SKIP_RECONCILE_BLOCK=1 → continuing despite open positions (DANGEROUS)")

        if var_total < self.cfg.min_var_collateral:
            logger.error(
                "var collateral $%.2f < $%.2f min", var_total, self.cfg.min_var_collateral,
            )
            await notify(
                f"<b>VarAsterFarmer START FAIL</b>\n"
                f"Variational collateral ${var_total:.2f} < ${self.cfg.min_var_collateral:.0f} min",
                dedup_key="var_aster_farmer_min_var_col",
            )
            return False
        if ast_total < self.cfg.min_ast_collateral:
            logger.error(
                "ast collateral $%.2f < $%.2f min", ast_total, self.cfg.min_ast_collateral,
            )
            await notify(
                f"<b>VarAsterFarmer START FAIL</b>\n"
                f"Aster collateral ${ast_total:.2f} < ${self.cfg.min_ast_collateral:.0f} min",
                dedup_key="var_aster_farmer_min_ast_col",
            )
            return False
        return True

    # ------------------------------------------------------------------
    # kill switches
    # ------------------------------------------------------------------

    async def _preflight(self) -> Optional[str]:
        kill_path = (REPO_ROOT / self.cfg.kill_file).resolve()
        if kill_path.exists():
            return f"kill file present ({kill_path})"

        if self._consec_fails >= self.cfg.max_consecutive_failures:
            return f"{self._consec_fails} consecutive failures (>= {self.cfg.max_consecutive_failures})"

        if self._round_trips >= self.cfg.daily_cap:
            return f"daily round-trip cap reached ({self._round_trips}/{self.cfg.daily_cap})"

        try:
            vcol = await self.var.get_collateral()
            var_total = self._scalar_collateral(vcol)
        except Exception as e:
            return f"get var collateral failed: {e}"
        if var_total < self.cfg.min_var_collateral:
            return f"var collateral ${var_total:.2f} < ${self.cfg.min_var_collateral:.0f}"

        try:
            acol = await self.ast.get_collateral()
            ast_avail = self._scalar_available(acol)
            ast_total = ast_avail if ast_avail is not None else self._scalar_collateral(acol)
        except Exception as e:
            return f"get ast collateral failed: {e}"
        if ast_total < self.cfg.min_ast_collateral:
            return f"ast collateral ${ast_total:.2f} < ${self.cfg.min_ast_collateral:.0f}"

        if (
            self._day_start_var_collateral is not None
            and self._day_start_ast_collateral is not None
        ):
            day_pnl = (var_total + ast_total) - (
                self._day_start_var_collateral + self._day_start_ast_collateral
            )
            if day_pnl < self.cfg.daily_stop_usd:
                return f"daily PnL ${day_pnl:.2f} < ${self.cfg.daily_stop_usd:.2f}"

        # roll-over day
        if self._today() != self._day_key:
            self._day_key = self._today()
            self._round_trips = 0
            self._day_start_var_collateral = var_total
            self._day_start_ast_collateral = ast_total

        return None

    # ------------------------------------------------------------------
    # one round-trip
    # ------------------------------------------------------------------

    async def _fetch_marks(self, asset: str) -> Optional[tuple]:
        try:
            v_mark, a_mark = await asyncio.gather(
                self.var.get_mark_price(self._var_symbol(asset)),
                self.ast.get_mark_price(self._ast_symbol(asset)),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("fetch marks(%s) failed: %s", asset, e)
            return None
        if isinstance(v_mark, Exception):
            logger.error("var mark err(%s): %s", asset, v_mark)
            return None
        if isinstance(a_mark, Exception):
            logger.error("ast mark err(%s): %s", asset, a_mark)
            return None

        try:
            v_mark = float(v_mark) if v_mark is not None else 0.0
            a_mark = float(a_mark) if a_mark is not None else 0.0
        except (TypeError, ValueError):
            logger.error("invalid mark types: var=%s ast=%s", v_mark, a_mark)
            return None
        if v_mark <= 0 or a_mark <= 0:
            logger.error("invalid marks: var=%s ast=%s", v_mark, a_mark)
            return None
        spread = abs(v_mark - a_mark) / ((v_mark + a_mark) / 2.0)
        if spread > self.cfg.max_spread_pct:
            logger.warning(
                "spread %.4f > cap %.4f (var=%.4f ast=%.4f) for %s",
                spread, self.cfg.max_spread_pct, v_mark, a_mark, asset,
            )
            return None
        return v_mark, a_mark, spread

    def _size_units(self, asset: str, mid_price: float) -> float:
        jitter = 1.0
        if self.cfg.size_jitter_pct > 0:
            jitter += random.uniform(-self.cfg.size_jitter_pct, self.cfg.size_jitter_pct)
        notional = max(MIN_NOTIONAL_USD, self.cfg.position_size_usd * jitter)
        raw = notional / mid_price
        return self._round_size(asset, raw)

    def _hold_seconds(self) -> int:
        # honor explicit hold_min/hold_max if both set; otherwise use interval_sec ± jitter
        if self.cfg.hold_min and self.cfg.hold_max and self.cfg.hold_max >= self.cfg.hold_min:
            return random.randint(int(self.cfg.hold_min), int(self.cfg.hold_max))
        if self.cfg.hold_jitter_pct <= 0:
            return self.cfg.interval_sec
        jitter = 1.0 + random.uniform(-self.cfg.hold_jitter_pct, self.cfg.hold_jitter_pct)
        return max(30, int(self.cfg.interval_sec * jitter))

    async def _open_both(
        self, asset: str, size: float, direction: str
    ) -> Dict[str, Any]:
        t0 = time.time()
        var_side = "buy" if direction == "var_long_ast_short" else "sell"
        ast_side = "SELL" if direction == "var_long_ast_short" else "BUY"

        # micro-jitter (ms) so the two opens don't lock-step on the wire
        delay_ms = random.uniform(0.0, 0.300)

        async def _var_open():
            return await self.var.create_order(
                self._var_symbol(asset), var_side, size, order_type="market"
            )

        async def _ast_open():
            await asyncio.sleep(delay_ms)
            return await self.ast.create_order(
                self._ast_symbol(asset), ast_side, size, order_type="market"
            )

        try:
            vres, ares = await asyncio.gather(_var_open(), _ast_open(), return_exceptions=True)
        except Exception as e:
            logger.error("open gather failed: %s", e)
            return {"ok": False, "error": str(e)}
        v_err = vres if isinstance(vres, Exception) else None
        a_err = ares if isinstance(ares, Exception) else None

        # verify positions exist on both
        await asyncio.sleep(1.5)
        deadline = t0 + self.cfg.fill_timeout_sec
        var_pos = None
        ast_pos = None
        while time.time() < deadline:
            vp, ap = await asyncio.gather(
                self.var.get_position(self._var_symbol(asset)),
                self.ast.get_position(self._ast_symbol(asset)),
                return_exceptions=True,
            )
            var_pos = vp if not isinstance(vp, Exception) else None
            ast_pos = ap if not isinstance(ap, Exception) else None
            if var_pos and ast_pos:
                break
            await asyncio.sleep(0.5)

        return {
            "ok": bool(var_pos and ast_pos),
            "var_order": None if v_err else vres,
            "ast_order": None if a_err else ares,
            "var_err": str(v_err) if v_err else None,
            "ast_err": str(a_err) if a_err else None,
            "var_pos": var_pos,
            "ast_pos": ast_pos,
            "open_latency": time.time() - t0,
        }

    async def _close_both(self, asset: str) -> Dict[str, Any]:
        t0 = time.time()
        vpos, apos = await asyncio.gather(
            self.var.get_position(self._var_symbol(asset)),
            self.ast.get_position(self._ast_symbol(asset)),
            return_exceptions=True,
        )
        if isinstance(vpos, Exception):
            vpos = None
        if isinstance(apos, Exception):
            apos = None

        try:
            await asyncio.gather(
                _ethereal_close(self.var, self._var_symbol(asset), vpos)
                if vpos else asyncio.sleep(0),
                self.ast.close_position(self._ast_symbol(asset), apos)
                if apos else asyncio.sleep(0),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("close gather failed: %s", e)

        # verify flat
        deadline = time.time() + self.cfg.close_verify_timeout
        vp = ap = None
        while time.time() < deadline:
            vp, ap = await asyncio.gather(
                self.var.get_position(self._var_symbol(asset)),
                self.ast.get_position(self._ast_symbol(asset)),
                return_exceptions=True,
            )
            if isinstance(vp, Exception):
                vp = None
            if isinstance(ap, Exception):
                ap = None
            if not vp and not ap:
                break
            await asyncio.sleep(0.5)

        return {
            "ok": (vp is None) and (ap is None),
            "var_residual": vp,
            "ast_residual": ap,
            "close_latency": time.time() - t0,
        }

    async def _round_trip(self) -> Dict[str, Any]:
        asset = self._pick_asset()
        direction = await self._direction(asset)
        marks = await self._fetch_marks(asset)
        if marks is None:
            return {"ok": False, "stage": "fetch_marks", "asset": asset}
        v_mark, a_mark, spread = marks
        mid = (v_mark + a_mark) / 2.0
        size_units = self._size_units(asset, mid)
        if size_units <= 0:
            return {"ok": False, "stage": "size_zero", "asset": asset, "mid": mid}
        notional = size_units * mid
        hold = self._hold_seconds()

        logger.info(
            "OPEN %s %s: size=%.6f (~$%.2f) var=%.4f ast=%.4f spread=%.4f%% hold=%ds",
            asset, direction, size_units, notional,
            v_mark, a_mark, spread * 100, hold,
        )

        if self.dry_run:
            logger.info(
                "DRY-RUN: would open var %s + ast %s for %s, hold %ds, close both",
                "buy" if direction == "var_long_ast_short" else "sell",
                "SELL" if direction == "var_long_ast_short" else "BUY",
                asset, hold,
            )
            return {
                "ok": True,
                "dry_run": True,
                "asset": asset,
                "direction": direction,
                "var_mark": v_mark,
                "ast_mark": a_mark,
                "size_units": size_units,
                "notional": notional,
                "hold_sec": hold,
            }

        open_res = await self._open_both(asset, size_units, direction)
        if not open_res.get("ok"):
            logger.error("OPEN FAILED: %s", open_res)
            await self._emergency_close(asset)
            return {"ok": False, "stage": "open", "asset": asset, "detail": open_res}

        vpos = open_res["var_pos"] or {}
        apos = open_res["ast_pos"] or {}
        try:
            v_entry = float(vpos.get("entry_price", 0))
        except (TypeError, ValueError):
            v_entry = 0.0
        try:
            a_entry = float(apos.get("entry_price", 0))
        except (TypeError, ValueError):
            a_entry = 0.0
        try:
            delta_imbalance = abs(float(vpos.get("size", 0)) - float(apos.get("size", 0)))
        except (TypeError, ValueError):
            delta_imbalance = float("nan")

        logger.info(
            "FILLED: var_entry=%.4f ast_entry=%.4f |size_delta|=%.6f latency=%.2fs",
            v_entry, a_entry, delta_imbalance, open_res["open_latency"],
        )

        logger.info("HOLD %ds...", hold)
        await asyncio.sleep(hold)

        close_res = await self._close_both(asset)
        if not close_res.get("ok"):
            logger.error("CLOSE INCOMPLETE: %s", close_res)
            await notify(
                f"<b>VarAsterFarmer CLOSE FAIL</b>\n"
                f"asset={asset}\n"
                f"var_residual={close_res.get('var_residual')}\n"
                f"ast_residual={close_res.get('ast_residual')}\n"
                f"Manual intervention may be needed.",
                dedup_key="var_aster_farmer_close_fail",
            )
            return {
                "ok": False,
                "stage": "close",
                "asset": asset,
                "open": open_res,
                "close": close_res,
            }

        try:
            vcol, acol = await asyncio.gather(
                self.var.get_collateral(),
                self.ast.get_collateral(),
            )
            var_total = self._scalar_collateral(vcol)
            ast_avail = self._scalar_available(acol)
            ast_total = ast_avail if ast_avail is not None else self._scalar_collateral(acol)
        except Exception:
            var_total = ast_total = float("nan")

        return {
            "ok": True,
            "asset": asset,
            "direction": direction,
            "var_mark": v_mark,
            "ast_mark": a_mark,
            "spread_pct": spread * 100,
            "size_units": size_units,
            "notional": notional,
            "var_entry": v_entry,
            "ast_entry": a_entry,
            "delta_imbalance": delta_imbalance,
            "open_latency": open_res["open_latency"],
            "close_latency": close_res["close_latency"],
            "hold_sec": hold,
            "var_collateral_after": var_total,
            "ast_collateral_after": ast_total,
        }

    async def _emergency_close(self, asset: str):
        logger.warning("EMERGENCY CLOSE triggered for %s", asset)
        try:
            vpos, apos = await asyncio.gather(
                self.var.get_position(self._var_symbol(asset)),
                self.ast.get_position(self._ast_symbol(asset)),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("emergency get_position failed: %s", e)
            vpos = apos = None
        if isinstance(vpos, Exception):
            vpos = None
        if isinstance(apos, Exception):
            apos = None
        tasks = []
        if vpos:
            tasks.append(_ethereal_close(self.var, self._var_symbol(asset), vpos))
        if apos:
            tasks.append(self.ast.close_position(self._ast_symbol(asset), apos))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await notify(
            f"<b>VarAsterFarmer EMERGENCY CLOSE</b>\n"
            f"asset={asset}\n"
            f"var_pos={vpos}\nast_pos={apos}\n"
            f"Farmer will STOP after this.",
            dedup_key="var_aster_farmer_emergency_close",
        )

    # ------------------------------------------------------------------
    # journal
    # ------------------------------------------------------------------

    def _journal(self, event: str, payload: Dict[str, Any]):
        row = {
            "ts": time.time(),
            "iso": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        try:
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as e:
            logger.debug("journal write failed: %s", e)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    async def run_loop(self):
        self._running = True
        await notify(
            f"<b>VarAsterFarmer START</b> ({'DRY' if self.dry_run else 'LIVE'})\n"
            f"size=${self.cfg.position_size_usd:.0f}/leg  hold={self.cfg.hold_min}-{self.cfg.hold_max}s  "
            f"cap={self.cfg.daily_cap}/day  stop=${self.cfg.daily_stop_usd:.2f}\n"
            f"assets={list(self.cfg.asset_pool)} rotation={self.cfg.asset_rotation} "
            f"funding_aware={self.cfg.funding_aware}",
            dedup_key="var_aster_farmer_start",
        )
        self._journal("start", {
            "dry_run": self.dry_run,
            "cfg": {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.cfg.__dict__.items()},
        })

        while self._running:
            reason = await self._preflight()
            if reason:
                logger.error("KILL SWITCH: %s", reason)
                await notify(
                    f"<b>VarAsterFarmer STOP</b>\nreason: <code>{reason}</code>",
                    dedup_key=f"var_aster_farmer_stop_{reason[:20]}",
                )
                self._journal("stop", {"reason": reason})
                break

            try:
                result = await self._round_trip()
            except Exception as e:
                logger.exception("round-trip exception: %s", e)
                result = {"ok": False, "stage": "exception", "error": str(e)}

            self._cycles += 1
            self._journal("cycle", result)

            if result.get("ok"):
                self._round_trips += 1
                self._consec_fails = 0
                if self.dry_run:
                    logger.info("DRY cycle OK — exiting dry-run loop after one pass")
                    break
            else:
                self._consec_fails += 1
                logger.error(
                    "cycle failed (consec=%d): stage=%s asset=%s",
                    self._consec_fails, result.get("stage"), result.get("asset"),
                )
                if self._consec_fails >= self.cfg.max_consecutive_failures:
                    await notify(
                        f"<b>VarAsterFarmer STOP</b>\n"
                        f"{self._consec_fails} consecutive failures",
                        dedup_key="var_aster_farmer_consec_fail",
                    )
                    break

            # cycle gap (jittered)
            if self._running and not self.dry_run:
                gap = random.uniform(
                    max(0.5, self.cfg.cycle_gap_min),
                    max(self.cfg.cycle_gap_min + 0.5, self.cfg.cycle_gap_max),
                )
                await asyncio.sleep(gap)

        self._journal("shutdown", {
            "cycles": self._cycles,
            "round_trips": self._round_trips,
            "consec_fails": self._consec_fails,
        })

    def stop(self):
        self._running = False

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# entrypoint
# ----------------------------------------------------------------------

async def _amain():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print one cycle without orders")
    mode.add_argument("--live", action="store_true", help="live orders")
    parser.add_argument("--interval", type=int, default=None,
                        help="override hold interval seconds (overrides hold_min/max)")
    parser.add_argument("--size-usd", type=float, default=None,
                        help="override per-leg notional USD")
    parser.add_argument("--asset", type=str, default=None,
                        help="override asset pool (comma-sep, e.g. 'BTC,ETH')")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="run reconcile + connect, then exit (sanity check)")
    parser.add_argument("--max-cycles", type=int, default=None,
                        help="exit after N successful round-trips")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        from dotenv import load_dotenv
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass

    cfg = FarmerConfig()
    if args.interval is not None:
        cfg.hold_min = int(args.interval)
        cfg.hold_max = int(args.interval)
        cfg.interval_sec = int(args.interval)
    if args.size_usd is not None:
        cfg.position_size_usd = float(args.size_usd)
    if args.asset:
        items = tuple(x.strip().upper() for x in args.asset.split(",") if x.strip())
        if items:
            cfg.asset_pool = items
    if args.max_cycles is not None:
        cfg.daily_cap = int(args.max_cycles)

    farmer = EthAstFarmer(cfg, dry_run=args.dry_run)

    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, farmer.stop)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        await farmer.connect()
    except Exception as e:
        logger.exception("connect failed: %s", e)
        await notify(
            f"<b>VarAsterFarmer CONNECT FAIL</b>\n<code>{e}</code>",
            dedup_key="var_aster_farmer_connect_fail",
        )
        return 2

    try:
        ok = await farmer.reconcile()
        if not ok:
            return 3
        if args.reconcile_only:
            logger.info("reconcile-only requested → exit 0")
            return 0
        await farmer.run_loop()
    finally:
        try:
            await farmer.close()
        except Exception:
            pass

    return 0


def main():
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
