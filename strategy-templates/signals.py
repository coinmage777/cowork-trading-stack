"""
Signal Modules — 플러그인 기반 진입/청산 시그널 시스템

autoresearch 컨셉 적용:
- 각 시그널은 독립 모듈로 작동 (enable/disable 가능)
- 시그널별 가중치를 evolver가 자동 조정
- 새 시그널 추가 시 클래스 하나만 작성하면 됨

시그널 출력 규격:
- score: -100 ~ +100 (양수=롱 유리, 음수=숏 유리)
- confidence: 0.0 ~ 1.0 (시그널 확신도)

사용법:
    registry = SignalRegistry()
    registry.register(MomentumDiffSignal())
    registry.register(SpreadZScoreSignal())

    composite = registry.evaluate(candles1, candles2, weights={"momentum_diff": 0.4, ...})
"""

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SignalResult:
    """시그널 평가 결과"""
    name: str
    score: float          # -100 ~ +100 (양수=coin1_long 유리)
    confidence: float     # 0.0 ~ 1.0
    metadata: dict = None # 디버깅/로깅용 추가 정보

    def __post_init__(self):
        self.score = max(-100, min(100, self.score))
        self.confidence = max(0.0, min(1.0, self.confidence))
        if self.metadata is None:
            self.metadata = {}


@dataclass
class CompositeSignal:
    """복합 시그널 결과"""
    direction: Optional[str]  # "coin1_long", "coin2_long", None
    strength: float           # 0 ~ 100 (진입 강도)
    signals: list             # 개별 SignalResult 리스트
    weighted_score: float     # 가중 합산 점수


class BaseSignal(ABC):
    """시그널 베이스 클래스"""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    def description(self) -> str:
        return ""

    @abstractmethod
    def evaluate(self, candles1: list, candles2: list, **kwargs) -> SignalResult:
        """
        캔들 데이터로 시그널 평가

        Parameters:
            candles1: coin1 캔들 (시간순, dict with open/high/low/close/volume)
            candles2: coin2 캔들
            **kwargs: 추가 파라미터 (config 값 등)

        Returns:
            SignalResult
        """
        pass


# ──────────────────────────────────────────
# 시그널 구현체들
# ──────────────────────────────────────────

class MomentumDiffSignal(BaseSignal):
    """기존 모멘텀 차이 시그널 (현행 전략 호환)"""

    @property
    def name(self) -> str:
        return "momentum_diff"

    @property
    def description(self) -> str:
        return "6지표 가중 모멘텀 점수 차이 (기존 pair_trader 로직)"

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        from .momentum import calculate_momentum_score

        min_candles = kwargs.get("min_candles", 200)
        if len(candles1) < min_candles or len(candles2) < min_candles:
            return SignalResult(self.name, 0.0, 0.0, {"reason": "candles_insufficient"})

        mom1 = calculate_momentum_score(candles1)
        mom2 = calculate_momentum_score(candles2)
        diff = mom1 - mom2

        # diff를 -100~+100 스케일로 변환 (max diff ≈ 60 기준)
        score = max(-100, min(100, diff * 1.67))
        confidence = min(1.0, abs(diff) / 30.0)  # diff 30 이상이면 확신도 1.0

        return SignalResult(
            self.name, score, confidence,
            {"mom1": mom1, "mom2": mom2, "raw_diff": diff}
        )


class SpreadZScoreSignal(BaseSignal):
    """스프레드 Z-Score 평균회귀 시그널"""

    @property
    def name(self) -> str:
        return "spread_zscore"

    @property
    def description(self) -> str:
        return "가격 비율의 Z-Score 기반 평균회귀 시그널"

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        lookback = kwargs.get("zscore_lookback", 100)

        if len(candles1) < lookback or len(candles2) < lookback:
            return SignalResult(self.name, 0.0, 0.0)

        # 스프레드 = log(price1/price2)
        spreads = []
        for i in range(-lookback, 0):
            p1 = candles1[i]["close"]
            p2 = candles2[i]["close"]
            if p2 > 0:
                spreads.append(math.log(p1 / p2))

        if len(spreads) < 20:
            return SignalResult(self.name, 0.0, 0.0)

        mean = sum(spreads) / len(spreads)
        std = math.sqrt(sum((s - mean) ** 2 for s in spreads) / (len(spreads) - 1))

        if std == 0:
            return SignalResult(self.name, 0.0, 0.0)

        current_spread = spreads[-1]
        zscore = (current_spread - mean) / std

        # Z-Score가 음수 → 스프레드가 평균 이하 → coin1 저평가 → coin1_long
        # Z-Score가 양수 → 스프레드가 평균 이상 → coin1 고평가 → coin2_long
        score = -zscore * 33.3  # z=±3 → score=±100
        score = max(-100, min(100, score))
        confidence = min(1.0, abs(zscore) / 2.5)

        return SignalResult(
            self.name, score, confidence,
            {"zscore": round(zscore, 3), "spread_mean": round(mean, 6), "spread_std": round(std, 6)}
        )


class RSIDivergenceSignal(BaseSignal):
    """RSI 다이버전스 시그널"""

    @property
    def name(self) -> str:
        return "rsi_divergence"

    @property
    def description(self) -> str:
        return "두 코인의 RSI 차이 + 과매수/과매도 반전 감지"

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        from .momentum import calculate_rsi

        if len(candles1) < 50 or len(candles2) < 50:
            return SignalResult(self.name, 0.0, 0.0)

        closes1 = [c["close"] for c in candles1]
        closes2 = [c["close"] for c in candles2]

        rsi1 = calculate_rsi(closes1, 14)
        rsi2 = calculate_rsi(closes2, 14)

        # RSI 차이 기반 (페어 트레이딩이니까 상대 RSI가 핵심)
        rsi_diff = rsi1 - rsi2

        # 과매수/과매도 반전 보너스
        reversal_bonus = 0
        if rsi1 > 70 and rsi2 < 40:
            reversal_bonus = -20  # coin1 과매수 + coin2 과매도 → coin2_long 유리
        elif rsi1 < 30 and rsi2 > 60:
            reversal_bonus = 20   # coin1 과매도 + coin2 과매수 → coin1_long 유리

        score = rsi_diff * 1.0 + reversal_bonus  # RSI diff 50 정도가 max
        score = max(-100, min(100, score))
        confidence = min(1.0, abs(rsi_diff) / 30.0)

        return SignalResult(
            self.name, score, confidence,
            {"rsi1": round(rsi1, 1), "rsi2": round(rsi2, 1), "rsi_diff": round(rsi_diff, 1), "reversal": reversal_bonus}
        )


class VolatilityRatioSignal(BaseSignal):
    """변동성 비율 시그널 — 변동성 낮은 쪽이 Long에 유리"""

    @property
    def name(self) -> str:
        return "volatility_ratio"

    @property
    def description(self) -> str:
        return "상대 변동성 비교 — 안정적인 쪽을 Long"

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        lookback = kwargs.get("vol_lookback", 50)

        if len(candles1) < lookback or len(candles2) < lookback:
            return SignalResult(self.name, 0.0, 0.0)

        def calc_volatility(candles, n):
            closes = [c["close"] for c in candles[-n:]]
            returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
            if len(returns) < 2:
                return 0.0
            mean = sum(returns) / len(returns)
            var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
            return math.sqrt(var)

        vol1 = calc_volatility(candles1, lookback)
        vol2 = calc_volatility(candles2, lookback)

        if vol1 == 0 or vol2 == 0:
            return SignalResult(self.name, 0.0, 0.0)

        # vol_ratio > 1 → coin1이 더 변동성 높음 → coin2_long 유리
        vol_ratio = vol1 / vol2

        # ratio 1.0 = 중립, 1.5 = 강한 시그널
        score = -(vol_ratio - 1.0) * 100  # ratio 1.5 → score -50
        score = max(-100, min(100, score))
        confidence = min(1.0, abs(vol_ratio - 1.0) / 0.5)

        return SignalResult(
            self.name, score, confidence,
            {"vol1": round(vol1, 6), "vol2": round(vol2, 6), "ratio": round(vol_ratio, 3)}
        )


class BollingerBreakoutSignal(BaseSignal):
    """볼린저밴드 상대 위치 시그널"""

    @property
    def name(self) -> str:
        return "bollinger_breakout"

    @property
    def description(self) -> str:
        return "볼린저밴드 %B 기반 과매수/과매도 판단"

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        period = kwargs.get("bb_period", 20)
        num_std = kwargs.get("bb_std", 2.0)

        if len(candles1) < period + 5 or len(candles2) < period + 5:
            return SignalResult(self.name, 0.0, 0.0)

        def calc_percent_b(candles, n, nstd):
            closes = [c["close"] for c in candles[-n:]]
            mean = sum(closes) / len(closes)
            std = math.sqrt(sum((c - mean) ** 2 for c in closes) / len(closes))
            if std == 0:
                return 0.5
            upper = mean + nstd * std
            lower = mean - nstd * std
            current = candles[-1]["close"]
            if upper == lower:
                return 0.5
            return (current - lower) / (upper - lower)

        pb1 = calc_percent_b(candles1, period, num_std)
        pb2 = calc_percent_b(candles2, period, num_std)

        # %B 차이: coin1이 밴드 상단 + coin2가 밴드 하단 → coin2_long
        pb_diff = pb1 - pb2  # 양수 → coin1이 더 과매수

        score = -pb_diff * 100  # pb_diff 1.0 → score -100
        score = max(-100, min(100, score))
        confidence = min(1.0, abs(pb_diff) / 0.6)

        return SignalResult(
            self.name, score, confidence,
            {"pb1": round(pb1, 3), "pb2": round(pb2, 3), "pb_diff": round(pb_diff, 3)}
        )


class PriceRatioMeanReversionSignal(BaseSignal):
    """가격 비율 평균회귀 (단순 SMA 기반)"""

    @property
    def name(self) -> str:
        return "price_ratio_mr"

    @property
    def description(self) -> str:
        return "price1/price2 비율의 SMA 대비 이탈도"

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        lookback = kwargs.get("ratio_lookback", 100)

        if len(candles1) < lookback or len(candles2) < lookback:
            return SignalResult(self.name, 0.0, 0.0)

        ratios = []
        for i in range(-lookback, 0):
            p1 = candles1[i]["close"]
            p2 = candles2[i]["close"]
            if p2 > 0:
                ratios.append(p1 / p2)

        if len(ratios) < 20:
            return SignalResult(self.name, 0.0, 0.0)

        sma = sum(ratios) / len(ratios)
        current = ratios[-1]

        if sma == 0:
            return SignalResult(self.name, 0.0, 0.0)

        # 현재 비율이 SMA 위 → coin1 고평가 → coin2_long
        deviation_pct = (current - sma) / sma * 100

        score = -deviation_pct * 20  # 5% 이탈 → score ±100
        score = max(-100, min(100, score))
        confidence = min(1.0, abs(deviation_pct) / 3.0)

        return SignalResult(
            self.name, score, confidence,
            {"current_ratio": round(current, 4), "sma_ratio": round(sma, 4), "deviation_pct": round(deviation_pct, 3)}
        )


class HurstRegimeFilter(BaseSignal):
    """
    Hurst Exponent 기반 레짐 필터.

    H < 0.5 → 평균회귀 레짐 (페어트레이딩 적합) → 양수 confidence
    H ≈ 0.5 → 랜덤워크 (판단 불가) → 낮은 confidence
    H > 0.5 → 트렌딩 레짐 (페어트레이딩 위험) → 강한 음수 score로 진입 차단

    R/S (Rescaled Range) 방식으로 Hurst Exponent 계산.
    score가 아닌 confidence를 활용해 다른 시그널을 억제하는 "필터" 역할.
    """

    @property
    def name(self) -> str:
        return "hurst_regime"

    @property
    def description(self) -> str:
        return "Hurst Exponent 기반 레짐 감지 — 트렌딩 시 진입 차단"

    @staticmethod
    def _hurst_rs(series: list[float]) -> float:
        """
        R/S (Rescaled Range) 방식으로 Hurst Exponent 계산.
        최소 100개 데이터 포인트 필요.
        """
        n = len(series)
        if n < 100:
            return 0.5  # 데이터 부족 시 중립

        # log returns
        returns = [math.log(series[i] / series[i - 1])
                   for i in range(1, n) if series[i - 1] > 0 and series[i] > 0]

        if len(returns) < 50:
            return 0.5

        # 다양한 윈도우 크기에서 R/S 계산
        min_window = 10
        max_window = len(returns) // 4
        if max_window <= min_window:
            return 0.5

        log_ns = []
        log_rs = []

        window = min_window
        while window <= max_window:
            rs_values = []
            num_segments = len(returns) // window

            for seg in range(num_segments):
                start = seg * window
                end = start + window
                segment = returns[start:end]

                mean_seg = sum(segment) / len(segment)
                deviations = [x - mean_seg for x in segment]

                # cumulative deviations
                cumdev = []
                running = 0.0
                for d in deviations:
                    running += d
                    cumdev.append(running)

                r = max(cumdev) - min(cumdev)
                s = math.sqrt(sum(d ** 2 for d in deviations) / len(deviations))

                if s > 1e-12:
                    rs_values.append(r / s)

            if rs_values:
                avg_rs = sum(rs_values) / len(rs_values)
                if avg_rs > 0:
                    log_ns.append(math.log(window))
                    log_rs.append(math.log(avg_rs))

            # 윈도우 크기 대략 1.3배씩 증가 (더 많은 데이터 포인트)
            window = max(window + 1, int(window * 1.3))

        if len(log_ns) < 3:
            return 0.5

        # 선형 회귀로 Hurst exponent 추정 (log(R/S) = H * log(n) + c)
        n_pts = len(log_ns)
        sum_x = sum(log_ns)
        sum_y = sum(log_rs)
        sum_xy = sum(x * y for x, y in zip(log_ns, log_rs))
        sum_x2 = sum(x ** 2 for x in log_ns)

        denom = n_pts * sum_x2 - sum_x ** 2
        if abs(denom) < 1e-12:
            return 0.5

        hurst = (n_pts * sum_xy - sum_x * sum_y) / denom
        return max(0.0, min(1.0, hurst))

    def evaluate(self, candles1, candles2, **kwargs) -> SignalResult:
        lookback = kwargs.get("hurst_lookback", 200)

        if len(candles1) < lookback or len(candles2) < lookback:
            return SignalResult(self.name, 0.0, 0.0, {"reason": "insufficient_data"})

        # 스프레드 = price1 / price2 비율의 시계열
        spread_series = []
        for i in range(-lookback, 0):
            p1 = candles1[i]["close"]
            p2 = candles2[i]["close"]
            if p2 > 0 and p1 > 0:
                spread_series.append(p1 / p2)

        if len(spread_series) < 100:
            return SignalResult(self.name, 0.0, 0.0, {"reason": "spread_too_short"})

        hurst = self._hurst_rs(spread_series)

        # H < 0.45 → 강한 평균회귀 → score +50 (진입 허용 부스트)
        # H 0.45~0.55 → 중립 구간 → score 0
        # H > 0.55 → 트렌딩 → score -100 (진입 차단)
        if hurst < 0.45:
            # 평균회귀 레짐 — 페어트레이딩에 유리
            score = (0.45 - hurst) * 200  # H=0.35 → score +20
            score = min(50.0, score)
            confidence = min(1.0, (0.45 - hurst) / 0.15)
        elif hurst > 0.55:
            # 트렌딩 레짐 — 진입 차단
            score = -(hurst - 0.55) * 400  # H=0.65 → score -40, H=0.8 → score -100
            score = max(-100.0, score)
            confidence = min(1.0, (hurst - 0.55) / 0.2)
        else:
            # 중립 구간 — 영향 없음
            score = 0.0
            confidence = 0.1  # 낮은 confidence로 가중치 최소화

        return SignalResult(
            self.name, score, confidence,
            {"hurst": round(hurst, 4), "regime": "mean_revert" if hurst < 0.45 else "trending" if hurst > 0.55 else "neutral",
             "spread_len": len(spread_series)}
        )


# ──────────────────────────────────────────
# Signal Registry — 시그널 관리/실행
# ──────────────────────────────────────────

# 사용 가능한 시그널 전체 목록
ALL_SIGNALS = {
    "momentum_diff": MomentumDiffSignal,
    "spread_zscore": SpreadZScoreSignal,
    "rsi_divergence": RSIDivergenceSignal,
    "volatility_ratio": VolatilityRatioSignal,
    "bollinger_breakout": BollingerBreakoutSignal,
    "price_ratio_mr": PriceRatioMeanReversionSignal,
    "hurst_regime": HurstRegimeFilter,
}

# 기본 가중치 (기존 전략 호환 = momentum_diff 100%)
DEFAULT_WEIGHTS = {
    "momentum_diff": 1.0,
    "spread_zscore": 0.0,
    "rsi_divergence": 0.0,
    "volatility_ratio": 0.0,
    "bollinger_breakout": 0.0,
    "price_ratio_mr": 0.0,
    "hurst_regime": 0.0,
}


class SignalRegistry:
    """시그널 등록/실행/합산"""

    def __init__(self, weights: dict = None):
        self.signals: dict[str, BaseSignal] = {}
        self.weights = weights or DEFAULT_WEIGHTS.copy()

    def register(self, signal: BaseSignal):
        self.signals[signal.name] = signal

    def register_all(self):
        """사용 가능한 모든 시그널 등록"""
        for name, cls in ALL_SIGNALS.items():
            self.signals[name] = cls()

    def register_active(self, weights: dict = None):
        """가중치 > 0인 시그널만 등록 (성능 최적화)"""
        w = weights or self.weights
        for name, weight in w.items():
            if weight > 0 and name in ALL_SIGNALS:
                self.signals[name] = ALL_SIGNALS[name]()
        self.weights = w

    def set_weights(self, weights: dict):
        self.weights = weights

    def evaluate(self, candles1: list, candles2: list, **kwargs) -> CompositeSignal:
        """
        등록된 시그널을 모두 실행하고 가중 합산.
        hurst_regime은 특별 처리: 트렌딩 레짐이면 전체 시그널을 억제.

        Returns:
            CompositeSignal with direction, strength, individual signals
        """
        results = []
        weighted_sum = 0.0
        total_weight = 0.0
        regime_penalty = 1.0  # 기본값: 패널티 없음

        # 1단계: 레짐 필터 먼저 평가 (가중합에는 포함 안 됨)
        regime_signal = self.signals.get("hurst_regime")
        regime_weight = self.weights.get("hurst_regime", 0.0)
        if regime_signal and regime_weight > 0:
            try:
                regime_result = regime_signal.evaluate(candles1, candles2, **kwargs)
                results.append(regime_result)
                hurst = regime_result.metadata.get("hurst", 0.5)

                if hurst > 0.55:
                    # 트렌딩 레짐 → 시그널 강도 감쇠 (H=0.55→100%, H=0.7→30%, H=0.85+→0%)
                    regime_penalty = max(0.0, 1.0 - (hurst - 0.55) * 3.33)
                elif hurst < 0.45:
                    # 평균회귀 레짐 → 시그널 부스트 (최대 1.3x)
                    regime_penalty = min(1.3, 1.0 + (0.45 - hurst) * 2.0)
            except Exception:
                pass

        # 2단계: 일반 시그널 가중 합산
        for name, signal in self.signals.items():
            if name == "hurst_regime":
                continue  # 이미 처리됨

            weight = self.weights.get(name, 0.0)
            if weight <= 0:
                continue

            try:
                result = signal.evaluate(candles1, candles2, **kwargs)
                results.append(result)

                # confidence로 가중치 조절 (확신도 낮으면 영향 줄임)
                effective_weight = weight * result.confidence
                weighted_sum += result.score * effective_weight
                total_weight += effective_weight
            except Exception:
                continue

        if total_weight == 0:
            return CompositeSignal(
                direction=None, strength=0.0,
                signals=results, weighted_score=0.0
            )

        final_score = weighted_sum / total_weight

        # 레짐 패널티/부스트 적용
        final_score *= regime_penalty

        # 방향 결정
        min_strength = kwargs.get("min_signal_strength", 10.0)  # 최소 시그널 강도

        if abs(final_score) < min_strength:
            direction = None
        elif final_score > 0:
            direction = "coin1_long"
        else:
            direction = "coin2_long"

        return CompositeSignal(
            direction=direction,
            strength=abs(final_score),
            signals=results,
            weighted_score=round(final_score, 2),
        )

    def get_active_signals(self) -> list[str]:
        """활성화된 시그널 이름 목록"""
        return [n for n, w in self.weights.items() if w > 0]

    def get_status(self) -> dict:
        return {
            "registered": list(self.signals.keys()),
            "weights": self.weights,
            "active": self.get_active_signals(),
        }
