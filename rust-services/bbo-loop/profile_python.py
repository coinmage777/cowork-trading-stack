"""
BBO loop Python 핫스팟 프로파일 — CPU vs IO 비율 결정.

두 가지를 측정:
1) gap_calculator.build_gap_result + calculate_impact_gap (pure CPU)
2) _fetch_all_binance_bbos 파싱 부분 (JSON parse + dict assembly) — CPU 포션
3) 실제 BBO loop 1회 iteration 시뮬레이션 (CPU-only, HTTP/IO 제외)

결과로 BBO loop에서 CPU가 차지하는 비율을 추정.
"""

from __future__ import annotations

import json
import random
import statistics
import time
from dataclasses import dataclass
from typing import Optional


# ====================================================================
# 타입 (backend.exchanges.types 미러)
# ====================================================================

@dataclass
class BBO:
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: Optional[int] = None

    @property
    def valid(self) -> bool:
        return self.bid is not None and self.ask is not None


@dataclass
class BithumbData:
    ask: Optional[float] = None
    usdt_krw_last: Optional[float] = None


@dataclass
class ExchangeData:
    exchange: str
    spot_bbo: Optional[BBO] = None
    futures_bbo: Optional[BBO] = None
    spot_supported: bool = False
    futures_supported: bool = False
    spot_gap: Optional[float] = None
    futures_gap: Optional[float] = None


@dataclass
class GapResult:
    ticker: str
    timestamp: int
    bithumb: BithumbData
    exchanges: dict[str, ExchangeData]


KRW_EXCHANGES = {'upbit', 'coinone'}
ALL_EXCHANGES = ['binance', 'bybit', 'okx', 'bitget', 'gate', 'htx', 'upbit', 'coinone']


# ====================================================================
# gap_calculator 미러 (pure CPU)
# ====================================================================

def calculate_gap(foreign_bid_usdt: float, usdt_krw: float, bithumb_ask_krw: float) -> float:
    return bithumb_ask_krw / (foreign_bid_usdt * usdt_krw) * 10_000


def calculate_gap_krw(domestic_bid_krw: float, bithumb_ask_krw: float) -> float:
    return bithumb_ask_krw / domestic_bid_krw * 10_000


def calculate_impact_gap(
    bithumb_asks: list[list[float]],
    foreign_bids: list[list[float]],
    usdt_krw: float,
    volume_usd: float,
    is_krw_exchange: bool = False,
) -> Optional[float]:
    if not bithumb_asks or not foreign_bids:
        return None
    if is_krw_exchange:
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
        return None
    foreign_bid_vwap = total_cost_foreign / total_qty
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
        return None
    bithumb_ask_vwap = total_cost_bithumb / total_qty
    if is_krw_exchange:
        return bithumb_ask_vwap / foreign_bid_vwap * 10_000
    return bithumb_ask_vwap / (foreign_bid_vwap * usdt_krw) * 10_000


def build_gap_result(
    ticker: str,
    bithumb_data: BithumbData,
    exchange_data_map: dict[str, ExchangeData],
) -> GapResult:
    bithumb_ask = bithumb_data.ask
    usdt_krw = bithumb_data.usdt_krw_last
    can_calculate = (
        bithumb_ask is not None and bithumb_ask > 0
        and usdt_krw is not None and usdt_krw > 0
    )
    updated: dict[str, ExchangeData] = {}
    for exchange_name, ex_data in exchange_data_map.items():
        spot_gap: Optional[float] = None
        futures_gap: Optional[float] = None
        is_krw = exchange_name in KRW_EXCHANGES
        can_calc_this = (
            (is_krw and bithumb_ask is not None and bithumb_ask > 0)
            or can_calculate
        )
        if can_calc_this:
            if ex_data.spot_bbo and ex_data.spot_bbo.bid is not None:
                try:
                    if is_krw:
                        spot_gap = calculate_gap_krw(ex_data.spot_bbo.bid, bithumb_ask)
                    else:
                        spot_gap = calculate_gap(ex_data.spot_bbo.bid, usdt_krw, bithumb_ask)
                except ZeroDivisionError:
                    spot_gap = None
            if ex_data.futures_bbo and ex_data.futures_bbo.bid is not None:
                try:
                    futures_gap = calculate_gap(ex_data.futures_bbo.bid, usdt_krw, bithumb_ask)
                except ZeroDivisionError:
                    futures_gap = None
        updated[exchange_name] = ExchangeData(
            exchange=ex_data.exchange,
            spot_bbo=ex_data.spot_bbo,
            futures_bbo=ex_data.futures_bbo,
            spot_supported=ex_data.spot_supported,
            futures_supported=ex_data.futures_supported,
            spot_gap=spot_gap,
            futures_gap=futures_gap,
        )
    return GapResult(
        ticker=ticker,
        timestamp=int(time.time()),
        bithumb=bithumb_data,
        exchanges=updated,
    )


# ====================================================================
# Binance bookTicker 파싱 미러 (JSON → dict[base, BBO])
# ====================================================================

def parse_binance_booktickers(spot_json: list[dict], futures_json: list[dict]) -> dict:
    result: dict[str, dict[str, Optional[BBO]]] = {}
    for item in spot_json:
        sym = item.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        base = sym[:-4]
        try:
            bid = float(item['bidPrice'])
            ask = float(item['askPrice'])
        except (KeyError, ValueError):
            continue
        if bid > 0 and ask > 0:
            result.setdefault(base, {'spot': None, 'futures': None})
            result[base]['spot'] = BBO(bid=bid, ask=ask)
    for item in futures_json:
        sym = item.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        base = sym[:-4]
        try:
            bid = float(item['bidPrice'])
            ask = float(item['askPrice'])
        except (KeyError, ValueError):
            continue
        if bid > 0 and ask > 0:
            result.setdefault(base, {'spot': None, 'futures': None})
            result[base]['futures'] = BBO(bid=bid, ask=ask)
    return result


# ====================================================================
# 시뮬 데이터 생성
# ====================================================================

def gen_binance_data(n_tickers: int = 500):
    """Binance bookTicker 응답 시뮬 (spot + futures JSON)."""
    rnd = random.Random(42)
    spot = []
    for i in range(n_tickers):
        base = f'C{i:04d}'
        bid = rnd.uniform(0.01, 100000)
        ask = bid * rnd.uniform(1.0001, 1.002)
        spot.append({'symbol': f'{base}USDT', 'bidPrice': f'{bid:.8f}', 'askPrice': f'{ask:.8f}'})
    futures = []
    for i in range(n_tickers // 2):
        base = f'C{i:04d}'
        bid = rnd.uniform(0.01, 100000)
        ask = bid * rnd.uniform(1.0001, 1.002)
        futures.append({'symbol': f'{base}USDT', 'bidPrice': f'{bid:.8f}', 'askPrice': f'{ask:.8f}'})
    return spot, futures


def gen_exchange_bbos(tickers: list[str], exchange_name: str) -> dict[str, dict[str, Optional[BBO]]]:
    """거래소 BBO 딕트 시뮬."""
    rnd = random.Random(hash(exchange_name) & 0xffffffff)
    out: dict[str, dict[str, Optional[BBO]]] = {}
    for t in tickers:
        bid = rnd.uniform(0.01, 100000)
        ask = bid * rnd.uniform(1.0001, 1.002)
        out[t] = {
            'spot': BBO(bid=bid, ask=ask),
            'futures': BBO(bid=bid * 1.001, ask=ask * 1.001) if exchange_name in {'binance','bybit','okx','bitget','gate','htx'} else None,
        }
    return out


def gen_orderbook(depth: int = 20, base_price: float = 30000) -> list[list[float]]:
    rnd = random.Random()
    out = []
    p = base_price
    for _ in range(depth):
        p = p * rnd.uniform(0.9999, 1.0005)
        q = rnd.uniform(0.01, 5.0)
        out.append([p, q])
    return out


# ====================================================================
# 벤치
# ====================================================================

def timeit(name: str, fn, warmup: int = 2, runs: int = 5) -> float:
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    median = statistics.median(ts)
    mn = min(ts)
    print(f'  {name:40s} median={median*1000:9.3f} ms   min={mn*1000:9.3f} ms')
    return median


def main():
    print('=== BBO loop Python 핫스팟 프로파일 ===\n')

    # 시뮬 데이터
    N_TICKERS = 500  # 빗썸 상장 ~500개
    tickers = [f'C{i:04d}' for i in range(N_TICKERS)]
    bithumb_bbos = {t: BBO(bid=random.uniform(0.01, 100000), ask=random.uniform(0.01, 100000)) for t in tickers}
    usdt_krw = 1380.5

    # Binance JSON 응답 시뮬
    spot_json, futures_json = gen_binance_data(N_TICKERS)

    # 각 거래소 BBO 시뮬
    exchange_bbos = {ex: gen_exchange_bbos(tickers, ex) for ex in ALL_EXCHANGES}

    print(f'[시뮬] N_TICKERS={N_TICKERS}, N_EXCHANGES={len(ALL_EXCHANGES)}\n')

    # 1. Binance JSON → BBO 파싱
    spot_json_str = json.dumps(spot_json)
    futures_json_str = json.dumps(futures_json)

    def bench_json_parse():
        sp = json.loads(spot_json_str)
        fu = json.loads(futures_json_str)
        return parse_binance_booktickers(sp, fu)

    def bench_parse_only():
        return parse_binance_booktickers(spot_json, futures_json)

    print('[1] Binance bookTicker JSON → BBO dict:')
    t_json = timeit('json.loads + parse (spot 500 + fut 250)', bench_json_parse)
    t_parse = timeit('parse only (no JSON decode)            ', bench_parse_only)

    # 2. build_gap_result 전체 루프 (500 tickers × 8 exchanges)
    def bench_build_all():
        for ticker in tickers:
            b_bbo = bithumb_bbos[ticker]
            bithumb_data = BithumbData(ask=b_bbo.ask, usdt_krw_last=usdt_krw)
            ex_data_map = {}
            for ex in ALL_EXCHANGES:
                ex_bbos = exchange_bbos[ex].get(ticker, {})
                ex_data_map[ex] = ExchangeData(
                    exchange=ex,
                    spot_bbo=ex_bbos.get('spot'),
                    futures_bbo=ex_bbos.get('futures'),
                    spot_supported=ex_bbos.get('spot') is not None,
                    futures_supported=ex_bbos.get('futures') is not None,
                )
            _ = build_gap_result(ticker, bithumb_data, ex_data_map)

    print('\n[2] build_gap_result for all tickers × all exchanges:')
    t_build = timeit('500 tickers × 8 ex build_gap_result     ', bench_build_all)

    # 3. calculate_impact_gap (per invocation, depth=20)
    b_asks = gen_orderbook(20, 30000)
    f_bids = gen_orderbook(20, 21.7)  # 1 BTC = 21.7 USDT? 그냥 숫자

    def bench_impact_1k():
        # impact gap은 기회 감지 시에만 호출 — 1000회 시뮬 (여러 유망 티커)
        for _ in range(1000):
            _ = calculate_impact_gap(b_asks, f_bids, usdt_krw, 10000.0, is_krw_exchange=False)

    print('\n[3] calculate_impact_gap (on-demand 기회탐지):')
    t_impact = timeit('1000회 (depth=20 orderbooks)           ', bench_impact_1k)

    # 4. 종합: 한 번의 BBO loop iteration이 CPU에서 얼마 소요?
    print('\n=== BBO loop iteration CPU 요소 추정 ===')
    # 한 iteration에 Binance JSON 1회 + 나머지 7 ex는 ccxt parse (측정 불가, 유사) + build_gap_result 1회
    # 매우 관대하게: t_json + 7 × t_json (ccxt 별도 파싱) + t_build + 기회 탐지 10회 평균
    # 하지만 실제로 Binance bookTicker 외 나머지 거래소는 ccxt가 C 익스텐션 없이 dict만 반환하므로 비용 비슷
    est_ccxt_parse_per_ex = t_json * 0.5  # 추정치 (JSON dict는 httpx가 해주고, ccxt는 필드 추출)
    total_cpu_per_loop = t_json + (len(ALL_EXCHANGES) - 1) * est_ccxt_parse_per_ex + t_build
    print(f'  추정 CPU 시간/iteration: {total_cpu_per_loop*1000:.2f} ms')
    print(f'  loop 주기 (BBO_POLL_INTERVAL): 3000 ms')
    cpu_ratio = total_cpu_per_loop / 3.0
    print(f'  CPU 비율 (3초 주기): {cpu_ratio*100:.3f}%')
    print(f'  → 나머지 {100 - cpu_ratio*100:.3f}% 가 HTTP wait + asyncio overhead')

    # 4-b. 단일 함수 처리량 (핫 path가 대체된다면 몇 x 빠르게?)
    print('\n[QPS] 단일 호출 처리량:')
    print(f'  build_gap_result for 500×8 = {1/t_build:.1f} / s  (1회 loop당)')
    print(f'  json parse + BBO dict     = {1/t_json:.1f} / s')
    print(f'  impact_gap (single call)  = {1000/(t_impact):.0f} / s')

    return {
        't_json': t_json,
        't_parse_only': t_parse,
        't_build': t_build,
        't_impact_1k': t_impact,
        'est_cpu_per_loop': total_cpu_per_loop,
    }


if __name__ == '__main__':
    m = main()
    print('\n=== 결론 ===')
    if m['est_cpu_per_loop'] < 0.05:  # 50ms 미만
        print('  CPU-bound 아님 (<50ms/iter). Rust 마이그 ROI 낮음.')
        print('  IO 최적화 우선: connection pooling, HTTP/2, 병렬 batch 조정.')
    elif m['est_cpu_per_loop'] < 0.3:
        print('  CPU 중간 (50-300ms). 부분 CPU 포팅 고려 가능 (parse + build_gap_result만).')
    else:
        print('  CPU-bound. Rust 전면 포팅 ROI 높음.')
