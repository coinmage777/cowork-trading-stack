"""환경변수 및 거래소 설정."""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))


def _csv_set(name: str) -> set[str]:
    raw = os.getenv(name, '')
    return {item.strip().lower() for item in raw.split(',') if item.strip()}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return float(default)
    text = raw.strip()
    if not text:
        return float(default)
    try:
        value = float(text)
    except ValueError:
        return float(default)
    return value

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# Bithumb
BITHUMB_API_KEY = os.getenv('BITHUMB_API_KEY', '')
BITHUMB_SECRET_KEY = os.getenv('BITHUMB_SECRET_KEY', '')
BITHUMB_WITHDRAW_RECEIVER_TYPE = os.getenv('BITHUMB_WITHDRAW_RECEIVER_TYPE', '').strip()
BITHUMB_WITHDRAW_RECEIVER_KO_NAME = os.getenv('BITHUMB_WITHDRAW_RECEIVER_KO_NAME', '').strip()
BITHUMB_WITHDRAW_RECEIVER_EN_NAME = os.getenv('BITHUMB_WITHDRAW_RECEIVER_EN_NAME', '').strip()
BITHUMB_WITHDRAW_RECEIVER_CORP_KO_NAME = os.getenv(
    'BITHUMB_WITHDRAW_RECEIVER_CORP_KO_NAME', ''
).strip()
BITHUMB_WITHDRAW_RECEIVER_CORP_EN_NAME = os.getenv(
    'BITHUMB_WITHDRAW_RECEIVER_CORP_EN_NAME', ''
).strip()
BITHUMB_WITHDRAW_REQUIRE_RECIPIENT_INFO_EXCHANGES = _csv_set(
    'BITHUMB_WITHDRAW_REQUIRE_RECIPIENT_INFO_EXCHANGES'
)

# 해외 거래소 API 키
EXCHANGE_CREDENTIALS = {
    'binance': {
        'apiKey': os.getenv('BINANCE_API_KEY', ''),
        'secret': os.getenv('BINANCE_SECRET', ''),
    },
    'bybit': {
        'apiKey': os.getenv('BYBIT_API_KEY', ''),
        'secret': os.getenv('BYBIT_SECRET', ''),
    },
    'okx': {
        'apiKey': os.getenv('OKX_API_KEY', ''),
        'secret': os.getenv('OKX_SECRET', ''),
        'password': os.getenv('OKX_PASSPHRASE', ''),
    },
    'bitget': {
        'apiKey': os.getenv('BITGET_API_KEY', ''),
        'secret': os.getenv('BITGET_SECRET', ''),
        'password': os.getenv('BITGET_PASSPHRASE', ''),
    },
    'gate': {
        'apiKey': os.getenv('GATE_API_KEY', ''),
        'secret': os.getenv('GATE_SECRET', ''),
    },
    'htx': {
        'apiKey': os.getenv('HTX_API_KEY', ''),
        'secret': os.getenv('HTX_SECRET', ''),
    },
    'upbit': {
        'apiKey': os.getenv('UPBIT_API_KEY', ''),
        'secret': os.getenv('UPBIT_SECRET', ''),
    },
    'coinone': {
        'apiKey': os.getenv('COINONE_API_KEY', ''),
        'secret': os.getenv('COINONE_SECRET', ''),
    },
}

# 거래소별 선물 지원 여부 (사전 정의, load_markets로 검증)
EXCHANGES_WITH_FUTURES = {'binance', 'bybit', 'okx', 'bitget', 'gate', 'htx'}

# 폴링 주기 (초)
BBO_POLL_INTERVAL = 3  # Binance bookTicker weight 4+2 × 20/min = 120 weight/min (한도 1200)
NETWORK_POLL_INTERVAL = 10
WITHDRAWAL_LIMIT_POLL_INTERVAL = 30
TICKER_DISCOVERY_INTERVAL = 300  # 빗썸 상장 목록 갱신 주기 (5분)
LOAN_CACHE_REFRESH_INTERVAL = 3600

# 갭 알림 임계값 (이하 = 역프)
GAP_ALERT_THRESHOLD = 9500
# 갭 알림 하한선 (이하 = 티커 충돌/데이터 오류로 간주하여 제외)
GAP_ALERT_FLOOR = 5000

# Impact price 검증 규모 (USD)
IMPACT_CHECK_VOLUME_USD = 1000
# True면 오더북 기반 impact 재검증을 수행한다.
ENABLE_IMPACT_CHECK = True

# 쿨다운 (초)
ALERT_COOLDOWN_SECONDS = 600

# 출금 preview TTL (초)
WITHDRAW_PREVIEW_TTL_SECONDS = 180
# 출금량 계산 시 안전 버퍼 비율 (예: 0.002 = 0.2%)
WITHDRAW_SAFETY_BUFFER_RATIO = 0.0
# 출금량 계산 시 최소 안전 버퍼 (코인 수량 단위)
WITHDRAW_SAFETY_BUFFER_MIN = 0.0
# 출금 요청 수량 소수점 자리수 (내림)
WITHDRAW_AMOUNT_DECIMALS = 6
# Keep True for safer preview sizing against Bithumb balance checks.
WITHDRAW_SUBTRACT_FEE_FROM_AMOUNT = True
# When True, preview fails if target address is not found in /v1/withdraws/coin_addresses.
# Keep False by default because some exchange-address flows rely on exchange_name in request.
WITHDRAW_REQUIRE_REGISTERED_ADDRESS_MATCH = False

# Hedge entry defaults
HEDGE_NOMINAL_USD = max(_float_env('HEDGE_NOMINAL_USD', 500.0), 0.0)
HEDGE_LEVERAGE = 4
HEDGE_ORDER_POLL_ATTEMPTS = 4
HEDGE_ORDER_POLL_DELAY_MS = 250
# Hedge status tolerances
# ratio example: 0.001 = 0.1%
HEDGE_RESIDUAL_RATIO_TOLERANCE = max(_float_env('HEDGE_RESIDUAL_RATIO_TOLERANCE', 0.001), 0.0)
HEDGE_RESIDUAL_NOTIONAL_USD_TOLERANCE = max(
    _float_env('HEDGE_RESIDUAL_NOTIONAL_USD_TOLERANCE', 1.0),
    0.0,
)
HEDGE_CLOSE_RESIDUAL_NOTIONAL_USD_TOLERANCE = max(
    _float_env('HEDGE_CLOSE_RESIDUAL_NOTIONAL_USD_TOLERANCE', 2.0),
    0.0,
)
