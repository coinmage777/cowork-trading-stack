"""Paper-mode parameter tuner that nudges strategy settings based on recent results."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import Config
from data_logger import DataLogger


@dataclass
class OptimizerProfile:
    name: str
    min_edge_threshold: float
    min_entry_edge: float
    kelly_fraction: float
    max_single_bet: float
    min_market_liquidity: float
    min_time_to_expiry: float
    max_trades_per_scan: int

    def to_config_dict(self) -> dict:
        return asdict(self)


class PaperOptimizer:
    def __init__(self, config: Config, db_logger: DataLogger):
        self.config = config
        self.db_logger = db_logger
        self.reference_group = config.performance_reference_group
        self.state_path = Path(config.base_dir) / "paper_optimizer_state.json"
        self._profiles = self._build_profiles()
        self._profiles_by_name = {profile.name: profile for profile in self._profiles}
        self._regime_rotation = {
            "defensive": ["P01", "P02"],
            "cautious": ["P02", "P03", "P04"],
            "balanced": ["P03", "P04", "P05"],
            "profitable": ["P03", "P04", "P05", "P06"],
        }
        self._status = {
            "enabled": bool(config.paper_optimizer_enabled),
            "phase": "idle",
            "regime": "warming_up",
            "profile_name": "P01",
            "reference_group": self.reference_group,
            "sample_size": 0,
            "sample_pnl": 0.0,
            "sample_win_rate": 0.0,
            "best_pnl": 0.0,
            "today_paper_pnl": 0.0,
            "today_closed_trades": 0,
            "profitable_day": False,
            "loss_streak": 0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_pnl": 0.0,
            "risk_scale": 1.0,
            "inactivity_minutes": None,
            "inactivity_risk_cap_applied": False,
            "blocked_strategies": [],
            "blocked_strategy_sides": [],
            "preferred_strategy_sides": [],
            "penalized_strategy_sides": [],
            "message": "waiting for enough paper trades",
            "active_config": {},
        }
        self._best_profile_name = "P03"
        self._last_regime = "warming_up"
        self._load_state()
        self._apply_profile(self._profiles_by_name.get(self._best_profile_name, self._profiles[0]), regime="warming_up", log_event=False, notes="initial profile")

    def _build_profiles(self) -> list[OptimizerProfile]:
        return [
            OptimizerProfile("P01", 0.025, 0.015, 0.12, 10.0, 800.0, 7.0, 1),
            OptimizerProfile("P02", 0.022, 0.013, 0.14, 12.0, 600.0, 6.0, 1),
            OptimizerProfile("P03", 0.020, 0.012, 0.16, 15.0, 500.0, 5.0, 1),
            OptimizerProfile("P04", 0.018, 0.010, 0.18, 18.0, 400.0, 5.0, 1),
            OptimizerProfile("P05", 0.015, 0.008, 0.20, 20.0, 300.0, 4.0, 1),
            OptimizerProfile("P06", 0.012, 0.006, 0.22, 25.0, 200.0, 3.0, 2),
        ]

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        profile_name = payload.get("best_profile_name")
        if profile_name in self._profiles_by_name:
            self._best_profile_name = profile_name
        self._last_regime = payload.get("last_regime", self._last_regime)

    def _save_state(self):
        payload = {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "best_profile_name": self._best_profile_name,
            "last_regime": self._last_regime,
            "status": {
                key: self._status.get(key)
                for key in (
                    "profile_name", "phase", "regime", "reference_group", "sample_pnl", "sample_size",
                    "today_paper_pnl", "today_closed_trades", "profitable_day", "risk_scale", "inactivity_minutes",
                    "inactivity_risk_cap_applied", "blocked_strategies", "blocked_strategy_sides",
                    "preferred_strategy_sides", "penalized_strategy_sides",
                )
            },
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _profile_from_history(self, allowed_names: list[str]) -> OptimizerProfile:
        history = self.db_logger.get_profile_performance(mode="paper", market_group=self.reference_group)
        best_name = None
        best_score = float("-inf")
        for row in history:
            name = row.get("profile_name")
            trade_count = int(row.get("trade_count") or 0)
            if name not in allowed_names or trade_count < 3:
                continue
            score = float(row.get("total_pnl") or 0.0) + float(row.get("win_rate") or 0.0) * 4.0 + float(row.get("avg_pnl") or 0.0)
            if score > best_score:
                best_score = score
                best_name = name
        preferred = self._best_profile_name if self._best_profile_name in allowed_names else allowed_names[0]
        return self._profiles_by_name.get(best_name or preferred, self._profiles[0])

    def _compute_stats(self, recent: list[dict]) -> dict:
        sample_size = len(recent)
        sample_pnl = sum(float(trade.get("pnl") or 0.0) for trade in recent)
        wins = sum(1 for trade in recent if float(trade.get("pnl") or 0.0) > 0)
        gross_profit = sum(float(trade.get("pnl") or 0.0) for trade in recent if float(trade.get("pnl") or 0.0) > 0)
        gross_loss = abs(sum(float(trade.get("pnl") or 0.0) for trade in recent if float(trade.get("pnl") or 0.0) < 0))
        profit_factor = gross_profit / gross_loss if gross_loss else (999.0 if gross_profit > 0 else 0.0)
        avg_pnl = sample_pnl / sample_size if sample_size else 0.0
        running = 0.0
        peak = 0.0
        max_drawdown = 0.0
        loss_streak = 0
        current_loss_streak = 0
        for trade in reversed(recent):
            pnl = float(trade.get("pnl") or 0.0)
            running += pnl
            peak = max(peak, running)
            max_drawdown = min(max_drawdown, running - peak)
            if pnl < 0:
                current_loss_streak += 1
                loss_streak = max(loss_streak, current_loss_streak)
            else:
                current_loss_streak = 0
        recent_loss_streak = 0
        for trade in recent:
            if float(trade.get("pnl") or 0.0) < 0:
                recent_loss_streak += 1
            else:
                break
        return {
            "sample_size": sample_size,
            "sample_pnl": sample_pnl,
            "sample_win_rate": wins / sample_size if sample_size else 0.0,
            "profit_factor": profit_factor,
            "avg_pnl": avg_pnl,
            "max_drawdown": max_drawdown,
            "loss_streak": recent_loss_streak,
            "worst_loss_streak": loss_streak,
        }

    def _side_biases(self) -> tuple[list[str], list[str], list[str], list[str]]:
        blocked: list[str] = []
        blocked_sides: list[str] = []
        preferred_sides: list[str] = []
        penalized_sides: list[str] = []
        for row in self.db_logger.get_strategy_performance(mode="paper", market_group=self.reference_group):
            name = row.get("strategy_name") or ""
            trade_count = int(row.get("trade_count") or 0)
            total_pnl = float(row.get("total_pnl") or 0.0)
            win_rate = float(row.get("win_rate") or 0.0)
            if trade_count >= 8 and total_pnl < -3.0 and win_rate < 0.35:
                blocked.append(name)
        side_rows = self.db_logger.get_strategy_side_performance(mode="paper", market_group=self.reference_group)
        by_strategy: dict[str, dict[str, dict]] = {}
        for row in side_rows:
            strategy = row.get("strategy_name") or ""
            side = row.get("side") or ""
            if not strategy or not side:
                continue
            by_strategy.setdefault(strategy, {})[side] = row
            trade_count = int(row.get("trade_count") or 0)
            total_pnl = float(row.get("total_pnl") or 0.0)
            win_rate = float(row.get("win_rate") or 0.0)
            key = f"{strategy}:{side}"
            if trade_count >= 2 and total_pnl < 0 and win_rate <= 0.34:
                blocked_sides.append(key)
            elif trade_count >= 4 and total_pnl > 0 and win_rate >= 0.5:
                preferred_sides.append(key)
            elif trade_count >= 2 and total_pnl < 0 and win_rate < 0.5:
                penalized_sides.append(key)
        for strategy, sides in by_strategy.items():
            up_row = sides.get("UP")
            down_row = sides.get("DOWN")
            if down_row and int(down_row.get("trade_count") or 0) >= 4 and float(down_row.get("total_pnl") or 0.0) > 0:
                key = f"{strategy}:DOWN"
                if key not in preferred_sides:
                    preferred_sides.append(key)
                if up_row and float(up_row.get("total_pnl") or 0.0) <= 0:
                    up_key = f"{strategy}:UP"
                    if up_key not in penalized_sides and up_key not in blocked_sides:
                        penalized_sides.append(up_key)
        return blocked, blocked_sides, preferred_sides, penalized_sides

    def _risk_scale_for_regime(self, regime: str, stats: dict) -> float:
        if regime == "defensive":
            return 0.55
        if regime == "cautious":
            return 0.80
        if regime == "profitable":
            return 1.00
        return 0.95 if stats["profit_factor"] >= 1.2 and stats["sample_pnl"] > 0 else 0.85

    def _minutes_since_last_reference_paper_close(self) -> int | None:
        latest = self.db_logger.get_closed_trades(mode="paper", limit=1, market_group=self.reference_group)
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


    def _select_regime(self, stats: dict, today_paper_pnl: float, today_trade_count: int) -> tuple[str, str, str]:
        profitable_day = today_trade_count >= self.config.optimizer_day_profit_min_trades and today_paper_pnl > 0
        if stats["sample_size"] < self.config.optimizer_min_closed_trades:
            return "warming_up", "balanced", "표본이 아직 적어서 균형 모드로 관찰 중입니다."
        if profitable_day and stats["profit_factor"] >= 1.1:
            return "profitable", "profitable", "오늘 페이퍼 손익이 플러스라 현재 설정을 유지합니다."
        # Only go defensive on severe drawdown, not short loss streaks
        if stats["sample_pnl"] <= -15 or stats["max_drawdown"] <= -20:
            return "tuning", "defensive", "심각한 낙폭으로 방어 모드 전환합니다."
        # Positive PnL with loss streak → cautious, not defensive
        if stats["loss_streak"] >= 4 and stats["sample_pnl"] <= 0:
            return "tuning", "cautious", "연속 손실이 이어져 보수적으로 조정합니다."
        if stats["sample_pnl"] > 0 and stats["sample_win_rate"] >= 0.40:
            return "holding", "balanced", "최근 표본이 양호하여 균형형 설정을 유지합니다."
        if stats["sample_pnl"] < 0 or stats["sample_win_rate"] < 0.40:
            return "tuning", "cautious", "최근 성과가 약해서 진입 기준을 보수적으로 조정합니다."
        return "holding", "balanced", "최근 표본이 안정적이라 균형형 설정을 유지합니다."

    def _apply_profile(self, profile: OptimizerProfile, regime: str, log_event: bool = True, notes: str = ""):
        config_map = profile.to_config_dict()
        for key in ("min_edge_threshold", "min_entry_edge", "kelly_fraction", "max_single_bet", "min_market_liquidity", "min_time_to_expiry", "max_trades_per_scan"):
            setattr(self.config, key, config_map[key])
        self._status["profile_name"] = profile.name
        self._status["regime"] = regime
        self._status["active_config"] = {key: config_map[key] for key in ("min_edge_threshold", "min_entry_edge", "kelly_fraction", "max_single_bet", "min_market_liquidity", "min_time_to_expiry", "max_trades_per_scan")}
        self._last_regime = regime
        self._save_state()
        if log_event:
            self.db_logger.log_optimizer_event(
                mode="paper",
                profile_name=profile.name,
                sample_size=int(self._status.get("sample_size", 0)),
                sample_pnl=float(self._status.get("sample_pnl", 0.0)),
                sample_win_rate=float(self._status.get("sample_win_rate", 0.0)),
                active_config=self._status["active_config"],
                notes=notes,
            )

    def evaluate(self) -> dict:
        recent = self.db_logger.get_closed_trades(mode="paper", limit=self.config.optimizer_recent_trade_window, market_group=self.reference_group)
        today_trades = self.db_logger.get_closed_trades_for_today(mode="paper", market_group=self.reference_group)
        today_paper_pnl = sum(float(trade.get("pnl") or 0.0) for trade in today_trades)
        profitable_day = len(today_trades) >= self.config.optimizer_day_profit_min_trades and today_paper_pnl > 0
        stats = self._compute_stats(recent)
        phase, regime, regime_message = self._select_regime(stats, today_paper_pnl, len(today_trades))
        next_profile = self._profile_from_history(self._regime_rotation.get(regime, ["P03"]))
        blocked_strategies, blocked_strategy_sides, preferred_strategy_sides, penalized_strategy_sides = self._side_biases()
        risk_scale = self._risk_scale_for_regime(regime, stats)
        inactivity_minutes = self._minutes_since_last_reference_paper_close()
        inactivity_risk_cap_applied = False
        inactivity_risk_cap_minutes = 72 * 60  # 3 days instead of 1 day
        inactivity_risk_cap_value = 0.65  # less punitive cap
        inactivity_block_reset_applied = False
        if inactivity_minutes is not None and inactivity_minutes >= inactivity_risk_cap_minutes and risk_scale > inactivity_risk_cap_value:
            risk_scale = inactivity_risk_cap_value
            inactivity_risk_cap_applied = True
            regime_message = f"{regime_message} 최근 기준 그룹 체결 공백이 길어 위험 스케일을 {inactivity_risk_cap_value:.2f}로 제한합니다."
        block_reset_minutes = max(0, int(getattr(self.config, "optimizer_inactivity_block_reset_minutes", 0)))
        if block_reset_minutes and inactivity_minutes is not None and inactivity_minutes >= block_reset_minutes:
            if blocked_strategies or blocked_strategy_sides:
                inactivity_block_reset_applied = True
                blocked_strategies = []
                blocked_strategy_sides = []
                regime_message = (
                    f"{regime_message} 기준 그룹 체결 공백이 {inactivity_minutes}분 이어져 차단을 잠시 풀고 재표본 수집을 재개합니다."
                )
        if stats["sample_pnl"] > self._status.get("best_pnl", float("-inf")):
            self._best_profile_name = next_profile.name
        self._status.update({
            **stats,
            "best_pnl": max(float(self._status.get("best_pnl", 0.0)), stats["sample_pnl"]),
            "today_paper_pnl": today_paper_pnl,
            "today_closed_trades": len(today_trades),
            "profitable_day": profitable_day,
            "phase": phase,
            "message": regime_message,
            "risk_scale": risk_scale,
            "inactivity_minutes": inactivity_minutes,
            "inactivity_risk_cap_applied": inactivity_risk_cap_applied,
            "inactivity_block_reset_applied": inactivity_block_reset_applied,
            "blocked_strategies": blocked_strategies,
            "blocked_strategy_sides": blocked_strategy_sides,
            "preferred_strategy_sides": preferred_strategy_sides,
            "penalized_strategy_sides": penalized_strategy_sides,
            "reference_group": self.reference_group,
        })
        changed = self._status.get("profile_name") != next_profile.name or self._status.get("regime") != regime
        notes = (
            f"group={self.reference_group} {regime_message} recent_pnl=${stats['sample_pnl']:+.2f}, win_rate={stats['sample_win_rate']:.1%}, "
            f"loss_streak={stats['loss_streak']}, drawdown=${stats['max_drawdown']:+.2f}, risk_scale={risk_scale:.2f}, "
            f"blocked={','.join(blocked_strategies) if blocked_strategies else '-'}, "
            f"blocked_sides={','.join(blocked_strategy_sides) if blocked_strategy_sides else '-'}, "
            f"preferred={','.join(preferred_strategy_sides) if preferred_strategy_sides else '-'}, "
            f"penalized={','.join(penalized_strategy_sides) if penalized_strategy_sides else '-'}"
        )
        if changed:
            self._apply_profile(next_profile, regime=regime, notes=notes)
        else:
            self._apply_profile(next_profile, regime=regime, log_event=False)
            self._save_state()
        return self.status()

    def status(self) -> dict:
        return dict(self._status)
