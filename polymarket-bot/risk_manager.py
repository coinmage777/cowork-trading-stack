"""Kelly criterion position sizing and risk management enforcement."""

from dataclasses import dataclass
from config import Config
from data_logger import DataLogger


@dataclass
class SizingResult:
    should_trade: bool
    bet_size: float
    reason: str
    kelly_raw: float
    kelly_fractional: float


class RiskManager:
    def __init__(self, config: Config, logger: DataLogger):
        self.config = config
        self.logger = logger
        self._halted = False
        self._halt_reason = ""

    def _tracked_open_trades(self) -> list[dict]:
        return [trade for trade in self.logger.get_open_trades()
                if trade.get("mode") != "shadow" and trade.get("market_group") != "weather"]

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def check_daily_stop_loss(self) -> bool:
        daily_pnl = self.logger.get_today_realized_pnl()
        if daily_pnl <= self.config.daily_stop_loss:
            self._halted = True
            self._halt_reason = f"Daily stop loss: ${daily_pnl:.2f}"
            return True
        return False

    def check_position_limits(self) -> bool:
        open_trades = self._tracked_open_trades()
        return len(open_trades) < self.config.max_open_positions

    def get_current_exposure(self) -> float:
        open_trades = self._tracked_open_trades()
        return sum(t["size"] for t in open_trades)

    def get_direction_balance(self) -> dict:
        """Count how many UP vs DOWN positions are open."""
        open_trades = self._tracked_open_trades()
        up_count = sum(1 for t in open_trades if t.get("side") == "UP")
        down_count = sum(1 for t in open_trades if t.get("side") == "DOWN")
        return {"up": up_count, "down": down_count, "total": len(open_trades)}

    def calculate_kelly_size(self, model_prob: float, market_prob: float,
                              minutes_to_expiry: float,
                              market_liquidity: float) -> SizingResult:
        # Pre-flight risk checks
        if self._halted:
            return SizingResult(False, 0.0, f"Halted: {self._halt_reason}", 0.0, 0.0)

        if self.check_daily_stop_loss():
            return SizingResult(False, 0.0, self._halt_reason, 0.0, 0.0)

        if not self.check_position_limits():
            return SizingResult(False, 0.0, "Max open positions reached", 0.0, 0.0)

        if minutes_to_expiry < self.config.min_time_to_expiry:
            return SizingResult(False, 0.0,
                                f"Too close to expiry: {minutes_to_expiry:.1f}m", 0.0, 0.0)

        if market_liquidity < self.config.min_market_liquidity:
            return SizingResult(False, 0.0,
                                f"Low liquidity: ${market_liquidity:.0f}", 0.0, 0.0)

        # Edge calculation
        edge = model_prob - market_prob
        if abs(edge) < self.config.min_edge_threshold:
            return SizingResult(False, 0.0,
                                f"Edge too small: {abs(edge)*100:.1f}%", 0.0, 0.0)

        if market_prob >= 1.0 or market_prob <= 0.0:
            return SizingResult(False, 0.0, "Invalid market probability", 0.0, 0.0)

        # Kelly criterion
        odds = (1.0 - market_prob) / market_prob
        kelly_raw = (model_prob * odds - (1.0 - model_prob)) / odds
        kelly_raw = max(kelly_raw, 0.0)

        kelly_fractional = kelly_raw * self.config.kelly_fraction

        # Size in USDC - use kelly fraction of max bet, with floor
        # kelly_fractional typically 0.001-0.05 → scale to meaningful bet
        bet_size = kelly_fractional * self.config.max_single_bet / self.config.kelly_fraction
        # Cheap tokens: can afford larger positions since risk per share is small
        if market_prob < 0.15:
            bet_size = min(bet_size * 1.5, self.config.max_single_bet)
        elif market_prob < 0.30:
            bet_size = min(bet_size * 1.2, self.config.max_single_bet)
        else:
            bet_size = min(bet_size, self.config.max_single_bet)
        # Ensure at least min_bet when edge is meaningful
        if edge >= self.config.min_edge_threshold and bet_size < self.config.min_bet_size:
            bet_size = self.config.min_bet_size

        # Scale down if near expiry (less time = less edge certainty)
        if minutes_to_expiry < 5.0:
            time_scale = minutes_to_expiry / 5.0
            bet_size *= max(time_scale, 0.5)

        # Check portfolio exposure limit
        current_exposure = self.get_current_exposure()
        remaining_capacity = self.config.max_portfolio_exposure - current_exposure
        if remaining_capacity <= 0:
            return SizingResult(False, 0.0,
                                f"Exposure limit: ${current_exposure:.0f}",
                                kelly_raw, kelly_fractional)
        bet_size = min(bet_size, remaining_capacity)

        # Minimum bet size
        if bet_size < self.config.min_bet_size:
            return SizingResult(False, 0.0,
                                f"Bet too small: ${bet_size:.2f}", kelly_raw, kelly_fractional)

        # Final edge check
        if abs(edge) < self.config.min_entry_edge:
            return SizingResult(False, 0.0,
                                f"Edge below entry: {abs(edge)*100:.1f}%",
                                kelly_raw, kelly_fractional)

        return SizingResult(
            should_trade=True,
            bet_size=round(bet_size, 2),
            reason=f"Edge: {edge*100:.1f}%, Kelly: {kelly_fractional*100:.1f}%",
            kelly_raw=kelly_raw,
            kelly_fractional=kelly_fractional,
        )

    def reset_halt(self):
        self._halted = False
        self._halt_reason = ""
