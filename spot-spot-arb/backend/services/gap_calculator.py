"""갭 계산 엔진.

해외 거래소:
  현물갭 = BTC_빗썸_KRW_ASK / (해외_USDT_BID × USDT_빗썸_KRW_LAST) × 10,000
  현선갭 = BTC_빗썸_KRW_ASK / (해외_USDT.P_BID × USDT_빗썸_KRW_LAST) × 10,000

국내 거래소 (업비트, 코인원):
  현물갭 = BTC_빗썸_KRW_ASK / 국내_KRW_BID × 10,000

10,000 = 패리티, 9,500 이하 = 5%+ 역프
"""

import time

from backend.exchanges.types import BithumbData, ExchangeData, GapResult
from backend.exchanges.manager import KRW_EXCHANGES


def calculate_gap(
    foreign_bid_usdt: float,
    usdt_krw: float,
    bithumb_ask_krw: float,
) -> float:
    """해외 거래소 갭 값을 계산한다.

    Args:
        foreign_bid_usdt: 해외 거래소의 USDT 기준 BID 가격
        usdt_krw: 빗썸 USDT/KRW last 가격
        bithumb_ask_krw: 빗썸 KRW 기준 ASK 가격

    Returns:
        갭 값 (10,000 = 패리티, <=9,500 = 역프)
    """
    return bithumb_ask_krw / (foreign_bid_usdt * usdt_krw) * 10_000


def calculate_gap_krw(
    domestic_bid_krw: float,
    bithumb_ask_krw: float,
) -> float:
    """국내 거래소 갭 값을 계산한다 (KRW 직접 비교).

    Args:
        domestic_bid_krw: 국내 거래소의 KRW 기준 BID 가격
        bithumb_ask_krw: 빗썸 KRW 기준 ASK 가격

    Returns:
        갭 값 (10,000 = 패리티, <=9,500 = 역프)
    """
    return bithumb_ask_krw / domestic_bid_krw * 10_000


def calculate_impact_gap(
    bithumb_asks: list[list[float]],
    foreign_bids: list[list[float]],
    usdt_krw: float,
    volume_usd: float,
    is_krw_exchange: bool = False,
) -> float | None:
    """오더북 기반 impact price로 갭을 재계산한다.

    1. 해외 bid VWAP: volume_usd 어치를 팔 때 평균 체결가 (bid를 위에서부터 소화)
    2. 빗썸 ask VWAP: 동일 코인 수량을 살 때 평균 체결가 (ask를 아래에서부터 소화)
    3. impact_gap = bithumb_ask_vwap / (foreign_bid_vwap × usdt_krw) × 10,000
       (국내 KRW 거래소: bithumb_ask_vwap / foreign_bid_vwap × 10,000)
    4. 호가 부족 시 None 반환

    Args:
        bithumb_asks: 빗썸 ask 호가 [[price, qty], ...] (낮은 가격순)
        foreign_bids: 해외 bid 호가 [[price, qty], ...] (높은 가격순)
        usdt_krw: 빗썸 USDT/KRW last 가격
        volume_usd: 검증 규모 (USD)
        is_krw_exchange: True면 해외가 KRW 기반 국내 거래소

    Returns:
        impact gap 값 또는 None (호가 부족)
    """
    if not bithumb_asks or not foreign_bids:
        return None

    # --- 1) 해외 bid VWAP: volume_usd 어치를 팔 때 평균 체결가 ---
    if is_krw_exchange:
        # KRW 거래소: volume_usd를 KRW로 환산
        remaining_value = volume_usd * usdt_krw
    else:
        remaining_value = volume_usd

    total_cost_foreign = 0.0
    total_qty = 0.0

    for price, qty in foreign_bids:
        if price <= 0:
            continue
        level_value = price * qty
        if level_value >= remaining_value:
            filled_qty = remaining_value / price
            total_cost_foreign += remaining_value
            total_qty += filled_qty
            remaining_value = 0.0
            break
        else:
            total_cost_foreign += level_value
            total_qty += qty
            remaining_value -= level_value

    if remaining_value > 0 or total_qty <= 0:
        # 호가 부족: volume_usd를 채울 수 없음
        return None

    foreign_bid_vwap = total_cost_foreign / total_qty

    # --- 2) 빗썸 ask VWAP: 동일 코인 수량을 살 때 평균 체결가 ---
    remaining_qty = total_qty
    total_cost_bithumb = 0.0

    for price, qty in bithumb_asks:
        if price <= 0:
            continue
        if qty >= remaining_qty:
            total_cost_bithumb += price * remaining_qty
            remaining_qty = 0.0
            break
        else:
            total_cost_bithumb += price * qty
            remaining_qty -= qty

    if remaining_qty > 0:
        # 빗썸 ask 호가 부족
        return None

    bithumb_ask_vwap = total_cost_bithumb / total_qty

    # --- 3) impact gap 계산 ---
    if is_krw_exchange:
        return bithumb_ask_vwap / foreign_bid_vwap * 10_000
    else:
        return bithumb_ask_vwap / (foreign_bid_vwap * usdt_krw) * 10_000


def build_gap_result(
    ticker: str,
    bithumb_data: BithumbData,
    exchange_data_map: dict[str, ExchangeData],
) -> GapResult:
    """각 거래소의 갭을 계산하여 GapResult를 조립한다.

    갭은 빗썸 ask와 USDT/KRW가 모두 유효할 때만 계산된다.

    Args:
        ticker: 예) "BTC"
        bithumb_data: 빗썸 BBO + USDT/KRW 데이터
        exchange_data_map: 거래소명 -> ExchangeData 매핑

    Returns:
        완성된 GapResult
    """
    bithumb_ask = bithumb_data.ask
    usdt_krw = bithumb_data.usdt_krw_last

    can_calculate = (
        bithumb_ask is not None
        and bithumb_ask > 0
        and usdt_krw is not None
        and usdt_krw > 0
    )

    updated_exchanges: dict[str, ExchangeData] = {}

    for exchange_name, ex_data in exchange_data_map.items():
        spot_gap: float | None = None
        futures_gap: float | None = None
        is_krw = exchange_name in KRW_EXCHANGES

        # 국내 거래소는 빗썸 ask만 있으면 계산 가능 (USDT 불필요)
        can_calc_this = (
            (is_krw and bithumb_ask is not None and bithumb_ask > 0)
            or can_calculate
        )

        if can_calc_this:
            # 현물갭
            if ex_data.spot_bbo and ex_data.spot_bbo.bid is not None:
                try:
                    if is_krw:
                        spot_gap = calculate_gap_krw(
                            ex_data.spot_bbo.bid,
                            bithumb_ask,    # type: ignore[arg-type]
                        )
                    else:
                        spot_gap = calculate_gap(
                            ex_data.spot_bbo.bid,
                            usdt_krw,       # type: ignore[arg-type]
                            bithumb_ask,    # type: ignore[arg-type]
                        )
                except ZeroDivisionError:
                    spot_gap = None

            # 현선갭 (국내 거래소는 선물 없음)
            if ex_data.futures_bbo and ex_data.futures_bbo.bid is not None:
                try:
                    futures_gap = calculate_gap(
                        ex_data.futures_bbo.bid,
                        usdt_krw,       # type: ignore[arg-type]
                        bithumb_ask,    # type: ignore[arg-type]
                    )
                except ZeroDivisionError:
                    futures_gap = None

        updated_exchanges[exchange_name] = ExchangeData(
            exchange=ex_data.exchange,
            spot_bbo=ex_data.spot_bbo,
            futures_bbo=ex_data.futures_bbo,
            spot_supported=ex_data.spot_supported,
            futures_supported=ex_data.futures_supported,
            spot_gap=spot_gap,
            futures_gap=futures_gap,
            networks=ex_data.networks,
            margin=ex_data.margin,
            loan=ex_data.loan,
        )

    return GapResult(
        ticker=ticker,
        timestamp=int(time.time()),
        bithumb=bithumb_data,
        exchanges=updated_exchanges,
    )
