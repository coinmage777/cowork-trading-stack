"""
NADO Pair Scalper — BTC/ETH 모멘텀 페어 스캘핑

BTC와 ETH의 모멘텀 차이를 감지하여 강세 코인 LONG / 약세 코인 SHORT 동시 진입.
달러 뉴트럴 구조로 시장 방향 무관, 상대적 강도 차이에서 수익 추구.

핵심:
- 20x 레버리지, 최대 20회 분할진입
- 1회 진입 마진 50 USDT (코인당 25 USDT, 노출 500 USDT/코인)
- 모멘텀 >= 1.0 진입, 모멘텀 이탈 청산
- 스프레드 익절 0.2%, 절대 손절 -10%
- 60초 스캔 주기
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .candle_fetcher import CandleFetcher
from .momentum import calculate_rsi

logger = logging.getLogger(__name__)

# Synergy: basis_arb → nado divergence 조기 경고
try:
    from .shared_state import EVENT_BUS, VENUE_CAP, POSITION_REGISTRY, REGIME_GATE
    _SYNERGY = True
    _divergence_pause_until: float = 0.0
    def _on_divergence(payload: dict):
        """basis_arb가 venue 간 가격 괴리 감지 시 nado 신규 진입 5분 차단."""
        global _divergence_pause_until
        if payload.get("gap_pct", 0) > 0.3:
            _divergence_pause_until = time.time() + 300
            logger.warning(
                f"  [nado] basis_arb divergence {payload.get('symbol')} "
                f"{payload.get('gap_pct'):.2f}% → 5분 진입 차단"
            )
    EVENT_BUS.subscribe("divergence_detected", _on_divergence)
except Exception:
    _SYNERGY = False
    _divergence_pause_until = 0.0

# 2026-04-18 v3: 포트폴리오 레벨 리스크 추적
# 모든 NadoPairScalper 인스턴스가 공유하는 global state
# {exchange_name: {"margin": committed_margin, "entry_time": ts}}
_PORTFOLIO_POSITIONS: dict = {}
_PORTFOLIO_DAILY_PNL: float = 0.0
_PORTFOLIO_DAY: str = ""  # "YYYY-MM-DD" to detect day rollover

# 2026-04-23 HIP-3 stale price gate:
# HIP-3 심볼(hyna:*)은 메인 HL WebSocket에 포함되지 않아 REST 폴백이 자주 발생.
# REST 폴백 시 응답이 2s+ 걸리고 가격이 오래된 값이라 pair_direction_error
# 판정이 잘못 나오는 문제가 있음. 아래 접두사로 시작하는 심볼에서
# 마크프라이스 조회가 HIP3_STALE_THRESHOLD_S 초 이상 걸리면 신규 진입을 스킵한다.
HIP3_SYMBOL_PREFIXES = ("hyna:",)
HIP3_STALE_THRESHOLD_S = 2.0


def _is_hip3_symbol(sym: str) -> bool:
    if not isinstance(sym, str):
        return False
    low = sym.strip().lower()
    return any(low.startswith(p) for p in HIP3_SYMBOL_PREFIXES)



def get_portfolio_total_margin() -> float:
    return sum(p.get("margin", 0) for p in _PORTFOLIO_POSITIONS.values())


# 2026-04-20: Venue-level aggregate cap
# 여러 alias(hyperliquid_2/hyena/miracle 등)가 같은 real_exchange 오더북 공유
# → 한 방향 동시 집중 진입 방지
_VENUE_POSITIONS: dict = {}  # {real_venue: {"btc_long": count, "eth_long": count}}
_EXCHANGE_TO_REAL_VENUE: dict = {}  # multi_runner가 startup 때 주입


def register_real_venue_mapping(alias_map: dict) -> None:
    _EXCHANGE_TO_REAL_VENUE.update(alias_map)


def _real_venue_of(exchange_name: str) -> str:
    return _EXCHANGE_TO_REAL_VENUE.get(exchange_name, exchange_name)


def get_venue_direction_count(real_venue: str, direction: str) -> int:
    return _VENUE_POSITIONS.get(real_venue, {}).get(direction, 0)


def register_venue_position(real_venue: str, direction: str) -> None:
    d = _VENUE_POSITIONS.setdefault(real_venue, {"btc_long": 0, "eth_long": 0})
    d[direction] = d.get(direction, 0) + 1


def unregister_venue_position(real_venue: str, direction: str) -> None:
    if real_venue in _VENUE_POSITIONS and direction:
        d = _VENUE_POSITIONS[real_venue]
        d[direction] = max(0, d.get(direction, 0) - 1)



def get_portfolio_position_count() -> int:
    return len(_PORTFOLIO_POSITIONS)


def register_portfolio_position(exchange: str, margin: float, ts: float = 0) -> None:
    _PORTFOLIO_POSITIONS[exchange] = {"margin": margin, "entry_time": ts or time.time()}


def unregister_portfolio_position(exchange: str) -> None:
    _PORTFOLIO_POSITIONS.pop(exchange, None)


def update_portfolio_pnl(pnl_usd: float) -> float:
    """거래 종료 시 누적 PnL 갱신. 자정에 자동 초기화."""
    global _PORTFOLIO_DAILY_PNL, _PORTFOLIO_DAY
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    if today != _PORTFOLIO_DAY:
        _PORTFOLIO_DAY = today
        _PORTFOLIO_DAILY_PNL = 0.0
    _PORTFOLIO_DAILY_PNL += pnl_usd
    return _PORTFOLIO_DAILY_PNL


def get_portfolio_daily_pnl() -> float:
    return _PORTFOLIO_DAILY_PNL


# 2026-04-18 v3: 글로벌 correlation 추세 감지
# 모든 거래소가 같은 BTC/ETH 시장 → 공유 state에 correlation 이력 누적
from collections import deque
_CORR_HISTORY: deque = deque(maxlen=60)  # 최근 60 샘플 (30초 scan = 30분)

# 2026-04-18 v4: Portfolio-level Stop-Loss Circuit Breaker
# 3분 내 5건+ stop_loss 시 전체 2시간 진입 중단
_STOP_LOSS_EVENTS: deque = deque(maxlen=30)  # (ts, exchange, pnl_usd)
_CIRCUIT_BREAKER_UNTIL: float = 0.0  # unix ts
_CB_WINDOW_SEC: int = 180   # 3분
_CB_THRESHOLD: int = 10      # 10건 이상 (2026-04-22: 15거래소 환경 노이즈 필터, 5→10 상향)
_CB_PAUSE_SEC: int = 3600   # 1시간 중단 (2026-04-22: 2h→1h, 시장 회복 기회 보전)


def record_stop_loss(exchange: str, pnl_usd: float) -> bool:
    """stop_loss 이벤트 기록. Circuit breaker 트리거되면 True 반환."""
    global _CIRCUIT_BREAKER_UNTIL
    now = time.time()
    _STOP_LOSS_EVENTS.append((now, exchange, pnl_usd))
    # 최근 window 내 stop_loss 카운트
    recent = [e for e in _STOP_LOSS_EVENTS if now - e[0] <= _CB_WINDOW_SEC]
    if len(recent) >= _CB_THRESHOLD and _CIRCUIT_BREAKER_UNTIL < now:
        _CIRCUIT_BREAKER_UNTIL = now + _CB_PAUSE_SEC
        return True
    return False


def is_circuit_breaker_active() -> tuple[bool, float]:
    """(active, remaining_seconds) 반환."""
    now = time.time()
    if _CIRCUIT_BREAKER_UNTIL > now:
        return True, _CIRCUIT_BREAKER_UNTIL - now
    return False, 0


def reset_circuit_breaker() -> None:
    global _CIRCUIT_BREAKER_UNTIL
    _CIRCUIT_BREAKER_UNTIL = 0


def record_correlation(corr: float) -> None:
    if corr is None:
        return
    _CORR_HISTORY.append((time.time(), float(corr)))


# 2026-04-18 v3: 변동성 regime 감지 (BTC 기준)
# 최근 60 샘플 (30초 scan = 30분) 가격 변화율 저장
_BTC_PRICE_HISTORY: deque = deque(maxlen=120)


def record_btc_price(price: float) -> None:
    if price > 0:
        _BTC_PRICE_HISTORY.append((time.time(), float(price)))


def current_volatility_regime() -> tuple[float, str]:
    """1시간 rolling std of 1-min returns (ATR 근사). (ATR%, regime label) 반환."""
    if len(_BTC_PRICE_HISTORY) < 10:
        return 0.0, "unknown"
    prices = [p for _, p in _BTC_PRICE_HISTORY]
    # 샘플별 % 변화
    changes = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0:
            changes.append((prices[i] - prices[i - 1]) / prices[i - 1] * 100)
    if not changes:
        return 0.0, "unknown"
    # std (%)
    mean = sum(changes) / len(changes)
    var = sum((c - mean) ** 2 for c in changes) / len(changes)
    atr_pct = var ** 0.5
    if atr_pct < 0.3:
        return atr_pct, "low"
    elif atr_pct < 0.6:
        return atr_pct, "medium"
    elif atr_pct < 1.0:
        return atr_pct, "high"
    else:
        return atr_pct, "extreme"


def volatility_margin_multiplier() -> float:
    """변동성 regime에 따른 margin multiplier."""
    _, regime = current_volatility_regime()
    return {
        "low": 1.2,        # 저변동성 = 스프레드 수렴 안정 → 공격적
        "medium": 1.0,     # 기본
        "high": 0.7,       # 고변동성 = 보수적
        "extreme": 0.0,    # 극변동성 = 진입 중단
        "unknown": 1.0,
    }.get(regime, 1.0)


def correlation_trend() -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(현재값, 최근10분 평균, 1시간 전 대비 변화량) 반환. 데이터 부족 시 None."""
    if len(_CORR_HISTORY) < 10:
        return None, None, None
    now_ts = time.time()
    current = _CORR_HISTORY[-1][1]
    cutoff_10m = now_ts - 600
    recent_10m = [c for t, c in _CORR_HISTORY if t >= cutoff_10m]
    avg_10m = sum(recent_10m) / len(recent_10m) if recent_10m else None
    # 1h 전과 비교 (있으면)
    cutoff_1h = now_ts - 3600
    older = [c for t, c in _CORR_HISTORY if t < cutoff_1h]
    avg_1h = sum(older) / len(older) if older else None
    delta = (avg_10m - avg_1h) if (avg_10m and avg_1h) else None
    return current, avg_10m, delta


@dataclass
class NadoPairConfig:
    leverage: int = 20
    total_margin_limit: float = 1000.0
    margin_per_entry: float = 50.0       # 1회 진입 마진 (양쪽 합계)
    max_entries: int = 20
    post_close_cooldown_sec: int = 0     # 청산 후 진입 금지 쿨다운 (0=비활성)
    coin1: str = "BTC"
    coin2: str = "ETH"
    scan_interval: int = 60              # 초
    momentum_threshold: float = 1.0      # 모멘텀 진입 임계값
    exit_momentum_threshold: float = 0.0  # 모멘텀 청산 임계값 (0이면 momentum_threshold와 동일)
    momentum_period: int = 14            # 모멘텀 계산 기간 (캔들 수)
    spread_entry_threshold: float = 0.002  # 추가 진입 스프레드 괴리 (0.2%)
    spread_take_profit: float = 0.002    # 익절 스프레드 (0.2%)
    stop_loss_percent: float = 10.0      # 절대 손절 (%)
    chart_time: int = 1                  # 분봉 (1분)
    candle_limit: int = 100
    use_limit_order: bool = True         # 지정가 주문 사용 (maker 수수료)
    limit_max_retries: int = 3           # 지정가 리트라이 횟수
    limit_ttl_ms: int = 3000             # 지정가 TTL (ms)
    # 코렐레이션 레짐 필터
    min_correlation: float = 0.7         # 최소 상관계수 (< 이면 진입 스킵)
    correlation_period: int = 24         # 상관계수 계산 기간 (가격 쌍의 개수)
    # 방향 비대칭
    coin2_long_entry_bonus: float = 0.0   # ETH LONG 진입 보너스 (임계값 0.15 감소)
    # 모멘텀 이탈 손실 캡: PnL이 이 값 이하면 모멘텀 안 봐도 즉시 청산
    momentum_loss_cap: float = 0.8        # % (0.8 = -0.8%에서 강제 청산)
    # 그룹 분산: 진입 전 대기 시간 (초)
    entry_delay: float = 0.0              # 그룹별 시차 (0/30/60/90초)
    # 그룹 라벨 (로그용)
    group: str = ""
    # 스프레드 기반 진입 (모멘텀 대체)
    spread_entry_enabled: bool = True     # 스프레드 이탈 진입 활성화
    spread_ma_period: int = 60            # 이동평균 기간 (스냅샷 수)
    spread_zscore_entry: float = 1.5      # Z-score 진입 임계값 (1.5 = 1.5 표준편차)
    # 트레일링 스탑 (마진 기준 %)
    trailing_activation: float = 0.5      # 2026-04-22 Plan B: 0.5%부터 trailing ON (빠른 이익 확정)
    trailing_callback: float = 0.3        # 2026-04-22 Plan B: 0.3% 되돌리면 청산 (타이트)
    trailing_tighten_above: float = 3.0   # 고점이 이 값 이상이면 callback 축소
    trailing_tighten_callback: float = 0.5  # 축소된 callback
    # 진입 차단 시간 (UTC)
    no_entry_hours: list = None
    # 시간대별 margin multiplier (UTC hour → multiplier). 2026-04-18 v3 추가
    hourly_margin_multipliers: dict = None
    # TGE 임박 거래소 scheduled margin boost (set via multi_runner에서 날짜 확인 후)
    scheduled_boost_multiplier: float = 1.0
    # 거래소별 equity 오버라이드 (0이면 자동)
    manual_equity_override: float = 0.0
    # 2026-04-18 v3: 포트폴리오 레벨 리스크 캡
    portfolio_max_margin_ratio: float = 0.4  # 전체 자본 대비 최대 오픈 마진 비율
    portfolio_max_positions: int = 10        # 동시 오픈 포지션 수 상한
    portfolio_daily_stop_usd: float = -50.0  # 일일 PnL -$50 도달 시 신규 진입 차단
    portfolio_total_capital: float = 2500.0  # 리스크 계산용 기준 자본
    # 2026-04-20: Venue aggregate cap — 같은 real_exchange 동일 방향 동시 포지션 제한
    max_same_venue_direction: int = 3
    # 2026-04-24: Regime gate (Minara 236→21 survivors 원리) — Kaufman ER + ADX + RSI 3중 필터
    regime_filter_enabled: bool = True
    regime_filter_shadow_only: bool = True   # 초기 shadow: 로그만 찍고 실제 진입은 허용
    regime_er_min: float = 0.30              # Kaufman Efficiency Ratio 하한
    regime_adx_min: float = 20.0             # ADX(14) 하한
    regime_rsi_min: float = 40.0             # RSI(14) 하한
    regime_rsi_max: float = 80.0             # RSI(14) 상한



@dataclass
class ScalperPosition:
    direction: str = ""      # "btc_long" or "eth_long"
    entry_count: int = 0
    total_margin: float = 0.0
    entry_type: str = ""     # "momentum" or "spread" — 청산 로직 분기용
    btc_size: float = 0.0
    eth_size: float = 0.0
    btc_entry_avg: float = 0.0
    eth_entry_avg: float = 0.0
    last_spread: float = 0.0  # 직전 진입 시 spread ratio
    entry_time: float = 0.0
    peak_pnl: float = 0.0        # 진입 후 최고 PnL% (trailing용)
    trailing_active: bool = False


def _calc_momentum(candles: list[dict], period: int = 14) -> float:
    """모멘텀 계산: 최근 period 봉 가격 변화율 (%)"""
    if len(candles) < period + 1:
        return 0.0
    closes = [float(c.get("close", c.get("c", 0))) for c in candles]
    if closes[-period - 1] == 0:
        return 0.0
    return (closes[-1] / closes[-period - 1] - 1) * 100


class NadoPairScalper:
    """BTC/ETH 모멘텀 페어 스캘핑 엔진"""

    def __init__(
        self,
        exchange_wrapper,
        candle_fetcher: CandleFetcher,
        config: NadoPairConfig,
        exchange_name: str = "",
    ):
        self.wrapper = exchange_wrapper
        self.candle_fetcher = candle_fetcher
        self.config = config
        self.exchange_name = exchange_name
        self.running = False

        self.pos = ScalperPosition()
        self.tag = exchange_name.upper()[:5].ljust(5)
        self._last_pair_error_time = 0.0  # pair_direction_error 쿨다운

        self._taker_fills = 0
        self._maker_fills = 0
        self.trade_logger = None
        self._state_file = Path(__file__).resolve().parent.parent / f"nado_scalper_state_{exchange_name}.json"
        self._last_position_alert_pnl: float = 0.0  # 포지션 -3% 알림 쓰로틀
        self._last_stop_loss_time: float = 0.0  # 손절 쿨다운
        self._last_close_time: float = 0.0  # 청산(익절/손절 무관) 후 일반 쿨다운
        self._current_trade_id: "int | None" = None  # DB 로깅용 현재 trade_id
        # 청산 모멘텀 임계값: 0이면 진입과 동일, 설정하면 느슨하게 (포지션 숨 쉴 공간)
        self._exit_threshold = config.exit_momentum_threshold or config.momentum_threshold
        # 코렐레이션 레짐: 최근 가격 쌍 버퍼
        self._price_history = []  # list of (coin1_price, coin2_price) tuples

    def set_logger(self, trade_logger):
        self.trade_logger = trade_logger

    # ── 모멘텀 & 스프레드 ──

    async def _get_momentum(self, coin: str) -> float:
        candles = await self.candle_fetcher.get_candles(
            coin, interval=self.config.chart_time, limit=self.config.candle_limit
        )
        if not candles or len(candles) < self.config.momentum_period + 1:
            return 0.0
        return _calc_momentum(candles, self.config.momentum_period)

    def _calc_spread(self, btc_price: float, eth_price: float) -> float:
        """BTC/ETH 가격 비율 (스프레드)"""
        if eth_price == 0:
            return 0.0
        return btc_price / eth_price

    def _update_spread_history(self, spread: float):
        """스프레드 히스토리 업데이트"""
        if not hasattr(self, '_spread_history'):
            self._spread_history = []
        self._spread_history.append(spread)
        max_len = self.config.spread_ma_period * 2
        if len(self._spread_history) > max_len:
            self._spread_history = self._spread_history[-max_len:]

    def _calc_spread_zscore(self) -> tuple[float, float, float]:
        """
        스프레드 Z-score 계산.
        Returns: (zscore, spread_ma, spread_std)
        Z > 0: 스프레드가 평균보다 높음 (BTC 상대적 고평가 → ETH Long 유리)
        Z < 0: 스프레드가 평균보다 낮음 (ETH 상대적 고평가 → BTC Long 유리)
        """
        if not hasattr(self, '_spread_history'):
            return 0.0, 0.0, 0.0
        period = self.config.spread_ma_period
        if len(self._spread_history) < period:
            return 0.0, 0.0, 0.0

        recent = self._spread_history[-period:]
        ma = sum(recent) / len(recent)
        std = (sum((x - ma) ** 2 for x in recent) / len(recent)) ** 0.5
        if std == 0:
            return 0.0, ma, 0.0

        current = self._spread_history[-1]
        zscore = (current - ma) / std
        return zscore, ma, std

    # 2026-04-24: Regime gate (Minara 236→21 survivors) —
    # Kaufman Efficiency Ratio(20) > 0.30 AND ADX(14) > 20 AND 40 < RSI(14) < 80
    # 모두 만족해야 True. 3개 중 1개라도 실패하면 False + 실패 사유 반환.
    async def _passes_regime_filter(self, coin: str) -> tuple[bool, str]:
        cfg = self.config
        if not cfg.regime_filter_enabled:
            return True, "disabled"
        try:
            candles = await self.candle_fetcher.get_candles(
                coin, interval=cfg.chart_time, limit=max(cfg.candle_limit, 60)
            )
        except Exception as e:
            return True, f"candle_err:{type(e).__name__}"  # fail-open on data error
        if not candles or len(candles) < 30:
            return True, f"insufficient_candles:{len(candles) if candles else 0}"

        closes = [float(c.get("close", c.get("c", 0))) for c in candles]
        highs = [float(c.get("high", c.get("h", 0))) for c in candles]
        lows = [float(c.get("low", c.get("l", 0))) for c in candles]

        # 1) Kaufman Efficiency Ratio over 20 bars
        er_period = 20
        if len(closes) < er_period + 1:
            return True, "er_insufficient"
        direction = abs(closes[-1] - closes[-er_period - 1])
        volatility = sum(abs(closes[i] - closes[i - 1]) for i in range(-er_period, 0))
        er = direction / volatility if volatility > 0 else 0.0

        # 2) ADX(14) — Wilder's smoothed DI+ / DI-
        adx_period = 14
        if len(closes) < adx_period * 2 + 1:
            return True, "adx_insufficient"
        try:
            plus_dm, minus_dm, trs = [], [], []
            for i in range(1, len(closes)):
                up_move = highs[i] - highs[i - 1]
                dn_move = lows[i - 1] - lows[i]
                plus_dm.append(up_move if (up_move > dn_move and up_move > 0) else 0.0)
                minus_dm.append(dn_move if (dn_move > up_move and dn_move > 0) else 0.0)
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                trs.append(tr)
            # Wilder smoothing (simple average of last period as proxy — conservative, avoids state)
            tr14 = sum(trs[-adx_period:]) / adx_period
            plus_di = (sum(plus_dm[-adx_period:]) / adx_period / tr14 * 100) if tr14 > 0 else 0.0
            minus_di = (sum(minus_dm[-adx_period:]) / adx_period / tr14 * 100) if tr14 > 0 else 0.0
            dx_denom = plus_di + minus_di
            if dx_denom == 0:
                adx = 0.0
            else:
                # rolling DX over last period
                dx_series = []
                for w_end in range(len(closes) - adx_period, len(closes)):
                    if w_end < adx_period:
                        continue
                    window_trs = trs[w_end - adx_period:w_end]
                    window_plus = plus_dm[w_end - adx_period:w_end]
                    window_minus = minus_dm[w_end - adx_period:w_end]
                    wtr = sum(window_trs) / adx_period if window_trs else 0.0
                    if wtr == 0:
                        continue
                    pdi = sum(window_plus) / adx_period / wtr * 100
                    mdi = sum(window_minus) / adx_period / wtr * 100
                    denom = pdi + mdi
                    if denom > 0:
                        dx_series.append(abs(pdi - mdi) / denom * 100)
                adx = sum(dx_series) / len(dx_series) if dx_series else 0.0
        except Exception:
            adx = 0.0

        # 3) RSI(14) bounded
        try:
            rsi = calculate_rsi(closes, period=14)
        except Exception:
            rsi = 50.0

        er_ok = er > cfg.regime_er_min
        adx_ok = adx > cfg.regime_adx_min
        rsi_ok = cfg.regime_rsi_min < rsi < cfg.regime_rsi_max
        passed = er_ok and adx_ok and rsi_ok
        detail = f"ER={er:.2f}({'ok' if er_ok else 'X'}) ADX={adx:.1f}({'ok' if adx_ok else 'X'}) RSI={rsi:.1f}({'ok' if rsi_ok else 'X'})"
        return passed, detail

    def _check_correlation(self) -> float:
        """
        Pearson 상관계수 계산 (최근 N 쌍의 가격 데이터).
        버퍼가 충분하지 않으면 1.0 반환 (필터 통과).
        """
        if len(self._price_history) < self.config.correlation_period:
            return 1.0

        # 최근 N 개의 가격 쌍 추출
        recent = self._price_history[-self.config.correlation_period:]
        coin1_prices = [p[0] for p in recent]
        coin2_prices = [p[1] for p in recent]

        # 단순 Pearson 상관계수 계산
        try:
            n = len(coin1_prices)
            if n < 2:
                return 1.0

            mean1 = sum(coin1_prices) / n
            mean2 = sum(coin2_prices) / n

            numerator = sum((coin1_prices[i] - mean1) * (coin2_prices[i] - mean2) for i in range(n))
            denom1 = sum((x - mean1) ** 2 for x in coin1_prices) ** 0.5
            denom2 = sum((x - mean2) ** 2 for x in coin2_prices) ** 0.5

            if denom1 == 0 or denom2 == 0:
                return 1.0

            correlation = numerator / (denom1 * denom2)
            return max(-1.0, min(1.0, correlation))  # Clamp to [-1, 1]
        except Exception as e:
            logger.debug(f"  {self.tag} │ 상관계수 계산 실패: {e}")
            return 1.0

    def _calc_pnl_percent(self, btc_price: float, eth_price: float) -> float:
        """합산 수익률 (%) = (LONG PnL + SHORT PnL) / 총 마진 × 100"""
        if not self.pos.direction or self.pos.total_margin == 0:
            return 0.0

        if self.pos.direction == "btc_long":
            # BTC LONG + ETH SHORT
            btc_pnl = (btc_price - self.pos.btc_entry_avg) * self.pos.btc_size
            eth_pnl = (self.pos.eth_entry_avg - eth_price) * self.pos.eth_size
        else:
            # ETH LONG + BTC SHORT
            eth_pnl = (eth_price - self.pos.eth_entry_avg) * self.pos.eth_size
            btc_pnl = (self.pos.btc_entry_avg - btc_price) * self.pos.btc_size

        total_pnl = btc_pnl + eth_pnl
        return (total_pnl / self.pos.total_margin) * 100

    # ── 지정가 주문 헬퍼 ──

    async def _get_bbo(self, symbol: str, side: str) -> Optional[float]:
        """오더북에서 BBO 가격. buy→best_bid+, sell→best_ask-"""
        try:
            if hasattr(self.wrapper, "get_orderbook"):
                book = await self.wrapper.get_orderbook(symbol)
                if book:
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    if bids and asks:
                        best_bid = float(bids[0][0])
                        best_ask = float(asks[0][0])
                        mid = (best_bid + best_ask) / 2
                        spread_pct = (best_ask - best_bid) / mid if mid else 0
                        # 스프레드의 30% 안쪽에 배치 (maker 체결 극대화)
                        offset = min(spread_pct * 0.3, 0.0003)  # 최대 0.03%
                        offset = max(offset, 0.00005)  # 최소 0.005%
                        if side == "buy":
                            return best_bid * (1 + offset)
                        else:
                            return best_ask * (1 - offset)
        except Exception as e:
            logger.debug(f"  {self.tag} │ {symbol} BBO 조회 실패: {e}")
        # 폴백: mark price
        try:
            mark = await self.wrapper.get_mark_price(symbol)
            if mark:
                return mark * 1.0001 if side == "buy" else mark * 0.9999
        except Exception:
            pass
        return None

    async def _limit_order_single(
        self, symbol: str, side: str, size: float
    ) -> tuple:
        """
        단일 코인 지정가 주문. (filled, fill_price) 반환.
        실패 시 시장가 폴백.
        """
        cfg = self.config
        if not cfg.use_limit_order:
            await self.wrapper.create_order(symbol, side, size, order_type="market")
            self._taker_fills += 1
            price = await self.wrapper.get_mark_price(symbol)
            return (True, price)

        ttl = cfg.limit_ttl_ms / 1000.0
        start = time.time()

        for attempt in range(cfg.limit_max_retries):
            if time.time() - start > ttl:
                break

            bbo = await self._get_bbo(symbol, side)
            if not bbo:
                break

            # 기존 주문 취소 (재시도 시)
            if attempt > 0:
                try:
                    await self.wrapper.cancel_orders(symbol)
                except Exception:
                    pass

            try:
                await self.wrapper.create_order(
                    symbol=symbol, side=side, amount=size,
                    price=bbo, order_type="limit", tif="Alo",
                )
            except Exception as e:
                err = str(e).lower()
                if "post only" in err or "would cross" in err:
                    await asyncio.sleep(0.1)
                    continue
                logger.debug(f"  {self.tag} │ {symbol} 지정가 실패: {e}")
                break

            # 체결 대기 (충분한 시간 확보)
            await asyncio.sleep(0.3)

            # 체결 확인
            try:
                orders = await self.wrapper.get_open_orders(symbol)
                if not orders:
                    pos = await self.wrapper.get_position(symbol)
                    if pos and float(pos.get("size", 0)) != 0:
                        self._maker_fills += 1
                        logger.info(f"  {self.tag} │ {symbol} maker 체결 @ {bbo:.2f}")
                        return (True, bbo)
                    # ALO 거부 (포지션 없음) → 재시도
                    logger.debug(f"  {self.tag} │ {symbol} ALO 거부 → 재시도 #{attempt+1}")
                    continue
                # 주문 아직 열려있음 → 다음 루프에서 리프라이스
                logger.debug(f"  {self.tag} │ {symbol} 미체결 대기 #{attempt+1}")
            except Exception as e:
                logger.debug(f"  {self.tag} │ {symbol} 체결 확인 실패: {e}")

        # 지정가 실패 → 잔여 주문 취소 후 시장가 폴백
        try:
            await self.wrapper.cancel_orders(symbol)
        except Exception:
            pass

        logger.info(f"  {self.tag} │ {symbol} 지정가 실패 → 시장가 폴백")
        await self.wrapper.create_order(symbol, side, size, order_type="market")
        self._taker_fills += 1
        price = await self.wrapper.get_mark_price(symbol)
        return (True, price)

    # ── 진입 ──

    async def _enter(self, direction: str, btc_price: float, eth_price: float):
        cfg = self.config

        # 2026-04-18 v3: 포트폴리오 레벨 리스크 체크 (첫 진입만)
        if self.pos.entry_count == 0:
            # 0. Stop-loss Circuit Breaker (3분 내 5건+ → 2h 중단)
            cb_active, cb_remaining = is_circuit_breaker_active()
            if cb_active:
                if int(cb_remaining) % 300 == 0:  # 5분마다 한번만 로그
                    logger.warning(f"  {self.tag} │ Portfolio Circuit Breaker {cb_remaining/60:.0f}분 남음 → 진입 차단")
                return
            # 1. 일일 circuit breaker
            daily_pnl = get_portfolio_daily_pnl()
            if daily_pnl <= cfg.portfolio_daily_stop_usd:
                logger.warning(f"  {self.tag} │ 일일 PnL ${daily_pnl:.2f} ≤ ${cfg.portfolio_daily_stop_usd} → 신규 진입 차단")
                return
            # 2. 동시 포지션 수
            pos_count = get_portfolio_position_count()
            if pos_count >= cfg.portfolio_max_positions:
                logger.info(f"  {self.tag} │ 동시 포지션 {pos_count}/{cfg.portfolio_max_positions} → 진입 스킵")
                return
            # 3. 총 오픈 마진
            total_margin = get_portfolio_total_margin()
            margin_cap = cfg.portfolio_total_capital * cfg.portfolio_max_margin_ratio
            if total_margin + cfg.margin_per_entry > margin_cap:
                logger.info(f"  {self.tag} │ 포트폴리오 마진 ${total_margin:.0f}+${cfg.margin_per_entry:.0f} > ${margin_cap:.0f} → 스킵")
                return
            # 4. Correlation trend anomaly 감지 (글로벌)
            cur_c, avg_10m, delta = correlation_trend()
            if avg_10m is not None and avg_10m < 0.75 and delta is not None and delta < -0.1:
                logger.warning(
                    f"  {self.tag} │ corr 하향 추세 (10m avg={avg_10m:.2f}, 1h delta={delta:+.2f}) → 진입 중단"
                )
                return
            # 5. Venue aggregate cap — 같은 real_exchange 동일 방향 동시 포지션 제한
            if cfg.max_same_venue_direction > 0:
                rv = _real_venue_of(self.exchange_name)
                vc = get_venue_direction_count(rv, direction)
                if vc >= cfg.max_same_venue_direction:
                    logger.info(
                        f"  {self.tag} │ venue[{rv}] {direction} 포화 "
                        f"{vc}/{cfg.max_same_venue_direction} → 진입 스킵"
                    )
                    return

        # 손절 쿨다운: 손절 후 1시간 동안 신규 진입 차단 (DCA 추가 진입은 허용)
        if self.pos.entry_count == 0 and self._last_stop_loss_time > 0:
            cooldown_elapsed = time.time() - self._last_stop_loss_time
            if cooldown_elapsed < 3600:
                remaining = int((3600 - cooldown_elapsed) / 60)
                logger.info(f"  {self.tag} │ 손절 쿨다운 {remaining}분 남음 → 진입 스킵")
                return

        # 일반 청산 후 쿨다운 (DCA 철회 시 필수 — 연쇄 재진입 방지)
        if self.pos.entry_count == 0 and cfg.post_close_cooldown_sec > 0 and self._last_close_time > 0:
            elapsed = time.time() - self._last_close_time
            if elapsed < cfg.post_close_cooldown_sec:
                remaining = int(cfg.post_close_cooldown_sec - elapsed)
                logger.debug(f"  {self.tag} │ 청산 후 쿨다운 {remaining}초 남음")
                return

        # 그룹 시차 대기: entry_delay초 후 가격 재조회하여 시그널 재검증
        if cfg.entry_delay > 0 and self.pos.entry_count == 0:
            await asyncio.sleep(cfg.entry_delay)
            # 가격 재조회
            try:
                btc_price = await asyncio.wait_for(self.wrapper.get_mark_price(cfg.coin1), timeout=15)
                eth_price = await asyncio.wait_for(self.wrapper.get_mark_price(cfg.coin2), timeout=15)
            except asyncio.TimeoutError:
                logger.warning(f"  {self.tag} │ 시차 대기 후 가격 타임아웃 → 진입 스킵")
                return
            # 스프레드 진입은 모멘텀 재확인 불필요 (z-score 기반이므로)
            if self.pos.entry_type != "spread":
                btc_mom = await self._get_momentum(cfg.coin1)
                eth_mom = await self._get_momentum(cfg.coin2)
                threshold = cfg.momentum_threshold
                if direction == "btc_long" and btc_mom < threshold:
                    logger.info(f"  {self.tag} │ 시차 대기 후 BTC 모멘텀 소멸 ({btc_mom:.2f} < {threshold}) → 스킵")
                    return
                if direction == "eth_long" and eth_mom < threshold * (1 - cfg.coin2_long_entry_bonus):
                    logger.info(f"  {self.tag} │ 시차 대기 후 ETH 모멘텀 소멸 ({eth_mom:.2f}) → 스킵")
                    return
            else:
                # 스프레드 진입: delay 후 z-score 재확인
                spread = self._calc_spread(btc_price, eth_price)
                self._update_spread_history(spread)
                zscore, _, _ = self._calc_spread_zscore()
                if abs(zscore) < cfg.spread_zscore_entry:
                    logger.info(f"  {self.tag} │ 시차 대기 후 z-score 소멸 ({zscore:+.2f} < {cfg.spread_zscore_entry}) → 스킵")
                    return

        # 2026-04-18 v3: 시간대별 적응형 margin multiplier (hourly_margin_multipliers 설정 기준)
        # 데이터: 05~09h UTC WR 100%, 11h UTC WR 32% (차단). 기본 1.0x
        import datetime as _dt
        hour_mult = 1.0
        hourly_mults = getattr(cfg, 'hourly_margin_multipliers', None) or {}
        if hourly_mults:
            cur_hour_utc = _dt.datetime.now(_dt.timezone.utc).hour
            hour_mult = float(hourly_mults.get(cur_hour_utc, 1.0))
            if hour_mult != 1.0:
                logger.debug(f"  {self.tag} │ hour={cur_hour_utc}h UTC margin ×{hour_mult}")
        # 2026-04-18 v3: 변동성 regime multiplier
        vol_mult = volatility_margin_multiplier()
        if vol_mult == 0:
            _, regime = current_volatility_regime()
            logger.warning(f"  {self.tag} │ 변동성 {regime} → 진입 중단")
            return
        effective_margin = cfg.margin_per_entry * hour_mult * cfg.scheduled_boost_multiplier * vol_mult
        notional_per_coin = (effective_margin / 2) * cfg.leverage  # USDT notional 2coin

        btc_size = int(notional_per_coin / btc_price * 1_000_000) / 1_000_000  # 6자리
        eth_size = int(notional_per_coin / eth_price * 10_000) / 10_000        # 4자리 (lotSize 0.0001 대응)

        if direction == "btc_long":
            btc_side, eth_side = "buy", "sell"
        else:
            btc_side, eth_side = "sell", "buy"

        entry_num = self.pos.entry_count + 1
        try:
            logger.info(f"  {self.tag} │ 진입 시도 #{entry_num} | {cfg.coin1} {btc_side} {btc_size:.6f}")
            fill1, p1 = await self._limit_order_single(cfg.coin1, btc_side, btc_size)
            if not fill1:
                logger.error(f"  {self.tag} │ {cfg.coin1} 진입 실패")
                return

            try:
                fill2, p2 = await self._limit_order_single(cfg.coin2, eth_side, eth_size)
                if not fill2:
                    raise Exception(f"{cfg.coin2} 체결 실패")
            except Exception as e2:
                # coin1 체결, coin2 실패 → orphan 방지: coin1 즉시 청산
                logger.error(f"  {self.tag} │ {cfg.coin2} 진입 실패: {e2} → {cfg.coin1} 청산 시도")
                try:
                    reverse_side = "sell" if btc_side == "buy" else "buy"
                    await self.wrapper.create_order(
                        symbol=cfg.coin1, side=reverse_side, amount=btc_size, order_type="market"
                    )
                    logger.info(f"  {self.tag} │ {cfg.coin1} orphan 청산 완료")
                except Exception as ce:
                    logger.error(f"  {self.tag} │ {cfg.coin1} orphan 청산 실패: {ce}")
                return
            # 체결 가격 업데이트
            btc_price = p1 or btc_price
            eth_price = p2 or eth_price

            # 평균가 업데이트
            old_btc_notional = self.pos.btc_entry_avg * self.pos.btc_size
            old_eth_notional = self.pos.eth_entry_avg * self.pos.eth_size
            self.pos.btc_size += btc_size
            self.pos.eth_size += eth_size
            self.pos.btc_entry_avg = (old_btc_notional + btc_price * btc_size) / self.pos.btc_size if self.pos.btc_size else btc_price
            self.pos.eth_entry_avg = (old_eth_notional + eth_price * eth_size) / self.pos.eth_size if self.pos.eth_size else eth_price
            self.pos.direction = direction
            self.pos.entry_count = entry_num
            self.pos.total_margin += cfg.margin_per_entry
            self.pos.last_spread = self._calc_spread(btc_price, eth_price)
            if entry_num == 1:
                self.pos.entry_time = time.time()
                # entry_type은 메인 루프에서 설정 (momentum or spread)

            dir_str = "BTC↑ ETH↓" if direction == "btc_long" else "ETH↑ BTC↓"
            logger.info(
                f"  {self.tag} │ 진입 #{entry_num}/{cfg.max_entries} {dir_str} | "
                f"BTC={btc_price:,.1f} ETH={eth_price:,.1f} "
                f"margin=${self.pos.total_margin:.0f}"
            )

            # 2026-04-18 v3: 포트폴리오 레벨 등록
            register_portfolio_position(self.exchange_name, self.pos.total_margin, self.pos.entry_time)
            # 2026-04-20: Venue 레벨 등록 (첫 진입만)
            if entry_num == 1:
                try:
                    register_venue_position(_real_venue_of(self.exchange_name), direction)
                except Exception:
                    pass

            self._save_state()

            if self.trade_logger:
                try:
                    if entry_num == 1:
                        # 첫 진입: open_trade로 trade_id 발급
                        self._current_trade_id = self.trade_logger.open_trade(
                            exchange=self.exchange_name,
                            direction=direction,
                            coin1=cfg.coin1,
                            coin2=cfg.coin2,
                        )
                    if self._current_trade_id:
                        self.trade_logger.log_entry(
                            trade_id=self._current_trade_id,
                            entry_number=entry_num,
                            price_coin1=btc_price,
                            price_coin2=eth_price,
                            margin=cfg.margin_per_entry,
                        )
                except Exception as le:
                    logger.debug(f"  {self.tag} │ trade_logger.log_entry 실패 (무시): {le}")

        except Exception as e:
            logger.error(f"  {self.tag} │ 진입 실패 #{entry_num}: {e}")

    # ── 청산 ──

    async def _close_all(self, reason: str, btc_price: float, eth_price: float):
        if not self.pos.direction:
            return

        pnl_pct = self._calc_pnl_percent(btc_price, eth_price)
        cfg = self.config

        try:
            # 거래소 실제 포지션 사이즈 기반 청산 (시장가로 확실히 체결)
            for coin in [cfg.coin1, cfg.coin2]:
                try:
                    pos = await self.wrapper.get_position(coin)
                    if pos and float(pos.get("size", 0)) > 0:
                        close_side = "sell" if pos["side"] == "long" else "buy"
                        actual_size = float(pos["size"])
                        try:
                            await self.wrapper.close_position(coin, pos)
                        except Exception:
                            await self.wrapper.create_order(
                                coin, close_side, actual_size, order_type="market"
                            )
                except Exception as e:
                    logger.error(f"  {self.tag} │ {coin} 청산 주문 실패: {e}")

            icon = "+" if pnl_pct >= 0 else "-"
            logger.info(
                f"  {self.tag} │ 청산 {icon}{abs(pnl_pct):.2f}% | {reason} | "
                f"entries={self.pos.entry_count} margin=${self.pos.total_margin:.0f}"
            )

            if self.trade_logger and self._current_trade_id:
                try:
                    self.trade_logger.close_trade(
                        trade_id=self._current_trade_id,
                        pnl_percent=pnl_pct,
                        pnl_usd=pnl_pct * self.pos.total_margin / 100,
                        reason=reason,
                    )
                except Exception as le:
                    logger.debug(f"  {self.tag} │ trade_logger.close_trade 실패 (무시): {le}")
                finally:
                    self._current_trade_id = None

        except Exception as e:
            logger.error(f"  {self.tag} │ 청산 실패: {e}")

        # 2026-04-18 v3: 포트폴리오 레벨 업데이트
        unregister_portfolio_position(self.exchange_name)
        # 2026-04-20: Venue 레벨 해제
        try:
            unregister_venue_position(_real_venue_of(self.exchange_name), self.pos.direction)
        except Exception:
            pass
        try:
            pnl_usd = pnl_pct * self.pos.total_margin / 100
            update_portfolio_pnl(pnl_usd)
            # 2026-04-18 v4: Circuit Breaker - stop_loss 이벤트 기록
            if ("손절" in reason or "correlation" in reason.lower()) and pnl_usd < 0:
                if record_stop_loss(self.exchange_name, pnl_usd):
                    # Breaker 트리거됨 → Telegram 알림
                    try:
                        from . import notifier as _notifier
                        recent_count = len([e for e in _STOP_LOSS_EVENTS if time.time() - e[0] <= _CB_WINDOW_SEC])
                        asyncio.create_task(_notifier.notify(
                            f"<b>🚨 Portfolio Circuit Breaker</b>\n"
                            f"  {_CB_WINDOW_SEC//60}분 내 stop_loss {recent_count}건 발생\n"
                            f"  → 신규 진입 {_CB_PAUSE_SEC//3600}시간 중단\n"
                            f"  마지막 트리거: {self.tag} ${pnl_usd:+.2f}",
                            dedup_key="portfolio_circuit_breaker",
                            dedup_seconds=3600,
                        ))
                    except Exception:
                        pass
        except Exception:
            pass

        self.pos = ScalperPosition()
        self._last_close_time = time.time()  # 쿨다운 기산점
        self._save_state()

    # ── 메인 루프 ──

    async def run(self, saved_state: dict = None):
        self.running = True
        if saved_state:
            self.restore_state(saved_state)
        else:
            self._load_state()

        cfg = self.config

        # 레버리지 설정
        for coin in [cfg.coin1, cfg.coin2]:
            try:
                await self.wrapper.update_leverage(coin, cfg.leverage, "cross")
            except Exception as e:
                logger.debug(f"  {self.tag} │ {coin} 레버리지 설정 실패: {e}")

        # 시작 시 고아 포지션 체크: 봇 상태 없는데 거래소에 포지션 있으면 청산
        if not self.pos.direction:
            try:
                orphan_found = False
                for coin in [cfg.coin1, cfg.coin2]:
                    try:
                        pos = await asyncio.wait_for(
                            self.wrapper.get_position(coin), timeout=15.0
                        )
                    except (asyncio.TimeoutError, Exception) as pe:
                        logger.debug(f"  {self.tag} │ {coin} 고아 체크 스킵: {pe}")
                        continue
                    if pos and float(pos.get("size", 0)) > 0:
                        orphan_found = True
                        close_side = "sell" if pos["side"] == "long" else "buy"
                        logger.warning(
                            f"  {self.tag} │ 고아 포지션 발견: {coin} {pos['side']} {pos['size']} → 청산"
                        )
                        try:
                            await asyncio.wait_for(
                                self.wrapper.close_position(coin, pos), timeout=15.0
                            )
                        except Exception:
                            try:
                                await asyncio.wait_for(
                                    self.wrapper.create_order(coin, close_side, float(pos["size"]), order_type="market"),
                                    timeout=15.0,
                                )
                            except Exception as oe:
                                logger.error(f"  {self.tag} │ {coin} 고아 청산 실패: {oe}")
                if orphan_found:
                    logger.info(f"  {self.tag} │ 고아 포지션 정리 완료")
            except Exception as e:
                logger.error(f"  {self.tag} │ 고아 포지션 체크 실패: {e}")

        # 오토스케일링: 잔고 기반 마진 조정
        try:
            # manual_equity 오버라이드 우선 적용 (scaling.manual_equity.{exchange_name})
            equity = 0
            if hasattr(cfg, 'manual_equity_override') and cfg.manual_equity_override > 0:
                equity = float(cfg.manual_equity_override)
                logger.info(f"  {self.tag} │ manual_equity 적용: ${equity:.0f}")
            else:
                collateral = await self.wrapper.get_collateral()
                if isinstance(collateral, dict):
                    equity = collateral.get("total_collateral", 0) or collateral.get("available_collateral", 0)
                    equity = float(equity or 0)
                    # 2026-04-18: HIP-3 Unified Account 대응 (HyENA 전용)
                    # HyENA는 USDC 대신 spot USDE를 perp 마진으로 사용
                    # 다른 거래소(hl_wallet_c 등)는 지갑 공유해도 spot USDE가 자기 collateral 아님
                    if equity < 1.0 and self.exchange_name in ("hyena", "hyena_2"):
                        spot = collateral.get("spot") or {}
                        if isinstance(spot, dict):
                            try:
                                usde = float(spot.get("USDE", 0) or 0)
                                if usde > 0:
                                    equity = usde
                                    logger.info(f"  {self.tag} │ spot USDE 사용: ${equity:.2f} (HIP-3)")
                            except Exception:
                                pass
                else:
                    equity = float(collateral or 0)
                # get_balance 폴백
                if not equity:
                    try:
                        bal = await self.wrapper.get_balance()
                        equity = float(bal) if bal else 0
                    except Exception:
                        pass
            if equity > 0:
                usable = equity * 0.8  # 잔고의 80% 사용
                scaled_total = min(usable, cfg.total_margin_limit)
                scaled_per_entry = max(5.0, scaled_total / cfg.max_entries)
                if scaled_per_entry < cfg.margin_per_entry:
                    logger.info(
                        f"  {self.tag} │ 스케일링: 잔고=${equity:.0f} → "
                        f"마진 ${cfg.margin_per_entry:.0f} → ${scaled_per_entry:.1f}/회 "
                        f"(총 ${scaled_per_entry * cfg.max_entries:.0f})"
                    )
                    cfg.margin_per_entry = scaled_per_entry
                    cfg.total_margin_limit = scaled_per_entry * cfg.max_entries
        except Exception as e:
            logger.warning(f"  {self.tag} │ 스케일링 실패: {e}")

        grp = f" G{cfg.group}" if cfg.group else ""
        delay = f" delay={cfg.entry_delay}s" if cfg.entry_delay > 0 else ""
        logger.info(
            f"  {self.tag} │ NadoPairScalper 시작{grp} | {cfg.coin1}/{cfg.coin2} "
            f"Lev={cfg.leverage}x Margin={cfg.margin_per_entry:.1f}$ Mom={cfg.momentum_threshold:.2f}"
            f"{delay} SL={cfg.stop_loss_percent}%"
        )

        if self.pos.direction:
            logger.info(
                f"  {self.tag} │ 기존 포지션 복구: {self.pos.direction} "
                f"x{self.pos.entry_count} margin=${self.pos.total_margin:.0f}"
            )

        while self.running:
            try:
                # 2026-04-22 Claude 데이터 감사: CB (systemic) 강제 청산 비활성화
                # 근거: 56건 WR 1.8%, 누적 -$17.85. 발동=거의 확실한 손실 = 제거.
                # 신규 진입 차단은 유지 (is_circuit_breaker_active 체크 다른 곳에서).
                cb_active, cb_remain = is_circuit_breaker_active()
                if cb_active and self.pos.direction:
                    if int(cb_remain) % 600 == 0:
                        logger.info(f"  {self.tag} │ CB 활성 중 ({cb_remain/60:.0f}분) — 포지션 유지 (강제청산 비활성)")
                    # 기존 포지션 그대로 유지, 다음 로직으로 진행

                # 0. 시간대 기반 margin 조정
                import datetime as _dt
                now_utc = _dt.datetime.now(_dt.timezone.utc)
                utc_hour = now_utc.hour
                kst_hour = (utc_hour + 9) % 24
                is_weekend = now_utc.weekday() >= 5  # 토(5), 일(6)

                # 적자 시간대 — 4/17 분석 결과 기반 (KST 00, 01, 22시 등 -100% 이상)
                bad_kst_hours = {0, 1, 22, 7, 4}
                if kst_hour in bad_kst_hours:
                    logger.debug(f"  {self.tag} │ {kst_hour}시 KST 적자 시간대 — 진입 스킵")
                    await asyncio.sleep(cfg.scan_interval)
                    continue

                # margin 조정: 새벽 HL 장애 or 주말 유동성 부족
                margin_mult = 1.0
                if 0 <= utc_hour < 4:
                    margin_mult *= 0.5
                if is_weekend:
                    margin_mult *= 0.5
                effective_margin = cfg.margin_per_entry * margin_mult

                # 1. 가격 조회 (15초 타임아웃 — hotstuff 등 hang 방지)
                _px_t0 = time.time()
                try:
                    btc_price = await asyncio.wait_for(self.wrapper.get_mark_price(cfg.coin1), timeout=15)
                    eth_price = await asyncio.wait_for(self.wrapper.get_mark_price(cfg.coin2), timeout=15)
                    btc_price = float(btc_price) if btc_price else 0
                    eth_price = float(eth_price) if eth_price else 0
                except (asyncio.TimeoutError, ValueError, TypeError):
                    logger.warning(f"  {self.tag} │ 가격 조회 실패/타임아웃")
                    await asyncio.sleep(cfg.scan_interval)
                    continue
                if not btc_price or not eth_price:
                    await asyncio.sleep(cfg.scan_interval)
                    continue

                # 1.1 HIP-3 stale price gate: hyna:* 심볼은 WS 미지원 → REST 폴백 시
                # 응답이 오래 걸리고 가격이 오래된 값일 가능성. 신규 진입 시에만 차단.
                _px_age = time.time() - _px_t0
                _hip3_pair = _is_hip3_symbol(cfg.coin1) or _is_hip3_symbol(cfg.coin2)
                if _hip3_pair and _px_age > HIP3_STALE_THRESHOLD_S and not self.pos.direction:
                    logger.info(
                        f"  {self.tag} │ [HIP-3 stale] {cfg.coin1}/{cfg.coin2} age={_px_age:.1f}s skip"
                    )
                    await asyncio.sleep(cfg.scan_interval)
                    continue

                # 1.5 가격 히스토리 업데이트 (코렐레이션 계산용)
                self._price_history.append((btc_price, eth_price))
                # 최대 길이 유지
                if len(self._price_history) > cfg.correlation_period * 2:
                    self._price_history = self._price_history[-(cfg.correlation_period * 2):]

                # 2. 모멘텀 조회
                btc_mom = await self._get_momentum(cfg.coin1)
                eth_mom = await self._get_momentum(cfg.coin2)

                # 2.5 페어 방향 검증: 둘 다 같은 방향이면 이상 → 5초 후 재확인, 2회 연속이면 청산 (60초 쿨다운)
                if self.pos.direction and (time.time() - self._last_pair_error_time) > 60:
                    try:
                        p1 = await self.wrapper.get_position(cfg.coin1)
                        p2 = await self.wrapper.get_position(cfg.coin2)
                        s1 = p1.get("side") if p1 and float(p1.get("size", 0)) > 0 else None
                        s2 = p2.get("side") if p2 and float(p2.get("size", 0)) > 0 else None
                        if s1 and s2 and s1 == s2:
                            logger.warning(
                                f"  {self.tag} │ 페어 방향 이상 감지 {cfg.coin1}={s1} {cfg.coin2}={s2} → 5초 후 재확인"
                            )
                            await asyncio.sleep(5)
                            p1b = await self.wrapper.get_position(cfg.coin1)
                            p2b = await self.wrapper.get_position(cfg.coin2)
                            s1b = p1b.get("side") if p1b and float(p1b.get("size", 0)) > 0 else None
                            s2b = p2b.get("side") if p2b and float(p2b.get("size", 0)) > 0 else None
                            if s1b and s2b and s1b == s2b:
                                logger.error(
                                    f"  {self.tag} │ 페어 방향 오류 확정! {cfg.coin1}={s1b} {cfg.coin2}={s2b} → 전체 청산"
                                )
                                await self._close_all("pair_direction_error", btc_price, eth_price)
                                self._last_pair_error_time = time.time()
                                continue
                            else:
                                logger.info(
                                    f"  {self.tag} │ 페어 방향 재확인 OK (일시 WS 불일치) {cfg.coin1}={s1b} {cfg.coin2}={s2b}"
                                )
                    except Exception as e:
                        logger.debug(f"  {self.tag} │ 페어 검증 실패: {e}")

                # 3. 포지션 있으면 청산 체크
                if self.pos.direction:
                    pnl_pct = self._calc_pnl_percent(btc_price, eth_price)

                    # 3-0. 포지션 보유 시간 하드리밋 (scalping)
                    # 2026-04-18: 조건부 청산 로직 — 수익 중이면 spread 수렴까지 시간 허용
                    # 30분: PnL > +0.3%면 60분까지 연장, 음수/미미면 즉시 청산
                    # 60분: 무조건 청산 (하드 리밋)
                    if self.pos.entry_time > 0:
                        age_min = (time.time() - self.pos.entry_time) / 60
                        should_close_by_time = False
                        if age_min > 90:
                            should_close_by_time = True  # 2026-04-20: 60→90 하드캡 (>120m WR 0% 방어)
                        elif age_min > 45 and pnl_pct <= 0.2:
                            # 2026-04-20: 30→45 연장. 데이터 분석 결과 30-45m 구간 spread_cv 알파 유지
                            # (30분+ spread_cv avg +0.45). PnL ≤0.2% 면 망한 포지션이니 청산
                            should_close_by_time = True
                        if should_close_by_time:
                            logger.warning(
                                f"  {self.tag} │ 포지션 {age_min:.0f}분+ 강제 청산 (PnL={pnl_pct:+.2f}%)"
                            )
                            try:
                                from . import notifier as _notifier
                                asyncio.create_task(_notifier.notify(
                                    f"<b>⏱ 포지션 시간초과 청산</b> {self.tag}\n"
                                    f"  {self.pos.direction} age {age_min:.0f}분 PnL {pnl_pct:+.2f}%",
                                    dedup_key=f"pos_timeout_{self.exchange_name}",
                                    dedup_seconds=300,
                                ))
                            except Exception:
                                pass
                            await self._close_all(f"시간초과 {age_min:.0f}분", btc_price, eth_price)
                            continue

                    # 3-1. 포지션 PnL -3% 하락 시 Telegram 알림 (1회만)
                    if pnl_pct <= -3.0 and self._last_position_alert_pnl > -3.0:
                        try:
                            from . import notifier as _notifier
                            asyncio.create_task(_notifier.notify(
                                f"<b>⚠ 포지션 경고</b> {self.tag}\n"
                                f"  {self.pos.direction} PnL <b>{pnl_pct:.2f}%</b> DCA={self.pos.entry_count}\n"
                                f"  margin=${self.pos.total_margin:.0f}",
                                dedup_key=f"pos_warn_{self.exchange_name}",
                                dedup_seconds=1800,
                            ))
                        except Exception:
                            pass
                    self._last_position_alert_pnl = pnl_pct

                    # 3-2. Correlation 붕괴 감지 (2026-04-18 v3)
                    # - 글로벌 dedup: 모든 거래소가 같은 BTC/ETH 시장 → 단일 알림
                    # - 자동 대피: corr < 0.3 AND PnL < 0 → 자동 청산 (페어 전제 완전 붕괴)
                    # - 수동 판단 기준 완화: corr < 0.4 (0.5 → 0.4, 경미한 이탈은 무시)
                    try:
                        cur_corr = self._check_correlation()
                        if cur_corr is not None:
                            # 심각 — 자동 청산 (T1.1b: 강화 — 2026-04-20)
                            # 트리거 조건 확대:
                            # (a) corr < 0.3 + 손실 (기존)
                            # (b) corr < 0.5 + PnL < -1% (새로 추가 — 중간 수준 붕괴 + 중간 손실)
                            # (c) 10분 내 corr 급락 > 0.25 + PnL < 0 (급락 감지)
                            _cur, _avg_10m, _delta = correlation_trend()
                            drop_recent = (_cur is not None and _avg_10m is not None
                                           and (_avg_10m - _cur) > 0.25)
                            # 2026-04-21 완화: corr_systemic 과민 반응 → 수렴 대기 못함
                            # 조건 대폭 엄격화: 정말 페어 붕괴 + 본격 손실 때만 작동
                            # 2026-04-22 Codex HIGH 수정: drop_recent 급락 방어 복원
                            systemic_trigger = (
                                (cur_corr < 0.2 and pnl_pct < -1.0)
                                or (cur_corr < 0.4 and pnl_pct < -3.0)
                                or (drop_recent and cur_corr < 0.5 and pnl_pct < -1.0)
                            )
                            if systemic_trigger:
                                trigger_desc = (
                                    "corr<0.3+loss" if cur_corr < 0.3
                                    else ("corr<0.5+loss>1%" if cur_corr < 0.5
                                          else "corr_rapid_drop")
                                )
                                logger.warning(f"  {self.tag} │ corr {cur_corr:.2f} PnL {pnl_pct:+.2f}% → mass-close ({trigger_desc})")
                                await self._close_all(
                                    f"corr_systemic {cur_corr:.2f} ({trigger_desc})",
                                    btc_price, eth_price,
                                )
                                continue
                            # 경고 — 글로벌 단일 알림
                            if cur_corr < 0.4:
                                from . import notifier as _notifier
                                asyncio.create_task(_notifier.notify(
                                    f"<b>⚠ BTC/ETH 상관관계 붕괴</b>\n"
                                    f"  corr = {cur_corr:.2f} (&lt;0.4)\n"
                                    f"  영향: {self.tag} PnL {pnl_pct:+.2f}% (외 다수 거래소)\n"
                                    f"  대응: corr&lt;0.3 + 손실 시 자동 청산됨",
                                    dedup_key="corr_global_alert",  # 글로벌 dedup
                                    dedup_seconds=3600,
                                ))
                    except Exception:
                        pass

                    # 6-0. 하드 손절 (최우선)
                    if pnl_pct <= -cfg.stop_loss_percent:
                        await self._close_all(f"손절 {pnl_pct:.2f}%", btc_price, eth_price)
                        self._last_stop_loss_time = time.time()  # 손절 쿨다운 시작
                    else:
                        # 6-1. 트레일링 스탑 업데이트
                        if pnl_pct > self.pos.peak_pnl:
                            self.pos.peak_pnl = pnl_pct
                        if not self.pos.trailing_active and pnl_pct >= cfg.trailing_activation:
                            self.pos.trailing_active = True
                            logger.info(
                                f"  {self.tag} │ 트레일링 활성 │ PnL +{pnl_pct:.2f}% (peak={self.pos.peak_pnl:.2f}%)"
                            )

                        trailing_hit = False
                        if self.pos.trailing_active:
                            cb = (cfg.trailing_tighten_callback
                                  if self.pos.peak_pnl >= cfg.trailing_tighten_above
                                  else cfg.trailing_callback)
                            drawdown = self.pos.peak_pnl - pnl_pct
                            if drawdown >= cb:
                                pnl_sign = "+" if pnl_pct >= 0 else ""
                                logger.info(
                                    f"  {self.tag} │ ★ 트레일링 {'익절' if pnl_pct >= 0 else '청산'} │ PnL {pnl_sign}{pnl_pct:.2f}% "
                                    f"(peak={self.pos.peak_pnl:.2f}% dd={drawdown:.2f}% cb={cb:.1f}%)"
                                )
                                await self._close_all(
                                    f"trailing_stop peak={self.pos.peak_pnl:.2f}% dd={drawdown:.2f}%",
                                    btc_price, eth_price
                                )
                                trailing_hit = True

                        if trailing_hit:
                            pass
                        # 6-2. 고정 TP (activation 미도달 시 안전망)
                        elif pnl_pct >= cfg.spread_take_profit * 100 and not self.pos.trailing_active:
                            await self._close_all(f"익절 {pnl_pct:.2f}%", btc_price, eth_price)
                        # 6-3. 손실 캡 (하드 스톱보다 느슨, 노이즈 이탈 방지)
                        elif pnl_pct <= -cfg.momentum_loss_cap:
                            await self._close_all(
                                f"손실캡 {pnl_pct:.2f}% (>{cfg.momentum_loss_cap}%)",
                                btc_price, eth_price
                            )
                        # 6-4. 모멘텀 이탈 (모멘텀 진입만)
                        elif self.pos.entry_type != "spread":
                            if self.pos.direction == "btc_long" and btc_mom < self._exit_threshold:
                                await self._close_all(f"BTC 모멘텀 이탈 ({btc_mom:.2f})", btc_price, eth_price)
                            elif self.pos.direction == "eth_long" and eth_mom < self._exit_threshold:
                                await self._close_all(f"ETH 모멘텀 이탈 ({eth_mom:.2f})", btc_price, eth_price)
                            elif self.pos.direction == "btc_long" and eth_mom >= cfg.momentum_threshold and btc_mom < self._exit_threshold:
                                await self._close_all(f"방향 전환 → ETH", btc_price, eth_price)
                            elif self.pos.direction == "eth_long" and btc_mom >= cfg.momentum_threshold and eth_mom < self._exit_threshold:
                                await self._close_all(f"방향 전환 → BTC", btc_price, eth_price)
                            # 추가 분할 진입 (모멘텀 진입 경로)
                            elif _SYNERGY and time.time() < _divergence_pause_until:
                                # basis_arb divergence 감지 중 → 신규 진입 차단
                                pass
                            elif self.pos.entry_count < cfg.max_entries:
                                current_spread = self._calc_spread(btc_price, eth_price)
                                spread_diff = abs(current_spread - self.pos.last_spread) / self.pos.last_spread if self.pos.last_spread else 0
                                if spread_diff >= cfg.spread_entry_threshold:
                                    btc_threshold = cfg.momentum_threshold
                                    eth_threshold = cfg.momentum_threshold * (1 - cfg.coin2_long_entry_bonus)
                                    if self.pos.direction == "btc_long" and btc_mom >= btc_threshold:
                                        await self._enter("btc_long", btc_price, eth_price)
                                    elif self.pos.direction == "eth_long" and eth_mom >= eth_threshold:
                                        await self._enter("eth_long", btc_price, eth_price)
                        # 6-5. 스프레드 수렴 청산 - GREEN EXIT (pair_pnl > 0 시에만 청산)
                        else:
                            spread = self._calc_spread(btc_price, eth_price)
                            self._update_spread_history(spread)
                            zscore, _, _ = self._calc_spread_zscore()
                            # 2026-04-22 Plan B: Green Exit
                            # - z수렴 + pair_pnl > 0 → 수익 확정 close
                            # - z수렴 + pnl < 0 → hold (회복 기대)
                            # - z확대 & pnl < -0.5% → Time-decay TP 폴백 청산
                            hold_minutes = (time.time() - self.pos.entry_time) / 60 if self.pos.entry_time else 0
                            time_decay_tp = max(0.1, 0.5 - hold_minutes * 0.007)  # 0~60분: 0.5→0.1%
                            if abs(zscore) < 0.3 and pnl_pct > 0:
                                await self._close_all(f"green_exit z={zscore:+.2f} pnl={pnl_pct:+.2f}%", btc_price, eth_price)
                            elif abs(zscore) < 0.3 and pnl_pct > -0.3 and hold_minutes > 30:
                                # 30분+ hold 시엔 소폭 손실도 수렴 닫기 (timeout 방지)
                                await self._close_all(f"spread_conv_timeout z={zscore:+.2f} pnl={pnl_pct:+.2f}%", btc_price, eth_price)
                            elif pnl_pct >= time_decay_tp:
                                # Time-decay TP: 오래 hold 할수록 낮은 TP
                                await self._close_all(f"time_decay_tp +{pnl_pct:.2f}% (after {hold_minutes:.0f}m)", btc_price, eth_price)

                    # 로그
                    if self.pos.direction:
                        logger.info(
                            f"  {self.tag} │ PnL {pnl_pct:+.2f}% | {self.pos.entry_count}/{cfg.max_entries} | "
                            f"BTC_mom={btc_mom:.2f} ETH_mom={eth_mom:.2f} | "
                            f"BTC={btc_price:,.0f} ETH={eth_price:,.1f}"
                        )

                # 4. 포지션 없으면 진입 체크
                else:
                    # 시간대 필터 (UTC)
                    if cfg.no_entry_hours:
                        from datetime import datetime, timezone
                        current_hour = datetime.now(timezone.utc).hour
                        if current_hour in cfg.no_entry_hours:
                            logger.info(f"  {self.tag} │ SKIP │ no-entry hour ({current_hour:02d} UTC)")
                            await asyncio.sleep(cfg.scan_interval)
                            continue

                    corr = self._check_correlation()
                    # 2026-04-18 v3: 글로벌 correlation/volatility history 기록
                    if corr is not None:
                        record_correlation(corr)
                    record_btc_price(btc_price)
                    spread = self._calc_spread(btc_price, eth_price)
                    self._update_spread_history(spread)
                    zscore, spread_ma, spread_std = self._calc_spread_zscore()

                    # 방향 비대칭
                    btc_threshold = cfg.momentum_threshold
                    eth_threshold = cfg.momentum_threshold * (1 - cfg.coin2_long_entry_bonus)

                    direction = None
                    entry_type = "momentum"

                    # Evolver Composite Signal — Medallion-style 다중 신호 합성
                    composite_score = 0.0
                    composite_direction = None  # composite이 시사하는 방향
                    try:
                        import yaml
                        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
                        with cfg_path.open("r", encoding="utf-8") as _f:
                            _cfg = yaml.safe_load(_f) or {}
                        evolver = _cfg.get("strategy_evolver", {})
                        if evolver.get("use_in_nado_scalper"):
                            w = evolver.get("signal_weights", {})
                            # 4개 정규화된 신호 (각 -2~+2 범위로 clamping)
                            # 1) 모멘텀 차이 (btc_long 방향이 양수)
                            mom_diff_raw = btc_mom - eth_mom
                            mom_sig = max(-2, min(2, mom_diff_raw / cfg.momentum_threshold))
                            # 2) 스프레드 z-score (반전 방향이 양수 = btc_long)
                            zscore_sig = max(-2, min(2, -zscore / max(cfg.spread_zscore_entry, 0.1)))
                            # 3) 상관계수 (높을수록 페어 전제 OK)
                            corr_sig = max(-2, min(2, (corr - 0.5) * 2))  # corr 0.5 기준
                            # 4) 변동성 (너무 높으면 -1, 적당하면 +1)
                            # 모멘텀의 절대값 평균을 volatility proxy
                            vol_avg = (abs(btc_mom) + abs(eth_mom)) / 2
                            vol_sig = 1.0 if vol_avg < cfg.momentum_threshold * 1.5 else -0.5

                            composite_score = (
                                mom_sig * w.get("momentum_diff", 0.35)
                                + zscore_sig * w.get("spread_zscore", 0.25)
                                + corr_sig * w.get("volatility_ratio", 0.10)  # corr 사용
                                + vol_sig * w.get("bollinger_breakout", 0.20)
                            )
                            # composite 방향: 양수면 btc_long, 음수면 eth_long
                            if abs(composite_score) > 0.5:
                                composite_direction = "btc_long" if composite_score > 0 else "eth_long"
                            # 2026-04-25: 신호 이력 기록 핫패스 I/O 비활성. SIGNAL_HISTORY_LOG=1 로 재활성
                            import os as _os_sh
                            if _os_sh.environ.get("SIGNAL_HISTORY_LOG", "") == "1":
                                sig_log = Path(__file__).resolve().parent.parent / "signal_history.jsonl"
                                import json as _json
                                from datetime import datetime as _dt2
                                sig_log.open("a", encoding="utf-8").write(_json.dumps({
                                    "ts": _dt2.utcnow().isoformat(),
                                    "exchange": self.exchange_name,
                                    "btc_mom": btc_mom, "eth_mom": eth_mom,
                                    "zscore": zscore, "corr": corr,
                                    "mom_sig": mom_sig, "zscore_sig": zscore_sig,
                                    "corr_sig": corr_sig, "vol_sig": vol_sig,
                                    "composite": composite_score,
                                    "comp_dir": composite_direction,
                                }) + "\n")
                    except Exception:
                        pass

                    # A. 모멘텀 기반 진입 (기존)
                    if btc_mom >= btc_threshold:
                        direction = "btc_long"
                    elif eth_mom >= eth_threshold and btc_mom < btc_threshold:
                        direction = "eth_long"

                    # A'. Composite override — 기존 진입 없을 때 composite 강한 신호면 진입 허용
                    # (use_in_nado_scalper: true 이고 composite |score| > 1.2 + 기존 약한 모멘텀 있을 때)
                    if direction is None and composite_direction and abs(composite_score) > 1.2:
                        # 기존 임계치의 절반이라도 넘으면 composite으로 진입
                        if composite_direction == "btc_long" and btc_mom >= btc_threshold * 0.5:
                            direction = "btc_long"
                            entry_type = "composite"
                        elif composite_direction == "eth_long" and eth_mom >= eth_threshold * 0.5:
                            direction = "eth_long"
                            entry_type = "composite"

                    # B. Composite 방향 일치 + 강한 신호 → margin boost
                    if direction and composite_direction == direction and composite_score > 1.5:
                        effective_margin = min(effective_margin * 1.3, cfg.margin_per_entry * 1.5)
                    # B'. Composite 방향 불일치 → margin 감소 (반대 신호)
                    elif direction and composite_direction and composite_direction != direction and abs(composite_score) > 1.0:
                        effective_margin *= 0.6
                        logger.info(f"  {self.tag} │ composite 반대신호 ({composite_score:+.2f}) → margin 감소")

                    # B. 스프레드 Z-score 기반 진입 (모멘텀 안 되면)
                    if direction is None and cfg.spread_entry_enabled and abs(zscore) >= cfg.spread_zscore_entry:
                        if zscore < -cfg.spread_zscore_entry:
                            direction = "btc_long"
                            entry_type = "spread"
                        elif zscore > cfg.spread_zscore_entry:
                            direction = "eth_long"
                            entry_type = "spread"
                        if direction:
                            logger.info(
                                f"  {self.tag} │ 스프레드 시그널 │ z={zscore:+.2f} → {direction} | "
                                f"spread={spread:.4f} ma={spread_ma:.4f}"
                            )

                    if direction:
                        if corr >= cfg.min_correlation:
                            # 2026-04-24: Regime gate — Minara 236→21 survivors (fee drag 방어)
                            regime_passed, regime_detail = await self._passes_regime_filter(cfg.coin1)
                            if not regime_passed:
                                if cfg.regime_filter_shadow_only:
                                    logger.info(
                                        f"  {self.tag} │ [regime_gate SHADOW] WOULD SKIP dir={direction} coin={cfg.coin1} {regime_detail}"
                                    )
                                    # shadow mode: log only, still enter
                                    self.pos.entry_type = entry_type
                                    await self._enter(direction, btc_price, eth_price)
                                else:
                                    logger.info(
                                        f"  {self.tag} │ [regime_gate] skipped dir={direction} coin={cfg.coin1} {regime_detail}"
                                    )
                            else:
                                logger.info(
                                    f"  {self.tag} │ [regime_gate] passed dir={direction} coin={cfg.coin1} {regime_detail}"
                                )
                                self.pos.entry_type = entry_type
                                await self._enter(direction, btc_price, eth_price)
                        else:
                            logger.info(
                                f"  {self.tag} │ SKIP │ low corr={corr:.3f} dir={direction}"
                            )
                    else:
                        logger.info(
                            f"  {self.tag} │ 대기 | mom={btc_mom:.2f}/{eth_mom:.2f} z={zscore:+.2f} corr={corr:.3f}"
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"  {self.tag} │ 사이클 에러: {e}")

            await asyncio.sleep(cfg.scan_interval)

    # ── 상태 관리 ──

    def _save_state(self):
        try:
            state = self.get_state()
            # atomic write: tmp에 쓰고 rename (크래시 시 파일 손상 방지)
            tmp_file = self._state_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(state, f)
            tmp_file.replace(self._state_file)
        except Exception as e:
            logger.debug(f"  {self.tag} │ 상태 저장 실패: {e}")

    def _load_state(self):
        try:
            if self._state_file.exists():
                with open(self._state_file) as f:
                    self.restore_state(json.load(f))
        except Exception as e:
            logger.warning(f"  {self.tag} │ 상태 로드 실패: {e}")

    def get_state(self) -> dict:
        if not self.pos.direction:
            return {}
        return {
            "strategy": "nado_pair_scalper",
            "direction": self.pos.direction,
            "entry_count": self.pos.entry_count,
            "total_margin": self.pos.total_margin,
            "entry_type": self.pos.entry_type,
            "btc_size": self.pos.btc_size,
            "eth_size": self.pos.eth_size,
            "btc_entry_avg": self.pos.btc_entry_avg,
            "eth_entry_avg": self.pos.eth_entry_avg,
            "last_spread": self.pos.last_spread,
            "peak_pnl": self.pos.peak_pnl,
            "trailing_active": self.pos.trailing_active,
        }

    def restore_state(self, state: dict):
        if not state or state.get("strategy") != "nado_pair_scalper":
            return
        self.pos = ScalperPosition(
            direction=state.get("direction", ""),
            entry_count=state.get("entry_count", 0),
            total_margin=state.get("total_margin", 0),
            entry_type=state.get("entry_type", ""),
            btc_size=state.get("btc_size", 0),
            eth_size=state.get("eth_size", 0),
            btc_entry_avg=state.get("btc_entry_avg", 0),
            eth_entry_avg=state.get("eth_entry_avg", 0),
            last_spread=state.get("last_spread", 0),
            entry_time=time.time(),
            peak_pnl=state.get("peak_pnl", 0.0),
            trailing_active=state.get("trailing_active", False),
        )

    async def shutdown(self, close_positions: bool = True):
        self.running = False
        if close_positions and self.pos.direction:
            try:
                btc_price = await self.wrapper.get_mark_price(self.config.coin1)
                eth_price = await self.wrapper.get_mark_price(self.config.coin2)
                await self._close_all("shutdown", btc_price, eth_price)
            except Exception as e:
                logger.error(f"  {self.tag} │ 종료 청산 실패: {e}")
