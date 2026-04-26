from mpdex.base import MultiPerpDex, MultiPerpDexMixin

# exchange_factory의 함수들을 "지연 임포트"로 재노출
# - 이렇게 해야 mpdex를 import할 때 exchanges의 무거운 의존성을 즉시 요구하지 않습니다.
async def create_exchange(exchange_name: str, key_params=None):
    # comment: 호출 시점에만 factory를 불러오므로, 선택적 의존성이 없을 경우에도 import mpdex는 안전합니다.
    from mpdex.factory import create_exchange as _create_exchange
    return await _create_exchange(exchange_name, key_params)

def symbol_create(exchange_name: str, coin: str, *, is_spot=False, quote=None):
    from mpdex.factory import symbol_create as _symbol_create
    return _symbol_create(exchange_name, coin, is_spot=is_spot, quote=quote)

# 개별 래퍼 클래스는 __getattr__로 지연 노출(필요할 때만 import)
# 사용 예: from mpdex import LighterExchange
import importlib

def __getattr__(name):
    mapping = {
        "LighterExchange": ("mpdex.exchanges.lighter", "LighterExchange"),
        "BackpackExchange": ("mpdex.exchanges.backpack", "BackpackExchange"),
        "EdgexExchange": ("mpdex.exchanges.edgex", "EdgexExchange"),
        "GrvtExchange": ("mpdex.exchanges.grvt", "GrvtExchange"),
        "ParadexExchange": ("mpdex.exchanges.paradex", "ParadexExchange"),
        "TreadfiHlExchange": ("mpdex.exchanges.treadfi_hl", "TreadfiHlExchange"),
        "TreadfiPcExchange": ("mpdex.exchanges.treadfi_pc", "TreadfiPcExchange"),
        "VariationalExchange": ("mpdex.exchanges.variational", "VariationalExchange"),
        "PacificaExchange": ("mpdex.exchanges.pacifica", "PacificaExchange"),
        "HyperliquidExchange": ("mpdex.exchanges.hyperliquid", "HyperliquidExchange"),
        "SuperstackExchange": ("mpdex.exchanges.superstack", "SuperstackExchange"),
        "StandXExchange": ("mpdex.exchanges.standx", "StandXExchange"),
        "ExtendedExchange": ("mpdex.exchanges.extended", "ExtendedExchange"),
        "HotstuffExchange": ("mpdex.exchanges.hotstuff", "HotstuffExchange"),
        "DecibelExchange": ("mpdex.exchanges.decibel", "DecibelExchange"),
    }
    if name in mapping:
        mod, attr = mapping[name]
        module = importlib.import_module(mod)
        return getattr(module, attr)
    raise AttributeError(f"module 'mpdex' has no attribute {name!r}")

__all__ = [  # 공개 심볼 명시
    "MultiPerpDex", "MultiPerpDexMixin",
    "create_exchange", "symbol_create",
    "LighterExchange", "BackpackExchange", "EdgexExchange", "GrvtExchange",
    "ParadexExchange", "TreadfiHlExchange", "TreadfiPcExchange",
    "VariationalExchange", "PacificaExchange", "HyperliquidExchange",
    "SuperstackExchange", "StandXExchange", "ExtendedExchange",
    "HotstuffExchange",
    "DecibelExchange"
]
