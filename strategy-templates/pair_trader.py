"""
Pair Trader Engine
BTC/ETH 모멘텀 페어 트레이딩 + DCA

전략 로직:
1. BTC, ETH 모멘텀 점수 비교
2. 차이가 MIN_MOMENTUM_DIFF 이상이면 방향 결정
3. 모멘텀 강한 쪽 Long, 약한 쪽 Short
4. DCA로 분할 진입 (최대 TRADING_LIMIT_COUNT회)
5. 수익률 CLOSE_TRIGGER_PERCENT 이상이면 청산
6. 손실 STOP_LOSS_PERCENT 이상이면 손절

래퍼 인터페이스 (MultiPerpDex base.py 기준):
- get_mark_price(symbol) → float
- create_order(symbol, side, amount, price=None, order_type='market')
- get_position(symbol) → dict {side, size, entry_price, ...}
- close_position(symbol, position)
- update_leverage(symbol, leverage, margin_mode)
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .momentum import calculate_momentum_score
from .candle_fetcher import CandleFetcher
from .signals import SignalRegistry, DEFAULT_WEIGHTS

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────
# 수수료율 상수 (거래소별)
# ──────────────────────────────────────────
MAKER_FEE_RATES = {
    "hyperliquid": 0.0001,   # 0.01%
    "miracle": 0.0001,
    "nado": 0.0002,
    "hotstuff": 0.0002,
    "standx": 0.0002,
    "ethereal": 0.0002,
    "decibel": 0.0002,
    "treadfi.pacifica": 0.0003,
}
TAKER_FEE_RATES = {
    "hyperliquid": 0.00035,  # 0.035%
    "miracle": 0.00035,
    "nado": 0.0005,
    "hotstuff": 0.0005,
    "standx": 0.0005,
    "ethereal": 0.0005,
    "decibel": 0.0005,
    "treadfi.pacifica": 0.0006,
}


@dataclass
class LimitOrderConfig:
    """지정가 주문 설정"""
    enabled: bool = False
    bbo_offset_percent: float = 0.01    # BBO ±0.01%
    adjust_interval_ms: int = 100       # 100ms마다 조정
    max_retries: int = 5                # 5회 후 시장가
    pair_timeout_ms: int = 1000         # 쌍 체결 타임아웃
    post_only: bool = True              # Post-Only(ALO) 강제
    reprice_threshold: float = 0.02     # BBO 변동 임계값 (%) — 이 이상 벌어지면 리프라이싱
    order_ttl_ms: int = 5000            # 주문 TTL (ms) — 미체결 자동 취소
    use_ioc_fallback: bool = True       # 시장가 폴백 시 IOC 사용
    adaptive_offset: bool = True        # 스프레드 기반 동적 오프셋


@dataclass
class RiskConfig:
    """리스크 관리 설정"""
    max_position_usd: float = 0         # 최대 포지션 (USD, 0=무제한)
    inventory_tilt_enabled: bool = False # 인벤토리 틸트
    inventory_tilt_ratio: float = 1.5   # 틸트 비율
    observe_mode: bool = False          # Observe 모드 (신규 진입 중지)


@dataclass
class TrailingStopConfig:
    """트레일링 스탑 설정"""
    enabled: bool = True
    activation_percent: float = 3.0     # 수익 N% 이상에서 트레일링 활성화
    callback_percent: float = 1.5       # 고점에서 N% 되돌리면 청산
    tighten_above: float = 7.0          # 수익 N% 이상이면 callback 축소
    tighten_callback: float = 0.8       # 축소된 callback (%)


@dataclass
class CircuitBreakerConfig:
    """서킷브레이커 설정 — N연속 손실 시 일시정지"""
    consecutive_losses: int = 5         # N연속 손실 시 발동
    cooldown_seconds: int = 3600        # 쿨다운 (초)


@dataclass
class PairTraderConfig:
    coin1: str = "BTC"
    coin2: str = "ETH"
    leverage: int = 20
    trading_limit_count: int = 20
    trading_margin: float = 1000
    entry_trigger_percent: float = 0.2
    close_trigger_percent: float = 0.2
    stop_loss_percent: float = 10.0
    momentum_option: bool = True
    min_momentum_diff: float = 3.0
    chart_time: int = 1           # 분봉
    candle_limit: int = 1000
    min_candles: int = 200
    scan_interval: int = 60       # 초
    entry_delay: float = 0.0     # 진입 지연 (초) — 거래소 간 시차 분산
    no_entry_hours: list = field(default_factory=list)  # 진입 차단 시간 (UTC), e.g. [2,3,4]
    early_exit_grace_cycles: int = 3    # 진입 후 N사이클 동안 조기 탈출 무시
    early_exit_threshold: float = 15.0  # 시그널 반전 강도 임계값
    max_volatility_ratio: float = 0     # 0=비활성, 변동성 초과 시 진입 차단
    coin1_long_margin_ratio: float = 1.0  # coin1_long 진입 시 마진 배수 (0.5=절반, 1.0=동일)
    stop_loss_cooldown: int = 0           # 손절 후 쿨다운 (초). 0=비활성
    fee_aware_entry: bool = False         # 수수료 인식 진입 필터
    min_correlation: float = 0.7          # 롤링 상관계수 임계값 (0=비활성)
    coin2_long_entry_bonus: float = 0.15  # coin2_long 진입 용이성 보너스 (15% 쉬운 진입)
    dynamic_sizing: bool = False          # ATR 기반 동적 포지션 사이징 (비활성 기본)
    target_atr_pct: float = 1.5           # 목표 ATR % (동적 사이징)
    dynamic_stop_loss: bool = False       # ATR 기반 동적 손절 (비활성 기본)
    stop_atr_multiplier: float = 2.5      # 손절 ATR 배수
    min_stop_pct: float = 3.0             # 최소 손절 (%)
    max_stop_pct: float = 8.0             # 최대 손절 (%)
    limit_order: LimitOrderConfig = field(default_factory=LimitOrderConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    trailing_stop: TrailingStopConfig = field(default_factory=TrailingStopConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)


@dataclass
class Position:
    coin: str
    side: str           # "long" or "short"
    entry_price: float = 0.0
    size: float = 0.0
    margin: float = 0.0
    entry_count: int = 0
    entries: list = field(default_factory=list)


class PairTrader:
    """페어 트레이딩 엔진 - multi-perp-dex 래퍼와 연동"""

    def __init__(
        self,
        exchange_wrapper,
        candle_fetcher: CandleFetcher,
        config: PairTraderConfig,
        exchange_name: str = "",
        signal_weights: dict = None,
    ):
        """
        Parameters:
            exchange_wrapper: MultiPerpDex 래퍼 인스턴스
                메서드: get_mark_price(), create_order(), get_position(),
                        close_position(), update_leverage()
            candle_fetcher: CandleFetcher 인스턴스 (캔들 데이터 조회)
            config: PairTraderConfig
            exchange_name: 거래소 이름 (로깅용)
            signal_weights: 시그널 가중치 dict (None이면 기존 모멘텀만 사용)
        """
        self.wrapper = exchange_wrapper
        self.candle_fetcher = candle_fetcher
        self.config = config
        self.exchange_name = exchange_name
        self.running = False

        # 시그널 레지스트리 (evolver 연동)
        self.signal_registry: Optional[SignalRegistry] = None
        if signal_weights and any(v > 0 for k, v in signal_weights.items() if k != "momentum_diff"):
            # momentum_diff 외 활성 시그널이 있으면 레지스트리 사용
            self.signal_registry = SignalRegistry(signal_weights)
            self.signal_registry.register_active()
        self._last_composite_score = 0.0  # 대시보드 노출용
        self._candles1_cache = None  # 시그널 모듈용 캔들 캐시
        self._candles2_cache = None

        # 트레이드 로거 (대시보드 연동)
        self.trade_logger = None
        self._current_trade_id = None
        self._correlation_id: Optional[str] = None  # 트레이드 사이클 UUID
        self._last_pnl_percent = 0.0
        self._close_reason = None

        # 수수료 트래킹
        self._fee_saved_total = 0.0       # 누적 절감액 (USD)
        self._cumulative_fee_usd = 0.0    # 현재 트레이드 누적 수수료 (USD)
        self._maker_fills = 0             # Maker 체결 횟수
        self._taker_fills = 0             # Taker 체결 횟수 (폴백 포함)
        self._maker_fee_rate = MAKER_FEE_RATES.get(exchange_name, 0.0002)
        self._taker_fee_rate = TAKER_FEE_RATES.get(exchange_name, 0.0005)

        # [MM통합] Observe 모드 — 런타임 토글 가능
        self.observe_mode = config.risk.observe_mode

        # 포지션 상태
        self.coin1_position: Optional[Position] = None
        self.coin2_position: Optional[Position] = None

        # 모멘텀 점수
        self.coin1_momentum: float = 50.0
        self.coin2_momentum: float = 50.0

        # 현재 방향
        self.direction: Optional[str] = None  # "coin1_long" or "coin2_long"
        self.entry_count: int = 0

        # DCA 레이스컨디션 방지용 락
        self._entry_lock = asyncio.Lock()

        # 트레일링 스탑 상태
        self._peak_pnl: float = 0.0          # 진입 후 최고 PnL (%)
        self._trailing_active: bool = False   # 트레일링 활성 여부
        self._cycles_since_entry: int = 0    # 진입 후 사이클 수 (조기 탈출 grace)

        # 서킷브레이커 상태
        self._consecutive_losses: int = 0
        self._circuit_breaker_until: float = 0  # Unix timestamp
        self._stop_loss_cooldown_until: float = 0  # 손절 후 개별 쿨다운

        # 짧은 표시 이름
        self.tag = exchange_name.upper()[:5].ljust(5)

        sig_mode = "signal_modules" if self.signal_registry else "momentum_only"
        logger.info(f"  {self.tag} │ 초기화 │ {config.coin1}/{config.coin2} x{config.leverage} [{sig_mode}]")

    # ──────────────────────────────────────────
    # 공통 헬퍼
    # ──────────────────────────────────────────

    @staticmethod
    def _has_position(pos: dict) -> bool:
        """포지션 존재 여부 확인"""
        return bool(pos and float(pos.get("size", 0)) != 0)

    async def _close_coin_positions(self, coins: list) -> bool:
        """여러 코인 포지션 청산. 하나라도 청산했으면 True"""
        closed_any = False
        for coin in coins:
            try:
                pos = await self.wrapper.get_position(coin)
                if self._has_position(pos):
                    await self.wrapper.close_position(coin, pos)
                    logger.info(f"  {self.tag} │ {coin} 기존 포지션 청산")
                    closed_any = True
            except Exception as e:
                logger.debug(f"  {self.tag} │ {coin} 포지션 청산 스킵: {e}")
        return closed_any

    def _safe_log_trade(self, method: str, **kwargs):
        """trade_logger 호출 래퍼 — 실패해도 트레이딩에 영향 없음"""
        if not self.trade_logger:
            return None
        try:
            return getattr(self.trade_logger, method)(**kwargs)
        except Exception as e:
            logger.debug(f"  {self.tag} │ trade_logger.{method} 실패: {e}")
            return None

    async def _get_mark_price_safe(self, symbol: str, retries: int = 2) -> Optional[float]:
        """get_mark_price with retry — 1회 실패 시 재시도"""
        for attempt in range(retries):
            try:
                price = await asyncio.wait_for(
                    self.wrapper.get_mark_price(symbol), timeout=10.0
                )
                if price is not None and price > 0:
                    return price
            except asyncio.TimeoutError:
                logger.warning(f"  {self.tag} │ {symbol} 가격 조회 타임아웃 ({attempt+1}/{retries})")
            except Exception as e:
                logger.warning(f"  {self.tag} │ {symbol} 가격 조회 실패 ({attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)
        return None

    def set_logger(self, trade_logger):
        """트레이드 로거 주입 (대시보드용)"""
        self.trade_logger = trade_logger

    def get_state(self) -> dict:
        """트레이더 상태 직렬화 (state_manager용)"""
        if self.entry_count == 0 or not self.direction:
            return {}
        entries = []
        if self.coin1_position and self.coin1_position.entries:
            entries = list(self.coin1_position.entries)
        return {
            "exchange_name": self.exchange_name,
            "direction": self.direction,
            "entry_count": self.entry_count,
            "trailing_active": self._trailing_active,
            "peak_pnl": self._peak_pnl,
            "current_trade_id": self._current_trade_id,
            "observe_mode": self.observe_mode,
            "cycles_since_entry": self._cycles_since_entry,
            "entries": entries,
            "coin1": self.config.coin1,
            "coin2": self.config.coin2,
            "timestamp": datetime.now().isoformat(),
        }

    def restore_state(self, state: dict):
        """저장된 상태에서 복원"""
        from dataclasses import dataclass
        self.direction = state["direction"]
        self.entry_count = state["entry_count"]
        self._trailing_active = state.get("trailing_active", False)
        self._peak_pnl = state.get("peak_pnl", 0.0)
        self._current_trade_id = state.get("current_trade_id")
        self.observe_mode = state.get("observe_mode", False)
        self._cycles_since_entry = state.get("cycles_since_entry", 0)

        entries = state.get("entries", [])
        if entries:
            is_coin1_long = self.direction == "coin1_long"
            self.coin1_position = Position(
                coin=self.config.coin1,
                side="long" if is_coin1_long else "short",
                entry_count=self.entry_count,
                entries=list(entries),
            )
            self.coin2_position = Position(
                coin=self.config.coin2,
                side="short" if is_coin1_long else "long",
                entry_count=self.entry_count,
                entries=list(entries),
            )

        trail = "ON" if self._trailing_active else "OFF"
        logger.info(
            f"  {self.tag} │ 상태 복원 │ {self.direction} DCA={self.entry_count} "
            f"trailing={trail} peak={self._peak_pnl:.1f}%"
        )

    async def run(self, saved_state: dict = None):
        """메인 루프"""
        self.running = True
        logger.info(f"  {self.tag} │ 트레이딩 시작")

        # 저장된 상태가 있으면 포지션 확인 후 복원
        if saved_state:
            pos1 = await self.wrapper.get_position(self.config.coin1)
            pos2 = await self.wrapper.get_position(self.config.coin2)
            has_pos = self._has_position(pos1) or self._has_position(pos2)
            if has_pos:
                self.restore_state(saved_state)
            else:
                # 포지션 없으면 DB의 open trade를 close 처리
                orphan_id = saved_state.get("current_trade_id")
                if orphan_id:
                    logger.info(f"  {self.tag} │ 고아 trade #{orphan_id} DB close 처리")
                    self._safe_log_trade(
                        "close_trade",
                        trade_id=orphan_id,
                        pnl_percent=0.0,
                        pnl_usd=0.0,
                        reason="orphan_on_restart",
                    )
                logger.info(f"  {self.tag} │ 저장 상태 있으나 포지션 없음 → 새로 시작")
                await self._cleanup_existing_positions()
        else:
            # 시작 시 기존 포지션 확인 → 있으면 청산
            await self._cleanup_existing_positions()

        # 레버리지 초기 설정
        try:
            await self.wrapper.update_leverage(self.config.coin1, leverage=self.config.leverage)
            await self.wrapper.update_leverage(self.config.coin2, leverage=self.config.leverage)
            logger.info(f"  {self.tag} │ 레버리지 {self.config.leverage}x 설정")
        except Exception as e:
            logger.warning(f"  {self.tag} │ 레버리지 설정 실패: {e}")

        while self.running:
            try:
                await self._trade_cycle()
                await asyncio.sleep(self.config.scan_interval)
            except Exception as e:
                logger.error(f"  {self.tag} │ 에러: {e}")
                await asyncio.sleep(self.config.scan_interval)

    def stop(self):
        """트레이딩 중지"""
        self.running = False
        logger.info(f"  {self.tag} │ 트레이딩 중지")

    async def shutdown(self, close_positions: bool = True):
        """봇 종료. close_positions=False면 포지션 유지 (graceful restart)"""
        self.running = False
        if close_positions:
            logger.info(f"  {self.tag} │ 종료 중... 포지션 정리")
            await self._cleanup_existing_positions()
        else:
            logger.info(f"  {self.tag} │ 종료 중... 포지션 유지 (graceful)")
        logger.info(f"  {self.tag} │ 종료 완료")

    async def _cleanup_existing_positions(self):
        """기존 포지션 확인 후 청산 (이전 페어 잔여 포지션 포함)"""
        try:
            coins_to_check = list({self.config.coin1, self.config.coin2, "BTC", "ETH", "SOL"})
            closed_any = await self._close_coin_positions(coins_to_check)

            if closed_any:
                self.coin1_position = None
                self.coin2_position = None
                self.direction = None
                self.entry_count = 0
                logger.info(f"  {self.tag} │ 포지션 정리 완료")
            else:
                logger.info(f"  {self.tag} │ 포지션 없음 → 시작")
        except Exception as e:
            logger.error(f"  {self.tag} │ 포지션 확인 실패: {e}")

    async def _trade_cycle(self):
        """하나의 트레이딩 사이클"""
        # 1. 모멘텀 업데이트
        await self._update_momentum()

        # 2. 현재 가격 조회 (래퍼의 get_mark_price 사용)
        price1 = await self._get_mark_price_safe(self.config.coin1)
        price2 = await self._get_mark_price_safe(self.config.coin2)

        if not price1 or not price2 or price1 <= 0 or price2 <= 0:
            logger.warning(f"  {self.tag} │ 가격 이상: {self.config.coin1}={price1}, {self.config.coin2}={price2} │ 사이클 스킵")
            return

        # 3. 포지션 없으면 → 진입 분석
        if self.entry_count == 0:
            # 대기 상태 스냅샷
            self._safe_log_trade(
                "log_snapshot",
                exchange=self.exchange_name,
                coin1_momentum=self.coin1_momentum,
                coin2_momentum=self.coin2_momentum,
                pnl_percent=0.0,
                direction=None,
                entry_count=0,
                price_coin1=price1,
                price_coin2=price2,
            )

            # 서킷브레이커 체크 — 활성 시 신규 진입 차단
            if time.time() < self._circuit_breaker_until:
                remaining = int(self._circuit_breaker_until - time.time())
                logger.info(f"  {self.tag} │ 서킷브레이커 활성 │ {remaining}초 남음")
                return

            # [MM통합] Observe 모드 — 신규 진입 차단
            if self.observe_mode:
                logger.info(f"  {self.tag} │ OBSERVE │ 신규 진입 차단 중")
                return

            # 손절 후 개별 쿨다운 — 리벤지 트레이딩 방지
            if self.config.stop_loss_cooldown > 0 and time.time() < self._stop_loss_cooldown_until:
                remaining = int(self._stop_loss_cooldown_until - time.time())
                logger.info(f"  {self.tag} │ 손절 쿨다운 │ {remaining}초 남음")
                return

            # 시간대 필터 — 손실 집중 시간대 진입 차단
            if self.config.no_entry_hours:
                current_hour = datetime.utcnow().hour
                if current_hour in self.config.no_entry_hours:
                    logger.info(f"  {self.tag} │ SKIP │ no-entry hour ({current_hour:02d} UTC)")
                    return

            # Feature 1: 레짐 필터 (롤링 상관계수) — 상관계수 낮으면 진입 차단
            safe_regime, correlation = self._check_regime()
            if not safe_regime:
                logger.info(
                    f"  {self.tag} │ SKIP │ low correlation={correlation:.3f} < {self.config.min_correlation}"
                )
                return

            direction = self._analyze_direction()
            if direction:
                # 변동성 필터 — 스프레드 급변 시 진입 보류
                if (self.config.max_volatility_ratio > 0
                        and self._candles1_cache and self._candles2_cache):
                    from .signals import VolatilityRatioSignal
                    vol_result = VolatilityRatioSignal().evaluate(
                        self._candles1_cache, self._candles2_cache)
                    if abs(vol_result.score) > self.config.max_volatility_ratio:
                        logger.info(
                            f"  {self.tag} │ SKIP │ volatility too high "
                            f"(vol={vol_result.score:+.0f} > {self.config.max_volatility_ratio})")
                        return

                # 수수료 인식 진입 필터 — 예상 수수료보다 기대수익이 낮으면 스킵
                if self.config.fee_aware_entry:
                    round_trip_fee_pct = (self._maker_fee_rate + self._taker_fee_rate) * 2 * 100
                    min_profit = self.config.close_trigger_percent
                    if min_profit < round_trip_fee_pct * 1.5:
                        logger.info(
                            f"  {self.tag} │ SKIP │ fee-aware: 익절({min_profit:.2f}%) < "
                            f"수수료x1.5({round_trip_fee_pct * 1.5:.3f}%)")
                        return

                if self.config.entry_delay > 0:
                    logger.info(f"  {self.tag} │ 진입 지연 {self.config.entry_delay:.0f}초 대기")
                    await asyncio.sleep(self.config.entry_delay)
                    # 지연 후 가격 재조회
                    price1 = await self._get_mark_price_safe(self.config.coin1)
                    price2 = await self._get_mark_price_safe(self.config.coin2)
                    if not price1 or not price2:
                        return
                self.direction = direction
                await self._execute_entry(price1, price2)
            return

        # 4. 포지션 있으면 → 청산/추가진입 판단
        pnl_percent = self._calculate_total_pnl_percent(price1, price2)
        self._last_pnl_percent = pnl_percent

        pnl_sign = "+" if pnl_percent >= 0 else ""
        dir_arrow = f"{self.config.coin1} Short / {self.config.coin2} Long" if self.direction == "coin2_long" else f"{self.config.coin1} Long / {self.config.coin2} Short"
        logger.info(
            f"  {self.tag} │ PnL {pnl_sign}{pnl_percent:.2f}% │ "
            f"DCA {self.entry_count}/{self.config.trading_limit_count} │ "
            f"{dir_arrow}"
        )

        # 스냅샷 로깅 (대시보드용)
        self._safe_log_trade(
            "log_snapshot",
            exchange=self.exchange_name,
            coin1_momentum=self.coin1_momentum,
            coin2_momentum=self.coin2_momentum,
            pnl_percent=pnl_percent,
            direction=self.direction,
            entry_count=self.entry_count,
            price_coin1=price1,
            price_coin2=price2,
        )

        # 사이클 카운터 (조기 탈출 grace period용)
        self._cycles_since_entry += 1

        # 조기 탈출 — 시그널 반전 감지
        if (self.signal_registry and self._candles1_cache and self._candles2_cache
                and not self._trailing_active
                and self._cycles_since_entry > self.config.early_exit_grace_cycles):
            composite = self.signal_registry.evaluate(
                self._candles1_cache, self._candles2_cache,
                min_candles=self.config.min_candles,
                min_signal_strength=self.config.early_exit_threshold,
            )
            if composite.direction and composite.direction != self.direction:
                logger.info(
                    f"  {self.tag} │ ★ EARLY EXIT │ signal reversal "
                    f"score={composite.weighted_score:+.1f} "
                    f"PnL={pnl_percent:+.2f}%"
                )
                self._close_reason = "signal_reversal"
                await self._execute_close()

                # Early exit는 수익성 조건에 무관하게 리셋 (보수적)
                if pnl_percent > 0:
                    self._consecutive_losses = 0
                return

        # [MM통합] Observe 모드 — 익절만 지정가로 시도, DCA/손절 차단
        if self.observe_mode:
            if pnl_percent >= self.config.close_trigger_percent:
                logger.info(f"  {self.tag} │ OBSERVE 익절 │ PnL {pnl_sign}{pnl_percent:.2f}%")
                self._close_reason = "observe_profit"
                await self._execute_close_limit()
            else:
                logger.info(f"  {self.tag} │ OBSERVE │ PnL {pnl_sign}{pnl_percent:.2f}% │ 청산 대기")
            return

        # 트레일링 스탑 처리
        ts = self.config.trailing_stop
        if ts.enabled:
            # 고점 갱신
            if pnl_percent > self._peak_pnl:
                self._peak_pnl = pnl_percent

            # 활성화 조건: 수익이 activation_percent 이상 도달
            if not self._trailing_active and pnl_percent >= ts.activation_percent:
                self._trailing_active = True
                logger.info(f"  {self.tag} │ 트레일링 활성 │ PnL {pnl_sign}{pnl_percent:.2f}% (고점={self._peak_pnl:.2f}%)")

            # 트레일링 청산 판단
            if self._trailing_active:
                # 고수익 구간에서는 callback 축소
                cb = ts.tighten_callback if self._peak_pnl >= ts.tighten_above else ts.callback_percent

                # 레짐 필터: 트렌딩 레짐이면 callback 추가 축소 (빠른 탈출)
                if (self.signal_registry and self._candles1_cache and self._candles2_cache
                        and self.signal_registry.weights.get("hurst_regime", 0) > 0):
                    hurst_signal = self.signal_registry.signals.get("hurst_regime")
                    if hurst_signal:
                        try:
                            hr = hurst_signal.evaluate(self._candles1_cache, self._candles2_cache)
                            hurst_val = hr.metadata.get("hurst", 0.5)
                            if hurst_val > 0.6:
                                # 강한 트렌딩 → callback을 70%로 축소 (더 빠른 탈출)
                                cb = cb * 0.7
                        except Exception as e:
                            logger.debug(f"  {self.tag} │ hurst_regime 평가 실패: {e}")

                drawdown = self._peak_pnl - pnl_percent
                if drawdown >= cb:
                    logger.info(
                        f"  {self.tag} │ ★ 트레일링 익절 │ PnL {pnl_sign}{pnl_percent:.2f}% "
                        f"(고점={self._peak_pnl:.2f}% 되돌림={drawdown:.2f}% cb={cb:.1f}%)"
                    )
                    self._close_reason = "trailing_stop"
                    await self._execute_close()

                    # 수익 달성 시 연속 손실 카운터 리셋
                    self._consecutive_losses = 0
                    return

        # 고정 익절 (트레일링 비활성이거나, 아직 activation 미도달 시)
        if pnl_percent >= self.config.close_trigger_percent and not self._trailing_active:
            logger.info(f"  {self.tag} │ ★ 익절 │ PnL {pnl_sign}{pnl_percent:.2f}%")
            self._close_reason = "profit_target"
            await self._execute_close()

            # 수익 달성 시 연속 손실 카운터 리셋
            self._consecutive_losses = 0
            return

        # Feature 4: 동적 손절 적용
        stop_loss_pct = self._calc_dynamic_stop(price1)

        # 손절
        if pnl_percent <= -stop_loss_pct:
            logger.info(
                f"  {self.tag} │ ✗ 손절 │ PnL {pnl_percent:.2f}% "
                f"({'동적' if self.config.dynamic_stop_loss else '고정'} {stop_loss_pct:.2f}%)"
            )
            self._close_reason = "stop_loss"
            await self._execute_close()

            # 손절 쿨다운 설정
            if self.config.stop_loss_cooldown > 0:
                self._stop_loss_cooldown_until = time.time() + self.config.stop_loss_cooldown
                logger.info(
                    f"  {self.tag} │ 손절 쿨다운 {self.config.stop_loss_cooldown}초 설정"
                )

            # 서킷브레이커: 연속 손실 기록
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.config.circuit_breaker.consecutive_losses:
                self._circuit_breaker_until = time.time() + self.config.circuit_breaker.cooldown_seconds
                logger.warning(
                    f"  {self.tag} │ 서킷브레이커 발동 │ {self._consecutive_losses}연패 │ "
                    f"{self.config.circuit_breaker.cooldown_seconds}초 쿨다운"
                )
            return

        # DCA 추가 진입
        if (pnl_percent <= -self.config.entry_trigger_percent and
                self.entry_count < self.config.trading_limit_count):
            async with self._entry_lock:
                # 더블-체크: 락 진입 후 다시 확인
                if self.entry_count >= self.config.trading_limit_count:
                    logger.debug(f"  {self.tag} │ DCA 한도 도달 (경합 회피)")
                    return
                # [MM통합] 인벤토리 리밋 체크
                if self._exceeds_max_position(price1, price2):
                    logger.warning(
                        f"  {self.tag} │ 포지션 한도 초과 │ "
                        f"max=${self.config.risk.max_position_usd:.0f} │ DCA 차단"
                    )
                    return
                logger.info(f"  {self.tag} │ DCA #{self.entry_count + 1} 추가 진입")
                await self._execute_entry(price1, price2)

    def _analyze_direction(self) -> Optional[str]:
        """방향 결정 — 시그널 레지스트리 있으면 복합 시그널, 없으면 기존 모멘텀"""

        # ── 시그널 모듈 모드 (evolver 연동) ──
        if self.signal_registry and self._candles1_cache and self._candles2_cache:
            composite = self.signal_registry.evaluate(
                self._candles1_cache, self._candles2_cache,
                min_candles=self.config.min_candles,
                min_signal_strength=self.config.min_momentum_diff,
            )
            self._last_composite_score = composite.weighted_score

            if composite.direction is None:
                active = self.signal_registry.get_active_signals()
                logger.info(
                    f"  {self.tag} │ 대기 │ "
                    f"composite={composite.weighted_score:+.1f} "
                    f"signals={active} (threshold={self.config.min_momentum_diff})"
                )
                return None

            dir_str = (
                f"{self.config.coin1} Long / {self.config.coin2} Short"
                if composite.direction == "coin1_long"
                else f"{self.config.coin2} Long / {self.config.coin1} Short"
            )
            sig_details = " | ".join(
                f"{s.name}={s.score:+.0f}({s.confidence:.1f})"
                for s in composite.signals if s.confidence > 0
            )
            logger.info(
                f"  {self.tag} │ 방향결정 │ {dir_str} "
                f"│ score={composite.weighted_score:+.1f} │ {sig_details}"
            )
            return composite.direction

        # ── 기존 모멘텀 모드 (하위 호환) ──
        if not self.config.momentum_option:
            return "coin1_long"

        diff = self.coin1_momentum - self.coin2_momentum

        # Feature 2: 방향 비대칭 보너스 — coin2_long(ETH long) 승률이 높으므로 진입 용이
        # coin2_long은 threshold를 낮춤 (쉬운 진입), coin1_long은 threshold 유지 (엄격한 진입)
        threshold = self.config.min_momentum_diff
        if diff < 0:  # coin2_long 쪽
            # coin2_long의 진입 threshold 완화
            effective_threshold = threshold * (1.0 - self.config.coin2_long_entry_bonus)
        else:  # coin1_long 쪽
            effective_threshold = threshold

        if abs(diff) < effective_threshold:
            logger.info(
                f"  {self.tag} │ 대기 │ "
                f"{self.config.coin1}={self.coin1_momentum:.1f} "
                f"{self.config.coin2}={self.coin2_momentum:.1f} "
                f"diff={diff:.1f} (>{effective_threshold:.1f})"
            )
            return None

        if diff > 0:
            logger.info(
                f"  {self.tag} │ 방향결정 │ {self.config.coin1} Long / {self.config.coin2} Short "
                f"({self.coin1_momentum:.1f} vs {self.coin2_momentum:.1f})"
            )
            return "coin1_long"
        else:
            logger.info(
                f"  {self.tag} │ 방향결정 │ {self.config.coin2} Long / {self.config.coin1} Short "
                f"({self.coin2_momentum:.1f} vs {self.coin1_momentum:.1f}) [보너스 적용]"
            )
            return "coin2_long"

    def _exceeds_max_position(self, price1: float, price2: float) -> bool:
        """[MM통합] 최대 포지션 USD 초과 여부 체크"""
        max_pos = self.config.risk.max_position_usd
        if max_pos <= 0:
            return False  # 무제한
        current_notional = self.config.trading_margin * self.entry_count * self.config.leverage
        return current_notional >= max_pos

    def _calculate_tilted_sizes(self, size1: float, size2: float) -> tuple:
        """
        [MM통합] 인벤토리 틸트 — DCA 시 역방향 사이즈 증가, 순방향 감소.
        포지션이 한쪽에 치우쳤을 때 자연스럽게 중립 복귀.
        """
        risk = self.config.risk
        if not risk.inventory_tilt_enabled or self.entry_count == 0:
            return size1, size2

        ratio = risk.inventory_tilt_ratio
        # DCA는 기존 방향과 같으니, 역방향(청산 방향) 사이즈를 키워서 더 빨리 평단 관리
        # entry_count가 높을수록 (더 많이 물릴수록) 틸트 강화
        tilt_factor = min(1.0 + (self.entry_count / 10.0) * (ratio - 1.0), ratio)

        # coin1과 coin2 사이즈를 균형 조정
        # 전체 마진은 유지하면서 비율만 조정
        avg = (size1 + size2) / 2
        size1_tilted = avg * tilt_factor
        size2_tilted = avg * (2.0 - tilt_factor)  # 합계 유지

        return size1_tilted, size2_tilted

    def _check_regime(self) -> tuple[bool, float]:
        """
        Feature 1: 롤링 상관계수 레짐 필터
        최근 24개 캔들(6시간 at 15min)의 Pearson 상관계수 계산.
        상관계수 >= min_correlation이면 안전한 페어트레이딩 레짐.

        Returns:
            (safe: bool, correlation: float)
            safe=True if correlation >= threshold, False if below
        """
        if self.config.min_correlation <= 0 or not self._candles1_cache or not self._candles2_cache:
            return True, 1.0  # 비활성 또는 캔들 부족 시 항상 안전

        try:
            import numpy as np

            # 최근 24개 캔들만 사용 (6시간 at 15min interval)
            closes1 = np.array([float(c.get('close', 0)) for c in self._candles1_cache[-24:]])
            closes2 = np.array([float(c.get('close', 0)) for c in self._candles2_cache[-24:]])

            if len(closes1) < 20 or len(closes2) < 20:
                return True, 1.0  # 데이터 부족 시 안전

            # Pearson 상관계수 계산
            corr_matrix = np.corrcoef(closes1, closes2)
            correlation = float(corr_matrix[0, 1])

            # NaN 체크 (변동 없는 시장)
            if np.isnan(correlation):
                correlation = 0.5  # 보수적인 값

            safe = correlation >= self.config.min_correlation
            return safe, correlation

        except Exception as e:
            logger.debug(f"  {self.tag} │ 상관계수 계산 실패: {e}")
            return True, 1.0  # 실패 시 안전

    def _calc_dynamic_margin(self, price1: float) -> tuple[float, float]:
        """
        Feature 3: ATR 기반 동적 포지션 사이징
        ATR이 높으면 (변동성 높음) 마진 축소, 낮으면 (안정적) 마진 확대.

        Returns:
            (adjusted_margin: float, atr_pct: float)
        """
        if not self.config.dynamic_sizing or not self._candles1_cache:
            return self.config.trading_margin, 0.0

        try:
            import numpy as np

            candles = self._candles1_cache[-14:]  # 최근 14개 캔들 (ATR 표준)
            if len(candles) < 14:
                return self.config.trading_margin, 0.0

            highs = np.array([float(c.get('high', 0)) for c in candles])
            lows = np.array([float(c.get('low', 0)) for c in candles])
            closes = np.array([float(c.get('close', 0)) for c in candles])

            # True Range 계산
            tr = np.maximum(
                highs - lows,
                np.maximum(
                    np.abs(highs - np.roll(closes, 1)),
                    np.abs(lows - np.roll(closes, 1))
                )
            )
            atr = np.mean(tr)
            atr_pct = (atr / price1) * 100

            # 마진 스케일링: target_atr를 기준으로 조정
            target_atr = self.config.target_atr_pct
            denominator = max(atr_pct, target_atr * 0.5)
            scale_factor = target_atr / denominator
            adjusted_margin = self.config.trading_margin * scale_factor

            # 마진 범위 제한: 0.5x ~ 1.5x
            adjusted_margin = max(
                self.config.trading_margin * 0.5,
                min(adjusted_margin, self.config.trading_margin * 1.5)
            )

            return adjusted_margin, atr_pct

        except Exception as e:
            logger.debug(f"  {self.tag} │ ATR 계산 실패: {e}")
            return self.config.trading_margin, 0.0

    def _calc_dynamic_stop(self, price1: float) -> float:
        """
        Feature 4: ATR 기반 동적 손절
        ATR로부터 손절 수준을 동적으로 계산.

        Returns:
            dynamic_stop_pct: float (손절 %)
        """
        if not self.config.dynamic_stop_loss or not self._candles1_cache:
            return self.config.stop_loss_percent

        try:
            import numpy as np

            candles = self._candles1_cache[-14:]
            if len(candles) < 14:
                return self.config.stop_loss_percent

            highs = np.array([float(c.get('high', 0)) for c in candles])
            lows = np.array([float(c.get('low', 0)) for c in candles])
            closes = np.array([float(c.get('close', 0)) for c in candles])

            # True Range 계산
            tr = np.maximum(
                highs - lows,
                np.maximum(
                    np.abs(highs - np.roll(closes, 1)),
                    np.abs(lows - np.roll(closes, 1))
                )
            )
            atr = np.mean(tr)
            atr_pct = (atr / price1) * 100

            dynamic_stop = atr_pct * self.config.stop_atr_multiplier

            # 범위 제한
            dynamic_stop = max(
                self.config.min_stop_pct,
                min(dynamic_stop, self.config.max_stop_pct)
            )

            return dynamic_stop

        except Exception as e:
            logger.debug(f"  {self.tag} │ 동적 손절 계산 실패: {e}")
            return self.config.stop_loss_percent

    async def _execute_entry(self, price1: float, price2: float):
        """진입 주문 실행 — 지정가 또는 시장가"""
        margin = self.config.trading_margin
        leverage = self.config.leverage
        lo = self.config.limit_order

        # Feature 3: 동적 포지션 사이징 (ATR 기반)
        if self.config.dynamic_sizing:
            margin, atr_pct = self._calc_dynamic_margin(price1)
            if atr_pct > 0:
                logger.info(
                    f"  {self.tag} │ 동적 사이징 │ ATR={atr_pct:.2f}% "
                    f"→ 마진 ${self.config.trading_margin:.0f} → ${margin:.0f}"
                )

        # coin1_long 비대칭 마진 — coin1_long 승률이 낮으므로 마진 축소
        if (self.direction == "coin1_long"
                and self.config.coin1_long_margin_ratio < 1.0
                and self.entry_count == 0):  # 신규 진입만 (DCA는 원래 마진)
            margin = margin * self.config.coin1_long_margin_ratio
            logger.info(
                f"  {self.tag} │ coin1_long 마진 축소 │ "
                f"${self.config.trading_margin:.0f} → ${margin:.0f} "
                f"(x{self.config.coin1_long_margin_ratio})"
            )

        try:
            size1 = (margin * leverage) / price1
            size2 = (margin * leverage) / price2

            # [MM통합] 인벤토리 틸트 적용
            size1, size2 = self._calculate_tilted_sizes(size1, size2)

            if self.direction == "coin1_long":
                side1, side2 = "buy", "sell"
            else:
                side1, side2 = "sell", "buy"

            if lo.enabled:
                await self._execute_limit_entry(
                    self.config.coin1, side1, size1,
                    self.config.coin2, side2, size2,
                    margin, price1, price2,
                )
            else:
                await self._execute_market_entry(
                    self.config.coin1, side1, size1,
                    self.config.coin2, side2, size2,
                    price1, price2, margin,
                )

        except Exception as e:
            logger.error(f"  {self.tag} │ 진입 실패: {e}")

    async def _execute_market_entry(
        self, coin1, side1, size1, coin2, side2, size2,
        price1, price2, margin,
    ):
        """기존 시장가 진입 (폴백용) — orphan 방지 포함"""
        fill1_ok = False
        try:
            logger.info(f"  {self.tag} │ 시장가 진입 │ {coin1} {side1} {size1:.6f}")
            await self.wrapper.create_order(
                symbol=coin1, side=side1,
                amount=size1, price=None, order_type="market"
            )
            fill1_ok = True
        except Exception as e:
            logger.error(f"  {self.tag} │ {coin1} 시장가 실패: {e}")
            return

        try:
            logger.info(f"  {self.tag} │ 시장가 진입 │ {coin2} {side2} {size2:.6f}")
            await self.wrapper.create_order(
                symbol=coin2, side=side2,
                amount=size2, price=None, order_type="market"
            )
        except Exception as e:
            # coin1은 체결됐는데 coin2 실패 → orphan 방지: coin1 즉시 청산
            logger.error(f"  {self.tag} │ {coin2} 시장가 실패: {e} → {coin1} 포지션 청산 시도")
            try:
                pos1 = await self.wrapper.get_position(coin1)
                if self._has_position(pos1):
                    await self.wrapper.close_position(coin1, pos1)
                    logger.info(f"  {self.tag} │ {coin1} orphan 포지션 청산 완료")
            except Exception as ce:
                logger.error(f"  {self.tag} │ {coin1} orphan 청산 실패: {ce}")
            return

        notional = margin * self.config.leverage * 2
        self._taker_fills += 2
        self._log_fee("taker", notional)
        logger.info(f"  {self.tag} │ 시장가 진입 완료 │ {coin1}={price1:.2f} {coin2}={price2:.2f}")

        self._record_entry(price1, price2, margin, fill_type="taker")

    async def _execute_limit_entry(
        self, coin1, side1, size1, coin2, side2, size2,
        margin, price1, price2,
    ):
        """
        BBO 지정가 진입 + 100ms 조정 + 5회 리트라이 + 시장가 폴백
        헤지 안전장치: 쌍 체결 보장
        """
        lo = self.config.limit_order
        interval = lo.adjust_interval_ms / 1000.0  # seconds
        max_retries = lo.max_retries
        pair_timeout = lo.pair_timeout_ms / 1000.0
        offset_pct = lo.bbo_offset_percent / 100.0

        fill1 = False
        fill2 = False
        entry_start = time.time()

        # ── COIN1 지정가 시도 ──
        fill1, fill1_price = await self._limit_order_with_retry(
            coin1, side1, size1, offset_pct, interval, max_retries
        )

        # ── COIN2 지정가 시도 ──
        fill2, fill2_price = await self._limit_order_with_retry(
            coin2, side2, size2, offset_pct, interval, max_retries
        )

        # ── 헤지 안전장치: 쌍 체결 보장 ──
        elapsed = time.time() - entry_start

        if fill1 and not fill2:
            logger.warning(f"  {self.tag} │ {coin1} 체결, {coin2} 미체결 → 시장가 강제")
            try:
                await self.wrapper.create_order(
                    symbol=coin2, side=side2,
                    amount=size2, price=None, order_type="market"
                )
                fill2 = True
                fill2_price = await self._get_mark_price_safe(coin2)
                self._taker_fills += 1
                logger.info(f"  {self.tag} │ {coin2} 헤지 시장가 체결 완료")
            except Exception as e:
                logger.error(f"  {self.tag} │ {coin2} 헤지 시장가 실패: {e}")

        elif fill2 and not fill1:
            logger.warning(f"  {self.tag} │ {coin2} 체결, {coin1} 미체결 → 시장가 강제")
            try:
                await self.wrapper.create_order(
                    symbol=coin1, side=side1,
                    amount=size1, price=None, order_type="market"
                )
                fill1 = True
                fill1_price = await self._get_mark_price_safe(coin1)
                self._taker_fills += 1
                logger.info(f"  {self.tag} │ {coin1} 헤지 시장가 체결 완료")
            except Exception as e:
                logger.error(f"  {self.tag} │ {coin1} 헤지 시장가 실패: {e}")

        elif not fill1 and not fill2:
            # 둘 다 실패 — 시장가 폴백
            logger.warning(f"  {self.tag} │ 양쪽 미체결 → 시장가 폴백")
            await self._execute_market_entry(
                coin1, side1, size1, coin2, side2, size2,
                price1, price2, margin,
            )
            return

        # 기록
        final_p1 = fill1_price or price1
        final_p2 = fill2_price or price2
        self._record_entry(final_p1, final_p2, margin, fill_type="maker")
        total_elapsed_ms = (time.time() - entry_start) * 1000
        logger.info(
            f"  {self.tag} │ 지정가 진입 완료 │ {total_elapsed_ms:.0f}ms │ "
            f"{coin1}={final_p1:.2f} {coin2}={final_p2:.2f}"
        )

    async def _limit_order_with_retry(
        self, symbol: str, side: str, size: float,
        offset_pct: float, interval: float, max_retries: int,
    ) -> tuple:
        """
        [MM통합 개선] 단일 코인 지정가 주문:
        - Post-Only(ALO) 강제 → Taker 체결 원천 차단
        - 리프라이싱 threshold → BBO 변동 시에만 재배치 (API 절약)
        - Order TTL → 장기 미체결 자동 취소
        - IOC 폴백 → 긴급 시장가 체결
        Returns: (filled: bool, fill_price: float|None)
        """
        lo = self.config.limit_order
        reprice_threshold = lo.reprice_threshold / 100.0  # % → ratio
        order_start = time.time()
        order_ttl = lo.order_ttl_ms / 1000.0
        last_order_price = None
        tif = "Alo" if lo.post_only else "Gtc"  # Post-Only or GTC

        for attempt in range(max_retries):
            # [MM통합] Order TTL 체크
            if time.time() - order_start > order_ttl:
                logger.info(f"  {self.tag} │ {symbol} TTL {lo.order_ttl_ms}ms 만료 → 시장가 폴백")
                break

            # 1) BBO 가격 조회
            bbo_price = await self._get_bbo_price(symbol, side, offset_pct)
            if bbo_price is None:
                logger.info(f"  {self.tag} │ {symbol} BBO 조회 실패 → 시장가 폴백")
                return (False, None)

            # [MM통합] 리프라이싱 threshold — BBO 변동이 작으면 재배치 스킵
            if last_order_price is not None and attempt > 0:
                price_diff = abs(bbo_price - last_order_price) / last_order_price
                if price_diff < reprice_threshold:
                    # BBO 거의 안 변했으면 기존 주문 유지, 체결만 확인
                    await asyncio.sleep(interval)
                    open_orders = await self.wrapper.get_open_orders(symbol)
                    if not open_orders:
                        pos = await self.wrapper.get_position(symbol)
                        if self._has_position(pos):
                            self._maker_fills += 1
                            self._log_fee("maker", size * last_order_price)
                            return (True, last_order_price)
                        else:
                            last_order_price = None
                            continue
                    continue

            # 2) 기존 주문 취소 (리프라이싱)
            if attempt > 0:
                try:
                    await self.wrapper.cancel_orders(symbol)
                except Exception as e:
                    logger.debug(f"  {self.tag} │ {symbol} 주문 취소 실패 (무시): {e}")

            # 3) 지정가 주문 배치 (Post-Only)
            logger.info(f"  {self.tag} │ {symbol} 지정가 #{attempt+1} │ {side} {size:.6f} @ {bbo_price:.4f} [{tif}]")
            try:
                result = await self.wrapper.create_order(
                    symbol=symbol, side=side,
                    amount=size, price=bbo_price, order_type="limit",
                    tif=tif,
                )
                last_order_price = bbo_price
            except Exception as e:
                # Post-Only 거부 시 (BBO 넘어서면 거부됨) → 다음 시도에서 가격 재조정
                err_str = str(e)
                if "post only" in err_str.lower() or "would cross" in err_str.lower():
                    logger.info(f"  {self.tag} │ {symbol} Post-Only 거부 → 가격 재조정")
                    await asyncio.sleep(interval)
                    continue
                logger.warning(f"  {self.tag} │ {symbol} 지정가 주문 실패: {e}")
                return (False, None)

            # 4) 대기
            await asyncio.sleep(interval)

            # 5) 체결 확인 — open_orders 비어있으면 포지션으로 이중 검증
            #    ALO 거부 시 주문이 사라져서 false-fill 오판 방지
            open_orders = await self.wrapper.get_open_orders(symbol)
            if not open_orders:
                # 포지션 존재 여부로 실제 체결 확인
                pos = await self.wrapper.get_position(symbol)
                if self._has_position(pos):
                    self._maker_fills += 1
                    self._log_fee("maker", size * bbo_price)
                    return (True, bbo_price)
                else:
                    logger.info(f"  {self.tag} │ {symbol} ALO 거부 (포지션 없음) → 재시도")
                    last_order_price = None  # 가격 재계산 강제
                    continue

        # max_retries/TTL 소진 → 잔여 주문 취소 후 시장가 폴백
        try:
            await self.wrapper.cancel_orders(symbol)
        except Exception as e:
            logger.debug(f"  {self.tag} │ {symbol} 잔여 주문 취소 실패 (무시): {e}")

        logger.info(f"  {self.tag} │ {symbol} 리트라이 소진 → 시장가 폴백")
        return await self._emergency_market_fill(symbol, side, size)

    async def _emergency_market_fill(self, symbol: str, side: str, size: float) -> tuple:
        """[MM통합] IOC 또는 시장가 긴급 체결"""
        lo = self.config.limit_order
        try:
            if lo.use_ioc_fallback:
                # IOC(Immediate-or-Cancel) — 즉시 체결 안 되면 취소
                mark = await self.wrapper.get_mark_price(symbol)
                if mark:
                    slippage = 0.002  # 0.2% 슬리피지 허용
                    ioc_price = mark * (1 + slippage) if side == "buy" else mark * (1 - slippage)
                    await self.wrapper.create_order(
                        symbol=symbol, side=side,
                        amount=size, price=ioc_price, order_type="limit",
                        tif="Ioc",
                    )
                    logger.info(f"  {self.tag} │ {symbol} IOC 체결 @ {ioc_price:.2f}")
                else:
                    await self.wrapper.create_order(
                        symbol=symbol, side=side,
                        amount=size, price=None, order_type="market"
                    )
            else:
                await self.wrapper.create_order(
                    symbol=symbol, side=side,
                    amount=size, price=None, order_type="market"
                )
            self._taker_fills += 1
            mark = await self.wrapper.get_mark_price(symbol)
            self._log_fee("taker", size * (mark or 0))
            return (True, mark)
        except Exception as e:
            logger.error(f"  {self.tag} │ {symbol} 긴급 체결 실패: {e}")
            return (False, None)

    async def _execute_close_limit(self):
        """[MM통합] Observe 모드 전용 — 지정가 청산 (Maker 수수료만)"""
        lo = self.config.limit_order
        offset_pct = lo.bbo_offset_percent / 100.0

        try:
            for coin in [self.config.coin1, self.config.coin2]:
                pos = await self.wrapper.get_position(coin)
                if not self._has_position(pos):
                    continue

                close_side = "sell" if pos.get("side", "").lower() in ("long", "buy") else "buy"
                close_size = float(pos["size"])
                bbo = await self._get_bbo_price(coin, close_side, offset_pct)

                if bbo:
                    tif = "Alo" if lo.post_only else "Gtc"
                    await self.wrapper.create_order(
                        symbol=coin, side=close_side,
                        amount=close_size, price=bbo, order_type="limit",
                        tif=tif,
                    )
                    logger.info(f"  {self.tag} │ OBSERVE │ {coin} 지정가 청산 주문 @ {bbo:.2f}")

            # 체결 대기 (다음 사이클에서 확인)
            await asyncio.sleep(1)

            # 전부 청산됐는지 확인
            all_closed = True
            for coin in [self.config.coin1, self.config.coin2]:
                pos = await self.wrapper.get_position(coin)
                if self._has_position(pos):
                    all_closed = False
                    break

            if all_closed:
                logger.info(f"  {self.tag} │ OBSERVE │ 전체 포지션 지정가 청산 완료")
                if self._current_trade_id:
                    self._safe_log_trade(
                        "close_trade",
                        trade_id=self._current_trade_id,
                        pnl_percent=self._last_pnl_percent,
                        pnl_usd=self._last_pnl_percent * self.config.trading_margin * self.entry_count / 100,
                        reason=self._close_reason or "observe",
                    )
                self.coin1_position = None
                self.coin2_position = None
                self.direction = None
                self.entry_count = 0
                self._current_trade_id = None
                self._close_reason = None
        except Exception as e:
            logger.error(f"  {self.tag} │ OBSERVE 청산 실패: {e}")

    async def _get_bbo_price(self, symbol: str, side: str, offset_pct: float) -> Optional[float]:
        """
        오더북에서 BBO 가격 추출.
        Long(buy) → Best Bid + offset
        Short(sell) → Best Ask - offset
        adaptive_offset=True: 스프레드가 좁으면 BBO에 더 가깝게, 넓으면 안쪽으로
        오더북 없으면 mark price 폴백.
        """
        try:
            if hasattr(self.wrapper, 'get_orderbook'):
                book = await self.wrapper.get_orderbook(symbol)
                if book:
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    if bids and asks:
                        best_bid = float(bids[0][0])
                        best_ask = float(asks[0][0])
                        mid = (best_bid + best_ask) / 2.0
                        spread_pct = (best_ask - best_bid) / mid if mid else 0

                        # 동적 오프셋: 스프레드의 30%만큼 안쪽에 배치 (Maker 체결률 UP)
                        lo = self.config.limit_order
                        if lo.adaptive_offset and spread_pct > 0:
                            adaptive = min(spread_pct * 0.3, offset_pct * 3)  # 최대 3배
                            effective_offset = max(adaptive, offset_pct * 0.5)  # 최소 기본의 절반
                        else:
                            effective_offset = offset_pct

                        if side == "buy":
                            return best_bid * (1 + effective_offset)
                        else:
                            return best_ask * (1 - effective_offset)
                    elif side == "buy" and bids:
                        return float(bids[0][0]) * (1 + offset_pct)
                    elif side == "sell" and asks:
                        return float(asks[0][0]) * (1 - offset_pct)
        except Exception as e:
            logger.debug(f"  {self.tag} │ {symbol} 오더북 조회 실패: {e}")

        # 폴백: mark price
        try:
            mark = await self._get_mark_price_safe(symbol)
            if mark:
                if side == "buy":
                    return mark * (1 + offset_pct)
                else:
                    return mark * (1 - offset_pct)
        except Exception as e:
            logger.debug(f"  {self.tag} │ {symbol} mark price 폴백 실패: {e}")
        return None

    def _log_fee(self, fill_type: str, notional: float):
        """수수료 트래킹 + 절감 로깅"""
        if fill_type == "maker":
            actual_fee = notional * self._maker_fee_rate
            would_be_fee = notional * self._taker_fee_rate
            saved = would_be_fee - actual_fee
            self._fee_saved_total += saved
            self._cumulative_fee_usd += actual_fee
        else:
            actual_fee = notional * self._taker_fee_rate
            self._cumulative_fee_usd += actual_fee
        logger.info(
            f"  {self.tag} │ 수수료 │ "
            f"Maker={self._maker_fills} Taker={self._taker_fills} "
            f"절감=${self._fee_saved_total:.2f}"
        )

    def _record_entry(self, price1, price2, margin, fill_type="taker"):
        """진입 기록 (공통)"""
        # 첫 진입 시 correlation_id 생성
        if self.entry_count == 0:
            self._correlation_id = str(uuid.uuid4())[:8]

        self.entry_count += 1

        entry = {
            "time": datetime.now().isoformat(),
            "price1": price1,
            "price2": price2,
            "ratio": price1 / price2 if price2 else 0,
            "margin": margin,
            "fill_type": fill_type,
        }

        if self.coin1_position is None:
            self.coin1_position = Position(
                coin=self.config.coin1,
                side="long" if self.direction == "coin1_long" else "short"
            )
        if self.coin2_position is None:
            self.coin2_position = Position(
                coin=self.config.coin2,
                side="short" if self.direction == "coin1_long" else "long"
            )

        self.coin1_position.entries.append(entry)
        self.coin2_position.entries.append(entry)
        self.coin1_position.entry_count = self.entry_count
        self.coin2_position.entry_count = self.entry_count

        logger.info(
            f"  {self.tag} │ [{self._correlation_id}] 진입 #{self.entry_count} [{fill_type}] │ "
            f"{self.config.coin1}={price1:.2f} {self.config.coin2}={price2:.2f}"
        )

        # 대시보드 로깅
        if self.entry_count == 1:
            self._current_trade_id = self._safe_log_trade(
                "open_trade",
                exchange=self.exchange_name,
                direction=self.direction,
                coin1=self.config.coin1,
                coin2=self.config.coin2,
            )
        if self._current_trade_id:
            self._safe_log_trade(
                "log_entry",
                trade_id=self._current_trade_id,
                entry_num=self.entry_count,
                price1=price1,
                price2=price2,
                margin=margin,
            )

    async def _execute_close(self):
        """청산 주문 실행 (래퍼의 close_position 사용)"""
        try:
            closed = await self._close_coin_positions(
                [self.config.coin1, self.config.coin2]
            )
            if closed:
                logger.info(f"  {self.tag} │ [{self._correlation_id}] 전체 청산 완료 │ 사유={self._close_reason}")
            else:
                logger.warning(f"  {self.tag} │ [{self._correlation_id}] 청산 대상 포지션 없음")

            # 대시보드 로깅
            if self._current_trade_id:
                self._safe_log_trade(
                    "close_trade",
                    trade_id=self._current_trade_id,
                    pnl_percent=self._last_pnl_percent,
                    pnl_usd=self._last_pnl_percent * self.config.trading_margin * self.entry_count / 100,
                    reason=self._close_reason or "manual",
                )
            else:
                logger.warning(
                    f"  {self.tag} │ [{self._correlation_id}] trade_id 없음 — DB close 기록 누락 "
                    f"(pnl={self._last_pnl_percent:.2f}% reason={self._close_reason})"
                )

            # 상태 초기화
            self.coin1_position = None
            self.coin2_position = None
            self.direction = None
            self.entry_count = 0
            self._current_trade_id = None
            self._correlation_id = None
            self._close_reason = None
            self._peak_pnl = 0.0
            self._trailing_active = False
            self._cycles_since_entry = 0
            self._cumulative_fee_usd = 0.0

        except Exception as e:
            logger.error(f"  {self.tag} │ [{self._correlation_id}] 청산 실패: {e}")

    def _calculate_total_pnl_percent(self, current_price1: float, current_price2: float) -> float:
        """전체 포지션 PnL % 계산"""
        if not self.coin1_position or not self.coin1_position.entries:
            return 0.0

        total_margin = self.config.trading_margin * self.entry_count
        if total_margin == 0:
            return 0.0

        total_pnl = 0.0

        for entry in self.coin1_position.entries:
            entry_price1 = entry["price1"]
            entry_price2 = entry["price2"]
            margin = entry["margin"]
            leverage = self.config.leverage

            if self.direction == "coin1_long":
                pnl1 = ((current_price1 - entry_price1) / entry_price1) * margin * leverage
                pnl2 = ((entry_price2 - current_price2) / entry_price2) * margin * leverage
            else:
                pnl1 = ((entry_price1 - current_price1) / entry_price1) * margin * leverage
                pnl2 = ((current_price2 - entry_price2) / entry_price2) * margin * leverage

            total_pnl += pnl1 + pnl2

        # 수수료 차감: 누적 진입 수수료 + 예상 청산 수수료 (taker 가정)
        close_notional = total_margin * self.config.leverage * 2
        estimated_close_fee = close_notional * self._taker_fee_rate
        total_fee = self._cumulative_fee_usd + estimated_close_fee
        total_pnl -= total_fee

        return (total_pnl / total_margin) * 100

    async def _update_momentum(self):
        """모멘텀 점수 업데이트 + 시그널 모듈용 캔들 캐시"""
        try:
            candles1 = await self.candle_fetcher.get_candles(
                symbol=self.config.coin1,
                interval=self.config.chart_time,
                limit=self.config.candle_limit
            )
            candles2 = await self.candle_fetcher.get_candles(
                symbol=self.config.coin2,
                interval=self.config.chart_time,
                limit=self.config.candle_limit
            )

            # 시그널 모듈용 캔들 캐시 (signal_registry가 있으면 _analyze_direction에서 사용)
            self._candles1_cache = candles1
            self._candles2_cache = candles2

            if candles1 and len(candles1) >= self.config.min_candles:
                self.coin1_momentum = calculate_momentum_score(candles1)

            if candles2 and len(candles2) >= self.config.min_candles:
                self.coin2_momentum = calculate_momentum_score(candles2)

            logger.debug(
                f"[{self.exchange_name}] 모멘텀 업데이트: "
                f"{self.config.coin1}={self.coin1_momentum:.1f}, "
                f"{self.config.coin2}={self.coin2_momentum:.1f}"
            )

        except Exception as e:
            logger.error(f"  {self.tag} │ 모멘텀 업데이트 실패: {e}")
