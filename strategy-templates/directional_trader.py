"""
Directional Trader — 폴리마켓 시그널 미러링 퍼프 트레이더

폴리마켓 봇이 발행한 시그널(signal_bridge.json)을 읽어서
같은 방향으로 퍼프 DEX에 단방향 롱/숏 진입.

기존 PairTrader 대체:
- PairTrader: 2개 코인 동시 롱+숏 (방향 중립)
- DirectionalTrader: 1개 코인 단방향 (폴리마켓 시그널 팔로우)

핵심 차이:
- 진입: 폴리마켓 시그널 도착 시 (스캔 주기 10초)
- 청산: 시간 기반 (윈도우 만료) + 손절/트레일링 스탑
- 레버리지: 낮게 (3-5x, 페어트레이딩의 15x 대비)
- 포지션: 단일 코인, 단일 방향

래퍼 인터페이스 (기존 PairTrader와 동일):
- get_mark_price(symbol) → float
- create_order(symbol, side, amount, price=None, order_type='market')
- get_position(symbol) → dict {side, size, entry_price, ...}
- close_position(symbol, position)
- update_leverage(symbol, leverage, margin_mode)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DirectionalConfig:
    """방향성 트레이딩 설정"""
    # 시그널 브릿지
    bridge_path: str = ""                  # signal_bridge.json 경로
    scan_interval: int = 10                # 시그널 폴링 주기 (초)

    # 진입 필터
    min_prob: float = 0.65                 # 최소 블렌딩 확률
    min_edge: float = 0.05                 # 최소 엣지 (prob - market_price)
    min_confidence_tier: str = "medium"    # 최소 컨피던스 ("high" or "medium")
    allowed_assets: list = field(default_factory=lambda: ["BTC", "ETH"])
    min_minutes_to_expiry: float = 5.0     # 만기 최소 N분 남아야 진입

    # 포지션 관리
    leverage: int = 5                      # 레버리지 (페어트레이딩 15x → 5x)
    trading_margin: float = 100            # 1회 진입 마진 (USD)
    max_concurrent: int = 2                # 동시 최대 포지션 수

    # 손절/익절
    stop_loss_percent: float = 0.5         # 손절 (%) — 5x에서 0.5% = 마진 2.5% 손실
    take_profit_percent: float = 1.0       # 고정 익절 (%)
    time_exit_minutes: float = 16.0        # 시간 기반 청산 (윈도우+1분 버퍼)

    # 트레일링 스탑
    trailing_enabled: bool = True
    trailing_activation: float = 0.3       # 수익 0.3%에서 트레일링 활성
    trailing_callback: float = 0.15        # 고점에서 0.15% 되돌리면 청산

    # 지정가 주문 (기존 LimitOrderConfig 재활용)
    use_limit_order: bool = True


@dataclass
class ActivePosition:
    """현재 진입 중인 포지션"""
    asset: str
    direction: str           # "long" or "short"
    entry_price: float
    size: float
    margin: float
    entry_time: float        # unix timestamp
    signal_id: str           # 추적용
    window_expiry: float     # 이 시각 이후 시간 기반 청산
    peak_pnl: float = 0.0
    trailing_active: bool = False


class DirectionalTrader:
    """폴리마켓 시그널 미러링 방향성 트레이더"""

    def __init__(
        self,
        exchange_wrapper,
        config: DirectionalConfig,
        exchange_name: str = "",
    ):
        self.wrapper = exchange_wrapper
        self.config = config
        self.exchange_name = exchange_name
        self.running = False

        # 포지션 관리
        self.positions: dict[str, ActivePosition] = {}  # asset → position
        self._processed_signals: set[str] = set()       # 이미 처리한 signal_id
        self._processed_max = 500

        # 트레이드 로거 (대시보드 연동)
        self.trade_logger = None
        self._current_trade_id = None

        # 수수료 트래킹
        self._fee_saved_total = 0.0
        self._maker_fills = 0
        self._taker_fills = 0

        # 통계
        self._total_trades = 0
        self._wins = 0
        self._total_pnl = 0.0

        self.tag = exchange_name.upper()[:5].ljust(5)

        # 브릿지 경로 검증
        self._bridge_path = Path(config.bridge_path) if config.bridge_path else None
        if not self._bridge_path:
            logger.warning(f"  {self.tag} │ bridge_path 미설정 — 시그널 수신 불가")

        logger.info(
            f"  {self.tag} │ DirectionalTrader 초기화 │ "
            f"lev={config.leverage}x margin=${config.trading_margin} "
            f"SL={config.stop_loss_percent}% TP={config.take_profit_percent}%"
        )

    def set_logger(self, trade_logger):
        self.trade_logger = trade_logger

    def get_state(self) -> dict:
        """상태 직렬화 (state_manager 호환)"""
        if not self.positions:
            return {}
        return {
            "exchange_name": self.exchange_name,
            "mode": "directional",
            "positions": {
                asset: {
                    "asset": pos.asset,
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "size": pos.size,
                    "margin": pos.margin,
                    "entry_time": pos.entry_time,
                    "signal_id": pos.signal_id,
                    "window_expiry": pos.window_expiry,
                    "peak_pnl": pos.peak_pnl,
                    "trailing_active": pos.trailing_active,
                }
                for asset, pos in self.positions.items()
            },
            "stats": {
                "total_trades": self._total_trades,
                "wins": self._wins,
                "total_pnl": self._total_pnl,
            },
            "timestamp": datetime.now().isoformat(),
        }

    def restore_state(self, state: dict):
        """저장 상태에서 복원"""
        positions = state.get("positions", {})
        for asset, pos_data in positions.items():
            self.positions[asset] = ActivePosition(**pos_data)

        stats = state.get("stats", {})
        self._total_trades = stats.get("total_trades", 0)
        self._wins = stats.get("wins", 0)
        self._total_pnl = stats.get("total_pnl", 0.0)

        logger.info(
            f"  {self.tag} │ 상태 복원 │ {len(self.positions)}개 포지션, "
            f"누적 {self._total_trades}트레이드"
        )

    async def run(self, saved_state: dict = None):
        """메인 루프"""
        self.running = True
        logger.info(f"  {self.tag} │ Directional Trading 시작")

        # 상태 복원
        if saved_state and saved_state.get("mode") == "directional":
            # 실제 포지션 확인 후 복원
            for asset in list(saved_state.get("positions", {}).keys()):
                pos = await self.wrapper.get_position(asset)
                if pos and float(pos.get("size", 0)) != 0:
                    self.restore_state(saved_state)
                    break
            else:
                logger.info(f"  {self.tag} │ 저장 상태 있으나 포지션 없음 → 새로 시작")
        else:
            # 기존 포지션 정리
            await self._cleanup_positions()

        # 레버리지 설정
        for asset in self.config.allowed_assets:
            try:
                await self.wrapper.update_leverage(
                    asset, leverage=self.config.leverage
                )
            except Exception as e:
                logger.warning(f"  {self.tag} │ {asset} 레버리지 설정 실패: {e}")

        while self.running:
            try:
                await self._trade_cycle()
                await asyncio.sleep(self.config.scan_interval)
            except Exception as e:
                logger.error(f"  {self.tag} │ 에러: {e}", exc_info=True)
                await asyncio.sleep(self.config.scan_interval)

    def stop(self):
        self.running = False
        logger.info(f"  {self.tag} │ 트레이딩 중지")

    async def shutdown(self, close_positions: bool = True):
        self.running = False
        if close_positions:
            logger.info(f"  {self.tag} │ 종료 중... 포지션 정리")
            await self._cleanup_positions()
        else:
            logger.info(f"  {self.tag} │ 종료 중... 포지션 유지 (graceful)")

    # ──────────────────────────────────────────
    # 트레이드 사이클
    # ──────────────────────────────────────────

    async def _trade_cycle(self):
        """한 사이클: 기존 포지션 관리 + 새 시그널 확인"""

        # 1. 기존 포지션 관리 (손절/익절/시간초과)
        await self._manage_positions()

        # 2. 새 시그널 확인 → 진입
        if len(self.positions) < self.config.max_concurrent:
            signals = self._read_signals()
            for sig in signals:
                if len(self.positions) >= self.config.max_concurrent:
                    break
                if self._should_enter(sig):
                    await self._execute_entry(sig)

    async def _manage_positions(self):
        """열린 포지션 관리"""
        now = time.time()
        to_close = []

        for asset, pos in self.positions.items():
            try:
                price = await self.wrapper.get_mark_price(asset)
                if not price:
                    continue

                pnl_pct = self._calc_pnl_percent(pos, price)

                # 로깅
                pnl_sign = "+" if pnl_pct >= 0 else ""
                elapsed = (now - pos.entry_time) / 60
                remaining = max(0, (pos.window_expiry - now) / 60)
                logger.info(
                    f"  {self.tag} │ {asset} {pos.direction.upper()} │ "
                    f"PnL {pnl_sign}{pnl_pct:.3f}% │ "
                    f"{elapsed:.0f}m elapsed, {remaining:.0f}m left"
                )

                # 대시보드 스냅샷
                if self.trade_logger:
                    try:
                        self.trade_logger.log_snapshot(
                            exchange=self.exchange_name,
                            coin1_momentum=0,
                            coin2_momentum=0,
                            pnl_percent=pnl_pct,
                            direction=pos.direction,
                            entry_count=1,
                            price_coin1=price,
                            price_coin2=0,
                        )
                    except Exception:
                        pass

                # 손절
                if pnl_pct <= -self.config.stop_loss_percent:
                    logger.info(f"  {self.tag} │ ✗ {asset} 손절 │ PnL {pnl_pct:.3f}%")
                    to_close.append((asset, "stop_loss", pnl_pct))
                    continue

                # 시간 기반 청산
                if now >= pos.window_expiry:
                    logger.info(
                        f"  {self.tag} │ ⏰ {asset} 시간 청산 │ PnL {pnl_sign}{pnl_pct:.3f}%"
                    )
                    to_close.append((asset, "time_exit", pnl_pct))
                    continue

                # 고정 익절 (트레일링 비활성 시)
                if not pos.trailing_active and pnl_pct >= self.config.take_profit_percent:
                    logger.info(f"  {self.tag} │ ★ {asset} 익절 │ PnL {pnl_sign}{pnl_pct:.3f}%")
                    to_close.append((asset, "take_profit", pnl_pct))
                    continue

                # 트레일링 스탑
                if self.config.trailing_enabled:
                    if pnl_pct > pos.peak_pnl:
                        pos.peak_pnl = pnl_pct

                    if not pos.trailing_active and pnl_pct >= self.config.trailing_activation:
                        pos.trailing_active = True
                        logger.info(
                            f"  {self.tag} │ {asset} 트레일링 활성 │ "
                            f"PnL {pnl_sign}{pnl_pct:.3f}%"
                        )

                    if pos.trailing_active:
                        drawdown = pos.peak_pnl - pnl_pct
                        if drawdown >= self.config.trailing_callback:
                            logger.info(
                                f"  {self.tag} │ ★ {asset} 트레일링 청산 │ "
                                f"PnL {pnl_sign}{pnl_pct:.3f}% "
                                f"(고점={pos.peak_pnl:.3f}% 되돌림={drawdown:.3f}%)"
                            )
                            to_close.append((asset, "trailing_stop", pnl_pct))
                            continue

            except Exception as e:
                logger.error(f"  {self.tag} │ {asset} 관리 에러: {e}")

        # 청산 실행
        for asset, reason, pnl_pct in to_close:
            await self._execute_close(asset, reason, pnl_pct)

    def _read_signals(self) -> list[dict]:
        """브릿지 파일에서 시그널 읽기"""
        if not self._bridge_path or not self._bridge_path.exists():
            return []

        try:
            data = json.loads(self._bridge_path.read_text(encoding="utf-8"))
            signals = data.get("active_signals", [])

            # 유효한 시그널만 필터
            now = time.time()
            valid = []
            for sig in signals:
                age = now - sig.get("timestamp", 0)
                window = sig.get("window_duration", 15) * 60
                if age < window and not sig.get("consumed", False):
                    valid.append(sig)

            return valid
        except Exception as e:
            logger.debug(f"  {self.tag} │ 브릿지 읽기 실패: {e}")
            return []

    def _should_enter(self, signal: dict) -> bool:
        """진입 조건 검증"""
        sig_id = signal.get("signal_id", "")

        # 이미 처리한 시그널
        if sig_id in self._processed_signals:
            return False

        asset = signal.get("asset", "")

        # 이미 해당 asset 포지션 있음
        if asset in self.positions:
            return False

        # 허용 asset
        if asset not in self.config.allowed_assets:
            return False

        # 최소 확률
        prob = signal.get("blended_prob", 0)
        if prob < self.config.min_prob:
            return False

        # 최소 엣지
        edge = signal.get("edge", 0)
        if edge < self.config.min_edge:
            return False

        # 컨피던스 티어
        tier = signal.get("confidence_tier", "low")
        tier_order = {"high": 3, "medium": 2, "low": 1}
        min_tier = tier_order.get(self.config.min_confidence_tier, 2)
        if tier_order.get(tier, 0) < min_tier:
            return False

        # 만기까지 최소 시간
        minutes_left = signal.get("minutes_to_expiry", 0)
        if minutes_left < self.config.min_minutes_to_expiry:
            return False

        return True

    async def _execute_entry(self, signal: dict):
        """시그널 기반 진입"""
        asset = signal["asset"]
        direction = signal["direction"]  # "long" or "short"
        sig_id = signal.get("signal_id", "unknown")

        try:
            price = await self.wrapper.get_mark_price(asset)
            if not price:
                logger.warning(f"  {self.tag} │ {asset} 가격 조회 실패")
                return

            # 마진 → 사이즈 계산
            margin = self.config.trading_margin
            size = (margin * self.config.leverage) / price

            # 주문 실행
            side = "buy" if direction == "long" else "sell"
            logger.info(
                f"  {self.tag} │ ▶ {asset} {direction.upper()} 진입 │ "
                f"price={price:.2f} size={size:.6f} margin=${margin:.0f} "
                f"lev={self.config.leverage}x │ sig={sig_id}"
            )

            order = await self.wrapper.create_order(
                symbol=asset,
                side=side,
                amount=size,
                order_type="market",
            )

            # 윈도우 만료 시각 계산
            window_duration = signal.get("window_duration", 15)
            signal_time = signal.get("timestamp", time.time())
            window_expiry = signal_time + (window_duration * 60) + 60  # +1분 버퍼

            # 포지션 등록
            self.positions[asset] = ActivePosition(
                asset=asset,
                direction=direction,
                entry_price=price,
                size=size,
                margin=margin,
                entry_time=time.time(),
                signal_id=sig_id,
                window_expiry=window_expiry,
            )

            # 시그널 consumed 마킹
            self._mark_signal_consumed(sig_id)
            self._processed_signals.add(sig_id)
            if len(self._processed_signals) > self._processed_max:
                # 오래된 것 정리
                self._processed_signals = set(list(self._processed_signals)[-250:])

            # 트레이드 로깅
            if self.trade_logger:
                try:
                    self._current_trade_id = self.trade_logger.log_entry(
                        exchange=self.exchange_name,
                        direction=direction,
                        coin1=asset,
                        coin2="",
                        entry_price_coin1=price,
                        entry_price_coin2=0,
                        margin=margin,
                        leverage=self.config.leverage,
                    )
                except Exception:
                    pass

            logger.info(f"  {self.tag} │ ✓ {asset} {direction.upper()} 진입 완료")

        except Exception as e:
            logger.error(f"  {self.tag} │ {asset} 진입 실패: {e}", exc_info=True)

    async def _execute_close(self, asset: str, reason: str, pnl_pct: float):
        """포지션 청산"""
        pos = self.positions.get(asset)
        if not pos:
            return

        try:
            current_pos = await self.wrapper.get_position(asset)
            if current_pos and float(current_pos.get("size", 0)) != 0:
                await self.wrapper.close_position(asset, current_pos)

            # 통계 업데이트
            self._total_trades += 1
            self._total_pnl += pnl_pct
            if pnl_pct > 0:
                self._wins += 1

            win_rate = (self._wins / self._total_trades * 100) if self._total_trades > 0 else 0

            logger.info(
                f"  {self.tag} │ ✓ {asset} 청산 │ reason={reason} "
                f"PnL={pnl_pct:+.3f}% │ "
                f"누적 {self._total_trades}T WR={win_rate:.0f}% "
                f"cumPnL={self._total_pnl:+.3f}%"
            )

            # 트레이드 로깅
            if self.trade_logger and self._current_trade_id:
                try:
                    price = await self.wrapper.get_mark_price(asset)
                    self.trade_logger.log_close(
                        trade_id=self._current_trade_id,
                        close_price_coin1=price or 0,
                        close_price_coin2=0,
                        pnl_percent=pnl_pct,
                        close_reason=reason,
                    )
                except Exception:
                    pass

            del self.positions[asset]

        except Exception as e:
            logger.error(f"  {self.tag} │ {asset} 청산 실패: {e}", exc_info=True)

    async def _cleanup_positions(self):
        """시작 시 기존 포지션 정리"""
        for asset in self.config.allowed_assets:
            try:
                pos = await self.wrapper.get_position(asset)
                if pos and float(pos.get("size", 0)) != 0:
                    await self.wrapper.close_position(asset, pos)
                    logger.info(f"  {self.tag} │ {asset} 기존 포지션 청산")
            except Exception:
                pass
        self.positions.clear()

    def _mark_signal_consumed(self, signal_id: str):
        """브릿지 파일에서 시그널 consumed 마킹"""
        if not self._bridge_path or not self._bridge_path.exists():
            return
        try:
            data = json.loads(self._bridge_path.read_text(encoding="utf-8"))
            for sig in data.get("active_signals", []):
                if sig.get("signal_id") == signal_id:
                    sig["consumed"] = True
                    sig["consumed_at"] = time.time()
                    sig["consumed_by"] = self.exchange_name
            data["last_updated"] = time.time()
            tmp = self._bridge_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self._bridge_path)
        except Exception:
            pass

    @staticmethod
    def _calc_pnl_percent(pos: ActivePosition, current_price: float) -> float:
        """포지션 PnL 계산 (%)"""
        if pos.entry_price == 0:
            return 0.0
        if pos.direction == "long":
            return (current_price - pos.entry_price) / pos.entry_price * 100
        else:
            return (pos.entry_price - current_price) / pos.entry_price * 100

    def get_dashboard_data(self) -> dict:
        """대시보드용 데이터"""
        win_rate = (self._wins / self._total_trades * 100) if self._total_trades > 0 else 0
        return {
            "mode": "directional",
            "exchange": self.exchange_name,
            "positions": {
                asset: {
                    "asset": pos.asset,
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "entry_time": pos.entry_time,
                    "signal_id": pos.signal_id,
                    "peak_pnl": pos.peak_pnl,
                    "trailing_active": pos.trailing_active,
                }
                for asset, pos in self.positions.items()
            },
            "stats": {
                "total_trades": self._total_trades,
                "wins": self._wins,
                "win_rate": round(win_rate, 1),
                "total_pnl": round(self._total_pnl, 3),
            },
            "config": {
                "leverage": self.config.leverage,
                "margin": self.config.trading_margin,
                "stop_loss": self.config.stop_loss_percent,
                "take_profit": self.config.take_profit_percent,
                "min_prob": self.config.min_prob,
            },
        }
