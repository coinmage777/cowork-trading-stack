"""Hedge entry service for Bithumb spot + overseas futures short."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import ccxt.async_support as ccxt

from backend import config
from backend.exchanges import manager as exchange_manager
from backend.exchanges.bithumb_private import (
    fetch_bithumb_order,
    fetch_bithumb_bbo,
    fetch_usdt_krw,
    submit_bithumb_spot_order,
)
from backend.services.hedge_jobs import HedgeJobStore
from backend.services.hedge_status import classify_hedge_status
from backend.services.withdraw_jobs import WithdrawJobStore

logger = logging.getLogger(__name__)

_QTY_EPSILON = 1e-8


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _filled_from_order(order: dict[str, Any]) -> float:
    filled = _safe_float(order.get('filled'))
    if filled is not None and filled > 0:
        return filled

    trades = order.get('trades')
    if isinstance(trades, list):
        total = 0.0
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            amount = _safe_float(trade.get('amount'))
            if amount and amount > 0:
                total += amount
        if total > 0:
            return total

    return 0.0


def _avg_from_order(order: dict[str, Any], filled: float) -> float | None:
    average = _safe_float(order.get('average'))
    if average is not None and average > 0:
        return average
    cost = _safe_float(order.get('cost'))
    if cost is not None and cost > 0 and filled > 0:
        return cost / filled

    info = order.get('info')
    if isinstance(info, dict):
        info_average = _safe_float(info.get('avg_price'))
        if info_average is not None and info_average > 0:
            return info_average
        executed_funds = _safe_float(info.get('executed_funds'))
        if executed_funds is not None and executed_funds > 0 and filled > 0:
            return executed_funds / filled
    return None


class HedgeTradeService:
    """Executes hedge-entry flow with best-effort rollback and adjustment."""

    def __init__(self) -> None:
        self._jobs = HedgeJobStore()
        self._lock = asyncio.Lock()

    def list_jobs(self, limit: int = 100, ticker: str | None = None) -> list[dict[str, Any]]:
        return self._jobs.list_jobs(limit=limit, ticker=ticker)

    def get_latest_active_job(self, ticker: str) -> dict[str, Any] | None:
        return self._jobs.latest_active_job(ticker=ticker)

    def get_latest_open_job(self, ticker: str) -> dict[str, Any] | None:
        return self._jobs.latest_open_job(ticker=ticker)

    def _resolve_expected_exit_spot_qty(
        self,
        job: dict[str, Any],
        spot_exchange: str,
    ) -> tuple[float, list[str], str]:
        entry_qty_spot = max(_safe_float(job.get('entry_qty_spot')) or 0.0, 0.0)
        if entry_qty_spot <= _QTY_EPSILON:
            return 0.0, [], 'entry_qty'

        ticker = str(job.get('ticker') or '').strip().upper()
        normalized_exchange = str(spot_exchange or '').strip().lower()
        created_at = int(job.get('created_at', 0) or 0)
        if not ticker or not normalized_exchange:
            return entry_qty_spot, [], 'entry_qty'

        withdraw_jobs = WithdrawJobStore().find_jobs(
            ticker=ticker,
            target_exchange=normalized_exchange,
            statuses={'submitted', 'done'},
            created_after=created_at,
        )
        if not withdraw_jobs:
            return entry_qty_spot, [], 'entry_qty'

        total_withdraw_amount = 0.0
        withdraw_job_ids: list[str] = []
        for withdraw_job in withdraw_jobs:
            amount = _safe_float(withdraw_job.get('amount'))
            if amount is None or amount <= _QTY_EPSILON:
                continue
            total_withdraw_amount += amount
            job_id = str(withdraw_job.get('job_id') or '').strip()
            if job_id:
                withdraw_job_ids.append(job_id)

        if total_withdraw_amount <= _QTY_EPSILON:
            return entry_qty_spot, [], 'entry_qty'

        return min(total_withdraw_amount, entry_qty_spot), withdraw_job_ids, 'withdraw_job'

    async def refresh_latest_job(
        self,
        ticker: str,
        exit_spot_exchange: str | None = None,
        exit_futures_exchange: str | None = None,
    ) -> dict[str, Any]:
        normalized_ticker = str(ticker or '').strip().upper()
        if not normalized_ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}

        async with self._lock:
            job = self._jobs.latest_open_job(ticker=normalized_ticker)
            if not job:
                return {'ok': True, 'job': None}

            refreshed = await self._refresh_job_locked(
                job=job,
                exit_spot_exchange=exit_spot_exchange,
                exit_futures_exchange=exit_futures_exchange,
            )
            refreshed_status = str(refreshed.get('status') or '').strip().lower()
            if refreshed_status == 'closed':
                return {'ok': True, 'job': None}

            return {'ok': True, 'job': refreshed}

    async def enter(
        self,
        ticker: str,
        futures_exchange: str,
        nominal_usd: float | None = None,
        leverage: int | None = None,
    ) -> dict[str, Any]:
        ticker = str(ticker or '').strip().upper()
        futures_exchange = str(futures_exchange or '').strip().lower()
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}
        if futures_exchange not in config.EXCHANGES_WITH_FUTURES:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': 'futures_exchange is invalid or futures unsupported',
            }

        nominal = _safe_float(nominal_usd)
        if nominal is None or nominal <= 0:
            nominal = float(getattr(config, 'HEDGE_NOMINAL_USD', 500.0))

        lev = int(leverage or getattr(config, 'HEDGE_LEVERAGE', 4))
        if lev <= 0:
            lev = 1

        async with self._lock:
            existing_job = self._jobs.latest_open_job(ticker=ticker)
            if existing_job:
                current_exchange = str(existing_job.get('futures_exchange') or '').strip().lower()
                if current_exchange and current_exchange != futures_exchange:
                    return {
                        'ok': False,
                        'code': 'ACTIVE_HEDGE_EXISTS',
                        'message': (
                            f'{ticker} active hedge already uses {current_exchange}; '
                            f'close it before opening {futures_exchange}'
                        ),
                        'job': existing_job,
                    }
            return await self._enter_locked(
                ticker=ticker,
                futures_exchange=futures_exchange,
                nominal_usd=nominal,
                leverage=lev,
                existing_job=existing_job,
            )

    async def _enter_locked(
        self,
        ticker: str,
        futures_exchange: str,
        nominal_usd: float,
        leverage: int,
        existing_job: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        futures_instance = exchange_manager.get_instance(futures_exchange, 'swap')
        if futures_instance is None:
            return {
                'ok': False,
                'code': 'EXCHANGE_INSTANCE_UNAVAILABLE',
                'message': f'{futures_exchange} swap instance unavailable',
            }

        if not futures_instance.markets:
            await futures_instance.load_markets()

        symbol_spot = f'{ticker}/KRW'
        symbol_futures = exchange_manager.get_symbol(
            ticker=ticker,
            market_type='swap',
            exchange_id=futures_exchange,
        )

        spot_bbo, futures_bbo, usdt_krw = await asyncio.gather(
            fetch_bithumb_bbo(ticker),
            exchange_manager.fetch_bbo(futures_instance, symbol_futures),
            fetch_usdt_krw(),
        )
        if spot_bbo is None or spot_bbo.ask is None or spot_bbo.ask <= 0:
            return {
                'ok': False,
                'code': 'PRICE_UNAVAILABLE',
                'message': f'bithumb ask unavailable for {ticker}',
            }
        futures_reference = (
            futures_bbo.bid if futures_bbo and futures_bbo.bid is not None else None
        )
        if futures_reference is None or futures_reference <= 0:
            return {
                'ok': False,
                'code': 'PRICE_UNAVAILABLE',
                'message': f'{futures_exchange} futures bid unavailable for {ticker}',
            }
        if usdt_krw is None or usdt_krw <= 0:
            return {
                'ok': False,
                'code': 'PRICE_UNAVAILABLE',
                'message': 'bithumb USDT/KRW unavailable',
            }

        raw_qty = nominal_usd / futures_reference
        futures_qty = self._normalize_amount(futures_instance, symbol_futures, raw_qty)
        spot_qty = self._normalize_amount(None, symbol_spot, futures_qty)
        if futures_qty <= _QTY_EPSILON or spot_qty <= _QTY_EPSILON:
            return {
                'ok': False,
                'code': 'QTY_TOO_SMALL',
                'message': 'normalized order quantity is too small',
            }

        warnings = await self._prepare_futures_account(
            exchange_instance=futures_instance,
            symbol=symbol_futures,
            leverage=leverage,
        )
        mode = 'scale_in' if existing_job else 'open'
        entry_timestamp = int(time.time())
        if existing_job:
            job = existing_job
        else:
            job = self._jobs.create_job(
                {
                    'ticker': ticker,
                    'status': 'requested',
                    'futures_exchange': futures_exchange,
                    'nominal_usd': nominal_usd,
                    'leverage': leverage,
                    'requested_qty': spot_qty,
                    'entry_usdt_krw': usdt_krw,
                    'price_reference': {
                        'bithumb_ask_krw': spot_bbo.ask,
                        'futures_bid_usdt': futures_reference,
                    },
                    'warnings': warnings,
                    'events': [],
                }
            )
            entry_timestamp = int(job.get('created_at') or entry_timestamp)

        attempt = await self._execute_entry_attempt(
            futures_instance=futures_instance,
            futures_exchange=futures_exchange,
            symbol_spot=symbol_spot,
            symbol_futures=symbol_futures,
            spot_qty=spot_qty,
            futures_qty=futures_qty,
            spot_reference_price=spot_bbo.ask,
            futures_reference=futures_reference,
            usdt_krw=usdt_krw,
        )

        if existing_job is None:
            return self._finalize_new_entry_job(
                job=job,
                attempt=attempt,
                nominal_usd=nominal_usd,
                requested_qty=spot_qty,
                futures_exchange=futures_exchange,
                futures_reference=futures_reference,
                warnings=warnings,
                leverage=leverage,
                price_reference={
                    'bithumb_ask_krw': spot_bbo.ask,
                    'futures_bid_usdt': futures_reference,
                },
                entry_timestamp=entry_timestamp,
            )

        return self._finalize_scale_in_job(
            job=job,
            attempt=attempt,
            nominal_usd=nominal_usd,
            requested_qty=spot_qty,
            futures_exchange=futures_exchange,
            futures_reference=futures_reference,
            warnings=warnings,
            leverage=leverage,
            entry_timestamp=entry_timestamp,
        )

    async def _execute_entry_attempt(
        self,
        futures_instance: ccxt.Exchange,
        futures_exchange: str,
        symbol_spot: str,
        symbol_futures: str,
        spot_qty: float,
        futures_qty: float,
        spot_reference_price: float,
        futures_reference: float,
        usdt_krw: float,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []

        spot_task = asyncio.create_task(
            self._submit_market_order(
                exchange_instance=None,
                exchange_name='bithumb',
                symbol=symbol_spot,
                side='buy',
                amount=spot_qty,
                market='spot',
                reference_price=spot_reference_price,
            )
        )
        futures_task = asyncio.create_task(
            self._submit_market_order(
                exchange_instance=futures_instance,
                exchange_name=futures_exchange,
                symbol=symbol_futures,
                side='sell',
                amount=futures_qty,
                market='futures',
            )
        )
        spot_leg, futures_leg = await asyncio.gather(spot_task, futures_task)

        spot_legs = [spot_leg]
        futures_legs = [futures_leg]

        spot_filled = float(spot_leg.get('filled_qty', 0) or 0)
        futures_filled = float(futures_leg.get('filled_qty', 0) or 0)

        if spot_filled <= _QTY_EPSILON and futures_filled <= _QTY_EPSILON:
            return {
                'ok': False,
                'code': 'ENTRY_FAILED',
                'status': 'failed',
                'message': 'both orders were not filled',
                'spot_legs': spot_legs,
                'futures_legs': futures_legs,
                'events': events,
                'snapshot': None,
            }

        if spot_filled > _QTY_EPSILON and futures_filled <= _QTY_EPSILON:
            rollback_qty = self._normalize_amount(None, symbol_spot, spot_filled)
            if rollback_qty > _QTY_EPSILON:
                rollback_leg = await self._submit_market_order(
                    exchange_instance=None,
                    exchange_name='bithumb',
                    symbol=symbol_spot,
                    side='sell',
                    amount=rollback_qty,
                    market='spot',
                )
                spot_legs.append(rollback_leg)
                rollback_filled = float(rollback_leg.get('filled_qty', 0) or 0)
                rolled_back = rollback_filled + _QTY_EPSILON >= spot_filled
                status = 'rolled_back' if rolled_back else 'rollback_failed'
                message = (
                    'futures leg was not filled; spot rollback executed'
                    if rolled_back
                    else 'futures leg was not filled; spot rollback attempted'
                )
            else:
                status = 'rollback_failed'
                message = 'futures leg was not filled; spot rollback qty invalid'

            return {
                'ok': False,
                'code': 'ONE_SIDED_FILL',
                'status': status,
                'message': message,
                'spot_legs': spot_legs,
                'futures_legs': futures_legs,
                'events': events,
                'snapshot': self._build_position_snapshot_from_legs(
                    spot_legs=spot_legs,
                    futures_legs=futures_legs,
                    futures_reference=futures_reference,
                    usdt_krw=usdt_krw,
                ),
            }

        if futures_filled > _QTY_EPSILON and spot_filled <= _QTY_EPSILON:
            rollback_qty = self._normalize_amount(
                exchange_instance=futures_instance,
                symbol=symbol_futures,
                amount=futures_filled,
            )
            if rollback_qty > _QTY_EPSILON:
                rollback_leg = await self._submit_market_order(
                    exchange_instance=futures_instance,
                    exchange_name=futures_exchange,
                    symbol=symbol_futures,
                    side='buy',
                    amount=rollback_qty,
                    market='futures',
                )
                futures_legs.append(rollback_leg)
                rollback_filled = float(rollback_leg.get('filled_qty', 0) or 0)
                rolled_back = rollback_filled + _QTY_EPSILON >= futures_filled
                status = 'rolled_back' if rolled_back else 'rollback_failed'
                message = (
                    'spot leg was not filled; futures rollback executed'
                    if rolled_back
                    else 'spot leg was not filled; futures rollback attempted'
                )
            else:
                status = 'rollback_failed'
                message = 'spot leg was not filled; futures rollback qty invalid'

            return {
                'ok': False,
                'code': 'ONE_SIDED_FILL',
                'status': status,
                'message': message,
                'spot_legs': spot_legs,
                'futures_legs': futures_legs,
                'events': events,
                'snapshot': self._build_position_snapshot_from_legs(
                    spot_legs=spot_legs,
                    futures_legs=futures_legs,
                    futures_reference=futures_reference,
                    usdt_krw=usdt_krw,
                ),
            }

        qty_diff = spot_filled - futures_filled
        if abs(qty_diff) > _QTY_EPSILON:
            adjust_side = 'sell' if qty_diff > 0 else 'buy'
            raw_adjust_qty = abs(qty_diff)
            adjust_qty = self._normalize_amount(
                exchange_instance=futures_instance,
                symbol=symbol_futures,
                amount=raw_adjust_qty,
            )
            if adjust_qty > _QTY_EPSILON:
                adjust_leg = await self._submit_market_order(
                    exchange_instance=futures_instance,
                    exchange_name=futures_exchange,
                    symbol=symbol_futures,
                    side=adjust_side,
                    amount=adjust_qty,
                    market='futures',
                )
                futures_legs.append(adjust_leg)
                events.append(
                    {
                        'type': 'futures_adjustment',
                        'requested_qty': adjust_qty,
                        'side': adjust_side,
                        'filled_qty': adjust_leg.get('filled_qty'),
                        'avg_price': adjust_leg.get('avg_price'),
                    }
                )

        snapshot = self._build_position_snapshot_from_legs(
            spot_legs=spot_legs,
            futures_legs=futures_legs,
            futures_reference=futures_reference,
            usdt_krw=usdt_krw,
        )
        status = str(snapshot.get('status') or 'partial_hedged')
        return {
            'ok': status in {'hedged', 'partial_hedged'},
            'code': None,
            'status': status,
            'message': 'hedge entry processed',
            'spot_legs': spot_legs,
            'futures_legs': futures_legs,
            'events': events,
            'snapshot': snapshot,
        }

    def _build_position_snapshot_from_legs(
        self,
        spot_legs: list[dict[str, Any]],
        futures_legs: list[dict[str, Any]],
        futures_reference: float,
        usdt_krw: float | None,
    ) -> dict[str, Any]:
        spot_net_qty, spot_avg_price = self._compute_net_spot(spot_legs)
        futures_net_qty, futures_avg_price = self._compute_net_futures_short(futures_legs)
        status_eval = classify_hedge_status(
            spot_qty=spot_net_qty,
            futures_qty=futures_net_qty,
            futures_price_usdt=futures_avg_price or futures_reference,
        )
        return {
            'status': str(status_eval.get('status') or 'partial_hedged'),
            'entry_qty_spot': spot_net_qty,
            'entry_qty_futures': futures_net_qty,
            'residual_qty': float(status_eval.get('residual_qty') or 0.0),
            'residual_ratio': float(status_eval.get('residual_ratio') or 0.0),
            'residual_notional_usd': _safe_float(status_eval.get('residual_notional_usd')),
            'hedge_ratio_tolerance': status_eval.get('hedge_ratio_tolerance'),
            'hedge_notional_tolerance_usd': status_eval.get('hedge_notional_tolerance_usd'),
            'entry_avg_spot_krw': spot_avg_price,
            'entry_avg_futures_usdt': futures_avg_price,
            'entry_usdt_krw': usdt_krw,
        }

    @staticmethod
    def _build_entry_batch(
        snapshot: dict[str, Any] | None,
        nominal_usd: float,
        requested_qty: float,
        opened_at: int,
    ) -> dict[str, Any] | None:
        if not isinstance(snapshot, dict):
            return None
        spot_qty = _safe_float(snapshot.get('entry_qty_spot'))
        futures_qty = _safe_float(snapshot.get('entry_qty_futures'))
        spot_avg_krw = _safe_float(snapshot.get('entry_avg_spot_krw'))
        futures_avg_usdt = _safe_float(snapshot.get('entry_avg_futures_usdt'))
        entry_usdt_krw = _safe_float(snapshot.get('entry_usdt_krw'))
        if (
            spot_qty is None
            or spot_qty <= _QTY_EPSILON
            or futures_qty is None
            or futures_qty <= _QTY_EPSILON
            or spot_avg_krw is None
            or spot_avg_krw <= 0
            or futures_avg_usdt is None
            or futures_avg_usdt <= 0
            or entry_usdt_krw is None
            or entry_usdt_krw <= 0
        ):
            return None
        return {
            'opened_at': opened_at,
            'nominal_usd': nominal_usd,
            'requested_qty': requested_qty,
            'entry_qty_spot': spot_qty,
            'entry_qty_futures': futures_qty,
            'entry_avg_spot_krw': spot_avg_krw,
            'entry_avg_futures_usdt': futures_avg_usdt,
            'entry_usdt_krw': entry_usdt_krw,
        }

    @staticmethod
    def _load_entry_batches(job: dict[str, Any]) -> list[dict[str, Any]]:
        raw_batches = job.get('entry_batches')
        if isinstance(raw_batches, list):
            return [dict(item) for item in raw_batches if isinstance(item, dict)]

        synthetic = HedgeTradeService._build_entry_batch(
            snapshot={
                'entry_qty_spot': job.get('entry_qty_spot'),
                'entry_qty_futures': job.get('entry_qty_futures'),
                'entry_avg_spot_krw': job.get('entry_avg_spot_krw'),
                'entry_avg_futures_usdt': job.get('entry_avg_futures_usdt'),
                'entry_usdt_krw': job.get('entry_usdt_krw'),
            },
            nominal_usd=_safe_float(job.get('nominal_usd')) or 0.0,
            requested_qty=_safe_float(job.get('requested_qty')) or 0.0,
            opened_at=int(job.get('created_at') or 0),
        )
        return [synthetic] if synthetic else []

    @staticmethod
    def _build_aggregate_entry_pricing(
        entry_batches: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        nominal_total = 0.0
        requested_qty_total = 0.0
        total_spot_qty = 0.0
        total_spot_cost_usdt = 0.0

        for batch in entry_batches:
            nominal_total += _safe_float(batch.get('nominal_usd')) or 0.0
            requested_qty_total += _safe_float(batch.get('requested_qty')) or 0.0

            spot_qty = _safe_float(batch.get('entry_qty_spot'))
            spot_avg_krw = _safe_float(batch.get('entry_avg_spot_krw'))
            entry_usdt_krw = _safe_float(batch.get('entry_usdt_krw'))
            if (
                spot_qty is None
                or spot_qty <= _QTY_EPSILON
                or spot_avg_krw is None
                or spot_avg_krw <= 0
                or entry_usdt_krw is None
                or entry_usdt_krw <= 0
            ):
                continue
            total_spot_qty += spot_qty
            total_spot_cost_usdt += spot_qty * (spot_avg_krw / entry_usdt_krw)

        entry_avg_spot_krw = _safe_float(snapshot.get('entry_avg_spot_krw'))
        entry_avg_futures_usdt = _safe_float(snapshot.get('entry_avg_futures_usdt'))

        entry_usdt_krw = None
        spot_avg_usdt = None
        if (
            total_spot_qty > _QTY_EPSILON
            and total_spot_cost_usdt > _QTY_EPSILON
            and entry_avg_spot_krw is not None
            and entry_avg_spot_krw > 0
        ):
            spot_avg_usdt = total_spot_cost_usdt / total_spot_qty
            entry_usdt_krw = entry_avg_spot_krw / spot_avg_usdt

        entry_spread = None
        entry_gap = None
        if (
            spot_avg_usdt is not None
            and spot_avg_usdt > 0
            and entry_avg_futures_usdt is not None
            and entry_avg_futures_usdt > 0
        ):
            entry_spread = entry_avg_futures_usdt - spot_avg_usdt
            entry_gap = spot_avg_usdt / entry_avg_futures_usdt * 10_000

        return {
            'nominal_usd': nominal_total,
            'requested_qty': requested_qty_total,
            'entry_usdt_krw': entry_usdt_krw,
            'entry_spread_usdt': entry_spread,
            'entry_gap': entry_gap,
        }

    @staticmethod
    def _compute_entry_gap_and_spread(
        entry_avg_spot_krw: float | None,
        entry_avg_futures_usdt: float | None,
        entry_usdt_krw: float | None,
    ) -> tuple[float | None, float | None]:
        if (
            entry_avg_spot_krw is None
            or entry_avg_spot_krw <= 0
            or entry_avg_futures_usdt is None
            or entry_avg_futures_usdt <= 0
            or entry_usdt_krw is None
            or entry_usdt_krw <= 0
        ):
            return None, None

        spot_avg_usdt = entry_avg_spot_krw / entry_usdt_krw
        return (
            spot_avg_usdt / entry_avg_futures_usdt * 10_000,
            entry_avg_futures_usdt - spot_avg_usdt,
        )

    def _build_aggregate_entry_updates(
        self,
        entry_batches: list[dict[str, Any]],
        snapshot: dict[str, Any] | None,
        fallback_nominal_usd: float,
        fallback_requested_qty: float,
    ) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            return {
                'nominal_usd': fallback_nominal_usd,
                'requested_qty': fallback_requested_qty,
                'entry_qty_spot': 0.0,
                'entry_qty_futures': 0.0,
                'residual_qty': 0.0,
                'residual_ratio': 0.0,
                'residual_notional_usd': None,
                'hedge_ratio_tolerance': None,
                'hedge_notional_tolerance_usd': None,
                'entry_avg_spot_krw': None,
                'entry_avg_futures_usdt': None,
                'entry_usdt_krw': None,
                'entry_gap': None,
                'entry_spread_usdt': None,
            }

        entry_qty_spot = max(_safe_float(snapshot.get('entry_qty_spot')) or 0.0, 0.0)
        entry_qty_futures = max(_safe_float(snapshot.get('entry_qty_futures')) or 0.0, 0.0)
        entry_avg_spot_krw = _safe_float(snapshot.get('entry_avg_spot_krw'))
        entry_avg_futures_usdt = _safe_float(snapshot.get('entry_avg_futures_usdt'))

        aggregate_pricing = self._build_aggregate_entry_pricing(entry_batches, snapshot)
        entry_usdt_krw = _safe_float(aggregate_pricing.get('entry_usdt_krw'))
        if entry_usdt_krw is None or entry_usdt_krw <= 0:
            entry_usdt_krw = _safe_float(snapshot.get('entry_usdt_krw'))

        entry_gap = _safe_float(aggregate_pricing.get('entry_gap'))
        entry_spread = _safe_float(aggregate_pricing.get('entry_spread_usdt'))
        if entry_gap is None or entry_spread is None:
            entry_gap, entry_spread = self._compute_entry_gap_and_spread(
                entry_avg_spot_krw=entry_avg_spot_krw,
                entry_avg_futures_usdt=entry_avg_futures_usdt,
                entry_usdt_krw=entry_usdt_krw,
            )

        nominal_total = _safe_float(aggregate_pricing.get('nominal_usd'))
        requested_qty_total = _safe_float(aggregate_pricing.get('requested_qty'))

        return {
            'nominal_usd': (
                nominal_total if nominal_total is not None and nominal_total > 0 else fallback_nominal_usd
            ),
            'requested_qty': (
                requested_qty_total
                if requested_qty_total is not None and requested_qty_total > 0
                else fallback_requested_qty
            ),
            'entry_qty_spot': entry_qty_spot,
            'entry_qty_futures': entry_qty_futures,
            'residual_qty': max(_safe_float(snapshot.get('residual_qty')) or 0.0, 0.0),
            'residual_ratio': max(_safe_float(snapshot.get('residual_ratio')) or 0.0, 0.0),
            'residual_notional_usd': _safe_float(snapshot.get('residual_notional_usd')),
            'hedge_ratio_tolerance': _safe_float(snapshot.get('hedge_ratio_tolerance')),
            'hedge_notional_tolerance_usd': _safe_float(
                snapshot.get('hedge_notional_tolerance_usd')
            ),
            'entry_avg_spot_krw': entry_avg_spot_krw,
            'entry_avg_futures_usdt': entry_avg_futures_usdt,
            'entry_usdt_krw': entry_usdt_krw,
            'entry_gap': entry_gap,
            'entry_spread_usdt': entry_spread,
        }

    @staticmethod
    def _build_entry_event(
        mode: str,
        entry_timestamp: int,
        nominal_usd: float,
        requested_qty: float,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = attempt.get('snapshot')
        if not isinstance(snapshot, dict):
            snapshot = {}
        return {
            'type': 'scale_in' if mode == 'scale_in' else 'entry',
            'timestamp': entry_timestamp,
            'nominal_usd': nominal_usd,
            'requested_qty': requested_qty,
            'status': str(attempt.get('status') or '').strip().lower() or None,
            'message': str(attempt.get('message') or '').strip() or None,
            'filled_qty_spot': _safe_float(snapshot.get('entry_qty_spot')) or 0.0,
            'filled_qty_futures': _safe_float(snapshot.get('entry_qty_futures')) or 0.0,
            'entry_avg_spot_krw': _safe_float(snapshot.get('entry_avg_spot_krw')),
            'entry_avg_futures_usdt': _safe_float(snapshot.get('entry_avg_futures_usdt')),
        }

    @staticmethod
    def _merge_unique_strings(*groups: Any) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                text = str(item or '').strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                merged.append(text)
        return merged

    @staticmethod
    def _base_close_reset_updates() -> dict[str, Any]:
        return {
            'close_qty_spot': 0.0,
            'close_qty_futures': 0.0,
            'closed_qty': 0.0,
            'closed_at': None,
            'finalized_at': None,
            'close_avg_spot_price': None,
            'close_avg_spot_quote': None,
            'close_avg_spot_usdt': None,
            'close_avg_futures_usdt': None,
            'close_usdt_krw': None,
            'close_gap': None,
            'close_spread_usdt': None,
            'exit_usdt_krw': None,
            'exit_avg_spot_krw': None,
            'exit_avg_spot_usdt': None,
            'exit_avg_futures_usdt': None,
            'exit_gap': None,
            'exit_spread_usdt': None,
            'final_pnl_usdt': None,
            'final_pnl_krw': None,
        }

    def _finalize_new_entry_job(
        self,
        job: dict[str, Any],
        attempt: dict[str, Any],
        nominal_usd: float,
        requested_qty: float,
        futures_exchange: str,
        futures_reference: float,
        warnings: list[str],
        leverage: int,
        price_reference: dict[str, Any],
        entry_timestamp: int,
    ) -> dict[str, Any]:
        job_id = str(job.get('job_id') or '').strip()
        if not job_id:
            return {
                'ok': False,
                'code': 'JOB_NOT_FOUND',
                'message': 'job not found',
            }

        attempt_ok = bool(attempt.get('ok'))
        snapshot = attempt.get('snapshot') if attempt_ok else None
        entry_batch = self._build_entry_batch(
            snapshot=snapshot,
            nominal_usd=nominal_usd,
            requested_qty=requested_qty,
            opened_at=entry_timestamp,
        )
        entry_batches = [entry_batch] if entry_batch else []
        entry_updates = self._build_aggregate_entry_updates(
            entry_batches=entry_batches,
            snapshot=snapshot if isinstance(snapshot, dict) else None,
            fallback_nominal_usd=nominal_usd,
            fallback_requested_qty=requested_qty,
        )

        status = str(attempt.get('status') or 'failed').strip().lower() or 'failed'
        if attempt_ok and isinstance(snapshot, dict):
            status = str(snapshot.get('status') or status).strip().lower() or status

        events = [self._build_entry_event('open', entry_timestamp, nominal_usd, requested_qty, attempt)]
        extra_events = attempt.get('events')
        if isinstance(extra_events, list):
            events.extend(extra_events)

        updates: dict[str, Any] = {
            'status': status,
            'message': str(attempt.get('message') or '').strip() or None,
            'futures_exchange': futures_exchange,
            'nominal_usd': nominal_usd,
            'leverage': leverage,
            'requested_qty': requested_qty,
            'entry_usdt_krw': job.get('entry_usdt_krw'),
            'price_reference': price_reference,
            'warnings': warnings,
            'events': events,
            'legs': {
                'spot': list(attempt.get('spot_legs') or []),
                'futures': list(attempt.get('futures_legs') or []),
            },
            'entry_batches': entry_batches,
            'exit_spot_exchange': futures_exchange,
            'exit_futures_exchange': futures_exchange,
            **self._base_close_reset_updates(),
        }

        if attempt_ok:
            updates.update(entry_updates)
        else:
            updates.update(
                {
                    'entry_qty_spot': 0.0,
                    'entry_qty_futures': 0.0,
                    'residual_qty': 0.0,
                    'residual_ratio': 0.0,
                    'residual_notional_usd': None,
                    'hedge_ratio_tolerance': None,
                    'hedge_notional_tolerance_usd': None,
                    'entry_avg_spot_krw': None,
                    'entry_avg_futures_usdt': None,
                    'entry_gap': None,
                    'entry_spread_usdt': None,
                }
            )

        final = self._jobs.update_job(job_id, updates)
        return {
            'ok': attempt_ok,
            'code': attempt.get('code'),
            'message': str(attempt.get('message') or '').strip() or None,
            'status': final.get('status') if isinstance(final, dict) else status,
            'job': final or job,
        }

    def _finalize_scale_in_job(
        self,
        job: dict[str, Any],
        attempt: dict[str, Any],
        nominal_usd: float,
        requested_qty: float,
        futures_exchange: str,
        futures_reference: float,
        warnings: list[str],
        leverage: int,
        entry_timestamp: int,
    ) -> dict[str, Any]:
        job_id = str(job.get('job_id') or '').strip()
        if not job_id:
            return {
                'ok': False,
                'code': 'JOB_NOT_FOUND',
                'message': 'job not found',
            }

        existing_spot_legs = list((job.get('legs') or {}).get('spot') or [])
        existing_futures_legs = list((job.get('legs') or {}).get('futures') or [])
        attempt_spot_legs = list(attempt.get('spot_legs') or [])
        attempt_futures_legs = list(attempt.get('futures_legs') or [])

        merged_spot_legs = [*existing_spot_legs, *attempt_spot_legs]
        merged_futures_legs = [*existing_futures_legs, *attempt_futures_legs]

        merged_snapshot = self._build_position_snapshot_from_legs(
            spot_legs=merged_spot_legs,
            futures_legs=merged_futures_legs,
            futures_reference=(
                _safe_float(job.get('entry_avg_futures_usdt'))
                or futures_reference
            ),
            usdt_krw=_safe_float(job.get('entry_usdt_krw')),
        )

        entry_batches = self._load_entry_batches(job)
        entry_batch = self._build_entry_batch(
            snapshot=attempt.get('snapshot'),
            nominal_usd=nominal_usd,
            requested_qty=requested_qty,
            opened_at=entry_timestamp,
        )
        batch_applied = entry_batch is not None
        if entry_batch is not None:
            entry_batches.append(entry_batch)

        entry_updates = self._build_aggregate_entry_updates(
            entry_batches=entry_batches,
            snapshot=merged_snapshot,
            fallback_nominal_usd=_safe_float(job.get('nominal_usd')) or 0.0,
            fallback_requested_qty=_safe_float(job.get('requested_qty')) or 0.0,
        )

        existing_events = list(job.get('events') or [])
        extra_events = list(attempt.get('events') or [])
        events = [
            *existing_events,
            self._build_entry_event('scale_in', entry_timestamp, nominal_usd, requested_qty, attempt),
            *extra_events,
        ]

        existing_warnings = list(job.get('warnings') or [])
        merged_warnings = self._merge_unique_strings(existing_warnings, warnings)

        attempt_ok = bool(attempt.get('ok'))
        attempt_message = str(attempt.get('message') or '').strip()
        if batch_applied:
            message = 'hedge position scaled in'
            if attempt_message and not attempt_ok:
                message = f'scale-in applied with issue: {attempt_message}'
        else:
            message = 'scale-in rolled back; existing hedge unchanged'
            if attempt_message and str(attempt.get('status') or '').strip().lower() == 'failed':
                message = attempt_message

        updated = self._jobs.update_job(
            job_id,
            {
                'status': str(merged_snapshot.get('status') or job.get('status') or 'partial_hedged'),
                'message': message,
                'futures_exchange': futures_exchange,
                'leverage': leverage,
                'warnings': merged_warnings,
                'events': events,
                'legs': {
                    'spot': merged_spot_legs,
                    'futures': merged_futures_legs,
                },
                'entry_batches': entry_batches,
                'exit_spot_exchange': (
                    str(job.get('exit_spot_exchange') or '').strip().lower() or futures_exchange
                ),
                'exit_futures_exchange': futures_exchange,
                **entry_updates,
                **self._base_close_reset_updates(),
            },
        )
        return {
            'ok': attempt_ok and batch_applied,
            'code': attempt.get('code'),
            'message': message,
            'status': updated.get('status') if isinstance(updated, dict) else merged_snapshot.get('status'),
            'job': updated or job,
        }

    async def _refresh_job_locked(
        self,
        job: dict[str, Any],
        exit_spot_exchange: str | None = None,
        exit_futures_exchange: str | None = None,
    ) -> dict[str, Any]:
        job_id = str(job.get('job_id') or '').strip()
        ticker = str(job.get('ticker') or '').strip().upper()
        if not job_id or not ticker:
            return job

        entry_qty_spot = max(_safe_float(job.get('entry_qty_spot')) or 0.0, 0.0)
        entry_qty_futures = max(_safe_float(job.get('entry_qty_futures')) or 0.0, 0.0)
        if entry_qty_spot <= _QTY_EPSILON or entry_qty_futures <= _QTY_EPSILON:
            return job

        spot_exchange = self._resolve_exit_spot_exchange(
            requested=exit_spot_exchange,
            job=job,
        )
        futures_exchange = self._resolve_exit_futures_exchange(
            requested=exit_futures_exchange,
            job=job,
        )
        expected_exit_spot_qty, linked_withdraw_job_ids, expected_exit_spot_source = (
            self._resolve_expected_exit_spot_qty(job, spot_exchange)
        )

        spot_instance = exchange_manager.get_instance(spot_exchange, 'spot')
        futures_instance = exchange_manager.get_instance(futures_exchange, 'swap')

        symbol_spot = exchange_manager.get_symbol(
            ticker=ticker,
            market_type='spot',
            exchange_id=spot_exchange,
        )
        symbol_futures = exchange_manager.get_symbol(
            ticker=ticker,
            market_type='swap',
            exchange_id=futures_exchange,
        )
        since_ms = max(int(job.get('created_at', 0) or 0), 0) * 1000
        usdt_krw = await fetch_usdt_krw()

        spot_summary, futures_summary = await asyncio.gather(
            self._fetch_close_trade_summary(
                exchange_instance=spot_instance,
                symbol=symbol_spot,
                market='spot',
                expected_side='sell',
                since_ms=since_ms,
                max_qty=expected_exit_spot_qty,
                quote_exchange=spot_exchange,
                usdt_krw=usdt_krw,
            ),
            self._fetch_close_trade_summary(
                exchange_instance=futures_instance,
                symbol=symbol_futures,
                market='futures',
                expected_side='buy',
                since_ms=since_ms,
                max_qty=entry_qty_futures,
                quote_exchange=futures_exchange,
                usdt_krw=usdt_krw,
            ),
        )

        close_qty_spot = float(spot_summary.get('qty') or 0.0)
        close_qty_futures = float(futures_summary.get('qty') or 0.0)
        close_avg_spot_price = _safe_float(spot_summary.get('avg_price'))
        close_avg_spot_usdt = _safe_float(spot_summary.get('avg_price_usdt'))
        close_avg_futures_usdt = _safe_float(futures_summary.get('avg_price_usdt'))
        close_spot_quote = str(spot_summary.get('quote') or '').strip().upper() or None

        exit_avg_spot_krw = None
        exit_avg_spot_usdt = None
        close_gap = None
        close_spread_usdt = None
        matched_close_qty = min(
            expected_exit_spot_qty,
            entry_qty_futures,
            close_qty_spot,
            close_qty_futures,
        )
        if close_avg_spot_price is not None and close_avg_spot_usdt is not None:
            if close_spot_quote == 'KRW':
                exit_avg_spot_krw = close_avg_spot_price
                exit_avg_spot_usdt = close_avg_spot_usdt
            else:
                exit_avg_spot_usdt = close_avg_spot_usdt
                if usdt_krw is not None and usdt_krw > 0:
                    exit_avg_spot_krw = close_avg_spot_usdt * usdt_krw
        if (
            exit_avg_spot_usdt is not None
            and exit_avg_spot_usdt > 0
            and close_avg_futures_usdt is not None
            and close_avg_futures_usdt > 0
        ):
            close_gap = exit_avg_spot_usdt / close_avg_futures_usdt * 10_000
            close_spread_usdt = exit_avg_spot_usdt - close_avg_futures_usdt

        final_pnl_usdt = None
        final_pnl_krw = None
        entry_spread_usdt = _safe_float(job.get('entry_spread_usdt'))
        if (
            entry_spread_usdt is not None
            and close_spread_usdt is not None
            and matched_close_qty > _QTY_EPSILON
        ):
            final_pnl_usdt = (entry_spread_usdt + close_spread_usdt) * matched_close_qty
            if usdt_krw is not None and usdt_krw > 0:
                final_pnl_krw = final_pnl_usdt * usdt_krw

        entry_avg_spot_krw = _safe_float(job.get('entry_avg_spot_krw'))
        entry_usdt_krw = _safe_float(job.get('entry_usdt_krw'))
        entry_avg_spot_usdt = None
        if (
            entry_avg_spot_krw is not None
            and entry_avg_spot_krw > 0
            and entry_usdt_krw is not None
            and entry_usdt_krw > 0
        ):
            entry_avg_spot_usdt = entry_avg_spot_krw / entry_usdt_krw

        spot_close_reference_usdt = (
            exit_avg_spot_usdt
            if exit_avg_spot_usdt is not None and exit_avg_spot_usdt > 0
            else entry_avg_spot_usdt
        )
        futures_close_reference_usdt = (
            close_avg_futures_usdt
            if close_avg_futures_usdt is not None and close_avg_futures_usdt > 0
            else _safe_float(job.get('entry_avg_futures_usdt'))
        )

        status = self._build_active_status(job)
        if (
            self._is_close_complete(
                expected_exit_spot_qty,
                close_qty_spot,
                reference_price_usdt=spot_close_reference_usdt,
            )
            and self._is_close_complete(
                entry_qty_futures,
                close_qty_futures,
                reference_price_usdt=futures_close_reference_usdt,
            )
            and exit_avg_spot_usdt is not None
            and close_avg_futures_usdt is not None
        ):
            status = 'closed'

        closed_at_candidates = [
            int(spot_summary.get('last_timestamp') or 0),
            int(futures_summary.get('last_timestamp') or 0),
        ]
        detected_closed_at = (
            max(closed_at_candidates) // 1000 if max(closed_at_candidates) > 0 else None
        )
        closed_at = detected_closed_at if status == 'closed' else None
        finalized_at = closed_at if status == 'closed' else None

        updated = self._jobs.update_job(
            job_id,
            {
                'status': status,
                'exit_spot_exchange': spot_exchange,
                'exit_futures_exchange': futures_exchange,
                'expected_exit_spot_qty': expected_exit_spot_qty,
                'expected_exit_spot_source': expected_exit_spot_source,
                'linked_withdraw_job_ids': linked_withdraw_job_ids,
                'close_qty_spot': close_qty_spot,
                'close_qty_futures': close_qty_futures,
                'closed_qty': matched_close_qty if matched_close_qty > _QTY_EPSILON else 0.0,
                'closed_at': closed_at,
                'finalized_at': finalized_at,
                'close_avg_spot_price': close_avg_spot_price if status == 'closed' else None,
                'close_avg_spot_quote': close_spot_quote if status == 'closed' else None,
                'close_avg_spot_usdt': close_avg_spot_usdt if status == 'closed' else None,
                'close_avg_futures_usdt': close_avg_futures_usdt if status == 'closed' else None,
                'close_usdt_krw': usdt_krw if status == 'closed' else None,
                'close_gap': close_gap if status == 'closed' else None,
                'close_spread_usdt': close_spread_usdt if status == 'closed' else None,
                'exit_usdt_krw': usdt_krw if status == 'closed' else None,
                'exit_avg_spot_krw': exit_avg_spot_krw if status == 'closed' else None,
                'exit_avg_spot_usdt': exit_avg_spot_usdt if status == 'closed' else None,
                'exit_avg_futures_usdt': close_avg_futures_usdt if status == 'closed' else None,
                'exit_gap': close_gap if status == 'closed' else None,
                'exit_spread_usdt': close_spread_usdt if status == 'closed' else None,
                'final_pnl_usdt': final_pnl_usdt if status == 'closed' else None,
                'final_pnl_krw': final_pnl_krw if status == 'closed' else None,
            },
        )
        return updated or job

    @staticmethod
    def _resolve_exit_spot_exchange(
        requested: str | None,
        job: dict[str, Any],
    ) -> str:
        candidates = [
            str(requested or '').strip().lower(),
            str(job.get('exit_spot_exchange') or '').strip().lower(),
            str(job.get('futures_exchange') or '').strip().lower(),
        ]
        for candidate in candidates:
            if candidate in exchange_manager.ALL_EXCHANGES:
                return candidate
        return str(job.get('futures_exchange') or 'binance').strip().lower() or 'binance'

    @staticmethod
    def _resolve_exit_futures_exchange(
        requested: str | None,
        job: dict[str, Any],
    ) -> str:
        candidates = [
            str(job.get('futures_exchange') or '').strip().lower(),
            str(requested or '').strip().lower(),
            str(job.get('exit_futures_exchange') or '').strip().lower(),
        ]
        for candidate in candidates:
            if candidate in config.EXCHANGES_WITH_FUTURES:
                return candidate
        return str(job.get('futures_exchange') or 'binance').strip().lower() or 'binance'

    @staticmethod
    def _build_active_status(job: dict[str, Any]) -> str:
        entry_qty_spot = max(_safe_float(job.get('entry_qty_spot')) or 0.0, 0.0)
        entry_qty_futures = max(_safe_float(job.get('entry_qty_futures')) or 0.0, 0.0)
        if entry_qty_spot <= _QTY_EPSILON or entry_qty_futures <= _QTY_EPSILON:
            return str(job.get('status') or 'failed').strip().lower() or 'failed'

        status_eval = classify_hedge_status(
            spot_qty=entry_qty_spot,
            futures_qty=entry_qty_futures,
            futures_price_usdt=job.get('entry_avg_futures_usdt'),
        )
        return str(status_eval.get('status') or 'partial_hedged')

    @staticmethod
    def _is_close_complete(
        target_qty: float,
        closed_qty: float,
        reference_price_usdt: float | None = None,
    ) -> bool:
        target = max(float(target_qty or 0.0), 0.0)
        closed = max(float(closed_qty or 0.0), 0.0)
        if target <= _QTY_EPSILON:
            return False
        ratio_tolerance = float(getattr(config, 'HEDGE_RESIDUAL_RATIO_TOLERANCE', 0.001) or 0.001)
        residual = max(target - closed, 0.0)
        if residual <= max(target * max(ratio_tolerance, 0.0), _QTY_EPSILON):
            return True

        price = _safe_float(reference_price_usdt)
        if price is None or price <= 0:
            return False

        notional_tolerance = float(
            getattr(config, 'HEDGE_CLOSE_RESIDUAL_NOTIONAL_USD_TOLERANCE', 2.0) or 2.0
        )
        notional_tolerance = max(notional_tolerance, 0.0)
        return residual * price <= notional_tolerance

    async def _prepare_futures_account(
        self,
        exchange_instance: ccxt.Exchange,
        symbol: str,
        leverage: int,
    ) -> list[str]:
        warnings: list[str] = []

        if exchange_instance.has.get('setMarginMode'):
            try:
                await exchange_instance.set_margin_mode('isolated', symbol, {'leverage': leverage})
            except Exception as exc:
                warnings.append(f'set_margin_mode failed: {exc}')
        else:
            warnings.append('set_margin_mode not supported')

        if exchange_instance.has.get('setLeverage'):
            try:
                await exchange_instance.set_leverage(
                    leverage,
                    symbol,
                    {'marginMode': 'isolated'},
                )
            except Exception as exc:
                warnings.append(f'set_leverage failed: {exc}')
        else:
            warnings.append('set_leverage not supported')

        return warnings

    async def _submit_market_order(
        self,
        exchange_instance: ccxt.Exchange | None,
        exchange_name: str,
        symbol: str,
        side: str,
        amount: float,
        market: str,
        reference_price: float | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            'exchange': exchange_name,
            'market': market,
            'symbol': symbol,
            'side': side,
            'requested_qty': amount,
            'status': 'failed',
            'filled_qty': 0.0,
            'avg_price': None,
            'cost': None,
            'order_id': None,
            'error': None,
            'poll_error': None,
        }

        attempts = int(getattr(config, 'HEDGE_ORDER_POLL_ATTEMPTS', 4))
        delay_ms = int(getattr(config, 'HEDGE_ORDER_POLL_DELAY_MS', 250))
        delay_sec = max(delay_ms, 1) / 1000

        if market == 'spot' and exchange_name == 'bithumb':
            try:
                order = await submit_bithumb_spot_order(
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    reference_price=reference_price,
                )
                merged = self._map_bithumb_order_result(order, result)
                result.update(merged)
            except Exception as exc:
                result['error'] = str(exc)
                return result

            order_id = str(result.get('order_id') or '').strip()
            if not order_id:
                return result

            for _ in range(max(attempts, 0)):
                filled_qty = float(result.get('filled_qty', 0) or 0)
                status = str(result.get('status') or '').lower()
                if filled_qty > _QTY_EPSILON and status in {'closed', 'filled'}:
                    break
                await asyncio.sleep(delay_sec)
                latest = await fetch_bithumb_order(order_id)
                if not isinstance(latest, dict):
                    continue
                merged = self._map_bithumb_order_result(latest, result)
                result.update(merged)

            return result

        if exchange_instance is None:
            result['error'] = f'{exchange_name} exchange instance unavailable'
            return result

        params: dict[str, Any] = {}

        try:
            order = await exchange_instance.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount,
                price=None,
                params=params,
            )
            merged = self._map_order_result(order, result)
            result.update(merged)
        except Exception as exc:
            result['error'] = str(exc)
            return result

        order_id = str(result.get('order_id') or '').strip()
        if not order_id:
            return result
        if not exchange_instance.has.get('fetchOrder'):
            return result

        poll_params = self._fetch_order_params(exchange_instance)
        for _ in range(max(attempts, 0)):
            filled_qty = float(result.get('filled_qty', 0) or 0)
            status = str(result.get('status') or '').lower()
            if filled_qty > _QTY_EPSILON and status in {'closed', 'filled'}:
                break
            await asyncio.sleep(delay_sec)
            try:
                latest = await exchange_instance.fetch_order(order_id, symbol, poll_params)
                result['poll_error'] = None
            except Exception as exc:
                result['poll_error'] = str(exc)
                continue
            merged = self._map_order_result(latest, result)
            result.update(merged)

        return result

    @staticmethod
    def _fetch_order_params(exchange_instance: ccxt.Exchange) -> dict[str, Any]:
        exchange_id = str(getattr(exchange_instance, 'id', '') or '').strip().lower()
        if exchange_id == 'bybit':
            # ccxt bybit fetch_order requires this flag to skip a warning-as-exception path.
            return {'acknowledged': True}
        return {}

    async def _fetch_close_trade_summary(
        self,
        exchange_instance: ccxt.Exchange | None,
        symbol: str,
        market: str,
        expected_side: str,
        since_ms: int,
        max_qty: float,
        quote_exchange: str,
        usdt_krw: float | None,
    ) -> dict[str, Any]:
        quote = 'KRW' if market == 'spot' and quote_exchange in exchange_manager.KRW_EXCHANGES else 'USDT'
        empty = {
            'qty': 0.0,
            'avg_price': None,
            'avg_price_usdt': None,
            'quote': quote,
            'last_timestamp': None,
        }
        if exchange_instance is None or max_qty <= _QTY_EPSILON:
            return empty

        try:
            if not exchange_instance.markets:
                await exchange_instance.load_markets()
        except Exception as exc:
            logger.debug(
                'load_markets failed for %s %s %s: %s',
                getattr(exchange_instance, 'id', '?'),
                market,
                symbol,
                exc,
            )
            return empty

        if exchange_instance.has.get('fetchMyTrades'):
            trades: Any = None
            try:
                trades = await exchange_instance.fetch_my_trades(
                    symbol,
                    since=since_ms or None,
                    limit=200,
                    params=self._fetch_my_trades_params(exchange_instance, market),
                )
            except Exception as exc:
                logger.debug(
                    'fetch_my_trades failed for %s %s %s: %s',
                    getattr(exchange_instance, 'id', '?'),
                    market,
                    symbol,
                    exc,
                )

            if isinstance(trades, list):
                total_qty = 0.0
                total_cost = 0.0
                last_timestamp: int | None = None
                filtered = sorted(
                    [trade for trade in trades if isinstance(trade, dict)],
                    key=lambda trade: int(trade.get('timestamp') or 0),
                )
                for trade in filtered:
                    side = str(trade.get('side') or '').strip().lower()
                    if side != expected_side:
                        continue

                    price = _safe_float(trade.get('price'))
                    amount = _safe_float(trade.get('amount'))
                    if price is None or price <= 0 or amount is None or amount <= _QTY_EPSILON:
                        continue

                    remaining_qty = max_qty - total_qty
                    if remaining_qty <= _QTY_EPSILON:
                        break

                    applied_qty = min(amount, remaining_qty)
                    total_qty += applied_qty
                    total_cost += applied_qty * price

                    trade_ts = int(trade.get('timestamp') or 0)
                    if trade_ts > 0:
                        last_timestamp = trade_ts

                if total_qty > _QTY_EPSILON:
                    avg_price = total_cost / total_qty
                    avg_price_usdt = avg_price
                    if quote == 'KRW':
                        if usdt_krw is None or usdt_krw <= 0:
                            avg_price_usdt = None
                        else:
                            avg_price_usdt = avg_price / usdt_krw

                    return {
                        'qty': total_qty,
                        'avg_price': avg_price,
                        'avg_price_usdt': avg_price_usdt,
                        'quote': quote,
                        'last_timestamp': last_timestamp,
                    }

        if not exchange_instance.has.get('fetchClosedOrders'):
            return empty

        try:
            orders = await exchange_instance.fetch_closed_orders(
                symbol,
                since=since_ms or None,
                limit=200,
            )
        except Exception as exc:
            logger.debug(
                'fetch_closed_orders failed for %s %s %s: %s',
                getattr(exchange_instance, 'id', '?'),
                market,
                symbol,
                exc,
            )
            return empty

        if not isinstance(orders, list):
            return empty

        total_qty = 0.0
        total_cost = 0.0
        last_timestamp: int | None = None
        filtered_orders = sorted(
            [order for order in orders if isinstance(order, dict)],
            key=lambda order: int(order.get('lastTradeTimestamp') or order.get('timestamp') or 0),
        )
        for order in filtered_orders:
            side = str(order.get('side') or '').strip().lower()
            if side != expected_side:
                continue

            filled = _filled_from_order(order)
            avg_price = _avg_from_order(order, filled)
            if filled <= _QTY_EPSILON or avg_price is None or avg_price <= 0:
                continue

            remaining_qty = max_qty - total_qty
            if remaining_qty <= _QTY_EPSILON:
                break

            applied_qty = min(filled, remaining_qty)
            total_qty += applied_qty
            total_cost += applied_qty * avg_price

            order_ts = int(order.get('lastTradeTimestamp') or order.get('timestamp') or 0)
            if order_ts > 0:
                last_timestamp = order_ts

        if total_qty <= _QTY_EPSILON:
            return empty

        avg_price = total_cost / total_qty
        avg_price_usdt = avg_price
        if quote == 'KRW':
            if usdt_krw is None or usdt_krw <= 0:
                avg_price_usdt = None
            else:
                avg_price_usdt = avg_price / usdt_krw

        return {
            'qty': total_qty,
            'avg_price': avg_price,
            'avg_price_usdt': avg_price_usdt,
            'quote': quote,
            'last_timestamp': last_timestamp,
        }

    @staticmethod
    def _fetch_my_trades_params(
        exchange_instance: ccxt.Exchange,
        market: str,
    ) -> dict[str, Any]:
        exchange_id = str(getattr(exchange_instance, 'id', '') or '').strip().lower()
        if exchange_id == 'bybit':
            category = 'linear' if market == 'futures' else 'spot'
            return {'category': category}
        return {}

    @staticmethod
    def _map_order_result(order: Any, base: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(order, dict):
            return {}

        filled_qty = _filled_from_order(order)
        avg_price = _avg_from_order(order, filled_qty)
        cost = _safe_float(order.get('cost'))
        if cost is None and filled_qty > 0 and avg_price is not None:
            cost = filled_qty * avg_price

        # 화이트리스트 필드만 반환 — `side`, `exchange`, `market`, `symbol` 은
        # 호출측(요청 컨텍스트)에서 이미 세팅했으므로 order 응답값으로 덮어쓰지
        # 않는다. (Bug #3 — reduceOnly close 시 side 가 거래소 응답에 의해 반대로
        # 덮여 close_long 이 None 이 되어 job 이 closed 로 전환되지 않는 문제)
        return {
            'order_id': order.get('id'),
            'status': str(order.get('status') or 'submitted').lower(),
            'filled_qty': filled_qty,
            'avg_price': avg_price,
            'cost': cost,
        }

    @staticmethod
    def _map_bithumb_order_result(order: Any, base: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(order, dict):
            return {}

        state = str(order.get('state') or order.get('status') or '').strip().lower()
        status_map = {
            'wait': 'open',
            'pending': 'open',
            'watch': 'open',
            'done': 'closed',
            'filled': 'closed',
            'cancel': 'canceled',
            'canceled': 'canceled',
        }
        status = status_map.get(
            state,
            str(base.get('status') or 'submitted').lower(),
        )

        gross_filled_qty = _safe_float(order.get('executed_volume'))
        if gross_filled_qty is None:
            volume = _safe_float(order.get('volume'))
            remaining = _safe_float(order.get('remaining_volume'))
            if volume is not None and remaining is not None:
                gross_filled_qty = max(volume - remaining, 0.0)
        if gross_filled_qty is None:
            gross_filled_qty = 0.0

        cost: float | None = None
        avg_price: float | None = None
        trades = order.get('trades')
        if isinstance(trades, list):
            total_qty = 0.0
            total_cost = 0.0
            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                trade_qty = _safe_float(trade.get('volume'))
                if trade_qty is None or trade_qty <= _QTY_EPSILON:
                    continue
                trade_cost = _safe_float(trade.get('funds'))
                if trade_cost is None:
                    trade_price = _safe_float(trade.get('price'))
                    if trade_price is not None and trade_price > 0:
                        trade_cost = trade_qty * trade_price
                if trade_cost is None:
                    continue
                total_qty += trade_qty
                total_cost += trade_cost

            if total_qty > _QTY_EPSILON:
                gross_filled_qty = max(gross_filled_qty, total_qty)
                cost = total_cost
                avg_price = total_cost / total_qty

        side = str(order.get('side') or '').strip().lower()
        ord_type = str(order.get('ord_type') or order.get('order_type') or '').strip().lower()
        price = _safe_float(order.get('price'))
        filled_qty = gross_filled_qty

        if price is not None and price > 0:
            if ord_type == 'price' and side in {'bid', 'buy'}:
                if cost is None:
                    cost = price
                if avg_price is None and filled_qty > _QTY_EPSILON:
                    avg_price = cost / filled_qty
            else:
                if avg_price is None:
                    avg_price = price
                if cost is None and filled_qty > _QTY_EPSILON:
                    cost = price * filled_qty

        if cost is None and avg_price is not None and filled_qty > _QTY_EPSILON:
            cost = avg_price * filled_qty

        # Bug #7 fix: Bithumb 현물 BUY 는 받은 코인에서 수수료를 차감한다.
        # `executed_volume` 은 gross(체결량)이라 이대로 entry_qty_spot 에 저장하면
        # 실제 잔고(net)보다 크게 기록됨 → 청산 SELL 시 "insufficient balance".
        # `paid_fee` 필드를 이용해 net 수량으로 보정. SELL 측은 KRW 로 수수료가
        # 빠져나가므로 coin 수량은 그대로.
        paid_fee = _safe_float(order.get('paid_fee')) or 0.0
        if side in {'bid', 'buy'} and paid_fee > 0 and gross_filled_qty > _QTY_EPSILON:
            # paid_fee 단위가 coin 인지 quote(KRW) 인지 구분. avg_price 가 있고
            # paid_fee > avg_price 이면 quote 단위로 추정 → coin 단위 환산은
            # 부정확하므로 수수료율 상한(0.25%)으로 캡만 적용.
            est_fee_coin = paid_fee
            if avg_price is not None and avg_price > 0 and paid_fee > gross_filled_qty:
                # paid_fee 가 KRW 단위로 온 케이스
                est_fee_coin = paid_fee / avg_price
            # 상한 0.25% (Bithumb 일반 수수료 상한)
            fee_cap = gross_filled_qty * 0.0025
            est_fee_coin = min(est_fee_coin, fee_cap)
            filled_qty = max(gross_filled_qty - est_fee_coin, 0.0)

        # 화이트리스트 필드만 반환 — `market`, `exchange`, `side`, `symbol` 등은
        # 호출측에서 request 기준으로 세팅한 값을 유지해야 한다. Bithumb 응답이
        # `market='KRW-XYZ'` 로 오는 경우 close_job 의 디스패처가 깨진다.
        return {
            'order_id': order.get('uuid') or order.get('order_id'),
            'status': status,
            'filled_qty': filled_qty,
            'avg_price': avg_price,
            'cost': cost,
        }

    @staticmethod
    def _normalize_amount(
        exchange_instance: ccxt.Exchange | None,
        symbol: str,
        amount: float,
    ) -> float:
        if amount <= 0:
            return 0.0

        if exchange_instance is None:
            return amount

        normalized = amount
        try:
            normalized = float(exchange_instance.amount_to_precision(symbol, normalized))
        except Exception:
            normalized = amount

        if normalized <= 0:
            return 0.0

        try:
            market = exchange_instance.market(symbol)
        except Exception:
            market = {}

        limits = market.get('limits') if isinstance(market, dict) else None
        amount_limits = limits.get('amount') if isinstance(limits, dict) else None
        min_amount = (
            _safe_float(amount_limits.get('min'))
            if isinstance(amount_limits, dict)
            else None
        )
        if min_amount is not None and normalized + _QTY_EPSILON < min_amount:
            return 0.0

        return normalized

    @staticmethod
    def _compute_net_spot(legs: list[dict[str, Any]]) -> tuple[float, float | None]:
        qty = 0.0
        cost = 0.0
        for leg in legs:
            side = str(leg.get('side', '')).lower()
            filled = float(leg.get('filled_qty', 0) or 0)
            price = _safe_float(leg.get('avg_price'))
            if filled <= _QTY_EPSILON or price is None or price <= 0:
                continue

            if side == 'buy':
                qty += filled
                cost += filled * price
                continue

            if side == 'sell' and qty > _QTY_EPSILON:
                close_qty = min(qty, filled)
                avg = cost / qty if qty > _QTY_EPSILON else 0.0
                qty -= close_qty
                cost -= avg * close_qty

        if qty <= _QTY_EPSILON:
            return 0.0, None
        return qty, cost / qty

    @staticmethod
    def _compute_net_futures_short(legs: list[dict[str, Any]]) -> tuple[float, float | None]:
        short_qty = 0.0
        short_cost = 0.0
        for leg in legs:
            side = str(leg.get('side', '')).lower()
            filled = float(leg.get('filled_qty', 0) or 0)
            price = _safe_float(leg.get('avg_price'))
            if filled <= _QTY_EPSILON or price is None or price <= 0:
                continue

            if side == 'sell':
                short_qty += filled
                short_cost += filled * price
                continue

            if side == 'buy' and short_qty > _QTY_EPSILON:
                close_qty = min(short_qty, filled)
                avg = short_cost / short_qty if short_qty > _QTY_EPSILON else 0.0
                short_qty -= close_qty
                short_cost -= avg * close_qty

        if short_qty <= _QTY_EPSILON:
            return 0.0, None
        return short_qty, short_cost / short_qty

    # ------------------------------------------------------------------
    # Auto close: entry의 반대 방향 주문을 제출하여 포지션 청산
    # ------------------------------------------------------------------

    async def close_job(
        self,
        ticker: str,
        reason: str = 'manual',
    ) -> dict[str, Any]:
        """열린 hedge 포지션을 자동 청산.

        - spot leg: Bithumb SELL (entry_qty_spot)
        - futures leg: 선물 BUY with reduceOnly (entry_qty_futures)
        - 성공/부분/실패 모두 refresh_latest_job()로 상태 갱신 후 반환
        """
        ticker = str(ticker or '').strip().upper()
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}

        async with self._lock:
            job = self._jobs.latest_open_job(ticker=ticker)
            if not job:
                return {'ok': False, 'code': 'NO_OPEN_JOB', 'message': f'no open hedge for {ticker}'}
            if job.get('status') not in {'hedged', 'partial_hedged'}:
                return {'ok': False, 'code': 'NOT_CLOSEABLE', 'message': f'status={job.get("status")}'}

            job_id = job.get('job_id')
            futures_exchange = str(job.get('futures_exchange') or 'binance').lower()
            qty_spot = float(job.get('entry_qty_spot') or 0)
            qty_futures = float(job.get('entry_qty_futures') or 0)

            if qty_spot <= _QTY_EPSILON and qty_futures <= _QTY_EPSILON:
                return {'ok': False, 'code': 'ZERO_QTY', 'message': 'entry qty both zero'}

            spot_symbol = exchange_manager.get_symbol(ticker, 'spot', 'bithumb')
            futures_symbol = exchange_manager.get_symbol(ticker, 'swap', futures_exchange)

            logger.info(
                '[close_job] %s start | reason=%s job=%s qty_spot=%.6f qty_futures=%.6f',
                ticker, reason, job_id, qty_spot, qty_futures,
            )

            # Bithumb market SELL은 reference_price 불필요 (volume-only)
            ref_spot_price = None

            futures_instance = exchange_manager.get_instance(futures_exchange, 'swap') \
                if qty_futures > _QTY_EPSILON else None

            # 2 leg 병렬 제출
            tasks: list[asyncio.Task] = []
            if qty_spot > _QTY_EPSILON:
                tasks.append(asyncio.create_task(
                    self._submit_market_order(
                        exchange_instance=None,
                        exchange_name='bithumb',
                        symbol=spot_symbol,
                        side='sell',
                        amount=qty_spot,
                        market='spot',
                        reference_price=ref_spot_price,
                    ),
                    name=f'close_spot_{ticker}',
                ))
            if qty_futures > _QTY_EPSILON and futures_instance is not None:
                # hedge: 선물 SHORT 포지션 청산 → reduceOnly BUY
                tasks.append(asyncio.create_task(
                    self._submit_futures_close_generic_reduce_only(
                        futures_instance=futures_instance,
                        exchange_name=futures_exchange,
                        symbol=futures_symbol,
                        side='buy',
                        amount=qty_futures,
                    ),
                    name=f'close_futures_{ticker}',
                ))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            spot_result: dict | None = None
            futures_result: dict | None = None
            for r in results:
                if isinstance(r, Exception):
                    logger.error('[close_job] leg exception: %s', r)
                    continue
                if not isinstance(r, dict):
                    continue
                if r.get('market') == 'spot':
                    spot_result = r
                elif r.get('market') == 'futures':
                    futures_result = r

            # close leg 기록
            close_legs = {
                'spot': [spot_result] if spot_result else [],
                'futures': [futures_result] if futures_result else [],
            }
            self._jobs.update_job(job_id, {
                'close_attempt_ts': int(time.time()),
                'close_reason': reason,
                'close_legs': close_legs,
            })

            # refresh로 체결 확정 + PnL 계산
            refresh_result = await self._refresh_job_locked(
                self._jobs.get_job(job_id) or job,
                exit_spot_exchange=None,
                exit_futures_exchange=None,
            )

            final_job = (refresh_result or {}).get('job') or job
            logger.info(
                '[close_job] %s done | status=%s',
                ticker, final_job.get('status'),
            )
            return {
                'ok': True,
                'code': 'OK',
                'job': final_job,
                'legs': close_legs,
            }

    async def _submit_futures_close_with_reduce_only(
        self,
        futures_instance: 'ccxt.Exchange',
        exchange_name: str,
        symbol: str,
        amount: float,
    ) -> dict[str, Any]:
        """DEPRECATED: side='buy' 하드코딩 래퍼. 새 코드는 반드시
        `_submit_futures_close_generic_reduce_only(side=...)` 를 직접 호출할 것.

        이 래퍼는 SHORT 포지션 청산 전용(=reduceOnly BUY)으로만 동작한다.
        LONG 청산 시 사용하면 BUY 주문이 나가서 포지션이 두 배가 되거나
        거래소에서 reject 된다. (Bug #1, #3 — 내부적으로 generic 경로로 위임)
        """
        return await self._submit_futures_close_generic_reduce_only(
            futures_instance=futures_instance,
            exchange_name=exchange_name,
            symbol=symbol,
            side='buy',
            amount=amount,
        )

    # ==================================================================
    # FF (Futures-Futures) arbitrage executor
    #
    # 선선갭 (futures vs futures). Bithumb 현물/출금 경로 없이 양 거래소
    # 선물만 사용한다.
    #   - buy_exchange  → LONG (ask 지불)
    #   - sell_exchange → SHORT (bid 수취)
    #   - 두 레그 모두 격리 마진, 동일 레버리지
    #   - close_ff: LONG 쪽 SELL reduceOnly + SHORT 쪽 BUY reduceOnly
    #
    # 기존 hedge 경로와 구분하기 위해 job 의 `trade_type='ff_arb'` 필드를 사용한다.
    # ==================================================================

    def _latest_open_ff_job(self, ticker: str) -> dict[str, Any] | None:
        """FF 아비트라지 — 열린 job 찾기. trade_type='ff_arb' + 수량 양쪽 > 0."""
        target = (ticker or '').strip().upper()
        if not target:
            return None
        candidates: list[dict[str, Any]] = []
        for item in self._jobs.list_jobs(limit=500):
            if str(item.get('ticker', '')).upper() != target:
                continue
            if str(item.get('trade_type') or '').strip().lower() != 'ff_arb':
                continue
            status = str(item.get('status') or '').strip().lower()
            if status not in {'hedged', 'partial_hedged', 'ff_open', 'ff_partial'}:
                continue
            qty_buy = _safe_float(item.get('entry_qty_buy_side')) or 0.0
            qty_sell = _safe_float(item.get('entry_qty_sell_side')) or 0.0
            if qty_buy <= _QTY_EPSILON or qty_sell <= _QTY_EPSILON:
                continue
            candidates.append(item)
        if not candidates:
            return None
        candidates.sort(key=lambda it: int(it.get('created_at', 0) or 0), reverse=True)
        return dict(candidates[0])

    async def enter_ff(
        self,
        ticker: str,
        buy_exchange: str,
        sell_exchange: str,
        notional_usd: float | None = None,
        leverage: int | None = None,
    ) -> dict[str, Any]:
        """FF 엔트리: buy_exchange LONG + sell_exchange SHORT.

        Returns dict with {ok, code?, message?, job}.
        """
        ticker = str(ticker or '').strip().upper()
        buy_exchange = str(buy_exchange or '').strip().lower()
        sell_exchange = str(sell_exchange or '').strip().lower()

        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker is required'}
        if buy_exchange == sell_exchange:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': 'buy_exchange and sell_exchange must differ',
            }
        if buy_exchange not in config.EXCHANGES_WITH_FUTURES:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': f'buy_exchange {buy_exchange} does not support futures',
            }
        if sell_exchange not in config.EXCHANGES_WITH_FUTURES:
            return {
                'ok': False,
                'code': 'INVALID_INPUT',
                'message': f'sell_exchange {sell_exchange} does not support futures',
            }

        nominal = _safe_float(notional_usd)
        if nominal is None or nominal <= 0:
            nominal = float(getattr(config, 'FF_EXECUTOR_NOTIONAL_USD', 30.0))
        if nominal <= 0:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'notional must be positive'}

        lev = int(leverage or getattr(config, 'FF_EXECUTOR_LEVERAGE', 3) or 3)
        if lev <= 0:
            lev = 1

        async with self._lock:
            existing = self._latest_open_ff_job(ticker)
            if existing:
                return {
                    'ok': False,
                    'code': 'FF_ACTIVE_EXISTS',
                    'message': f'{ticker} already has an open FF arbitrage job',
                    'job': existing,
                }
            # 동일 티커의 일반 hedge(spot+futures) 가 열려 있으면 리스크 중첩 방지
            spot_hedge = self._jobs.latest_open_job(ticker=ticker)
            if spot_hedge:
                return {
                    'ok': False,
                    'code': 'SPOT_HEDGE_ACTIVE',
                    'message': f'{ticker} spot-hedge job is open; close it before FF entry',
                    'job': spot_hedge,
                }

            return await self._enter_ff_locked(
                ticker=ticker,
                buy_exchange=buy_exchange,
                sell_exchange=sell_exchange,
                notional_usd=nominal,
                leverage=lev,
            )

    async def _enter_ff_locked(
        self,
        ticker: str,
        buy_exchange: str,
        sell_exchange: str,
        notional_usd: float,
        leverage: int,
    ) -> dict[str, Any]:
        buy_instance = exchange_manager.get_instance(buy_exchange, 'swap')
        sell_instance = exchange_manager.get_instance(sell_exchange, 'swap')
        if buy_instance is None:
            return {
                'ok': False,
                'code': 'EXCHANGE_INSTANCE_UNAVAILABLE',
                'message': f'{buy_exchange} swap instance unavailable',
            }
        if sell_instance is None:
            return {
                'ok': False,
                'code': 'EXCHANGE_INSTANCE_UNAVAILABLE',
                'message': f'{sell_exchange} swap instance unavailable',
            }

        try:
            if not buy_instance.markets:
                await buy_instance.load_markets()
            if not sell_instance.markets:
                await sell_instance.load_markets()
        except Exception as exc:
            return {
                'ok': False,
                'code': 'LOAD_MARKETS_FAILED',
                'message': f'load_markets failed: {exc}',
            }

        buy_symbol = exchange_manager.get_symbol(ticker, 'swap', buy_exchange)
        sell_symbol = exchange_manager.get_symbol(ticker, 'swap', sell_exchange)

        # BBO 조회 — qty 산정은 buy 쪽 midpoint 기준 (구매가 기준 보수적으로 qty 계산)
        try:
            buy_bbo, sell_bbo = await asyncio.gather(
                exchange_manager.fetch_bbo(buy_instance, buy_symbol),
                exchange_manager.fetch_bbo(sell_instance, sell_symbol),
            )
        except Exception as exc:
            return {
                'ok': False,
                'code': 'BBO_FETCH_FAILED',
                'message': f'fetch_bbo failed: {exc}',
            }

        buy_ask = _safe_float(buy_bbo.ask) if buy_bbo is not None else None
        buy_bid = _safe_float(buy_bbo.bid) if buy_bbo is not None else None
        sell_bid = _safe_float(sell_bbo.bid) if sell_bbo is not None else None
        sell_ask = _safe_float(sell_bbo.ask) if sell_bbo is not None else None

        if buy_ask is None or buy_ask <= 0 or buy_bid is None or buy_bid <= 0:
            return {
                'ok': False,
                'code': 'PRICE_UNAVAILABLE',
                'message': f'{buy_exchange} futures bbo unavailable for {ticker}',
            }
        if sell_bid is None or sell_bid <= 0 or sell_ask is None or sell_ask <= 0:
            return {
                'ok': False,
                'code': 'PRICE_UNAVAILABLE',
                'message': f'{sell_exchange} futures bbo unavailable for {ticker}',
            }

        # qty 산정: buy 쪽 ask (구매 체결가 근사) 기준
        reference_price = buy_ask
        raw_qty = notional_usd / reference_price
        buy_qty = self._normalize_amount(buy_instance, buy_symbol, raw_qty)
        sell_qty = self._normalize_amount(sell_instance, sell_symbol, raw_qty)
        # 양쪽 정합: 작은 쪽 기준으로 재정규화
        target_qty = min(buy_qty, sell_qty)
        if target_qty <= _QTY_EPSILON:
            return {
                'ok': False,
                'code': 'QTY_TOO_SMALL',
                'message': 'normalized qty is too small on one of the legs',
            }
        buy_qty = self._normalize_amount(buy_instance, buy_symbol, target_qty)
        sell_qty = self._normalize_amount(sell_instance, sell_symbol, target_qty)
        if buy_qty <= _QTY_EPSILON or sell_qty <= _QTY_EPSILON:
            return {
                'ok': False,
                'code': 'QTY_TOO_SMALL',
                'message': 'normalized qty mismatch',
            }

        # 격리 + 레버리지 설정 (양쪽)
        buy_warnings, sell_warnings = await asyncio.gather(
            self._prepare_futures_account(buy_instance, buy_symbol, leverage),
            self._prepare_futures_account(sell_instance, sell_symbol, leverage),
        )
        warnings = self._merge_unique_strings(
            [f'buy({buy_exchange}): {w}' for w in (buy_warnings or [])],
            [f'sell({sell_exchange}): {w}' for w in (sell_warnings or [])],
        )

        pre_entry_spread_pct = None
        pre_entry_spread_usdt = None
        if sell_bid > 0 and buy_ask > 0:
            pre_entry_spread_usdt = sell_bid - buy_ask
            pre_entry_spread_pct = (sell_bid - buy_ask) / buy_ask * 100.0

        entry_timestamp = int(time.time())

        # Job record 선생성 (실패 시에도 남긴다)
        job = self._jobs.create_job(
            {
                'ticker': ticker,
                'trade_type': 'ff_arb',
                'status': 'requested',
                'buy_exchange': buy_exchange,
                'sell_exchange': sell_exchange,
                'futures_exchange': buy_exchange,  # legacy field 호환
                'leverage': leverage,
                'notional_usd': notional_usd,
                'requested_qty': target_qty,
                'price_reference': {
                    'buy_bid': buy_bid,
                    'buy_ask': buy_ask,
                    'sell_bid': sell_bid,
                    'sell_ask': sell_ask,
                },
                'warnings': warnings,
                'events': [],
            }
        )
        job_id = str(job.get('job_id') or '').strip()

        # 병렬 제출
        long_task = asyncio.create_task(
            self._submit_market_order(
                exchange_instance=buy_instance,
                exchange_name=buy_exchange,
                symbol=buy_symbol,
                side='buy',
                amount=buy_qty,
                market='futures',
            )
        )
        short_task = asyncio.create_task(
            self._submit_market_order(
                exchange_instance=sell_instance,
                exchange_name=sell_exchange,
                symbol=sell_symbol,
                side='sell',
                amount=sell_qty,
                market='futures',
            )
        )
        long_leg, short_leg = await asyncio.gather(long_task, short_task)

        long_legs: list[dict[str, Any]] = [long_leg]
        short_legs: list[dict[str, Any]] = [short_leg]

        long_filled = float(long_leg.get('filled_qty', 0) or 0)
        short_filled = float(short_leg.get('filled_qty', 0) or 0)

        # Case 1: 둘 다 실패
        if long_filled <= _QTY_EPSILON and short_filled <= _QTY_EPSILON:
            updated = self._jobs.update_job(
                job_id,
                {
                    'status': 'failed',
                    'message': 'both FF legs failed to fill',
                    'legs': {'buy': long_legs, 'sell': short_legs},
                },
            )
            return {
                'ok': False,
                'code': 'ENTRY_FAILED',
                'message': 'both FF legs were not filled',
                'job': updated or job,
            }

        # Case 2: LONG 체결, SHORT 미체결 → LONG 롤백 (reduceOnly SELL)
        if long_filled > _QTY_EPSILON and short_filled <= _QTY_EPSILON:
            rollback_qty = self._normalize_amount(buy_instance, buy_symbol, long_filled)
            rollback_result = None
            if rollback_qty > _QTY_EPSILON:
                # LONG 청산 → side='sell' (Bug #1 fix: 이전엔 'buy' 하드코딩 래퍼라
                # 롤백이 포지션을 두 배로 만들었음)
                rollback_result = await self._submit_futures_close_generic_reduce_only(
                    futures_instance=buy_instance,
                    exchange_name=buy_exchange,
                    symbol=buy_symbol,
                    side='sell',
                    amount=rollback_qty,
                )
                long_legs.append(rollback_result)
            rollback_filled = float((rollback_result or {}).get('filled_qty') or 0.0)
            rolled_back = rollback_filled + _QTY_EPSILON >= long_filled
            status = 'rolled_back' if rolled_back else 'rollback_failed'
            updated = self._jobs.update_job(
                job_id,
                {
                    'status': status,
                    'message': (
                        'SHORT leg not filled; LONG rolled back'
                        if rolled_back
                        else 'SHORT leg not filled; LONG rollback attempted but incomplete'
                    ),
                    'legs': {'buy': long_legs, 'sell': short_legs},
                },
            )
            return {
                'ok': False,
                'code': 'ONE_SIDED_FILL',
                'message': (updated or {}).get('message') if isinstance(updated, dict) else None,
                'job': updated or job,
            }

        # Case 3: SHORT 체결, LONG 미체결 → SHORT 롤백 (reduceOnly BUY)
        if short_filled > _QTY_EPSILON and long_filled <= _QTY_EPSILON:
            rollback_qty = self._normalize_amount(sell_instance, sell_symbol, short_filled)
            rollback_result = None
            if rollback_qty > _QTY_EPSILON:
                rollback_result = await self._submit_futures_close_generic_reduce_only(
                    futures_instance=sell_instance,
                    exchange_name=sell_exchange,
                    symbol=sell_symbol,
                    side='buy',
                    amount=rollback_qty,
                )
                short_legs.append(rollback_result)
            rollback_filled = float((rollback_result or {}).get('filled_qty') or 0.0)
            rolled_back = rollback_filled + _QTY_EPSILON >= short_filled
            status = 'rolled_back' if rolled_back else 'rollback_failed'
            updated = self._jobs.update_job(
                job_id,
                {
                    'status': status,
                    'message': (
                        'LONG leg not filled; SHORT rolled back'
                        if rolled_back
                        else 'LONG leg not filled; SHORT rollback attempted but incomplete'
                    ),
                    'legs': {'buy': long_legs, 'sell': short_legs},
                },
            )
            return {
                'ok': False,
                'code': 'ONE_SIDED_FILL',
                'message': (updated or {}).get('message') if isinstance(updated, dict) else None,
                'job': updated or job,
            }

        # Case 4: 양쪽 체결 (qty 미스매치 가능 → 작은 쪽 기준으로 조정)
        events: list[dict[str, Any]] = []
        qty_diff = long_filled - short_filled
        if abs(qty_diff) > _QTY_EPSILON:
            if qty_diff > 0:
                # LONG 초과 → LONG reduce (SELL reduceOnly on buy_exchange)
                adj_qty = self._normalize_amount(buy_instance, buy_symbol, qty_diff)
                if adj_qty > _QTY_EPSILON:
                    # LONG 축소 → side='sell' (Bug #1 fix)
                    adj_leg = await self._submit_futures_close_generic_reduce_only(
                        futures_instance=buy_instance,
                        exchange_name=buy_exchange,
                        symbol=buy_symbol,
                        side='sell',
                        amount=adj_qty,
                    )
                    long_legs.append(adj_leg)
                    events.append({
                        'type': 'ff_adjust_long_reduce',
                        'requested_qty': adj_qty,
                        'filled_qty': adj_leg.get('filled_qty'),
                    })
            else:
                # SHORT 초과 → SHORT reduce (BUY reduceOnly on sell_exchange)
                adj_qty = self._normalize_amount(sell_instance, sell_symbol, abs(qty_diff))
                if adj_qty > _QTY_EPSILON:
                    adj_leg = await self._submit_futures_close_generic_reduce_only(
                        futures_instance=sell_instance,
                        exchange_name=sell_exchange,
                        symbol=sell_symbol,
                        side='buy',
                        amount=adj_qty,
                    )
                    short_legs.append(adj_leg)
                    events.append({
                        'type': 'ff_adjust_short_reduce',
                        'requested_qty': adj_qty,
                        'filled_qty': adj_leg.get('filled_qty'),
                    })

        # 체결가/수량 집계
        long_net_qty, long_avg = self._compute_net_ff_long(long_legs)
        short_net_qty, short_avg = self._compute_net_ff_short(short_legs)

        matched_qty = min(long_net_qty, short_net_qty)
        if matched_qty <= _QTY_EPSILON:
            updated = self._jobs.update_job(
                job_id,
                {
                    'status': 'failed',
                    'message': 'post-adjust net qty is zero',
                    'legs': {'buy': long_legs, 'sell': short_legs},
                    'events': events,
                },
            )
            return {
                'ok': False,
                'code': 'ENTRY_FAILED',
                'message': 'FF legs net qty zero after adjustment',
                'job': updated or job,
            }

        entry_spread_usdt = None
        entry_spread_pct = None
        if long_avg is not None and long_avg > 0 and short_avg is not None and short_avg > 0:
            entry_spread_usdt = short_avg - long_avg
            entry_spread_pct = (short_avg - long_avg) / long_avg * 100.0

        is_balanced = (
            abs(long_net_qty - short_net_qty)
            <= max(matched_qty * 0.01, _QTY_EPSILON * 10)
        )
        status = 'ff_open' if is_balanced else 'ff_partial'

        entry_event = {
            'type': 'ff_entry',
            'timestamp': entry_timestamp,
            'ticker': ticker,
            'buy_exchange': buy_exchange,
            'sell_exchange': sell_exchange,
            'notional_usd': notional_usd,
            'leverage': leverage,
            'long_filled': long_net_qty,
            'short_filled': short_net_qty,
            'long_avg': long_avg,
            'short_avg': short_avg,
            'entry_spread_usdt': entry_spread_usdt,
            'entry_spread_pct': entry_spread_pct,
            'pre_entry_spread_pct': pre_entry_spread_pct,
        }

        updated = self._jobs.update_job(
            job_id,
            {
                'status': status,
                'message': 'FF entry opened',
                'legs': {'buy': long_legs, 'sell': short_legs},
                'events': [entry_event, *events],
                'entry_qty_buy_side': long_net_qty,
                'entry_qty_sell_side': short_net_qty,
                'entry_avg_buy': long_avg,
                'entry_avg_sell': short_avg,
                'entry_spread_pct': entry_spread_pct,
                'entry_spread_usdt': entry_spread_usdt,
                'pre_entry_spread_pct': pre_entry_spread_pct,
                'pre_entry_spread_usdt': pre_entry_spread_usdt,
                'warnings': warnings,
            },
        )

        logger.info(
            '[enter_ff] %s OPEN | buy=%s@%.6g sell=%s@%.6g qty=%.6f spread=%.3f%% status=%s',
            ticker,
            buy_exchange,
            long_avg or 0.0,
            sell_exchange,
            short_avg or 0.0,
            matched_qty,
            entry_spread_pct or 0.0,
            status,
        )

        return {
            'ok': True,
            'code': 'OK',
            'message': 'FF entry opened',
            'status': status,
            'job': updated or job,
        }

    async def close_ff(self, ticker: str, reason: str = 'manual') -> dict[str, Any]:
        """FF 포지션 청산: LONG → reduceOnly SELL, SHORT → reduceOnly BUY."""
        ticker = str(ticker or '').strip().upper()
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}

        async with self._lock:
            job = self._latest_open_ff_job(ticker)
            if not job:
                return {'ok': False, 'code': 'NO_OPEN_FF_JOB', 'message': f'no open FF job for {ticker}'}

            job_id = str(job.get('job_id') or '').strip()
            buy_exchange = str(job.get('buy_exchange') or '').strip().lower()
            sell_exchange = str(job.get('sell_exchange') or '').strip().lower()
            qty_buy = float(_safe_float(job.get('entry_qty_buy_side')) or 0.0)
            qty_sell = float(_safe_float(job.get('entry_qty_sell_side')) or 0.0)

            if not buy_exchange or not sell_exchange:
                return {'ok': False, 'code': 'INVALID_JOB', 'message': 'job missing exchanges'}
            if qty_buy <= _QTY_EPSILON and qty_sell <= _QTY_EPSILON:
                return {'ok': False, 'code': 'ZERO_QTY', 'message': 'both leg qty zero'}

            buy_instance = exchange_manager.get_instance(buy_exchange, 'swap')
            sell_instance = exchange_manager.get_instance(sell_exchange, 'swap')
            if buy_instance is None or sell_instance is None:
                return {
                    'ok': False,
                    'code': 'EXCHANGE_INSTANCE_UNAVAILABLE',
                    'message': 'swap instance missing for close',
                }

            buy_symbol = exchange_manager.get_symbol(ticker, 'swap', buy_exchange)
            sell_symbol = exchange_manager.get_symbol(ticker, 'swap', sell_exchange)

            logger.info(
                '[close_ff] %s START | job=%s buy=%s qty=%.6f sell=%s qty=%.6f reason=%s',
                ticker, job_id, buy_exchange, qty_buy, sell_exchange, qty_sell, reason,
            )

            tasks: list[asyncio.Task] = []
            if qty_buy > _QTY_EPSILON:
                norm_qty_buy = self._normalize_amount(buy_instance, buy_symbol, qty_buy)
                if norm_qty_buy > _QTY_EPSILON:
                    # FF LONG leg 청산 → reduceOnly SELL (Bug #1 fix: 이전엔
                    # side='buy' 하드코딩 래퍼를 사용해 LONG 포지션을 오히려 늘리거나
                    # 거래소가 reject 했음)
                    tasks.append(asyncio.create_task(
                        self._submit_futures_close_generic_reduce_only(
                            futures_instance=buy_instance,
                            exchange_name=buy_exchange,
                            symbol=buy_symbol,
                            side='sell',
                            amount=norm_qty_buy,
                        ),
                        name=f'ff_close_long_{ticker}',
                    ))
            if qty_sell > _QTY_EPSILON:
                norm_qty_sell = self._normalize_amount(sell_instance, sell_symbol, qty_sell)
                if norm_qty_sell > _QTY_EPSILON:
                    # FF SHORT leg 청산 → reduceOnly BUY
                    tasks.append(asyncio.create_task(
                        self._submit_futures_close_generic_reduce_only(
                            futures_instance=sell_instance,
                            exchange_name=sell_exchange,
                            symbol=sell_symbol,
                            side='buy',
                            amount=norm_qty_sell,
                        ),
                        name=f'ff_close_short_{ticker}',
                    ))

            if not tasks:
                return {'ok': False, 'code': 'ZERO_QTY', 'message': 'no close tasks scheduled'}

            results = await asyncio.gather(*tasks, return_exceptions=True)
            close_long: dict[str, Any] | None = None
            close_short: dict[str, Any] | None = None
            for r in results:
                if isinstance(r, Exception):
                    logger.error('[close_ff] leg exception: %s', r)
                    continue
                if not isinstance(r, dict):
                    continue
                exchange_name = str(r.get('exchange') or '').strip().lower()
                # Bug #2 fix: side 매칭 제거. 이전엔 side='sell' 를 기대했지만
                # 래퍼가 항상 'buy' 를 echo → close_long 이 영원히 None 이었음.
                # 이제는 side 가 올바르게 세팅되지만, 같은 buy_exchange/sell_exchange
                # 여도 exchange 기준 매칭이 더 안전. 동일 거래소 케이스(거의 없음)는
                # 첫 번째 매칭을 long 으로, 두 번째를 short 로 취급하지 않고
                # side 로 보조 판별.
                side = str(r.get('side') or '').strip().lower()
                if exchange_name == buy_exchange and exchange_name != sell_exchange:
                    close_long = r
                elif exchange_name == sell_exchange and exchange_name != buy_exchange:
                    close_short = r
                elif exchange_name == buy_exchange == sell_exchange:
                    # 동일 거래소 엣지케이스 — side 로 구분
                    if side == 'sell' and close_long is None:
                        close_long = r
                    elif side == 'buy' and close_short is None:
                        close_short = r

            close_legs = {
                'buy_side_close': [close_long] if close_long else [],
                'sell_side_close': [close_short] if close_short else [],
            }
            self._jobs.update_job(
                job_id,
                {
                    'close_attempt_ts': int(time.time()),
                    'close_reason': reason,
                    'close_legs': close_legs,
                },
            )

            refreshed = self._refresh_ff_job_locked(job_id)
            final_job = refreshed or self._jobs.get_job(job_id) or job
            logger.info(
                '[close_ff] %s DONE | status=%s',
                ticker, final_job.get('status'),
            )
            return {
                'ok': True,
                'code': 'OK',
                'job': final_job,
                'legs': close_legs,
            }

    def _refresh_ff_job_locked(self, job_id: str) -> dict[str, Any] | None:
        """FF close legs 기록으로 net qty / PnL 재계산."""
        job = self._jobs.get_job(job_id)
        if not job:
            return None

        legs = job.get('legs') or {}
        buy_legs = list(legs.get('buy') or [])
        sell_legs = list(legs.get('sell') or [])
        close_legs = job.get('close_legs') or {}
        buy_close_legs = list(close_legs.get('buy_side_close') or [])
        sell_close_legs = list(close_legs.get('sell_side_close') or [])

        # 열려있는 LONG 잔량 / SHORT 잔량
        long_net_qty, long_avg_entry = self._compute_net_ff_long_with_closes(
            buy_legs, buy_close_legs,
        )
        short_net_qty, short_avg_entry = self._compute_net_ff_short_with_closes(
            sell_legs, sell_close_legs,
        )

        # 청산 체결가 집계
        close_long_qty, close_long_avg = self._sum_fills(buy_close_legs)
        close_short_qty, close_short_avg = self._sum_fills(sell_close_legs)

        entry_buy_qty = _safe_float(job.get('entry_qty_buy_side')) or 0.0
        entry_sell_qty = _safe_float(job.get('entry_qty_sell_side')) or 0.0
        entry_buy_avg = _safe_float(job.get('entry_avg_buy'))
        entry_sell_avg = _safe_float(job.get('entry_avg_sell'))

        # closed 판정: 양 레그 모두 entry 대비 1% 이내 잔량
        def _is_closed(entry_qty: float, residual: float) -> bool:
            if entry_qty <= _QTY_EPSILON:
                return False
            tol = max(entry_qty * 0.01, _QTY_EPSILON * 10)
            return residual <= tol

        long_closed = _is_closed(entry_buy_qty, long_net_qty)
        short_closed = _is_closed(entry_sell_qty, short_net_qty)

        status = str(job.get('status') or '').strip().lower()
        final_pnl_usdt = None
        matched_close = min(close_long_qty, close_short_qty)

        if long_closed and short_closed:
            status = 'closed'
            if (
                entry_buy_avg is not None
                and entry_buy_avg > 0
                and entry_sell_avg is not None
                and entry_sell_avg > 0
                and close_long_avg is not None
                and close_long_avg > 0
                and close_short_avg is not None
                and close_short_avg > 0
                and matched_close > _QTY_EPSILON
            ):
                # LONG PnL = (close_long_avg - entry_buy_avg) * qty
                # SHORT PnL = (entry_sell_avg - close_short_avg) * qty
                long_pnl = (close_long_avg - entry_buy_avg) * matched_close
                short_pnl = (entry_sell_avg - close_short_avg) * matched_close
                final_pnl_usdt = long_pnl + short_pnl

        updates: dict[str, Any] = {
            'status': status,
            'close_qty_buy_side': close_long_qty,
            'close_qty_sell_side': close_short_qty,
            'close_avg_buy_side': close_long_avg,
            'close_avg_sell_side': close_short_avg,
            'residual_qty_buy_side': long_net_qty,
            'residual_qty_sell_side': short_net_qty,
        }
        if status == 'closed':
            updates['closed_at'] = int(time.time())
            updates['finalized_at'] = int(time.time())
            updates['final_pnl_usdt'] = final_pnl_usdt

        return self._jobs.update_job(job_id, updates)

    @staticmethod
    def _sum_fills(legs: list[dict[str, Any]]) -> tuple[float, float | None]:
        total_qty = 0.0
        total_cost = 0.0
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            filled = _safe_float(leg.get('filled_qty'))
            avg = _safe_float(leg.get('avg_price'))
            if filled is None or filled <= _QTY_EPSILON or avg is None or avg <= 0:
                continue
            total_qty += filled
            total_cost += filled * avg
        if total_qty <= _QTY_EPSILON:
            return 0.0, None
        return total_qty, total_cost / total_qty

    @staticmethod
    def _compute_net_ff_long(legs: list[dict[str, Any]]) -> tuple[float, float | None]:
        """LONG leg: buy=증가, sell=감소 (adjust reduce)."""
        qty = 0.0
        cost = 0.0
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            side = str(leg.get('side') or '').lower()
            filled = _safe_float(leg.get('filled_qty'))
            price = _safe_float(leg.get('avg_price'))
            if filled is None or filled <= _QTY_EPSILON or price is None or price <= 0:
                continue
            if side == 'buy':
                qty += filled
                cost += filled * price
            elif side == 'sell' and qty > _QTY_EPSILON:
                close_qty = min(qty, filled)
                avg = cost / qty if qty > _QTY_EPSILON else 0.0
                qty -= close_qty
                cost -= avg * close_qty
        if qty <= _QTY_EPSILON:
            return 0.0, None
        return qty, cost / qty

    @staticmethod
    def _compute_net_ff_short(legs: list[dict[str, Any]]) -> tuple[float, float | None]:
        """SHORT leg: sell=증가, buy=감소 (adjust reduce)."""
        qty = 0.0
        cost = 0.0
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            side = str(leg.get('side') or '').lower()
            filled = _safe_float(leg.get('filled_qty'))
            price = _safe_float(leg.get('avg_price'))
            if filled is None or filled <= _QTY_EPSILON or price is None or price <= 0:
                continue
            if side == 'sell':
                qty += filled
                cost += filled * price
            elif side == 'buy' and qty > _QTY_EPSILON:
                close_qty = min(qty, filled)
                avg = cost / qty if qty > _QTY_EPSILON else 0.0
                qty -= close_qty
                cost -= avg * close_qty
        if qty <= _QTY_EPSILON:
            return 0.0, None
        return qty, cost / qty

    @classmethod
    def _compute_net_ff_long_with_closes(
        cls,
        entry_legs: list[dict[str, Any]],
        close_legs: list[dict[str, Any]],
    ) -> tuple[float, float | None]:
        combined: list[dict[str, Any]] = list(entry_legs)
        # close_legs 는 항상 reduce (SELL) 이므로 side 강제 덮어쓰기 없이 그대로 추가 (원래 side=sell)
        for leg in close_legs:
            if isinstance(leg, dict):
                combined.append(leg)
        return cls._compute_net_ff_long(combined)

    @classmethod
    def _compute_net_ff_short_with_closes(
        cls,
        entry_legs: list[dict[str, Any]],
        close_legs: list[dict[str, Any]],
    ) -> tuple[float, float | None]:
        combined: list[dict[str, Any]] = list(entry_legs)
        for leg in close_legs:
            if isinstance(leg, dict):
                combined.append(leg)
        return cls._compute_net_ff_short(combined)

    async def _submit_futures_close_generic_reduce_only(
        self,
        futures_instance: 'ccxt.Exchange',
        exchange_name: str,
        symbol: str,
        side: str,
        amount: float,
    ) -> dict[str, Any]:
        """reduceOnly close — SHORT 포지션 청산용 (side='buy') 이나 일반화.

        기존 _submit_futures_close_with_reduce_only 는 'buy' 하드코딩이라
        FF SHORT 레그(sell_exchange) 청산용으로 별도 제공.
        """
        side = str(side or '').strip().lower()
        if side not in {'buy', 'sell'}:
            return {
                'exchange': exchange_name,
                'market': 'futures',
                'symbol': symbol,
                'side': side,
                'requested_qty': amount,
                'status': 'failed',
                'filled_qty': 0.0,
                'avg_price': None,
                'cost': None,
                'order_id': None,
                'error': f'invalid side {side}',
                'poll_error': None,
                'reduce_only': True,
            }

        result: dict[str, Any] = {
            'exchange': exchange_name,
            'market': 'futures',
            'symbol': symbol,
            'side': side,
            'requested_qty': amount,
            'status': 'failed',
            'filled_qty': 0.0,
            'avg_price': None,
            'cost': None,
            'order_id': None,
            'error': None,
            'poll_error': None,
            'reduce_only': True,
        }

        attempts = int(getattr(config, 'HEDGE_ORDER_POLL_ATTEMPTS', 4))
        delay_ms = int(getattr(config, 'HEDGE_ORDER_POLL_DELAY_MS', 250))
        delay_sec = max(delay_ms, 1) / 1000

        try:
            order = await futures_instance.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount,
                price=None,
                params={'reduceOnly': True},
            )
        except Exception as exc:
            result['error'] = str(exc)
            logger.error('[ff_close_reduce %s] %s create_order failed: %s', side, symbol, exc)
            return result

        order_id = order.get('id') if isinstance(order, dict) else None
        result['order_id'] = order_id

        for _ in range(attempts):
            try:
                poll_params = {}
                if exchange_name == 'bybit':
                    poll_params['acknowledged'] = True
                current = await futures_instance.fetch_order(order_id, symbol, poll_params) \
                    if order_id else order
            except Exception as exc:
                result['poll_error'] = str(exc)
                break
            filled = _safe_float(current.get('filled') if isinstance(current, dict) else None) or 0.0
            status = str((current or {}).get('status', '')).lower() if isinstance(current, dict) else ''
            if filled > _QTY_EPSILON and status in {'closed', 'filled'}:
                # update() 로 병합 — side/exchange/symbol/reduce_only 등 request 컨텍스트 유지
                result.update(self._map_order_result(current, result))
                result['status'] = 'ok'
                return result
            await asyncio.sleep(delay_sec)

        if isinstance(order, dict):
            result.update(self._map_order_result(order, result))
            if result.get('filled_qty', 0) > _QTY_EPSILON:
                result['status'] = 'ok'
        return result
