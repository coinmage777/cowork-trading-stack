"""??щ쑞 ?袁⑹뒠 筌뤴뫀諭???BBO, USDT/KRW, ?곗뮄???뺣즲 鈺곌퀬??"""

import logging
import math
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
import ccxt.async_support as ccxt

from backend.exchanges.types import BBO, NetworkInfo, WithdrawalLimit
from backend import config

logger = logging.getLogger(__name__)

# ??щ쑞 ccxt ?紐꾨뮞??곷뮞 (?⑤벊?)
_bithumb: ccxt.bithumb | None = None

# ??щ쑞 net_type 筌?Ŋ??(wallet status API?癒?퐣 鈺곌퀬??
_bithumb_net_types_cache: dict[str, list[str]] = {}
_bithumb_wallet_cache: list[dict] = []
_bithumb_cache_ts: float = 0
_BITHUMB_CACHE_TTL = 300  # 5??
_PUBLIC_API_TIMEOUT = 10.0


async def _fetch_public_json(path: str) -> dict[str, Any] | None:
    """Fetch a Bithumb public API payload and return its JSON body."""
    url = f'https://api.bithumb.com{path}'
    try:
        async with httpx.AsyncClient(timeout=_PUBLIC_API_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        logger.warning('Bithumb public API request failed (%s): %s', path, exc)
        return None

    if not isinstance(payload, dict):
        logger.warning('Bithumb public API returned non-dict payload (%s)', path)
        return None

    status = str(payload.get('status', '')).strip()
    if status != '0000':
        logger.warning(
            'Bithumb public API returned bad status (%s): %s',
            path,
            status or '<missing>',
        )
        return None

    return payload

def _parse_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _clean_optional_tag(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.upper() in {'NONE', 'NULL', 'N/A', 'NA', '-'}:
        return None
    return text


def _normalize_receiver_type(value: str | None) -> str | None:
    cleaned = _clean_optional_tag(value)
    if cleaned is None:
        return None

    lowered = cleaned.strip().lower()
    if lowered in {'personal', 'individual', 'person', 'private', 'user'}:
        return 'personal'
    if lowered in {
        'corporation', 'corp', 'corporate', 'company', 'business', 'enterprise'
    }:
        return 'corporation'
    return lowered


def _normalize_exchange_name(value: str | None) -> str:
    text = str(value or '').strip().lower()
    if not text:
        return ''

    alias = {
        'gate': 'gateio',
        'gate.io': 'gateio',
        'gateio': 'gateio',
        'htx': 'huobi',
        'huobi': 'huobi',
    }
    return alias.get(text, text)


def _exchange_name_candidates(value: str | None) -> list[str]:
    normalized = _normalize_exchange_name(value)
    raw = str(value or '').strip().lower()

    candidates: list[str] = []

    def _add(name: str) -> None:
        text = name.strip()
        if text and text not in candidates:
            candidates.append(text)

    _add(normalized)

    if raw in {'gate', 'gate.io', 'gateio'} or normalized == 'gateio':
        _add('gate.io')
        _add('gate')
    elif raw in {'htx', 'huobi'} or normalized == 'huobi':
        _add('htx')

    return candidates


def _is_exchange_lookup_request_fail(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 400:
        return False
    try:
        payload = exc.response.json()
        if not isinstance(payload, dict):
            return False
        error = payload.get('error')
        if not isinstance(error, dict):
            return False
        name = str(error.get('name', '')).strip().lower()
        message = str(error.get('message', '')).strip()
        if name != 'request_fail':
            return False

        # Only retry without exchange_name when the failure is clearly about
        # exchange lookup. Other request_fail cases (e.g. unregistered address)
        # must surface as-is.
        message_lower = message.lower()
        lookup_markers = (
            '거래소 정보 조회',
            '거래소 정보',
            'exchange info',
            'exchange information',
            'exchange lookup',
        )
        return any(marker in message_lower for marker in lookup_markers)
    except Exception:
        return False


def _missing_required_receiver_fields_for_exchange_withdraw(
    receiver_type: str | None,
    receiver_ko_name: str | None,
    receiver_en_name: str | None,
    receiver_corp_ko_name: str | None,
    receiver_corp_en_name: str | None,
) -> list[str]:
    missing: list[str] = []
    if receiver_type is None:
        missing.append('receiver_type')
    if receiver_ko_name is None:
        missing.append('receiver_ko_name')
    if receiver_en_name is None:
        missing.append('receiver_en_name')

    if receiver_type == 'corporation':
        if receiver_corp_ko_name is None:
            missing.append('receiver_corp_ko_name')
        if receiver_corp_en_name is None:
            missing.append('receiver_corp_en_name')

    return missing


def _pick_nested_float(data: dict, path: list[str]) -> float | None:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _parse_float(current)


def _get_bithumb_instance() -> ccxt.bithumb:
    """??щ쑞 ccxt ?紐꾨뮞??곷뮞??獄쏆꼹???뺣뼄 (筌왖???λ뜃由??."""
    global _bithumb
    if _bithumb is None:
        _bithumb = ccxt.bithumb({
            'apiKey': config.BITHUMB_API_KEY,
            'secret': config.BITHUMB_SECRET_KEY,
            'enableRateLimit': True,
        })
    return _bithumb


def get_bithumb_instance() -> ccxt.bithumb:
    """Public accessor for shared Bithumb ccxt instance."""
    return _get_bithumb_instance()


async def init_bithumb() -> None:
    """??뺤쒔 ??뽰삂 ????щ쑞 ?紐꾨뮞??곷뮞???λ뜃由?酉釉??筌띾뜆???類ｋ궖??嚥≪뮆諭??뺣뼄."""
    # Keep startup independent from ccxt market discovery. Bithumb public
    # market loading is currently unstable and should not take down the API.
    _get_bithumb_instance()
    logger.info('Bithumb adapter ready (direct public API mode)')


async def discover_bithumb_tickers() -> set[str]:
    """??щ쑞 KRW 筌띾뜆????袁⑷퍥 ?怨쀫묽??獄쏆꼹???뺣뼄.

    Returns:
        ?? {"BTC", "ETH", "XRP", ...}
    """
    tickers: set[str] = set()
    payload = await _fetch_public_json('/public/ticker/ALL_KRW')
    if payload is None:
        return tickers

    data = payload.get('data')
    if not isinstance(data, dict):
        logger.warning('Bithumb ticker discovery returned invalid data payload')
        return tickers

    for currency, entry in data.items():
        if currency in {'date', 'timestamp', 'payment_currency'}:
            continue
        if isinstance(entry, dict):
            tickers.add(currency.upper())
    return tickers


async def fetch_all_bithumb_bbos() -> dict[str, BBO]:
    """/public/orderbook/ALL_KRW API嚥??袁⑷퍥 KRW ??BBO????⑦겣 鈺곌퀬???뺣뼄.

    ??щ쑞 fetch_tickers()??bid/ask??獄쏆꼹???? ??놁몵沃샕嚥?
    orderbook ALL API???????뤿연 1???紐꾪뀱嚥??袁⑷퍥 ?꾨뗄???best bid/ask??揶쎛?紐꾩궔??

    Returns:
        {ticker: BBO} 筌띲끋釉? ?? {"BTC": BBO(bid=..., ask=...), ...}
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                'https://api.bithumb.com/public/orderbook/ALL_KRW?count=1'
            )
            resp.raise_for_status()
            data = resp.json()

        if data.get('status') != '0000':
            logger.error('fetch_all_bithumb_bbos bad status: %s', data.get('status'))
            return {}

        orderbook_data = data.get('data', {})
        ts = orderbook_data.get('timestamp')
        timestamp = int(ts) if ts else None

        result: dict[str, BBO] = {}
        for currency, ob in orderbook_data.items():
            if currency in ('timestamp', 'payment_currency'):
                continue
            if not isinstance(ob, dict):
                continue

            bids = ob.get('bids', [])
            asks = ob.get('asks', [])
            bid = float(bids[0]['price']) if bids else None
            ask = float(asks[0]['price']) if asks else None

            if bid is not None and ask is not None:
                result[currency] = BBO(bid=bid, ask=ask, timestamp=timestamp)

        return result
    except Exception as exc:
        logger.error('fetch_all_bithumb_bbos failed: %s', exc)
        return {}


async def fetch_all_bithumb_network_info() -> dict[str, list[NetworkInfo]]:
    """筌?Ŋ???wallet ?怨쀬뵠?怨쀫퓠???袁⑷퍥 ???넅????쎈뱜??곌쾿 ?類ｋ궖??獄쏆꼹???뺣뼄.

    Returns:
        {currency: [NetworkInfo, ...]} 筌띲끋釉?    """
    await _ensure_bithumb_wallet_cache()

    result: dict[str, list[NetworkInfo]] = {}
    for item in _bithumb_wallet_cache:
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


async def fetch_bithumb_bbo(ticker: str) -> BBO | None:
    """??щ쑞?癒?퐣 ?諭???怨쀫묽??BBO??揶쎛?紐꾩궔??

    Args:
        ticker: ?? "BTC"

    Returns:
        BBO ?紐꾨뮞??곷뮞 ?癒?뮉 None (??쎈솭 ??
    """
    symbol = str(ticker or '').strip().upper()
    if not symbol:
        return None

    payload = await _fetch_public_json(f'/public/orderbook/{symbol}_KRW?count=1')
    if payload is None:
        return None

    data = payload.get('data')
    if not isinstance(data, dict):
        logger.debug('fetch_bithumb_bbo invalid payload for %s', symbol)
        return None

    bids = data.get('bids')
    asks = data.get('asks')
    top_bid = bids[0] if isinstance(bids, list) and bids else None
    top_ask = asks[0] if isinstance(asks, list) and asks else None
    bid = _parse_float(top_bid.get('price')) if isinstance(top_bid, dict) else None
    ask = _parse_float(top_ask.get('price')) if isinstance(top_ask, dict) else None

    timestamp_raw = data.get('timestamp')
    try:
        timestamp = int(timestamp_raw) if timestamp_raw is not None else None
    except (TypeError, ValueError):
        timestamp = None

    if bid is None and ask is None:
        return None
    return BBO(bid=bid, ask=ask, timestamp=timestamp)


async def fetch_bithumb_orderbook(ticker: str, depth: int = 5) -> list[list[float]]:
    """??щ쑞 ask ??삳쐭??鈺곌퀬??

    /public/orderbook/{TICKER}_KRW?count={depth} API???紐꾪뀱??뤿연
    ask ?硫? ?귐딅뮞?紐? 獄쏆꼹???뺣뼄 (??? 揶쎛野꺿뫗??.

    Args:
        ticker: ?? "BTC"
        depth: ?硫? 繹먮봿??(疫꿸퀡??5)

    Returns:
        [[price, qty], ...] ask ?硫? ?귐딅뮞??(??? 揶쎛野꺿뫗??.
        ??쎈솭 ?????귐딅뮞??
    """
    try:
        url = f'https://api.bithumb.com/public/orderbook/{ticker}_KRW?count={depth}'
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if data.get('status') != '0000':
            logger.debug('fetch_bithumb_orderbook bad status for %s: %s', ticker, data.get('status'))
            return []

        asks_raw = data.get('data', {}).get('asks', [])
        # ??щ쑞 ?臾먮뼗: [{"price": "...", "quantity": "..."}, ...]
        asks: list[list[float]] = []
        for entry in asks_raw:
            price = float(entry['price'])
            qty = float(entry['quantity'])
            asks.append([price, qty])

        # ??? 揶쎛野꺿뫗???類ｌ졊 (??щ쑞 API????? ?類ｌ졊??뤿선 ???筌?癰귣똻??
        asks.sort(key=lambda x: x[0])
        return asks

    except Exception as exc:
        logger.debug('fetch_bithumb_orderbook failed for %s: %s', ticker, exc)
        return []


async def fetch_usdt_krw() -> float | None:
    """??щ쑞?癒?퐣 USDT/KRW 筌띾뜆?筌?揶쎛野꺿뫗??揶쎛?紐꾩궔??

    Returns:
        USDT/KRW last 揶쎛野??癒?뮉 None (??쎈솭 ??
    """
    payload = await _fetch_public_json('/public/ticker/USDT_KRW')
    if payload is None:
        return None

    data = payload.get('data')
    if not isinstance(data, dict):
        logger.debug('fetch_usdt_krw invalid payload')
        return None

    return _parse_float(data.get('closing_price'))


def _build_jwt_token(query_string: str = '') -> str:
    """??щ쑞 API??JWT ?醫뤾쿃????밴쉐??뺣뼄."""
    import hashlib
    import uuid

    payload: dict = {
        'access_key': config.BITHUMB_API_KEY,
        'nonce': str(uuid.uuid4()),
        'timestamp': int(time.time() * 1000),
    }

    if query_string:
        query_hash = hashlib.sha512(query_string.encode()).hexdigest()
        payload['query_hash'] = query_hash
        payload['query_hash_alg'] = 'SHA512'

    token = jwt.encode(payload, config.BITHUMB_SECRET_KEY, algorithm='HS256')
    # pyjwt >= 2.0 returns str directly
    return token if isinstance(token, str) else token.decode('utf-8')


def _build_query_string(params: dict[str, Any]) -> str:
    """Build deterministic query string for JWT query-hash payloads."""
    clean: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        clean[str(key)] = str(value)
    return urlencode(clean)


def _format_number_string(value: float, precision: int = 16) -> str:
    text = f'{float(value):.{precision}f}'.rstrip('0').rstrip('.')
    return text if text else '0'


def _spot_symbol_to_market_id(symbol: str) -> str:
    """Convert ccxt symbol (e.g. BTC/KRW) to Bithumb market id (KRW-BTC)."""
    parts = str(symbol or '').strip().upper().split('/')
    if len(parts) != 2:
        raise ValueError(f'invalid bithumb spot symbol: {symbol}')
    base = parts[0].strip()
    quote = parts[1].split(':')[0].strip()
    if not base or not quote:
        raise ValueError(f'invalid bithumb spot symbol: {symbol}')
    return f'{quote}-{base}'


async def submit_bithumb_spot_order(
    symbol: str,
    side: str,
    amount: float,
    reference_price: float | None = None,
) -> dict[str, Any]:
    """Submit Bithumb spot order via JWT private API (/v1/orders).

    Rules from Bithumb docs:
      - market buy  => side=bid, ord_type=price, price required
      - market sell => side=ask, ord_type=market, volume required
    """
    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        raise RuntimeError('Bithumb API keys not configured')

    qty = float(amount)
    if not math.isfinite(qty) or qty <= 0:
        raise ValueError('amount must be greater than 0')

    normalized_side = str(side or '').strip().lower()
    if normalized_side not in {'buy', 'sell', 'bid', 'ask'}:
        raise ValueError(f'unsupported bithumb side: {side}')

    market = _spot_symbol_to_market_id(symbol)
    body: dict[str, str] = {'market': market}

    if normalized_side in {'buy', 'bid'}:
        ref = float(reference_price or 0.0)
        if not math.isfinite(ref) or ref <= 0:
            raise ValueError('reference_price is required for bithumb market buy')
        total_quote = max(int(math.ceil(qty * ref)), 1)
        body.update(
            {
                'side': 'bid',
                'ord_type': 'price',
                'price': str(total_quote),
            }
        )
    else:
        body.update(
            {
                'side': 'ask',
                'ord_type': 'market',
                'volume': _format_number_string(qty, precision=12),
            }
        )

    query_string = _build_query_string(body)
    token = _build_jwt_token(query_string)
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                'https://api.bithumb.com/v1/orders',
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, dict) else {'info': data}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:300]
        raise RuntimeError(
            f'Bithumb order HTTP {exc.response.status_code}: {detail}'
        ) from exc


async def fetch_bithumb_order(order_id: str) -> dict[str, Any] | None:
    """Fetch Bithumb private order detail by uuid/order_id."""
    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        return None

    target_id = str(order_id or '').strip()
    if not target_id:
        return None

    # Primary: stable v1 endpoint.
    # Fallback: v2 beta endpoint uses order_id.
    candidates = [
        ('https://api.bithumb.com/v1/order', {'uuid': target_id}),
        ('https://api.bithumb.com/v2/order', {'order_id': target_id}),
    ]

    last_error: Exception | None = None
    for url, params in candidates:
        query_string = _build_query_string(params)
        token = _build_jwt_token(query_string)
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f'{url}?{query_string}', headers=headers)
                response.raise_for_status()
                data = response.json()
            if isinstance(data, dict):
                return data
            return None
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        logger.debug('fetch_bithumb_order failed (%s): %s', target_id, last_error)
    return None


async def _ensure_bithumb_wallet_cache() -> None:
    """??щ쑞 wallet status API???紐꾪뀱??뤿연 net_type 筌?Ŋ?녺몴?揶쏄퉮???뺣뼄."""
    global _bithumb_net_types_cache, _bithumb_wallet_cache, _bithumb_cache_ts

    now = time.time()
    if now - _bithumb_cache_ts < _BITHUMB_CACHE_TTL and _bithumb_wallet_cache:
        return

    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        return

    try:
        token = _build_jwt_token()
        url = 'https://api.bithumb.com/v1/status/wallet'
        headers = {'Authorization': f'Bearer {token}'}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, list):
            return

        _bithumb_wallet_cache = data
        _bithumb_net_types_cache.clear()
        for item in data:
            curr = item.get('currency', '')
            net_type = item.get('net_type', '')
            if curr and net_type:
                _bithumb_net_types_cache.setdefault(curr, []).append(net_type)

        _bithumb_cache_ts = now
        logger.debug('Bithumb wallet cache updated: %d currencies', len(_bithumb_net_types_cache))

    except Exception as exc:
        logger.debug('_ensure_bithumb_wallet_cache failed: %s', exc)


async def fetch_bithumb_network_info(currency: str) -> list[NetworkInfo]:
    """??щ쑞 wallet status?癒?퐣 ?諭?????넅????쎈뱜??곌쾿 ?類ｋ궖??獄쏆꼹???뺣뼄."""
    await _ensure_bithumb_wallet_cache()

    result: list[NetworkInfo] = []
    for item in _bithumb_wallet_cache:
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


async def fetch_bithumb_registered_withdraw_addresses(
    currency: str,
    net_type: str | None = None,
) -> list[dict[str, str | None]] | None:
    """Return registered Bithumb withdraw addresses for a currency/network.

    Returns:
        - list: successful query (possibly empty)
        - None: query could not be completed
    """
    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        return None

    def _normalize(value: str) -> str:
        return ''.join(ch for ch in value.upper() if ch.isalnum())

    try:
        token = _build_jwt_token()
        headers = {'Authorization': f'Bearer {token}'}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                'https://api.bithumb.com/v1/withdraws/coin_addresses',
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, list):
            return None

        code = currency.strip().upper()
        net_norm = _normalize(net_type or '')
        result: list[dict[str, str | None]] = []

        for item in data:
            item_currency = str(item.get('currency', '')).strip().upper()
            if item_currency != code:
                continue

            item_net = str(item.get('net_type', '')).strip()
            if net_norm and _normalize(item_net) != net_norm:
                continue

            item_address = str(item.get('withdraw_address', '')).strip()
            if not item_address:
                continue

            item_tag = _clean_optional_tag(item.get('secondary_address'))
            result.append({
                'currency': item_currency,
                'net_type': item_net,
                'address': item_address,
                'tag': item_tag,
            })

        return result
    except httpx.HTTPStatusError as exc:
        logger.warning(
            'Bithumb registered withdraw address HTTP error (%s): %s %s',
            currency,
            exc.response.status_code,
            exc.response.text[:200],
        )
        return None
    except Exception as exc:
        logger.debug(
            'fetch_bithumb_registered_withdraw_addresses failed for %s: %s',
            currency,
            exc,
        )
        return None


async def fetch_bithumb_balance(currency: str) -> float | None:
    """Return available balance for a currency from Bithumb private API.

    Return rules:
      - success + currency exists: available balance
      - success + currency absent: 0.0
      - auth/network/parse failure: None
    """
    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        return None

    try:
        code = currency.strip().upper()
        token = _build_jwt_token()
        headers = {'Authorization': f'Bearer {token}'}

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                'https://api.bithumb.com/v1/accounts',
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, list):
            return None

        for item in data:
            if str(item.get('currency', '')).upper() != code:
                continue

            # v1/accounts returns available funds as "balance".
            balance = _parse_float(item.get('balance'))
            if balance is None:
                return None
            return max(balance, 0.0)

        # Currency row is absent => treat as no holdings.
        return 0.0

    except httpx.HTTPStatusError as exc:
        logger.warning(
            'Bithumb balance HTTP error (%s): %s %s',
            currency,
            exc.response.status_code,
            exc.response.text[:200],
        )
        return None
    except Exception as exc:
        logger.warning('Bithumb balance fetch failed (%s): %s', currency, exc)
        return None

async def fetch_withdrawal_limit(
    currency: str,
    net_type: str | None = None,
) -> WithdrawalLimit | None:
    """??щ쑞?癒?퐣 ?諭?????넅???곗뮄????뺣즲??鈺곌퀬???뺣뼄.

    Args:
        currency: ???넅 ?꾨뗀諭?(?? "BTC")

    Returns:
        WithdrawalLimit ?紐꾨뮞??곷뮞 ?癒?뮉 None (??쎈솭 ??
    """
    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        logger.debug('Bithumb API keys not configured, skipping withdrawal limit fetch')
        return WithdrawalLimit(currency=currency)

    resolved_net_type = (net_type or '').strip()
    if not resolved_net_type:
        # net_type 筌?Ŋ??癒?퐣 鈺곌퀬??        await _ensure_bithumb_wallet_cache()
        net_types = _bithumb_net_types_cache.get(currency, [])
        if not net_types:
            # net_type??筌뤴뫀?ㅿ쭖????넅 ?꾨뗀諭??癒?퍥????뺣즲
            net_types = [currency]
        resolved_net_type = net_types[0]

    query_string = f'currency={currency}&net_type={resolved_net_type}'
    try:
        token = _build_jwt_token(query_string)
        url = f'https://api.bithumb.com/v1/withdraws/chance?{query_string}'
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

        # ??щ쑞 ?臾먮뼗 ???뼓
        withdraw_limit_data = data.get('withdraw_limit', {})

        onetime_coin = _parse_float(withdraw_limit_data.get('onetime'))
        daily_coin = _parse_float(withdraw_limit_data.get('daily'))
        remaining_daily_coin = _parse_float(withdraw_limit_data.get('remaining_daily'))
        expected_fee = (
            _parse_float(data.get('expected_withdraw_fee'))
            or _parse_float(data.get('withdraw_fee'))
            or _parse_float(data.get('fee'))
            or _pick_nested_float(data, ['currency', 'withdraw_fee'])
            or _pick_nested_float(data, ['currency', 'withdrawal_fee'])
        )
        min_withdraw = (
            _parse_float(data.get('minimum_withdraw_amount'))
            or _parse_float(data.get('withdrawal_min_amount'))
            or _pick_nested_float(data, ['currency', 'withdraw_min'])
            or _pick_nested_float(data, ['currency', 'withdrawal_min'])
            or _pick_nested_float(data, ['currency', 'limits', 'withdraw', 'min'])
        )

        # KRW ??뤾텦: ?袁⑹삺 ??щ쑞 ask 揶쎛野?????        onetime_krw: float | None = None
        daily_krw: float | None = None
        remaining_daily_krw: float | None = None

        bbo = await fetch_bithumb_bbo(currency)
        ask_price = bbo.ask if bbo else None

        if ask_price:
            if onetime_coin is not None:
                onetime_krw = onetime_coin * ask_price
            if daily_coin is not None:
                daily_krw = daily_coin * ask_price
            if remaining_daily_coin is not None:
                remaining_daily_krw = remaining_daily_coin * ask_price

        return WithdrawalLimit(
            currency=currency,
            onetime_coin=onetime_coin,
            onetime_krw=onetime_krw,
            daily_coin=daily_coin,
            daily_krw=daily_krw,
            remaining_daily_coin=remaining_daily_coin,
            remaining_daily_krw=remaining_daily_krw,
            expected_fee=expected_fee,
            min_withdraw=min_withdraw,
        )

    except httpx.HTTPStatusError as exc:
        logger.warning(
            'fetch_withdrawal_limit HTTP error for %s (net_type=%s): %s %s',
            currency,
            resolved_net_type,
            exc.response.status_code,
            exc.response.text[:200],
        )
        return WithdrawalLimit(currency=currency)
    except Exception as exc:
        logger.debug('fetch_withdrawal_limit failed for %s: %s', currency, exc)
        return WithdrawalLimit(currency=currency)


async def submit_bithumb_withdrawal(
    currency: str,
    amount: float,
    address: str,
    net_type: str,
    exchange_name: str | None = None,
    tag: str | None = None,
    receiver_type: str | None = None,
    receiver_ko_name: str | None = None,
    receiver_en_name: str | None = None,
    receiver_corp_ko_name: str | None = None,
    receiver_corp_en_name: str | None = None,
) -> dict:
    """Submit coin withdrawal via Bithumb JWT v1 private API."""
    if not config.BITHUMB_API_KEY or not config.BITHUMB_SECRET_KEY:
        raise RuntimeError('Bithumb API keys not configured')

    currency_code = currency.strip().upper()
    network = net_type.strip()
    target_address = address.strip()
    target_exchange = _normalize_exchange_name(exchange_name)
    exchange_candidates = _exchange_name_candidates(target_exchange)
    tag_value = _clean_optional_tag(tag)
    receiver_type_value = _normalize_receiver_type(receiver_type)
    receiver_ko_name_value = _clean_optional_tag(receiver_ko_name)
    receiver_en_name_value = _clean_optional_tag(receiver_en_name)
    receiver_corp_ko_name_value = _clean_optional_tag(receiver_corp_ko_name)
    receiver_corp_en_name_value = _clean_optional_tag(receiver_corp_en_name)
    # Bithumb coin withdraw API allows up to 6 decimal places.
    amount_str = f'{float(amount):.6f}'.rstrip('0').rstrip('.')

    if not currency_code:
        raise ValueError('currency is required')
    if not network:
        raise ValueError('net_type is required')
    if not target_address:
        raise ValueError('address is required')
    if float(amount) <= 0:
        raise ValueError('amount must be greater than 0')
    if not target_exchange:
        raise ValueError('exchange_name is required')
    if not exchange_candidates:
        raise ValueError(f'unsupported exchange_name: {exchange_name}')

    missing_receiver_fields = _missing_required_receiver_fields_for_exchange_withdraw(
        receiver_type=receiver_type_value,
        receiver_ko_name=receiver_ko_name_value,
        receiver_en_name=receiver_en_name_value,
        receiver_corp_ko_name=receiver_corp_ko_name_value,
        receiver_corp_en_name=receiver_corp_en_name_value,
    )
    if missing_receiver_fields:
        raise ValueError(
            'missing receiver fields for exchange withdrawal: '
            + ', '.join(missing_receiver_fields)
        )

    body: dict[str, str] = {
        'currency': currency_code,
        'net_type': network,
        'amount': amount_str,
        'address': target_address,
        'exchange_name': exchange_candidates[0],
    }
    if tag_value is not None:
        body['secondary_address'] = tag_value
    if receiver_type_value is not None:
        body['receiver_type'] = receiver_type_value
    if receiver_ko_name_value is not None:
        body['receiver_ko_name'] = receiver_ko_name_value
    if receiver_en_name_value is not None:
        body['receiver_en_name'] = receiver_en_name_value
    if receiver_corp_ko_name_value is not None:
        body['receiver_corp_ko_name'] = receiver_corp_ko_name_value
    if receiver_corp_en_name_value is not None:
        body['receiver_corp_en_name'] = receiver_corp_en_name_value

    last_error: httpx.HTTPStatusError | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        for exchange in exchange_candidates:
            req_body = dict(body)
            req_body['exchange_name'] = exchange

            # Use the same key=value&... shape as Bithumb's official examples.
            # Percent-encoding here can cause query-hash mismatch for non-ASCII values.
            query_string = '&'.join(f'{key}={value}' for key, value in req_body.items())
            token = _build_jwt_token(query_string)
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            }

            try:
                response = await client.post(
                    'https://api.bithumb.com/v1/withdraws/coin',
                    headers=headers,
                    json=req_body,
                )
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {'info': data}
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exchange and _is_exchange_lookup_request_fail(exc):
                    detail = exc.response.text[:200]
                    logger.warning(
                        'Bithumb withdraw exchange lookup failed, retrying without/with alias: exchange_name=%s detail=%s',
                        exchange,
                        detail,
                    )
                    continue
                detail = exc.response.text[:300]
                raise RuntimeError(
                    f'Bithumb withdraw HTTP {exc.response.status_code}: {detail}'
                ) from exc

    if last_error is not None:
        detail = last_error.response.text[:300]
        raise RuntimeError(
            f'Bithumb withdraw HTTP {last_error.response.status_code}: {detail}'
        ) from last_error
    raise RuntimeError('Bithumb withdraw request failed before HTTP response')


async def close_bithumb() -> None:
    """??щ쑞 ?紐꾨뮞??곷뮞????ル뮉??"""
    global _bithumb
    if _bithumb is not None:
        try:
            await _bithumb.close()
        except Exception as exc:
            logger.error('Error closing bithumb instance: %s', exc)
        _bithumb = None

