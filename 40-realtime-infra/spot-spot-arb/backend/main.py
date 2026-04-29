"""FastAPI ?뷀듃由ы룷?명듃 ??REST API + WebSocket."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.exchanges import manager as exchange_manager
from backend.exchanges.bithumb_private import close_bithumb, init_bithumb
from backend.exchanges.types import GapResult
from backend.services.auto_trigger import AutoTriggerService
from backend.services.auto_exit_service import AutoExitService
from backend.services.auto_transfer_service import AutoTransferService
from backend.services.bithumb_follower import BithumbFollower
from backend.services.bridge_client import BridgeClient
from backend.services.commodity_basis_arb import CommodityBasisArb
from backend.services.cross_listing_long import CrossListingLong
from backend.services.dex_trader import DexTrader
from backend.services.futures_futures_scanner import FuturesFuturesScanner
from backend.services.gap_recorder import GapRecorder
from backend.services.hack_halt_detector import HackHaltDetector
from backend.services.hedge_trade_service import HedgeTradeService
from backend.services.leverage_gap_scanner import LeverageGapScanner
from backend.services.listing_detector import ListingDetector
from backend.services.listing_executor import ListingExecutor
from backend.services.lp_manager import LPManager
from backend.services.margin_sell_arb import MarginSellArb
from backend.services.ntt_rate_limit_scanner import NttRateLimitScanner
from backend.services.oracle_divergence_short import OracleDivergenceShort
from backend.services.poller import PollerService
from backend.services.preposition_hedge import PrePositionHedge
from backend.services.system_health import (
    SystemHealthAggregator,
    register_endpoints as _register_system_health_endpoints,
)
from backend.services.theddari_scanner import TheddariScanner
from backend.services.wallet_tracker import WalletTracker
from backend.services.withdraw_service import WithdrawService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

# ?꾩뿭 ?대윭 ?몄뒪?댁뒪
poller = PollerService()
withdraw_service = WithdrawService()
hedge_trade_service = HedgeTradeService()
theddari_scanner = TheddariScanner()  # 별도 데이터 소스로 유지 (auto_trigger는 poller 직접 사용)
auto_trigger_service = AutoTriggerService(
    poller=poller,
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
    wide_scanner=None,
)
auto_exit_service = AutoExitService(
    poller=poller,
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
auto_transfer_service = AutoTransferService(
    poller=poller,
    bithumb_client=None,
    withdraw_service=withdraw_service,
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
gap_recorder = GapRecorder(poller=poller)
ff_scanner = FuturesFuturesScanner(
    poller=poller,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
    hedge_service=hedge_trade_service,
)
leverage_gap_scanner = LeverageGapScanner(
    poller=poller,
    gap_recorder=gap_recorder,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
    # Phase 4 에서 브릿지 클라이언트가 on_leverage_gap 콜백을 구독한다
    on_leverage_gap=None,
)
# Phase 4: 현현 브릿지 클라이언트 — dex_trader 는 Phase 3 에서 채워질 예정
bridge_client = BridgeClient(
    dex_trader=None,
    leverage_gap_scanner=leverage_gap_scanner,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# NTT 레이트리밋 감지기 (detect-only) — bridge_client 가 이벤트 subscribe.
# SPACE/CYBER 형 2x 스프레드 구간을 사전 탐지하기 위한 관측자.
ntt_rate_limit_scanner = NttRateLimitScanner(
    bridge_client=bridge_client,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
listing_detector = ListingDetector(
    poller=None,  # Phase 1: detect-only, no execution
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Phase 2: 상장 감지 → CEX 자동 숏. add_listener 로 in-process 팬아웃 구독.
listing_executor = ListingExecutor(
    listing_detector=listing_detector,
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Phase 3: 상장 감지 → DEX 자동 매수 (Uniswap V3 / PancakeSwap V2).
# 트리플 락 + kill switch (data/KILL_DEX) 로 실자금 경로 차단.
dex_trader = DexTrader(
    listing_detector=listing_detector,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Phase 5: Bithumb 후따리 — Upbit 상장 뒤 Bithumb 체결 개시 시 저가 매수 후 수렴 대기
bithumb_follower = BithumbFollower(
    listing_detector=listing_detector,
    bithumb_client=None,  # lazy import → backend.exchanges.bithumb_private 사용
    poller=poller,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Phase 6: Uniswap V3 LP 자동예치/회수 — 상장 직후 집중 유동성 공급 후 range 이탈/타임아웃 시 회수
lp_manager = LPManager(
    dex_trader=None,  # Phase 3/4 dex_trader 가 준비되면 주입
    listing_detector=listing_detector,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Wallet Tracker — 온체인 감시 지갑의 CEX 입금 감지 → 자동 숏/알럿 (Pika+pann+plusev 카탈로그 기반)
wallet_tracker = WalletTracker(
    dex_trader=dex_trader,
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# 현-선 암살 (Pika 스타일 사전 헷지) — gap이 neutral zone일 때 진입, widening 방향으로 수익
preposition_hedge = PrePositionHedge(
    poller=poller,
    hedge_service=hedge_trade_service,
    listing_detector=listing_detector,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Margin Sell Arb — Bybit 마진 차입 + spot 매도 + Binance perp 롱 (Pika+pann+plusev top-3 HIGH)
margin_sell_arb = MarginSellArb(
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Oracle Divergence Short — pre-IPO / 저유동성 perp (Ventuals/HL HIP-3/Ostium/Bybit pre-market).
# markPrice 오라클이 공정가치 대비 +N% 과대평가되면 SHORT. funding cap 패치 감지 시 즉시 청산.
oracle_divergence_short = OracleDivergenceShort(
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Commodity Basis Arb — Ostium/HL/MEXC/Bybit 원자재 perp 크로스-벤유 basis 스프레드
# (plusevdeal #7, JUSTCRYT #1). LIVE 는 CCXT 가능 벤유 페어만, Ostium 은 Phase X.1 stub.
commodity_basis_arb = CommodityBasisArb(
    hedge_service=hedge_trade_service,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Hack Halt Detector — Upbit/Bithumb/Bybit/Binance/OKX 공지 + 온체인 exploit
# → 현선 헷지 또는 선물 숏 자동 진입 (pannpunch #260 Upbit SOL 핫월렛 해킹 전략)
hack_halt_detector = HackHaltDetector(
    hedge_service=hedge_trade_service,
    wallet_tracker=wallet_tracker,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)
# Cross-Listing Follow Long — Bithumb 단독 상장 → Upbit 견상 가능성 선제 LONG (Pika #5)
# listing_detector 팬아웃 구독. score >= threshold 시 10분 관망 후 Binance perp 롱.
# Upbit 공지 뜨면 win 청산, 6h timeout 또는 -10% stop loss.
cross_listing_long = CrossListingLong(
    listing_detector=listing_detector,
    hedge_service=hedge_trade_service,
    poller=poller,
    telegram_service=poller._telegram if hasattr(poller, '_telegram') else None,
)

# WebSocket ?곌껐 愿由?# value: 援щ룆 以묒씤 ?곗빱 吏묓빀
_ws_connections: dict[WebSocket, set[str]] = {}


# ------------------------------------------------------------------
# 吏곷젹???ы띁
# ------------------------------------------------------------------

def _gap_result_to_dict(result: GapResult) -> dict[str, Any]:
    """GapResult瑜?WebSocket ?몄떆 ?뺤떇?쇰줈 蹂?섑븳??"""
    bithumb = result.bithumb
    withdrawal = bithumb.withdrawal_limit

    bithumb_networks_list = [
        {
            'network': n.network,
            'deposit': n.deposit,
            'withdraw': n.withdraw,
            'fee': n.fee,
        }
        for n in bithumb.networks
    ]

    bithumb_dict: dict[str, Any] = {
        'ask': bithumb.ask,
        'usdt_krw_last': bithumb.usdt_krw_last,
        'withdrawal_limit': {
            'onetime_coin': withdrawal.onetime_coin if withdrawal else None,
            'onetime_krw': withdrawal.onetime_krw if withdrawal else None,
            'daily_coin': withdrawal.daily_coin if withdrawal else None,
            'daily_krw': withdrawal.daily_krw if withdrawal else None,
            'remaining_daily_coin': withdrawal.remaining_daily_coin if withdrawal else None,
            'remaining_daily_krw': withdrawal.remaining_daily_krw if withdrawal else None,
            'expected_fee': withdrawal.expected_fee if withdrawal else None,
            'min_withdraw': withdrawal.min_withdraw if withdrawal else None,
        },
        'networks': bithumb_networks_list,
    }

    exchanges_dict: dict[str, Any] = {}
    for exchange_name, ex_data in result.exchanges.items():
        networks_list = [
            {
                'network': n.network,
                'deposit': n.deposit,
                'withdraw': n.withdraw,
                'fee': n.fee,
            }
            for n in ex_data.networks
        ]
        exchanges_dict[exchange_name] = {
            'spot': {
                'supported': ex_data.spot_supported,
                'bid': ex_data.spot_bbo.bid if ex_data.spot_bbo else None,
                'ask': ex_data.spot_bbo.ask if ex_data.spot_bbo else None,
                'gap': round(ex_data.spot_gap) if ex_data.spot_gap is not None else None,
            },
            'futures': {
                'supported': ex_data.futures_supported,
                'bid': ex_data.futures_bbo.bid if ex_data.futures_bbo else None,
                'ask': ex_data.futures_bbo.ask if ex_data.futures_bbo else None,
                'gap': round(ex_data.futures_gap) if ex_data.futures_gap is not None else None,
            },
            'networks': networks_list,
            'margin': {
                'supported': ex_data.margin.supported,
            },
            'loan': {
                'supported': ex_data.loan.supported,
            },
        }

    return {
        'type': 'gap_update',
        'ticker': result.ticker,
        'timestamp': result.timestamp,
        'bithumb': bithumb_dict,
        'exchanges': exchanges_dict,
    }


def _cleanup_tickers(tickers: set[str]) -> None:
    """?ㅻⅨ WebSocket ?곌껐?먯꽌 ?ъ슜?섏? ?딅뒗 ?곗빱瑜??대윭?먯꽌 ?쒓굅?쒕떎."""
    all_watched: set[str] = set()
    for subs in _ws_connections.values():
        all_watched |= subs
    for t in tickers:
        if t not in all_watched:
            poller.unsubscribe_ticker(t)


# ------------------------------------------------------------------
# Lifespan
# ------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """???쒖옉/醫낅즺 ??嫄곕옒??珥덇린??諛??대윭 ?쒖옉/以묒?."""
    logger.info('Initializing Bithumb...')
    await init_bithumb()
    logger.info('Initializing exchanges...')
    await exchange_manager.init_exchanges()
    logger.info('Starting poller...')
    await poller.start()
    # auto-trigger는 poller가 실데이터를 쌓을 시간 10초 확보 후 시작
    await asyncio.sleep(10)
    logger.info('Starting theddari scanner...')
    await theddari_scanner.start()
    logger.info('Starting auto-trigger...')
    await auto_trigger_service.start()
    logger.info('Starting auto-exit...')
    await auto_exit_service.start()
    logger.info('Starting auto-transfer...')
    await auto_transfer_service.start()
    logger.info('Starting gap recorder...')
    await gap_recorder.start()
    logger.info('Starting futures-futures scanner...')
    await ff_scanner.start()
    logger.info('Starting leverage-gap scanner...')
    await leverage_gap_scanner.start()
    logger.info('Starting bridge client (Phase 4 — 현현 브릿지)...')
    await bridge_client.start()
    logger.info('Starting NTT rate limit scanner...')
    # bridge_client 에 NTT 이벤트 구독 시키기 (있을 때만)
    try:
        if hasattr(bridge_client, '_on_ntt_event'):
            ntt_rate_limit_scanner.subscribe(bridge_client._on_ntt_event)
    except Exception as exc:  # noqa: BLE001
        logger.debug('[main] ntt subscribe wiring err: %s', exc)
    await ntt_rate_limit_scanner.start()
    logger.info('Starting listing detector...')
    await listing_detector.start()
    # Phase 2 executor — detector 가 running 상태일 때만 등록 (detector 내부 add_listener)
    logger.info('Starting listing executor (Phase 2 — CEX 자동 숏)...')
    await listing_executor.start()
    # Phase 3 dex_trader — detector 이벤트 공유 구독 (Uniswap/Pancake 즉시 매수)
    logger.info('Starting dex trader (Phase 3 — DEX 즉시 매수)...')
    await dex_trader.start()
    # Phase 5 follower — detector add_listener 재사용
    logger.info('Starting bithumb follower (Phase 5 — 후따리)...')
    await bithumb_follower.start()
    # Phase 6 LP manager — detector add_listener 재사용
    logger.info('Starting LP manager (Phase 6 — Uniswap V3 LP)...')
    await lp_manager.start()
    # Wallet tracker — 온체인 감시 (CEX 핫월렛 입금 감지 → 자동 숏/알럿)
    logger.info('Starting wallet tracker...')
    await wallet_tracker.start()
    # Margin sell arb — Bybit 차입 + Binance perp 롱 (Pika+pann+plusev top-3)
    logger.info('Starting margin sell arb...')
    await margin_sell_arb.start()
    # Oracle Divergence Short — pre-IPO / 저유동성 perp 스캐너
    logger.info('Starting oracle divergence short...')
    await oracle_divergence_short.start()
    # Commodity Basis Arb — RWA commodity perp 크로스-벤유 basis
    logger.info('Starting commodity basis arb...')
    await commodity_basis_arb.start()
    # Hack Halt Detector — CEX 공지/핫월렛 해킹/온체인 exploit → 자동 헷지
    logger.info('Starting hack halt detector...')
    await hack_halt_detector.start()
    # Cross-Listing Follow Long — Bithumb 단독 → Upbit 견상 선제 롱
    logger.info('Starting cross-listing long (Pika #5)...')
    await cross_listing_long.start()
    # 현-선 암살 사전 헷지 — listing_detector 준비 후 시작
    logger.info('Starting preposition hedge (현-선 암살)...')
    await preposition_hedge.start()
    yield
    logger.info('Stopping preposition hedge...')
    await preposition_hedge.stop()
    logger.info('Stopping cross-listing long...')
    await cross_listing_long.stop()
    logger.info('Stopping hack halt detector...')
    await hack_halt_detector.stop()
    logger.info('Stopping commodity basis arb...')
    await commodity_basis_arb.stop()
    logger.info('Stopping oracle divergence short...')
    await oracle_divergence_short.stop()
    logger.info('Stopping margin sell arb...')
    await margin_sell_arb.stop()
    logger.info('Stopping wallet tracker...')
    await wallet_tracker.stop()
    logger.info('Stopping LP manager...')
    await lp_manager.stop()
    logger.info('Stopping bithumb follower...')
    await bithumb_follower.stop()
    logger.info('Stopping dex trader...')
    await dex_trader.stop()
    logger.info('Stopping listing executor...')
    await listing_executor.stop()
    logger.info('Stopping listing detector...')
    await listing_detector.stop()
    logger.info('Stopping NTT rate limit scanner...')
    await ntt_rate_limit_scanner.stop()
    logger.info('Stopping bridge client...')
    await bridge_client.stop()
    logger.info('Stopping leverage-gap scanner...')
    await leverage_gap_scanner.stop()
    logger.info('Stopping futures-futures scanner...')
    await ff_scanner.stop()
    logger.info('Stopping gap recorder...')
    await gap_recorder.stop()
    logger.info('Stopping auto-transfer...')
    await auto_transfer_service.stop()
    logger.info('Stopping auto-exit...')
    await auto_exit_service.stop()
    logger.info('Stopping auto-trigger...')
    await auto_trigger_service.stop()
    logger.info('Stopping theddari scanner...')
    await theddari_scanner.stop()
    logger.info('Stopping poller...')
    await poller.stop()
    logger.info('Closing exchanges...')
    await exchange_manager.close_exchanges()
    await close_bithumb()
    logger.info('Shutdown complete')


# ------------------------------------------------------------------
# FastAPI ??# ------------------------------------------------------------------

app = FastAPI(title='Bithumb Arbitrage Dashboard', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


# ------------------------------------------------------------------
# REST ?붾뱶?ъ씤??# ------------------------------------------------------------------

@app.get('/api/health')
async def health_check():
    """?ъ뒪 泥댄겕."""
    return {'status': 'ok', 'timestamp': int(time.time())}


@app.get('/api/auto/status')
async def auto_status():
    return auto_trigger_service.status()


@app.post('/api/auto/dry-run')
async def auto_set_dry_run(payload: dict[str, Any]):
    dry_run = bool(payload.get('dry_run', True))
    auto_trigger_service.set_dry_run(dry_run)
    return {'ok': True, 'dry_run': dry_run}


@app.post('/api/auto/enabled')
async def auto_set_enabled(payload: dict[str, Any]):
    enabled = bool(payload.get('enabled', True))
    auto_trigger_service.set_enabled(enabled)
    return {'ok': True, 'enabled': enabled}


@app.get('/api/auto/exit-status')
async def auto_exit_status():
    return auto_exit_service.status()


@app.get('/api/auto/transfer-status')
async def auto_transfer_status():
    """Bithumb→Binance 전송 아비트라지 서비스 상태 + 최근 job."""
    return auto_transfer_service.status()


@app.post('/api/auto/transfer-start')
async def auto_transfer_start(payload: dict[str, Any]):
    """수동 트리거 — 특정 티커에 대해 즉시 전송 시도.

    Body: {"ticker": "SPK", "dry_run": true}
    """
    ticker = str(payload.get('ticker') or '').strip().upper()
    dry_run = payload.get('dry_run')
    dry_run_arg = None if dry_run is None else bool(dry_run)
    return await auto_transfer_service.trigger_manual(
        ticker=ticker,
        dry_run=dry_run_arg,
    )


@app.post('/api/auto/transfer-abort')
async def auto_transfer_abort(payload: dict[str, Any]):
    """진행 중 transfer job 을 긴급 abort (상태 전이만 — 실 출금 취소 아님)."""
    job_id = str(payload.get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    return auto_transfer_service.abort_job(job_id)


@app.post('/api/auto/transfer-enabled')
async def auto_transfer_set_enabled(payload: dict[str, Any]):
    enabled = bool(payload.get('enabled', False))
    auto_transfer_service.set_enabled(enabled)
    return {'ok': True, 'enabled': enabled}


@app.post('/api/auto/transfer-dry-run')
async def auto_transfer_set_dry_run(payload: dict[str, Any]):
    dry_run = bool(payload.get('dry_run', True))
    auto_transfer_service.set_dry_run(dry_run)
    return {'ok': True, 'dry_run': dry_run}


@app.post('/api/hedge/close')
async def hedge_close(payload: dict[str, Any]):
    """수동 / 자동 close: {ticker, reason?} — 현재 open hedge 를 청산."""
    ticker = str(payload.get('ticker') or '').strip().upper()
    reason = str(payload.get('reason') or 'manual')
    if not ticker:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
    return await hedge_trade_service.close_job(ticker=ticker, reason=reason)


# ------------------------------------------------------------------
# 현-선 암살 (Pre-position hedge) — Pika 스타일 사전 헷지
# ------------------------------------------------------------------

@app.get('/api/auto/preposition-status')
async def preposition_status():
    return preposition_hedge.status()


@app.post('/api/auto/preposition-enter')
async def preposition_enter(payload: dict[str, Any]):
    """수동 진입: {ticker, target_exchange?, notional_usd?, expected_gap_widening_pct?, max_hold_hours?}."""
    ticker = str(payload.get('ticker') or '').strip().upper()
    if not ticker:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
    target_exchange = str(payload.get('target_exchange') or 'binance').strip().lower()
    notional_usd = payload.get('notional_usd')
    try:
        notional_usd = float(notional_usd) if notional_usd is not None else None
    except (TypeError, ValueError):
        notional_usd = None
    try:
        widening = float(payload.get('expected_gap_widening_pct') or 5.0)
    except (TypeError, ValueError):
        widening = 5.0
    try:
        hold_hours = float(payload.get('max_hold_hours') or 48.0)
    except (TypeError, ValueError):
        hold_hours = 48.0
    return await preposition_hedge.enter_preposition(
        ticker=ticker,
        target_exchange=target_exchange,
        notional_usd=notional_usd,
        expected_gap_widening_pct=widening,
        max_hold_hours=hold_hours,
        trigger_reason=str(payload.get('trigger_reason') or 'manual_api'),
    )


@app.post('/api/auto/preposition-exit')
async def preposition_exit(payload: dict[str, Any]):
    job_id = str(payload.get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    reason = str(payload.get('reason') or 'manual_api')
    return await preposition_hedge.exit_preposition(job_id=job_id, reason=reason)


@app.post('/api/auto/preposition-watchlist')
async def preposition_set_watchlist(payload: dict[str, Any]):
    """전체 watchlist 교체: {entries: [{ticker, expected_gap_widening_pct, max_hold_hours, note?}]}."""
    entries = payload.get('entries')
    if not isinstance(entries, list):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'entries[] required'}
    return preposition_hedge.set_watchlist(entries)


@app.delete('/api/auto/preposition-watchlist/{ticker}')
async def preposition_remove_watchlist(ticker: str):
    return preposition_hedge.remove_watchlist(ticker)


@app.get('/api/auto/gap-recorder')
async def gap_recorder_status():
    return gap_recorder.status()


@app.get('/api/auto/gap-stats')
async def gap_stats(hours: int = 24, min_samples: int = 10):
    """최근 N시간 동안 ticker × exchange별 futures_gap 통계 + 역프/김프 카운트."""
    return {'hours': hours, 'stats': gap_recorder.stats(hours=hours, min_samples=min_samples)}


@app.get('/api/auto/ff-scanner')
async def ff_scanner_status(limit: int = 20):
    """선선갭 스캐너 상태 + 최근 opportunity top N."""
    return {
        'status': ff_scanner.status(),
        'recent_opportunities': ff_scanner.recent_opportunities(limit=limit),
    }


@app.get('/api/auto/leverage-gap')
async def leverage_gap_status(limit: int = 20):
    """레갭 스캐너 (Phase 7 — detect only) 상태 + 최근 N분 내 펌프 이벤트."""
    return {
        'status': leverage_gap_scanner.status(),
        'recent_events': leverage_gap_scanner.recent_events(limit=limit),
    }


@app.get('/api/auto/ntt-scanner')
async def ntt_scanner_status(limit: int = 20):
    """NTT(Wormhole) 레이트리밋 스캐너 상태 + 현재 capacity + 최근 이벤트."""
    return {
        'status': ntt_rate_limit_scanner.status(),
        'current_capacities': ntt_rate_limit_scanner.current_capacities(),
        'recent_events': ntt_rate_limit_scanner.recent_events(limit=limit),
    }


# ------------------------------------------------------------------
# Bridge (Phase 4 — 현현 크로스체인 브릿지) endpoints
# ------------------------------------------------------------------


@app.get('/api/auto/bridge-status')
async def bridge_status(limit: int = 50):
    """브릿지 클라이언트 상태 + 최근 N개 job."""
    return {
        'status': bridge_client.status(),
        'recent_jobs': bridge_client.recent_jobs(limit=limit),
    }


@app.post('/api/auto/bridge')
async def bridge_request(payload: dict[str, Any]):
    """수동 브릿지 실행.

    Body: {"from_chain":"bsc","to_chain":"base","token":"USDC","amount":100,
           "amount_usd":100}  # amount_usd 는 stable 이면 생략 가능
    """
    body = payload or {}
    from_chain = str(body.get('from_chain') or '').strip().lower()
    to_chain = str(body.get('to_chain') or '').strip().lower()
    token = str(body.get('token') or '').strip().upper()
    try:
        amount = float(body.get('amount') or 0.0)
    except (TypeError, ValueError):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount must be number'}
    amount_usd_raw = body.get('amount_usd')
    amount_usd = None
    if amount_usd_raw is not None:
        try:
            amount_usd = float(amount_usd_raw)
        except (TypeError, ValueError):
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount_usd must be number'}
    return await bridge_client.bridge(
        from_chain=from_chain,
        to_chain=to_chain,
        token=token,
        amount_tokens=amount,
        amount_usd=amount_usd,
        origin='manual',
    )


@app.post('/api/auto/bridge-quote')
async def bridge_quote(payload: dict[str, Any]):
    """브릿지 사전 견적 (route 유효성 + 수수료/ETA 추정). 온체인 호출 아님.

    Body: {"from_chain":"bsc","to_chain":"base","token":"USDC","amount":100}
    """
    body = payload or {}
    try:
        amount = float(body.get('amount') or 0.0)
    except (TypeError, ValueError):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount must be number'}
    return await bridge_client.get_bridge_quote(
        from_chain=str(body.get('from_chain') or '').strip().lower(),
        to_chain=str(body.get('to_chain') or '').strip().lower(),
        token=str(body.get('token') or '').strip().upper(),
        amount=amount,
    )


@app.get('/api/auto/bridge-jobs/{job_id}')
async def bridge_job_get(job_id: str):
    """개별 브릿지 job 조회 (도착 상태 확인용)."""
    job = bridge_client.get_job(job_id)
    if job is None:
        return {'ok': False, 'code': 'JOB_NOT_FOUND', 'message': 'job not found'}
    return {'ok': True, 'job': job}


@app.post('/api/auto/bridge-abort')
async def bridge_abort(payload: dict[str, Any]):
    """진행 중 브릿지 job 을 abort (상태 전이만 — 온체인 불가역)."""
    job_id = str((payload or {}).get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    return bridge_client.abort_job(job_id)


@app.get('/api/auto/wide-scanner')
async def wide_scanner_status(limit: int = 20):
    """theddari 광역 스캐너 현재 상태 + 극단 gap 상위 N개 시그널."""
    return {
        'status': theddari_scanner.status(),
        'extreme_signals': theddari_scanner.extreme_signals(limit=limit),
    }


@app.get('/api/auto/listing-detector')
async def listing_detector_status(limit: int = 20):
    """상장 감지기 (Phase 1 — detect only) 상태 + 최근 이벤트."""
    return {
        'status': listing_detector.status(),
        'recent_events': listing_detector.recent_events(limit=limit),
    }


@app.get('/api/auto/listing-executor')
async def listing_executor_status(limit: int = 20):
    """상장 실행기 (Phase 2 — CEX 자동 숏) 상태 + 최근 실행 기록."""
    return {
        'status': listing_executor.status(),
        'recent_jobs': listing_executor.recent_jobs(limit=limit),
    }


@app.post('/api/auto/listing-close')
async def listing_close(payload: dict[str, Any]):
    """상장 숏 포지션을 수동 청산한다.

    Body: {"ticker": "CHIP", "reason": "manual"}
    """
    ticker = str((payload or {}).get('ticker') or '').strip().upper()
    reason = str((payload or {}).get('reason') or 'manual')
    if not ticker:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}
    return await listing_executor.close(ticker=ticker, reason=reason)


# ------------------------------------------------------------------
# Oracle Divergence Short — pre-IPO / 저유동성 perp 스캐너
# ------------------------------------------------------------------

@app.get('/api/auto/oracle-div-status')
async def oracle_div_status(limit: int = 20):
    """Oracle divergence 스캐너 상태 + 최근 jobs."""
    return {
        'status': oracle_divergence_short.status(),
        'recent_jobs': oracle_divergence_short.recent_jobs(limit=limit),
    }


@app.post('/api/auto/oracle-div-enter')
async def oracle_div_enter(payload: dict[str, Any]):
    """수동 진입: {symbol, exchange, notional_usd?}."""
    body = payload or {}
    symbol = str(body.get('symbol') or '').strip().upper()
    exchange = str(body.get('exchange') or '').strip().lower()
    if not symbol or not exchange:
        return {'ok': False, 'code': 'INVALID_INPUT',
                'message': 'symbol and exchange required'}
    notional_usd = body.get('notional_usd')
    notional_arg: Any = None
    if notional_usd is not None:
        try:
            notional_arg = float(notional_usd)
        except (TypeError, ValueError):
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'notional_usd must be number'}
    return await oracle_divergence_short.enter_manual(
        symbol=symbol, exchange=exchange, notional_usd=notional_arg,
    )


@app.post('/api/auto/oracle-div-exit')
async def oracle_div_exit(payload: dict[str, Any]):
    """수동 청산: {job_id, reason?}."""
    body = payload or {}
    job_id = str(body.get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    reason = str(body.get('reason') or 'manual')
    return await oracle_divergence_short.exit_manual(job_id=job_id, reason=reason)


@app.post('/api/auto/oracle-watchlist')
async def oracle_watchlist_add(payload: dict[str, Any]):
    """Watchlist 엔트리 추가/업데이트.

    Body: {"exchange":"ventuals","symbol":"SPACEX",
           "reference_price_src":"notional_fdv",
           "reference_fdv":3.5e11,"circulating_supply":1.5e9,"notes":"..."}
    """
    return oracle_divergence_short.add_watch_entry(payload or {})


@app.delete('/api/auto/oracle-watchlist/{symbol}')
async def oracle_watchlist_remove(symbol: str, exchange: str = ''):
    """Watchlist 엔트리 제거. exchange query 로 특정 거래소만 지정 가능."""
    return oracle_divergence_short.remove_watch_entry(
        symbol=symbol, exchange=exchange or None,
    )


# ------------------------------------------------------------------
# Cross-Listing Follow Long — Bithumb 단독 → Upbit 견상 선제 롱
# ------------------------------------------------------------------

@app.get('/api/auto/cross-listing-status')
async def cross_listing_status(limit: int = 20):
    """견상 long 서비스 상태 + 최근 jobs."""
    return {
        'status': cross_listing_long.status(),
        'recent_jobs': cross_listing_long.recent_jobs(limit=limit),
    }


@app.post('/api/auto/cross-listing-enter')
async def cross_listing_enter(payload: dict[str, Any]):
    """수동 진입 트리거: {ticker, notional?}. score/observe skip 하고 바로 진입."""
    body = payload or {}
    ticker = str(body.get('ticker') or '').strip().upper()
    if not ticker:
        return {'ok': False, 'code': 'INVALID_INPUT',
                'message': 'ticker required'}
    notional_raw = body.get('notional') or body.get('notional_usd')
    notional_arg: Any = None
    if notional_raw is not None:
        try:
            notional_arg = float(notional_raw)
        except (TypeError, ValueError):
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'notional must be number'}
    return await cross_listing_long.enter_manual(
        ticker=ticker, notional_usd=notional_arg,
    )


@app.post('/api/auto/cross-listing-exit')
async def cross_listing_exit(payload: dict[str, Any]):
    """수동 청산: {job_id, reason?}."""
    body = payload or {}
    job_id = str(body.get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    reason = str(body.get('reason') or 'manual')
    return await cross_listing_long.exit_manual(job_id=job_id, reason=reason)


# ------------------------------------------------------------------
# Commodity Basis Arb — RWA commodity cross-venue basis
# ------------------------------------------------------------------

@app.get('/api/auto/commodity-basis-status')
async def commodity_basis_status(limit: int = 20):
    """Commodity basis arb 상태 + 최근 jobs + 현재 감지된 스프레드 기회."""
    return {
        'status': commodity_basis_arb.status(),
        'recent_jobs': commodity_basis_arb.recent_jobs(limit=limit),
    }


@app.post('/api/auto/commodity-basis-enter')
async def commodity_basis_enter(payload: dict[str, Any]):
    """수동 진입: {symbol, buy_exchange, sell_exchange, notional?}."""
    body = payload or {}
    symbol = str(body.get('symbol') or '').strip().upper()
    buy_exchange = str(body.get('buy_exchange') or '').strip().lower()
    sell_exchange = str(body.get('sell_exchange') or '').strip().lower()
    if not symbol or not buy_exchange or not sell_exchange:
        return {
            'ok': False, 'code': 'INVALID_INPUT',
            'message': 'symbol, buy_exchange, sell_exchange required',
        }
    notional_raw = body.get('notional') or body.get('notional_usd')
    notional_arg: Any = None
    if notional_raw is not None:
        try:
            notional_arg = float(notional_raw)
        except (TypeError, ValueError):
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'notional must be number'}
    return await commodity_basis_arb.enter_manual(
        symbol=symbol,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        notional_usd=notional_arg,
    )


@app.post('/api/auto/commodity-basis-exit')
async def commodity_basis_exit(payload: dict[str, Any]):
    """수동 청산: {job_id, reason?}."""
    body = payload or {}
    job_id = str(body.get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT',
                'message': 'job_id required'}
    reason = str(body.get('reason') or 'manual')
    return await commodity_basis_arb.exit_manual(job_id=job_id, reason=reason)


@app.post('/api/auto/commodity-watchlist')
async def commodity_watchlist_add(payload: dict[str, Any]):
    """Commodity watchlist 엔트리 추가/업데이트.

    Body: {"symbol":"BRENT", "venues":[{"exchange":"...","symbol":"...","type":"perp"},...],
           "min_spread_pct":0.15, "notes":"..."}
    """
    return commodity_basis_arb.add_watch_entry(payload or {})


@app.delete('/api/auto/commodity-watchlist/{symbol}')
async def commodity_watchlist_remove(symbol: str):
    """Commodity watchlist 엔트리 제거."""
    return commodity_basis_arb.remove_watch_entry(symbol=symbol)


# ------------------------------------------------------------------
# Hack Halt Detector — CEX 해킹/정지/Exploit 감지 + 자동 헷지
# ------------------------------------------------------------------

@app.get('/api/auto/hack-halt-status')
async def hack_halt_status(limit: int = 20):
    """Hack/Halt 감지기 상태 + 최근 이벤트 + open 포지션."""
    return {
        'status': hack_halt_detector.status(),
        'recent_events': hack_halt_detector.recent_events(limit=limit),
    }


@app.post('/api/auto/hack-halt-abort')
async def hack_halt_abort(payload: dict[str, Any]):
    """Hack/Halt open 포지션 강제 청산.

    Body: {"job_id": "hh_...", "reason": "manual"}
    """
    body = payload or {}
    job_id = str(body.get('job_id') or '').strip()
    reason = str(body.get('reason') or 'manual')
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    return await hack_halt_detector.abort(job_id=job_id, reason=reason)


@app.get('/api/auto/dex-trader')
async def dex_trader_status(limit: int = 20):
    """DEX 매수기 (Phase 3) 상태 + 최근 jobs."""
    return {
        'status': dex_trader.status(),
        'recent_jobs': dex_trader.recent_jobs(limit=limit),
    }


@app.post('/api/auto/dex-buy')
async def dex_buy_manual(payload: dict[str, Any]):
    """수동 DEX 매수 (정상 상장 감지 이벤트가 아닐 때).

    Body: {"ticker": "CHIP", "contract": "0x...", "chain": "base",
           "amount_usd": 100, "slippage_pct": 5}
    """
    body = payload or {}
    ticker = str(body.get('ticker') or '').strip().upper()
    contract = str(body.get('contract') or body.get('contract_address') or '').strip()
    chain = str(body.get('chain') or '').strip().lower()
    try:
        amount_usd = float(body.get('amount_usd') or 0.0)
    except (TypeError, ValueError):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount_usd must be number'}
    slippage_raw = body.get('slippage_pct', body.get('slippage', 5.0))
    try:
        slippage_pct = float(slippage_raw)
    except (TypeError, ValueError):
        slippage_pct = 5.0
    if not ticker or not contract or not chain:
        return {'ok': False, 'code': 'INVALID_INPUT',
                'message': 'ticker, contract, chain required'}
    return await dex_trader.buy_on_dex(
        ticker=ticker,
        contract_address=contract,
        chain=chain,
        amount_usd=amount_usd,
        slippage_pct=slippage_pct,
    )


@app.post('/api/auto/dex-sell')
async def dex_sell_manual(payload: dict[str, Any]):
    """수동 DEX 매도 (포지션 청산).

    Body: {"ticker": "CHIP", "contract": "0x...", "chain": "base",
           "amount_tokens": 12345.67, "slippage_pct": 5}
    """
    body = payload or {}
    ticker = str(body.get('ticker') or '').strip().upper()
    contract = str(body.get('contract') or body.get('contract_address') or '').strip()
    chain = str(body.get('chain') or '').strip().lower()
    try:
        amount_tokens = float(body.get('amount_tokens') or 0.0)
    except (TypeError, ValueError):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount_tokens must be number'}
    slippage_raw = body.get('slippage_pct', body.get('slippage', 5.0))
    try:
        slippage_pct = float(slippage_raw)
    except (TypeError, ValueError):
        slippage_pct = 5.0
    if not ticker or not contract or not chain:
        return {'ok': False, 'code': 'INVALID_INPUT',
                'message': 'ticker, contract, chain required'}
    return await dex_trader.sell_on_dex(
        ticker=ticker,
        contract_address=contract,
        chain=chain,
        amount_tokens=amount_tokens,
        slippage_pct=slippage_pct,
    )


@app.get('/api/auto/dex-price')
async def dex_price_query(contract: str, chain: str = 'base'):
    """Dexscreener USD price 조회 (디버깅용). `contract` + `chain` query."""
    price = await dex_trader.get_dex_price(contract, chain)
    return {'ok': price > 0, 'contract': contract, 'chain': chain, 'price_usd': price}


# ------------------------------------------------------------------
# Wallet Tracker — 온체인 감시 지갑 → CEX 입금 감지
# ------------------------------------------------------------------

@app.get('/api/auto/wallet-tracker')
async def wallet_tracker_status(limit: int = 20):
    """지갑 트래커 상태 + 최근 N개 이벤트 + 현재 watchlist."""
    return {
        'status': wallet_tracker.status(),
        'recent_events': wallet_tracker.recent_events(limit=limit),
        'watchlist': wallet_tracker.watchlist(),
    }


@app.post('/api/auto/wallet-watchlist')
async def wallet_watchlist_add(payload: dict[str, Any]):
    """감시 지갑 추가.

    Body: {"label": "gsr_mm", "address": "0x...", "chains": ["ethereum", "base"],
           "action": "short_hedge" | "alert" | "dex_dump_detector",
           "token_filter": ["ARIA"]}
    """
    body = payload or {}
    label = str(body.get('label') or '').strip()
    address = str(body.get('address') or '').strip()
    chains = body.get('chains') or []
    action = str(body.get('action') or body.get('action_on_cex_deposit') or 'alert').strip()
    token_filter = body.get('token_filter') or []
    if not isinstance(chains, list):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'chains must be list'}
    if not isinstance(token_filter, list):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'token_filter must be list'}
    return wallet_tracker.add_watch(
        label=label,
        address=address,
        chains=[str(c) for c in chains],
        action_on_cex_deposit=action,
        token_filter=[str(t) for t in token_filter],
    )


@app.delete('/api/auto/wallet-watchlist/{label}')
async def wallet_watchlist_remove(label: str):
    """감시 지갑 제거."""
    return wallet_tracker.remove_watch(label)


@app.get('/api/auto/follower-status')
async def follower_status(limit: int = 50):
    """Bithumb 후따리 (Phase 5) 상태 + 최근 jobs."""
    return {
        'status': bithumb_follower.status(),
        'recent_jobs': bithumb_follower.recent_jobs(limit=limit),
    }


@app.post('/api/auto/follower-abort')
async def follower_abort(payload: dict[str, Any]):
    """후따리 watch/monitor 강제 종료 (진입 전이면 취소, 진입 후면 즉시 시장가 매도).

    Body: {"ticker": "CHIP"}
    """
    ticker = str((payload or {}).get('ticker') or '').strip().upper()
    if not ticker:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}
    return bithumb_follower.abort_ticker(ticker)


# ------------------------------------------------------------------
# Phase 6: LP Manager (Uniswap V3 집중 유동성) 엔드포인트
# ------------------------------------------------------------------

@app.get('/api/auto/lp-status')
async def lp_status_ep(limit: int = 50):
    """LP 매니저 상태 + 최근 포지션 jsonl."""
    return {
        'status': lp_manager.status(),
        'recent_positions': lp_manager.recent_positions(limit=limit),
    }


@app.get('/api/auto/lp-positions')
async def lp_positions_ep(chain: str = ''):
    """현재 열린 LP 포지션 목록. chain 인자로 필터 가능."""
    positions = await lp_manager.list_positions(chain=chain)
    return {'positions': positions, 'count': len(positions)}


@app.post('/api/auto/lp-mint')
async def lp_mint_ep(payload: dict[str, Any]):
    """수동 LP 민팅.

    Body: {"chain":"base","pool":"0x...","amount_usd":100,"range_pct":10,
           "token0":"0x...","token1":"0x..."}
    """
    payload = payload or {}
    chain = str(payload.get('chain') or '').strip().lower()
    pool = str(payload.get('pool') or payload.get('pool_address') or '').strip()
    if not chain or not pool:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'chain and pool required'}
    amount_usd = float(payload.get('amount_usd') or 0) or 0.0
    range_pct = float(payload.get('range_pct') or 0) or 0.0
    token0 = str(payload.get('token0') or '').strip()
    token1 = str(payload.get('token1') or '').strip()
    try:
        return await lp_manager.mint_position(
            pool_address=pool,
            chain=chain,
            token0=token0,
            token1=token1,
            amount_usd=amount_usd,
            tick_range_pct=range_pct,
        )
    except NotImplementedError as exc:
        return {'ok': False, 'code': 'NOT_IMPLEMENTED', 'message': str(exc)}


@app.post('/api/auto/lp-close')
async def lp_close_ep(payload: dict[str, Any]):
    """LP 포지션 회수 + NFT burn.

    Body: {"position_id":12345,"chain":"base","reason":"manual"}
    """
    payload = payload or {}
    position_id = payload.get('position_id')
    chain = str(payload.get('chain') or '').strip().lower()
    reason = str(payload.get('reason') or 'manual')
    if position_id is None or not chain:
        return {
            'ok': False,
            'code': 'INVALID_INPUT',
            'message': 'position_id and chain required',
        }
    try:
        return await lp_manager.close_position(
            position_id=int(position_id), chain=chain, reason=reason
        )
    except NotImplementedError as exc:
        return {'ok': False, 'code': 'NOT_IMPLEMENTED', 'message': str(exc)}
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': str(exc)}


@app.post('/api/auto/lp-collect')
async def lp_collect_ep(payload: dict[str, Any]):
    """LP 포지션의 미수령 수수료 수령.

    Body: {"position_id":12345,"chain":"base"}
    """
    payload = payload or {}
    position_id = payload.get('position_id')
    chain = str(payload.get('chain') or '').strip().lower()
    if position_id is None or not chain:
        return {
            'ok': False,
            'code': 'INVALID_INPUT',
            'message': 'position_id and chain required',
        }
    try:
        return await lp_manager.collect_fees(
            position_id=int(position_id), chain=chain
        )
    except NotImplementedError as exc:
        return {'ok': False, 'code': 'NOT_IMPLEMENTED', 'message': str(exc)}
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': str(exc)}


@app.post('/api/auto/lp-tge-mint')
async def lp_tge_mint_ep(payload: dict[str, Any]):
    """TGE 전용 LP 민팅 (Pika #3 "신" 전략).

    tight-range(±5%) + gas-aware + 풀 검증(liquidity/volume/age) + 자동 re-range.

    Body: {"chain":"ethereum","pool":"0x...","token0":"0x...","token1":"0x...",
           "ticker":"CHIP","reason":"manual_tge"}
    """
    payload = payload or {}
    chain = str(payload.get('chain') or '').strip().lower()
    pool = str(payload.get('pool') or payload.get('pool_address') or '').strip()
    ticker = str(payload.get('ticker') or '').strip().upper()
    reason = str(payload.get('reason') or 'manual_tge')
    token0 = str(payload.get('token0') or '').strip()
    token1 = str(payload.get('token1') or '').strip()
    if not chain or not pool:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'chain and pool required'}
    try:
        return await lp_manager.trigger_tge_mint(
            pool_address=pool,
            chain=chain,
            token0=token0,
            token1=token1,
            ticker=ticker,
            reason=reason,
        )
    except NotImplementedError as exc:
        return {'ok': False, 'code': 'NOT_IMPLEMENTED', 'message': str(exc)}
    except (TypeError, ValueError) as exc:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': str(exc)}


@app.get('/api/tickers')
async def get_tickers():
    """?꾩껜 媛먯떆 ?곗빱 + WebSocket 援щ룆 ?곗빱 諛섑솚."""
    return {
        'all_tickers': poller.get_all_tickers(),
        'ws_tickers': poller.get_ws_tickers(),
    }


@app.post('/api/tickers')
async def add_ticker(body: dict):
    """WebSocket 援щ룆 ?곗빱 異붽?.

    Body: {"ticker": "BTC"}
    """
    ticker = str(body.get('ticker', '')).strip().upper()
    if not ticker:
        return {'error': 'ticker is required'}, 400
    poller.subscribe_ticker(ticker)
    return {
        'all_tickers': poller.get_all_tickers(),
        'ws_tickers': poller.get_ws_tickers(),
    }


@app.delete('/api/tickers/{ticker}')
async def remove_ticker(ticker: str):
    """WebSocket 援щ룆 ?곗빱 ?쒓굅."""
    poller.unsubscribe_ticker(ticker.upper())
    return {
        'all_tickers': poller.get_all_tickers(),
        'ws_tickers': poller.get_ws_tickers(),
    }


# ------------------------------------------------------------------
# ?ㅽ듃?뚰겕 媛먯떆 由ъ뒪???붾뱶?ъ씤??# ------------------------------------------------------------------

@app.get('/api/price-mute-list')
async def get_price_mute_list():
    """媛寃??뚮┝ mute 由ъ뒪?몃? 諛섑솚?쒕떎."""
    return {'items': poller.get_price_mute_items()}


@app.post('/api/price-mute-list')
async def add_price_mute(body: dict):
    """媛寃??뚮┝ mute ??ぉ??異붽??쒕떎.

    Body: {"exchange": "binance", "ticker": "BTC"}
    """
    exchange = str(body.get('exchange', '')).strip()
    ticker = str(body.get('ticker', '')).strip().upper()
    if not exchange or not ticker:
        return {'error': 'exchange, ticker are required'}, 400
    added = poller.add_price_mute_item(exchange, ticker)
    return {
        'added': added,
        'items': poller.get_price_mute_items(),
    }


@app.delete('/api/price-mute-list')
async def remove_price_mute(body: dict):
    """媛寃??뚮┝ mute ??ぉ???쒓굅?쒕떎.

    Body: {"exchange": "binance", "ticker": "BTC"}
    """
    exchange = str(body.get('exchange', '')).strip()
    ticker = str(body.get('ticker', '')).strip().upper()
    if not exchange or not ticker:
        return {'error': 'exchange, ticker are required'}, 400
    removed = poller.remove_price_mute_item(exchange, ticker)
    return {
        'removed': removed,
        'items': poller.get_price_mute_items(),
    }


@app.get('/api/network-watchlist')
async def get_network_watchlist():
    """?ㅽ듃?뚰겕 媛먯떆 由ъ뒪?몃? 諛섑솚?쒕떎."""
    return {'items': poller.network_watcher.get_items()}


@app.post('/api/network-watchlist')
async def add_network_watch(body: dict):
    """?ㅽ듃?뚰겕 媛먯떆 ??ぉ??異붽??쒕떎.

    Body: {"exchange": "binance", "ticker": "BTC", "network": "BTC"}
    """
    exchange = str(body.get('exchange', '')).strip()
    ticker = str(body.get('ticker', '')).strip().upper()
    network = str(body.get('network', '')).strip()
    if not exchange or not ticker or not network:
        return {'error': 'exchange, ticker, network are required'}, 400
    added = poller.network_watcher.add_item(exchange, ticker, network)
    return {
        'added': added,
        'items': poller.network_watcher.get_items(),
    }


@app.post('/api/test/fake-network')
async def fake_network(body: dict):
    """?뚯뒪?몄슜: ?ㅽ듃?뚰겕 ?곹깭瑜?媛吏쒕줈 二쇱엯?섍퀬 蹂??媛먯?瑜?利됱떆 ?ㅽ뻾?쒕떎.

    Body: {"exchange": "gate", "ticker": "IOTX", "network": "IOTX",
           "deposit": true, "withdraw": false}
    """
    exchange = str(body.get('exchange', '')).strip().lower()
    ticker = str(body.get('ticker', '')).strip().upper()
    network_name = str(body.get('network', '')).strip()
    deposit = bool(body.get('deposit', False))
    withdraw = bool(body.get('withdraw', False))

    from backend.exchanges.types import NetworkInfo as NI

    cache = poller.network_cache
    if exchange not in cache:
        cache[exchange] = {}
    cache[exchange][ticker] = [
        NI(network=network_name, deposit=deposit, withdraw=withdraw),
    ]

    # 利됱떆 蹂??媛먯? ?ㅽ뻾
    await poller.network_watcher.check_changes(cache)

    return {
        'status': 'ok',
        'injected': {
            'exchange': exchange,
            'ticker': ticker,
            'network': network_name,
            'deposit': deposit,
            'withdraw': withdraw,
        },
    }


@app.delete('/api/network-watchlist')
async def remove_network_watch(body: dict):
    """?ㅽ듃?뚰겕 媛먯떆 ??ぉ???쒓굅?쒕떎.

    Body: {"exchange": "binance", "ticker": "BTC", "network": "BTC"}
    """
    exchange = str(body.get('exchange', '')).strip()
    ticker = str(body.get('ticker', '')).strip().upper()
    network = str(body.get('network', '')).strip()
    if not exchange or not ticker or not network:
        return {'error': 'exchange, ticker, network are required'}, 400
    removed = poller.network_watcher.remove_item(exchange, ticker, network)
    return {
        'removed': removed,
        'items': poller.network_watcher.get_items(),
    }


# ------------------------------------------------------------------
# 異쒓툑 ?먮룞???붾뱶?ъ씤??# ------------------------------------------------------------------

@app.post('/api/withdraw/preview')
async def withdraw_preview(body: dict):
    """異쒓툑 ?ㅽ뻾 ??寃利??섎웾 怨꾩궛 ?꾨━酉?

    Body: {
      "ticker": "BTC",
      "target_exchange": "binance",
      "withdraw_network": "BTC",
      "deposit_network": "BTC"
    }
    """
    ticker = str(body.get('ticker', '')).strip().upper()
    target_exchange = str(body.get('target_exchange', '')).strip().lower()
    withdraw_network = str(body.get('withdraw_network', '')).strip()
    deposit_network = str(body.get('deposit_network', '')).strip()

    return await withdraw_service.preview(
        ticker=ticker,
        target_exchange=target_exchange,
        withdraw_network=withdraw_network,
        deposit_network=deposit_network,
    )


@app.post('/api/withdraw/execute')
async def withdraw_execute(body: dict):
    """異쒓툑 ?ㅽ뻾.

    Body: {"preview_token": "<token>"}
    """
    preview_token = str(body.get('preview_token', '')).strip()
    return await withdraw_service.execute(preview_token=preview_token)


@app.get('/api/withdraw/jobs')
async def list_withdraw_jobs(limit: int = 100):
    """異쒓툑 ?묒뾽 ?대젰 議고쉶."""
    safe_limit = max(1, min(int(limit), 500))
    return {'items': withdraw_service.list_jobs(limit=safe_limit)}


@app.get('/api/withdraw/jobs/{job_id}')
async def get_withdraw_job(job_id: str):
    """?⑥씪 異쒓툑 ?묒뾽 議고쉶."""
    job = withdraw_service.get_job(job_id)
    if not job:
        return {'ok': False, 'code': 'JOB_NOT_FOUND', 'message': 'job not found'}
    return {'ok': True, 'job': job}


# ------------------------------------------------------------------
# Hedge trade endpoints
# ------------------------------------------------------------------

@app.post('/api/hedge/enter')
async def hedge_enter(body: dict):
    """Hedge entry: Bithumb spot buy + overseas futures short."""
    ticker = str(body.get('ticker', '')).strip().upper()
    futures_exchange = str(body.get('futures_exchange', '')).strip().lower()
    nominal_usd = body.get('nominal_usd')
    leverage = body.get('leverage')

    return await hedge_trade_service.enter(
        ticker=ticker,
        futures_exchange=futures_exchange,
        nominal_usd=nominal_usd,
        leverage=leverage,
    )


@app.get('/api/hedge/jobs')
async def list_hedge_jobs(limit: int = 100, ticker: str | None = None):
    """List recent hedge trade logs."""
    safe_limit = max(1, min(int(limit), 500))
    normalized_ticker = str(ticker).strip().upper() if ticker else None
    return {
        'items': hedge_trade_service.list_jobs(
            limit=safe_limit,
            ticker=normalized_ticker,
        )
    }


@app.get('/api/hedge/latest')
async def get_latest_hedge_job(ticker: str):
    """Get latest tracked hedge job for ticker."""
    normalized_ticker = str(ticker or '').strip().upper()
    if not normalized_ticker:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}
    job = hedge_trade_service.get_latest_active_job(normalized_ticker)
    if not job:
        return {'ok': False, 'code': 'NOT_FOUND', 'message': 'tracked hedge job not found'}
    return {'ok': True, 'job': job}


@app.post('/api/hedge/refresh')
async def refresh_hedge_job(body: dict):
    """Refresh latest tracked hedge job and sync close-fill based PnL."""
    ticker = str(body.get('ticker', '')).strip().upper()
    exit_spot_exchange = body.get('exit_spot_exchange')
    exit_futures_exchange = body.get('exit_futures_exchange')
    return await hedge_trade_service.refresh_latest_job(
        ticker=ticker,
        exit_spot_exchange=(
            str(exit_spot_exchange).strip().lower() if exit_spot_exchange is not None else None
        ),
        exit_futures_exchange=(
            str(exit_futures_exchange).strip().lower()
            if exit_futures_exchange is not None
            else None
        ),
    )


# ------------------------------------------------------------------
# FF (Futures-Futures arbitrage) endpoints
# ------------------------------------------------------------------

@app.post('/api/auto/ff-enter')
async def ff_enter(payload: dict[str, Any]):
    """수동 FF 진입.

    Body: {"ticker":"BTC","buy_exchange":"gate","sell_exchange":"bybit",
           "notional_usd":30,"leverage":3}
    """
    if hedge_trade_service is None:
        return {'ok': False, 'code': 'UNAVAILABLE', 'message': 'hedge_service not available'}
    ticker = str(payload.get('ticker') or '').strip().upper()
    buy_exchange = str(payload.get('buy_exchange') or '').strip().lower()
    sell_exchange = str(payload.get('sell_exchange') or '').strip().lower()
    notional_usd = payload.get('notional_usd')
    leverage = payload.get('leverage')
    return await hedge_trade_service.enter_ff(
        ticker=ticker,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        notional_usd=notional_usd,
        leverage=leverage,
    )


@app.post('/api/auto/ff-close')
async def ff_close(payload: dict[str, Any]):
    """FF 포지션 청산.

    Body: {"ticker":"BTC","reason":"manual"}
    """
    if hedge_trade_service is None:
        return {'ok': False, 'code': 'UNAVAILABLE', 'message': 'hedge_service not available'}
    ticker = str(payload.get('ticker') or '').strip().upper()
    reason = str(payload.get('reason') or 'manual')
    return await hedge_trade_service.close_ff(ticker=ticker, reason=reason)


# ------------------------------------------------------------------
# Margin Sell Arb (Pika+pann+plusev top-3 HIGH) 엔드포인트
# ------------------------------------------------------------------

@app.get('/api/auto/margin-arb-status')
async def margin_arb_status_ep(limit: int = 20):
    """마진셀 arb 상태 + 열려있는 jobs + 최근 opportunities."""
    return {
        'status': margin_sell_arb.status(),
        'open_jobs': margin_sell_arb.open_jobs(),
        'opportunities': margin_sell_arb.recent_opportunities(limit=limit),
    }


@app.post('/api/auto/margin-arb-enter')
async def margin_arb_enter_ep(payload: dict[str, Any]):
    """수동 마진셀 arb 진입.

    Body: {"ticker":"BARD","borrow_qty":100,
           "borrow_exchange":"bybit","perp_exchange":"binance","leverage":3}
    """
    payload = payload or {}
    ticker = str(payload.get('ticker') or '').strip().upper()
    if not ticker:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
    try:
        borrow_qty = float(payload.get('borrow_qty') or 0) or 0.0
    except (TypeError, ValueError):
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'borrow_qty must be number'}
    borrow_exchange = str(payload.get('borrow_exchange') or 'bybit').strip().lower()
    perp_exchange = str(payload.get('perp_exchange') or 'binance').strip().lower()
    leverage = int(payload.get('leverage') or 0) or None
    try:
        return await margin_sell_arb.enter_arb(
            ticker=ticker,
            borrow_exchange=borrow_exchange,
            perp_exchange=perp_exchange,
            borrow_qty=borrow_qty,
            leverage=leverage,
            origin='manual',
        )
    except NotImplementedError as exc:
        return {'ok': False, 'code': 'NOT_IMPLEMENTED', 'message': str(exc)}


@app.post('/api/auto/margin-arb-exit')
async def margin_arb_exit_ep(payload: dict[str, Any]):
    """마진셀 arb 청산 — perp 종료 + spot 매수 + loan 상환.

    Body: {"job_id":"abc123..."}
    """
    payload = payload or {}
    job_id = str(payload.get('job_id') or '').strip()
    if not job_id:
        return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'job_id required'}
    try:
        return await margin_sell_arb.exit_arb(job_id=job_id)
    except NotImplementedError as exc:
        return {'ok': False, 'code': 'NOT_IMPLEMENTED', 'message': str(exc)}


# ------------------------------------------------------------------
# System Health — 전체 서비스 통합 대시보드 + 글로벌 kill switch
# ------------------------------------------------------------------
# 모든 서비스 인스턴스를 한 곳에 모아 통합 health check/metrics 를 노출하고,
# 원클릭 긴급 정지(14개+ kill switch 파일 일괄 생성) 엔드포인트를 제공한다.

_system_services: dict[str, Any] = {
    'poller': poller,
    'theddari_scanner': theddari_scanner,
    'auto_trigger': auto_trigger_service,
    'auto_exit': auto_exit_service,
    'auto_transfer': auto_transfer_service,
    'gap_recorder': gap_recorder,
    'ff_scanner': ff_scanner,
    'leverage_gap_scanner': leverage_gap_scanner,
    'bridge_client': bridge_client,
    'ntt_rate_limit_scanner': ntt_rate_limit_scanner,
    'listing_detector': listing_detector,
    'listing_executor': listing_executor,
    'dex_trader': dex_trader,
    'bithumb_follower': bithumb_follower,
    'lp_manager': lp_manager,
    'wallet_tracker': wallet_tracker,
    'preposition_hedge': preposition_hedge,
    'margin_sell_arb': margin_sell_arb,
    'oracle_divergence_short': oracle_divergence_short,
    'commodity_basis_arb': commodity_basis_arb,
    'hack_halt_detector': hack_halt_detector,
    'cross_listing_long': cross_listing_long,
    'hedge_trade_service': hedge_trade_service,
    'withdraw_service': withdraw_service,
}
system_health = SystemHealthAggregator(
    services_dict=_system_services,
    telegram_service=(
        poller._telegram if hasattr(poller, '_telegram') else None
    ),
)
_register_system_health_endpoints(app, system_health)


# ------------------------------------------------------------------
# WebSocket ?붾뱶?ъ씤??# ------------------------------------------------------------------

@app.websocket('/ws')
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket ?곌껐 ??援щ룆 ?곗빱?????二쇨린?곸쑝濡?媛??곗씠???몄떆."""
    await websocket.accept()
    _ws_connections[websocket] = set()
    logger.info('WebSocket connected. Total: %d', len(_ws_connections))

    push_task: asyncio.Task | None = None

    async def push_loop() -> None:
        """援щ룆 以묒씤 ?곗빱??媛??곗씠?곕? 二쇨린?곸쑝濡??꾩넚?쒕떎."""
        while True:
            await asyncio.sleep(2)
            subscribed = _ws_connections.get(websocket, set())
            if not subscribed:
                continue
            state = poller.state
            messages = []
            for ticker in subscribed:
                result = state.get(ticker)
                if result:
                    messages.append(_gap_result_to_dict(result))
            for msg in messages:
                try:
                    await websocket.send_json(msg)
                except Exception:
                    return

    try:
        push_task = asyncio.create_task(push_loop())

        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type')

            if msg_type == 'subscribe':
                new_tickers = set(str(t).upper() for t in data.get('tickers', []))
                old_tickers = _ws_connections.get(websocket, set())
                _ws_connections[websocket] = new_tickers

                # Incremental update: subscribe only newly added tickers.
                added = new_tickers - old_tickers
                for t in added:
                    poller.subscribe_ticker(t)

                # ?쒓굅???곗빱 ?뺣━ (?ㅻⅨ ?곌껐?먯꽌?????곗씠硫??대윭?먯꽌 ?쒓굅)
                removed = old_tickers - new_tickers
                if removed:
                    _cleanup_tickers(removed)

                logger.info(
                    'WebSocket subscribe update: total=%d, added=%d, removed=%d',
                    len(new_tickers),
                    len(added),
                    len(removed),
                )
                # Immediate snapshot only for newly added tickers.
                state = poller.state
                for ticker in added:
                    result = state.get(ticker)
                    if result:
                        await websocket.send_json(_gap_result_to_dict(result))

            elif msg_type == 'unsubscribe':
                tickers = set(str(t).upper() for t in data.get('tickers', []))
                current = _ws_connections.get(websocket, set())
                _ws_connections[websocket] = current - tickers
                _cleanup_tickers(tickers)

    except WebSocketDisconnect:
        logger.info('WebSocket disconnected')
    except Exception as exc:
        logger.error('WebSocket error: %s', exc)
    finally:
        if push_task:
            push_task.cancel()
            try:
                await push_task
            except asyncio.CancelledError:
                pass
        disconnected_tickers = _ws_connections.pop(websocket, set())
        if disconnected_tickers:
            _cleanup_tickers(disconnected_tickers)
        logger.info('WebSocket cleaned up. Total: %d', len(_ws_connections))
