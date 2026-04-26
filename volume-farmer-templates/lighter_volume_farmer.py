"""
Lighter Volume Farmer — cross-wallet delta-neutral hedge.

Lighter BTC LONG  (account <REDACTED>, l1=0xWALLA..., WALLET_A_PK)
  +
HL      BTC SHORT (wallet 0xWALLB..., WALLET_B_PK)

Per cycle:
  1) safety checks (kill switches)
  2) fetch mark from both venues, abort if spread > 0.5%
  3) asyncio.gather(lighter.buy, hl.sell) near-simultaneous open
  4) verify both legs filled within 4s, else STOP + alert
  5) hold ``interval_sec`` (default 300s = 5min)
  6) asyncio.gather(lighter.close, hl.close) reduce-only
  7) verify both flat, journal cycle

Kill switches:
  - Daily PnL < LIGHTER_FARMER_DAILY_STOP_USD (default -$3)
  - Both legs not filled in 4s
  - Lighter collateral < $100
  - 5 consecutive fill failures
  - File data/KILL_LIGHTER_FARMER exists

Architecture note:
  Lighter SDK does sync HTTP in __init__ → can deadlock when other isolated
  exchanges are in the same event loop. We use SubprocessExchangeWrapper
  (system Python) to spawn a Lighter bridge process.  HL runs in-process
  via mpdex.factory.create_exchange (no isolation needed).

Usage:
  cd <INSTALL_DIR>/multi-perp-dex
  source main_venv/bin/activate
  python -m strategies.lighter_volume_farmer --dry-run
  python -m strategies.lighter_volume_farmer --live
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

# Allow "python -m strategies.lighter_volume_farmer" from the repo root,
# OR direct execution from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpdex.factory import create_exchange  # noqa: E402
from strategies.subprocess_wrapper import SubprocessExchangeWrapper  # noqa: E402

try:
    from strategies.notifier import notify  # noqa: E402
except Exception:  # pragma: no cover
    async def notify(*args, **kwargs):  # type: ignore
        return False


logger = logging.getLogger("lighter_volume_farmer")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# Lighter wallet (WALLET_A_PK = 0xWALLA...) — also where Lighter API key is registered
LIGHTER_ACCOUNT_NAME = "lighter"        # config.yaml exchanges.lighter
LIGHTER_L1_ADDRESS = "<EVM_ADDRESS>"

# HL hedge wallet (WALLET_B_PK = 0xWALLB...) — separate wallet for sybil protection
HL_HEDGE_WALLET = "<EVM_ADDRESS>"

# v2 jitter — rotate asset/size/hold to avoid exact-pattern fingerprinting.
ASSET_POOL_DEFAULT = ("BTC", "ETH", "SOL")


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


# Lighter step sizes (size_decimals from market_info; conservative defaults).
# Trimmed to lot precision so order isn't rejected for over-precision.
ASSET_SIZE_DECIMALS = {"BTC": 5, "ETH": 4, "SOL": 2}


@dataclass
class FarmerConfig:
    position_size_usd: float = field(
        default_factory=lambda: _env_float("LIGHTER_FARMER_POSITION_SIZE_USD", 50.0)
    )
    leverage: int = field(default_factory=lambda: _env_int("LIGHTER_FARMER_LEVERAGE", 3))
    interval_sec: int = field(default_factory=lambda: _env_int("LIGHTER_FARMER_INTERVAL_SEC", 300))
    daily_cap: int = field(default_factory=lambda: _env_int("LIGHTER_FARMER_DAILY_CAP", 20))
    daily_stop_usd: float = field(
        default_factory=lambda: _env_float("LIGHTER_FARMER_DAILY_STOP_USD", -3.0)
    )
    kill_file: str = field(
        default_factory=lambda: os.environ.get(
            "LIGHTER_FARMER_KILL_FILE", "data/KILL_LIGHTER_FARMER"
        )
    )

    # Asset rotation (v2 polish)
    asset_pool: tuple = field(
        default_factory=lambda: _env_list("LIGHTER_FARMER_ASSET_POOL", ASSET_POOL_DEFAULT)
    )
    asset_rotation: bool = field(default_factory=lambda: _env_bool("LIGHTER_FARMER_ASSET_ROTATION", True))
    direction_random: bool = field(default_factory=lambda: _env_bool("LIGHTER_FARMER_DIRECTION_RANDOM", False))

    # Jitter (v2 polish)
    size_jitter_pct: float = field(default_factory=lambda: _env_float("LIGHTER_FARMER_SIZE_JITTER_PCT", 0.10))   # ±10%
    hold_jitter_pct: float = field(default_factory=lambda: _env_float("LIGHTER_FARMER_HOLD_JITTER_PCT", 0.20))   # ±20%
    cycle_gap_min: float = field(default_factory=lambda: _env_float("LIGHTER_FARMER_CYCLE_GAP_MIN", 2.0))
    cycle_gap_max: float = field(default_factory=lambda: _env_float("LIGHTER_FARMER_CYCLE_GAP_MAX", 8.0))

    # Hard limits
    min_lighter_collateral: float = field(
        default_factory=lambda: _env_float("LIGHTER_FARMER_MIN_LIGHTER_COLLATERAL", 100.0)
    )
    max_spread_pct: float = field(default_factory=lambda: _env_float("LIGHTER_FARMER_MAX_SPREAD_PCT", 0.005))
    fill_timeout_sec: float = field(default_factory=lambda: _env_float("LIGHTER_FARMER_FILL_TIMEOUT_SEC", 4.0))
    max_consecutive_failures: int = field(
        default_factory=lambda: _env_int("LIGHTER_FARMER_MAX_CONSEC_FAILS", 5)
    )
    close_verify_timeout: float = field(
        default_factory=lambda: _env_float("LIGHTER_FARMER_CLOSE_VERIFY_TIMEOUT", 5.0)
    )


class LighterVolumeFarmer:
    def __init__(self, cfg: FarmerConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.lighter = None  # SubprocessExchangeWrapper
        self.hl = None       # in-process MultiPerpDex
        self._running = False
        self._cycles = 0
        self._round_trips = 0
        self._consec_fails = 0
        self._day_key = self._today()
        self._day_start_lighter_collateral: Optional[float] = None
        self._day_start_hl_collateral: Optional[float] = None
        self._journal_path = DATA_DIR / "lighter_volume_farmer.jsonl"
        self._asset_idx = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        hl_pk = os.environ.get("WALLET_B_PK")
        if not hl_pk:
            raise RuntimeError("WALLET_B_PK env var not set")

        # ---- Lighter via SubprocessExchangeWrapper -----------------
        # config.yaml provides account_id / api_key_id / l1_address / private_key
        # under exchanges.lighter.keys.  We just mirror multi_runner: spawn the
        # bridge in system Python (lighter SDK ships in main_venv).
        config_path = str(REPO_ROOT / "config.yaml")
        venv_python = sys.executable  # we run from main_venv → SDK is here
        self.lighter = SubprocessExchangeWrapper(
            venv_python=venv_python,
            exchange="lighter",
            config_path=config_path,
            account=LIGHTER_ACCOUNT_NAME,
            display_name="lighter_farmer",
        )
        logger.info("starting Lighter bridge subprocess (account=%s)...", LIGHTER_ACCOUNT_NAME)
        await self.lighter.start()

        # ---- HL in-process ----------------------------------------
        hl_kp = SimpleNamespace(
            wallet_address=HL_HEDGE_WALLET,
            wallet_private_key=hl_pk,
            agent_api_address=None,
            agent_api_private_key=None,
            by_agent=False,
            vault_address=None,
            builder_code=None,
            builder_fee_pair=None,
            FrontendMarket=False,
        )
        logger.info("connecting HL hedge venue (%s)...", HL_HEDGE_WALLET)
        self.hl = await create_exchange("hyperliquid", hl_kp)
        logger.info("Lighter bridge + HL connected")

    async def close(self):
        # Lighter bridge first (subprocess)
        if self.lighter is not None:
            try:
                await self.lighter.close()
            except Exception as e:
                logger.debug("lighter.close err: %s", e)
        if self.hl is not None:
            try:
                await self.hl.close()
            except Exception as e:
                logger.debug("hl.close err: %s", e)

    # ------------------------------------------------------------------
    # asset rotation
    # ------------------------------------------------------------------

    def _pick_asset(self) -> str:
        if not self.cfg.asset_rotation or len(self.cfg.asset_pool) <= 1:
            return self.cfg.asset_pool[0] if self.cfg.asset_pool else "BTC"
        asset = self.cfg.asset_pool[self._asset_idx % len(self.cfg.asset_pool)]
        self._asset_idx += 1
        return asset

    def _direction(self) -> str:
        """Return 'lighter_long_hl_short' or 'lighter_short_hl_long'."""
        if self.cfg.direction_random and random.random() < 0.5:
            return "lighter_short_hl_long"
        return "lighter_long_hl_short"

    @staticmethod
    def _hl_symbol(asset: str) -> str:
        return asset.upper()

    @staticmethod
    def _lighter_symbol(asset: str) -> str:
        return asset.upper()

    @staticmethod
    def _round_size(asset: str, size: float) -> float:
        decimals = ASSET_SIZE_DECIMALS.get(asset.upper(), 4)
        # trim down (avoid over-spending notional)
        factor = 10 ** decimals
        return int(size * factor) / factor

    # ------------------------------------------------------------------
    # pre-flight
    # ------------------------------------------------------------------

    async def reconcile(self) -> bool:
        """Return True if clean. False = abort start."""
        # Reconcile each asset in pool — if any has an open position, abort.
        clean = True
        per_asset: Dict[str, Dict[str, Any]] = {}
        for asset in self.cfg.asset_pool:
            try:
                lpos, hpos = await asyncio.gather(
                    self.lighter.get_position(self._lighter_symbol(asset)),
                    self.hl.get_position(self._hl_symbol(asset)),
                    return_exceptions=True,
                )
            except Exception as e:
                logger.error("reconcile gather %s err: %s", asset, e)
                return False
            if isinstance(lpos, Exception):
                logger.warning("lighter.get_position(%s) err: %s", asset, lpos)
                lpos = None
            if isinstance(hpos, Exception):
                logger.warning("hl.get_position(%s) err: %s", asset, hpos)
                hpos = None
            per_asset[asset] = {"lighter": lpos, "hl": hpos}
            if lpos or hpos:
                clean = False

        try:
            lcol = await self.lighter.get_collateral()
        except Exception as e:
            logger.error("lighter.get_collateral failed: %s", e)
            return False
        try:
            hcol = await self.hl.get_collateral()
        except Exception as e:
            logger.error("hl.get_collateral failed: %s", e)
            return False

        # SubprocessWrapper unwraps to scalar; in-process HL returns dict
        lighter_total = float(lcol) if not isinstance(lcol, dict) else float(lcol.get("total_collateral", 0.0))
        hl_total = float(hcol.get("total_collateral", 0.0)) if isinstance(hcol, dict) else float(hcol)

        logger.info(
            "reconcile: lighter_col=$%.2f hl_col=$%.2f  per-asset=%s",
            lighter_total, hl_total, per_asset,
        )
        self._day_start_lighter_collateral = lighter_total
        self._day_start_hl_collateral = hl_total

        if not clean:
            msg_parts = [f"{a}: L={d['lighter']} H={d['hl']}" for a, d in per_asset.items() if d["lighter"] or d["hl"]]
            logger.error("RECONCILE FAIL: pre-existing positions: %s", "; ".join(msg_parts))
            await notify(
                f"<b>LighterFarmer RECONCILE FAIL</b>\n"
                f"Pre-existing positions:\n<code>{'; '.join(msg_parts)}</code>\n"
                f"Close manually before restart.",
                dedup_key="lighter_farmer_reconcile_fail",
            )
            return False

        if lighter_total < self.cfg.min_lighter_collateral:
            logger.error(
                "lighter collateral $%.2f < $%.2f min",
                lighter_total, self.cfg.min_lighter_collateral,
            )
            await notify(
                f"<b>LighterFarmer START FAIL</b>\n"
                f"Lighter collateral ${lighter_total:.2f} < ${self.cfg.min_lighter_collateral:.0f} min",
                dedup_key="lighter_farmer_min_col_fail",
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
            lcol = await self.lighter.get_collateral()
            lighter_total = float(lcol) if not isinstance(lcol, dict) else float(lcol.get("total_collateral", 0.0))
        except Exception as e:
            return f"get lighter collateral failed: {e}"
        if lighter_total < self.cfg.min_lighter_collateral:
            return f"lighter collateral ${lighter_total:.2f} < ${self.cfg.min_lighter_collateral:.0f}"

        try:
            hcol = await self.hl.get_collateral()
            hl_total = float(hcol.get("total_collateral", 0.0)) if isinstance(hcol, dict) else float(hcol)
        except Exception as e:
            return f"get hl collateral failed: {e}"

        if (
            self._day_start_lighter_collateral is not None
            and self._day_start_hl_collateral is not None
        ):
            day_pnl = (lighter_total + hl_total) - (
                self._day_start_lighter_collateral + self._day_start_hl_collateral
            )
            if day_pnl < self.cfg.daily_stop_usd:
                return f"daily PnL ${day_pnl:.2f} < ${self.cfg.daily_stop_usd:.2f}"

        # roll-over day
        if self._today() != self._day_key:
            self._day_key = self._today()
            self._round_trips = 0
            self._day_start_lighter_collateral = lighter_total
            self._day_start_hl_collateral = hl_total

        return None

    # ------------------------------------------------------------------
    # one round-trip
    # ------------------------------------------------------------------

    async def _fetch_marks(self, asset: str) -> Optional[tuple]:
        try:
            lighter_mark, hl_mark = await asyncio.gather(
                self.lighter.get_mark_price(self._lighter_symbol(asset)),
                self.hl.get_mark_price(self._hl_symbol(asset)),
            )
        except Exception as e:
            logger.error("fetch marks(%s) failed: %s", asset, e)
            return None

        try:
            lighter_mark = float(lighter_mark) if lighter_mark is not None else 0.0
            hl_mark = float(hl_mark) if hl_mark is not None else 0.0
        except (TypeError, ValueError):
            logger.error("invalid mark types: lighter=%s hl=%s", lighter_mark, hl_mark)
            return None
        if lighter_mark <= 0 or hl_mark <= 0:
            logger.error("invalid marks: lighter=%s hl=%s", lighter_mark, hl_mark)
            return None
        spread = abs(lighter_mark - hl_mark) / ((lighter_mark + hl_mark) / 2.0)
        if spread > self.cfg.max_spread_pct:
            logger.warning(
                "spread %.4f > cap %.4f (lighter=%.4f hl=%.4f) for %s",
                spread, self.cfg.max_spread_pct, lighter_mark, hl_mark, asset,
            )
            return None
        return lighter_mark, hl_mark, spread

    def _size_units(self, asset: str, mid_price: float) -> float:
        # jitter notional
        jitter = 1.0
        if self.cfg.size_jitter_pct > 0:
            jitter += random.uniform(-self.cfg.size_jitter_pct, self.cfg.size_jitter_pct)
        notional = max(20.0, self.cfg.position_size_usd * jitter)
        raw = notional / mid_price
        return self._round_size(asset, raw)

    def _hold_seconds(self) -> int:
        if self.cfg.hold_jitter_pct <= 0:
            return self.cfg.interval_sec
        jitter = 1.0 + random.uniform(-self.cfg.hold_jitter_pct, self.cfg.hold_jitter_pct)
        return max(30, int(self.cfg.interval_sec * jitter))

    async def _open_both(
        self, asset: str, size: float, direction: str
    ) -> Dict[str, Any]:
        t0 = time.time()
        lighter_side = "buy" if direction == "lighter_long_hl_short" else "sell"
        hl_side = "sell" if direction == "lighter_long_hl_short" else "buy"

        try:
            lres, hres = await asyncio.gather(
                self.lighter.create_order(
                    self._lighter_symbol(asset), lighter_side, size, order_type="market"
                ),
                self.hl.create_order(
                    self._hl_symbol(asset), hl_side, size, order_type="market"
                ),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("open gather failed: %s", e)
            return {"ok": False, "error": str(e)}
        lighter_err = lres if isinstance(lres, Exception) else None
        hl_err = hres if isinstance(hres, Exception) else None

        # verify positions exist
        await asyncio.sleep(1.5)
        deadline = t0 + self.cfg.fill_timeout_sec
        lighter_pos = None
        hl_pos = None
        while time.time() < deadline:
            lp, hp = await asyncio.gather(
                self.lighter.get_position(self._lighter_symbol(asset)),
                self.hl.get_position(self._hl_symbol(asset)),
                return_exceptions=True,
            )
            lighter_pos = lp if not isinstance(lp, Exception) else None
            hl_pos = hp if not isinstance(hp, Exception) else None
            if lighter_pos and hl_pos:
                break
            await asyncio.sleep(0.5)

        return {
            "ok": bool(lighter_pos and hl_pos),
            "lighter_order": None if lighter_err else lres,
            "hl_order": None if hl_err else hres,
            "lighter_err": str(lighter_err) if lighter_err else None,
            "hl_err": str(hl_err) if hl_err else None,
            "lighter_pos": lighter_pos,
            "hl_pos": hl_pos,
            "open_latency": time.time() - t0,
        }

    async def _close_both(self, asset: str) -> Dict[str, Any]:
        t0 = time.time()
        lpos, hpos = await asyncio.gather(
            self.lighter.get_position(self._lighter_symbol(asset)),
            self.hl.get_position(self._hl_symbol(asset)),
            return_exceptions=True,
        )
        if isinstance(lpos, Exception):
            lpos = None
        if isinstance(hpos, Exception):
            hpos = None

        try:
            await asyncio.gather(
                self.lighter.close_position(self._lighter_symbol(asset), lpos)
                if lpos else asyncio.sleep(0),
                self.hl.close_position(self._hl_symbol(asset), hpos)
                if hpos else asyncio.sleep(0),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("close gather failed: %s", e)

        # verify flat
        deadline = time.time() + self.cfg.close_verify_timeout
        lp = hp = None
        while time.time() < deadline:
            lp, hp = await asyncio.gather(
                self.lighter.get_position(self._lighter_symbol(asset)),
                self.hl.get_position(self._hl_symbol(asset)),
                return_exceptions=True,
            )
            if isinstance(lp, Exception):
                lp = None
            if isinstance(hp, Exception):
                hp = None
            if not lp and not hp:
                break
            await asyncio.sleep(0.5)

        return {
            "ok": (lp is None) and (hp is None),
            "lighter_residual": lp,
            "hl_residual": hp,
            "close_latency": time.time() - t0,
        }

    async def _round_trip(self) -> Dict[str, Any]:
        asset = self._pick_asset()
        direction = self._direction()
        marks = await self._fetch_marks(asset)
        if marks is None:
            return {"ok": False, "stage": "fetch_marks", "asset": asset}
        lighter_mark, hl_mark, spread = marks
        mid = (lighter_mark + hl_mark) / 2.0
        size_units = self._size_units(asset, mid)
        if size_units <= 0:
            return {"ok": False, "stage": "size_zero", "asset": asset, "mid": mid}
        notional = size_units * mid
        hold = self._hold_seconds()

        logger.info(
            "OPEN %s %s: size=%.6f (~$%.2f) lighter=%.4f hl=%.4f spread=%.4f%% hold=%ds",
            asset, direction, size_units, notional,
            lighter_mark, hl_mark, spread * 100, hold,
        )

        if self.dry_run:
            logger.info(
                "DRY-RUN: would open lighter %s + hl %s for %s, hold %ds, close both",
                "buy" if direction == "lighter_long_hl_short" else "sell",
                "sell" if direction == "lighter_long_hl_short" else "buy",
                asset, hold,
            )
            return {
                "ok": True,
                "dry_run": True,
                "asset": asset,
                "direction": direction,
                "lighter_mark": lighter_mark,
                "hl_mark": hl_mark,
                "size_units": size_units,
                "notional": notional,
                "hold_sec": hold,
            }

        open_res = await self._open_both(asset, size_units, direction)
        if not open_res.get("ok"):
            logger.error("OPEN FAILED: %s", open_res)
            await self._emergency_close(asset)
            return {"ok": False, "stage": "open", "asset": asset, "detail": open_res}

        lpos = open_res["lighter_pos"] or {}
        hpos = open_res["hl_pos"] or {}
        try:
            lighter_entry = float(lpos.get("entry_price", 0))
        except (TypeError, ValueError):
            lighter_entry = 0.0
        try:
            hl_entry = float(hpos.get("entry_price", 0))
        except (TypeError, ValueError):
            hl_entry = 0.0
        try:
            delta_imbalance = abs(float(lpos.get("size", 0)) - float(hpos.get("size", 0)))
        except (TypeError, ValueError):
            delta_imbalance = float("nan")

        logger.info(
            "FILLED: lighter_entry=%.4f hl_entry=%.4f |size_delta|=%.6f latency=%.2fs",
            lighter_entry, hl_entry, delta_imbalance, open_res["open_latency"],
        )

        logger.info("HOLD %ds...", hold)
        await asyncio.sleep(hold)

        close_res = await self._close_both(asset)
        if not close_res.get("ok"):
            logger.error("CLOSE INCOMPLETE: %s", close_res)
            await notify(
                f"<b>LighterFarmer CLOSE FAIL</b>\n"
                f"asset={asset}\n"
                f"lighter_residual={close_res.get('lighter_residual')}\n"
                f"hl_residual={close_res.get('hl_residual')}\n"
                f"Manual intervention may be needed.",
                dedup_key="lighter_farmer_close_fail",
            )
            return {
                "ok": False,
                "stage": "close",
                "asset": asset,
                "open": open_res,
                "close": close_res,
            }

        try:
            lcol, hcol = await asyncio.gather(
                self.lighter.get_collateral(),
                self.hl.get_collateral(),
            )
            lighter_total = float(lcol) if not isinstance(lcol, dict) else float(lcol.get("total_collateral", 0.0))
            hl_total = float(hcol.get("total_collateral", 0.0)) if isinstance(hcol, dict) else float(hcol)
        except Exception:
            lighter_total = hl_total = float("nan")

        return {
            "ok": True,
            "asset": asset,
            "direction": direction,
            "lighter_mark": lighter_mark,
            "hl_mark": hl_mark,
            "spread_pct": spread * 100,
            "size_units": size_units,
            "notional": notional,
            "lighter_entry": lighter_entry,
            "hl_entry": hl_entry,
            "delta_imbalance": delta_imbalance,
            "open_latency": open_res["open_latency"],
            "close_latency": close_res["close_latency"],
            "hold_sec": hold,
            "lighter_collateral_after": lighter_total,
            "hl_collateral_after": hl_total,
        }

    async def _emergency_close(self, asset: str):
        logger.warning("EMERGENCY CLOSE triggered for %s", asset)
        try:
            lpos, hpos = await asyncio.gather(
                self.lighter.get_position(self._lighter_symbol(asset)),
                self.hl.get_position(self._hl_symbol(asset)),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("emergency get_position failed: %s", e)
            lpos = hpos = None
        if isinstance(lpos, Exception):
            lpos = None
        if isinstance(hpos, Exception):
            hpos = None
        tasks = []
        if lpos:
            tasks.append(self.lighter.close_position(self._lighter_symbol(asset), lpos))
        if hpos:
            tasks.append(self.hl.close_position(self._hl_symbol(asset), hpos))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await notify(
            f"<b>LighterFarmer EMERGENCY CLOSE</b>\n"
            f"asset={asset}\n"
            f"lighter_pos={lpos}\nhl_pos={hpos}\n"
            f"Farmer will STOP after this.",
            dedup_key="lighter_farmer_emergency_close",
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
            f"<b>LighterFarmer START</b> ({'DRY' if self.dry_run else 'LIVE'})\n"
            f"size=${self.cfg.position_size_usd:.0f}/leg  hold={self.cfg.interval_sec}s±{self.cfg.hold_jitter_pct*100:.0f}%  "
            f"cap={self.cfg.daily_cap}/day  stop=${self.cfg.daily_stop_usd:.2f}\n"
            f"assets={list(self.cfg.asset_pool)} rotation={self.cfg.asset_rotation}",
            dedup_key="lighter_farmer_start",
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
                    f"<b>LighterFarmer STOP</b>\nreason: <code>{reason}</code>",
                    dedup_key=f"lighter_farmer_stop_{reason[:20]}",
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
                        f"<b>LighterFarmer STOP</b>\n"
                        f"{self._consec_fails} consecutive failures",
                        dedup_key="lighter_farmer_consec_fail",
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
                        help="override hold interval seconds (default: env or 300)")
    parser.add_argument("--size-usd", type=float, default=None,
                        help="override per-leg notional USD (default: env or 50)")
    parser.add_argument("--asset", type=str, default=None,
                        help="override asset pool (comma-sep, e.g. 'BTC,ETH')")
    parser.add_argument("--reconcile-only", action="store_true",
                        help="run reconcile + connect, then exit (sanity check)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # load .env if present (repo-local)
    try:
        from dotenv import load_dotenv
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            load_dotenv(env_path)
    except ImportError:
        pass

    cfg = FarmerConfig()
    if args.interval is not None:
        cfg.interval_sec = int(args.interval)
    if args.size_usd is not None:
        cfg.position_size_usd = float(args.size_usd)
    if args.asset:
        items = tuple(x.strip().upper() for x in args.asset.split(",") if x.strip())
        if items:
            cfg.asset_pool = items

    farmer = LighterVolumeFarmer(cfg, dry_run=args.dry_run)

    # signal handling (POSIX). Windows: ignore.
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
            f"<b>LighterFarmer CONNECT FAIL</b>\n<code>{e}</code>",
            dedup_key="lighter_farmer_connect_fail",
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
