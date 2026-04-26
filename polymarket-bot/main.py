"""Polymarket multi-market trading bot orchestrator."""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from config import Config
from strategy import PriceEngine
from market_scanner import MarketScanner
from risk_manager import RiskManager, SizingResult
from data_logger import DataLogger
from dashboard import Dashboard
from paper_optimizer import PaperOptimizer
from copy_trader import CopyTradeTracker
from ml_model import MLModel
from signal_publisher import SignalPublisher
try:
    from auto_redeemer import AutoRedeemer
except ImportError:
    AutoRedeemer = None
from weather_strategy import WeatherStrategy, WeatherConfig
try:
    from predict_client import PredictClient
    from predict_sniper import PredictMarketFetcher, PredictSniper, get_binance_price
    PREDICT_AVAILABLE = True
except ImportError:
    PREDICT_AVAILABLE = False

try:
    from merge_split_arb import MergeSplitArb, MergeSplitConfig
    MERGE_SPLIT_AVAILABLE = True
except ImportError:
    MERGE_SPLIT_AVAILABLE = False

try:
    from reversal_sniper import ReversalSniper, ReversalSniperConfig
    REVERSAL_SNIPE_AVAILABLE = True
except ImportError:
    REVERSAL_SNIPE_AVAILABLE = False

try:
    from mm_strategy import StoikovMM
    MM_AVAILABLE = True
except ImportError:
    MM_AVAILABLE = False

try:
    import notifier as _poly_notifier
except ImportError:
    _poly_notifier = None

logger = logging.getLogger("polybot")


class TradingBot:
    def __init__(self, config: Config, mode: str = "paper"):
        self.config = config
        self.requested_mode = mode
        self.db_logger = DataLogger(config.db_path, busy_timeout_ms=config.db_busy_timeout_ms)
        self.mode, self.mode_note = self._resolve_runtime_mode(mode)
        self.scanner = MarketScanner(config, mode=self.mode)
        self.copy_trader = CopyTradeTracker(config)
        self.market_specs = self.scanner.market_specs
        self.assets = sorted({spec.asset_symbol for spec in self.market_specs})
        self.primary_asset = "BTC" if "BTC" in self.assets else (self.assets[0] if self.assets else "BTC")
        self.price_engines = {asset: PriceEngine(config, asset_symbol=asset) for asset in self.assets}
        self.price_engine = self.price_engines[self.primary_asset]
        self.risk_manager = RiskManager(config, self.db_logger)
        self.dashboard = Dashboard(self.db_logger)
        self.optimizer = PaperOptimizer(config, self.db_logger) if self.mode == "paper" and config.paper_optimizer_enabled else None
        self.ml_model = MLModel(config) if config.ml_enabled else None
        # Signal publisher for perp-dex bridge
        bridge_path = getattr(config, "signal_bridge_path", None)
        min_pub_prob = getattr(config, "signal_publish_min_prob", 0.65)
        self._signal_publisher = SignalPublisher(
            bridge_path=bridge_path,
            min_publish_prob=min_pub_prob,
        ) if getattr(config, "signal_publish_enabled", True) else None
        # Auto redeemer (legacy, may fail on Python <3.11)
        self._redeemer = AutoRedeemer(config) if AutoRedeemer else None
        # Weather market strategy
        self._weather: WeatherStrategy | None = None
        if config.weather_enabled:
            wcfg = WeatherConfig(
                enabled=config.weather_enabled,
                scan_interval_sec=config.weather_scan_interval_sec,
                forecast_refresh_sec=config.weather_forecast_refresh_sec,
                min_edge_threshold=config.weather_min_edge,
                min_liquidity=config.weather_min_liquidity,
                max_bet_size=config.weather_max_bet,
                min_bet_size=config.weather_min_bet,
                kelly_fraction=config.weather_kelly_fraction,
                max_open_weather_positions=config.weather_max_positions,
                target_cities=[c.strip() for c in config.weather_target_cities.split(",") if c.strip()],
            )
            self._weather = WeatherStrategy(wcfg)
        # Predict.fun sniper
        self._predict_client: "PredictClient | None" = None
        self._predict_sniper: "PredictSniper | None" = None
        if PREDICT_AVAILABLE and config.predict_enabled and config.predict_api_key and config.predict_private_key:
            self._predict_client = PredictClient(
                api_key=config.predict_api_key,
                private_key=config.predict_private_key,
                predict_account=config.predict_account,
            )
            fetcher = PredictMarketFetcher(api_key=config.predict_api_key)
            self._predict_sniper = PredictSniper(
                self._predict_client, fetcher,
                paper=(mode != "live"),
            )
            # Wire DB logging callbacks for predict.fun trades
            self._predict_sniper.set_db_callbacks(
                on_open=self._predict_log_trade,
                on_close=self._predict_close_trade,
            )
        # MERGE-SPLIT structural arbitrage (탐지 기본값: dry-run)
        self._merge_split: "MergeSplitArb | None" = None
        if MERGE_SPLIT_AVAILABLE:
            try:
                ms_cfg = MergeSplitConfig()
                if ms_cfg.enabled:
                    self._merge_split = MergeSplitArb(
                        clob_client=self.scanner,
                        predict_client=self._predict_client,
                        db_logger=self.db_logger,
                        telegram_notifier=_poly_notifier,
                        config=ms_cfg,
                        mode=self.mode,
                    )
            except Exception as exc:
                logger.error(f"[merge_split] 초기화 실패: {exc}", exc_info=True)
                self._merge_split = None
        # 5-min 꼬리 리버설 스나이퍼 (탐지 기본값: dry-run)
        # Predict.fun 1시간 마켓도 지원 — predict_client/fetcher 가 있으면 병렬 스캔
        self._reversal_sniper: "ReversalSniper | None" = None
        if REVERSAL_SNIPE_AVAILABLE:
            try:
                rs_cfg = ReversalSniperConfig()
                if rs_cfg.enabled:
                    # Predict.fun 연동 — _predict_sniper 가 초기화된 경우에만 주입
                    predict_client_for_reversal = None
                    predict_fetcher_for_reversal = None
                    if self._predict_sniper is not None and self._predict_client is not None:
                        predict_client_for_reversal = self._predict_client
                        predict_fetcher_for_reversal = getattr(self._predict_sniper, "fetcher", None)
                    self._reversal_sniper = ReversalSniper(
                        scanner=self.scanner,
                        binance_price_source=self.price_engines,
                        db_logger=self.db_logger,
                        notifier=_poly_notifier,
                        config=rs_cfg,
                        mode=self.mode,
                        predict_client=predict_client_for_reversal,
                        predict_fetcher=predict_fetcher_for_reversal,
                    )
            except Exception as exc:
                logger.error(f"[reversal_snipe] 초기화 실패: {exc}", exc_info=True)
                self._reversal_sniper = None
        # Stoikov-Avellaneda MM (default disabled via MM_ENABLED=false)
        self._mm: "StoikovMM | None" = None
        if MM_AVAILABLE and getattr(config, "mm_enabled", False):
            try:
                self._mm = StoikovMM(self.scanner, self.db_logger, config)
            except Exception as exc:
                logger.error(f"[MM] init failed: {exc}", exc_info=True)
                self._mm = None
        self._running = False
        self._active_market_ids: set[str] = set()
        self._active_shadow_keys: set[str] = set()
        self._order_fill_cache: dict[str, float] = {}  # order_id -> size_matched (cached early)
        self._blocked_skip_log_state: dict[str, dict[str, float | int]] = {}
        # Daily circuit breaker: pause live trading if daily loss exceeds threshold
        self._daily_loss_limit = float(getattr(config, "daily_stop_loss", -50.0))
        self._circuit_breaker_tripped = False
        self._circuit_breaker_checked_at = 0.0
        self._skip_log_state: dict[str, float] = {}
        self._shadow_group_cooldowns: dict[str, float] = {}
        self._dashboard_failures = 0
        self._dashboard_disabled = False
        self._snapshot_counter = 0
        self._stale_trade_grace_sec = max(300, int(getattr(config, "stale_trade_reconcile_grace_sec", 900)))

    def _build_recovery_status(self) -> dict | None:
        if not self.config.recovery_mode_enabled:
            return None
        cumulative_pnl = self.db_logger.get_cumulative_realized_pnl(mode="paper")
        start_pnl = self.config.recovery_start_pnl
        target_pnl = self.config.live_resume_profit_target
        effective_pnl = start_pnl + cumulative_pnl
        baseline = target_pnl - start_pnl
        progressed = effective_pnl - start_pnl
        progress_ratio = 1.0 if baseline <= 0 else max(0.0, min(1.0, progressed / baseline))
        remaining = max(0.0, target_pnl - effective_pnl)
        locked = effective_pnl < target_pnl
        return {
            "locked": locked,
            "start_pnl": start_pnl,
            "cumulative_pnl": cumulative_pnl,
            "effective_pnl": effective_pnl,
            "target_pnl": target_pnl,
            "remaining_to_unlock": remaining,
            "progress_ratio": progress_ratio,
            "message": "Live trading unlocked." if not locked else "Paper trading only until recovery target is met.",
        }

    def _build_paper_gate_status(self) -> dict | None:
        if not self.config.paper_live_gate_enabled:
            return None
        recent = self.db_logger.get_closed_trades(
            mode="paper",
            limit=self.config.paper_live_gate_min_trades,
            market_group=self.config.performance_reference_group,
        )
        sample_size = len(recent)
        pnl_values = [float(trade.get("pnl", 0.0) or 0.0) for trade in recent]
        sample_pnl = sum(pnl_values)
        wins = sum(1 for pnl in pnl_values if pnl > 0)
        gross_profit = sum(pnl for pnl in pnl_values if pnl > 0)
        gross_loss = abs(sum(pnl for pnl in pnl_values if pnl < 0))
        win_rate = wins / sample_size if sample_size else 0.0
        avg_pnl = sample_pnl / sample_size if sample_size else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
        passes_win_rate = win_rate >= self.config.paper_live_gate_min_win_rate
        passes_profit_factor = profit_factor >= self.config.paper_live_gate_min_profit_factor and avg_pnl > 0
        ready = (
            sample_size >= self.config.paper_live_gate_min_trades
            and sample_pnl >= self.config.paper_live_gate_min_pnl
            and (passes_win_rate or passes_profit_factor)
        )
        return {
            "ready": ready,
            "sample_size": sample_size,
            "sample_pnl": sample_pnl,
            "sample_win_rate": win_rate,
            "sample_avg_pnl": avg_pnl,
            "sample_profit_factor": profit_factor,
            "required_trades": self.config.paper_live_gate_min_trades,
            "required_pnl": self.config.paper_live_gate_min_pnl,
            "required_win_rate": self.config.paper_live_gate_min_win_rate,
            "required_profit_factor": self.config.paper_live_gate_min_profit_factor,
            "passes_win_rate": passes_win_rate,
            "passes_profit_factor": passes_profit_factor,
            "reference_group": self.config.performance_reference_group,
            "message": "Paper performance is strong enough for live." if ready else "Live locked until paper performance clears the readiness gate.",
        }

    def _resolve_runtime_mode(self, requested_mode: str) -> tuple[str, str]:
        if requested_mode != "live":
            return requested_mode, ""
        recovery_status = self._build_recovery_status()
        if recovery_status and recovery_status["locked"]:
            return "paper", f"RECOVERY MODE: live orders disabled. Effective recovery PnL is ${recovery_status['effective_pnl']:+.2f}. Remaining to unlock: ${recovery_status['remaining_to_unlock']:.2f}."
        paper_gate = self._build_paper_gate_status()
        if paper_gate and not paper_gate["ready"]:
            return "paper", (
                f"PAPER GATE: live orders disabled. {paper_gate['reference_group']} sample is ${paper_gate['sample_pnl']:+.2f} "
                f"across {paper_gate['sample_size']}/{paper_gate['required_trades']} trades with {paper_gate['sample_win_rate']:.1%} win rate and PF {paper_gate['sample_profit_factor']:.2f}."
            )
        if not self.config.enforce_paper_after_live_loss:
            return requested_mode, ""
        today_live_pnl = self.db_logger.get_today_realized_pnl(mode="live")
        today_total_pnl = self.db_logger.get_today_realized_pnl()
        if today_live_pnl <= self.config.live_to_paper_loss_threshold and today_total_pnl < self.config.live_resume_profit_target:
            return "paper", f"LIVE locked: today's live PnL is ${today_live_pnl:+.2f}. Running PAPER until total daily PnL reaches ${self.config.live_resume_profit_target:+.2f}."
        return requested_mode, ""

    def _refresh_dashboard_status(self):
        self.dashboard.set_recovery_status(self._build_recovery_status())
        self.dashboard.set_paper_gate_status(self._build_paper_gate_status())
        self.dashboard.set_optimizer_status(self.optimizer.status() if self.optimizer else None)

    def _log_guardrail_status(self, optimizer_status: dict[str, Any]):
        recovery_status = self._build_recovery_status()
        paper_gate = self._build_paper_gate_status()
        blocked = optimizer_status.get("blocked_strategies") if optimizer_status else []
        blocked_label = ",".join(blocked) if blocked else "-"
        recovery_label = "off"
        if recovery_status is not None:
            recovery_label = f"{'locked' if recovery_status.get('locked') else 'unlocked'} effective=${recovery_status.get('effective_pnl', 0.0):+.2f} target=${recovery_status.get('target_pnl', 0.0):+.2f}"
        gate_label = "off"
        if paper_gate is not None:
            gate_label = "ready" if paper_gate.get("ready") else (
                f"locked group={paper_gate.get('reference_group', '-')} pnl=${paper_gate.get('sample_pnl', 0.0):+.2f} "
                f"wr={paper_gate.get('sample_win_rate', 0.0):.1%} pf={paper_gate.get('sample_profit_factor', 0.0):.2f} trades={paper_gate.get('sample_size', 0)}/{paper_gate.get('required_trades', 0)}"
            )
        live_ready = True
        if recovery_status is not None and recovery_status.get("locked"):
            live_ready = False
        if paper_gate is not None and not paper_gate.get("ready"):
            live_ready = False
        logger.info(f"[GUARD] live_ready={str(live_ready).lower()} recovery={recovery_label} paper_gate={gate_label} blocked={blocked_label}")

    async def start(self):
        self._running = True
        logger.info(
            f"Starting bot in {self.mode.upper()} mode" + (f" (requested {self.requested_mode.upper()})" if self.mode != self.requested_mode else "") + "..."
        )
        self._refresh_dashboard_status()
        if self.optimizer is not None:
            try:
                initial_status = self.optimizer.evaluate()
                self.dashboard.set_optimizer_status(initial_status)
                self._log_guardrail_status(initial_status)
            except Exception as exc:
                logger.error(f"Initial optimizer evaluation failed: {exc}", exc_info=True)
        if self.mode_note:
            logger.warning(self.mode_note)
            self.dashboard.set_trade_info(self.mode_note)
        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))
        if self.mode == "live":
            balance = await self.scanner.check_balance()
            self.dashboard.set_balance(balance)
            if balance < 1.0:
                logger.warning(f"USDC balance too low: ${balance:.2f}. Need at least $1.")
        for trade in self.db_logger.get_open_trades(mode=self.mode):
            self._active_market_ids.add(str(trade["market_id"]))
        for trade in self.db_logger.get_open_trades(mode="shadow"):
            self._active_shadow_keys.add(self._shadow_key(str(trade["market_id"]), str(trade.get("side", ""))))
        self._trim_excess_shadow_positions()
        self._trim_simulated_paper_positions()
        await self._reconcile_stale_open_trades()
        if self.ml_model is not None:
            try:
                trained = self.ml_model.train(self.db_logger, min_trades=self.config.ml_min_train_trades)
                if not trained:
                    logger.info("[ML] Model not trained yet (insufficient data). Will retry periodically.")
            except Exception as exc:
                logger.error(f"[ML] Initial training failed: {exc}", exc_info=True)

        tasks = [asyncio.create_task(engine.start()) for engine in self.price_engines.values()]
        tasks.extend(
            [
                asyncio.create_task(self._signal_loop()),
                asyncio.create_task(self._position_monitor_loop()),
                asyncio.create_task(self._dashboard_loop()),
            ]
        )
        if self.optimizer is not None:
            tasks.append(asyncio.create_task(self._optimizer_loop()))
        if self.ml_model is not None:
            tasks.append(asyncio.create_task(self._ml_retrain_loop()))
        tasks.append(asyncio.create_task(self._redeem_loop()))
        if self._weather is not None:
            tasks.append(asyncio.create_task(self._weather_loop()))
            tasks.append(asyncio.create_task(self._weather_exit_loop()))
        if self._predict_sniper is not None:
            tasks.append(asyncio.create_task(self._predict_loop()))
        if self._merge_split is not None:
            tasks.append(asyncio.create_task(self._merge_split.start()))
        if self._reversal_sniper is not None:
            tasks.append(asyncio.create_task(self._reversal_sniper.start()))
        if self._mm is not None:
            tasks.append(asyncio.create_task(self._mm.run_loop()))
        if self.mode == "live":
            tasks.append(asyncio.create_task(self._balance_snapshot_loop()))
        tasks.append(asyncio.create_task(self._wal_checkpoint_loop()))
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def stop(self):
        logger.info("Shutting down...")
        self._running = False
        for engine in self.price_engines.values():
            engine.stop()

    async def _cleanup(self):
        if self._merge_split is not None:
            try:
                await self._merge_split.stop()
            except Exception as exc:
                logger.debug(f"[merge_split] stop 예외: {exc}")
        if self._reversal_sniper is not None:
            try:
                await self._reversal_sniper.stop()
            except Exception as exc:
                logger.debug(f"[reversal_snipe] stop 예외: {exc}")
        await self.scanner.close()
        await self.copy_trader.close()
        if self._weather is not None:
            await self._weather.close()
        if self._predict_client is not None:
            await self._predict_client.close()
        self.db_logger.close()
        logger.info("Shutdown complete.")

    async def _signal_loop(self):
        logger.info("Waiting for price data to warm up...")
        while self._running and any(engine.latest_price == 0 for engine in self.price_engines.values()):
            await asyncio.sleep(1)
        logger.info("Price feed active. Starting signal loop.")
        while self._running:
            try:
                await self._scan_and_trade()
            except Exception as exc:
                logger.error(f"Signal loop error: {exc}", exc_info=True)
            await asyncio.sleep(self.config.scan_interval)

    async def _optimizer_loop(self):
        await asyncio.sleep(5)
        while self._running and self.optimizer is not None:
            try:
                status = self.optimizer.evaluate()
                self.dashboard.set_optimizer_status(status)
                blocked = status.get("blocked_strategies") or []
                logger.info(
                    f"[OPT] phase={status['phase']} regime={status.get('regime', '-')} profile={status['profile_name']} "
                    f"group={status.get('reference_group', '-')} risk={float(status.get('risk_scale', 1.0)):.2f} "
                    f"sample_pnl=${status['sample_pnl']:+.2f} trades={status['sample_size']} today=${status['today_paper_pnl']:+.2f} "
                    f"blocked={','.join(blocked) if blocked else '-'}"
                )
                self._log_guardrail_status(status)
            except Exception as exc:
                logger.error(f"Optimizer error: {exc}", exc_info=True)
            await asyncio.sleep(self.config.optimizer_eval_interval_sec)
    async def _ml_retrain_loop(self):
        await asyncio.sleep(30)
        while self._running and self.ml_model is not None:
            try:
                if self.ml_model.should_retrain(self.config.ml_retrain_interval_sec):
                    self.ml_model.train(self.db_logger, min_trades=self.config.ml_min_train_trades)
                    perf = self.ml_model.get_performance_summary()
                    logger.info(
                        f"[ML] Retrained. sharpe={perf['sharpe_ratio']:.2f}({perf['sharpe_label']}) "
                        f"n={perf['train_count']} returns={perf['n_closed_returns']} "
                        f"avg_mae={perf['avg_mae']:.4f} avg_mfe={perf['avg_mfe']:.4f}"
                    )
            except Exception as exc:
                logger.error(f"[ML] Retrain error: {exc}", exc_info=True)
            await asyncio.sleep(300)

    async def _cache_order_fill(self, order_id: str):
        """주문 후 3초 뒤 fill 상태 캐시. resolve 시 API가 삭제돼도 참조 가능."""
        try:
            client = self.scanner._get_clob_client()
            order = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_order(order_id)
            )
            if order:
                sm = float(order.get("size_matched", 0) or 0)
                self._order_fill_cache[order_id] = sm
                if sm > 0:
                    logger.info(f"[FILL] Order {order_id[:20]}... MATCHED {sm}")
        except Exception:
            pass
        # Keep cache manageable
        if len(self._order_fill_cache) > 500:
            oldest = list(self._order_fill_cache.keys())[:250]
            for k in oldest:
                self._order_fill_cache.pop(k, None)

    async def _balance_snapshot_loop(self):
        """10분마다 USDC 잔고를 기록 — 온체인 기준 진짜 수익 추적.

        Multi-instance guard: if another bot process wrote within the last 300s,
        skip this cycle. Prevents duplicate snapshots ~34s apart (two live bots
        writing the same file) which inflates the JSONL ~2x.
        """
        await asyncio.sleep(30)
        snapshot_file = Path(__file__).parent / "balance_snapshots.jsonl"
        MIN_GAP_SECONDS = 300  # half of the 600s cycle — tolerates jitter
        logger.info(f"[BALANCE] Snapshot loop started (every 600s, min_gap={MIN_GAP_SECONDS}s)")
        while self._running:
            try:
                # Guard: if another instance wrote recently, skip. Peek last line only.
                skip_write = False
                if snapshot_file.exists():
                    try:
                        # Read tail cheaply — file is small-ish; OK for this cadence.
                        tail_lines = snapshot_file.read_text(encoding="utf-8").splitlines()[-5:]
                        last_ts = None
                        for line in reversed(tail_lines):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                last_entry = json.loads(line)
                                last_ts = last_entry.get("ts")
                                break
                            except json.JSONDecodeError:
                                continue
                        if last_ts:
                            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                            if 0 <= age < MIN_GAP_SECONDS:
                                skip_write = True
                                logger.info(
                                    f"[BALANCE] Skip snapshot — another writer {age:.0f}s ago "
                                    f"(likely duplicate bot instance)"
                                )
                    except Exception as guard_err:
                        logger.debug(f"[BALANCE] Gap guard read failed: {guard_err}")

                if not skip_write:
                    balance = await self.scanner.check_balance()
                    if balance > 0:
                        import json as _json
                        entry = {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "usdc": round(balance, 2),
                        }
                        # Append one line (JSONL format — avoids read-modify-write race with Google Drive)
                        with open(snapshot_file, "a", encoding="utf-8") as sf:
                            sf.write(_json.dumps(entry) + "\n")
                        logger.info(f"[BALANCE] Snapshot: ${balance:.2f}")
            except Exception as exc:
                logger.warning(f"[BALANCE] Snapshot error: {exc}")
            await asyncio.sleep(600)

    async def _wal_checkpoint_loop(self):
        """30분마다 WAL 체크포인트 실행 — DB corruption 방지"""
        await asyncio.sleep(120)  # 시작 2분 후
        logger.info("[DB] WAL checkpoint loop started (every 1800s)")
        while self._running:
            try:
                self.db_logger.wal_checkpoint()
                logger.debug("[DB] WAL checkpoint completed")
            except Exception as e:
                logger.warning(f"[DB] WAL checkpoint error: {e}")
            await asyncio.sleep(1800)

    async def _redeem_loop(self):
        """resolve된 포지션 자동 클레임 루프 (claim_venv subprocess 또는 현재 venv)"""
        await asyncio.sleep(60)  # 시작 후 1분 대기
        import sys as _sys
        # Windows / Linux 경로 분기 + fallback to current python
        candidates = [
            Path(__file__).parent / "claim_venv" / "Scripts" / "python.exe",  # Windows
            Path(__file__).parent / "claim_venv" / "bin" / "python",  # Linux
        ]
        claim_venv_py = next((p for p in candidates if p.exists()), None)
        if claim_venv_py is None:
            # 현재 실행 중인 Python으로 폴백 (main_venv 사용)
            claim_venv_py = Path(_sys.executable)
            logger.info(f"[CLAIM] claim_venv 없음, 현재 venv 사용: {claim_venv_py}")
        claimer_script = Path(__file__).parent / "auto_claimer.py"
        if not claimer_script.exists():
            logger.warning("[CLAIM] auto_claimer.py not found — disabled")
            return
        logger.info("[CLAIM] Auto-claim loop started (every 120s via claim_venv)")
        while self._running:
            try:
                env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
                proc = await asyncio.create_subprocess_exec(
                    str(claim_venv_py), str(claimer_script),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(Path(__file__).parent),
                    env=env,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                output = stdout.decode("utf-8", errors="replace").strip()
                if output:
                    # Parse JSON result from auto_claimer (last JSON object in output)
                    try:
                        import json as _json
                        # Extract last line that looks like JSON
                        json_str = None
                        for line in output.splitlines():
                            line = line.strip()
                            if line.startswith("{") or line.startswith("["):
                                json_str = line
                        if not json_str:
                            # Try full output (pretty-printed JSON)
                            idx = output.rfind("{")
                            if idx >= 0:
                                json_str = output[idx:]
                        result = _json.loads(json_str) if json_str else {}
                        delta = result.get("delta", 0)
                        if delta > 0:
                            logger.info(
                                f"[CLAIM] ${result['usdc_before']:.2f} -> ${result['usdc_after']:.2f} "
                                f"(+${delta:.2f}) in {result['batches']} batches"
                            )
                    except (ValueError, KeyError):
                        if "CLAIM" in output or "redeem" in output.lower():
                            logger.info(f"[CLAIM] {output[-200:]}")
                if stderr.decode("utf-8", errors="replace").strip():
                    err_msg = stderr.decode("utf-8", errors="replace").strip()
                    if "error" in err_msg.lower():
                        logger.warning(f"[CLAIM] stderr: {err_msg[-200:]}")
            except asyncio.TimeoutError:
                logger.warning("[CLAIM] Subprocess timed out")
                if proc.returncode is None:
                    proc.kill()
            except Exception as exc:
                logger.error(f"[CLAIM] Loop error: {exc}", exc_info=True)
            await asyncio.sleep(120)

    async def _weather_loop(self):
        """날씨 마켓 스캔 루프 — 기존 크립토 전략과 독립적으로 동작"""
        await asyncio.sleep(10)
        logger.info("[WEATHER] Weather strategy loop started")
        while self._running and self._weather is not None:
            try:
                opps = await self._weather.scan()
                if opps:
                    for opp in opps[:3]:  # 상위 3개만 로그
                        logger.info(
                            f"[WEATHER] {opp['side']} {opp['city']} | "
                            f"forecast={opp['model_prob']:.1%} vs market={opp['market_prob']:.1%} | "
                            f"edge={opp['edge']:.1%} | ${opp['bet_size']:.2f} | "
                            f"{opp['question'][:50]}"
                        )
                    # Paper trade: log to DB
                    if self.mode == "paper":
                        for opp in opps[:1]:  # 최고 엣지 1건만 페이퍼 트레이드
                            self.db_logger.log_trade(
                                market_id=opp["market_id"],
                                market_question=opp["question"],
                                side=opp["side"],
                                entry_price=opp["entry_price"],
                                size=opp["bet_size"],
                                signal_values={"city": opp["city"], "threshold": opp.get("threshold", 0), "forecast_prob": round(opp["model_prob"], 4), "market_prob": round(opp["market_prob"], 4), "source": opp.get("source", ""), "token_id": opp.get("token_id", ""), "metric": opp.get("metric", "high_temp"), "direction": opp.get("direction", "above")},
                                model_prob=opp["model_prob"],
                                market_prob=opp["market_prob"],
                                edge=opp["edge"],
                                kelly_fraction=0,
                                expiry_time=str(opp.get("expiry_ts", 0) or ""),
                                strategy_name=opp["strategy"],
                                mode=self.mode,
                                market_group="weather",
                                asset_symbol="WX",
                                token_id=opp.get("token_id", ""),
                            )
                            logger.info(
                                f"[WEATHER] Paper trade placed: {opp['side']} ${opp['bet_size']:.2f} @ {opp['entry_price']:.2f} "
                                f"edge={opp['edge']:.1%} on {opp['question'][:40]}"
                            )
                    # Live trade: weather has its own risk limits (not shared with crypto)
                    elif self.mode == "live":
                        # Count current weather positions + collect open market_ids for dedup
                        open_wx_trades = self.db_logger.get_open_trades(market_group="weather")
                        weather_open = len(open_wx_trades)
                        open_wx_market_ids = {t.get("market_id", "") for t in open_wx_trades}
                        max_wx = self.config.weather_max_positions
                        for opp in opps[:3]:
                            if weather_open >= max_wx:
                                logger.info(f"[WEATHER] Max weather positions reached ({weather_open}/{max_wx})")
                                break
                            # Skip if we already have a position on this market
                            if opp["market_id"] in open_wx_market_ids:
                                continue
                            bet_size = min(opp["bet_size"], self.config.weather_max_bet)
                            if bet_size < self.config.weather_min_bet:
                                continue
                            logger.info(
                                f"[WEATHER] Placing LIVE order: {opp['side']} ${bet_size:.2f} @ {opp['entry_price']:.3f} "
                                f"edge={opp['edge']:.1%} | {opp['question'][:50]}"
                            )
                            order_id = await self.scanner.place_order(
                                opp["token_id"], opp["side"],
                                bet_size, opp["entry_price"], mode="live",
                            )
                            if order_id:
                                self.db_logger.log_trade(
                                    market_id=opp["market_id"],
                                    market_question=opp["question"],
                                    side=opp["side"],
                                    entry_price=opp["entry_price"],
                                    size=bet_size,
                                    signal_values={"city": opp["city"], "threshold": opp.get("threshold", 0), "forecast_prob": round(opp["model_prob"], 4), "market_prob": round(opp["market_prob"], 4), "source": opp.get("forecast_source", ""), "token_id": opp.get("token_id", ""), "metric": opp.get("metric", "high_temp"), "direction": opp.get("direction", "above")},
                                    model_prob=opp["model_prob"],
                                    market_prob=opp["market_prob"],
                                    edge=opp["edge"],
                                    kelly_fraction=0,
                                    expiry_time=str(opp.get("expiry_ts", 0) or ""),
                                    order_id=str(order_id),
                                    mode="live",
                                    strategy_name=opp["strategy"],
                                    market_group="weather",
                                    asset_symbol="WX",
                                    token_id=opp.get("token_id", ""),
                                )
                                weather_open += 1
                                open_wx_market_ids.add(opp["market_id"])
                                logger.info(f"[WEATHER] Order FILLED: {order_id} ({weather_open}/{max_wx})")
                            else:
                                logger.warning(f"[WEATHER] Order rejected: {opp['question'][:40]}")
                status = self._weather.format_status()
                self.dashboard.set_trade_info(f"[WX] {status}")
            except Exception as exc:
                logger.error(f"[WEATHER] Loop error: {exc}", exc_info=True)
            await asyncio.sleep(self.config.weather_scan_interval_sec)

    async def _weather_exit_loop(self):
        """Periodically re-check forecasts for open weather positions and sell if edge gone."""
        await asyncio.sleep(30)
        if self._weather is None or not self.config.weather_exit_enabled:
            return
        logger.info("[WEATHER-EXIT] Weather exit monitor started")
        while self._running and self._weather is not None:
            try:
                open_wx = self.db_logger.get_open_trades(mode="live", market_group="weather")
                for trade in open_wx:
                    try:
                        await self._check_weather_exit(trade)
                    except Exception as exc:
                        logger.error(f"[WEATHER-EXIT] Check error #{trade.get('id')}: {exc}")
            except Exception as exc:
                logger.error(f"[WEATHER-EXIT] Loop error: {exc}", exc_info=True)
            await asyncio.sleep(self.config.weather_exit_check_interval_sec)

    async def _check_weather_exit(self, trade: dict):
        """Evaluate a single weather position and sell if edge disappeared."""
        trade_id = int(trade["id"])
        sv = trade.get("signal_values", "{}")
        if isinstance(sv, str):
            sv = json.loads(sv) if sv else {}

        city = sv.get("city", "")
        threshold = float(sv.get("threshold", 0))
        entry_forecast = float(sv.get("forecast_prob", 0))
        metric = sv.get("metric", "high_temp")
        direction = sv.get("direction", "above")
        token_id = str(trade.get("token_id") or sv.get("token_id") or "")
        entry_price = float(trade.get("entry_price", 0))
        size = float(trade.get("size", 0))
        expiry_ts = float(trade.get("expiry_time", 0) or 0)

        if not city or not token_id or entry_price <= 0:
            return

        # Derive end_date from expiry_ts
        end_date = ""
        if expiry_ts > 0:
            end_date = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%Y-%m-%d")

        # Re-evaluate forecast
        result = await self._weather.evaluate_position(
            city=city, metric=metric, threshold=threshold, direction=direction,
            end_date=end_date, entry_price=entry_price,
            entry_forecast_prob=entry_forecast, expiry_ts=expiry_ts,
            edge_buffer=self.config.weather_exit_edge_buffer,
            forecast_drop_pct=self.config.weather_exit_forecast_drop_pct,
            urgent_hours=self.config.weather_exit_urgent_hours,
        )

        if result["action"] == "hold":
            return

        # Get best bid and decide if worth selling
        best_bid = await self.scanner.get_best_bid(token_id)
        if best_bid < entry_price * self.config.weather_exit_min_sell_price_ratio:
            logger.info(f"[WEATHER-EXIT] #{trade_id} bid {best_bid:.3f} too low vs entry {entry_price:.3f}, skip")
            return

        shares = round(size / entry_price, 2)
        order_id = await self.scanner.sell_order(token_id, shares, best_bid, mode=self.mode)

        if order_id:
            sell_value = best_bid * shares
            pnl = sell_value - size  # sell proceeds minus original dollar cost
            self.db_logger.close_trade(trade_id, exit_price=best_bid, pnl=round(pnl, 4))
            logger.info(
                f"[WEATHER-EXIT] SOLD #{trade_id} | {result['reason']} | "
                f"forecast {entry_forecast:.1%}->{result['current_forecast_prob']:.1%} | "
                f"{city} {threshold} | exit={best_bid:.3f} entry={entry_price:.3f} pnl=${pnl:+.2f}"
            )
        else:
            logger.warning(f"[WEATHER-EXIT] #{trade_id} sell failed | {result['reason']}")

    async def _reconcile_predict_trades(self):
        """Predict.fun DB의 open 트레이드 중 만료된 것 자동 close"""
        try:
            open_predict = self.db_logger.get_open_trades(strategy_name="predict_snipe")
            now = time.time()
            for trade in open_predict:
                expiry = float(trade.get("expiry_time", 0) or 0)
                if expiry <= 0 or now < expiry + 120:  # 2분 grace
                    continue
                # Binance kline으로 승패 판정
                trade_id = int(trade["id"])
                entry_price = float(trade.get("entry_price") or 0)
                size = float(trade.get("size") or 0)
                side = str(trade.get("side", "UP"))
                asset = str(trade.get("asset_symbol") or "BTC")
                sv = trade.get("signal_values", "{}")
                if isinstance(sv, str):
                    sv = json.loads(sv) if sv else {}
                strike = float(sv.get("strike_price", 0))
                if strike <= 0:
                    # 판정 불가 → 24시간 후 강제 close
                    ts_str = str(trade.get("timestamp", ""))
                    try:
                        created = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                        if now - created > 86400:
                            self.db_logger.close_trade(trade_id, 0.0, -size)
                            logger.warning(f"[PREDICT-DB] Force-closed stale #{trade_id} (no strike, >24h)")
                    except Exception:
                        pass
                    continue
                # Get closing price from Binance
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=10) as client:
                        resp = await client.get(
                            f"https://api.binance.com/api/v3/klines",
                            params={"symbol": f"{asset}USDT", "interval": "1m",
                                    "startTime": int((expiry - 120) * 1000),
                                    "endTime": int(expiry * 1000), "limit": 3},
                        )
                        if resp.status_code == 200:
                            klines = resp.json()
                            if klines:
                                close_price = float(klines[-1][4])
                                went_up = close_price >= strike
                                won = (went_up and side == "UP") or (not went_up and side == "DOWN")
                                pnl = size * (1.0 / entry_price - 1.0) if won and entry_price > 0 else -size
                                self.db_logger.close_trade(trade_id, 1.0 if won else 0.0, round(pnl, 4))
                                logger.info(
                                    f"[PREDICT-DB] Resolved #{trade_id} {'WIN' if won else 'LOSS'} ${pnl:+.2f} | "
                                    f"{asset} strike={strike:.2f} close={close_price:.2f}"
                                )
                except Exception as e:
                    logger.debug(f"[PREDICT-DB] Kline fetch failed for #{trade_id}: {e}")
        except Exception as e:
            logger.error(f"[PREDICT-DB] Reconcile error: {e}")

    def _predict_log_trade(self, trade_data: dict) -> int:
        """Predict.fun 거래를 trades.db에 기록"""
        try:
            trade_id = self.db_logger.log_trade(**trade_data)
            logger.info(f"[PREDICT-DB] Logged trade #{trade_id} {trade_data.get('asset_symbol','')} {trade_data.get('side','')}")
            return trade_id
        except Exception as e:
            logger.error(f"[PREDICT-DB] log_trade failed: {e}")
            return 0

    def _predict_close_trade(self, market_id: int, won: bool, entry_price: float, size: float):
        """Predict.fun 거래 종료 기록"""
        try:
            # Find open trade by market_id
            open_trades = self.db_logger.get_open_trades(strategy_name="predict_snipe")
            for trade in open_trades:
                if str(trade.get("market_id")) == str(market_id):
                    trade_id = int(trade["id"])
                    ep = float(trade.get("entry_price") or entry_price)
                    sz = float(trade.get("size") or size)
                    pnl = sz * (1.0 / ep - 1.0) if won and ep > 0 else -sz
                    self.db_logger.close_trade(trade_id, 1.0 if won else 0.0, round(pnl, 4))
                    logger.info(f"[PREDICT-DB] Closed #{trade_id} {'WIN' if won else 'LOSS'} ${pnl:+.2f}")
                    return
        except Exception as e:
            logger.error(f"[PREDICT-DB] close_trade failed: {e}")

    async def _predict_loop(self):
        """Predict.fun crypto up/down expiry snipe loop"""
        await asyncio.sleep(5)
        if self._predict_client is None or self._predict_sniper is None:
            return
        for attempt in range(3):
            try:
                await self._predict_client.connect()
                break
            except Exception as exc:
                logger.error(f"[PREDICT] Client connect failed (attempt {attempt + 1}/3): {exc}")
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                else:
                    logger.error("[PREDICT] Giving up after 3 connect attempts")
                    return
        mode_label = "PAPER" if self._predict_sniper.paper else "LIVE"
        logger.info(f"[PREDICT] Predict.fun sniper loop started ({mode_label})")
        last_claim_time = 0.0

        # BNB 가스비 모니터 시작 (connect 후 signer_address가 설정되므로 여기서 생성)
        self._bnb_monitor = None
        if not self._predict_sniper.paper:
            try:
                from bnb_monitor import BnbGasMonitor
                import notifier as _notifier_mod
                signer = getattr(self._predict_client, "signer_address", "") or ""
                if signer:
                    self._bnb_monitor = BnbGasMonitor(
                        signer_address=signer,
                        notifier=_notifier_mod,
                    )
                    asyncio.create_task(self._bnb_monitor.run())
                    logger.info(f"[BNB] monitor 시작: signer={signer}")
                else:
                    logger.warning("[BNB] signer_address 미설정 — 모니터 스킵")
            except Exception as e:
                logger.warning(f"[BNB] monitor 시작 실패: {e}")

        while self._running:
            try:
                # Circuit breaker — skip new snipes but still claim
                if not self._predict_sniper.paper and self._check_daily_circuit_breaker():
                    logger.debug("[PREDICT] Circuit breaker active, skipping snipe scan")
                else:
                    await self._predict_sniper.scan_and_snipe()
                stats = self._predict_sniper.get_stats()
                if stats["trades"] > 0 and stats["trades"] % 5 == 0:
                    logger.info(f"[PREDICT] Stats: {stats['trades']} trades, active={stats['active_markets']}")
                # Auto-claim resolved positions + close DB entries
                now = time.time()
                if not self._predict_sniper.paper and now - last_claim_time >= self.config.predict_claim_interval_sec:
                    last_claim_time = now
                    # BNB 부족 시 claim 스킵 (가스비 없이 시도하면 로그 스팸만)
                    if self._bnb_monitor is not None and self._bnb_monitor.is_critical():
                        logger.warning("[PREDICT] BNB critical — claim 루프 스킵")
                    else:
                        await self._predict_sniper.claim_resolved()
                    await self._reconcile_predict_trades()
            except Exception as exc:
                logger.error(f"[PREDICT] Loop error: {exc}", exc_info=True)
            await asyncio.sleep(self.config.predict_scan_interval_sec)

    def _log_skip(self, key: str, message: str, interval_sec: int | None = None):
        now_ts = time.time()
        window = int(interval_sec or self.config.repeated_skip_log_interval_sec)
        next_ts = float(self._skip_log_state.get(key, 0.0))
        if now_ts < next_ts:
            return
        logger.info(message)
        self._skip_log_state[key] = now_ts + max(5, window)

    def _capture_primary_snapshot(self, signal_output):
        self.dashboard.add_price_history(signal_output.current_price)
        self._snapshot_counter += 1
        every = max(1, int(getattr(self.config, "db_snapshot_log_every", 5)))
        if self._snapshot_counter % every == 0:
            self.db_logger.log_price_snapshot(
                signal_output.current_price,
                signal_output.rsi,
                signal_output.bb_upper,
                signal_output.bb_lower,
                signal_output.vwap,
                signal_output.momentum,
            )

    async def _load_markets_for_spec(self, spec) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, int, list[Any]]:
        engine = self.price_engines[spec.asset_symbol]
        signal_output = engine.compute_signals()
        if signal_output is None:
            return [], [], f"{spec.group_id}:warming", 0, []
        self.scanner.set_reference_price(spec.asset_symbol, signal_output.current_price)
        markets = await self.scanner.fetch_active_markets(spec)
        if not markets:
            return [], [], f"{spec.group_id}:empty", 0, []

        # Feature 1: Fetch strike prices for real markets and compute strike-based probability
        strike_prob_up = None
        if self.config.strike_price_enabled:
            strike_probs = []
            for market in markets:
                if market.strike_price is not None and market.strike_price > 0:
                    continue  # already has strike
                # Calculate window start from expiry - duration
                window_start_ts = market.expiry_timestamp - (market.duration_minutes * 60)
                strike = await engine.fetch_strike_price(window_start_ts)
                if strike and strike > 0:
                    # Mutate strike_price on the market (MarketInfo is not frozen)
                    market.strike_price = strike
            # Calculate blended strike probability using the nearest expiring market
            nearest = min((m for m in markets if m.strike_price and m.strike_price > 0), key=lambda m: m.minutes_to_expiry, default=None)
            if nearest and nearest.strike_price:
                strike_prob_up = engine.calc_strike_prob(
                    signal_output.current_price, nearest.strike_price,
                    nearest.minutes_to_expiry, nearest.duration_minutes,
                )

        # Feature 2: Orderbook sentiment overlay on model probability
        ob_adjustment = 0.0
        if self.config.orderbook_enabled and markets:
            try:
                sample_market = markets[0]
                up_sent = await self.scanner.get_orderbook_sentiment(sample_market.up_token_id)
                if up_sent.get("valid"):
                    ob_adjustment = up_sent["sentiment"] * self.config.orderbook_sentiment_weight
            except Exception:
                pass

        adjusted_model_prob = signal_output.model_prob_up + ob_adjustment

        market_rows: list[dict[str, Any]] = []
        opportunities: list[dict[str, Any]] = []
        for market in markets:
            market_rows.append(
                {
                    "question": f"[{market.market_group}] {market.question}",
                    "up": market.up_price,
                    "down": market.down_price,
                    "expiry": market.minutes_to_expiry,
                    "liq": market.liquidity,
                    "edge_up": adjusted_model_prob - market.implied_prob_up,
                    "edge_down": (1 - adjusted_model_prob) - market.down_price,
                    "has_position": market.market_id in self._active_market_ids,
                    "strike": market.strike_price,
                }
            )
        for opp in self.scanner.find_edge_opportunities(markets, adjusted_model_prob, signal_output.current_price, self.config, strike_prob_up=strike_prob_up):
            opp["signal"] = signal_output
            opp["asset_symbol"] = spec.asset_symbol
            opp["market_group"] = spec.group_id
            opportunities.append(opp)
        vol_label = f"vol={signal_output.vol_regime}" if self.config.vol_regime_enabled else ""
        strike_label = f"strike={strike_prob_up:.1%}" if strike_prob_up is not None else ""
        label = f"{spec.group_id}:{adjusted_model_prob:.1%} {vol_label} {strike_label}".strip()
        return opportunities, market_rows, label, len(markets), markets

    def _check_daily_circuit_breaker(self) -> bool:
        """balance_snapshots.json 기반 일일 손실 체크. True면 거래 중단."""
        now = time.time()
        # 60초마다만 체크 (파일 I/O 절약)
        if now - self._circuit_breaker_checked_at < 60:
            return self._circuit_breaker_tripped
        self._circuit_breaker_checked_at = now

        try:
            snapshot_file = Path(__file__).parent / "balance_snapshots.jsonl"
            if not snapshot_file.exists():
                return False
            # JSONL (line-delimited), NOT a JSON array. Tolerate bad lines.
            snapshots = []
            for line in snapshot_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if not snapshots or len(snapshots) < 2:
                return False

            # Baseline = snapshot closest to (now - 24h). Trailing 24h window,
            # not calendar-day start — matches user's "24시 기준 깎이는 것" wording.
            now_dt = datetime.now(timezone.utc)
            cutoff_ts = now_dt.timestamp() - 86400
            baseline_balance = None
            baseline_ts_iso = None
            for snap in snapshots:
                ts_str = snap.get("ts", "")
                try:
                    snap_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                if snap_dt.timestamp() >= cutoff_ts:
                    # First snapshot at or after 24h-ago is the baseline
                    try:
                        baseline_balance = float(snap.get("usdc", 0))
                        baseline_ts_iso = ts_str
                    except (TypeError, ValueError):
                        continue
                    break

            if baseline_balance is None or baseline_balance <= 0:
                # No snapshot older than 24h yet → use the oldest available as a soft baseline
                try:
                    baseline_balance = float(snapshots[0].get("usdc", 0))
                    baseline_ts_iso = snapshots[0].get("ts", "")
                except (TypeError, ValueError):
                    return False
                if baseline_balance <= 0:
                    return False

            try:
                current_balance = float(snapshots[-1].get("usdc", 0))
            except (TypeError, ValueError):
                return False
            if current_balance <= 0:
                return False

            daily_pnl = current_balance - baseline_balance

            if daily_pnl <= self._daily_loss_limit:
                if not self._circuit_breaker_tripped:
                    logger.warning(
                        f"[CIRCUIT-BREAKER] 24h loss ${daily_pnl:+.2f} exceeded limit ${self._daily_loss_limit:.2f}. "
                        f"Pausing live trading. Baseline(@{baseline_ts_iso})=${baseline_balance:.2f} Now=${current_balance:.2f}"
                    )
                    # Fire Telegram alert once per day
                    try:
                        import notifier as _notifier_mod
                        msg = (
                            f"<b>[CIRCUIT-BREAKER]</b> 24h loss ${daily_pnl:+.2f} "
                            f"exceeded limit ${self._daily_loss_limit:.2f}\n"
                            f"Baseline=${baseline_balance:.2f} → Now=${current_balance:.2f}\n"
                            f"Live trading paused (position monitoring continues)."
                        )
                        asyncio.create_task(
                            _notifier_mod.notify(msg, dedup_key="daily_stop_hit", dedup_seconds=86400)
                        )
                    except Exception as notify_err:
                        logger.debug(f"Circuit breaker notify failed: {notify_err}")
                self._circuit_breaker_tripped = True
                return True
            else:
                if self._circuit_breaker_tripped:
                    logger.info(f"[CIRCUIT-BREAKER] 24h PnL recovered to ${daily_pnl:+.2f}. Resuming.")
                self._circuit_breaker_tripped = False
                return False
        except Exception as e:
            logger.warning(f"Circuit breaker check error: {e}")
            return self._circuit_breaker_tripped

    async def _scan_and_trade(self):
        if self.risk_manager.is_halted:
            return
        # Daily circuit breaker — only blocks NEW entries, doesn't affect position monitoring
        if self.mode == "live" and self._check_daily_circuit_breaker():
            return
        primary_signal = self.price_engines[self.primary_asset].compute_signals()
        if primary_signal is None:
            return
        self._capture_primary_snapshot(primary_signal)

        total_markets = 0
        opportunities: list[dict[str, Any]] = []
        market_rows: list[dict[str, Any]] = []
        scan_labels: list[str] = []
        markets_by_id: dict[str, Any] = {}

        for spec in self.market_specs:
            try:
                spec_opps, spec_rows, scan_label, market_count, spec_markets = await self._load_markets_for_spec(spec)
                for market in spec_markets:
                    markets_by_id[str(market.market_id)] = market
            except Exception as exc:
                logger.error(f"Market load failed for {spec.group_id}: {exc}", exc_info=True)
                continue
            total_markets += market_count
            if scan_label:
                scan_labels.append(scan_label)
            market_rows.extend(spec_rows)
            opportunities.extend(spec_opps)

        try:
            copy_opps, copy_label = await self.copy_trader.build_opportunities(markets_by_id)
            if copy_label:
                scan_labels.append(copy_label)
            opportunities.extend(copy_opps)
        except Exception as exc:
            logger.error(f"Copy-trader scan failed: {exc}", exc_info=True)

        self.dashboard.set_markets_info(market_rows)
        self.dashboard.set_scan_info(f"Scanned {total_markets} markets | {' | '.join(scan_labels) if scan_labels else '-'} | {len(opportunities)} opportunities")
        if not opportunities:
            return

        opportunities.sort(key=lambda item: (item["entry_price"], -abs(item["edge"])))
        logger.info(f"Found {len(opportunities)} edge opportunities across {len(self.market_specs)} groups")
        optimizer_status = self.optimizer.status() if self.optimizer else {}
        optimizer_status = self._apply_runtime_inactivity_risk_cap(optimizer_status)
        shadow_guard_status = self._build_shadow_guard_status()
        self._record_shadow_candidates(opportunities, optimizer_status, shadow_guard_status)

        trades_this_scan = 0
        max_per_scan = getattr(self.config, "max_trades_per_scan", 1)
        for opp in opportunities:
            if not self._running or trades_this_scan >= max_per_scan:
                break
            executed = await self._attempt_opportunity(opp, optimizer_status, shadow_guard_status)
            if executed:
                trades_this_scan += 1

    def _direction_limit_reached(self, side: str) -> bool:
        dir_bal = self.risk_manager.get_direction_balance()
        if side == "UP" and dir_bal["up"] >= 2:
            return True
        if side == "DOWN" and dir_bal["down"] >= 2:
            return True
        return False

    def _should_block_strategy(self, market, strategy_name: str, strategy_side_key: str, optimizer_status: dict[str, Any]) -> bool:
        enforce_block = market.market_group == self.config.performance_reference_group or not self.config.paper_optimizer_block_reference_only
        if not enforce_block:
            return False
        if strategy_name in optimizer_status.get("blocked_strategies", []):
            self._log_blocked_strategy_skip(market.question, strategy_name, optimizer_status)
            return True
        if strategy_side_key in optimizer_status.get("blocked_strategy_sides", []):
            self._log_blocked_strategy_skip(market.question, strategy_side_key, optimizer_status)
            return True
        return False

    def _required_edge_for_market(self, market) -> float:
        extra_edge_buffer = 0.0
        if market.market_group != self.config.performance_reference_group:
            extra_edge_buffer += float(getattr(self.config, "non_reference_trade_edge_buffer", 0.0))
        if market.asset_symbol == "ETH":
            extra_edge_buffer += float(getattr(self.config, "eth_trade_edge_buffer", 0.0))
        return self.config.min_entry_edge + extra_edge_buffer

    def _minutes_since_last_reference_paper_close(self) -> int | None:
        latest = self.db_logger.get_closed_trades(
            mode="paper",
            limit=1,
            market_group=self.config.performance_reference_group,
        )
        if not latest:
            return None
        raw_ts = str(latest[0].get("timestamp") or "").strip()
        if not raw_ts:
            return None
        try:
            closed_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except Exception:
            return None
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - closed_at).total_seconds() // 60))

    def _apply_runtime_inactivity_risk_cap(self, optimizer_status: dict[str, Any]) -> dict[str, Any]:
        if self.mode != "paper":
            return optimizer_status or {}
        status = dict(optimizer_status or {})
        risk_scale = float(status.get("risk_scale", 1.0))
        inactivity_cap_minutes = max(0, int(getattr(self.config, "optimizer_inactivity_risk_cap_minutes", 72 * 60)))
        inactivity_cap_value = float(getattr(self.config, "optimizer_inactivity_risk_cap_value", 0.65))
        inactivity_minutes = self._minutes_since_last_reference_paper_close()
        status["_runtime_inactivity_minutes"] = inactivity_minutes
        status["_runtime_inactivity_risk_cap_applied"] = False
        if inactivity_cap_minutes <= 0:
            return status
        if inactivity_minutes is not None and inactivity_minutes >= inactivity_cap_minutes and risk_scale > inactivity_cap_value:
            status["risk_scale"] = inactivity_cap_value
            status["_runtime_inactivity_risk_cap_applied"] = True
            self._log_skip(
                "runtime:inactivity-risk-cap",
                f"[RISK] Runtime inactivity cap applied: reference-group paper inactivity is {inactivity_minutes}m, risk capped to {inactivity_cap_value:.2f}.",
                interval_sec=900,
            )
        return status

    def _optimizer_controls_for_market(self, market, optimizer_status: dict[str, Any], strategy_side_key: str) -> tuple[float, float]:
        if market.market_group != self.config.performance_reference_group:
            return 0.65 * float(getattr(self.config, "non_reference_bet_scale", 0.5)), 1.0
        risk_scale = float(optimizer_status.get("risk_scale", 1.0))
        preferred_strategy_sides = optimizer_status.get("preferred_strategy_sides", [])
        penalized_strategy_sides = optimizer_status.get("penalized_strategy_sides", [])
        side_scale = 1.0
        if strategy_side_key in preferred_strategy_sides:
            side_scale = 1.15
        elif strategy_side_key in penalized_strategy_sides:
            side_scale = 0.55
        return risk_scale, side_scale

    def _build_shadow_guard_status(self) -> dict[str, set[str]]:
        status = {"blocked_groups": set(), "blocked_group_sides": set()}
        if not getattr(self.config, "shadow_guard_enabled", False):
            return status
        for row in self.db_logger.get_market_group_performance(mode="shadow"):
            group = str(row.get("market_group") or "")
            if not group:
                continue
            if getattr(self.config, "shadow_guard_reference_only", True) and group == self.config.performance_reference_group:
                continue
            trade_count = int(row.get("trade_count") or 0)
            total_pnl = float(row.get("total_pnl") or 0.0)
            win_rate = float(row.get("win_rate") or 0.0)
            if trade_count >= int(getattr(self.config, "shadow_guard_min_group_trades", 0)) and total_pnl <= float(getattr(self.config, "shadow_guard_group_max_pnl", 0.0)) and win_rate < float(getattr(self.config, "shadow_guard_group_max_win_rate", 0.0)):
                status["blocked_groups"].add(group)
        for row in self.db_logger.get_market_group_side_performance(mode="shadow"):
            group = str(row.get("market_group") or "")
            side = str(row.get("side") or "")
            if not group or not side:
                continue
            if getattr(self.config, "shadow_guard_reference_only", True) and group == self.config.performance_reference_group:
                continue
            trade_count = int(row.get("trade_count") or 0)
            total_pnl = float(row.get("total_pnl") or 0.0)
            win_rate = float(row.get("win_rate") or 0.0)
            if trade_count >= int(getattr(self.config, "shadow_guard_min_side_trades", 0)) and total_pnl <= float(getattr(self.config, "shadow_guard_side_max_pnl", 0.0)) and win_rate < float(getattr(self.config, "shadow_guard_side_max_win_rate", 0.0)):
                status["blocked_group_sides"].add(f"{group}:{side}")
        return status

    def _blocked_by_shadow_guard(self, market, side: str, status: dict[str, set[str]] | None = None, *, is_shadow: bool, interval_sec: int = 300, log_reason: bool = True) -> bool:
        status = status or self._build_shadow_guard_status()
        group = str(market.market_group)
        side_key = f"{group}:{side}"
        if group in status["blocked_groups"]:
            if log_reason:
                label = "shadow guard" if is_shadow else "performance guard"
                self._skip_market(market, f"{label} blocked group {group}", key_suffix=f"guard:{group}", interval_sec=interval_sec)
            return True
        if side_key in status["blocked_group_sides"]:
            if log_reason:
                label = "shadow guard" if is_shadow else "performance guard"
                self._skip_market(market, f"{label} blocked {side_key}", key_suffix=f"guard:{side_key}", interval_sec=interval_sec)
            return True
        return False

    def _skip_market(self, market, reason: str, *, key_suffix: str = "", interval_sec: int | None = None):
        key = f"skip:{market.market_id}:{reason}:{key_suffix}"
        self._log_skip(key, f"Skip {market.question[:30]}: {reason}", interval_sec=interval_sec)

    def _build_trade_payload(self, opp: dict[str, Any], market, sizing: SizingResult, optimizer_status: dict[str, Any], adjusted_bet_size: float, signal_dict: dict | None = None) -> dict[str, Any]:
        engine = self.price_engines[market.asset_symbol]
        strategy_name = str(opp.get("strategy", "unknown"))
        risk_scale, _ = self._optimizer_controls_for_market(market, optimizer_status, f"{strategy_name}:{opp['side']}")
        return {
            "market_id": market.market_id,
            "side": opp["side"],
            "size": adjusted_bet_size,
            "entry_price": opp["entry_price"],
            "signal_values": signal_dict if signal_dict is not None else engine.get_signal_dict(),
            "model_prob": opp["model_prob"],
            "market_prob": opp["market_prob"],
            "edge": opp["edge"],
            "kelly_fraction": sizing.kelly_fractional * risk_scale,
            "expiry_time": str(market.expiry_timestamp),
            "market_question": market.question,
            "strategy_name": strategy_name,
            "profile_name": optimizer_status.get("profile_name", ""),
            "optimizer_phase": optimizer_status.get("phase", ""),
            "market_liquidity": market.liquidity,
            "minutes_to_expiry": market.minutes_to_expiry,
            "asset_symbol": market.asset_symbol,
            "market_group": market.market_group,
            "market_duration_min": market.duration_minutes,
        }
    async def _attempt_opportunity(self, opp: dict[str, Any], optimizer_status: dict[str, Any], shadow_guard_status: dict[str, set[str]]) -> bool:
        market = opp["market"]
        strategy_name = str(opp.get("strategy", "unknown"))
        strategy_side_key = f"{strategy_name}:{opp['side']}"
        is_special_strategy = strategy_name in ("expiry_snipe", "hedge_arb", "hc_snipe")

        # In live mode, only allow whitelisted strategies; others go to shadow
        if self.mode == "live":
            allowed = [s.strip() for s in self.config.live_only_strategies.split(",") if s.strip()]
            if allowed and strategy_name not in allowed:
                # High-conviction: promote to live as hc_snipe (prob>=85%, SOL UP excluded)
                if (strategy_name == "expiry_snipe" and opp["model_prob"] >= 0.80
                        and "hc_snipe" in allowed
                        and not (market.asset_symbol == "SOL" and opp["side"] == "UP")
                        and market.market_id not in self._active_market_ids):
                    # Override: treat as hc_snipe live trade with small bet
                    opp = dict(opp)
                    opp["strategy"] = "hc_snipe"
                    strategy_name = "hc_snipe"
                    # Continue to normal trade flow below (don't return)
                else:
                    self._skip_market(market, f"strategy {strategy_name} is shadow-only in live mode", key_suffix="live-filter", interval_sec=300)
                    return False

        # Asset+side blocklist (data-driven: BTC DOWN WR=18%, SOL UP WR=25%)
        blocked_pairs = [p.strip().upper() for p in getattr(self.config, "blocked_asset_sides", "").split(",") if p.strip()]
        asset_side_key = f"{getattr(market, 'asset_symbol', '')}:{opp['side']}".upper()
        if asset_side_key in blocked_pairs:
            self._skip_market(market, f"blocked asset+side: {asset_side_key}", key_suffix="blocked-pair", interval_sec=600)
            return False

        if market.market_id in self._active_market_ids:
            self._skip_market(market, "already have position", key_suffix="active")
            return False
        if self._direction_limit_reached(opp["side"]):
            dir_bal = self.risk_manager.get_direction_balance()
            self._skip_market(market, f"too many {opp['side']} positions (up={dir_bal['up']} down={dir_bal['down']})", key_suffix=opp["side"])
            return False
        if market.minutes_to_expiry > max(30.0, market.duration_minutes * 2.0):
            self._skip_market(market, f"too far out ({market.minutes_to_expiry:.0f}m)", key_suffix="expiry")
            return False
        # Special strategies (expiry_snipe, hedge_arb) skip optimizer blocks
        if not is_special_strategy:
            if self._should_block_strategy(market, strategy_name, strategy_side_key, optimizer_status):
                return False
            if self._blocked_by_shadow_guard(market, opp["side"], shadow_guard_status, is_shadow=False):
                return False

        is_simulated = str(market.market_id).startswith("sim_")
        if self.mode == "paper" and is_simulated and not self.config.allow_simulated_paper_trades:
            self._skip_market(market, "simulated market kept shadow-only", key_suffix="sim", interval_sec=300)
            return False

        # Special strategies have their own edge logic
        if not is_special_strategy:
            required_edge = self._required_edge_for_market(market)
            actual_edge = float(opp.get("edge") or 0.0)
            if actual_edge < required_edge:
                self._skip_market(market, f"edge {actual_edge:.3f} below buffered entry {required_edge:.3f}", key_suffix="edge")
                return False

        # Cache signal dict for this opportunity (used by ML, trade payload, signal publisher)
        engine = self.price_engines.get(market.asset_symbol)
        cached_signal_dict = engine.get_signal_dict() if engine else {}

        # ML prediction + safety margin filter
        ml_prediction = None
        if self.ml_model is not None and not is_special_strategy:
            signal_dict = cached_signal_dict
            strike_dist = 0.0
            if market.strike_price and market.strike_price > 0 and engine:
                strike_dist = (engine.latest_price - market.strike_price) / market.strike_price
            ml_prediction = self.ml_model.predict(
                signal_dict=signal_dict,
                market_price=opp["entry_price"],
                implied_prob_up=market.implied_prob_up,
                minutes_to_expiry=market.minutes_to_expiry,
                liquidity=market.liquidity,
                duration_minutes=market.duration_minutes,
                side=opp["side"],
                strike_distance_pct=strike_dist,
                direction_bias=signal_dict.get("direction_bias", 0.0),
                model_prob_rule=opp["model_prob"],
            )
            if self.config.ml_safety_margin_required and not ml_prediction.safety_margin_ok:
                self._skip_market(
                    market,
                    f"ML safety margin fail: price={opp['entry_price']:.3f} > {ml_prediction.blended_prob:.3f}*{self.config.ml_safety_margin_discount:.2f}={ml_prediction.blended_prob * self.config.ml_safety_margin_discount:.3f}",
                    key_suffix="ml-safety",
                )
                return False

        sizing = self.risk_manager.calculate_kelly_size(
            opp["model_prob"],
            opp["market_prob"],
            max(market.minutes_to_expiry, 5.1) if is_special_strategy else market.minutes_to_expiry,
            max(market.liquidity, 500.1) if is_special_strategy else market.liquidity,
        )
        if not sizing.should_trade:
            self._skip_market(market, f"{sizing.reason} (kelly={sizing.kelly_raw:.3f} frac={sizing.kelly_fractional:.3f})", key_suffix="sizing")
            return False

        risk_scale, side_scale = self._optimizer_controls_for_market(market, optimizer_status, strategy_side_key)
        scaled_bet_size = sizing.bet_size * risk_scale * side_scale

        # Expiry snipe gets a bet multiplier (high win rate strategy)
        if strategy_name == "expiry_snipe":
            scaled_bet_size *= getattr(self.config, "expiry_snipe_bet_multiplier", 1.0)

        # hc_snipe: cap at $2 while validating live performance
        if strategy_name == "hc_snipe":
            scaled_bet_size = min(scaled_bet_size, 2.0)

        if scaled_bet_size < self.config.min_bet_size:
            self._skip_market(market, f"scaled bet too small (${scaled_bet_size:.2f})", key_suffix="bet")
            return False

        adjusted_bet_size = round(scaled_bet_size, 2)

        # Use /price endpoint for actual executable price (not /book which shows 0.01/0.99)
        fill_price = opp["entry_price"]
        if self.mode == "live" and strategy_name == "expiry_snipe":
            try:
                actual_price = await self.scanner.get_executable_price(opp["token_id"])
                if actual_price and 0.001 <= actual_price <= 0.999:
                    fill_price = actual_price
                    actual_edge = opp["model_prob"] - actual_price
                    if actual_edge < 0.03:
                        self._skip_market(market, f"exec price {actual_price:.3f} no edge ({actual_edge:.1%})", key_suffix="exec-edge")
                        return False
            except Exception:
                pass  # fallback to mid-price

        order_id = await self.scanner.place_order(opp["token_id"], "BUY", adjusted_bet_size, fill_price, mode=self.mode)
        if not order_id:
            self._skip_market(market, f"order rejected: ${adjusted_bet_size:.2f}@{opp['entry_price']:.3f} token={str(opp['token_id'])[:16]}", key_suffix="order", interval_sec=120)
            return False

        # Cache fill status early (before API deletes expired order data)
        if self.mode == "live" and order_id:
            asyncio.get_event_loop().call_later(3.0, lambda oid=order_id: asyncio.ensure_future(self._cache_order_fill(oid)))

        self._active_market_ids.add(market.market_id)
        # Use the scanner's actual USDC cost (accounts for Polymarket's 5-share minimum),
        # not the intent-level adjusted_bet_size. When bet intent < 5*price the true cost
        # is higher, and close_trade math assumes size == USDC cost.
        actual_cost = getattr(self.scanner, "_last_order_cost", 0.0) or adjusted_bet_size
        recorded_size = round(max(adjusted_bet_size, float(actual_cost)), 2)
        payload = self._build_trade_payload(opp, market, sizing, optimizer_status, recorded_size, signal_dict=cached_signal_dict)
        payload["order_id"] = order_id
        payload["mode"] = self.mode
        trade_id = self.db_logger.log_trade(**payload)

        # Publish signal to perp-dex bridge
        if hasattr(self, "_signal_publisher") and self._signal_publisher is not None:
            try:
                binance_price = engine.latest_price if engine else 0.0
                direction = "long" if opp["side"] == "UP" else "short"
                blended = ml_prediction.blended_prob if ml_prediction else opp["model_prob"]
                ml_p = ml_prediction.ml_prob if ml_prediction else 0.0
                self._signal_publisher.publish(
                    asset=market.asset_symbol,
                    direction=direction,
                    blended_prob=blended,
                    rule_prob=opp["model_prob"],
                    ml_prob=ml_p,
                    market_price=opp["entry_price"],
                    entry_price_binance=binance_price,
                    minutes_to_expiry=market.minutes_to_expiry,
                    window_duration=market.duration_minutes,
                    signal_data=cached_signal_dict,
                    mode=self.mode,
                )
            except Exception as pub_err:
                logger.warning(f"Signal publish failed: {pub_err}")

        # Save ML data and start MAE/MFE tracking
        if ml_prediction and self.ml_model is not None:
            self.db_logger.update_trade_ml_data(trade_id, ml_prediction.ml_prob, ml_prediction.confidence, ml_prediction.exit_target)
            self.ml_model.start_tracking(trade_id, opp["entry_price"], ml_prediction.blended_prob)

        ml_label = ""
        if ml_prediction:
            ml_label = f" ml={ml_prediction.ml_prob:.2f} blend={ml_prediction.blended_prob:.2f} val={ml_prediction.entry_value_ratio:.2f}"
        trade_msg = (
            f"TRADE [{market.market_group}/{strategy_name}] {opp['side']} {market.question[:28]}... ${adjusted_bet_size:.2f} "
            f"@ {opp['entry_price']:.3f} edge={opp['edge'] * 100:.1f}% risk={risk_scale:.2f} side={side_scale:.2f}{ml_label}"
        )
        if strategy_name == "copy_wallet" and opp.get("copy_note"):
            trade_msg += f" | copy={opp['copy_note']}"
        logger.info(trade_msg)
        self.dashboard.set_trade_info(trade_msg)
        self._refresh_dashboard_status()
        return True

    def _shadow_key(self, market_id: str, side: str) -> str:
        return f"{market_id}:{side}"

    def _record_shadow_candidates(self, opportunities: list[dict], optimizer_status: dict[str, Any], shadow_guard_status: dict[str, set[str]]):
        if not getattr(self.config, "shadow_trading_enabled", False):
            return
        max_candidates = max(0, int(getattr(self.config, "shadow_max_candidates_per_scan", 0)))
        if max_candidates <= 0:
            return
        total_open = self.db_logger.count_open_trades(mode="shadow")
        if total_open >= int(getattr(self.config, "shadow_max_open_total", 0)):
            return

        logged = 0
        now_ts = time.time()
        for opp in opportunities:
            if logged >= max_candidates or total_open >= int(getattr(self.config, "shadow_max_open_total", 0)):
                break
            market = opp["market"]
            if self._blocked_by_shadow_guard(market, opp.get("side", ""), shadow_guard_status, is_shadow=True, log_reason=False):
                self._skip_market(market, f"shadow guard blocked {market.market_group}:{opp.get('side', '')}", key_suffix="shadow-guard", interval_sec=300)
                continue
            shadow_key = self._shadow_key(market.market_id, opp.get("side", ""))
            if shadow_key in self._active_shadow_keys:
                continue
            per_group_open = self.db_logger.count_open_trades(mode="shadow", market_group=market.market_group)
            if per_group_open >= int(getattr(self.config, "shadow_max_open_per_group", 0)):
                continue
            is_simulated = str(market.market_id).startswith("sim_")
            if is_simulated:
                if not getattr(self.config, "shadow_allow_simulated", False):
                    continue
                next_allowed = float(self._shadow_group_cooldowns.get(market.market_group, 0.0))
                if now_ts < next_allowed:
                    continue
            trade_id = self.db_logger.log_trade(
                market_id=market.market_id,
                side=opp["side"],
                size=round(max(self.config.min_bet_size, getattr(self.config, "shadow_bet_size", 1.0)), 2),
                entry_price=opp["entry_price"],
                signal_values=self.price_engines[market.asset_symbol].get_signal_dict(),
                model_prob=opp["model_prob"],
                market_prob=opp["market_prob"],
                edge=opp["edge"],
                kelly_fraction=0.0,
                expiry_time=str(market.expiry_timestamp),
                market_question=market.question,
                order_id=f"shadow_{market.market_id}_{opp['side']}",
                mode="shadow",
                strategy_name=opp.get("strategy", "unknown"),
                profile_name=optimizer_status.get("profile_name", ""),
                optimizer_phase=optimizer_status.get("phase", ""),
                market_liquidity=market.liquidity,
                minutes_to_expiry=market.minutes_to_expiry,
                asset_symbol=market.asset_symbol,
                market_group=market.market_group,
                market_duration_min=market.duration_minutes,
            )
            self._active_shadow_keys.add(shadow_key)
            total_open += 1
            logged += 1
            if is_simulated:
                self._shadow_group_cooldowns[market.market_group] = now_ts + int(getattr(self.config, "shadow_simulated_cooldown_sec", 0))
            logger.info(
                f"[SHADOW] Tracking #{trade_id} [{market.market_group}/{opp.get('strategy', 'unknown')}] {opp['side']} {market.question[:35]}... @ {opp['entry_price']:.3f} edge={opp['edge'] * 100:.1f}%"
                + (f" | copy={opp['copy_note']}" if opp.get("strategy") == "copy_wallet" and opp.get("copy_note") else "")
            )

    def _trim_simulated_paper_positions(self):
        if getattr(self.config, "allow_simulated_paper_trades", False):
            return
        for trade in self.db_logger.get_open_trades(mode="paper"):
            if not str(trade.get("market_id") or "").startswith("sim_"):
                continue
            self.db_logger.close_trade(int(trade["id"]), float(trade.get("entry_price") or 0.0), 0.0, update_daily=False)
            self._active_market_ids.discard(str(trade["market_id"]))
            logger.info(f"[PAPER] Trimmed simulated paper #{trade['id']} {trade.get('market_group', '-')}/{trade.get('side', '-')}")

    def _trim_excess_shadow_positions(self):
        shadow_open = self.db_logger.get_open_trades(mode="shadow")
        max_total = int(getattr(self.config, "shadow_max_open_total", 0))
        max_per_group = int(getattr(self.config, "shadow_max_open_per_group", 0))
        group_counts: dict[str, int] = {}
        kept = 0
        for trade in shadow_open:
            group = str(trade.get("market_group") or "")
            group_counts.setdefault(group, 0)
            keep = group_counts[group] < max_per_group and kept < max_total
            if keep:
                group_counts[group] += 1
                kept += 1
                continue
            self.db_logger.close_trade(int(trade["id"]), float(trade.get("entry_price") or 0.0), 0.0, update_daily=False)
            self._active_shadow_keys.discard(self._shadow_key(str(trade["market_id"]), str(trade.get("side", ""))))
            logger.info(f"[SHADOW] Trimmed excess open shadow #{trade['id']} {group}/{trade.get('side', '-')}")

    def _log_blocked_strategy_skip(self, market_question: str, strategy_key: str, optimizer_status: dict[str, Any]):
        profile_name = optimizer_status.get("profile_name", "-")
        phase = optimizer_status.get("phase", "-")
        risk_scale = float(optimizer_status.get("risk_scale", 1.0))
        msg = (
            f"Skip {market_question[:30]}: strategy {strategy_key} blocked by paper optimizer "
            f"(profile={profile_name} phase={phase} risk={risk_scale:.2f})"
        )
        self._log_skip(f"blocked:{strategy_key}", msg)
    async def _reconcile_stale_open_trades(self):
        now = time.time()
        stale_trades = []
        for trade in self.db_logger.get_open_trades():
            expiry = float(trade.get("expiry_time", 0) or 0)
            if expiry <= 0 or now < expiry + self._stale_trade_grace_sec:
                continue
            stale_trades.append(trade)
        if not stale_trades:
            return
        logger.info(f"Reconciling {len(stale_trades)} stale open trades left from previous sessions")
        for trade in stale_trades:
            await self._close_trade_if_resolved(trade, now=now, force_close=True, reason="startup reconcile")

    async def _position_monitor_loop(self):
        while self._running:
            try:
                open_trades = self.db_logger.get_open_trades()
                now = time.time()
                for trade in open_trades:
                    # Update MAE/MFE tracking for open positions
                    if self.ml_model is not None:
                        trade_id = int(trade["id"])
                        side = str(trade.get("side", "UP"))
                        asset = str(trade.get("asset_symbol") or "BTC").upper()
                        engine = self.price_engines.get(asset)
                        if engine and engine.latest_price > 0:
                            # Use market-implied current price as proxy
                            entry_price = float(trade.get("entry_price") or 0.5)
                            # Estimate current contract price based on price movement
                            self.ml_model.update_tracking(trade_id, entry_price, side)
                    await self._close_trade_if_resolved(trade, now=now)
            except Exception as exc:
                logger.error(f"Position monitor error: {exc}", exc_info=True)
            await asyncio.sleep(10)

    async def _close_trade_if_resolved(self, trade: dict[str, Any], *, now: float | None = None, force_close: bool = False, reason: str = "") -> bool:
        now = now if now is not None else time.time()
        expiry = float(trade.get("expiry_time", 0) or 0)
        if expiry <= 0:
            # Safety: force-close trades with no expiry after 24h
            ts_str = str(trade.get("timestamp", "") or "")
            if ts_str:
                try:
                    created = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    if now - created > 86400:  # 24 hours
                        logger.warning(f"[SAFETY] Force-closing #{trade['id']} — no expiry, open >24h")
                        return await self._force_close_as_loss(trade, reason="no_expiry_24h")
                except Exception:
                    pass
            return False
        if not force_close and now < expiry + 30:
            return False
        won = await self._resolve_trade(trade)
        resolution = "resolved"
        if won is None:
            if now > expiry + self._stale_trade_grace_sec:
                won = False
                resolution = "stale_grace"
            else:
                return False
        exit_price = 1.0 if won else 0.0
        entry_price = float(trade.get("entry_price") or 0.0)
        size = float(trade.get("size") or 0.0)  # dollar cost
        trade_mode = str(trade.get("mode") or "paper")
        trade_id = int(trade["id"])

        # For live trades: verify the order actually filled on-chain
        if trade_mode == "live" and trade.get("order_id"):
            try:
                client = self.scanner._get_clob_client()
                order = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: client.get_order(trade["order_id"])
                )
                if order:
                    size_matched = float(order.get("size_matched", 0) or 0)
                    original_size_shares = float(order.get("original_size", 0) or order.get("size", 0) or 0)
                    if size_matched == 0:
                        self.db_logger.close_trade(trade_id, 0.0, 0.0, update_daily=False)
                        self._active_market_ids.discard(str(trade["market_id"]))
                        logger.info(f"[LIVE] UNFILLED #{trade_id} (order never matched) | {trade.get('market_question', '')[:30]}")
                        return True
                    # Partial-fill guard: scale DB-recorded size down to the filled fraction
                    # before the win/loss formula runs. Otherwise we credit/charge phantom shares.
                    if original_size_shares > 0 and size_matched < original_size_shares * 0.999:
                        fill_ratio = size_matched / original_size_shares
                        size = size * fill_ratio
                        logger.warning(
                            f"[LIVE] PARTIAL FILL #{trade_id} {size_matched:.2f}/{original_size_shares:.2f} "
                            f"({fill_ratio:.1%}) — scaled size to ${size:.2f}"
                        )
                else:
                    # API returned nothing — order expired/deleted. Check cached fill status.
                    cached_fill = self._order_fill_cache.get(str(trade["order_id"]))
                    if cached_fill is not None and cached_fill == 0:
                        self.db_logger.close_trade(trade_id, 0.0, 0.0, update_daily=False)
                        self._active_market_ids.discard(str(trade["market_id"]))
                        logger.info(f"[LIVE] UNFILLED #{trade_id} (cached: never matched) | {trade.get('market_question', '')[:30]}")
                        return True
                    # No cache and no API data — assume filled (conservative)
            except Exception as verify_err:
                # API error — check cache
                cached_fill = self._order_fill_cache.get(str(trade.get("order_id", "")))
                if cached_fill is not None and cached_fill == 0:
                    self.db_logger.close_trade(trade_id, 0.0, 0.0, update_daily=False)
                    self._active_market_ids.discard(str(trade["market_id"]))
                    logger.info(f"[LIVE] UNFILLED #{trade_id} (cached: never matched) | {trade.get('market_question', '')[:30]}")
                    return True
                logger.debug(f"Order verify failed #{trade_id}: {verify_err}")

        if won:
            # shares = size / entry_price, payout = shares * $1
            pnl = size * (1.0 / entry_price - 1.0) if entry_price > 0 else 0.0
        else:
            pnl = -size
        self.db_logger.close_trade(trade_id, exit_price, pnl, update_daily=trade_mode != "shadow")

        # Finalize MAE/MFE tracking
        ml_suffix = ""
        if self.ml_model is not None:
            metrics = self.ml_model.finish_tracking(trade_id, exit_price, pnl)
            if metrics:
                self.db_logger.update_trade_mae_mfe(trade_id, metrics["mae"], metrics["mfe"])
                ml_suffix = f" mae={metrics['mae']:.3f} mfe={metrics['mfe']:.3f} left={metrics['left_on_table']:.3f}"

        if trade_mode == "shadow":
            self._active_shadow_keys.discard(self._shadow_key(str(trade["market_id"]), str(trade.get("side", ""))))
        else:
            self._active_market_ids.discard(str(trade["market_id"]))
        suffix = f" ({reason})" if reason else ""
        logger.info(
            f"[{trade_mode.upper()}] CLOSED #{trade_id} {('WIN' if pnl > 0 else 'LOSS')} ${pnl:+.2f} | "
            f"{trade.get('market_group', '-')}/{trade.get('market_question', '')[:30]} "
            f"entry={entry_price:.3f} via={resolution}{suffix}{ml_suffix}"
        )
        self.risk_manager.check_daily_stop_loss()
        self._refresh_dashboard_status()
        return True

    async def _force_close_as_loss(self, trade: dict[str, Any], reason: str = "") -> bool:
        """expiry 없는 좀비 트레이드를 손실로 강제 close"""
        entry_price = float(trade.get("entry_price") or 0.0)
        size = float(trade.get("size") or 0.0)
        pnl = -size  # full dollar cost lost
        trade_id = int(trade["id"])
        trade_mode = str(trade.get("mode") or "paper")
        self.db_logger.close_trade(trade_id, 0.0, pnl, update_daily=trade_mode != "shadow")
        if trade_mode == "shadow":
            self._active_shadow_keys.discard(self._shadow_key(str(trade["market_id"]), str(trade.get("side", ""))))
        else:
            self._active_market_ids.discard(str(trade["market_id"]))
        logger.info(
            f"[{trade_mode.upper()}] CLOSED #{trade_id} LOSS ${pnl:+.2f} | "
            f"{trade.get('market_group', '-')}/{trade.get('market_question', '')[:30]} "
            f"entry={entry_price:.3f} via={reason}"
        )
        return True

    async def _resolve_trade(self, trade: dict[str, Any]):
        market_id = str(trade["market_id"])
        trade_mode = str(trade.get("mode") or self.mode)
        if trade_mode in {"paper", "shadow"} and market_id.startswith("sim_"):
            return __import__("random").random() < float(trade.get("model_prob", 0.5) or 0.5)
        # Primary: CLOB API (authoritative on-chain resolution)
        try:
            market_data = self.scanner._get_clob_client().get_market(market_id)
            if isinstance(market_data, dict) and market_data.get("closed"):
                tokens = market_data.get("tokens", [])
                side = trade.get("side", "UP")
                for tok in tokens:
                    outcome_name = str(tok.get("outcome", "")).lower()
                    is_winner = tok.get("winner", False)
                    if outcome_name in ("up", "yes") and side == "UP" and is_winner:
                        return True
                    if outcome_name in ("down", "no") and side == "DOWN" and is_winner:
                        return True
                if any(tok.get("winner") for tok in tokens):
                    return False
        except Exception as exc:
            logger.warning(f"CLOB resolution check error for {market_id}: {exc}")
        # Fallback: gamma API
        try:
            response = await self.scanner._gamma_client.get("/markets", params={"id": market_id})
            if response.status_code == 200:
                data = response.json()
                markets = data if isinstance(data, list) else [data]
                for market in markets:
                    if market.get("resolved"):
                        outcome = str(market.get("outcome", "")).lower()
                        side = trade.get("side", "UP")
                        return outcome == ("up" if side == "UP" else "down")
        except Exception as exc:
            logger.warning(f"Gamma resolution check error for {market_id}: {exc}")

        expiry = float(trade.get("expiry_time", 0))
        duration_sec = float(trade.get("market_duration_min") or 15.0) * 60.0
        window_start_ts = expiry - duration_sec
        asset_symbol = str(trade.get("asset_symbol") or "BTC").upper()
        now = time.time()
        if now > expiry + 60:
            try:
                import httpx

                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self.config.binance_rest_url}/api/v3/klines",
                        params={
                            "symbol": f"{asset_symbol}USDT",
                            "interval": "1m",
                            "startTime": int(window_start_ts * 1000),
                            "endTime": int(expiry * 1000),
                            "limit": max(16, int(duration_sec // 60) + 2),
                        },
                    )
                    if resp.status_code == 200:
                        klines = resp.json()
                        if len(klines) >= 2:
                            start_price = float(klines[0][1])
                            end_price = float(klines[-1][4])
                            went_up = end_price >= start_price
                            side = trade.get("side", "UP")
                            logger.info(f"Self-resolving #{trade['id']}: {asset_symbol} {start_price:.2f}->{end_price:.2f} ({'UP' if went_up else 'DOWN'}), side={side}")
                            return went_up if side == "UP" else not went_up
            except Exception as exc:
                logger.warning(f"Binance kline fetch error for {asset_symbol}: {exc}")

            signal_vals = trade.get("signal_values", {})
            if isinstance(signal_vals, str):
                try:
                    signal_vals = json.loads(signal_vals)
                except Exception:
                    signal_vals = {}
            entry_price = signal_vals.get("price", 0) or signal_vals.get("vwap", 0)
            engine = self.price_engines.get(asset_symbol)
            current_price = engine.latest_price if engine else 0
            if entry_price > 0 and current_price > 0:
                went_up = current_price >= entry_price
                side = trade.get("side", "UP")
                logger.info(f"Self-resolving #{trade['id']} (fallback): {asset_symbol} {entry_price:.2f}->{current_price:.2f} ({'UP' if went_up else 'DOWN'}), side={side}")
                return went_up if side == "UP" else not went_up
        return None

    async def _dashboard_loop(self):
        ml_log_counter = 0
        while self._running:
            try:
                self._refresh_dashboard_status()
                if not self._dashboard_disabled:
                    self.dashboard.render(
                        btc_price=self.price_engines[self.primary_asset].latest_price,
                        signal=self.price_engines[self.primary_asset].latest_signal,
                        mode=self.mode,
                        halted=self.risk_manager.is_halted,
                        halt_reason=self.risk_manager.halt_reason,
                    )
                # Log ML performance periodically (every ~60 seconds)
                ml_log_counter += 1
                if self.ml_model is not None and ml_log_counter % 30 == 0:
                    perf = self.ml_model.get_performance_summary()
                    if perf["n_closed_returns"] > 0:
                        logger.info(
                            f"[ML] SR={perf['sharpe_ratio']:.2f}({perf['sharpe_label']}) "
                            f"trained={perf['ml_trained']} n_train={perf['train_count']} "
                            f"tracks={perf['active_tracks']} avg_ret={perf['avg_return']:.4f}"
                        )
                self._dashboard_failures = 0
            except Exception as exc:
                self._dashboard_failures += 1
                if self._dashboard_failures >= 3:
                    self._dashboard_disabled = True
                    logger.warning(f"Terminal dashboard disabled after repeated errors: {exc}")
                else:
                    logger.error(f"Dashboard error: {exc}")
            await asyncio.sleep(2)


def setup_logging(level: str = "INFO", log_path: str | None = None):
    log_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(formatter)
    target_log_path = Path(log_path) if log_path else Path("bot.log")
    target_log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(target_log_path, mode="a", encoding="utf-8", maxBytes=2_000_000, backupCount=5)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(log_level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    for lib in ("httpx", "httpcore", "hpack", "websockets", "websockets.client", "websockets.server", "asyncio", "rlp"):
        logging.getLogger(lib).setLevel(logging.WARNING)
        logging.getLogger(lib).propagate = False


def _acquire_singleton_lock():
    """Prevent duplicate bot instances (root cause of 2026-04-24 6x-stack losses)."""
    import atexit
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.lock")
    if os.path.exists(lock_path):
        try:
            with open(lock_path) as f:
                other_pid = int(f.read().strip())
            import psutil
            if psutil.pid_exists(other_pid):
                proc = psutil.Process(other_pid)
                if "python" in proc.name().lower() or "py.exe" in proc.name().lower():
                    print(f"ERROR: Another bot instance is running (PID {other_pid}). Kill it first or delete {lock_path} if stale.")
                    sys.exit(1)
        except (ValueError, psutil.NoSuchProcess, ImportError, OSError):
            pass
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))
    def _release():
        try:
            if os.path.exists(lock_path):
                with open(lock_path) as f:
                    if int(f.read().strip()) == os.getpid():
                        os.remove(lock_path)
        except (ValueError, OSError):
            pass
    atexit.register(_release)


def main():
    parser = argparse.ArgumentParser(description="Polymarket multi-market trading bot")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper", help="Trading mode: paper or live")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()
    _acquire_singleton_lock()
    config = Config()
    if args.mode == "live" and (not config.clob_api_key or not config.private_key):
        print("ERROR: Live mode requires POLYMARKET_API_KEY and PRIVATE_KEY in .env")
        sys.exit(1)
    setup_logging(args.log_level, config.log_path)
    bot = TradingBot(config, mode=args.mode)
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("Bot stopped by user.")


if __name__ == "__main__":
    main()










