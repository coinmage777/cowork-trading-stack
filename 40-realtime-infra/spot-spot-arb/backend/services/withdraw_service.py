"""Withdrawal preview/execute service."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any

import ccxt.async_support as ccxt

from backend import config
from backend.exchanges import manager as exchange_manager
from backend.exchanges.bithumb_private import (
    fetch_bithumb_balance,
    fetch_bithumb_network_info,
    fetch_bithumb_registered_withdraw_addresses,
    fetch_withdrawal_limit,
    submit_bithumb_withdrawal,
)
from backend.exchanges.types import NetworkInfo, WithdrawalLimit
from backend.services.withdraw_jobs import WithdrawJobStore

logger = logging.getLogger(__name__)


def _norm_network(value: str) -> str:
    return ''.join(ch for ch in value.upper() if ch.isalnum())


def _norm_text(value: str) -> str:
    return ''.join(ch for ch in value.strip().upper() if ch.isalnum())


def _mask_address(address: str) -> str:
    if len(address) <= 12:
        return address
    return f'{address[:8]}...{address[-6:]}'


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_optional_tag(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.upper() in {'NONE', 'NULL', 'N/A', 'NA', '-'}:
        return None
    return text


def _normalize_receiver_type(value: Any) -> str | None:
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


class WithdrawService:
    """Handles withdrawal preview and execution with safety checks."""

    def __init__(self) -> None:
        self._jobs = WithdrawJobStore()
        self._previews: dict[str, dict[str, Any]] = {}
        self._execute_lock = asyncio.Lock()

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._jobs.list_jobs(limit=limit)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._jobs.get_job(job_id)

    async def preview(
        self,
        ticker: str,
        target_exchange: str,
        withdraw_network: str,
        deposit_network: str,
    ) -> dict[str, Any]:
        self._cleanup_previews()

        ticker = ticker.strip().upper()
        target_exchange = target_exchange.strip().lower()
        withdraw_network = withdraw_network.strip()
        deposit_network = deposit_network.strip()

        if not ticker:
            return self._error('INVALID_INPUT', 'ticker is required')
        if target_exchange not in exchange_manager.ALL_EXCHANGES:
            return self._error('INVALID_INPUT', 'target_exchange is invalid')
        if target_exchange == 'coinone':
            return self._error(
                'TARGET_EXCHANGE_UNSUPPORTED',
                'coinone automatic withdraw is disabled',
            )
        if not withdraw_network or not deposit_network:
            return self._error(
                'INVALID_INPUT',
                'withdraw_network and deposit_network are required',
            )

        # 1) Validate Bithumb withdrawal network for the ticker.
        bithumb_networks = await fetch_bithumb_network_info(ticker)
        bithumb_net = self._find_network(bithumb_networks, withdraw_network)
        if bithumb_net is None:
            return self._error(
                'NO_WITHDRAW_NETWORK',
                f'bithumb network not found for {ticker}: {withdraw_network}',
            )
        if not bithumb_net.withdraw:
            return self._error(
                'WITHDRAW_DISABLED',
                f'bithumb withdraw disabled on network: {withdraw_network}',
            )

        # 2) Validate target exchange deposit network for the ticker.
        target_networks = await self._fetch_target_networks(target_exchange, ticker)
        target_net = self._find_network(target_networks, deposit_network)
        if target_net is None:
            return self._error(
                'NO_DEPOSIT_NETWORK',
                (
                    f'{target_exchange} network not found for '
                    f'{ticker}: {deposit_network}'
                ),
            )
        if not target_net.deposit:
            return self._error(
                'DEPOSIT_DISABLED',
                (
                    f'{target_exchange} deposit disabled '
                    f'on network: {deposit_network}'
                ),
            )
        resolved_deposit_network = str(target_net.network or '').strip() or deposit_network
        if target_exchange == 'coinone':
            coinone_validation = self._validate_coinone_network_safety(
                ticker=ticker,
                bithumb_networks=bithumb_networks,
            )
            if coinone_validation is not None:
                return coinone_validation

        # 3) Resolve a fresh deposit address from the target exchange.
        deposit_address = await self._fetch_deposit_address(
            target_exchange=target_exchange,
            ticker=ticker,
            deposit_network=resolved_deposit_network,
        )
        if not deposit_address.get('ok'):
            return deposit_address

        address = str(deposit_address.get('address', '')).strip()
        tag = _clean_optional_tag(deposit_address.get('tag'))

        # 3.5) Validate that destination is registered in Bithumb withdraw address book.
        registered_targets = await fetch_bithumb_registered_withdraw_addresses(
            currency=ticker,
            net_type=withdraw_network,
        )
        require_registered_match = bool(
            getattr(config, 'WITHDRAW_REQUIRE_REGISTERED_ADDRESS_MATCH', False)
        )
        if (
            require_registered_match
            and
            registered_targets is not None
            and not self._is_registered_withdraw_target(
                registered_targets,
                address=address,
                tag=tag,
            )
        ):
            registered_count = len(registered_targets)
            sample = (
                _mask_address(str(registered_targets[0].get('address', '')))
                if registered_count > 0
                else '-'
            )
            return self._error(
                'WITHDRAW_ADDRESS_NOT_REGISTERED',
                (
                    f'bithumb registered withdraw address not found for '
                    f'{ticker}:{withdraw_network}. '
                    f'registered={registered_count}, sample={sample}'
                ),
            )

        # 4) Balance/limit-based amount calculation.
        free_balance = await fetch_bithumb_balance(ticker)
        if free_balance is None:
            return self._error(
                'BALANCE_UNAVAILABLE',
                f'鍮쀬뜽 {ticker} ?붽퀬 議고쉶???ㅽ뙣?덉뒿?덈떎',
            )
        if free_balance <= 0:
            return self._error('INSUFFICIENT_BALANCE', f'?ъ슜 媛?ν븳 {ticker} ?붽퀬媛 ?놁뒿?덈떎')

        limit = await fetch_withdrawal_limit(ticker, net_type=withdraw_network)
        estimated_fee = self._pick_estimated_fee(bithumb_net, limit)
        safety_buffer = self._calculate_safety_buffer(free_balance)
        # Keep fee subtraction enabled by default because Bithumb balance checks
        # can reject near-full amounts as insufficient when fee headroom is absent.
        subtract_fee = self._should_subtract_fee_from_amount()
        raw_amount = free_balance - safety_buffer
        if subtract_fee:
            raw_amount -= estimated_fee
        amount = self._floor_amount(raw_amount)

        if amount <= 0:
            return self._error(
                'INSUFFICIENT_BALANCE',
                (
                    'available balance is too small after safety buffer'
                    + (' and fee' if subtract_fee else '')
                ),
            )

        min_withdraw = self._pick_min_withdraw(bithumb_net, limit)
        if min_withdraw is not None and amount < min_withdraw:
            return self._error(
                'BELOW_MIN_WITHDRAW',
                (
                    f'calculated amount {amount} is below minimum '
                    f'withdraw amount {min_withdraw}'
                ),
            )

        if limit and limit.onetime_coin is not None and amount > limit.onetime_coin:
            return self._error(
                'EXCEED_ONETIME_LIMIT',
                f'amount {amount} exceeds onetime limit {limit.onetime_coin}',
            )
        if (
            limit
            and limit.remaining_daily_coin is not None
            and amount > limit.remaining_daily_coin
        ):
            return self._error(
                'EXCEED_DAILY_REMAINING_LIMIT',
                (
                    f'amount {amount} exceeds remaining daily limit '
                    f'{limit.remaining_daily_coin}'
                ),
            )

        preview_token = str(uuid.uuid4())
        now = int(time.time())
        ttl_sec = int(getattr(config, 'WITHDRAW_PREVIEW_TTL_SECONDS', 180))
        expires_at = now + max(ttl_sec, 30)

        snapshot = {
            'preview_token': preview_token,
            'created_at': now,
            'expires_at': expires_at,
            'ticker': ticker,
            'target_exchange': target_exchange,
            'withdraw_network': withdraw_network,
            'deposit_network': resolved_deposit_network,
            'target_address': address,
            'target_tag': tag,
            'free_balance': free_balance,
            'estimated_fee': estimated_fee,
            'safety_buffer': safety_buffer,
            'withdraw_amount': amount,
            'limit': self._serialize_limit(limit),
        }
        self._previews[preview_token] = snapshot

        return {
            'ok': True,
            'preview_token': preview_token,
            'expires_at': expires_at,
            'ticker': ticker,
            'target_exchange': target_exchange,
            'withdraw_network': withdraw_network,
            'deposit_network': resolved_deposit_network,
            'target_address_masked': _mask_address(address),
            'target_tag': tag,
            'free_balance': free_balance,
            'estimated_fee': estimated_fee,
            'safety_buffer': safety_buffer,
            'withdraw_amount': amount,
            'limit': snapshot['limit'],
        }

    async def execute(self, preview_token: str) -> dict[str, Any]:
        self._cleanup_previews()

        preview_token = preview_token.strip()
        if not preview_token:
            return self._error('INVALID_INPUT', 'preview_token is required')

        async with self._execute_lock:
            existing = self._jobs.find_by_preview_token(preview_token)
            if existing and existing.get('status') in {'requested', 'submitted', 'done'}:
                return {
                    'ok': True,
                    'idempotent': True,
                    'job': existing,
                }

            snapshot = self._previews.get(preview_token)
            if snapshot is None:
                return self._error('PREVIEW_NOT_FOUND', 'preview token not found or expired')

            if int(snapshot.get('expires_at', 0)) < int(time.time()):
                self._previews.pop(preview_token, None)
                return self._error('PREVIEW_EXPIRED', 'preview token expired')
            if str(snapshot.get('target_exchange', '')).strip().lower() == 'coinone':
                self._previews.pop(preview_token, None)
                return self._error(
                    'TARGET_EXCHANGE_UNSUPPORTED',
                    'coinone automatic withdraw is disabled',
                )

            job = self._jobs.create_job({
                'preview_token': preview_token,
                'status': 'requested',
                'ticker': snapshot['ticker'],
                'target_exchange': snapshot['target_exchange'],
                'withdraw_network': snapshot['withdraw_network'],
                'deposit_network': snapshot['deposit_network'],
                'target_address': snapshot['target_address'],
                'target_tag': snapshot['target_tag'],
                'amount': snapshot['withdraw_amount'],
            })
            job_id = job['job_id']

            # Re-validate target deposit address right before executing withdrawal.
            latest_address = await self._fetch_deposit_address(
                target_exchange=snapshot['target_exchange'],
                ticker=snapshot['ticker'],
                deposit_network=snapshot['deposit_network'],
            )
            if not latest_address.get('ok'):
                failed = self._jobs.update_job(job_id, {
                    'status': 'failed',
                    'error_code': latest_address.get('code'),
                    'error_message': latest_address.get('message'),
                })
                return {
                    'ok': False,
                    'code': latest_address.get('code', 'ADDRESS_CHECK_FAILED'),
                    'message': latest_address.get('message', 'failed to refresh address'),
                    'job': failed,
                }

            latest_addr = str(latest_address.get('address', '')).strip()
            latest_tag = _clean_optional_tag(latest_address.get('tag'))

            if (
                _norm_text(latest_addr) != _norm_text(snapshot['target_address'])
                or _norm_text(latest_tag or '') != _norm_text(snapshot['target_tag'] or '')
            ):
                failed = self._jobs.update_job(job_id, {
                    'status': 'failed',
                    'error_code': 'ADDRESS_CHANGED',
                    'error_message': 'target exchange deposit address or tag changed',
                    'latest_target_address': latest_addr,
                    'latest_target_tag': latest_tag,
                })
                return {
                    'ok': False,
                    'code': 'ADDRESS_CHANGED',
                    'message': 'deposit address/tag changed. run preview again.',
                    'job': failed,
                }

            recipient_info = self._get_withdraw_recipient_info()
            missing_fields = self._missing_required_recipient_fields(
                exchange_name=str(snapshot.get('target_exchange', '')),
                recipient_info=recipient_info,
            )
            if missing_fields:
                message = (
                    'missing recipient info for Bithumb withdraw request: '
                    + ', '.join(missing_fields)
                    + '. set these env vars before retrying.'
                )
                failed = self._jobs.update_job(job_id, {
                    'status': 'failed',
                    'error_code': 'RECIPIENT_INFO_MISSING',
                    'error_message': message,
                })
                return {
                    'ok': False,
                    'code': 'RECIPIENT_INFO_MISSING',
                    'message': message,
                    'job': failed,
                }

            try:
                result = await submit_bithumb_withdrawal(
                    currency=snapshot['ticker'],
                    amount=float(snapshot['withdraw_amount']),
                    address=snapshot['target_address'],
                    net_type=snapshot['withdraw_network'],
                    exchange_name=snapshot['target_exchange'],
                    tag=snapshot['target_tag'],
                    receiver_type=recipient_info.get('receiver_type'),
                    receiver_ko_name=recipient_info.get('receiver_ko_name'),
                    receiver_en_name=recipient_info.get('receiver_en_name'),
                    receiver_corp_ko_name=recipient_info.get('receiver_corp_ko_name'),
                    receiver_corp_en_name=recipient_info.get('receiver_corp_en_name'),
                )
                submitted = self._jobs.update_job(job_id, {
                    'status': 'submitted',
                    'tx_id': result.get('id') or result.get('txid'),
                    'exchange_response': result,
                })
                return {'ok': True, 'job': submitted}
            except Exception as exc:
                message = self._normalize_withdraw_error_message(
                    str(exc).strip() or exc.__class__.__name__,
                    exchange_name=str(snapshot.get('target_exchange', '')),
                )
                failed = self._jobs.update_job(job_id, {
                    'status': 'failed',
                    'error_code': 'WITHDRAW_REQUEST_FAILED',
                    'error_message': message,
                })
                return {
                    'ok': False,
                    'code': 'WITHDRAW_REQUEST_FAILED',
                    'message': message,
                    'job': failed,
                }

    def _cleanup_previews(self) -> None:
        now = int(time.time())
        expired = [
            token
            for token, snapshot in self._previews.items()
            if int(snapshot.get('expires_at', 0)) < now
        ]
        for token in expired:
            self._previews.pop(token, None)

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {
            'ok': False,
            'code': code,
            'message': message,
        }

    @staticmethod
    def _find_network(networks: list[NetworkInfo], name: str) -> NetworkInfo | None:
        target = _norm_network(name)
        for network in networks:
            if _norm_network(network.network) == target:
                return network
        return None

    @staticmethod
    def _pick_estimated_fee(
        network_info: NetworkInfo,
        limit: WithdrawalLimit | None,
    ) -> float:
        if network_info.fee is not None:
            return max(float(network_info.fee), 0.0)
        if limit and limit.expected_fee is not None:
            return max(float(limit.expected_fee), 0.0)
        return 0.0

    @staticmethod
    def _pick_min_withdraw(
        network_info: NetworkInfo,
        limit: WithdrawalLimit | None,
    ) -> float | None:
        if network_info.min_withdraw is not None:
            return float(network_info.min_withdraw)
        if limit and limit.min_withdraw is not None:
            return float(limit.min_withdraw)
        return None

    @staticmethod
    def _calculate_safety_buffer(free_balance: float) -> float:
        ratio = float(getattr(config, 'WITHDRAW_SAFETY_BUFFER_RATIO', 0.002))
        minimum = float(getattr(config, 'WITHDRAW_SAFETY_BUFFER_MIN', 0.0))
        return max(free_balance * max(ratio, 0.0), max(minimum, 0.0))

    @staticmethod
    def _should_subtract_fee_from_amount() -> bool:
        return bool(getattr(config, 'WITHDRAW_SUBTRACT_FEE_FROM_AMOUNT', True))

    @staticmethod
    def _floor_amount(value: float) -> float:
        decimals = int(getattr(config, 'WITHDRAW_AMOUNT_DECIMALS', 6))
        quant = Decimal('1').scaleb(-max(decimals, 0))
        floored = Decimal(str(max(value, 0.0))).quantize(quant, rounding=ROUND_DOWN)
        return float(floored)

    @staticmethod
    def _serialize_limit(limit: WithdrawalLimit | None) -> dict[str, float | None]:
        if limit is None:
            return {
                'onetime_coin': None,
                'remaining_daily_coin': None,
                'daily_coin': None,
                'expected_fee': None,
                'min_withdraw': None,
            }
        return {
            'onetime_coin': _safe_float(limit.onetime_coin),
            'remaining_daily_coin': _safe_float(limit.remaining_daily_coin),
            'daily_coin': _safe_float(limit.daily_coin),
            'expected_fee': _safe_float(limit.expected_fee),
            'min_withdraw': _safe_float(limit.min_withdraw),
        }

    @staticmethod
    def _get_withdraw_recipient_info() -> dict[str, str]:
        mapping = {
            'receiver_type': getattr(config, 'BITHUMB_WITHDRAW_RECEIVER_TYPE', ''),
            'receiver_ko_name': getattr(config, 'BITHUMB_WITHDRAW_RECEIVER_KO_NAME', ''),
            'receiver_en_name': getattr(config, 'BITHUMB_WITHDRAW_RECEIVER_EN_NAME', ''),
            'receiver_corp_ko_name': getattr(
                config, 'BITHUMB_WITHDRAW_RECEIVER_CORP_KO_NAME', ''
            ),
            'receiver_corp_en_name': getattr(
                config, 'BITHUMB_WITHDRAW_RECEIVER_CORP_EN_NAME', ''
            ),
        }
        result: dict[str, str] = {}
        for key, value in mapping.items():
            if key == 'receiver_type':
                cleaned = _normalize_receiver_type(value)
            else:
                cleaned = _clean_optional_tag(value)
            if cleaned is not None:
                result[key] = cleaned
        return result

    @staticmethod
    def _missing_required_recipient_fields(
        exchange_name: str,
        recipient_info: dict[str, str],
    ) -> list[str]:
        configured = getattr(
            config, 'BITHUMB_WITHDRAW_REQUIRE_RECIPIENT_INFO_EXCHANGES', set()
        )
        if not isinstance(configured, set):
            return []
        if exchange_name.strip().lower() not in configured:
            return []

        required = ['receiver_type', 'receiver_ko_name', 'receiver_en_name']
        missing: list[str] = []
        for field in required:
            if not recipient_info.get(field):
                missing.append(field)
        receiver_type = recipient_info.get('receiver_type', '').strip().lower()
        if receiver_type == 'corporation':
            if not recipient_info.get('receiver_corp_ko_name'):
                missing.append('receiver_corp_ko_name')
            if not recipient_info.get('receiver_corp_en_name'):
                missing.append('receiver_corp_en_name')
        return missing

    @staticmethod
    def _normalize_withdraw_error_message(
        message: str,
        exchange_name: str,
    ) -> str:
        if 'request_fail' in message:
            receiver_type = _normalize_receiver_type(
                getattr(config, 'BITHUMB_WITHDRAW_RECEIVER_TYPE', '')
            )
            receiver_ko = _clean_optional_tag(
                getattr(config, 'BITHUMB_WITHDRAW_RECEIVER_KO_NAME', '')
            )
            receiver_en = _clean_optional_tag(
                getattr(config, 'BITHUMB_WITHDRAW_RECEIVER_EN_NAME', '')
            )
            if receiver_type and receiver_ko and receiver_en:
                return message
            hints = [
                'BITHUMB_WITHDRAW_RECEIVER_TYPE',
                'BITHUMB_WITHDRAW_RECEIVER_KO_NAME',
                'BITHUMB_WITHDRAW_RECEIVER_EN_NAME',
            ]
            return (
                message
                + ' | missing recipient fields for '
                + (exchange_name or 'target exchange')
                + '. configure env vars: '
                + ', '.join(hints)
            )
        return message

    @staticmethod
    def _is_registered_withdraw_target(
        registered_targets: list[dict[str, str | None]],
        address: str,
        tag: str | None,
    ) -> bool:
        target_address = address.strip()
        target_tag = _clean_optional_tag(tag) or ''

        for item in registered_targets:
            registered_address = str(item.get('address', '')).strip()
            registered_tag = _clean_optional_tag(item.get('tag')) or ''
            if registered_address == target_address and registered_tag == target_tag:
                return True
        return False

    def _validate_coinone_network_safety(
        self,
        ticker: str,
        bithumb_networks: list[NetworkInfo],
    ) -> dict[str, Any] | None:
        withdrawable = [
            str(network.network).strip()
            for network in bithumb_networks
            if network.withdraw
        ]
        if len(withdrawable) <= 1:
            return None

        return self._error(
            'COINONE_NETWORK_UNVERIFIED',
            (
                f'coinone does not expose deposit chain info for {ticker}. '
                'automatic withdraw is allowed only when Bithumb has a single '
                'withdraw-enabled network. available_bithumb_networks='
                + ', '.join(withdrawable)
            ),
        )

    async def _fetch_target_networks(
        self,
        target_exchange: str,
        ticker: str,
    ) -> list[NetworkInfo]:
        if target_exchange == 'upbit':
            return await exchange_manager.fetch_upbit_network_info(ticker)
        if target_exchange == 'coinone':
            return await exchange_manager.fetch_coinone_network_info(ticker)

        instance = exchange_manager.get_instance(target_exchange, 'spot')
        owns_instance = False
        if instance is None:
            instance = await self._create_spot_instance(target_exchange)
            owns_instance = instance is not None
        if instance is None:
            return []

        try:
            return await exchange_manager.fetch_network_info(instance, ticker)
        finally:
            if owns_instance:
                try:
                    await instance.close()
                except Exception:
                    pass

    async def _fetch_deposit_address(
        self,
        target_exchange: str,
        ticker: str,
        deposit_network: str,
    ) -> dict[str, Any]:
        instance = exchange_manager.get_instance(target_exchange, 'spot')
        owns_instance = False
        if instance is None:
            instance = await self._create_spot_instance(target_exchange)
            owns_instance = instance is not None
        if instance is None:
            return self._error(
                'EXCHANGE_INSTANCE_UNAVAILABLE',
                f'failed to initialize {target_exchange} spot instance',
            )

        try:
            payload = await self._fetch_deposit_address_payload(
                exchange_instance=instance,
                ticker=ticker,
                deposit_network=deposit_network,
            )
            if payload is None:
                return self._error(
                    'DEPOSIT_ADDRESS_UNAVAILABLE',
                    (
                        f'no deposit address for {ticker} '
                        f'on {target_exchange}:{deposit_network}'
                    ),
                )

            address = str(payload.get('address', '')).strip()
            if not address:
                return self._error(
                    'DEPOSIT_ADDRESS_UNAVAILABLE',
                    (
                        f'empty deposit address for {ticker} '
                        f'on {target_exchange}:{deposit_network}'
                    ),
                )

            return {
                'ok': True,
                'address': address,
                'tag': payload.get('tag'),
                'raw': payload.get('raw'),
            }
        except Exception as exc:
            return self._error(
                'DEPOSIT_ADDRESS_QUERY_FAILED',
                (
                    f'{target_exchange} deposit address query failed '
                    f'({ticker}:{deposit_network}): {exc}'
                ),
            )
        finally:
            if owns_instance:
                try:
                    await instance.close()
                except Exception:
                    pass

    async def _fetch_deposit_address_payload(
        self,
        exchange_instance: ccxt.Exchange,
        ticker: str,
        deposit_network: str,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {'network': deposit_network}
        network_norm = _norm_network(deposit_network)
        exchange_id = str(getattr(exchange_instance, 'id', '')).lower()
        if exchange_id == 'upbit':
            return await self._fetch_upbit_deposit_address_payload(
                exchange_instance=exchange_instance,
                ticker=ticker,
                deposit_network=deposit_network,
                network_norm=network_norm,
            )
        if exchange_id == 'coinone':
            return await self._fetch_coinone_deposit_address_payload(
                exchange_instance=exchange_instance,
                ticker=ticker,
            )

        if exchange_id == 'bitget':
            coin_id = ticker
            try:
                currency = exchange_instance.currency(ticker)
                if isinstance(currency, dict):
                    coin_id = str(currency.get('id') or ticker).strip() or ticker
            except Exception:
                pass

            chains = self._build_bitget_chain_candidates(
                exchange_instance=exchange_instance,
                ticker=ticker,
                deposit_network=deposit_network,
            )
            last_exc: Exception | None = None
            for chain in chains:
                try:
                    response = await exchange_instance.privateSpotGetV2SpotWalletDepositAddress({
                        'coin': coin_id,
                        'chain': chain,
                    })
                    payload = response.get('data') if isinstance(response, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    address = str(payload.get('address') or '').strip()
                    if not address:
                        continue
                    tag = payload.get('tag')
                    return {
                        'address': address,
                        'tag': str(tag).strip() if tag else None,
                        'raw': payload,
                    }
                except Exception as exc:
                    last_exc = exc
                    text = str(exc).lower()
                    if '40019' in text and 'chain' in text and 'empty' in text:
                        continue
                    if 'parameter chain cannot be empty' in text:
                        continue
                    if '40017' in text and 'parameter verification failed' in text:
                        continue
                    raise

            if last_exc is not None:
                raise last_exc
            return None

        if exchange_instance.has.get('fetchDepositAddress'):
            data = await exchange_instance.fetch_deposit_address(ticker, params)
            extracted = self._extract_address_from_item(
                item=data,
                ticker=ticker,
                network_norm=network_norm,
            )
            if extracted:
                return extracted

        if exchange_instance.has.get('fetchDepositAddresses'):
            try:
                data = await exchange_instance.fetch_deposit_addresses([ticker], params)
            except TypeError:
                data = await exchange_instance.fetch_deposit_addresses(params=params)
            # some exchanges return dict keyed by currency, others return list
            if isinstance(data, dict):
                for _, entry in data.items():
                    extracted = self._extract_address_from_item(
                        item=entry,
                        ticker=ticker,
                        network_norm=network_norm,
                    )
                    if extracted:
                        return extracted
            elif isinstance(data, list):
                for entry in data:
                    extracted = self._extract_address_from_item(
                        item=entry,
                        ticker=ticker,
                        network_norm=network_norm,
                    )
                    if extracted:
                        return extracted

        return None

    async def _fetch_coinone_deposit_address_payload(
        self,
        exchange_instance: ccxt.Exchange,
        ticker: str,
    ) -> dict[str, Any] | None:
        try:
            data = await exchange_instance.fetch_deposit_addresses([ticker])
        except TypeError:
            data = await exchange_instance.fetch_deposit_addresses()

        if isinstance(data, dict):
            preferred = data.get(ticker.upper()) or data.get(ticker.lower())
            if preferred is not None:
                extracted = self._extract_address_from_item(
                    item=preferred,
                    ticker=ticker,
                    network_norm=None,
                )
                if extracted:
                    return extracted

            for _, entry in data.items():
                extracted = self._extract_address_from_item(
                    item=entry,
                    ticker=ticker,
                    network_norm=None,
                )
                if extracted:
                    return extracted
            return None

        if isinstance(data, list):
            for entry in data:
                extracted = self._extract_address_from_item(
                    item=entry,
                    ticker=ticker,
                    network_norm=None,
                )
                if extracted:
                    return extracted

        return None

    async def _fetch_upbit_deposit_address_payload(
        self,
        exchange_instance: ccxt.Exchange,
        ticker: str,
        deposit_network: str,
        network_norm: str,
    ) -> dict[str, Any] | None:
        params = {'network': deposit_network}

        try:
            data = await exchange_instance.fetch_deposit_address(ticker, params)
            extracted = self._extract_address_from_item(
                item=data,
                ticker=ticker,
                network_norm=network_norm,
            )
            if extracted:
                return extracted
        except Exception as exc:
            if (
                not self._is_upbit_address_not_ready(exc)
                and not self._is_upbit_invalid_parameter(exc)
            ):
                raise

        created = False
        net_type = self._resolve_upbit_net_type(exchange_instance, ticker, deposit_network)
        create_params_candidates: list[dict[str, Any]] = []
        if net_type:
            create_params_candidates.append({'net_type': net_type})
        create_params_candidates.append({})

        for create_params in create_params_candidates:
            try:
                await exchange_instance.create_deposit_address(ticker, create_params)
                created = True
                break
            except Exception as exc:
                if self._is_upbit_address_not_ready(exc):
                    # Upbit can return not_found/address-none while creation is in progress.
                    created = True
                    continue
                if create_params:
                    # Retry once without optional params when net_type-specific call fails.
                    continue
                raise

        if not created:
            return None

        for wait_sec in (0.0, 0.4, 1.0, 2.0):
            if wait_sec > 0:
                await asyncio.sleep(wait_sec)
            try:
                data = await exchange_instance.fetch_deposit_address(ticker, params)
            except Exception as exc:
                if self._is_upbit_address_not_ready(exc):
                    continue
                raise

            extracted = self._extract_address_from_item(
                item=data,
                ticker=ticker,
                network_norm=network_norm,
            )
            if extracted:
                return extracted

        return None

    @staticmethod
    def _resolve_upbit_net_type(
        exchange_instance: ccxt.Exchange,
        ticker: str,
        deposit_network: str,
    ) -> str:
        try:
            network_id = exchange_instance.network_code_to_id(deposit_network, ticker)
            text = str(network_id or '').strip()
            if text:
                return text
        except Exception:
            pass
        return str(deposit_network or '').strip()

    @staticmethod
    def _is_upbit_coin_address_not_found(exc: Exception) -> bool:
        text = str(exc).lower()
        return 'coin_address_not_found' in text

    @staticmethod
    def _is_upbit_address_none(exc: Exception) -> bool:
        text = str(exc).lower()
        return 'address is none' in text

    @staticmethod
    def _is_upbit_address_pending(exc: Exception) -> bool:
        text = str(exc).lower()
        return 'addresspending' in text or 'is generating' in text

    @staticmethod
    def _is_upbit_address_not_ready(exc: Exception) -> bool:
        return (
            WithdrawService._is_upbit_coin_address_not_found(exc)
            or WithdrawService._is_upbit_address_pending(exc)
            or WithdrawService._is_upbit_address_none(exc)
        )

    @staticmethod
    def _is_upbit_invalid_parameter(exc: Exception) -> bool:
        text = str(exc).lower()
        return 'invalid_parameter' in text

    @staticmethod
    def _resolve_bitget_chain(
        exchange_instance: ccxt.Exchange,
        ticker: str,
        deposit_network: str,
    ) -> str:
        network = str(deposit_network or '').strip()
        if not network:
            return ''

        try:
            mapped = str(exchange_instance.network_code_to_id(network, ticker) or '').strip()
        except Exception:
            mapped = ''
        if mapped:
            return mapped

        currencies = getattr(exchange_instance, 'currencies', None)
        if isinstance(currencies, dict):
            currency = currencies.get(ticker) if isinstance(currencies.get(ticker), dict) else None
            networks = currency.get('networks') if isinstance(currency, dict) else None
            target_norm = _norm_network(network)
            if isinstance(networks, dict):
                for key, net_data in networks.items():
                    key_text = str(key or '').strip()
                    net_id = ''
                    if isinstance(net_data, dict):
                        net_id = str(
                            net_data.get('id') or net_data.get('network') or ''
                        ).strip()
                    if _norm_network(key_text) == target_norm:
                        return net_id or key_text
                    if net_id and _norm_network(net_id) == target_norm:
                        return net_id

        return network

    @staticmethod
    def _build_bitget_chain_candidates(
        exchange_instance: ccxt.Exchange,
        ticker: str,
        deposit_network: str,
    ) -> list[str]:
        candidates: list[str] = []

        def _add(value: Any) -> None:
            text = str(value or '').strip()
            if text and text not in candidates:
                candidates.append(text)

        _add(
            WithdrawService._resolve_bitget_chain(
                exchange_instance=exchange_instance,
                ticker=ticker,
                deposit_network=deposit_network,
            )
        )
        _add(deposit_network)

        currencies = getattr(exchange_instance, 'currencies', None)
        target_norm = _norm_network(str(deposit_network or ''))
        if isinstance(currencies, dict):
            currency = currencies.get(ticker) if isinstance(currencies.get(ticker), dict) else None
            networks = currency.get('networks') if isinstance(currency, dict) else None
            if isinstance(networks, dict):
                for key, net_data in networks.items():
                    key_text = str(key or '').strip()
                    net_id = ''
                    if isinstance(net_data, dict):
                        net_id = str(
                            net_data.get('id') or net_data.get('network') or ''
                        ).strip()
                    if _norm_network(key_text) == target_norm:
                        _add(net_id)
                        _add(key_text)
                    elif net_id and _norm_network(net_id) == target_norm:
                        _add(net_id)

        if ticker.strip().upper() == 'TRX' and target_norm in {'TRX', 'TRC20'}:
            _add('TRX')
            _add('TRC20')

        return candidates

    @staticmethod
    def _extract_address_from_item(
        item: Any,
        ticker: str,
        network_norm: str | None,
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None

        code = str(item.get('currency') or item.get('code') or '').upper()
        if code and code != ticker.upper():
            return None

        network = str(item.get('network') or item.get('chain') or '').strip()
        if network and network_norm and _norm_network(network) != network_norm:
            return None

        info = item.get('info') if isinstance(item.get('info'), dict) else {}
        address = (
            item.get('address')
            or item.get('walletAddress')
            or item.get('wallet_address')
            or info.get('address')
            or info.get('wallet_address')
        )
        if not address:
            return None

        tag = (
            item.get('tag')
            or item.get('memo')
            or item.get('destinationTag')
            or item.get('destination_tag')
            or info.get('tag')
            or info.get('memo')
            or info.get('destination_tag')
        )

        return {
            'address': str(address).strip(),
            'tag': str(tag).strip() if tag else None,
            'raw': item,
        }

    @staticmethod
    async def _create_spot_instance(exchange_name: str) -> ccxt.Exchange | None:
        credentials = config.EXCHANGE_CREDENTIALS.get(exchange_name, {})
        api_key = str(credentials.get('apiKey', '')).strip()
        secret = str(credentials.get('secret', '')).strip()
        if not api_key or not secret:
            return None

        params = {
            **credentials,
            'options': {'defaultType': 'spot'},
            'enableRateLimit': True,
        }
        try:
            exchange_class = getattr(ccxt, exchange_name)
            instance = exchange_class(params)
            await instance.load_markets()
            return instance
        except Exception as exc:
            logger.error('Failed to create temporary %s spot instance: %s', exchange_name, exc)
            return None

