"""
Pair Trading Backtester
히스토리컬 캔들 데이터로 페어 트레이딩 전략을 시뮬레이션

사용법:
    python -m strategies.backtester --config config.yaml
    python -m strategies.backtester --config config.yaml --days 7
    python -m strategies.backtester --config config.yaml --optimize
    python -m strategies.backtester --config config.yaml --chart

pair_trader.py 의 실제 로직을 그대로 재현:
- 모멘텀 기반 방향 결정
- DCA 진입 (entry_trigger_percent 마다)
- 익절 (close_trigger_percent)
- 손절 (stop_loss_percent)
- 수수료 반영
"""

import asyncio
import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import yaml

from .momentum import calculate_momentum_score


# ──────────────────────────────────────────
# 설정
# ──────────────────────────────────────────

@dataclass
class BacktestConfig:
    coin1: str = "ETH"
    coin2: str = "SOL"
    leverage: int = 15
    trading_limit_count: int = 20
    trading_margin: float = 1000.0
    entry_trigger_percent: float = 0.5
    close_trigger_percent: float = 3.0
    stop_loss_percent: float = 5.0
    momentum_option: bool = True
    min_momentum_diff: float = 4.0
    chart_time: int = 1          # 캔들 타임프레임 (분)
    min_candles: int = 200       # 모멘텀 계산 최소 캔들
    scan_interval: int = 1       # 몇 캔들마다 체크 (1=매 캔들)
    initial_equity: float = 20000.0
    maker_fee_rate: float = 0.0001   # 0.01%
    taker_fee_rate: float = 0.00035  # 0.035%
    use_maker: bool = True           # Maker 주문 가정


def load_config_from_yaml(path: str) -> BacktestConfig:
    """config.yaml에서 백테스트 설정 로드"""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    strat = raw.get("strategy", {})
    cfg = BacktestConfig(
        coin1=strat.get("coin1", "ETH"),
        coin2=strat.get("coin2", "SOL"),
        leverage=strat.get("leverage", 15),
        trading_limit_count=strat.get("trading_limit_count", 20),
        trading_margin=strat.get("trading_margin", 1000.0),
        entry_trigger_percent=strat.get("entry_trigger_percent", 0.5),
        close_trigger_percent=strat.get("close_trigger_percent", 3.0),
        stop_loss_percent=strat.get("stop_loss_percent", 5.0),
        momentum_option=strat.get("momentum_option", True),
        min_momentum_diff=strat.get("min_momentum_diff", 4.0),
        chart_time=strat.get("chart_time", 1),
        min_candles=strat.get("min_candles", 200),
        scan_interval=strat.get("scan_interval", 1),
    )

    # 스케일링에서 reference_equity 가져오기
    scaling = raw.get("scaling", {})
    if scaling.get("enabled"):
        cfg.initial_equity = scaling.get("reference_equity", 20000.0)

    return cfg


# ──────────────────────────────────────────
# 캔들 데이터 다운로드
# ──────────────────────────────────────────

async def fetch_candles_bulk(symbol: str, interval: int, days: int, proxy: str = None) -> list[dict]:
    """Hyperliquid에서 대량 캔들 데이터 다운로드"""
    import aiohttp

    url = "https://api.hyperliquid.xyz/info"
    interval_map = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1d"}
    tf = interval_map.get(interval, "1m")

    now_ms = int(time.time() * 1000)
    interval_ms = interval * 60 * 1000
    total_candles = (days * 24 * 60) // interval

    all_candles = []
    # API 한 번에 최대 5000개 → 분할 요청
    chunk_size = 5000
    end_ms = now_ms
    target_start_ms = now_ms - (total_candles * interval_ms)

    print(f"  {symbol} 캔들 다운로드 중... ({days}일, {interval}분봉, ~{total_candles}개)")

    async with aiohttp.ClientSession() as session:
        while end_ms > target_start_ms:
            start_ms = max(target_start_ms, end_ms - (chunk_size * interval_ms))

            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol.upper(),
                    "interval": tf,
                    "startTime": start_ms,
                    "endTime": end_ms,
                }
            }

            try:
                async with session.post(url, json=payload, proxy=proxy) as resp:
                    data = await resp.json()
            except Exception as e:
                print(f"  {symbol} 다운로드 실패: {e}")
                break

            if not data:
                break

            chunk = []
            for c in data:
                chunk.append({
                    "timestamp": c.get("t", 0),
                    "open": float(c.get("o", 0)),
                    "high": float(c.get("h", 0)),
                    "low": float(c.get("l", 0)),
                    "close": float(c.get("c", 0)),
                    "volume": float(c.get("v", 0)),
                })

            all_candles = chunk + all_candles  # 오래된 순서로 정렬

            # 다음 청크: 받은 데이터의 가장 오래된 타임스탬프 기준
            oldest_ts = min(c.get("t", 0) for c in data)
            end_ms = oldest_ts
            print(f"  {symbol} ... {len(all_candles)}개 수집")
            await asyncio.sleep(0.3)  # rate limit

    # 중복 제거 (타임스탬프 기준)
    seen = set()
    unique = []
    for c in all_candles:
        ts = c["timestamp"]
        if ts not in seen:
            seen.add(ts)
            unique.append(c)
    all_candles = sorted(unique, key=lambda x: x["timestamp"])

    print(f"  {symbol} 다운로드 완료: {len(all_candles)}개")
    return all_candles


# ──────────────────────────────────────────
# 백테스트 엔진
# ──────────────────────────────────────────

@dataclass
class TradeRecord:
    """개별 트레이드 기록"""
    open_idx: int
    close_idx: int = 0
    direction: str = ""
    entries: list = field(default_factory=list)
    pnl_percent: float = 0.0
    pnl_usd: float = 0.0
    reason: str = ""
    duration_candles: int = 0
    max_drawdown: float = 0.0
    max_profit: float = 0.0


class BacktestEngine:
    """페어 트레이딩 백테스트 시뮬레이터"""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.equity = config.initial_equity
        self.trades: list[TradeRecord] = []
        self.equity_curve: list[float] = []

        # 현재 포지션 상태
        self.direction: Optional[str] = None
        self.entry_count: int = 0
        self.entries: list[dict] = []
        self._current_trade: Optional[TradeRecord] = None
        self._trade_max_dd: float = 0.0
        self._trade_max_profit: float = 0.0

        # 통계
        self.total_fees: float = 0.0
        self.total_trades: int = 0

    def run(self, candles1: list[dict], candles2: list[dict]) -> dict:
        """
        백테스트 실행

        Parameters:
            candles1: coin1 캔들 데이터 (시간순)
            candles2: coin2 캔들 데이터 (시간순)

        Returns:
            dict: 백테스트 결과 요약
        """
        min_len = min(len(candles1), len(candles2))
        if min_len < self.config.min_candles + 10:
            return {"error": f"캔들 부족: {min_len}개 (최소 {self.config.min_candles + 10}개 필요)"}

        self.equity = self.config.initial_equity
        self.trades = []
        self.equity_curve = []
        self.direction = None
        self.entry_count = 0
        self.entries = []
        self.total_fees = 0.0
        self.total_trades = 0

        start_idx = self.config.min_candles  # 모멘텀 계산에 필요한 워밍업

        for i in range(start_idx, min_len, self.config.scan_interval):
            price1 = candles1[i]["close"]
            price2 = candles2[i]["close"]

            # 모멘텀 계산 (최근 min_candles 캔들 사용)
            window1 = candles1[max(0, i - self.config.min_candles - 50):i + 1]
            window2 = candles2[max(0, i - self.config.min_candles - 50):i + 1]

            mom1 = calculate_momentum_score(window1) if self.config.momentum_option else 50.0
            mom2 = calculate_momentum_score(window2) if self.config.momentum_option else 50.0

            self._tick(i, price1, price2, mom1, mom2)
            self.equity_curve.append(self.equity)

        # 마지막에 남은 포지션 강제 청산
        if self.entry_count > 0:
            price1 = candles1[min_len - 1]["close"]
            price2 = candles2[min_len - 1]["close"]
            pnl_pct = self._calc_pnl(price1, price2)
            self._close_position(min_len - 1, price1, price2, pnl_pct, "end_of_data")

        return self._summary()

    def _tick(self, idx: int, price1: float, price2: float, mom1: float, mom2: float):
        """캔들 하나 처리"""
        if self.entry_count == 0:
            # 포지션 없음 → 진입 판단
            direction = self._analyze_direction(mom1, mom2)
            if direction:
                self.direction = direction
                self._open_entry(idx, price1, price2)
        else:
            # 포지션 있음 → 청산/DCA 판단
            pnl_pct = self._calc_pnl(price1, price2)

            # 트레이드 내 최대 손실/수익 추적
            if pnl_pct < self._trade_max_dd:
                self._trade_max_dd = pnl_pct
            if pnl_pct > self._trade_max_profit:
                self._trade_max_profit = pnl_pct

            # 익절
            if pnl_pct >= self.config.close_trigger_percent:
                self._close_position(idx, price1, price2, pnl_pct, "profit_target")
                return

            # 손절
            if pnl_pct <= -self.config.stop_loss_percent:
                self._close_position(idx, price1, price2, pnl_pct, "stop_loss")
                return

            # DCA
            if (pnl_pct <= -self.config.entry_trigger_percent and
                    self.entry_count < self.config.trading_limit_count):
                # 마진 여유 체크
                next_margin = self.config.trading_margin
                if self.equity > next_margin:
                    self._open_entry(idx, price1, price2)

    def _analyze_direction(self, mom1: float, mom2: float) -> Optional[str]:
        """모멘텀 기반 방향 결정"""
        if not self.config.momentum_option:
            return "coin1_long"

        diff = mom1 - mom2
        if abs(diff) < self.config.min_momentum_diff:
            return None

        return "coin1_long" if diff > 0 else "coin2_long"

    def _open_entry(self, idx: int, price1: float, price2: float):
        """진입/DCA 추가"""
        margin = self.config.trading_margin
        leverage = self.config.leverage

        # 수수료 계산
        notional = margin * leverage * 2  # 양쪽
        fee_rate = self.config.maker_fee_rate if self.config.use_maker else self.config.taker_fee_rate
        fee = notional * fee_rate
        self.total_fees += fee
        self.equity -= fee

        self.entry_count += 1
        entry = {
            "idx": idx,
            "price1": price1,
            "price2": price2,
            "margin": margin,
        }
        self.entries.append(entry)

        if self.entry_count == 1:
            self._current_trade = TradeRecord(
                open_idx=idx,
                direction=self.direction,
            )
            self._trade_max_dd = 0.0
            self._trade_max_profit = 0.0

        if self._current_trade:
            self._current_trade.entries.append(entry)

    def _close_position(self, idx: int, price1: float, price2: float, pnl_pct: float, reason: str):
        """포지션 청산"""
        total_margin = self.config.trading_margin * self.entry_count
        pnl_usd = pnl_pct * total_margin / 100.0

        # 청산 수수료
        notional = total_margin * self.config.leverage * 2
        fee_rate = self.config.maker_fee_rate if self.config.use_maker else self.config.taker_fee_rate
        fee = notional * fee_rate
        self.total_fees += fee
        pnl_usd -= fee  # 청산 수수료 차감

        self.equity += pnl_usd

        if self._current_trade:
            self._current_trade.close_idx = idx
            self._current_trade.pnl_percent = pnl_pct
            self._current_trade.pnl_usd = pnl_usd
            self._current_trade.reason = reason
            self._current_trade.duration_candles = idx - self._current_trade.open_idx
            self._current_trade.max_drawdown = self._trade_max_dd
            self._current_trade.max_profit = self._trade_max_profit
            self.trades.append(self._current_trade)

        self.total_trades += 1
        self.direction = None
        self.entry_count = 0
        self.entries = []
        self._current_trade = None

    def _calc_pnl(self, current_price1: float, current_price2: float) -> float:
        """PnL % 계산 (pair_trader.py와 동일 로직)"""
        if not self.entries:
            return 0.0

        total_margin = self.config.trading_margin * self.entry_count
        if total_margin == 0:
            return 0.0

        total_pnl = 0.0
        leverage = self.config.leverage

        for entry in self.entries:
            ep1 = entry["price1"]
            ep2 = entry["price2"]
            margin = entry["margin"]

            if self.direction == "coin1_long":
                pnl1 = ((current_price1 - ep1) / ep1) * margin * leverage
                pnl2 = ((ep2 - current_price2) / ep2) * margin * leverage
            else:
                pnl1 = ((ep1 - current_price1) / ep1) * margin * leverage
                pnl2 = ((current_price2 - ep2) / ep2) * margin * leverage

            total_pnl += pnl1 + pnl2

        return (total_pnl / total_margin) * 100

    def _summary(self) -> dict:
        """결과 요약"""
        if not self.trades:
            return {
                "total_trades": 0,
                "message": "트레이드 없음",
                "final_equity": self.equity,
            }

        wins = [t for t in self.trades if t.pnl_usd > 0]
        losses = [t for t in self.trades if t.pnl_usd <= 0]
        profit_trades = [t for t in self.trades if t.reason == "profit_target"]
        stop_trades = [t for t in self.trades if t.reason == "stop_loss"]

        total_pnl = sum(t.pnl_usd for t in self.trades)
        win_rate = len(wins) / len(self.trades) * 100 if self.trades else 0

        avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0

        # 최대 낙폭 (equity curve 기반)
        max_dd = 0.0
        peak = self.config.initial_equity
        for eq in self.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 평균 트레이드 기간 (캔들 수)
        avg_duration = sum(t.duration_candles for t in self.trades) / len(self.trades)

        # 평균 DCA 횟수
        avg_entries = sum(len(t.entries) for t in self.trades) / len(self.trades)

        # Profit Factor
        gross_profit = sum(t.pnl_usd for t in wins)
        gross_loss = abs(sum(t.pnl_usd for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Sharpe Ratio (트레이드 기준)
        if len(self.trades) > 1:
            returns = [t.pnl_usd for t in self.trades]
            avg_ret = sum(returns) / len(returns)
            std_ret = math.sqrt(sum((r - avg_ret) ** 2 for r in returns) / (len(returns) - 1))
            sharpe = (avg_ret / std_ret) * math.sqrt(len(self.trades)) if std_ret > 0 else 0
        else:
            sharpe = 0

        return {
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl_usd": round(total_pnl, 2),
            "total_pnl_percent": round((self.equity - self.config.initial_equity) / self.config.initial_equity * 100, 2),
            "final_equity": round(self.equity, 2),
            "initial_equity": self.config.initial_equity,
            "avg_win_usd": round(avg_win, 2),
            "avg_loss_usd": round(avg_loss, 2),
            "max_drawdown_percent": round(max_dd, 2),
            "profit_factor": round(profit_factor, 3),
            "sharpe_ratio": round(sharpe, 3),
            "total_fees": round(self.total_fees, 2),
            "avg_duration_candles": round(avg_duration, 1),
            "avg_dca_entries": round(avg_entries, 1),
            "profit_target_closes": len(profit_trades),
            "stop_loss_closes": len(stop_trades),
            "end_of_data_closes": len([t for t in self.trades if t.reason == "end_of_data"]),
        }


# ──────────────────────────────────────────
# 파라미터 최적화
# ──────────────────────────────────────────

def run_optimization(candles1: list, candles2: list, base_config: BacktestConfig) -> list[dict]:
    """파라미터 그리드 서치"""
    results = []

    param_grid = {
        "close_trigger_percent": [2.0, 3.0, 5.0, 7.0],
        "stop_loss_percent": [3.0, 5.0, 7.0, 10.0],
        "entry_trigger_percent": [0.3, 0.5, 1.0, 1.5],
        "trading_limit_count": [5, 10, 15, 20],
        "min_momentum_diff": [3.0, 4.0, 6.0, 8.0],
    }

    # 전체 조합 수
    total = 1
    for v in param_grid.values():
        total *= len(v)

    print(f"\n  파라미터 최적화 시작 ({total}개 조합)")
    print(f"  {'─' * 60}")

    count = 0
    for ct in param_grid["close_trigger_percent"]:
        for sl in param_grid["stop_loss_percent"]:
            if sl <= ct:
                continue  # 손절이 익절보다 작으면 스킵 (R:R 너무 불리)
            for et in param_grid["entry_trigger_percent"]:
                for tlc in param_grid["trading_limit_count"]:
                    for mmd in param_grid["min_momentum_diff"]:
                        cfg = BacktestConfig(
                            coin1=base_config.coin1,
                            coin2=base_config.coin2,
                            leverage=base_config.leverage,
                            trading_margin=base_config.trading_margin,
                            chart_time=base_config.chart_time,
                            min_candles=base_config.min_candles,
                            scan_interval=base_config.scan_interval,
                            initial_equity=base_config.initial_equity,
                            maker_fee_rate=base_config.maker_fee_rate,
                            taker_fee_rate=base_config.taker_fee_rate,
                            use_maker=base_config.use_maker,
                            momentum_option=base_config.momentum_option,
                            close_trigger_percent=ct,
                            stop_loss_percent=sl,
                            entry_trigger_percent=et,
                            trading_limit_count=tlc,
                            min_momentum_diff=mmd,
                        )

                        engine = BacktestEngine(cfg)
                        result = engine.run(candles1, candles2)

                        if result.get("total_trades", 0) > 0:
                            result["params"] = {
                                "close_trigger": ct,
                                "stop_loss": sl,
                                "entry_trigger": et,
                                "limit_count": tlc,
                                "momentum_diff": mmd,
                            }
                            results.append(result)

                        count += 1
                        if count % 50 == 0:
                            print(f"  진행: {count}/{total} ({count/total*100:.0f}%)")

    # PnL 기준 정렬
    results.sort(key=lambda x: x.get("total_pnl_usd", 0), reverse=True)

    print(f"\n  완료: {len(results)}개 유효 결과")
    return results


# ──────────────────────────────────────────
# 출력
# ──────────────────────────────────────────

def print_result(result: dict, config: BacktestConfig):
    """백테스트 결과 출력"""
    print(f"\n{'═' * 60}")
    print(f"  BACKTEST RESULT — {config.coin1}/{config.coin2}")
    print(f"{'═' * 60}")

    if result.get("error"):
        print(f"  ERROR: {result['error']}")
        return

    if result.get("total_trades", 0) == 0:
        print(f"  트레이드 없음 (모멘텀 차이가 불충분하거나 데이터 부족)")
        return

    # 수익 요약
    pnl = result["total_pnl_usd"]
    pnl_sign = "+" if pnl >= 0 else ""
    print(f"\n  {'─' * 40}")
    print(f"  수익 요약")
    print(f"  {'─' * 40}")
    print(f"  시작 자본:      ${result['initial_equity']:,.0f}")
    print(f"  최종 자본:      ${result['final_equity']:,.2f}")
    print(f"  총 수익:        {pnl_sign}${pnl:,.2f} ({pnl_sign}{result['total_pnl_percent']}%)")
    print(f"  총 수수료:      ${result['total_fees']:,.2f}")

    # 트레이드 통계
    print(f"\n  {'─' * 40}")
    print(f"  트레이드 통계")
    print(f"  {'─' * 40}")
    print(f"  총 트레이드:    {result['total_trades']}건")
    print(f"  승리:           {result['wins']}건")
    print(f"  패배:           {result['losses']}건")
    print(f"  승률:           {result['win_rate']}%")
    print(f"  익절 청산:      {result['profit_target_closes']}건")
    print(f"  손절 청산:      {result['stop_loss_closes']}건")

    # 리스크 지표
    print(f"\n  {'─' * 40}")
    print(f"  리스크 지표")
    print(f"  {'─' * 40}")
    print(f"  평균 수익:      ${result['avg_win_usd']:,.2f}")
    print(f"  평균 손실:      ${result['avg_loss_usd']:,.2f}")
    print(f"  Profit Factor:  {result['profit_factor']}")
    print(f"  Sharpe Ratio:   {result['sharpe_ratio']}")
    print(f"  최대 낙폭:      {result['max_drawdown_percent']}%")

    # 포지션 특성
    print(f"\n  {'─' * 40}")
    print(f"  포지션 특성")
    print(f"  {'─' * 40}")
    print(f"  평균 보유기간:  {result['avg_duration_candles']:.0f} 캔들 (~{result['avg_duration_candles'] * config.chart_time:.0f}분)")
    print(f"  평균 DCA 횟수:  {result['avg_dca_entries']:.1f}회")

    # 현재 파라미터
    print(f"\n  {'─' * 40}")
    print(f"  사용된 파라미터")
    print(f"  {'─' * 40}")
    print(f"  레버리지:       {config.leverage}x")
    print(f"  1회 마진:       ${config.trading_margin:,.0f}")
    print(f"  DCA 최대:       {config.trading_limit_count}회")
    print(f"  익절:           {config.close_trigger_percent}%")
    print(f"  손절:           {config.stop_loss_percent}%")
    print(f"  DCA 트리거:     {config.entry_trigger_percent}%")
    print(f"  모멘텀 차이:    {config.min_momentum_diff}")
    print(f"  타임프레임:     {config.chart_time}분봉")

    print(f"\n{'═' * 60}\n")


def print_optimization_results(results: list[dict], top_n: int = 10):
    """최적화 결과 출력"""
    print(f"\n{'═' * 80}")
    print(f"  TOP {min(top_n, len(results))} PARAMETER SETS (PnL 기준)")
    print(f"{'═' * 80}")

    print(f"\n  {'#':>3} {'PnL($)':>10} {'PnL(%)':>8} {'승률':>6} {'PF':>6} {'MDD':>6} "
          f"{'익절':>4} {'손절':>4} {'DCA트리거':>8} {'DCA맥스':>6} {'모멘텀':>6}")
    print(f"  {'─' * 78}")

    for i, r in enumerate(results[:top_n]):
        p = r["params"]
        pnl_sign = "+" if r["total_pnl_usd"] >= 0 else ""
        print(
            f"  {i+1:>3} "
            f"{pnl_sign}{r['total_pnl_usd']:>9,.0f} "
            f"{pnl_sign}{r['total_pnl_percent']:>7.1f}% "
            f"{r['win_rate']:>5.1f}% "
            f"{r['profit_factor']:>5.2f} "
            f"{r['max_drawdown_percent']:>5.1f}% "
            f"{p['close_trigger']:>4.1f} "
            f"{p['stop_loss']:>4.1f} "
            f"{p['entry_trigger']:>8.2f} "
            f"{p['limit_count']:>6} "
            f"{p['momentum_diff']:>6.1f}"
        )

    if len(results) > top_n:
        worst = results[-1]
        p = worst["params"]
        print(f"  {'─' * 78}")
        print(f"  최하위: PnL=${worst['total_pnl_usd']:+,.0f} 승률={worst['win_rate']:.1f}% "
              f"익절={p['close_trigger']} 손절={p['stop_loss']} DCA트리거={p['entry_trigger']}")

    print(f"\n  총 {len(results)}개 조합 테스트 완료")
    print(f"{'═' * 80}\n")


def print_trade_list(trades: list[TradeRecord], config: BacktestConfig, limit: int = 30):
    """개별 트레이드 목록 출력"""
    print(f"\n  최근 {min(limit, len(trades))}건 트레이드:")
    print(f"  {'#':>3} {'방향':>10} {'DCA':>3} {'PnL($)':>10} {'PnL(%)':>8} {'사유':>12} {'기간':>8} {'MDD':>7} {'MHP':>7}")
    print(f"  {'─' * 80}")

    for i, t in enumerate(trades[-limit:]):
        dir_str = f"{config.coin1}L" if t.direction == "coin1_long" else f"{config.coin2}L"
        pnl_sign = "+" if t.pnl_usd >= 0 else ""
        reason_map = {"profit_target": "익절", "stop_loss": "손절", "end_of_data": "종료"}
        reason = reason_map.get(t.reason, t.reason)
        duration = f"{t.duration_candles * config.chart_time}분"

        print(
            f"  {i+1:>3} "
            f"{dir_str:>10} "
            f"{len(t.entries):>3} "
            f"{pnl_sign}{t.pnl_usd:>9,.0f} "
            f"{pnl_sign}{t.pnl_percent:>7.2f}% "
            f"{reason:>12} "
            f"{duration:>8} "
            f"{t.max_drawdown:>6.2f}% "
            f"{t.max_profit:>6.2f}%"
        )


def save_equity_curve(equity_curve: list[float], path: str):
    """Equity curve를 CSV로 저장"""
    with open(path, "w") as f:
        f.write("index,equity\n")
        for i, eq in enumerate(equity_curve):
            f.write(f"{i},{eq:.2f}\n")
    print(f"  Equity curve 저장: {path}")


# ──────────────────────────────────────────
# 메인
# ──────────────────────────────────────────

async def async_main():
    parser = argparse.ArgumentParser(description="Pair Trading Backtester")
    parser.add_argument("--config", default="config.yaml", help="설정 파일 경로")
    parser.add_argument("--days", type=int, default=7, help="백테스트 기간 (일)")
    parser.add_argument("--optimize", action="store_true", help="파라미터 최적화 실행")
    parser.add_argument("--trades", action="store_true", help="개별 트레이드 목록 출력")
    parser.add_argument("--save-curve", type=str, default=None, help="Equity curve CSV 저장 경로")
    parser.add_argument("--chart-time", type=int, default=None, help="타임프레임 오버라이드 (분)")
    parser.add_argument("--coin1", type=str, default=None, help="Coin1 오버라이드")
    parser.add_argument("--coin2", type=str, default=None, help="Coin2 오버라이드")
    args = parser.parse_args()

    # 설정 로드
    config = load_config_from_yaml(args.config)

    # 오버라이드
    if args.chart_time:
        config.chart_time = args.chart_time
    if args.coin1:
        config.coin1 = args.coin1
    if args.coin2:
        config.coin2 = args.coin2

    print(f"\n{'═' * 60}")
    print(f"  Pair Trading Backtester")
    print(f"  {config.coin1}/{config.coin2} | {args.days}일 | {config.chart_time}분봉")
    print(f"{'═' * 60}")

    # 캔들 데이터 다운로드
    candles1, candles2 = await asyncio.gather(
        fetch_candles_bulk(config.coin1, config.chart_time, args.days),
        fetch_candles_bulk(config.coin2, config.chart_time, args.days),
    )

    if not candles1 or not candles2:
        print("  캔들 데이터 다운로드 실패")
        return

    if args.optimize:
        # 파라미터 최적화
        results = run_optimization(candles1, candles2, config)
        if results:
            print_optimization_results(results)

            # 최적 파라미터로 다시 실행해서 상세 결과 출력
            best = results[0]
            best_params = best["params"]
            config.close_trigger_percent = best_params["close_trigger"]
            config.stop_loss_percent = best_params["stop_loss"]
            config.entry_trigger_percent = best_params["entry_trigger"]
            config.trading_limit_count = best_params["limit_count"]
            config.min_momentum_diff = best_params["momentum_diff"]

            print(f"\n  최적 파라미터로 상세 결과:")
            engine = BacktestEngine(config)
            result = engine.run(candles1, candles2)
            print_result(result, config)

            if args.trades:
                print_trade_list(engine.trades, config)
    else:
        # 단일 백테스트
        engine = BacktestEngine(config)
        result = engine.run(candles1, candles2)
        print_result(result, config)

        if args.trades:
            print_trade_list(engine.trades, config)

        if args.save_curve:
            save_equity_curve(engine.equity_curve, args.save_curve)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
