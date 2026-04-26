"""Simple terminal dashboard with paper performance, recovery, and optimizer history."""

import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from data_logger import DataLogger
from strategy import SignalOutput

W = 110
logger = logging.getLogger("polybot")


class Dashboard:
    def __init__(self, logger: DataLogger):
        self.logger = logger
        self._last_render = 0.0
        self.render_interval = 2.0
        self._render_enabled = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._render_disabled_reason = "stdout is not a TTY" if not self._render_enabled else ""
        self._last_scan_info = ""
        self._last_trade_info = ""
        self._price_history: deque[float] = deque(maxlen=20)
        self._markets_info: list[dict] = []
        self._balance = 0.0
        self._recovery_status: Optional[dict] = None
        self._optimizer_status: Optional[dict] = None
        self._paper_gate_status: Optional[dict] = None

    def set_scan_info(self, info: str):
        self._last_scan_info = info

    def set_trade_info(self, info: str):
        self._last_trade_info = info

    def add_price_history(self, price: float):
        self._price_history.append(price)

    def set_markets_info(self, markets_data: list[dict]):
        self._markets_info = markets_data

    def set_balance(self, balance: float):
        self._balance = balance

    def set_recovery_status(self, status: Optional[dict]):
        self._recovery_status = status

    def set_optimizer_status(self, status: Optional[dict]):
        self._optimizer_status = status

    def set_paper_gate_status(self, status: Optional[dict]):
        self._paper_gate_status = status

    def _line(self, text: str = "") -> str:
        return text[:W]

    def _rule(self, char: str = "-") -> str:
        return char * W

    def _format_pct(self, value: float) -> str:
        return f"{value * 100:+.1f}%"

    def _progress_bar(self, ratio: float, width: int = 24) -> str:
        ratio = max(0.0, min(1.0, ratio))
        filled = int(round(ratio * width))
        return "#" * filled + "." * (width - filled)

    def _paper_summary(self) -> dict:
        closed_today = self.logger.get_closed_trades_for_today(mode="paper")
        recent = self.logger.get_closed_trades(mode="paper", limit=20)
        total = len(closed_today)
        today_pnl = sum((trade.get("pnl", 0.0) or 0.0) for trade in closed_today)
        wins = sum(1 for trade in closed_today if (trade.get("pnl", 0.0) or 0.0) > 0)
        losses = sum(1 for trade in closed_today if (trade.get("pnl", 0.0) or 0.0) < 0)
        gross_profit = sum((trade.get("pnl", 0.0) or 0.0) for trade in closed_today if (trade.get("pnl", 0.0) or 0.0) > 0)
        gross_loss = abs(sum((trade.get("pnl", 0.0) or 0.0) for trade in closed_today if (trade.get("pnl", 0.0) or 0.0) < 0))
        win_rate = wins / total if total else 0.0
        avg_pnl = today_pnl / total if total else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        streak = 0
        streak_type = ""
        for trade in closed_today:
            pnl = trade.get("pnl", 0.0) or 0.0
            current_type = "W" if pnl > 0 else "L" if pnl < 0 else "F"
            if streak == 0:
                streak = 1
                streak_type = current_type
            elif current_type == streak_type:
                streak += 1
            else:
                break

        recent_pnl = sum((trade.get("pnl", 0.0) or 0.0) for trade in recent)
        recent_wins = sum(1 for trade in recent if (trade.get("pnl", 0.0) or 0.0) > 0)
        recent_wr = recent_wins / len(recent) if recent else 0.0
        return {
            "today_pnl": today_pnl,
            "today_trades": total,
            "today_win_rate": win_rate,
            "today_avg_pnl": avg_pnl,
            "today_profit_factor": profit_factor,
            "wins": wins,
            "losses": losses,
            "streak": streak,
            "streak_type": streak_type,
            "recent_pnl": recent_pnl,
            "recent_wr": recent_wr,
            "recent_count": len(recent),
            "goal_hit": total >= 12 and today_pnl > 0,
        }

    def render(self, btc_price: float, signal: Optional[SignalOutput], mode: str = "paper",
               halted: bool = False, halt_reason: str = ""):
        if not self._render_enabled:
            return

        now = time.time()
        if now - self._last_render < self.render_interval:
            return
        self._last_render = now

        if btc_price > 0:
            self.add_price_history(btc_price)

        try:
            os.system("cls" if os.name == "nt" else "clear")
        except OSError as exc:
            self._render_enabled = False
            self._render_disabled_reason = str(exc)
            logger.warning(f"Terminal dashboard disabled: clear failed ({exc})")
            return

        paper_summary = self._paper_summary() if mode == "paper" else None
        optimizer_events = self.logger.get_recent_optimizer_events(limit=5) if mode == "paper" else []

        lines: list[str] = []
        lines.append(self._rule("="))
        lines.append(self._line(f"POLYMARKET BTC TERMINAL  [{mode.upper()}]  {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"))
        if halted:
            lines.append(self._line(f"HALTED: {halt_reason}"))
        if mode == "paper":
            goal_text = "GOAL: positive paper day with enough trades before trusting live mode"
            lines.append(self._line(goal_text))
        lines.append(self._rule("="))

        if btc_price > 0:
            change = 0.0
            pct = 0.0
            if len(self._price_history) >= 2 and self._price_history[0] != 0:
                change = btc_price - self._price_history[0]
                pct = (change / self._price_history[0]) * 100
            lines.append(self._line(f"BTC/USDT: ${btc_price:,.2f}   Change: {change:+,.2f} ({pct:+.2f}%)"))
        else:
            lines.append("BTC/USDT: waiting for price feed...")

        if signal is None:
            lines.append("Signals: warming up...")
        else:
            lines.append(self._line(
                f"Signals  RSI {signal.rsi:5.1f} | Momentum {signal.momentum:+.3f} | Bias {signal.direction_bias:+.3f} | P(Up) {signal.model_prob_up:.1%}"
            ))
            lines.append(self._line(
                f"Bands    Low ${signal.bb_lower:,.0f} | Mid ${signal.bb_mid:,.0f} | High ${signal.bb_upper:,.0f} | VWAP ${signal.vwap:,.0f}"
            ))

        if paper_summary:
            lines.append(self._rule())
            lines.append("Paper Scoreboard")
            lines.append(self._line(
                f"  Today ${paper_summary['today_pnl']:+.2f} | Trades {paper_summary['today_trades']} | Win rate {paper_summary['today_win_rate']:.1%} | Avg/trade ${paper_summary['today_avg_pnl']:+.2f} | PF {paper_summary['today_profit_factor']:.2f}"
            ))
            lines.append(self._line(
                f"  Wins {paper_summary['wins']} / Losses {paper_summary['losses']} | Streak {paper_summary['streak']}{paper_summary['streak_type'] or '-'} | Recent20 ${paper_summary['recent_pnl']:+.2f} | Recent20 WR {paper_summary['recent_wr']:.1%}"
            ))
            goal_ratio = 1.0 if paper_summary['goal_hit'] else min(1.0, max(0.0, paper_summary['today_trades'] / 12.0))
            goal_label = "YES" if paper_summary['goal_hit'] else "NO"
            lines.append(self._line(
                f"  Profit goal hit {goal_label} | {self._progress_bar(goal_ratio)} {goal_ratio * 100:5.1f}% of minimum 12-trade validation window"
            ))

        lines.append(self._rule())
        lines.append("Active Markets")
        if not self._markets_info:
            lines.append("  none yet")
        else:
            for market in self._markets_info[:5]:
                best_edge = max(market.get("edge_up", 0), market.get("edge_down", 0))
                question = market.get("question", "?")[:50]
                position = "Y" if market.get("has_position") else "N"
                lines.append(self._line(
                    f"  {question:<50} up {market.get('up', 0):.3f} dn {market.get('down', 0):.3f} edge {self._format_pct(best_edge):>7} exp {market.get('expiry', 0):>4.0f}m pos {position}"
                ))
        if self._last_scan_info:
            lines.append(self._line(f"Scan: {self._last_scan_info}"))

        lines.append(self._rule())
        lines.append("Open Positions")
        open_trades = self.logger.get_open_trades(mode=mode)
        if not open_trades:
            lines.append("  none")
        else:
            total_exposure = 0.0
            for trade in open_trades[:6]:
                total_exposure += trade["size"]
                lines.append(self._line(
                    f"  #{trade['id']:<3} {trade.get('market_question', trade['market_id'])[:42]:<42} {trade['side']:<4} ${trade['size']:>6.2f} @ {trade['entry_price']:.4f} {trade.get('mode','?')}"
                ))
            lines.append(self._line(f"Total exposure: ${total_exposure:.2f}"))

        lines.append(self._rule())
        lines.append("Recent Paper History" if mode == "paper" else "Recent Trades")
        recent = self.logger.get_closed_trades(mode="paper", limit=8) if mode == "paper" else self.logger.get_all_closed_trades()[-8:]
        if not recent:
            lines.append("  none")
        else:
            for trade in recent:
                pnl = trade.get("pnl", 0.0) or 0.0
                lines.append(self._line(
                    f"  #{trade['id']:<3} {trade.get('timestamp','')[:16]:<16} {trade.get('market_question', trade['market_id'])[:36]:<36} {trade['side']:<4} pnl ${pnl:+.2f}"
                ))

        stats = self.logger.get_session_stats()
        daily = self.logger.get_daily_pnl()
        lines.append(self._rule())
        lines.append(self._line(
            f"Performance  Today ${daily['realized_pnl']:+.2f} | All-time ${stats['total_pnl']:+.2f} | Win rate {stats['win_rate']:.1%} | Trades {stats['total_trades']}"
        ))

        if self._recovery_status:
            lines.append(self._rule())
            lines.append("Recovery Status")
            state = "LOCKED" if self._recovery_status.get("locked") else "READY"
            lines.append(self._line(
                f"  Mode {state} | Start ${self._recovery_status['start_pnl']:+.2f} | Cumulative ${self._recovery_status.get('cumulative_pnl', 0.0):+.2f} | Effective ${self._recovery_status['effective_pnl']:+.2f}"
            ))
            lines.append(self._line(
                f"  Target ${self._recovery_status['target_pnl']:+.2f} | Remaining ${self._recovery_status['remaining_to_unlock']:.2f} | {self._progress_bar(self._recovery_status['progress_ratio'])} {self._recovery_status['progress_ratio'] * 100:5.1f}%"
            ))
            if self._recovery_status.get("message"):
                lines.append(self._line(f"  {self._recovery_status['message']}"))

        if self._paper_gate_status:
            lines.append(self._rule())
            lines.append("Live Gate Status")
            state = "READY" if self._paper_gate_status.get("ready") else "LOCKED"
            lines.append(self._line(
                f"  State {state} | Group {self._paper_gate_status.get('reference_group', '-')} | Sample ${self._paper_gate_status.get('sample_pnl', 0.0):+.2f} over {self._paper_gate_status.get('sample_size', 0)}/{self._paper_gate_status.get('required_trades', 0)} trades"
            ))
            lines.append(self._line(
                f"  WR {self._paper_gate_status.get('sample_win_rate', 0.0):.1%} (need {self._paper_gate_status.get('required_win_rate', 0.0):.1%}) | PF {self._paper_gate_status.get('sample_profit_factor', 0.0):.2f} (need {self._paper_gate_status.get('required_profit_factor', 0.0):.2f})"
            ))
            lines.append(self._line(
                f"  Win-rate check {'PASS' if self._paper_gate_status.get('passes_win_rate') else 'FAIL'} | PF check {'PASS' if self._paper_gate_status.get('passes_profit_factor') else 'FAIL'}"
            ))
            if self._paper_gate_status.get("message"):
                lines.append(self._line(f"  {self._paper_gate_status['message']}"))
        if self._optimizer_status:
            lines.append(self._rule())
            lines.append("Paper Optimizer")
            lines.append(self._line(
                f"  Phase {self._optimizer_status.get('phase', 'idle')} | Profile {self._optimizer_status.get('profile_name', '-')} | Sample ${self._optimizer_status.get('sample_pnl', 0.0):+.2f} over {self._optimizer_status.get('sample_size', 0)} trades | WR {self._optimizer_status.get('sample_win_rate', 0.0):.1%}"
            ))
            lines.append(self._line(
                f"  Today paper ${self._optimizer_status.get('today_paper_pnl', 0.0):+.2f} over {self._optimizer_status.get('today_closed_trades', 0)} trades | Best sample ${self._optimizer_status.get('best_pnl', 0.0):+.2f} | Profitable day {'YES' if self._optimizer_status.get('profitable_day') else 'NO'}"
            ))
            active = self._optimizer_status.get('active_config', {})
            if active:
                lines.append(self._line(
                    f"  Config edge {active.get('min_edge_threshold', 0):.3f} | entry {active.get('min_entry_edge', 0):.3f} | kelly {active.get('kelly_fraction', 0):.3f} | max bet ${active.get('max_single_bet', 0):.2f} | liq ${active.get('min_market_liquidity', 0):.0f}"
                ))
            if self._optimizer_status.get("message"):
                lines.append(self._line(f"  {self._optimizer_status['message']}"))

        if optimizer_events:
            lines.append(self._rule())
            lines.append("Optimizer History")
            for event in optimizer_events:
                lines.append(self._line(
                    f"  {event.get('timestamp','')[:16]} {event.get('profile_name','-'):>4} sample ${event.get('sample_pnl',0.0):+.2f} wr {event.get('sample_win_rate',0.0):.1%} n={event.get('sample_size',0)}"
                ))

        open_exposure = sum(t["size"] for t in open_trades)
        balance = self._balance if self._balance > 0 else 100.0
        lines.append(self._rule())
        lines.append(self._line(f"Balance ${balance:,.2f} | Exposure ${open_exposure:,.2f} | Available ${balance - open_exposure:,.2f}"))

        if self._last_trade_info:
            lines.append(self._rule())
            lines.append(self._line(f"Last trade: {self._last_trade_info}"))

        lines.append(self._rule("="))
        lines.append("bot.log | Ctrl+C to stop")
        try:
            print("\n".join(lines), flush=True)
        except OSError as exc:
            self._render_enabled = False
            self._render_disabled_reason = str(exc)
            logger.warning(f"Terminal dashboard disabled: print failed ({exc})")


