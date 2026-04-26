"""
Rise.trade Volume Farmer V2 — sybil-resistant cross-wallet delta-neutral hedge.

Rise (wallet 0xWALLA..., WALLET_A_PK, mainnet)
  +
HL   (wallet 0xWALLB..., WALLET_B_PK)

V2 randomization (anti-fingerprint):
  * Asset rotation: BTC / ETH / SOL / HYPE (env RISE_FARMER_ASSETS)
  * Size jitter: ±N% on USD notional (env RISE_FARMER_SIZE_JITTER)
  * Hold jitter: random sec in [HOLD_MIN, HOLD_MAX]
  * Cycle gap jitter: random sec in [CYCLE_GAP_MIN, CYCLE_GAP_MAX]
  * Entry micro-jitter: 50~500ms random sleep before placing each leg
  * Direction random: Rise long+HL short OR Rise short+HL long (50/50)
  * Cross-asset hedge (default OFF): non-perfect hedge across assets

Per cycle:
  1) safety checks (kill switches)
  2) random asset/size/direction/hold pick
  3) fetch mark from both venues, abort if spread > 0.5%
  4) micro-jittered near-simultaneous open via asyncio.gather
  5) verify both legs filled within 3s, else STOP + alert
  6) hold randomized seconds
  7) asyncio.gather(rise.close, hl.close) reduce-only
  8) verify both flat, journal cycle
  9) random gap [CYCLE_GAP_MIN..CYCLE_GAP_MAX] before next round

Kill switches:
  - Daily PnL < RISE_FARMER_DAILY_STOP_USD (default -$3)
  - Both legs not filled in 3s
  - Rise collateral < $100
  - 5 consecutive fill failures
  - File data/KILL_RISE_FARMER exists

Usage:
  cd <INSTALL_DIR>/multi-perp-dex
  source main_venv/bin/activate
  python -m strategies.rise_volume_farmer --dry-run
  python -m strategies.rise_volume_farmer --live
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
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Allow "python -m strategies.rise_volume_farmer" from the repo root,
# OR direct execution from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mpdex.factory import create_exchange  # noqa: E402

try:
    from strategies.notifier import notify  # noqa: E402
except Exception:  # pragma: no cover
    async def notify(*args, **kwargs):  # type: ignore
        return False


logger = logging.getLogger("rise_volume_farmer")

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

RISE_WALLET = "<EVM_ADDRESS>"
HL_WALLET = "<EVM_ADDRESS>"


# ---------------------------------------------------------------------------
# Asset metadata
# ---------------------------------------------------------------------------
# Each entry: rise symbol, hl symbol, size decimals (round to), min lot, min notional
ASSET_META: Dict[str, Dict[str, Any]] = {
    "BTC":  {"rise": "BTC-PERP",  "hl": "BTC",  "decimals": 5, "min_size": 0.00015, "min_notional": 11.0},
    "ETH":  {"rise": "ETH-PERP",  "hl": "ETH",  "decimals": 4, "min_size": 0.001,   "min_notional": 11.0},
    "SOL":  {"rise": "SOL-PERP",  "hl": "SOL",  "decimals": 2, "min_size": 0.1,     "min_notional": 11.0},
    "HYPE": {"rise": "HYPE-PERP", "hl": "HYPE", "decimals": 2, "min_size": 0.1,     "min_notional": 11.0},
}


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
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_assets(default: List[str]) -> List[str]:
    raw = os.environ.get("RISE_FARMER_ASSETS")
    if not raw:
        return default
    out = []
    for tok in raw.split(","):
        sym = tok.strip().upper()
        if sym in ASSET_META:
            out.append(sym)
        elif sym:
            logger.warning("unknown asset in RISE_FARMER_ASSETS: %s (skipped)", sym)
    return out or default


@dataclass
class FarmerConfig:
    # Base notional ($/leg) — per-cycle size_usd is jittered around this.
    position_size_usd: float = field(
        default_factory=lambda: _env_float("RISE_FARMER_SIZE_USD",
                                           _env_float("RISE_FARMER_POSITION_SIZE_USD", 50.0))
    )
    leverage: int = field(default_factory=lambda: _env_int("RISE_FARMER_LEVERAGE", 3))

    # Hold (per-cycle): random.randint(hold_min, hold_max)
    # Falls back to legacy RISE_FARMER_INTERVAL_SEC for hold_min if HOLD_MIN unset.
    hold_min_sec: int = field(default_factory=lambda: _env_int(
        "RISE_FARMER_HOLD_MIN", _env_int("RISE_FARMER_INTERVAL_SEC", 180)))
    hold_max_sec: int = field(default_factory=lambda: _env_int("RISE_FARMER_HOLD_MAX", 600))

    # Cycle gap (between round-trips): random.uniform(gap_min, gap_max)
    cycle_gap_min: int = field(default_factory=lambda: _env_int("RISE_FARMER_CYCLE_GAP_MIN", 60))
    cycle_gap_max: int = field(default_factory=lambda: _env_int("RISE_FARMER_CYCLE_GAP_MAX", 180))

    # Size jitter: actual_usd = size_usd * uniform(1-jit, 1+jit)
    size_jitter: float = field(default_factory=lambda: _env_float("RISE_FARMER_SIZE_JITTER", 0.30))

    # Asset rotation
    assets: List[str] = field(default_factory=lambda: _env_assets(["BTC", "ETH", "SOL", "HYPE"]))

    # Cross-asset hedge (DANGEROUS — leaves delta exposure). Default OFF.
    cross_asset_hedge: bool = field(default_factory=lambda: _env_bool("RISE_FARMER_CROSS_ASSET_HEDGE", False))

    # Daily limits
    daily_cap: int = field(default_factory=lambda: _env_int("RISE_FARMER_DAILY_CAP", 20))
    daily_stop_usd: float = field(
        default_factory=lambda: _env_float("RISE_FARMER_DAILY_STOP_USD", -3.0)
    )

    kill_file: str = field(
        default_factory=lambda: os.environ.get(
            "RISE_FARMER_KILL_FILE", "data/KILL_RISE_FARMER"
        )
    )
    min_rise_collateral: float = 100.0
    max_spread_pct: float = 0.005   # 0.5%
    fill_timeout_sec: float = 3.0
    max_consecutive_failures: int = 5
    close_verify_timeout: float = 5.0

    # Entry micro-jitter range (per leg)
    entry_jitter_min_ms: int = 50
    entry_jitter_max_ms: int = 500

    def __post_init__(self):
        # Sanity: hold_min <= hold_max
        if self.hold_min_sec > self.hold_max_sec:
            self.hold_min_sec, self.hold_max_sec = self.hold_max_sec, self.hold_min_sec
        if self.cycle_gap_min > self.cycle_gap_max:
            self.cycle_gap_min, self.cycle_gap_max = self.cycle_gap_max, self.cycle_gap_min
        # Clamp jitter
        if self.size_jitter < 0:
            self.size_jitter = 0.0
        if self.size_jitter > 0.9:
            self.size_jitter = 0.9
        # Ensure assets non-empty
        if not self.assets:
            self.assets = ["BTC"]


class RiseVolumeFarmer:
    def __init__(self, cfg: FarmerConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.rise = None
        self.hl = None
        self._running = False
        self._cycles = 0
        self._round_trips = 0
        self._consec_fails = 0
        self._day_key = self._today()
        self._day_start_rise_collateral: Optional[float] = None
        self._day_start_hl_collateral: Optional[float] = None
        self._journal_path = DATA_DIR / "rise_volume_farmer.jsonl"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self):
        rise_pk = os.environ.get("WALLET_A_PK")
        hl_pk = os.environ.get("WALLET_B_PK")
        if not rise_pk:
            raise RuntimeError("WALLET_A_PK env var not set")
        if not hl_pk:
            raise RuntimeError("WALLET_B_PK env var not set")

        rise_kp = SimpleNamespace(
            wallet_address=RISE_WALLET,
            private_key=rise_pk,
            base_url="https://api.rise.trade",
            timeout=10.0,
        )
        hl_kp = SimpleNamespace(
            wallet_address=HL_WALLET,
            wallet_private_key=hl_pk,
            agent_api_address=None,
            agent_api_private_key=None,
            by_agent=False,
            vault_address=None,
            builder_code=None,
            builder_fee_pair=None,
            FrontendMarket=False,
        )

        logger.info("connecting to Rise (%s) + HL (%s)...", RISE_WALLET, HL_WALLET)
        self.rise = await create_exchange("risex", rise_kp)
        self.hl = await create_exchange("hyperliquid", hl_kp)
        logger.info(
            "both exchanges connected | assets=%s size=$%.0f±%.0f%% hold=%d~%ds gap=%d~%ds cross=%s",
            self.cfg.assets,
            self.cfg.position_size_usd,
            self.cfg.size_jitter * 100,
            self.cfg.hold_min_sec, self.cfg.hold_max_sec,
            self.cfg.cycle_gap_min, self.cfg.cycle_gap_max,
            self.cfg.cross_asset_hedge,
        )

    async def close(self):
        for ex_name, ex in [("rise", self.rise), ("hl", self.hl)]:
            if ex is None:
                continue
            try:
                await ex.close()
            except Exception as e:
                logger.debug("close(%s) err: %s", ex_name, e)

    # ------------------------------------------------------------------
    # pre-flight
    # ------------------------------------------------------------------

    async def reconcile(self) -> bool:
        """Return True if clean (no pre-existing positions on any tracked asset)."""
        rise_col = await self.rise.get_collateral()
        hl_col = await self.hl.get_collateral()
        rise_total = float(rise_col.get("total_collateral", 0.0))
        hl_total = float(hl_col.get("total_collateral", 0.0))
        self._day_start_rise_collateral = rise_total
        self._day_start_hl_collateral = hl_total

        # Check positions across all configured assets
        dirty: List[str] = []
        for asset in self.cfg.assets:
            meta = ASSET_META[asset]
            try:
                rise_pos = await self.rise.get_position(meta["rise"])
            except Exception as e:
                logger.warning("reconcile rise %s err: %s", asset, e)
                rise_pos = None
            try:
                hl_pos = await self.hl.get_position(meta["hl"])
            except Exception as e:
                logger.warning("reconcile hl %s err: %s", asset, e)
                hl_pos = None
            if rise_pos or hl_pos:
                dirty.append(f"{asset}: rise={rise_pos} hl={hl_pos}")

        logger.info(
            "reconcile: rise col=$%.2f  hl col=$%.2f  dirty=%d",
            rise_total, hl_total, len(dirty),
        )

        if dirty:
            msg_lines = "\n".join(dirty)
            logger.error("RECONCILE FAIL: pre-existing positions:\n%s", msg_lines)
            await notify(
                "<b>RiseFarmer RECONCILE FAIL</b>\n"
                f"<code>{msg_lines}</code>\n"
                "Close manually before restart.",
                dedup_key="rise_farmer_reconcile_fail",
            )
            return False

        if rise_total < self.cfg.min_rise_collateral:
            logger.error("rise collateral $%.2f < $%.2f min", rise_total, self.cfg.min_rise_collateral)
            await notify(
                f"<b>RiseFarmer START FAIL</b>\n"
                f"Rise collateral ${rise_total:.2f} < ${self.cfg.min_rise_collateral:.0f} min",
                dedup_key="rise_farmer_min_col_fail",
            )
            return False

        return True

    # ------------------------------------------------------------------
    # kill switches
    # ------------------------------------------------------------------

    async def _preflight(self) -> Optional[str]:
        """Return reason string if should stop, else None."""
        # 1) kill file
        kill_path = (REPO_ROOT / self.cfg.kill_file).resolve()
        if kill_path.exists():
            return f"kill file present ({kill_path})"

        # 2) consecutive failures
        if self._consec_fails >= self.cfg.max_consecutive_failures:
            return f"{self._consec_fails} consecutive failures (>= {self.cfg.max_consecutive_failures})"

        # 3) daily cap
        if self._round_trips >= self.cfg.daily_cap:
            return f"daily round-trip cap reached ({self._round_trips}/{self.cfg.daily_cap})"

        # 4) Rise collateral floor
        try:
            col = await self.rise.get_collateral()
            rise_total = float(col.get("total_collateral", 0.0))
        except Exception as e:
            return f"get rise collateral failed: {e}"
        if rise_total < self.cfg.min_rise_collateral:
            return f"rise collateral ${rise_total:.2f} < ${self.cfg.min_rise_collateral:.0f}"

        # 5) daily PnL stop
        try:
            hl_col = await self.hl.get_collateral()
            hl_total = float(hl_col.get("total_collateral", 0.0))
        except Exception as e:
            return f"get hl collateral failed: {e}"
        if (
            self._day_start_rise_collateral is not None
            and self._day_start_hl_collateral is not None
        ):
            day_pnl = (rise_total + hl_total) - (
                self._day_start_rise_collateral + self._day_start_hl_collateral
            )
            if day_pnl < self.cfg.daily_stop_usd:
                return f"daily PnL ${day_pnl:.2f} < ${self.cfg.daily_stop_usd:.2f}"

        # 6) roll-over day
        if self._today() != self._day_key:
            self._day_key = self._today()
            self._round_trips = 0
            self._day_start_rise_collateral = rise_total
            self._day_start_hl_collateral = hl_total

        return None

    # ------------------------------------------------------------------
    # one round-trip
    # ------------------------------------------------------------------

    async def _fetch_marks(self, asset: str) -> Optional[tuple]:
        meta = ASSET_META[asset]
        try:
            rise_mark, hl_mark = await asyncio.gather(
                self.rise.get_mark_price(meta["rise"]),
                self.hl.get_mark_price(meta["hl"]),
            )
        except Exception as e:
            logger.error("fetch marks (%s) failed: %s", asset, e)
            return None
        if not rise_mark or not hl_mark:
            logger.error("invalid marks (%s): rise=%s hl=%s", asset, rise_mark, hl_mark)
            return None
        spread = abs(float(rise_mark) - float(hl_mark)) / ((float(rise_mark) + float(hl_mark)) / 2.0)
        if spread > self.cfg.max_spread_pct:
            logger.warning(
                "spread %.4f > cap %.4f (%s rise=%.4f hl=%.4f)",
                spread, self.cfg.max_spread_pct, asset, float(rise_mark), float(hl_mark),
            )
            return None
        return float(rise_mark), float(hl_mark), spread

    def _calc_size(self, asset: str, mid_price: float, size_usd: float) -> Tuple[float, float]:
        """Return (size_lot, actual_notional_usd) honoring asset decimals + min_size."""
        meta = ASSET_META[asset]
        if mid_price <= 0:
            return 0.0, 0.0
        raw = size_usd / mid_price
        rounded = round(raw, meta["decimals"])
        # enforce min size
        if rounded < meta["min_size"]:
            rounded = meta["min_size"]
        notional = rounded * mid_price
        # enforce min notional (Rise/HL refuse < ~$11)
        if notional < meta["min_notional"]:
            # bump until passes
            inc = 10 ** (-meta["decimals"])
            while rounded * mid_price < meta["min_notional"]:
                rounded = round(rounded + inc, meta["decimals"])
                if rounded > raw * 5:  # safety cap
                    break
            notional = rounded * mid_price
        return rounded, notional

    async def _open_both(
        self, asset: str, size_lot: float, rise_side: str, hl_side: str,
        cross_asset: Optional[str] = None, cross_size_lot: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Place both legs simultaneously with micro-jittered timing.

        cross_asset/cross_size_lot: when set, HL leg trades a different asset
        for cross-asset hedge experiments (DANGEROUS, default off).
        """
        meta = ASSET_META[asset]
        hl_meta = ASSET_META[cross_asset] if cross_asset else meta
        hl_size = cross_size_lot if cross_size_lot is not None else size_lot

        # Per-leg micro-jitter (independently sample to break simultaneity)
        rise_jit_ms = random.randint(self.cfg.entry_jitter_min_ms, self.cfg.entry_jitter_max_ms)
        hl_jit_ms = random.randint(self.cfg.entry_jitter_min_ms, self.cfg.entry_jitter_max_ms)

        async def _rise_leg():
            await asyncio.sleep(rise_jit_ms / 1000.0)
            return await self.rise.create_order(meta["rise"], rise_side, size_lot, order_type="market")

        async def _hl_leg():
            await asyncio.sleep(hl_jit_ms / 1000.0)
            return await self.hl.create_order(hl_meta["hl"], hl_side, hl_size, order_type="market")

        t0 = time.time()
        try:
            rise_res, hl_res = await asyncio.gather(_rise_leg(), _hl_leg(), return_exceptions=True)
        except Exception as e:
            logger.error("open gather failed: %s", e)
            return {"ok": False, "error": str(e)}
        rise_err = rise_res if isinstance(rise_res, Exception) else None
        hl_err = hl_res if isinstance(hl_res, Exception) else None

        # verify positions exist
        await asyncio.sleep(1.5)
        deadline = t0 + max(self.cfg.fill_timeout_sec,
                            (max(rise_jit_ms, hl_jit_ms) / 1000.0) + 3.0)
        rise_pos = None
        hl_pos = None
        while time.time() < deadline:
            rp, hp = await asyncio.gather(
                self.rise.get_position(meta["rise"]),
                self.hl.get_position(hl_meta["hl"]),
                return_exceptions=True,
            )
            rise_pos = rp if not isinstance(rp, Exception) else None
            hl_pos = hp if not isinstance(hp, Exception) else None
            if rise_pos and hl_pos:
                break
            await asyncio.sleep(0.5)

        return {
            "ok": bool(rise_pos and hl_pos),
            "rise_order": None if rise_err else rise_res,
            "hl_order": None if hl_err else hl_res,
            "rise_err": str(rise_err) if rise_err else None,
            "hl_err": str(hl_err) if hl_err else None,
            "rise_pos": rise_pos,
            "hl_pos": hl_pos,
            "open_latency": time.time() - t0,
            "rise_jit_ms": rise_jit_ms,
            "hl_jit_ms": hl_jit_ms,
        }

    async def _close_both(
        self, asset: str, cross_asset: Optional[str] = None
    ) -> Dict[str, Any]:
        meta = ASSET_META[asset]
        hl_meta = ASSET_META[cross_asset] if cross_asset else meta
        t0 = time.time()
        rise_pos, hl_pos = await asyncio.gather(
            self.rise.get_position(meta["rise"]),
            self.hl.get_position(hl_meta["hl"]),
            return_exceptions=True,
        )
        if isinstance(rise_pos, Exception):
            rise_pos = None
        if isinstance(hl_pos, Exception):
            hl_pos = None

        try:
            await asyncio.gather(
                self.rise.close_position(meta["rise"], rise_pos) if rise_pos else asyncio.sleep(0),
                self.hl.close_position(hl_meta["hl"], hl_pos) if hl_pos else asyncio.sleep(0),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("close gather failed: %s", e)

        # verify flat
        deadline = time.time() + self.cfg.close_verify_timeout
        rp = hp = None
        while time.time() < deadline:
            rp, hp = await asyncio.gather(
                self.rise.get_position(meta["rise"]),
                self.hl.get_position(hl_meta["hl"]),
                return_exceptions=True,
            )
            if isinstance(rp, Exception):
                rp = None
            if isinstance(hp, Exception):
                hp = None
            if not rp and not hp:
                break
            await asyncio.sleep(0.5)

        return {
            "ok": (rp is None) and (hp is None),
            "rise_residual": rp,
            "hl_residual": hp,
            "close_latency": time.time() - t0,
        }

    def _pick_cycle_params(self) -> Dict[str, Any]:
        """Random pick: asset, size_usd, hold_sec, direction (and optional cross-asset)."""
        asset = random.choice(self.cfg.assets)

        jit_lo = max(0.0, 1.0 - self.cfg.size_jitter)
        jit_hi = 1.0 + self.cfg.size_jitter
        size_usd = self.cfg.position_size_usd * random.uniform(jit_lo, jit_hi)

        hold_sec = random.randint(self.cfg.hold_min_sec, self.cfg.hold_max_sec)

        # 50/50 direction. "long"=rise buy + hl sell, "short"=rise sell + hl buy.
        direction = random.choice(["long", "short"])

        cross_asset: Optional[str] = None
        cross_size_usd: Optional[float] = None
        if self.cfg.cross_asset_hedge and len(self.cfg.assets) >= 2:
            others = [a for a in self.cfg.assets if a != asset]
            if others:
                cross_asset = random.choice(others)
                # slight $ delta on the hedge leg too
                cross_size_usd = self.cfg.position_size_usd * random.uniform(jit_lo, jit_hi)

        return {
            "asset": asset,
            "size_usd": size_usd,
            "hold_sec": hold_sec,
            "direction": direction,
            "cross_asset": cross_asset,
            "cross_size_usd": cross_size_usd,
        }

    async def _round_trip(self) -> Dict[str, Any]:
        params = self._pick_cycle_params()
        asset = params["asset"]
        size_usd = params["size_usd"]
        hold_sec = params["hold_sec"]
        direction = params["direction"]
        cross_asset: Optional[str] = params["cross_asset"]
        cross_size_usd: Optional[float] = params["cross_size_usd"]

        marks = await self._fetch_marks(asset)
        if marks is None:
            return {"ok": False, "stage": "fetch_marks", "asset": asset}
        rise_mark, hl_mark, spread = marks
        mid = (rise_mark + hl_mark) / 2.0

        size_lot, notional = self._calc_size(asset, mid, size_usd)
        if size_lot <= 0:
            return {"ok": False, "stage": "size_calc", "asset": asset}

        # Cross-asset: compute hedge leg size separately
        cross_size_lot: Optional[float] = None
        cross_notional: Optional[float] = None
        if cross_asset and cross_size_usd is not None:
            cross_marks = await self._fetch_marks(cross_asset)
            if cross_marks is None:
                return {"ok": False, "stage": "fetch_marks_cross", "asset": cross_asset}
            _, hl_cross_mark, _ = cross_marks
            cross_size_lot, cross_notional = self._calc_size(cross_asset, hl_cross_mark, cross_size_usd)

        # Direction → sides
        if direction == "long":
            rise_side, hl_side = "buy", "sell"
        else:
            rise_side, hl_side = "sell", "buy"

        logger.info(
            "OPEN: %s %s size=%s (~$%.2f) rise=%.4f hl=%.4f spread=%.4f%% hold=%ds%s",
            asset, direction.upper(),
            f"{size_lot:.{ASSET_META[asset]['decimals']}f}",
            notional, rise_mark, hl_mark, spread * 100, hold_sec,
            (f"  CROSS={cross_asset} {cross_size_lot}" if cross_asset else ""),
        )

        if self.dry_run:
            logger.info(
                "DRY-RUN: would open %s %s on rise + %s on hl, hold %ds",
                asset, rise_side, hl_side, hold_sec,
            )
            return {
                "ok": True,
                "dry_run": True,
                "asset": asset,
                "direction": direction,
                "rise_mark": rise_mark,
                "hl_mark": hl_mark,
                "size_lot": size_lot,
                "notional": notional,
                "hold_sec": hold_sec,
                "cross_asset": cross_asset,
                "cross_size_lot": cross_size_lot,
            }

        open_res = await self._open_both(
            asset, size_lot, rise_side, hl_side,
            cross_asset=cross_asset, cross_size_lot=cross_size_lot,
        )
        if not open_res.get("ok"):
            logger.error("OPEN FAILED (%s %s): %s", asset, direction, open_res)
            await self._emergency_close(asset, cross_asset=cross_asset)
            return {
                "ok": False, "stage": "open", "detail": open_res,
                "asset": asset, "direction": direction,
            }

        rise_pos = open_res["rise_pos"]
        hl_pos = open_res["hl_pos"]
        rise_entry = float(rise_pos.get("entry_price", 0)) if rise_pos else 0.0
        hl_entry = float(hl_pos.get("entry_price", 0)) if hl_pos else 0.0
        try:
            delta_imbalance = abs(float(rise_pos.get("size", 0)) - float(hl_pos.get("size", 0)))
        except Exception:
            delta_imbalance = float("nan")
        logger.info(
            "FILLED %s %s: rise_entry=%.4f hl_entry=%.4f |size_delta|=%.6f latency=%.2fs jit=%dms/%dms",
            asset, direction, rise_entry, hl_entry, delta_imbalance,
            open_res["open_latency"], open_res.get("rise_jit_ms", 0), open_res.get("hl_jit_ms", 0),
        )

        # hold (randomized)
        logger.info("HOLD %ds (%s %s)...", hold_sec, asset, direction)
        await asyncio.sleep(hold_sec)

        close_res = await self._close_both(asset, cross_asset=cross_asset)
        if not close_res.get("ok"):
            logger.error("CLOSE INCOMPLETE (%s): %s", asset, close_res)
            await notify(
                f"<b>RiseFarmer CLOSE FAIL</b>\n"
                f"asset={asset} direction={direction}\n"
                f"rise_residual={close_res.get('rise_residual')}\n"
                f"hl_residual={close_res.get('hl_residual')}\n"
                f"Manual intervention may be needed.",
                dedup_key="rise_farmer_close_fail",
            )
            return {
                "ok": False, "stage": "close",
                "asset": asset, "direction": direction,
                "open": open_res, "close": close_res,
            }

        # PnL estimate from collateral delta
        try:
            rise_col, hl_col = await asyncio.gather(
                self.rise.get_collateral(),
                self.hl.get_collateral(),
            )
            rise_total = float(rise_col.get("total_collateral", 0.0))
            hl_total = float(hl_col.get("total_collateral", 0.0))
        except Exception:
            rise_total = hl_total = float("nan")

        return {
            "ok": True,
            "asset": asset,
            "direction": direction,
            "rise_mark": rise_mark,
            "hl_mark": hl_mark,
            "spread_pct": spread * 100,
            "size_lot": size_lot,
            "notional": notional,
            "hold_sec": hold_sec,
            "rise_entry": rise_entry,
            "hl_entry": hl_entry,
            "delta_imbalance": delta_imbalance,
            "open_latency": open_res["open_latency"],
            "close_latency": close_res["close_latency"],
            "rise_jit_ms": open_res.get("rise_jit_ms"),
            "hl_jit_ms": open_res.get("hl_jit_ms"),
            "cross_asset": cross_asset,
            "cross_size_lot": cross_size_lot,
            "rise_collateral_after": rise_total,
            "hl_collateral_after": hl_total,
        }

    async def _emergency_close(self, asset: str, cross_asset: Optional[str] = None):
        """Best-effort close both legs (used on open-failure)."""
        meta = ASSET_META[asset]
        hl_meta = ASSET_META[cross_asset] if cross_asset else meta
        logger.warning("EMERGENCY CLOSE triggered (%s%s)",
                       asset, f"/{cross_asset}" if cross_asset else "")
        try:
            rise_pos, hl_pos = await asyncio.gather(
                self.rise.get_position(meta["rise"]),
                self.hl.get_position(hl_meta["hl"]),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("emergency get_position failed: %s", e)
            rise_pos = hl_pos = None
        if isinstance(rise_pos, Exception):
            rise_pos = None
        if isinstance(hl_pos, Exception):
            hl_pos = None
        tasks = []
        if rise_pos:
            tasks.append(self.rise.close_position(meta["rise"], rise_pos))
        if hl_pos:
            tasks.append(self.hl.close_position(hl_meta["hl"], hl_pos))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await notify(
            f"<b>RiseFarmer EMERGENCY CLOSE</b>\n"
            f"asset={asset} cross={cross_asset}\n"
            f"rise_pos={rise_pos}\nhl_pos={hl_pos}\n"
            f"Farmer will STOP after this.",
            dedup_key="rise_farmer_emergency_close",
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
            f"<b>RiseFarmer V2 START</b> ({'DRY' if self.dry_run else 'LIVE'})\n"
            f"assets={','.join(self.cfg.assets)}\n"
            f"size=${self.cfg.position_size_usd:.0f}±{self.cfg.size_jitter*100:.0f}%/leg  "
            f"hold={self.cfg.hold_min_sec}~{self.cfg.hold_max_sec}s  "
            f"gap={self.cfg.cycle_gap_min}~{self.cfg.cycle_gap_max}s\n"
            f"cap={self.cfg.daily_cap}/day  stop=${self.cfg.daily_stop_usd:.2f}  "
            f"cross_asset={self.cfg.cross_asset_hedge}",
            dedup_key="rise_farmer_start",
        )
        self._journal("start", {
            "dry_run": self.dry_run,
            "cfg": self.cfg.__dict__,
        })

        while self._running:
            reason = await self._preflight()
            if reason:
                logger.error("KILL SWITCH: %s", reason)
                await notify(
                    f"<b>RiseFarmer STOP</b>\nreason: <code>{reason}</code>",
                    dedup_key=f"rise_farmer_stop_{reason[:20]}",
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
                        f"<b>RiseFarmer STOP</b>\n"
                        f"{self._consec_fails} consecutive failures",
                        dedup_key="rise_farmer_consec_fail",
                    )
                    break

            # randomized gap between cycles
            if self._running and not self.dry_run:
                gap = random.uniform(self.cfg.cycle_gap_min, self.cfg.cycle_gap_max)
                logger.info("GAP %.1fs before next cycle", gap)
                # break sleep into chunks so kill-file/SIGTERM is responsive
                slept = 0.0
                step = 5.0
                while slept < gap and self._running:
                    chunk = min(step, gap - slept)
                    await asyncio.sleep(chunk)
                    slept += chunk

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
    parser.add_argument("--size-usd", type=float, default=None,
                        help="override per-leg base notional USD (default: env or 50)")
    parser.add_argument("--hold-min", type=int, default=None,
                        help="override min hold seconds (default: env or 180)")
    parser.add_argument("--hold-max", type=int, default=None,
                        help="override max hold seconds (default: env or 600)")
    parser.add_argument("--gap-min", type=int, default=None,
                        help="override min cycle-gap seconds (default: env or 60)")
    parser.add_argument("--gap-max", type=int, default=None,
                        help="override max cycle-gap seconds (default: env or 180)")
    parser.add_argument("--assets", type=str, default=None,
                        help="comma-separated asset list, e.g. BTC,ETH,SOL,HYPE")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s │ %(message)s",
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
    if args.size_usd is not None:
        cfg.position_size_usd = float(args.size_usd)
    if args.hold_min is not None:
        cfg.hold_min_sec = int(args.hold_min)
    if args.hold_max is not None:
        cfg.hold_max_sec = int(args.hold_max)
    if args.gap_min is not None:
        cfg.cycle_gap_min = int(args.gap_min)
    if args.gap_max is not None:
        cfg.cycle_gap_max = int(args.gap_max)
    if args.assets:
        wanted = []
        for tok in args.assets.split(","):
            sym = tok.strip().upper()
            if sym in ASSET_META:
                wanted.append(sym)
        if wanted:
            cfg.assets = wanted
    # re-run sanity
    cfg.__post_init__()

    farmer = RiseVolumeFarmer(cfg, dry_run=args.dry_run)

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
            f"<b>RiseFarmer CONNECT FAIL</b>\n<code>{e}</code>",
            dedup_key="rise_farmer_connect_fail",
        )
        return 2

    try:
        ok = await farmer.reconcile()
        if not ok:
            return 3
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
