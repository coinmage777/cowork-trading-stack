"""LP Manager (Phase 6) — Uniswap V3 LP 자동예치/회수 매니저.

4/21 CHIP 플레이북 학습:
- CHIP 상장 직후 DEX 풀에 초고변동성 + 대형 볼륨 → LP 수수료 과잉 창출
- 전략: 현재가 ±몇 % 의 tight range 로 NFT 포지션 민팅
- 30~60분 보유 → 가격이 range 이탈하면 즉시 회수 (IL 중단)
- listing_detector 이벤트 기반으로 Dexscreener 에서 풀 존재 여부 조회

실행 경로는 트리플 락:
    LP_ENABLED=true AND LP_DRY_RUN=false AND LP_LIVE_CONFIRM=true
그 외에는 전부 dry-run 시뮬레이션 (파일 기록만).

기본 정책 — **Phase 6 의 실자금 민팅은 스텁** (NonfungiblePositionManager ABI + tick 수학 복잡도 높음).
스텁 상태에서도:
1. Dexscreener 풀 조회 O
2. 포지션 JSONL 기록/트래킹 O
3. range 이탈 자동 회수 감시 O
4. dry-run 시뮬레이션 (fake position_id, fee/price 추정) O
5. 트리플 락 / kill switch / daily cap O

web3.py, RPC URL, private key 는 Phase 3/4 와 공유:
    DEX_ETH_RPC_URL / DEX_BASE_RPC_URL / DEX_PRIVATE_KEY
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# env helpers (listing_executor 동일 컨벤션)
# ----------------------------------------------------------------------


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        return float(default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(raw.strip())
    except ValueError:
        return int(default)


def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


# ----------------------------------------------------------------------
# 체인별 NonfungiblePositionManager 주소 (Uniswap V3 공식)
# ----------------------------------------------------------------------

_NFPM_ADDRESSES: dict[str, str] = {
    'ethereum': '<EVM_ADDRESS>',
    'eth': '<EVM_ADDRESS>',
    'base': '<EVM_ADDRESS>',
}

_CHAIN_RPC_ENV: dict[str, str] = {
    'ethereum': 'DEX_ETH_RPC_URL',
    'eth': 'DEX_ETH_RPC_URL',
    'base': 'DEX_BASE_RPC_URL',
}

# Dexscreener chain id 매핑
_DEXSCREENER_CHAIN: dict[str, str] = {
    'ethereum': 'ethereum',
    'eth': 'ethereum',
    'base': 'base',
}


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class LPManagerConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False
    notional_usd: float = 100.0
    tick_range_pct: float = 10.0
    hold_minutes: int = 60
    max_position_usd: float = 150.0
    daily_cap_usd: float = 300.0
    auto_close_on_range_exit: bool = True
    min_pool_liquidity_usd: float = 100_000.0
    kill_switch_file: str = 'data/KILL_LP'
    positions_path: str = 'data/lp_positions.jsonl'
    monitor_interval_sec: int = 30
    # Dexscreener API
    dexscreener_timeout_sec: float = 6.0
    # ------------------------------------------------------------------
    # TGE mode (Pika_Kim strategy #3 — "TGE ETH체인 LP 공급은 신")
    # ------------------------------------------------------------------
    tge_enabled: bool = True
    tge_auto_on_listing: bool = False
    tge_min_pool_liquidity_usd: float = 50_000.0
    tge_tick_range_pct: float = 5.0
    tge_hold_minutes: int = 60
    tge_re_range_max: int = 3
    tge_min_volume_1h_usd: float = 10_000.0
    tge_min_expected_fees_usd: float = 50.0
    tge_notional_usd: float = 200.0
    tge_gas_cost_per_tx_usd: float = 20.0
    tge_re_range_check_sec: int = 60
    tge_range_boundary_threshold: float = 0.8  # price 위치 > 80% of half-range
    tge_range_exit_grace_sec: int = 300  # 5분 범위이탈 지속 시 close
    tge_volume_drop_grace_sec: int = 600  # 10분 연속 저볼륨 시 close
    tge_pool_max_age_hours: float = 24.0
    tge_pool_growth_min_pct_per_hr: float = 50.0

    @classmethod
    def load(cls) -> 'LPManagerConfig':
        return cls(
            enabled=_bool_env('LP_ENABLED', True),
            dry_run=_bool_env('LP_DRY_RUN', True),
            live_confirm=_bool_env('LP_LIVE_CONFIRM', False),
            notional_usd=max(_float_env('LP_NOTIONAL_USD', 100.0), 0.0),
            tick_range_pct=max(_float_env('LP_TICK_RANGE_PCT', 10.0), 0.1),
            hold_minutes=max(_int_env('LP_HOLD_MINUTES', 60), 1),
            max_position_usd=max(_float_env('LP_MAX_POSITION_USD', 150.0), 0.0),
            daily_cap_usd=max(_float_env('LP_DAILY_CAP_USD', 300.0), 0.0),
            auto_close_on_range_exit=_bool_env('LP_AUTO_CLOSE_ON_RANGE_EXIT', True),
            min_pool_liquidity_usd=max(
                _float_env('LP_MIN_POOL_LIQUIDITY_USD', 100_000.0), 0.0
            ),
            kill_switch_file=_str_env('LP_KILL_SWITCH_FILE', 'data/KILL_LP'),
            positions_path=_str_env('LP_POSITIONS_PATH', 'data/lp_positions.jsonl'),
            monitor_interval_sec=max(_int_env('LP_MONITOR_INTERVAL_SEC', 30), 5),
            dexscreener_timeout_sec=max(
                _float_env('LP_DEXSCREENER_TIMEOUT_SEC', 6.0), 1.0
            ),
            tge_enabled=_bool_env('LP_TGE_ENABLED', True),
            tge_auto_on_listing=_bool_env('LP_TGE_AUTO_ON_LISTING', False),
            tge_min_pool_liquidity_usd=max(
                _float_env('LP_TGE_MIN_POOL_LIQUIDITY_USD', 50_000.0), 0.0
            ),
            tge_tick_range_pct=max(_float_env('LP_TGE_TICK_RANGE_PCT', 5.0), 0.1),
            tge_hold_minutes=max(_int_env('LP_TGE_HOLD_MINUTES', 60), 1),
            tge_re_range_max=max(_int_env('LP_TGE_RE_RANGE_MAX', 3), 0),
            tge_min_volume_1h_usd=max(
                _float_env('LP_TGE_MIN_VOLUME_1H_USD', 10_000.0), 0.0
            ),
            tge_min_expected_fees_usd=max(
                _float_env('LP_TGE_MIN_EXPECTED_FEES_USD', 50.0), 0.0
            ),
            tge_notional_usd=max(_float_env('LP_TGE_NOTIONAL_USD', 200.0), 0.0),
            tge_gas_cost_per_tx_usd=max(
                _float_env('LP_TGE_GAS_COST_PER_TX_USD', 20.0), 0.0
            ),
            tge_re_range_check_sec=max(
                _int_env('LP_TGE_RE_RANGE_CHECK_SEC', 60), 10
            ),
            tge_range_boundary_threshold=max(
                min(_float_env('LP_TGE_RANGE_BOUNDARY_THRESHOLD', 0.8), 0.999), 0.1
            ),
            tge_range_exit_grace_sec=max(
                _int_env('LP_TGE_RANGE_EXIT_GRACE_SEC', 300), 0
            ),
            tge_volume_drop_grace_sec=max(
                _int_env('LP_TGE_VOLUME_DROP_GRACE_SEC', 600), 0
            ),
            tge_pool_max_age_hours=max(
                _float_env('LP_TGE_POOL_MAX_AGE_HOURS', 24.0), 0.1
            ),
            tge_pool_growth_min_pct_per_hr=max(
                _float_env('LP_TGE_POOL_GROWTH_MIN_PCT_PER_HR', 50.0), 0.0
            ),
        )


@dataclass
class _LPState:
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = 0.0
    # position_id -> record
    open_positions: dict[int, dict[str, Any]] = field(default_factory=dict)
    total_detected: int = 0
    total_minted: int = 0
    total_closed: int = 0
    total_dry_run: int = 0
    total_skipped: int = 0
    total_errors: int = 0
    last_error: str = ''
    last_mint_ts: float = 0.0
    # TGE-specific stats
    tge_total_mints: int = 0
    tge_total_closes: int = 0
    tge_total_re_ranges: int = 0
    tge_total_fees_collected_usd: float = 0.0
    tge_total_hold_minutes: float = 0.0  # 누적 hold 시간 (분) — avg 계산용
    tge_total_skipped_gas: int = 0
    tge_total_skipped_liquidity: int = 0
    tge_total_skipped_volume: int = 0
    tge_last_mint_ts: float = 0.0


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ----------------------------------------------------------------------
# LPManager
# ----------------------------------------------------------------------


class LPManager:
    """Uniswap V3 집중 유동성 LP 자동 매니저.

    사용 예:
        lp = LPManager(
            listing_detector=listing_detector,
            telegram_service=telegram,
        )
        await lp.start()
        ...
        await lp.stop()
    """

    def __init__(
        self,
        dex_trader: Any = None,
        listing_detector: Any = None,
        telegram_service: Any = None,
        cfg: Optional[LPManagerConfig] = None,
    ) -> None:
        self.dex_trader = dex_trader
        self.detector = listing_detector
        self.telegram = telegram_service
        self.cfg = cfg or LPManagerConfig.load()
        self.state = _LPState(daily_reset_epoch=_today_midnight_epoch())

        self._running: bool = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._tge_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        # dry-run 시 할당할 가짜 position_id
        self._mock_position_counter: int = 900_000_000

        # lazy web3 캐시 (체인 -> Web3 인스턴스)
        self._w3_cache: dict[str, Any] = {}
        # lazy curl_cffi 세션 (dexscreener rate-limit 방어)
        self._http_session: Any = None

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        try:
            Path(self.cfg.positions_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[lp] positions_path prep failed: %s', exc)

        # 재시작 복원 — 최근 JSONL 에서 status=active 포지션만 메모리에 복원
        self._restore_open_positions()

        # listing_detector 콜백 등록
        if self.detector is not None and hasattr(self.detector, 'add_listener'):
            self.detector.add_listener(self._on_listing_event)

        # range exit / hold-expiry 감시 루프
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name='lp_monitor')
        # TGE 전용 re-range 감시 루프 (tge_enabled 일 때만)
        if self.cfg.tge_enabled:
            self._tge_task = asyncio.create_task(
                self._tge_monitor_loop(), name='lp_tge_monitor'
            )

        logger.info(
            '[lp] started (enabled=%s dry_run=%s live_confirm=%s notional=$%.0f '
            'range=±%.1f%% hold=%dmin daily_cap=$%.0f tge=%s)',
            self.cfg.enabled,
            self.cfg.dry_run,
            self.cfg.live_confirm,
            self.cfg.notional_usd,
            self.cfg.tick_range_pct,
            self.cfg.hold_minutes,
            self.cfg.daily_cap_usd,
            self.cfg.tge_enabled,
        )

    async def stop(self) -> None:
        self._running = False
        if self.detector is not None and hasattr(self.detector, 'remove_listener'):
            try:
                self.detector.remove_listener(self._on_listing_event)
            except Exception:  # noqa: BLE001
                pass
        task = self._monitor_task
        self._monitor_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        tge_task = self._tge_task
        self._tge_task = None
        if tge_task is not None and not tge_task.done():
            tge_task.cancel()
            try:
                await tge_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # http session close
        sess = self._http_session
        self._http_session = None
        if sess is not None:
            try:
                await sess.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info('[lp] stopped')

    # ------------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        live_armed = (
            self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm
        )
        tge_closes = max(self.state.tge_total_closes, 0)
        avg_hold_min = (
            (self.state.tge_total_hold_minutes / tge_closes) if tge_closes > 0 else 0.0
        )
        tge_stats = {
            'enabled': self.cfg.tge_enabled,
            'auto_on_listing': self.cfg.tge_auto_on_listing,
            'total_mints': self.state.tge_total_mints,
            'total_closes': self.state.tge_total_closes,
            'total_re_ranges': self.state.tge_total_re_ranges,
            'total_fees_collected_usd': round(
                self.state.tge_total_fees_collected_usd, 4
            ),
            'avg_hold_minutes': round(avg_hold_min, 2),
            'skipped_gas': self.state.tge_total_skipped_gas,
            'skipped_liquidity': self.state.tge_total_skipped_liquidity,
            'skipped_volume': self.state.tge_total_skipped_volume,
            'last_mint_ts': self.state.tge_last_mint_ts,
            'config': {
                'min_pool_liquidity_usd': self.cfg.tge_min_pool_liquidity_usd,
                'tick_range_pct': self.cfg.tge_tick_range_pct,
                'hold_minutes': self.cfg.tge_hold_minutes,
                'notional_usd': self.cfg.tge_notional_usd,
                're_range_max': self.cfg.tge_re_range_max,
                'min_volume_1h_usd': self.cfg.tge_min_volume_1h_usd,
                'min_expected_fees_usd': self.cfg.tge_min_expected_fees_usd,
                'gas_cost_per_tx_usd': self.cfg.tge_gas_cost_per_tx_usd,
                'pool_max_age_hours': self.cfg.tge_pool_max_age_hours,
                'pool_growth_min_pct_per_hr': self.cfg.tge_pool_growth_min_pct_per_hr,
            },
        }
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': live_armed,
            'kill_switch_active': self._kill_switch_active(),
            'notional_usd': self.cfg.notional_usd,
            'max_position_usd': self.cfg.max_position_usd,
            'tick_range_pct': self.cfg.tick_range_pct,
            'hold_minutes': self.cfg.hold_minutes,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'daily_spent_usd': round(self.state.daily_spent_usd, 2),
            'auto_close_on_range_exit': self.cfg.auto_close_on_range_exit,
            'min_pool_liquidity_usd': self.cfg.min_pool_liquidity_usd,
            'open_positions_count': len(self.state.open_positions),
            'open_positions': [dict(v) for v in self.state.open_positions.values()],
            'total_detected': self.state.total_detected,
            'total_minted': self.state.total_minted,
            'total_closed': self.state.total_closed,
            'total_dry_run': self.state.total_dry_run,
            'total_skipped': self.state.total_skipped,
            'total_errors': self.state.total_errors,
            'last_error': self.state.last_error,
            'last_mint_ts': self.state.last_mint_ts,
            'positions_path': self.cfg.positions_path,
            'kill_switch_file': self.cfg.kill_switch_file,
            'tge_stats': tge_stats,
        }

    def recent_positions(self, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(self.cfg.positions_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lp] recent_positions read failed: %s', exc)
            return []
        out: list[dict[str, Any]] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # 공개 API (main.py 에서 직접 호출)
    # ------------------------------------------------------------------

    async def mint_position(
        self,
        pool_address: str,
        chain: str,
        token0: str = '',
        token1: str = '',
        amount_usd: float = 0.0,
        tick_range_pct: float = 0.0,
    ) -> dict[str, Any]:
        """Uniswap V3 NFT 포지션 민팅.

        dry-run 모드에서는 시뮬레이션된 position_id 를 반환하고
        JSONL 에 active 상태로 기록한다. 실자금 경로는 `LP_LIVE_CONFIRM=true`
        이고 `dry_run=false` 일 때만 열리며, 현재는 `NotImplementedError`
        (Phase 6.1 에서 tick math + NFPM ABI 구현 예정).
        """
        self.state.total_detected += 1

        pool_address = (pool_address or '').strip()
        chain_norm = self._normalize_chain(chain)
        token0 = (token0 or '').strip()
        token1 = (token1 or '').strip()
        amount = float(amount_usd) if amount_usd else self.cfg.notional_usd
        range_pct = float(tick_range_pct) if tick_range_pct else self.cfg.tick_range_pct

        # --- 게이트 1: 기본 입력 검증
        if not pool_address or not pool_address.startswith('0x'):
            return self._skip('invalid pool_address', pool=pool_address, chain=chain_norm)
        if chain_norm not in _NFPM_ADDRESSES:
            return self._skip(f'unsupported chain: {chain}', pool=pool_address)

        # --- 게이트 2: enabled
        if not self.cfg.enabled:
            return self._skip('LP disabled', pool=pool_address, chain=chain_norm)

        # --- 게이트 3: kill switch
        if self._kill_switch_active():
            return self._skip(
                f'kill switch: {self.cfg.kill_switch_file}',
                pool=pool_address,
                chain=chain_norm,
            )

        # --- 게이트 4: 포지션당 hard cap
        if amount > self.cfg.max_position_usd:
            return self._skip(
                f'amount {amount} exceeds max_position_usd {self.cfg.max_position_usd}',
                pool=pool_address,
                chain=chain_norm,
            )

        # --- 게이트 5: daily cap
        self._maybe_rollover_daily()
        if self.state.daily_spent_usd + amount > self.cfg.daily_cap_usd:
            return self._skip(
                f'daily cap ${self.state.daily_spent_usd:.2f}+${amount:.2f}'
                f'>{self.cfg.daily_cap_usd:.2f}',
                pool=pool_address,
                chain=chain_norm,
            )

        # --- 풀 정보 조회 (dexscreener). 실패해도 dry-run 은 진행 가능.
        pool_info = await self._fetch_pool_info(chain_norm, pool_address)
        current_price = None
        liquidity_usd = None
        if pool_info:
            current_price = pool_info.get('price_usd')
            liquidity_usd = pool_info.get('liquidity_usd')
            if not token0:
                token0 = pool_info.get('token0_address', '') or token0
            if not token1:
                token1 = pool_info.get('token1_address', '') or token1

        # --- 게이트 6: 유동성 하한 (실자금 라이브에서만 엄격)
        live_armed = (
            self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm
        )
        if live_armed and liquidity_usd is not None:
            if liquidity_usd < self.cfg.min_pool_liquidity_usd:
                return self._skip(
                    f'pool liquidity ${liquidity_usd:.0f} < min '
                    f'${self.cfg.min_pool_liquidity_usd:.0f}',
                    pool=pool_address,
                    chain=chain_norm,
                )

        # --- range 계산 (현재가 기준 ± range_pct%)
        if current_price is None or current_price <= 0:
            # dry-run 이어도 가격 모르면 placeholder
            range_low = 0.0
            range_high = 0.0
        else:
            range_low = current_price * (1 - range_pct / 100.0)
            range_high = current_price * (1 + range_pct / 100.0)

        # --- 트리플 락 체크
        if not live_armed:
            # dry-run — 가짜 position_id 할당
            position_id = self._next_mock_position_id()
            self.state.total_dry_run += 1
            record = {
                'ts': int(time.time()),
                'position_id': position_id,
                'chain': chain_norm,
                'pool': pool_address,
                'token0': token0,
                'token1': token1,
                'range_low': round(range_low, 10),
                'range_high': round(range_high, 10),
                'entry_price': current_price,
                'amount_usd': round(amount, 2),
                'tick_range_pct': range_pct,
                'status': 'active',
                'fees_collected_usd': 0.0,
                'mode': 'dry_run',
                'hold_expires_ts': int(time.time()) + self.cfg.hold_minutes * 60,
                'pool_liquidity_usd': liquidity_usd,
            }
            self.state.open_positions[position_id] = record
            self.state.daily_spent_usd += amount
            self.state.last_mint_ts = record['ts']
            await self._append_record(record)
            logger.info(
                '[lp] DRY_RUN mint pid=%d chain=%s pool=%s amount=$%.0f '
                'range=%.8f-%.8f liq=%s',
                position_id,
                chain_norm,
                pool_address,
                amount,
                range_low,
                range_high,
                liquidity_usd,
            )
            await self._notify(
                f'🧪 [LP DRY] mint pid={position_id} {chain_norm}\n'
                f'pool {pool_address[:10]}…\n'
                f'  amount=${amount:.0f} range=±{range_pct}% entry={current_price}',
                alert_key='lp_dry',
            )
            return {'ok': True, 'mode': 'dry_run', 'position': record}

        # --- 라이브 민팅 (Phase 6.1)
        raise NotImplementedError('LP mint — Phase 6.1')

    async def collect_fees(self, position_id: int, chain: str) -> dict[str, Any]:
        """미수령 수수료 수령. 현재는 dry-run 시뮬레이션만."""
        pid = int(position_id)
        chain_norm = self._normalize_chain(chain)
        record = self.state.open_positions.get(pid)
        if not record:
            return {'ok': False, 'code': 'NOT_FOUND', 'message': f'position {pid} not open'}

        live_armed = (
            self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm
        )

        if not live_armed:
            # 시뮬레이션: 경과 시간당 0.05% 수수료로 추정
            elapsed_sec = time.time() - record.get('ts', time.time())
            simulated_fee = (
                record.get('amount_usd', 0.0) * 0.0005 * (elapsed_sec / 600.0)
            )
            record['fees_collected_usd'] = round(
                record.get('fees_collected_usd', 0.0) + simulated_fee, 4
            )
            logger.info(
                '[lp] DRY_RUN collect_fees pid=%d +$%.4f (total $%.4f)',
                pid,
                simulated_fee,
                record['fees_collected_usd'],
            )
            return {
                'ok': True,
                'mode': 'dry_run',
                'position_id': pid,
                'collected_usd': round(simulated_fee, 4),
                'total_fees_usd': record['fees_collected_usd'],
            }

        raise NotImplementedError('LP collect_fees — Phase 6.1')

    async def close_position(
        self,
        position_id: int,
        chain: str,
        reason: str = 'manual',
    ) -> dict[str, Any]:
        """유동성 회수 + NFT burn. dry-run 시뮬레이션 포함."""
        pid = int(position_id)
        chain_norm = self._normalize_chain(chain)
        record = self.state.open_positions.get(pid)
        if not record:
            return {
                'ok': False,
                'code': 'NOT_FOUND',
                'message': f'position {pid} not open',
            }

        live_armed = (
            self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm
        )

        if not live_armed:
            # dry-run — status=closed 전환 + 수수료 시뮬레이션 커밋 + 종료 레코드 append
            await self.collect_fees(pid, chain_norm)
            record = self.state.open_positions.pop(pid, record)
            record['status'] = 'closed'
            record['closed_ts'] = int(time.time())
            record['close_reason'] = reason
            self.state.total_closed += 1
            await self._append_record(record)
            logger.info(
                '[lp] DRY_RUN close pid=%d reason=%s fees=$%.4f',
                pid,
                reason,
                record.get('fees_collected_usd', 0.0),
            )
            await self._notify(
                f'🧪 [LP DRY] close pid={pid} {chain_norm}\n'
                f'reason={reason} fees=${record.get("fees_collected_usd",0.0):.4f}',
                alert_key='lp_dry',
            )
            return {'ok': True, 'mode': 'dry_run', 'position': record}

        raise NotImplementedError('LP close_position — Phase 6.1')

    async def list_positions(self, chain: str = '') -> list[dict[str, Any]]:
        """활성 LP 포지션 목록. chain 지정 시 해당 체인만."""
        chain_norm = self._normalize_chain(chain) if chain else ''
        out: list[dict[str, Any]] = []
        for rec in self.state.open_positions.values():
            if chain_norm and rec.get('chain') != chain_norm:
                continue
            out.append(dict(rec))
        return out

    # ------------------------------------------------------------------
    # listing_detector 콜백 — 자동 LP 진입
    # ------------------------------------------------------------------

    def _on_listing_event(self, event: dict[str, Any]) -> Any:
        if not self._running:
            return None
        try:
            return asyncio.create_task(
                self._handle_listing_safe(event),
                name=f'lp_listing_{event.get("ticker", "unknown")}',
            )
        except RuntimeError:
            return None

    async def _handle_listing_safe(self, event: dict[str, Any]) -> None:
        try:
            await self._handle_listing(event)
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'{type(exc).__name__}: {exc}'
            logger.exception('[lp] listing handler error: %s', exc)

    async def _handle_listing(self, event: dict[str, Any]) -> None:
        """상장 이벤트 → Dexscreener 로 DEX 풀 조회 → 조건 충족 시 LP 민팅.

        TGE 이벤트(explicit flag 또는 heuristic)면 tight-range / gas-aware
        TGE 경로로 분기. 그 외는 기존 통상 LP mint.
        """
        ticker = str(event.get('ticker') or '').strip().upper()
        if not ticker:
            return
        if not self.cfg.enabled:
            return
        if self._kill_switch_active():
            logger.debug('[lp] skip %s: kill switch', ticker)
            return

        # Dexscreener 티커 검색 (여러 풀 중 Uniswap V3 + 유동성 가장 큰 것)
        best = await self._find_best_v3_pool(ticker)
        if not best:
            logger.debug('[lp] %s: no uniswap v3 pool found', ticker)
            return

        liquidity_usd = best.get('liquidity_usd') or 0.0
        pool_addr = best.get('pool_address', '')
        chain = best.get('chain', '')

        # TGE 라우팅 — explicit flag 또는 pool heuristic
        is_tge_event = self._is_tge_event(event)
        pool_info_full: Optional[dict[str, Any]] = None
        if pool_addr and chain:
            try:
                pool_info_full = await self._fetch_pool_info(chain, pool_addr)
            except Exception:  # noqa: BLE001
                pool_info_full = None
        is_tge_heuristic = bool(
            pool_info_full and self._is_tge_pool_by_heuristic(pool_info_full)
        )
        route_tge = (
            self.cfg.tge_enabled
            and self.cfg.tge_auto_on_listing
            and (is_tge_event or is_tge_heuristic)
        )

        if route_tge:
            logger.info(
                '[lp][tge] %s routed to TGE path (explicit=%s heuristic=%s) '
                'pool=%s chain=%s liq=$%.0f',
                ticker,
                is_tge_event,
                is_tge_heuristic,
                pool_addr,
                chain,
                liquidity_usd,
            )
            try:
                await self.trigger_tge_mint(
                    pool_address=pool_addr,
                    chain=chain,
                    token0=best.get('token0_address', ''),
                    token1=best.get('token1_address', ''),
                    ticker=ticker,
                    reason='auto_listing_tge',
                )
            except NotImplementedError as exc:
                logger.warning('[lp][tge] live mint stub for %s: %s', ticker, exc)
                self.state.total_errors += 1
                self.state.last_error = 'live mint stub'
            return

        if liquidity_usd < self.cfg.min_pool_liquidity_usd:
            logger.info(
                '[lp] %s skip: liquidity $%.0f < min $%.0f',
                ticker,
                liquidity_usd,
                self.cfg.min_pool_liquidity_usd,
            )
            self.state.total_skipped += 1
            return

        logger.info(
            '[lp] %s listing → auto-mint candidate: pool=%s chain=%s liq=$%.0f',
            ticker,
            pool_addr,
            chain,
            liquidity_usd,
        )
        # mint_position 호출 — 내부에서 모든 게이트 재검증
        try:
            await self.mint_position(
                pool_address=pool_addr,
                chain=chain,
                token0=best.get('token0_address', ''),
                token1=best.get('token1_address', ''),
                amount_usd=self.cfg.notional_usd,
                tick_range_pct=self.cfg.tick_range_pct,
            )
        except NotImplementedError as exc:
            logger.warning('[lp] live mint stub hit for %s: %s', ticker, exc)
            self.state.total_errors += 1
            self.state.last_error = 'live mint stub'

    # ------------------------------------------------------------------
    # 모니터링 루프 — hold expiry + range exit
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self._tick_monitor()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning('[lp] monitor tick error: %s', exc)
            try:
                await asyncio.sleep(self.cfg.monitor_interval_sec)
            except asyncio.CancelledError:
                raise

    async def _tick_monitor(self) -> None:
        if not self.state.open_positions:
            return
        now = time.time()
        # 리스트로 스냅샷 (iteration 중 변경 방지)
        for pid, record in list(self.state.open_positions.items()):
            hold_expiry = float(record.get('hold_expires_ts') or 0.0)
            chain = record.get('chain') or ''
            pool = record.get('pool') or ''

            # 1) hold 만료 → 자동 close
            if hold_expiry > 0 and now >= hold_expiry:
                logger.info('[lp] pid=%d hold expired, closing', pid)
                try:
                    await self.close_position(pid, chain, reason='hold_expired')
                except Exception as exc:  # noqa: BLE001
                    logger.warning('[lp] hold-expiry close failed pid=%d: %s', pid, exc)
                continue

            # 2) range exit 자동 close
            if self.cfg.auto_close_on_range_exit and pool:
                try:
                    info = await self._fetch_pool_info(chain, pool)
                except Exception as exc:  # noqa: BLE001
                    logger.debug('[lp] pid=%d pool refresh err: %s', pid, exc)
                    info = None
                if info is None:
                    continue
                cur = info.get('price_usd')
                lo = float(record.get('range_low') or 0.0)
                hi = float(record.get('range_high') or 0.0)
                if cur is None or lo <= 0 or hi <= 0:
                    continue
                if cur < lo or cur > hi:
                    logger.info(
                        '[lp] pid=%d range exit (price=%.8f range=%.8f-%.8f), closing',
                        pid,
                        cur,
                        lo,
                        hi,
                    )
                    try:
                        await self.close_position(pid, chain, reason='range_exit')
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            '[lp] range-exit close failed pid=%d: %s', pid, exc
                        )

    # ------------------------------------------------------------------
    # Dexscreener 조회
    # ------------------------------------------------------------------

    async def _http(self) -> Any:
        if self._http_session is not None:
            return self._http_session
        try:
            from curl_cffi.requests import AsyncSession as CurlAsyncSession  # type: ignore
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lp] curl_cffi unavailable: %s', exc)
            return None
        try:
            self._http_session = CurlAsyncSession(
                impersonate='chrome124',
                timeout=self.cfg.dexscreener_timeout_sec,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lp] curl_cffi session create err: %s', exc)
            self._http_session = None
        return self._http_session

    async def _fetch_pool_info(
        self, chain: str, pool_address: str
    ) -> Optional[dict[str, Any]]:
        """Dexscreener 로 pool address 조회 → price/liquidity 반환."""
        chain_id = _DEXSCREENER_CHAIN.get(self._normalize_chain(chain))
        if not chain_id or not pool_address:
            return None
        session = await self._http()
        if session is None:
            return None
        url = f'https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pool_address}'
        try:
            resp = await session.get(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lp] dexscreener err: %s', exc)
            return None
        if getattr(resp, 'status_code', None) != 200:
            return None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return None
        pair = None
        if isinstance(data, dict):
            pairs = data.get('pairs') or []
            if isinstance(pairs, list) and pairs:
                pair = pairs[0]
            elif isinstance(data.get('pair'), dict):
                pair = data.get('pair')
        if not isinstance(pair, dict):
            return None
        try:
            price_usd = float(pair.get('priceUsd') or 0.0) or None
        except (TypeError, ValueError):
            price_usd = None
        liq = pair.get('liquidity') or {}
        try:
            liquidity_usd = float(liq.get('usd') or 0.0) or None
        except (TypeError, ValueError):
            liquidity_usd = None
        t0 = (pair.get('baseToken') or {}).get('address') or ''
        t1 = (pair.get('quoteToken') or {}).get('address') or ''
        # TGE-specific signals: volume/age/price-change
        vol = pair.get('volume') or {}
        try:
            volume_h1 = float(vol.get('h1') or 0.0)
        except (TypeError, ValueError):
            volume_h1 = 0.0
        try:
            volume_m5 = float(vol.get('m5') or 0.0)
        except (TypeError, ValueError):
            volume_m5 = 0.0
        price_change = pair.get('priceChange') or {}
        try:
            price_change_h1 = float(price_change.get('h1') or 0.0)
        except (TypeError, ValueError):
            price_change_h1 = 0.0
        pair_created_at_ms = pair.get('pairCreatedAt')
        pool_age_hours: Optional[float] = None
        if isinstance(pair_created_at_ms, (int, float)) and pair_created_at_ms > 0:
            try:
                pool_age_hours = max(
                    (time.time() - float(pair_created_at_ms) / 1000.0) / 3600.0, 0.0
                )
            except Exception:  # noqa: BLE001
                pool_age_hours = None
        # fee tier (Uniswap V3 basis points) — 예상 수수료 계산용
        fee_tier_bps = None
        try:
            if 'feeTier' in pair:
                fee_tier_bps = float(pair.get('feeTier') or 0.0)
            elif 'fee' in pair:
                fee_tier_bps = float(pair.get('fee') or 0.0)
        except (TypeError, ValueError):
            fee_tier_bps = None
        return {
            'price_usd': price_usd,
            'liquidity_usd': liquidity_usd,
            'dex_id': pair.get('dexId') or '',
            'token0_address': t0,
            'token1_address': t1,
            'pool_address': pool_address,
            'chain': chain_id,
            'volume_h1_usd': volume_h1,
            'volume_m5_usd': volume_m5,
            'price_change_h1_pct': price_change_h1,
            'pool_age_hours': pool_age_hours,
            'fee_tier_bps': fee_tier_bps,
        }

    async def _find_best_v3_pool(self, ticker: str) -> Optional[dict[str, Any]]:
        """dexscreener 심볼 검색 → Uniswap V3 + 지원 체인 + 유동성 최대."""
        session = await self._http()
        if session is None:
            return None
        url = f'https://api.dexscreener.com/latest/dex/search?q={ticker}'
        try:
            resp = await session.get(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lp] dexscreener search err: %s', exc)
            return None
        if getattr(resp, 'status_code', None) != 200:
            return None
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return None
        pairs = data.get('pairs') if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            return None

        supported_chains = set(_DEXSCREENER_CHAIN.values())
        best: Optional[dict[str, Any]] = None
        best_liq = 0.0
        t_upper = ticker.upper()
        for p in pairs:
            if not isinstance(p, dict):
                continue
            chain_id = p.get('chainId')
            dex_id = str(p.get('dexId') or '').lower()
            if chain_id not in supported_chains:
                continue
            if 'uniswap' not in dex_id:
                continue
            # v3 판별 — dexId 에 v3 포함되거나 version 필드가 v3
            version = str(p.get('labels') or p.get('version') or '').lower()
            if 'v3' not in dex_id and 'v3' not in version:
                # dexscreener 는 uniswap v3 를 dexId=uniswap 으로 뭉치는 경우도 있어 둘 다 허용
                if dex_id not in {'uniswap', 'uniswap-v3', 'uniswapv3'}:
                    continue
            base_sym = str((p.get('baseToken') or {}).get('symbol') or '').upper()
            quote_sym = str((p.get('quoteToken') or {}).get('symbol') or '').upper()
            if t_upper not in {base_sym, quote_sym}:
                continue
            try:
                liq = float((p.get('liquidity') or {}).get('usd') or 0.0)
            except (TypeError, ValueError):
                liq = 0.0
            if liq <= best_liq:
                continue
            try:
                price_usd = float(p.get('priceUsd') or 0.0) or None
            except (TypeError, ValueError):
                price_usd = None
            best_liq = liq
            best = {
                'pool_address': p.get('pairAddress') or '',
                'chain': chain_id,
                'dex_id': dex_id,
                'token0_address': (p.get('baseToken') or {}).get('address') or '',
                'token1_address': (p.get('quoteToken') or {}).get('address') or '',
                'price_usd': price_usd,
                'liquidity_usd': liq,
                'ticker': t_upper,
            }
        return best

    # ------------------------------------------------------------------
    # TGE (Token Generation Event) — Pika_Kim strategy #3
    # ------------------------------------------------------------------

    def _is_tge_event(self, event: dict[str, Any]) -> bool:
        """listing_detector event 가 TGE 인지 판정.

        트리거:
        - event['tge_info'] 가 truthy (explicit TGE flag)
        - event['event_type'] == 'tge'
        - title/notice 에 'TGE' 포함
        """
        if not isinstance(event, dict):
            return False
        if event.get('tge_info'):
            return True
        etype = str(event.get('event_type') or '').lower()
        if etype in {'tge', 'token_generation_event', 'first_launch'}:
            return True
        title = str(event.get('title') or '')
        notice = str(event.get('notice') or event.get('message') or '')
        haystack = f'{title} {notice}'.upper()
        if 'TGE' in haystack or 'TOKEN GENERATION' in haystack:
            return True
        return False

    def _is_tge_pool_by_heuristic(self, pool_info: dict[str, Any]) -> bool:
        """Dexscreener pool 데이터로 TGE 여부 휴리스틱 판정.

        - 풀 생성 < tge_pool_max_age_hours (default 24h)
        - volume_h1 / (liquidity/2) 비율 > tge_pool_growth_min_pct_per_hr
          (시간당 50% 이상의 회전율 = 급성장 중)
        """
        if not isinstance(pool_info, dict):
            return False
        age_hours = pool_info.get('pool_age_hours')
        if age_hours is None or age_hours > self.cfg.tge_pool_max_age_hours:
            return False
        liq = pool_info.get('liquidity_usd') or 0.0
        vol_h1 = pool_info.get('volume_h1_usd') or 0.0
        if liq <= 0:
            # 나이만 충족해도 TGE 후보로 인정 (liquidity 0 은 아주 신규 풀)
            return True
        # volume 이 liquidity 의 절반(single-sided 근사) 이상 회전률이면 성장 중
        growth_pct_per_hr = (vol_h1 / max(liq / 2.0, 1.0)) * 100.0
        return growth_pct_per_hr >= self.cfg.tge_pool_growth_min_pct_per_hr

    def _estimate_tge_fees(
        self,
        notional_usd: float,
        liquidity_usd: Optional[float],
        volume_h1_usd: Optional[float],
        hold_minutes: int,
        fee_tier_bps: Optional[float],
    ) -> float:
        """TGE 기간 1시간 보유 시 예상 수수료 추정 (conservative).

        공식: expected_fees ≈ notional_share × hourly_volume × fee_rate × (hold/60)
        - notional_share = notional / (liquidity + notional)
        - fee_rate = fee_tier_bps / 10000 (0.3% default)
        - tight range 이므로 실효 유동성은 notional 거의 전체가 활성
        """
        vol = float(volume_h1_usd or 0.0)
        if vol <= 0 or notional_usd <= 0:
            return 0.0
        liq = float(liquidity_usd or 0.0)
        # 기본 가정: 0.3% 수수료 (Uniswap V3 가장 흔한 tier)
        fee_rate = 0.003
        if fee_tier_bps and fee_tier_bps > 0:
            # dexscreener 의 feeTier 는 basis point 단위 (3000 = 0.3%)
            if fee_tier_bps > 100:
                fee_rate = fee_tier_bps / 1_000_000.0
            else:
                fee_rate = fee_tier_bps / 10_000.0
        # concentrated range 에서 notional 이 실효 유동성의 상당 부분을 차지
        # boost factor: tight range 일수록 fee capture 비율 상승 (±5% → 약 3배)
        tight_boost = max(10.0 / max(self.cfg.tge_tick_range_pct, 0.1), 1.0)
        share = notional_usd / max(liq + notional_usd, 1.0)
        share = min(share * tight_boost, 1.0)
        hold_frac = max(min(hold_minutes / 60.0, 24.0), 0.0)
        return max(vol * share * fee_rate * hold_frac, 0.0)

    async def trigger_tge_mint(
        self,
        pool_address: str,
        chain: str,
        token0: str = '',
        token1: str = '',
        ticker: str = '',
        reason: str = 'manual_tge',
    ) -> dict[str, Any]:
        """TGE 전용 민팅 진입점 — 기본 mint_position 보다 엄격한 검증."""
        if not self.cfg.tge_enabled:
            return self._skip('tge disabled', pool=pool_address, chain=chain)
        chain_norm = self._normalize_chain(chain)
        pool_norm = (pool_address or '').strip()
        if not pool_norm or not pool_norm.startswith('0x'):
            return self._skip('invalid pool_address', pool=pool_norm, chain=chain_norm)
        if chain_norm not in _NFPM_ADDRESSES:
            return self._skip(
                f'unsupported chain: {chain}', pool=pool_norm, chain=chain_norm
            )
        if self._kill_switch_active():
            return self._skip(
                'kill switch', pool=pool_norm, chain=chain_norm
            )

        # 풀 검증
        pool_info = await self._fetch_pool_info(chain_norm, pool_norm)
        if not pool_info:
            self.state.tge_total_skipped_liquidity += 1
            return self._skip(
                'pool not found on dexscreener',
                pool=pool_norm,
                chain=chain_norm,
            )
        liquidity_usd = pool_info.get('liquidity_usd') or 0.0
        volume_h1 = pool_info.get('volume_h1_usd') or 0.0
        current_price = pool_info.get('price_usd')
        fee_tier_bps = pool_info.get('fee_tier_bps')

        # 유동성 하한 — TGE 전용 임계값
        if liquidity_usd < self.cfg.tge_min_pool_liquidity_usd:
            self.state.tge_total_skipped_liquidity += 1
            return self._skip(
                f'tge pool liq ${liquidity_usd:.0f} < min '
                f'${self.cfg.tge_min_pool_liquidity_usd:.0f}',
                pool=pool_norm,
                chain=chain_norm,
            )
        # 볼륨 하한 — 수수료 캡처 최소 조건
        if volume_h1 < self.cfg.tge_min_volume_1h_usd:
            self.state.tge_total_skipped_volume += 1
            return self._skip(
                f'tge 1h vol ${volume_h1:.0f} < min '
                f'${self.cfg.tge_min_volume_1h_usd:.0f}',
                pool=pool_norm,
                chain=chain_norm,
            )

        # --- Gas-awareness: expected_fees > (min_expected_fees + gas_cost) ?
        notional = self.cfg.tge_notional_usd
        expected_fees = self._estimate_tge_fees(
            notional_usd=notional,
            liquidity_usd=liquidity_usd,
            volume_h1_usd=volume_h1,
            hold_minutes=self.cfg.tge_hold_minutes,
            fee_tier_bps=fee_tier_bps,
        )
        # 기대 수수료는 gas 비용도 커버해야 함 (mint 1회 = 1x gas)
        gas_cost = self.cfg.tge_gas_cost_per_tx_usd
        net_expected = expected_fees - gas_cost
        if expected_fees < self.cfg.tge_min_expected_fees_usd or net_expected <= 0:
            self.state.tge_total_skipped_gas += 1
            return self._skip(
                f'tge expected_fees ${expected_fees:.2f} < min '
                f'${self.cfg.tge_min_expected_fees_usd:.2f} or net<=0 (gas ${gas_cost})',
                pool=pool_norm,
                chain=chain_norm,
            )

        # --- Tight range (TGE-specific)
        range_pct = self.cfg.tge_tick_range_pct
        result = await self.mint_position(
            pool_address=pool_norm,
            chain=chain_norm,
            token0=token0 or pool_info.get('token0_address', ''),
            token1=token1 or pool_info.get('token1_address', ''),
            amount_usd=notional,
            tick_range_pct=range_pct,
        )
        if not result.get('ok'):
            return result
        # TGE 포지션 표시 — 모니터 루프가 re-range / volume-drop 체크
        pos = result.get('position') or {}
        pid = pos.get('position_id')
        if isinstance(pid, int) and pid in self.state.open_positions:
            rec = self.state.open_positions[pid]
            rec['is_tge'] = True
            rec['tge_reason'] = reason
            rec['tge_ticker'] = (ticker or '').upper()
            rec['tge_re_range_count'] = 0
            rec['tge_expected_fees_usd'] = round(expected_fees, 4)
            rec['tge_gas_cost_usd'] = round(gas_cost, 2)
            rec['tge_volume_h1_usd'] = volume_h1
            rec['tge_fee_tier_bps'] = fee_tier_bps
            rec['tge_pool_age_hours'] = pool_info.get('pool_age_hours')
            rec['tge_out_of_range_since'] = 0.0
            rec['tge_low_volume_since'] = 0.0
            # hold_expires override — TGE hold_minutes 기준
            rec['hold_expires_ts'] = (
                int(rec.get('ts') or time.time()) + self.cfg.tge_hold_minutes * 60
            )
            self.state.tge_total_mints += 1
            self.state.tge_last_mint_ts = float(rec.get('ts') or time.time())
            await self._append_record(rec)
            logger.info(
                '[lp][tge] mint pid=%d ticker=%s pool=%s liq=$%.0f vol1h=$%.0f '
                'expected_fees=$%.2f net=$%.2f range=±%.1f%% hold=%dmin',
                pid,
                rec.get('tge_ticker'),
                pool_norm,
                liquidity_usd,
                volume_h1,
                expected_fees,
                net_expected,
                range_pct,
                self.cfg.tge_hold_minutes,
            )
            await self._notify(
                f'🧪 [LP TGE] mint pid={pid} {chain_norm} {rec.get("tge_ticker") or ""}\n'
                f'pool {pool_norm[:10]}…\n'
                f'  amount=${notional:.0f} range=±{range_pct}% '
                f'expected_fees=${expected_fees:.2f} (gas ${gas_cost})'
            )
        return result

    async def _tge_monitor_loop(self) -> None:
        """TGE 전용 백그라운드 감시 — re-range + volume-drop + sustained-range-exit."""
        while self._running:
            try:
                await self._tge_monitor_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning('[lp][tge] monitor tick err: %s', exc)
            try:
                await asyncio.sleep(self.cfg.tge_re_range_check_sec)
            except asyncio.CancelledError:
                raise

    async def _tge_monitor_tick(self) -> None:
        # TGE 포지션만 대상
        tge_positions = [
            (pid, rec)
            for pid, rec in list(self.state.open_positions.items())
            if rec.get('is_tge')
        ]
        if not tge_positions:
            return
        now = time.time()
        for pid, rec in tge_positions:
            pool = rec.get('pool') or ''
            chain = rec.get('chain') or ''
            if not pool or not chain:
                continue
            try:
                info = await self._fetch_pool_info(chain, pool)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[lp][tge] pid=%d fetch err: %s', pid, exc)
                continue
            if not info:
                continue
            price = info.get('price_usd')
            vol_h1 = info.get('volume_h1_usd') or 0.0
            lo = float(rec.get('range_low') or 0.0)
            hi = float(rec.get('range_high') or 0.0)
            if price is None or lo <= 0 or hi <= 0:
                continue

            # ----- 1) Sustained range-exit check
            in_range = lo <= price <= hi
            if not in_range:
                since = float(rec.get('tge_out_of_range_since') or 0.0)
                if since <= 0:
                    rec['tge_out_of_range_since'] = now
                elif (now - since) >= self.cfg.tge_range_exit_grace_sec:
                    logger.info(
                        '[lp][tge] pid=%d sustained range-exit (%.2fs >= %ds), closing',
                        pid,
                        now - since,
                        self.cfg.tge_range_exit_grace_sec,
                    )
                    try:
                        await self._tge_close(pid, chain, reason='tge_range_exit')
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            '[lp][tge] range-exit close err pid=%d: %s', pid, exc
                        )
                    continue
            else:
                rec['tge_out_of_range_since'] = 0.0

            # ----- 2) Volume-drop check (10min sustained)
            if vol_h1 < self.cfg.tge_min_volume_1h_usd:
                vsince = float(rec.get('tge_low_volume_since') or 0.0)
                if vsince <= 0:
                    rec['tge_low_volume_since'] = now
                elif (now - vsince) >= self.cfg.tge_volume_drop_grace_sec:
                    logger.info(
                        '[lp][tge] pid=%d volume dropped $%.0f < $%.0f for %.0fs, closing',
                        pid,
                        vol_h1,
                        self.cfg.tge_min_volume_1h_usd,
                        now - vsince,
                    )
                    try:
                        await self._tge_close(pid, chain, reason='tge_volume_drop')
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            '[lp][tge] volume-drop close err pid=%d: %s', pid, exc
                        )
                    continue
            else:
                rec['tge_low_volume_since'] = 0.0

            # ----- 3) Re-range check (within 80% of half-range toward boundary)
            if in_range and self.cfg.tge_re_range_max > 0:
                rr_count = int(rec.get('tge_re_range_count') or 0)
                if rr_count < self.cfg.tge_re_range_max:
                    mid = (lo + hi) / 2.0
                    half = (hi - lo) / 2.0
                    if half > 0:
                        # 현재가가 half-range 의 80% 를 넘어 경계 방향으로 치우쳤는가
                        displacement = abs(price - mid) / half
                        if displacement >= self.cfg.tge_range_boundary_threshold:
                            logger.info(
                                '[lp][tge] pid=%d near boundary (disp=%.2f >= %.2f), '
                                're-ranging (%d/%d)',
                                pid,
                                displacement,
                                self.cfg.tge_range_boundary_threshold,
                                rr_count + 1,
                                self.cfg.tge_re_range_max,
                            )
                            try:
                                await self._tge_re_range(pid, info)
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    '[lp][tge] re-range err pid=%d: %s', pid, exc
                                )

    async def _tge_re_range(self, pid: int, info: dict[str, Any]) -> None:
        """TGE 포지션 re-range: close + reopen at current price.

        Gas-awareness: re-range = 2x gas cost (remove + mint). 남은 기대수익이
        2x gas 이상이어야 수행. 3회 hard cap.
        """
        rec = self.state.open_positions.get(pid)
        if not rec:
            return
        rr_count = int(rec.get('tge_re_range_count') or 0)
        if rr_count >= self.cfg.tge_re_range_max:
            logger.info('[lp][tge] pid=%d re-range cap reached (%d)', pid, rr_count)
            return

        # 남은 hold 시간 기준 expected_fees 계산
        now = time.time()
        hold_expiry = float(rec.get('hold_expires_ts') or 0.0)
        remaining_sec = max(hold_expiry - now, 0.0)
        remaining_min = int(remaining_sec / 60.0)
        if remaining_min <= 0:
            logger.info('[lp][tge] pid=%d no hold remaining, skipping re-range', pid)
            return

        notional = float(rec.get('amount_usd') or self.cfg.tge_notional_usd)
        expected_fees = self._estimate_tge_fees(
            notional_usd=notional,
            liquidity_usd=info.get('liquidity_usd'),
            volume_h1_usd=info.get('volume_h1_usd'),
            hold_minutes=remaining_min,
            fee_tier_bps=info.get('fee_tier_bps'),
        )
        re_range_gas = self.cfg.tge_gas_cost_per_tx_usd * 2.0
        if expected_fees <= re_range_gas:
            logger.info(
                '[lp][tge] pid=%d re-range uneconomic: expected $%.2f <= gas $%.2f',
                pid,
                expected_fees,
                re_range_gas,
            )
            return

        chain = rec.get('chain') or ''
        pool = rec.get('pool') or ''
        token0 = rec.get('token0') or ''
        token1 = rec.get('token1') or ''
        ticker = rec.get('tge_ticker') or ''

        # 1) 기존 포지션 close
        close_result = await self.close_position(
            pid, chain, reason=f'tge_re_range_{rr_count + 1}'
        )
        if not close_result.get('ok'):
            logger.warning('[lp][tge] pid=%d close for re-range failed', pid)
            return

        # 2) 같은 pool 에 재진입 (새 position_id)
        new_result = await self.mint_position(
            pool_address=pool,
            chain=chain,
            token0=token0,
            token1=token1,
            amount_usd=notional,
            tick_range_pct=self.cfg.tge_tick_range_pct,
        )
        if not new_result.get('ok'):
            logger.warning('[lp][tge] pid=%d re-range new mint failed', pid)
            return
        new_pos = new_result.get('position') or {}
        new_pid = new_pos.get('position_id')
        if isinstance(new_pid, int) and new_pid in self.state.open_positions:
            new_rec = self.state.open_positions[new_pid]
            new_rec['is_tge'] = True
            new_rec['tge_reason'] = rec.get('tge_reason') or 'tge_re_range'
            new_rec['tge_ticker'] = ticker
            new_rec['tge_re_range_count'] = rr_count + 1
            new_rec['tge_parent_pid'] = pid
            # remaining hold time 유지 (새 포지션이 기존 만료시각 이어받음)
            new_rec['hold_expires_ts'] = int(hold_expiry)
            new_rec['tge_out_of_range_since'] = 0.0
            new_rec['tge_low_volume_since'] = 0.0
            await self._append_record(new_rec)
        self.state.tge_total_re_ranges += 1
        logger.info(
            '[lp][tge] re-range done %d→%s count=%d/%d expected=$%.2f gas=$%.2f',
            pid,
            new_pid,
            rr_count + 1,
            self.cfg.tge_re_range_max,
            expected_fees,
            re_range_gas,
        )

    async def _tge_close(self, pid: int, chain: str, reason: str) -> dict[str, Any]:
        """TGE 포지션 전용 close — close_position 래퍼 + TGE 통계 갱신."""
        rec = self.state.open_positions.get(pid)
        start_ts = float(rec.get('ts') or time.time()) if rec else time.time()
        amount_usd = float(rec.get('amount_usd') or 0.0) if rec else 0.0
        prev_fees = float(rec.get('fees_collected_usd') or 0.0) if rec else 0.0

        result = await self.close_position(pid, chain, reason=reason)
        if not result.get('ok'):
            return result

        closed = result.get('position') or {}
        closed_ts = float(closed.get('closed_ts') or time.time())
        total_fees = float(closed.get('fees_collected_usd') or prev_fees)
        hold_min = max((closed_ts - start_ts) / 60.0, 0.0)

        self.state.tge_total_closes += 1
        self.state.tge_total_fees_collected_usd += total_fees
        self.state.tge_total_hold_minutes += hold_min
        logger.info(
            '[lp][tge] close pid=%d reason=%s fees=$%.4f hold=%.1fmin amount=$%.0f',
            pid,
            reason,
            total_fees,
            hold_min,
            amount_usd,
        )
        return result

    # ------------------------------------------------------------------
    # 헬퍼
    # ------------------------------------------------------------------

    def _normalize_chain(self, chain: str) -> str:
        c = (chain or '').strip().lower()
        if c in {'ethereum', 'eth', 'mainnet'}:
            return 'ethereum'
        if c == 'base':
            return 'base'
        return c

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _maybe_rollover_daily(self) -> None:
        now_midnight = _today_midnight_epoch()
        if now_midnight > self.state.daily_reset_epoch:
            self.state.daily_reset_epoch = now_midnight
            self.state.daily_spent_usd = 0.0

    def _next_mock_position_id(self) -> int:
        self._mock_position_counter += 1
        return self._mock_position_counter

    def _skip(self, reason: str, **ctx: Any) -> dict[str, Any]:
        self.state.total_skipped += 1
        self.state.last_error = reason
        logger.info('[lp] skip: %s %s', reason, ctx)
        return {'ok': False, 'code': 'SKIPPED', 'reason': reason, **ctx}

    async def _append_record(self, record: dict[str, Any]) -> None:
        async with self._write_lock:
            try:
                Path(self.cfg.positions_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.cfg.positions_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('[lp] append record failed: %s', exc)

    def _restore_open_positions(self) -> None:
        """JSONL 재생 — 동일 position_id 의 가장 최신 레코드만 채택.

        status=='active' 이면 메모리 복원, 'closed' 이면 제외.
        """
        path = Path(self.cfg.positions_path)
        if not path.exists():
            return
        latest: dict[int, dict[str, Any]] = {}
        try:
            with path.open('r', encoding='utf-8') as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    pid = rec.get('position_id')
                    if not isinstance(pid, int):
                        continue
                    latest[pid] = rec
        except Exception as exc:  # noqa: BLE001
            logger.warning('[lp] restore read failed: %s', exc)
            return
        restored = 0
        for pid, rec in latest.items():
            if rec.get('status') == 'active':
                self.state.open_positions[pid] = rec
                restored += 1
                # mock counter 가 기존 id 와 충돌 안 하도록 최대값 추적
                if pid > self._mock_position_counter:
                    self._mock_position_counter = pid
        if restored:
            logger.info('[lp] restored %d active positions', restored)

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        try:
            prim = getattr(self.telegram, '_send_message', None)
            if prim is not None:
                try:
                    await prim(text, alert_key=alert_key)
                    return
                except TypeError:
                    await prim(text)
                    return
            send = getattr(self.telegram, 'send_message', None)
            if send is None:
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[lp] telegram err: %s', exc)
