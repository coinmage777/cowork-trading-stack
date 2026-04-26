"""
Momentum Score Calculator
6가지 지표 가중 평균으로 모멘텀 점수 계산

지표 가중치:
- Price Change: 30%
- MA Alignment: 20%
- RSI: 15%
- Volume Change: 15%
- Sharpe Ratio: 10%
- Consistency: 10%
"""

import math


def calculate_ma(prices: list[float], period: int) -> float:
    """이동평균 계산"""
    if len(prices) < period:
        return 0.0
    return sum(prices[-period:]) / period


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """RSI 계산"""
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(-period, 0):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_stddev(values: list[float]) -> float:
    """표준편차 계산"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def calculate_momentum_score(candles: list[dict]) -> float:
    """
    모멘텀 점수 계산 (0~100)

    Parameters:
        candles: list of dict with keys: open, high, low, close, volume
                 최소 200개 이상 필요

    Returns:
        float: 모멘텀 점수 (0~100, 높을수록 강한 상승 모멘텀)
    """
    if len(candles) < 200:
        return 50.0  # 데이터 부족시 중립

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    # 1. Price Change Score (30%)
    # 최근 50봉 가격 변화율
    price_change = (closes[-1] - closes[-50]) / closes[-50] * 100
    price_change_score = max(0, min(100, 50 + price_change * 5))

    # 2. MA Alignment Score (20%)
    # MA5 > MA10 > MA20 > MA50 > MA100 순서 정렬도
    ma5 = calculate_ma(closes, 5)
    ma10 = calculate_ma(closes, 10)
    ma20 = calculate_ma(closes, 20)
    ma50 = calculate_ma(closes, 50)
    ma100 = calculate_ma(closes, 100)

    ma_list = [ma5, ma10, ma20, ma50, ma100]
    ma_score = 0
    for i in range(len(ma_list) - 1):
        if ma_list[i] > ma_list[i + 1]:
            ma_score += 25
    # 0, 25, 50, 75, 100

    # 3. RSI Score (15%)
    rsi = calculate_rsi(closes, 14)
    if rsi >= 70:
        rsi_score = 80 + (rsi - 70) * 0.67  # 70~100 -> 80~100
    elif rsi >= 50:
        rsi_score = 50 + (rsi - 50) * 1.5   # 50~70 -> 50~80
    elif rsi >= 30:
        rsi_score = 10 + (rsi - 30) * 2      # 30~50 -> 10~50
    else:
        rsi_score = rsi * 0.33                # 0~30 -> 0~10

    # 4. Volume Score (15%)
    # 최근 20봉 평균 거래량 vs 이전 50봉 평균
    recent_vol = sum(volumes[-20:]) / 20
    prev_vol = sum(volumes[-70:-20]) / 50 if len(volumes) >= 70 else sum(volumes[:-20]) / max(1, len(volumes) - 20)

    if prev_vol > 0:
        vol_ratio = recent_vol / prev_vol
        volume_score = max(0, min(100, vol_ratio * 50))
    else:
        volume_score = 50

    # 5. Sharpe Ratio Score (10%)
    # 최근 50봉 수익률의 샤프비율
    returns = []
    for i in range(-50, 0):
        r = (closes[i] - closes[i - 1]) / closes[i - 1]
        returns.append(r)

    avg_return = sum(returns) / len(returns)
    std_return = calculate_stddev(returns)

    if std_return > 0:
        sharpe = avg_return / std_return * math.sqrt(365)
        sharpe_score = max(0, min(100, 50 + sharpe * 20))
    else:
        sharpe_score = 50

    # 6. Consistency Score (10%)
    # 최근 20봉 중 양봉 비율
    positive_candles = sum(1 for i in range(-20, 0) if closes[i] > closes[i - 1])
    consistency_score = (positive_candles / 20) * 100

    # 가중 평균
    total_score = (
        price_change_score * 0.3 +
        ma_score * 0.2 +
        rsi_score * 0.15 +
        volume_score * 0.15 +
        sharpe_score * 0.1 +
        consistency_score * 0.1
    )

    return round(total_score, 2)
