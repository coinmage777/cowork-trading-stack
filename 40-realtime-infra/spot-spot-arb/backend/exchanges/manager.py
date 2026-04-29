"""ccxt 기반 거래소 통합 관리자."""

import asyncio
import logging
import uuid
from typing import Any
import ccxt.async_support as ccxt
import httpx
import jwt

from backend.exchanges.types import BBO, FeatureSupport, NetworkInfo
from backend import config

logger = logging.getLogger(__name__)

COINONE_NETWORK_LABEL = '확인불가'

# 거래소 인스턴스 저장소
# key: "binance_spot", "binance_swap", "upbit_spot", etc.
_exchange_instances: dict[str, ccxt.Exchange] = {}
_loan_supported_assets: dict[str, set[str] | None] = {}

# 현물 전용 거래소 (선물 없음)
SPOT_ONLY_EXCHANGES = {'upbit', 'coinone'}

# KRW 기반 국내 거래소
KRW_EXCHANGES = {'upbit', 'coinone'}

# 현물 마진 미지원 거래소
NO_SPOT_MARGIN_EXCHANGES = {'upbit', 'coinone'}

# Crypto loan 미지원 거래소
NO_CRYPTO_LOAN_EXCHANGES = {'upbit', 'coinone'}

# Crypto loan API를 아직 구분하지 못한 거래소
UNKNOWN_CRYPTO_LOAN_EXCHANGES = {'htx'}

# 지원 거래소 목록
ALL_EXCHANGES = ['binance', 'bybit', 'okx', 'bitget', 'gate', 'htx', 'upbit', 'coinone']


def get_symbol(ticker: str, market_type: str, exchange_id: str = '') -> str:
    """티커와 마켓 타입으로 ccxt 심볼 반환.

    Args:
        ticker: 예) "BTC"
        market_type: "spot" 또는 "swap"
        exchange_id: 거래소 ID (국내 거래소는 KRW 페어 사용)

    Returns:
        "BTC/KRW" for domestic spot, "BTC/USDT" for foreign spot,
        "BTC/USDT:USDT" for swap
    """
    if market_type == 'swap':
        return f'{ticker}/USDT:USDT'
    if exchange_id in KRW_EXCHANGES:
        return f'{ticker}/KRW'
    return f'{ticker}/USDT'


def _create_exchange_instance(exchange_id: str, market_type: str) -> ccxt.Exchange:
    """거래소 인스턴스 생성."""
    credentials = config.EXCHANGE_CREDENTIALS.get(exchange_id, {})
    options: dict = {}

    if market_type == 'swap':
        options['defaultType'] = 'swap'
    else:
        options['defaultType'] = 'spot'

    params = {
        **credentials,
        'options': options,
        'enableRateLimit': True,
    }

    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class(params)


async def _close_failed_instance(instance: ccxt.Exchange, label: str) -> None:
    try:
        await instance.close()
    except Exception as exc:
        logger.debug('Failed to close %s after init error: %s', label, exc)


async def init_exchanges() -> None:
    """모든 거래소 인스턴스를 생성하고 마켓 정보를 로드한다."""
    global _exchange_instances

    for exchange_id in ALL_EXCHANGES:
        # 업비트는 직접 API 사용 (ccxt 불필요)
        if exchange_id == 'upbit':
            logger.info('Skipping ccxt for upbit (using direct API)')
            continue

        # 현물 인스턴스
        spot_instance: ccxt.Exchange | None = None
        try:
            spot_instance = _create_exchange_instance(exchange_id, 'spot')
            await spot_instance.load_markets()
            _exchange_instances[f'{exchange_id}_spot'] = spot_instance
            logger.info('Initialized %s spot', exchange_id)
        except Exception as exc:
            logger.error('Failed to init %s spot: %s', exchange_id, exc)
            if spot_instance is not None:
                await _close_failed_instance(spot_instance, f'{exchange_id}_spot')

        # 선물 인스턴스 (지원하는 거래소만)
        if exchange_id in config.EXCHANGES_WITH_FUTURES:
            swap_instance: ccxt.Exchange | None = None
            try:
                swap_instance = _create_exchange_instance(exchange_id, 'swap')
                await swap_instance.load_markets()
                _exchange_instances[f'{exchange_id}_swap'] = swap_instance
                logger.info('Initialized %s swap', exchange_id)
            except Exception as exc:
                logger.error('Failed to init %s swap: %s', exchange_id, exc)
                if swap_instance is not None:
                    await _close_failed_instance(swap_instance, f'{exchange_id}_swap')

    try:
        await refresh_exchange_loan_cache()
    except Exception as exc:
        logger.warning('Initial crypto loan cache refresh failed: %s', exc)


async def close_exchanges() -> None:
    """모든 거래소 인스턴스를 닫는다."""
    for key, instance in list(_exchange_instances.items()):
        try:
            await instance.close()
            logger.info('Closed exchange instance: %s', key)
        except Exception as exc:
            logger.error('Error closing %s: %s', key, exc)
    _exchange_instances.clear()
    _loan_supported_assets.clear()


def get_instance(exchange_id: str, market_type: str) -> ccxt.Exchange | None:
    """특정 거래소 인스턴스를 반환한다."""
    key = f'{exchange_id}_{market_type}'
    instance = _exchange_instances.get(key)
    if instance is not None:
        return instance

    # Upbit public market data is fetched via direct HTTP in this project,
    # but private close-detection still needs an authenticated ccxt client.
    if exchange_id == 'upbit' and market_type == 'spot':
        try:
            instance = _create_exchange_instance(exchange_id, market_type)
        except Exception as exc:
            logger.error('Failed to lazily init %s: %s', key, exc)
            return None
        _exchange_instances[key] = instance
        logger.info('Initialized %s lazily', key)
        return instance

    return None


def get_all_instances() -> dict[str, ccxt.Exchange]:
    """모든 거래소 인스턴스를 반환한다."""
    return dict(_exchange_instances)


def _extract_supported_assets(rows: object, field_name: str) -> set[str]:
    supported_assets: set[str] = set()
    if not isinstance(rows, list):
        return supported_assets

    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_value = row.get(field_name)
        if raw_value is None:
            continue
        asset = str(raw_value).strip().upper()
        if asset:
            supported_assets.add(asset)
    return supported_assets


async def _fetch_binance_loan_supported_assets() -> set[str] | None:
    instance = _exchange_instances.get('binance_spot')
    if instance is None:
        return None
    payload = await instance.sapiV2GetLoanFlexibleLoanableData()
    rows = payload if isinstance(payload, list) else payload.get('rows', payload.get('data', []))
    return _extract_supported_assets(rows, 'loanCoin')


async def _fetch_bybit_loan_supported_assets() -> set[str] | None:
    instance = _exchange_instances.get('bybit_spot')
    if instance is None:
        return None
    payload = await instance.publicGetV5CryptoLoanCommonLoanableData()
    rows = payload.get('result', {}).get('list', [])
    assets = _extract_supported_assets(rows, 'currency')
    if assets:
        return assets
    return _extract_supported_assets(rows, 'loanCurrency')


async def _fetch_okx_loan_supported_assets() -> set[str] | None:
    instance = _exchange_instances.get('okx_spot')
    if instance is None:
        return None
    payload = await instance.privateGetFinanceFlexibleLoanBorrowCurrencies()
    rows = payload.get('data', []) if isinstance(payload, dict) else payload
    return _extract_supported_assets(rows, 'borrowCcy')


async def _fetch_bitget_loan_supported_assets() -> set[str] | None:
    instance = _exchange_instances.get('bitget_spot')
    if instance is None:
        return None
    payload = await instance.publicEarnGetV2EarnLoanPublicCoininfos()
    data = payload.get('data', payload) if isinstance(payload, dict) else payload
    if not isinstance(data, dict):
        return None
    return _extract_supported_assets(data.get('loanInfos', []), 'coin')


async def _fetch_gate_loan_supported_assets() -> set[str] | None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get('https://api.gateio.ws/api/v4/unified/currencies')
        response.raise_for_status()
        rows = response.json()

    if not isinstance(rows, list):
        return None

    supported_assets: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get('loan_status', '')).strip().lower() != 'enable':
            continue
        asset = str(row.get('name', '')).strip().upper()
        if asset:
            supported_assets.add(asset)
    return supported_assets


async def _fetch_crypto_loan_supported_assets(exchange_id: str) -> set[str] | None:
    if exchange_id in NO_CRYPTO_LOAN_EXCHANGES:
        return set()
    if exchange_id in UNKNOWN_CRYPTO_LOAN_EXCHANGES:
        return None

    if exchange_id == 'binance':
        return await _fetch_binance_loan_supported_assets()
    if exchange_id == 'bybit':
        return await _fetch_bybit_loan_supported_assets()
    if exchange_id == 'okx':
        return await _fetch_okx_loan_supported_assets()
    if exchange_id == 'bitget':
        return await _fetch_bitget_loan_supported_assets()
    if exchange_id == 'gate':
        return await _fetch_gate_loan_supported_assets()
    return None


async def refresh_exchange_loan_cache() -> None:
    tasks = {
        exchange_id: asyncio.create_task(_fetch_crypto_loan_supported_assets(exchange_id))
        for exchange_id in ALL_EXCHANGES
    }
    await asyncio.gather(*tasks.values(), return_exceptions=True)

    for exchange_id, task in tasks.items():
        try:
            result = task.result()
        except Exception as exc:
            logger.warning('Failed to refresh %s crypto loan cache: %s', exchange_id, exc)
            _loan_supported_assets[exchange_id] = None
            continue

        _loan_supported_assets[exchange_id] = result
        if result is None:
            logger.info('Crypto loan cache for %s is unavailable', exchange_id)
        else:
            logger.info('Crypto loan cache refreshed for %s: %d assets', exchange_id, len(result))


def get_spot_margin_info(exchange_id: str, ticker: str) -> FeatureSupport:
    """거래소의 특정 spot 심볼에 대한 margin 지원 여부를 반환한다."""
    normalized_exchange = str(exchange_id or '').strip().lower()
    normalized_ticker = str(ticker or '').strip().upper()
    if not normalized_exchange or not normalized_ticker:
        return FeatureSupport()

    if normalized_exchange in NO_SPOT_MARGIN_EXCHANGES:
        return FeatureSupport(supported=False)

    spot_instance = _exchange_instances.get(f'{normalized_exchange}_spot')
    if spot_instance is None:
        return FeatureSupport()

    symbol = get_symbol(normalized_ticker, 'spot', normalized_exchange)
    market = spot_instance.markets.get(symbol)
    if not isinstance(market, dict):
        return FeatureSupport(supported=False)

    market_margin = market.get('margin')
    if market_margin is not None:
        return FeatureSupport(supported=bool(market_margin))

    info = market.get('info')
    if isinstance(info, dict):
        if normalized_exchange == 'bybit':
            margin_trading = str(info.get('marginTrading', '')).strip().lower()
            if margin_trading:
                return FeatureSupport(supported=margin_trading != 'none')

        if normalized_exchange == 'okx':
            inst_type = str(info.get('instType', '')).strip().upper()
            if inst_type:
                return FeatureSupport(supported=inst_type == 'MARGIN')

        if normalized_exchange == 'gate':
            leverage = _to_positive_float(info.get('leverage'))
            if leverage is not None:
                return FeatureSupport(supported=leverage > 0)

    has_margin = spot_instance.has.get('margin')
    if has_margin is not None:
        return FeatureSupport(supported=bool(has_margin))
    return FeatureSupport()


def get_crypto_loan_info(exchange_id: str, ticker: str) -> FeatureSupport:
    """거래소의 특정 코인에 대한 crypto loan 지원 여부를 반환한다."""
    normalized_exchange = str(exchange_id or '').strip().lower()
    normalized_ticker = str(ticker or '').strip().upper()
    if not normalized_exchange or not normalized_ticker:
        return FeatureSupport()

    if normalized_exchange in NO_CRYPTO_LOAN_EXCHANGES:
        return FeatureSupport(supported=False)

    supported_assets = _loan_supported_assets.get(normalized_exchange)
    if supported_assets is None:
        return FeatureSupport()
    return FeatureSupport(supported=normalized_ticker in supported_assets)


def _to_positive_float(value: object) -> float | None:
    """숫자형 값을 양수 float로 변환한다."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def _extract_bid_ask_from_ticker_data(
    ticker_data: dict,
) -> tuple[float | None, float | None]:
    """ccxt ticker payload에서 bid/ask를 안전하게 추출한다.

    일부 거래소(예: coinone)는 bid/ask 필드를 비워두고 raw info에 best_bids/
    best_asks만 제공할 수 있어, 해당 경우를 폴백으로 처리한다.
    """
    bid = _to_positive_float(ticker_data.get('bid'))
    ask = _to_positive_float(ticker_data.get('ask'))
    if bid is not None and ask is not None:
        return bid, ask

    info = ticker_data.get('info')
    if not isinstance(info, dict):
        return bid, ask

    if bid is None:
        best_bids = info.get('best_bids')
        if isinstance(best_bids, list) and best_bids:
            top_bid = best_bids[0]
            if isinstance(top_bid, dict):
                bid = _to_positive_float(top_bid.get('price'))
            elif isinstance(top_bid, (list, tuple)) and top_bid:
                bid = _to_positive_float(top_bid[0])
        if bid is None:
            bid = _to_positive_float(info.get('best_bid') or info.get('bid'))

    if ask is None:
        best_asks = info.get('best_asks')
        if isinstance(best_asks, list) and best_asks:
            top_ask = best_asks[0]
            if isinstance(top_ask, dict):
                ask = _to_positive_float(top_ask.get('price'))
            elif isinstance(top_ask, (list, tuple)) and top_ask:
                ask = _to_positive_float(top_ask[0])
        if ask is None:
            ask = _to_positive_float(info.get('best_ask') or info.get('ask'))

    return bid, ask


async def fetch_bbo(exchange_instance: ccxt.Exchange, symbol: str) -> BBO | None:
    """거래소에서 BBO(Best Bid/Offer)를 가져온다.

    fetch_ticker()를 먼저 시도하고, bid/ask가 없으면 fetch_order_book()으로 폴백.

    Args:
        exchange_instance: ccxt 거래소 인스턴스
        symbol: ccxt 심볼 (예: "BTC/USDT" 또는 "BTC/USDT:USDT")

    Returns:
        BBO 인스턴스 또는 None (실패 시)
    """
    try:
        ticker = await exchange_instance.fetch_ticker(symbol)
        bid = ticker.get('bid')
        ask = ticker.get('ask')
        ts = ticker.get('timestamp')

        # bid/ask가 없으면 오더북으로 폴백 (limit=5: 바이낸스 선물은 1 미지원)
        if bid is None or ask is None:
            ob = await exchange_instance.fetch_order_book(symbol, limit=5)
            bids = ob.get('bids', [])
            asks = ob.get('asks', [])
            bid = bids[0][0] if bids else None
            ask = asks[0][0] if asks else None
            ts = ob.get('timestamp') or ts

        return BBO(bid=bid, ask=ask, timestamp=ts)

    except Exception as exc:
        logger.debug(
            'fetch_bbo failed for %s on %s: %s',
            symbol,
            exchange_instance.id,
            exc,
        )
        return None


async def fetch_network_info(
    exchange_instance: ccxt.Exchange, currency: str
) -> list[NetworkInfo]:
    """거래소에서 특정 통화의 네트워크별 입출금 정보를 가져온다.

    Args:
        exchange_instance: ccxt 거래소 인스턴스
        currency: 통화 코드 (예: "BTC")

    Returns:
        NetworkInfo 리스트 (실패 시 빈 리스트)
    """
    try:
        currencies = await exchange_instance.fetch_currencies()
        currency_data = currencies.get(currency)
        if not currency_data:
            return []

        networks_raw = currency_data.get('networks') or {}
        result: list[NetworkInfo] = []

        if networks_raw:
            for network_id, net_data in networks_raw.items():
                info = NetworkInfo(
                    network=network_id,
                    deposit=bool(net_data.get('deposit', False)),
                    withdraw=bool(net_data.get('withdraw', False)),
                    fee=net_data.get('fee'),
                    min_withdraw=net_data.get('limits', {}).get('withdraw', {}).get('min'),
                )
                result.append(info)
        else:
            # 네트워크 정보가 없는 경우 통화 레벨 정보 사용
            deposit = currency_data.get('deposit')
            withdraw = currency_data.get('withdraw')
            # deposit/withdraw 둘 다 None이면 정보가 없는 것이므로 스킵
            if deposit is None and withdraw is None:
                return []
            info = NetworkInfo(
                network='메인넷',
                deposit=bool(deposit),
                withdraw=bool(withdraw),
                fee=currency_data.get('fee'),
            )
            result.append(info)

        return result

    except Exception as exc:
        logger.debug(
            'fetch_network_info failed for %s on %s: %s',
            currency,
            exchange_instance.id,
            exc,
        )
        return []


async def fetch_upbit_network_info(currency: str) -> list[NetworkInfo]:
    """업비트 전용 네트워크 정보 조회 (/v1/status/wallet API).

    ccxt에서 fetchCurrencies를 지원하지 않으므로 직접 API를 호출한다.

    Args:
        currency: 통화 코드 (예: "BTC")

    Returns:
        NetworkInfo 리스트 (실패 시 빈 리스트)
    """
    access_key = config.EXCHANGE_CREDENTIALS.get('upbit', {}).get('apiKey', '')
    secret_key = config.EXCHANGE_CREDENTIALS.get('upbit', {}).get('secret', '')

    if not access_key or not secret_key:
        return []

    try:
        payload = {
            'access_key': access_key,
            'nonce': str(uuid.uuid4()),
        }
        token = jwt.encode(payload, secret_key)

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                'https://api.upbit.com/v1/status/wallet',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            data = resp.json()

        if not isinstance(data, list):
            return []

        result: list[NetworkInfo] = []
        for item in data:
            if item.get('currency') != currency:
                continue
            wallet_state = item.get('wallet_state', '')
            net_type = item.get('net_type', currency)
            result.append(NetworkInfo(
                network=net_type,
                deposit=wallet_state in ('working', 'deposit_only'),
                withdraw=wallet_state in ('working', 'withdraw_only'),
                fee=None,
            ))

        return result

    except Exception as exc:
        logger.debug('fetch_upbit_network_info failed for %s: %s', currency, exc)
        return []


async def fetch_coinone_network_info(currency: str) -> list[NetworkInfo]:
    """코인원 전용 네트워크 정보 조회 (공개 API).

    코인원은 네트워크명을 제공하지 않으므로 입출금 상태와 수수료만 조회한다.

    Args:
        currency: 통화 코드 (예: "BTC")

    Returns:
        NetworkInfo 리스트 (실패 시 빈 리스트)
    """
    try:
        url = f'https://api.coinone.co.kr/public/v2/currencies/{currency}'
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            data = resp.json()

        if data.get('result') != 'success':
            return []

        parsed = _parse_coinone_networks(data.get('currencies', []))
        return parsed.get(currency.strip().upper(), [])

    except Exception as exc:
        logger.debug('fetch_coinone_network_info failed for %s: %s', currency, exc)
        return []


def _parse_coinone_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_coinone_networks(currencies: Any) -> dict[str, list[NetworkInfo]]:
    result: dict[str, list[NetworkInfo]] = {}
    if not isinstance(currencies, list):
        return result

    for item in currencies:
        if not isinstance(item, dict):
            continue

        code = str(item.get('symbol') or '').strip().upper()
        if not code:
            continue

        result[code] = [NetworkInfo(
            network=COINONE_NETWORK_LABEL,
            deposit=str(item.get('deposit_status', '')).strip().lower() == 'normal',
            withdraw=str(item.get('withdraw_status', '')).strip().lower() == 'normal',
            fee=_parse_coinone_number(item.get('withdrawal_fee')),
            min_withdraw=_parse_coinone_number(item.get('withdrawal_min_amount')),
        )]

    return result


async def _fetch_all_coinone_network_info() -> dict[str, list[NetworkInfo]]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get('https://api.coinone.co.kr/public/v2/currencies')
            data = resp.json()

        if data.get('result') != 'success':
            return {}

        return _parse_coinone_networks(data.get('currencies', []))
    except Exception as exc:
        logger.debug('_fetch_all_coinone_network_info failed: %s', exc)
        return {}


# ------------------------------------------------------------------
# Bulk 조회 함수 (fetch_tickers / fetch_currencies 1회 호출)
# ------------------------------------------------------------------

async def fetch_all_bbos(exchange_name: str) -> dict[str, dict[str, BBO | None]]:
    """거래소의 전체 ticker BBO를 일괄 조회한다.

    Binance: bookTicker API 직접 호출 (weight 4, fetch_tickers의 weight 100 대비 25배 경량)
    기타 거래소: ccxt fetch_tickers() 사용

    Args:
        exchange_name: 거래소 ID (예: "binance")

    Returns:
        {ticker: {"spot": BBO|None, "futures": BBO|None}} 매핑
    """
    if exchange_name == 'binance':
        return await _fetch_all_binance_bbos()
    if exchange_name == 'upbit':
        return await _fetch_all_upbit_bbos()

    result: dict[str, dict[str, BBO | None]] = {}
    is_krw = exchange_name in KRW_EXCHANGES
    quote = 'KRW' if is_krw else 'USDT'

    # 현물
    spot_instance = _exchange_instances.get(f'{exchange_name}_spot')
    if spot_instance:
        try:
            tickers = await spot_instance.fetch_tickers()
            for symbol, data in tickers.items():
                if f'/{quote}' not in symbol:
                    continue
                # swap 심볼 제외 (예: BTC/USDT:USDT)
                if ':' in symbol:
                    continue
                base = symbol.split('/')[0]
                bid, ask = _extract_bid_ask_from_ticker_data(data)
                ts = data.get('timestamp')
                bbo = BBO(bid=bid, ask=ask, timestamp=ts) if bid is not None and ask is not None else None
                result.setdefault(base, {'spot': None, 'futures': None})
                result[base]['spot'] = bbo
        except Exception as exc:
            logger.error('fetch_all_bbos spot failed for %s: %s', exchange_name, exc)

    # 선물 (지원 거래소만)
    if exchange_name in config.EXCHANGES_WITH_FUTURES:
        swap_instance = _exchange_instances.get(f'{exchange_name}_swap')
        if swap_instance:
            try:
                tickers = await swap_instance.fetch_tickers()
                for symbol, data in tickers.items():
                    if '/USDT:USDT' not in symbol:
                        continue
                    base = symbol.split('/')[0]
                    bid, ask = _extract_bid_ask_from_ticker_data(data)
                    ts = data.get('timestamp')
                    bbo = BBO(bid=bid, ask=ask, timestamp=ts) if bid is not None and ask is not None else None
                    result.setdefault(base, {'spot': None, 'futures': None})
                    result[base]['futures'] = bbo
            except Exception as exc:
                logger.error('fetch_all_bbos swap failed for %s: %s', exchange_name, exc)

    return result


async def _fetch_all_binance_bbos() -> dict[str, dict[str, BBO | None]]:
    """Binance bookTicker API로 BBO를 일괄 조회한다.

    GET /api/v3/ticker/bookTicker (weight 4) — spot
    GET /fapi/v1/ticker/bookTicker (weight 2) — futures
    ccxt fetch_tickers() (weight 100) 대비 25배 경량.
    """
    result: dict[str, dict[str, BBO | None]] = {}

    async with httpx.AsyncClient(timeout=10.0) as client:
        # spot + futures 병렬 호출
        spot_resp, futures_resp = await asyncio.gather(
            client.get('https://api.binance.com/api/v3/ticker/bookTicker'),
            client.get('https://fapi.binance.com/fapi/v1/ticker/bookTicker'),
            return_exceptions=True,
        )

        # Spot 파싱
        if not isinstance(spot_resp, Exception):
            try:
                for item in spot_resp.json():
                    sym = item.get('symbol', '')
                    if not sym.endswith('USDT'):
                        continue
                    base = sym[:-4]  # "BTCUSDT" -> "BTC"
                    bid = float(item['bidPrice'])
                    ask = float(item['askPrice'])
                    if bid > 0 and ask > 0:
                        result.setdefault(base, {'spot': None, 'futures': None})
                        result[base]['spot'] = BBO(bid=bid, ask=ask)
            except Exception as exc:
                logger.error('Binance spot bookTicker parse error: %s', exc)
        else:
            logger.error('Binance spot bookTicker request failed: %s', spot_resp)

        # Futures 파싱
        if not isinstance(futures_resp, Exception):
            try:
                for item in futures_resp.json():
                    sym = item.get('symbol', '')
                    if not sym.endswith('USDT'):
                        continue
                    base = sym[:-4]
                    bid = float(item['bidPrice'])
                    ask = float(item['askPrice'])
                    if bid > 0 and ask > 0:
                        result.setdefault(base, {'spot': None, 'futures': None})
                        result[base]['futures'] = BBO(bid=bid, ask=ask)
            except Exception as exc:
                logger.error('Binance futures bookTicker parse error: %s', exc)
        else:
            logger.error('Binance futures bookTicker request failed: %s', futures_resp)

    return result


async def _fetch_all_upbit_bbos() -> dict[str, dict[str, BBO | None]]:
    """업비트 직접 API로 BBO를 일괄 조회한다.

    ccxt를 사용하지 않고 업비트 REST API를 직접 호출한다.
    1. /v1/market/all — KRW 마켓 목록 조회
    2. /v1/orderbook — 배치 오더북 조회 (1호가 bid/ask)
    """
    result: dict[str, dict[str, BBO | None]] = {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. KRW 마켓 목록 조회
        try:
            market_resp = await client.get(
                'https://api.upbit.com/v1/market/all',
                params={'is_details': 'false'},
            )
            if market_resp.status_code != 200:
                logger.error('Upbit market/all failed: %d', market_resp.status_code)
                return result

            krw_markets: list[str] = []
            for item in market_resp.json():
                market = item.get('market', '')
                if market.startswith('KRW-'):
                    krw_markets.append(market)

        except Exception as exc:
            logger.error('Upbit market/all error: %s', exc)
            return result

        if not krw_markets:
            return result

        # 2. 배치로 orderbook 조회 (한 번에 최대 100개)
        BATCH_SIZE = 100
        for i in range(0, len(krw_markets), BATCH_SIZE):
            batch = krw_markets[i:i + BATCH_SIZE]
            try:
                resp = await client.get(
                    'https://api.upbit.com/v1/orderbook',
                    params={'markets': ','.join(batch)},
                )
                if resp.status_code != 200:
                    logger.error('Upbit orderbook batch failed: %d', resp.status_code)
                    continue

                for item in resp.json():
                    market = item.get('market', '')  # "KRW-BTC"
                    if not market.startswith('KRW-'):
                        continue
                    base = market[4:]  # "BTC"
                    units = item.get('orderbook_units', [])
                    if not units:
                        continue
                    ask = units[0].get('ask_price')
                    bid = units[0].get('bid_price')
                    ts = item.get('timestamp')
                    if bid is not None and ask is not None and bid > 0 and ask > 0:
                        result.setdefault(base, {'spot': None, 'futures': None})
                        result[base]['spot'] = BBO(bid=bid, ask=ask, timestamp=ts)

            except Exception as exc:
                logger.error('Upbit orderbook batch error: %s', exc)

    return result


async def fetch_orderbook(
    exchange_name: str, ticker: str, market_type: str = 'spot', depth: int = 5,
) -> list[list[float]]:
    """거래소 bid 오더북 조회.

    Binance: httpx 직접 호출 (/api/v3/depth, weight 5)
    기타: ccxt fetch_order_book(symbol, limit=depth)

    Args:
        exchange_name: 거래소 ID (예: "binance")
        ticker: 예) "BTC"
        market_type: "spot" 또는 "swap"
        depth: 호가 깊이 (기본 5)

    Returns:
        [[price, qty], ...] bid 호가 리스트 (높은 가격순).
        실패 시 빈 리스트.
    """
    is_krw = exchange_name in KRW_EXCHANGES

    # Upbit: 직접 API 호출
    if exchange_name == 'upbit':
        try:
            market = f'KRW-{ticker}'
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    'https://api.upbit.com/v1/orderbook',
                    params={'markets': market},
                )
                resp.raise_for_status()
                data = resp.json()

            bids: list[list[float]] = []
            if data and isinstance(data, list):
                units = data[0].get('orderbook_units', [])
                for entry in units[:depth]:
                    price = float(entry['bid_price'])
                    qty = float(entry['bid_size'])
                    bids.append([price, qty])

            return bids

        except Exception as exc:
            logger.debug('fetch_orderbook (upbit) failed for %s: %s', ticker, exc)
            return []

    # Binance: httpx 직접 호출 (weight 5, ccxt보다 경량)
    if exchange_name == 'binance':
        try:
            if market_type == 'swap':
                base_url = 'https://fapi.binance.com/fapi/v1/depth'
                symbol = f'{ticker}USDT'
            else:
                base_url = 'https://api.binance.com/api/v3/depth'
                symbol = f'{ticker}USDT'

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(base_url, params={'symbol': symbol, 'limit': depth})
                resp.raise_for_status()
                data = resp.json()

            bids: list[list[float]] = []
            for entry in data.get('bids', []):
                price = float(entry[0])
                qty = float(entry[1])
                bids.append([price, qty])

            return bids  # Binance는 이미 높은 가격순

        except Exception as exc:
            logger.debug('fetch_orderbook (binance) failed for %s: %s', ticker, exc)
            return []

    # 기타 거래소: ccxt fetch_order_book
    instance = _exchange_instances.get(f'{exchange_name}_{market_type}')
    if not instance:
        return []

    symbol = get_symbol(ticker, market_type, exchange_name)
    try:
        ob = await instance.fetch_order_book(symbol, limit=depth)
        bids: list[list[float]] = []
        for entry in ob.get('bids', []):
            price = float(entry[0])
            qty = float(entry[1])
            bids.append([price, qty])
        return bids  # ccxt는 이미 높은 가격순

    except Exception as exc:
        logger.debug('fetch_orderbook (%s) failed for %s: %s', exchange_name, ticker, exc)
        return []


async def fetch_all_network_info(exchange_name: str) -> dict[str, list[NetworkInfo]]:
    """거래소의 전체 통화 네트워크 정보를 일괄 조회한다.

    업비트/코인원은 전용 bulk API를 사용한다.

    Args:
        exchange_name: 거래소 ID (예: "binance")

    Returns:
        {currency: [NetworkInfo, ...]} 매핑
    """
    # 업비트: wallet status API (전체 조회)
    if exchange_name == 'upbit':
        return await _fetch_all_upbit_network_info()

    # 코인원: 공개 currencies API로 전체 입출금 상태를 조회한다.
    if exchange_name == 'coinone':
        return await _fetch_all_coinone_network_info()

    # 해외 거래소: fetch_currencies() 1회 호출
    spot_instance = _exchange_instances.get(f'{exchange_name}_spot')
    if not spot_instance:
        return {}

    try:
        currencies = await spot_instance.fetch_currencies()
        result: dict[str, list[NetworkInfo]] = {}

        for currency_code, currency_data in currencies.items():
            networks_raw = currency_data.get('networks') or {}
            infos: list[NetworkInfo] = []

            if networks_raw:
                for network_id, net_data in networks_raw.items():
                    infos.append(NetworkInfo(
                        network=network_id,
                        deposit=bool(net_data.get('deposit', False)),
                        withdraw=bool(net_data.get('withdraw', False)),
                        fee=net_data.get('fee'),
                        min_withdraw=net_data.get('limits', {}).get('withdraw', {}).get('min'),
                    ))
            else:
                deposit = currency_data.get('deposit')
                withdraw = currency_data.get('withdraw')
                if deposit is None and withdraw is None:
                    continue
                infos.append(NetworkInfo(
                    network='메인넷',
                    deposit=bool(deposit),
                    withdraw=bool(withdraw),
                    fee=currency_data.get('fee'),
                ))

            if infos:
                result[currency_code] = infos

        return result

    except Exception as exc:
        logger.error('fetch_all_network_info failed for %s: %s', exchange_name, exc)
        return {}


async def _fetch_all_upbit_network_info() -> dict[str, list[NetworkInfo]]:
    """업비트 wallet status API로 전체 통화 네트워크 정보를 일괄 조회한다."""
    access_key = config.EXCHANGE_CREDENTIALS.get('upbit', {}).get('apiKey', '')
    secret_key = config.EXCHANGE_CREDENTIALS.get('upbit', {}).get('secret', '')

    if not access_key or not secret_key:
        return {}

    try:
        payload = {
            'access_key': access_key,
            'nonce': str(uuid.uuid4()),
        }
        token = jwt.encode(payload, secret_key)

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                'https://api.upbit.com/v1/status/wallet',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
            )
            data = resp.json()

        if not isinstance(data, list):
            return {}

        result: dict[str, list[NetworkInfo]] = {}
        for item in data:
            currency = item.get('currency', '')
            if not currency:
                continue
            wallet_state = item.get('wallet_state', '')
            net_type = item.get('net_type', currency)
            info = NetworkInfo(
                network=net_type,
                deposit=wallet_state in ('working', 'deposit_only'),
                withdraw=wallet_state in ('working', 'withdraw_only'),
                fee=None,
            )
            result.setdefault(currency, []).append(info)

        return result

    except Exception as exc:
        logger.debug('_fetch_all_upbit_network_info failed: %s', exc)
        return {}
