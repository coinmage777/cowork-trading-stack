"""
Predict.fun API Client for Arbitrage Bot
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, asdict

import aiohttp

PREDICT_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'predict_market_cache.json')

try:
    from predict_sdk import (
        OrderBuilder, ChainId, OrderBuilderOptions,
        Side as SDKSide, LimitHelperInput, BuildOrderInput
    )
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    logging.warning("predict-sdk not installed")

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    ETH_AVAILABLE = True
except ImportError:
    ETH_AVAILABLE = False
    logging.warning("eth-account not installed")


@dataclass
class Market:
    id: int
    question: str
    category: str
    yes_token_id: str
    no_token_id: str
    team_name: str = ""  # 팀명 (Houston, Atlanta 등)
    status: str = ""  # ACTIVE, RESOLVED, etc.
    yes_price: float = 0.0
    no_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    neg_risk: bool = False
    yield_bearing: bool = False
    fee_rate_bps: int = 200
    # Boost fields
    is_boosted: bool = False
    boost_starts_at: str = ""  # ISO 8601 timestamp
    boost_ends_at: str = ""    # ISO 8601 timestamp


@dataclass
class Position:
    market_id: int
    token_id: str
    side: str  # YES or NO
    shares: float
    avg_price: float


class PredictClient:
    """Predict.fun API Client"""

    def __init__(
        self,
        api_key: str,
        private_key: str,
        predict_account: str = "",
        base_url: str = "https://api.predict.fun",
    ):
        self.api_key = api_key
        self.private_key = self._normalize_key(private_key)
        self.predict_account = predict_account
        self.base_url = base_url.rstrip('/')

        self.session: Optional[aiohttp.ClientSession] = None
        self.jwt_token: Optional[str] = None
        self.order_builder = None
        self.signer_address: str = ""
        self.jwt_consecutive_failures: int = 0
        self.jwt_alert_needed: bool = False

        # 마켓 캐시
        self._cache = self._load_cache()

    def _load_cache(self) -> Dict:
        """캐시 파일 로드"""
        try:
            if os.path.exists(PREDICT_CACHE_FILE):
                with open(PREDICT_CACHE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logging.warning(f"[Predict] Cache load error: {e}")
        return {}

    def _save_cache(self):
        """캐시 파일 저장"""
        try:
            with open(PREDICT_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, indent=2)
        except Exception as e:
            logging.warning(f"[Predict] Cache save error: {e}")

    def _normalize_key(self, pk: str) -> str:
        if not pk:
            return ""
        pk = pk.strip()
        if pk.startswith("0x"):
            pk = pk[2:]
        return pk if len(pk) == 64 else ""

    async def connect(self):
        """Initialize session and SDK"""
        if not self.session:
            headers = {"Content-Type": "application/json", "x-api-key": self.api_key}
            self.session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30))

        # Initialize SDK (only once)
        if SDK_AVAILABLE and self.private_key and not self.order_builder:
            try:
                if ETH_AVAILABLE:
                    signer = Account.from_key(self.private_key)
                    self.signer_address = signer.address
                    logging.info(f"[Predict] Signer: {self.signer_address}")

                # Create options with predict_account if provided
                options = None
                if self.predict_account:
                    options = OrderBuilderOptions(predict_account=self.predict_account)
                    logging.info(f"[Predict] Predict Account: {self.predict_account[:10]}...")

                # Use OrderBuilder.make() with BNB_MAINNET (chain 56)
                self.order_builder = OrderBuilder.make(ChainId.BNB_MAINNET, self.private_key, options)
                logging.info("[Predict] SDK initialized")
            except Exception as e:
                logging.error(f"[Predict] SDK init error: {e}")

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def get_jwt_token(self) -> Optional[str]:
        """Get JWT token for authenticated requests"""
        if self.jwt_token:
            return self.jwt_token

        if not self.private_key:
            return None

        try:
            # Get message to sign
            async with self.session.get(f"{self.base_url}/v1/auth/message") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                message = data.get("data", {}).get("message")

            signature = None
            signer_address = None

            # Try to sign with predict_account first (if using Privy)
            if self.predict_account and self.order_builder:
                try:
                    if hasattr(self.order_builder, 'sign_predict_account_message'):
                        signature = self.order_builder.sign_predict_account_message(message)
                        signer_address = self.predict_account
                        logging.info("[Predict] Using predict_account for JWT")
                except Exception as e:
                    logging.warning(f"[Predict] predict_account sign failed: {e}")

            # Fallback to direct signing
            if not signature and ETH_AVAILABLE:
                signer = Account.from_key(self.private_key)
                signable = encode_defunct(text=message)
                signed = signer.sign_message(signable)
                signature = signed.signature.hex()
                if not signature.startswith("0x"):
                    signature = "0x" + signature
                signer_address = signer.address

            if not signature or not signer_address:
                logging.error("[Predict] Failed to sign JWT message")
                return None

            # Get JWT
            body = {"signer": signer_address, "message": message, "signature": signature}
            async with self.session.post(f"{self.base_url}/v1/auth", json=body) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.jwt_token = data.get("data", {}).get("token")
                    logging.info("[Predict] JWT obtained")
                    return self.jwt_token
                else:
                    text = await resp.text()
                    logging.error(f"[Predict] JWT auth failed: {resp.status} - {text[:200]}")

        except Exception as e:
            logging.error(f"[Predict] JWT error: {e}")

        return None

    async def _request(self, method: str, endpoint: str, require_jwt: bool = False, **kwargs) -> Optional[Dict]:
        """Make API request with retry logic"""
        await self.connect()

        if not endpoint.startswith("/v1"):
            endpoint = f"/v1{endpoint}"

        url = f"{self.base_url}{endpoint}"

        for attempt in range(3):
            headers = dict(self.session.headers)

            if require_jwt:
                token = await self.get_jwt_token()
                if token:
                    headers["Authorization"] = f"Bearer {token}"

            try:
                await asyncio.sleep(0.5)  # Rate limit
                async with self.session.request(method, url, headers=headers, **kwargs) as resp:
                    text = await resp.text()

                    if resp.status in [200, 201]:
                        self.jwt_consecutive_failures = 0
                        return json.loads(text)
                    elif resp.status == 401:
                        # JWT expired - force refresh and retry
                        self.jwt_consecutive_failures += 1
                        if self.jwt_consecutive_failures >= 5 and not self.jwt_alert_needed:
                            self.jwt_alert_needed = True
                            logging.critical(f"[Predict] JWT failed {self.jwt_consecutive_failures} times consecutively!")
                        logging.warning(f"[Predict] 401 Unauthorized, refreshing JWT...")
                        self.jwt_token = None
                        if require_jwt:
                            await self.get_jwt_token()
                        continue
                    elif resp.status in [429, 500]:
                        # Rate limit or server error - wait and retry
                        logging.warning(f"[Predict] {resp.status}, retrying...")
                        await asyncio.sleep(2)
                        continue
                    else:
                        logging.error(f"[Predict] {method} {endpoint} -> {resp.status}: {text[:200]}")
                        return {"success": False, "status": resp.status, "message": text[:200]}

            except Exception as e:
                logging.error(f"[Predict] Request error: {e}")
                await asyncio.sleep(1)

        return None

    def _parse_teams_from_question(self, question: str) -> List[str]:
        """질문에서 팀명 추출

        예: "Houston at Atlanta Winner?" → ["Houston", "Atlanta"]
        예: "Rockets vs Hawks Winner?" → ["Rockets", "Hawks"]
        """
        import re

        # "X at Y Winner?" 또는 "X vs Y Winner?" 패턴
        patterns = [
            r'^(.+?)\s+at\s+(.+?)\s+Winner',
            r'^(.+?)\s+vs\.?\s+(.+?)\s+Winner',
            r'^(.+?)\s+@\s+(.+?)\s+Winner',
        ]

        for pattern in patterns:
            match = re.match(pattern, question, re.IGNORECASE)
            if match:
                return [match.group(1).strip(), match.group(2).strip()]

        return []

    async def get_markets_by_category(self, categories: List[str]) -> List[Market]:
        """Get markets by category slugs (캐시 사용)"""
        today = datetime.now().strftime('%Y-%m-%d')
        result = []

        # 캐시 확인 - 모든 카테고리가 캐시에 있는지 확인
        all_cached = True
        for cat in categories:
            if cat not in self._cache or self._cache[cat].get('date') != today:
                all_cached = False
                break

        if all_cached:
            # 캐시에서 로드
            logging.info("[Predict] 캐시에서 마켓 로드")
            for cat in categories:
                cached = self._cache[cat]
                for m in cached.get('markets', []):
                    result.append(Market(
                        id=m['id'],
                        question=m['question'],
                        category=cat,
                        yes_token_id=m['yes_token_id'],
                        no_token_id=m['no_token_id'],
                        team_name=m.get('team_name', ''),
                        neg_risk=m.get('neg_risk', False),
                        yield_bearing=m.get('yield_bearing', False),
                        fee_rate_bps=int(m.get('fee_rate_bps', 200) or 200),
                    ))
            return result

        # 캐시 없음 - API에서 로드
        logging.info("[Predict] API에서 마켓 로드")
        all_markets = []
        cursor = None

        while True:
            params = {'first': '100'}
            if cursor:
                params['after'] = cursor

            data = await self._request("GET", "/markets", params=params)
            if not data or not data.get('data'):
                break

            markets = data.get('data', [])
            all_markets.extend(markets)

            cursor = data.get('cursor')
            if not cursor or len(markets) < 100:
                break

            await asyncio.sleep(0.2)

        # Filter by categories
        for m in all_markets:
            cat = m.get('categorySlug', '')
            if cat in categories and m.get('status') != 'RESOLVED':
                outcomes = m.get('outcomes', [])
                yes_token = ""
                no_token = ""

                for o in outcomes:
                    name = (o.get('name', '') or '')
                    name_upper = name.upper()
                    token = str(o.get('onChainId', '') or '')

                    if name_upper in ('YES', 'UP'):
                        yes_token = token
                    elif name_upper in ('NO', 'DOWN'):
                        no_token = token
                    else:
                        # YES/NO가 아니면 순서대로
                        if not yes_token:
                            yes_token = token
                        elif not no_token:
                            no_token = token

                if yes_token and no_token:
                    result.append(Market(
                        id=m.get('id'),
                        question=m.get('question', ''),
                        category=cat,
                        yes_token_id=yes_token,
                        no_token_id=no_token,
                        team_name="",  # 나중에 ID 순서로 할당
                        neg_risk=m.get('isNegRisk', False),
                        yield_bearing=m.get('isYieldBearing', False),
                        fee_rate_bps=int(m.get('feeRateBps', 200) or 200),
                    ))

        # 같은 question을 가진 마켓들을 ID 순서로 팀명 할당
        from collections import defaultdict
        question_groups = defaultdict(list)
        for m in result:
            question_groups[m.question].append(m)

        for question, markets in question_groups.items():
            teams = self._parse_teams_from_question(question)
            if len(teams) >= 2 and len(markets) >= 2:
                markets.sort(key=lambda x: x.id)
                markets[0].team_name = teams[0]
                markets[1].team_name = teams[1]
                logging.info(f"[Predict] 팀 매칭: #{markets[0].id}={teams[0]}, #{markets[1].id}={teams[1]}")

        # 캐시에 저장
        for cat in categories:
            cat_markets = [m for m in result if m.category == cat]
            self._cache[cat] = {
                'date': today,
                'markets': [
                    {
                        'id': m.id,
                        'question': m.question,
                        'yes_token_id': m.yes_token_id,
                        'no_token_id': m.no_token_id,
                        'team_name': m.team_name,
                        'neg_risk': m.neg_risk,
                        'yield_bearing': m.yield_bearing,
                        'fee_rate_bps': m.fee_rate_bps,
                    }
                    for m in cat_markets
                ]
            }
        self._save_cache()

        return result

    async def get_all_markets(self) -> List[Market]:
        """Get all markets from API (no filtering)"""
        logging.info("[Predict] Fetching all markets...")
        all_markets = []
        cursor = None

        while True:
            params = {'first': '100'}
            if cursor:
                params['after'] = cursor

            data = await self._request("GET", "/markets", params=params)
            if not data or not data.get('data'):
                break

            markets = data.get('data', [])
            all_markets.extend(markets)

            cursor = data.get('cursor')
            if not cursor or len(markets) < 100:
                break

            await asyncio.sleep(0.2)

        logging.info(f"[Predict] Loaded {len(all_markets)} markets total")

        # Convert to Market objects
        result = []
        for m in all_markets:
            cat = m.get('categorySlug', '')
            status = m.get('status', '')

            if status == 'RESOLVED':
                continue

            outcomes = m.get('outcomes', [])
            yes_token = ""
            no_token = ""

            for o in outcomes:
                name = (o.get('name', '') or '')
                name_upper = name.upper()
                token = str(o.get('onChainId', '') or '')

                if name_upper in ('YES', 'UP'):
                    yes_token = token
                elif name_upper in ('NO', 'DOWN'):
                    no_token = token
                else:
                    if not yes_token:
                        yes_token = token
                    elif not no_token:
                        no_token = token

            if yes_token and no_token:
                result.append(Market(
                    id=m.get('id'),
                    question=m.get('question', ''),
                    category=cat,
                    yes_token_id=yes_token,
                    no_token_id=no_token,
                    team_name="",
                    status=status,
                    neg_risk=m.get('isNegRisk', m.get('negRisk', False)),
                    yield_bearing=m.get('isYieldBearing', m.get('yieldBearing', False)),
                    fee_rate_bps=int(m.get('feeRateBps', 200) or 200),
                    is_boosted=m.get('isBoosted', False),
                    boost_starts_at=m.get('boostStartsAt') or "",
                    boost_ends_at=m.get('boostEndsAt') or "",
                ))
            else:
                # Log dropped markets for Super Bowl / multi-outcome debugging
                if cat and (cat.startswith('super-bowl') or cat.startswith('first-song')):
                    outcome_names = [o.get('name', '') for o in outcomes]
                    logging.warning(f"[Predict] DROPPED #{m.get('id')} ({cat}): no yes/no tokens. "
                                    f"outcomes={outcome_names}, status={status}, "
                                    f"yes_token={bool(yes_token)}, no_token={bool(no_token)}")

        return result

    async def get_orderbook(self, market_id: int) -> Tuple[float, float, float, float]:
        """Get orderbook prices: (best_bid, best_ask, bid_size, ask_size)"""
        data = await self._request("GET", f"/markets/{market_id}/orderbook")
        if not data or not data.get('success'):
            return 0, 0, 0, 0

        ob = data.get('data', {})
        bids = ob.get('bids', [])
        asks = ob.get('asks', [])

        best_bid = float(bids[0][0]) if bids else 0
        best_ask = float(asks[0][0]) if asks else 0
        bid_size = float(bids[0][1]) if bids else 0
        ask_size = float(asks[0][1]) if asks else 0

        return best_bid, best_ask, bid_size, ask_size

    async def get_positions(self) -> List[Position]:
        """Get current positions"""
        data = await self._request("GET", "/positions", require_jwt=True)
        if not data or not data.get('success'):
            logging.warning(f"[Predict] get_positions failed: {data}")
            return []

        positions = []
        for p in data.get('data', []):
            # market_id: market.id에서 추출
            market_obj = p.get('market', {}) or {}
            market_id = market_obj.get('id')

            # token_id: outcome.onChainId에서 추출
            outcome_obj = p.get('outcome', {}) or {}
            token_id = outcome_obj.get('onChainId', '')

            # amount는 wei 단위 (1e18으로 나눠야 함)
            amount_wei = p.get('amount', '0')
            try:
                shares = float(amount_wei) / 1e18
            except:
                shares = 0

            # side: outcome.name에서 추출 (Yes/No)
            side = outcome_obj.get('name', '')

            logging.debug(f"[Position] market={market_id}, token={token_id[:20] if token_id else 'N/A'}..., shares={shares:.2f}, side={side}")

            positions.append(Position(
                market_id=market_id,
                token_id=token_id,
                side=side,
                shares=shares,
                avg_price=0,  # API에서 제공 안함
            ))

        return positions

    async def get_open_orders(self) -> List[Dict]:
        """Get ALL open orders (with pagination to avoid default limit)"""
        all_orders = []
        offset = 0
        limit = 100

        while True:
            data = await self._request(
                "GET",
                f"/orders?status=OPEN&limit={limit}&offset={offset}",
                require_jwt=True
            )
            if not data or not data.get('success'):
                break

            batch = data.get('data', [])
            if not batch:
                break

            all_orders.extend(batch)

            # If we got fewer than limit, we've reached the end
            if len(batch) < limit:
                break

            offset += limit

        return all_orders

    async def get_order(self, order_id: str) -> Optional[Dict]:
        """Get order details by order ID"""
        data = await self._request("GET", f"/orders/{order_id}", require_jwt=True)
        if not data or not data.get('success'):
            return None
        return data.get('data')

    async def get_order_fill_price(self, order_id: str) -> Optional[float]:
        """Get average fill price for an order"""
        order = await self.get_order(order_id)
        if not order:
            return None
        # Try different field names for fill price
        fill_price = order.get('avgFillPrice') or order.get('fillPrice') or order.get('averagePrice')
        if fill_price:
            return float(fill_price)
        return None

    async def place_order(
        self,
        market_id: int,
        token_id: str,
        side: str,  # "BUY" or "SELL"
        price: float,
        shares: int,
        neg_risk: bool = False,
        yield_bearing: bool = False,
        fee_rate_bps: int = 200,
    ) -> Optional[Dict]:
        """Place a limit order"""
        if not self.order_builder:
            logging.error("[Predict] OrderBuilder not initialized")
            return None

        if not token_id:
            logging.warning(f"[Predict] Empty token_id for market={market_id}, skipping order")
            return None

        try:
            # Convert to SDK side enum
            sdk_side = SDKSide.BUY if side.upper() == "BUY" else SDKSide.SELL

            # Round price to 2 decimal places (API requirement)
            # float(f"...") 방식으로 정확한 2자리 보장
            price = float(f"{price:.2f}")

            # Convert to wei (1e18) - 정수 연산으로 정밀도 보장
            price_cents = int(round(price * 100))  # 0.57 → 57
            price_wei = price_cents * 10**16  # 57 * 1e16 = 5.7e17
            quantity_wei = int(shares * 1e18)

            # Calculate order amounts using LimitHelperInput
            limit_input = LimitHelperInput(
                side=sdk_side,
                price_per_share_wei=price_wei,
                quantity_wei=quantity_wei,
            )
            amounts = self.order_builder.get_limit_order_amounts(limit_input)

            # Build order input
            order_input = BuildOrderInput(
                side=sdk_side,
                token_id=str(token_id),
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=fee_rate_bps,
            )

            # Build order
            order = self.order_builder.build_order("LIMIT", order_input)

            # Build typed data and sign
            typed_data = self.order_builder.build_typed_data(order, is_neg_risk=neg_risk, is_yield_bearing=yield_bearing)
            signed_order = self.order_builder.sign_typed_data_order(typed_data)
            order_hash = self.order_builder.build_typed_data_hash(typed_data)

            # Get signature
            sig = signed_order.signature
            if not sig.startswith("0x"):
                sig = "0x" + sig

            # Get signature type
            sig_type = order.signature_type
            if hasattr(sig_type, 'value'):
                sig_type = int(sig_type.value)
            elif sig_type is not None:
                sig_type = int(sig_type)
            else:
                sig_type = 0

            # Convert order to dict for API submission
            order_dict = {
                "hash": order_hash,
                "salt": str(order.salt),
                "maker": str(order.maker),
                "signer": str(order.signer),
                "taker": str(order.taker) if order.taker else "<EVM_ADDRESS>",
                "tokenId": str(order.token_id),
                "makerAmount": str(order.maker_amount),
                "takerAmount": str(order.taker_amount),
                "expiration": str(order.expiration),
                "nonce": str(order.nonce),
                "feeRateBps": str(order.fee_rate_bps),
                "side": int(sdk_side.value),
                "signatureType": int(sig_type),
                "signature": str(sig),
            }

            # Build payload
            order_payload = {
                "data": {
                    "order": order_dict,
                    "pricePerShare": str(amounts.price_per_share),
                    "strategy": "LIMIT",
                }
            }

            # Submit order
            data = await self._request(
                "POST",
                "/orders",
                json=order_payload,
                require_jwt=True
            )

            if data and data.get('success'):
                logging.info(f"[Predict] Order placed: market={market_id}, {side} {shares}@{price}")
                return data.get('data')
            else:
                # min_order_value 에러는 WARNING으로 (스팸 방지)
                msg_str = str(data.get('message', ''))
                if 'min_order_value' in msg_str:
                    logging.warning(f"[Predict] Order rejected (min value): market={market_id}, {shares}@{price}")
                else:
                    logging.error(f"[Predict] Order failed: {data}")

        except Exception as e:
            logging.error(f"[Predict] Place order error: {e}")
            import traceback
            traceback.print_exc()

        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        if not order_id:
            return False

        # Try different payload formats (endpoint is /orders/remove, not /orders/cancel)
        payloads = [
            {"data": {"ids": [order_id]}},
            {"ids": [order_id]},
            {"data": {"orderHashes": [order_id]}},
            {"orderHashes": [order_id]},
        ]

        for payload in payloads:
            data = await self._request(
                "POST",
                "/orders/remove",
                json=payload,
                require_jwt=True
            )
            if data and data.get('success', False):
                logging.info(f"[Predict] Cancelled order: {order_id[:16]}...")
                return True

        return False

    async def cancel_orders(self, order_ids: List[str]) -> bool:
        """Cancel multiple orders at once"""
        if not order_ids:
            return True

        # Try different payload formats
        payloads = [
            {"data": {"ids": order_ids}},
            {"ids": order_ids},
            {"data": {"orderHashes": order_ids}},
            {"orderHashes": order_ids},
        ]

        for payload in payloads:
            data = await self._request(
                "POST",
                "/orders/remove",
                json=payload,
                require_jwt=True
            )
            if data and data.get('success', False):
                logging.info(f"[Predict] Cancelled {len(order_ids)} orders")
                return True

        return False

    async def get_balance(self) -> float:
        """Get USDC balance"""
        if not self.order_builder:
            return -1

        try:
            target = self.predict_account if self.predict_account else self.signer_address
            balance_wei = await self.order_builder.balance_of_async("USDT", target)
            return float(balance_wei) / 1e18
        except Exception as e:
            logging.error(f"[Predict] Balance error: {e}")
            return -1
