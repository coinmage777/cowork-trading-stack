"""Auto-trigger 설정.

환경변수에서 로드하며 실행 중에는 변경 불가(시작 시점 스냅샷).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, '').strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, '').strip() or default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'y', 'on')


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key, '').strip()
    if not raw:
        return default
    return [t.strip().upper() for t in raw.split(',') if t.strip()]


@dataclass
class AutoTriggerConfig:
    enabled: bool = True
    dry_run: bool = True  # 절대 기본값 True 유지
    watchlist: list[str] = field(default_factory=lambda: ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'])
    futures_exchange: str = 'binance'

    # 역프 진입 임계값: gap < threshold 일 때 진입. parity=10000
    gap_enter_threshold: float = 9900.0  # 1% 역프

    nominal_usd: float = 20.0
    leverage: int = 4
    daily_cap_usd: float = 50.0
    per_ticker_cooldown_min: int = 60
    poll_interval_sec: int = 15

    consecutive_loss_halt: int = 3
    halt_duration_min: int = 60
    kill_switch_file: str = 'data/KILL_ARB'

    # Phase 2 ddari (기본 off)
    ddari_enabled: bool = False
    ddari_db_url: str = ''
    ddari_score_threshold: float = 1000.0
    ddari_require_matched_target: bool = False

    @classmethod
    def load(cls) -> 'AutoTriggerConfig':
        return cls(
            enabled=_env_bool('AUTO_TRIGGER_ENABLED', True),
            dry_run=_env_bool('AUTO_TRIGGER_DRY_RUN', True),
            watchlist=_env_list('AUTO_TRIGGER_WATCHLIST', ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE']),
            futures_exchange=_env('AUTO_TRIGGER_FUTURES_EXCHANGE', 'binance').lower(),
            gap_enter_threshold=_env_float('AUTO_TRIGGER_GAP_THRESHOLD', 9900.0),
            nominal_usd=_env_float('AUTO_TRIGGER_NOMINAL_USD', 20.0),
            leverage=_env_int('AUTO_TRIGGER_LEVERAGE', 4),
            daily_cap_usd=_env_float('AUTO_TRIGGER_DAILY_CAP_USD', 50.0),
            per_ticker_cooldown_min=_env_int('AUTO_TRIGGER_PER_TICKER_COOLDOWN_MIN', 60),
            poll_interval_sec=_env_int('AUTO_TRIGGER_POLL_INTERVAL_SEC', 15),
            consecutive_loss_halt=_env_int('AUTO_TRIGGER_CONSECUTIVE_LOSS_HALT', 3),
            halt_duration_min=_env_int('AUTO_TRIGGER_HALT_DURATION_MIN', 60),
            kill_switch_file=_env('AUTO_TRIGGER_KILL_SWITCH_FILE', 'data/KILL_ARB'),
            ddari_enabled=_env_bool('AUTO_TRIGGER_DDARI_ENABLED', False),
            ddari_db_url=_env('AUTO_TRIGGER_DDARI_DB_URL', ''),
            ddari_score_threshold=_env_float('AUTO_TRIGGER_DDARI_SCORE_THRESHOLD', 1000.0),
            ddari_require_matched_target=_env_bool('AUTO_TRIGGER_DDARI_REQUIRE_MATCHED', False),
        )
