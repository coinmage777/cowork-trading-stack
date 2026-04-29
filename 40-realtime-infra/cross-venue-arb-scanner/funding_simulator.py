"""
Funding Rate Arbitrage Simulator
=================================
DB 히스토리 데이터로 백테스트 + 실시간 페이퍼 트레이딩.

백테스트:
  python -m strategies.funding_simulator --mode backtest --days 14
페이퍼:
  python -m strategies.funding_simulator --mode paper --config config.yaml
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# HL 기반 거래소 (서로 아비트라지 불가)
HL_BASED = {
    "hyperliquid", "hyperliquid_2", "miracle", "dreamcash", "based",
    "supercexy", "bullpen", "dexari", "liquid", "hyena", "hl_wallet_b",
    "hl_wallet_c", "katana", "decibel", "ethereal", "treadfi", "hyena_2",
}


@dataclass
class SimConfig:
    min_spread_8h: float = 0.0005    # 최소 스프레드 (8h, 소수점)
    close_spread_8h: float = 0.0001  # 청산 스프레드
    margin_per_leg: float = 50.0
    leverage: int = 5
    max_positions: int = 2
    max_hold_hours: float = 12.0
    exchange_fees: dict = field(default_factory=lambda: {
        "hyperliquid": 0.00035, "katana_independent": 0.00035,
        "dydx": 0.0005, "vertex": 0.0004, "edgex": 0.0005,
        "nado": 0.0005, "grvt": 0.0004, "lighter": 0.0004,
        "standx": 0.0005, "reya": 0.0005,
    })
    slippage_bps: float = 0.0002  # 양쪽 합계


@dataclass
class PaperPosition:
    id: int = 0
    symbol: str = ""
    long_exchange: str = ""
    short_exchange: str = ""
    entry_spread_8h: float = 0.0
    entry_time: str = ""
    margin_per_leg: float = 50.0
    leverage: int = 5
    funding_collected: float = 0.0
    fee_cost: float = 0.0


class FundingSimulator:

    def __init__(self, config: SimConfig, db_path: str = "funding_rates.db"):
        self.config = config
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.positions: list[PaperPosition] = []
        self.closed_trades: list[dict] = []

    def _get_fee(self, exchange: str) -> float:
        return self.config.exchange_fees.get(exchange, 0.0005)

    def _is_hl_pair(self, ex1: str, ex2: str) -> bool:
        """HL 기반끼리는 아비트라지 불가"""
        return ex1 in HL_BASED and ex2 in HL_BASED

    def _notional(self) -> float:
        return self.config.margin_per_leg * self.config.leverage

    # ── Backtest ──

    def backtest(self, days: int = 14):
        """DB 히스토리로 백테스트"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Get all spread snapshots
        rows = self.db.execute("""
            SELECT timestamp, symbol, max_exchange, min_exchange,
                   max_rate_8h, min_rate_8h, spread_8h, all_rates, actionable
            FROM funding_spreads
            WHERE timestamp > ?
            ORDER BY timestamp
        """, (cutoff,)).fetchall()

        if not rows:
            print("데이터 없음. funding_collector를 먼저 실행하세요.")
            return

        positions: list[PaperPosition] = []
        trades: list[dict] = []
        next_id = 1

        for row in rows:
            ts = row["timestamp"]
            symbol = row["symbol"]
            spread = row["spread_8h"]
            all_rates = json.loads(row["all_rates"]) if row["all_rates"] else {}

            # Check exits first
            for pos in list(positions):
                if pos.symbol != symbol:
                    continue

                current_long_rate = all_rates.get(pos.long_exchange, 0)
                current_short_rate = all_rates.get(pos.short_exchange, 0)
                current_spread = current_short_rate - current_long_rate

                # Funding collection estimate (per snapshot interval ~10min)
                interval_hours = 10 / 60  # 10 minutes
                notional = self._notional()
                funding_per_interval = current_spread * notional * interval_hours / 8
                pos.funding_collected += funding_per_interval

                # Check close conditions
                close_reason = None
                if current_spread <= self.config.close_spread_8h:
                    close_reason = "spread_narrowed"
                elif current_spread < 0:
                    close_reason = "spread_reversed"

                # Time-based exit
                try:
                    open_dt = datetime.fromisoformat(pos.entry_time)
                    now_dt = datetime.fromisoformat(ts)
                    hours_held = (now_dt - open_dt).total_seconds() / 3600
                    if hours_held >= self.config.max_hold_hours:
                        close_reason = "timeout"
                except Exception:
                    pass

                if close_reason:
                    pnl = pos.funding_collected - pos.fee_cost
                    trades.append({
                        "open_time": pos.entry_time,
                        "close_time": ts,
                        "symbol": pos.symbol,
                        "long_exchange": pos.long_exchange,
                        "short_exchange": pos.short_exchange,
                        "entry_spread": pos.entry_spread_8h,
                        "exit_spread": current_spread,
                        "funding_collected": pos.funding_collected,
                        "fee_cost": pos.fee_cost,
                        "pnl": pnl,
                        "close_reason": close_reason,
                    })
                    positions.remove(pos)

            # Check entry
            if len(positions) >= self.config.max_positions:
                continue

            max_ex = row["max_exchange"]
            min_ex = row["min_exchange"]

            # Skip HL-HL pairs
            if self._is_hl_pair(max_ex, min_ex):
                continue

            # Skip if already have position for this symbol
            if any(p.symbol == symbol for p in positions):
                continue

            if spread < self.config.min_spread_8h:
                continue

            # Fee calculation
            fee_long = self._get_fee(min_ex)
            fee_short = self._get_fee(max_ex)
            notional = self._notional()
            total_fee = (fee_long + fee_short) * 2 * notional  # entry + exit
            total_fee += self.config.slippage_bps * 2 * notional

            # Entry
            pos = PaperPosition(
                id=next_id,
                symbol=symbol,
                long_exchange=min_ex,  # Long where rate is lowest
                short_exchange=max_ex,  # Short where rate is highest
                entry_spread_8h=spread,
                entry_time=ts,
                margin_per_leg=self.config.margin_per_leg,
                leverage=self.config.leverage,
                fee_cost=total_fee,
            )
            positions.append(pos)
            next_id += 1

        # Force close remaining
        for pos in positions:
            pnl = pos.funding_collected - pos.fee_cost
            trades.append({
                "open_time": pos.entry_time,
                "close_time": rows[-1]["timestamp"] if rows else "",
                "symbol": pos.symbol,
                "long_exchange": pos.long_exchange,
                "short_exchange": pos.short_exchange,
                "entry_spread": pos.entry_spread_8h,
                "exit_spread": 0,
                "funding_collected": pos.funding_collected,
                "fee_cost": pos.fee_cost,
                "pnl": pnl,
                "close_reason": "end_of_data",
            })

        self.closed_trades = trades
        self._save_to_db(trades)
        self._print_results(trades, days)

    def _save_to_db(self, trades: list[dict]):
        for t in trades:
            self.db.execute("""
                INSERT INTO paper_trades
                (open_time, close_time, symbol, long_exchange, short_exchange,
                 entry_spread_8h, exit_spread_8h, margin_per_leg, leverage,
                 est_funding_collected, est_fee_cost, est_pnl, status, close_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t["open_time"], t["close_time"], t["symbol"],
                t["long_exchange"], t["short_exchange"],
                t["entry_spread"], t["exit_spread"],
                self.config.margin_per_leg, self.config.leverage,
                t["funding_collected"], t["fee_cost"], t["pnl"],
                "closed", t["close_reason"],
            ))
        self.db.commit()

    def _print_results(self, trades: list[dict], days: int):
        print(f"\n{'='*60}")
        print(f"  펀딩 아비트라지 백테스트 ({days}일)")
        print(f"{'='*60}")

        if not trades:
            print("  거래 없음")
            return

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        total_funding = sum(t["funding_collected"] for t in trades)
        total_fees = sum(t["fee_cost"] for t in trades)

        print(f"\n  총 거래: {len(trades)}회")
        print(f"  승률: {len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.1f}%)")
        print(f"  총 수취 펀딩: ${total_funding:.2f}")
        print(f"  총 수수료: -${total_fees:.2f}")
        print(f"  순 PnL: ${total_pnl:+.2f}")
        if days > 0:
            print(f"  일 평균: ${total_pnl/days:+.2f}")
            capital = self.config.margin_per_leg * 2 * self.config.max_positions
            apr = (total_pnl / days * 365 / capital * 100) if capital else 0
            print(f"  연환산 APR: ~{apr:.0f}% (자본 ${capital:.0f} 기준)")

        # By pair
        pair_stats: dict[str, list] = {}
        for t in trades:
            key = f"{t['short_exchange']}↔{t['long_exchange']}"
            pair_stats.setdefault(key, []).append(t)

        print(f"\n  거래소 페어별:")
        for pair, ts in sorted(pair_stats.items(), key=lambda x: -sum(t["pnl"] for t in x[1])):
            pair_pnl = sum(t["pnl"] for t in ts)
            pair_wins = sum(1 for t in ts if t["pnl"] > 0)
            print(f"    {pair:30s} {len(ts)}회, ${pair_pnl:+.2f}, 승률 {pair_wins/len(ts)*100:.0f}%")

        # By close reason
        print(f"\n  청산 이유:")
        reason_stats: dict[str, list] = {}
        for t in trades:
            reason_stats.setdefault(t["close_reason"], []).append(t)
        for reason, ts in reason_stats.items():
            avg_pnl = sum(t["pnl"] for t in ts) / len(ts)
            print(f"    {reason:20s} {len(ts)}회, avg ${avg_pnl:+.2f}")

        # Hold time distribution
        print(f"\n  보유 시간:")
        for label, lo, hi in [("<1h", 0, 1), ("1-4h", 1, 4), ("4-12h", 4, 12), (">12h", 12, 999)]:
            bucket = []
            for t in trades:
                try:
                    dt = (datetime.fromisoformat(t["close_time"]) -
                          datetime.fromisoformat(t["open_time"])).total_seconds() / 3600
                    if lo <= dt < hi:
                        bucket.append(t)
                except Exception:
                    pass
            if bucket:
                avg = sum(t["pnl"] for t in bucket) / len(bucket)
                print(f"    {label:8s} {len(bucket)}회, avg ${avg:+.2f}")


# ── CLI ──

def main():
    import argparse
    import yaml

    parser = argparse.ArgumentParser(description="Funding Rate Simulator")
    parser.add_argument("--mode", choices=["backtest", "paper"], default="backtest")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--min-spread", type=float, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    arb_cfg = config.get("funding_arb", {})
    sim_config = SimConfig(
        min_spread_8h=args.min_spread or arb_cfg.get("min_spread_8h", 0.0005),
        close_spread_8h=arb_cfg.get("close_spread_8h", 0.0001),
        margin_per_leg=arb_cfg.get("margin_per_leg", 50.0),
        leverage=arb_cfg.get("leverage", 5),
        max_positions=arb_cfg.get("max_positions", 2),
        max_hold_hours=arb_cfg.get("max_hold_hours", 12.0),
        exchange_fees=arb_cfg.get("exchange_fees", {}),
    )

    db_path = config.get("funding_collector", {}).get("db_path", "funding_rates.db")
    sim = FundingSimulator(sim_config, db_path)

    if args.mode == "backtest":
        sim.backtest(args.days)
    elif args.mode == "paper":
        print("페이퍼 모드는 funding_collector와 함께 실시간으로 실행됩니다.")
        print("먼저 데이터를 충분히 수집한 후 backtest로 파라미터를 검증하세요.")


if __name__ == "__main__":
    main()
