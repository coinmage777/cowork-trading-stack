"""데이터 타입 정의."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BBO:
    """Best Bid/Offer."""
    bid: Optional[float] = None
    ask: Optional[float] = None
    timestamp: Optional[int] = None

    @property
    def valid(self) -> bool:
        return self.bid is not None and self.ask is not None


@dataclass
class NetworkInfo:
    """입출금 네트워크 정보."""
    network: str
    deposit: bool = False
    withdraw: bool = False
    fee: Optional[float] = None
    min_withdraw: Optional[float] = None


@dataclass
class FeatureSupport:
    """현물 마진/대출 가능 여부."""
    supported: Optional[bool] = None


@dataclass
class WithdrawalLimit:
    """빗썸 출금 한도."""
    currency: str = ''
    onetime_coin: Optional[float] = None
    onetime_krw: Optional[float] = None
    daily_coin: Optional[float] = None
    daily_krw: Optional[float] = None
    remaining_daily_coin: Optional[float] = None
    remaining_daily_krw: Optional[float] = None
    expected_fee: Optional[float] = None
    min_withdraw: Optional[float] = None


@dataclass
class ExchangeData:
    """개별 거래소의 특정 티커 데이터."""
    exchange: str
    spot_bbo: Optional[BBO] = None
    futures_bbo: Optional[BBO] = None
    spot_supported: bool = False
    futures_supported: bool = False
    spot_gap: Optional[float] = None
    futures_gap: Optional[float] = None
    networks: list[NetworkInfo] = field(default_factory=list)
    margin: FeatureSupport = field(default_factory=FeatureSupport)
    loan: FeatureSupport = field(default_factory=FeatureSupport)


@dataclass
class BithumbData:
    """빗썸 측 데이터."""
    ask: Optional[float] = None
    usdt_krw_last: Optional[float] = None
    withdrawal_limit: Optional[WithdrawalLimit] = None
    networks: list[NetworkInfo] = field(default_factory=list)


@dataclass
class GapResult:
    """특정 티커의 전체 갭 결과."""
    ticker: str
    timestamp: int
    bithumb: BithumbData = field(default_factory=BithumbData)
    exchanges: dict[str, ExchangeData] = field(default_factory=dict)
