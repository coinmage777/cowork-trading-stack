# 10. Gold Cross-Exchange Arb

The arb pattern from my collection most worth generalizing — when a tokenized asset, or the same asset on different venues, trades at different prices.

The "Gold" name comes from where I first applied this — tokenized gold (PAXG, XAUT, etc.). The same pattern applies to other assets.

## The tokenized gold market

Gold tokenized in crypto:
- **PAXG** (Paxos Gold) — 1 PAXG = 1 troy ounce
- **XAUT** (Tether Gold) — same idea
- **TradFi gold ETFs** (GLD, IAU) — not crypto, but useful for price comparison
- **Futures markets** — COMEX etc.

### The opportunity

In theory all of these represent the same asset (1 oz gold), but:
- Prices differ across venues (liquidity / demand)
- Crypto vs TradFi time gaps (TradFi closes weekends)
- Funding / carry cost differentials

The **tradable** subset of those gaps is what an automated bot captures.

## My general pattern

### Pattern 1: same asset across venues

Same asset A is $100 on exchange X but $99 on exchange Y.

Trade:
- Buy on Y, sell on X
- Hold until convergence
- Close

Conditions:
- Capital pre-distributed to both (no transfers needed)
- Fees + slippage < the gap
- Both have enough depth

### Pattern 2: basis arb (spot vs perp)

Same asset:
- Spot: $100
- Perp: $101 (positive funding, longs paying)

Trade:
- Buy spot + short perp
- Collect funding + close at convergence

Near-riskless when funding stays positive. Carry trade.

Risks:
- Funding flips negative
- Perp exchange halts withdrawals or liquidations
- Capital lock-up (no instant unwind)

### Pattern 3: time gap (TradFi vs crypto)

When TradFi is closed (weekends, US holidays):
- TradFi price frozen at Friday close
- Crypto price keeps moving → gap widens
- Monday open often closes the gap

Trade:
- Track crypto over weekend
- Enter just before Monday open if gap is large
- Close when gap fills

Backtest carefully. TradFi gaps don't always close (big news = no fill).

### Pattern 4: tokenized vs underlying

PAXG (tokenized gold) vs LBMA gold price:
- Typical gap ±0.5%
- Occasionally widens to +2% (crypto market dislocates)
- Mean-reversion bet

But LBMA isn't 24/7 — limits comparison data.

## Real automation — code structure

```python
class CrossExchangeArb:
    def __init__(self, asset: str, exchanges: list[ExchangeAdapter], 
                 entry_spread_bps: float = 30, exit_spread_bps: float = 5):
        self.asset = asset
        self.exchanges = exchanges
        self.entry_spread_bps = entry_spread_bps
        self.exit_spread_bps = exit_spread_bps
        self.position = None

    async def fetch_prices(self) -> dict:
        tasks = [ex.get_mark_price(self.asset) for ex in self.exchanges]
        prices = await asyncio.gather(*tasks, return_exceptions=True)
        return {ex.name: p for ex, p in zip(self.exchanges, prices) if not isinstance(p, Exception)}

    async def detect_opportunity(self, prices: dict):
        if len(prices) < 2:
            return None
        sorted_px = sorted(prices.items(), key=lambda x: x[1])
        cheap_ex, cheap_px = sorted_px[0]
        expensive_ex, expensive_px = sorted_px[-1]
        spread_bps = (expensive_px - cheap_px) / cheap_px * 10000
        if spread_bps >= self.entry_spread_bps:
            return {"long_ex": cheap_ex, "short_ex": expensive_ex, 
                    "long_px": cheap_px, "short_px": expensive_px, "spread_bps": spread_bps}
        return None

    async def open(self, opp):
        long_task = self._exchange(opp["long_ex"]).create_order(
            self.asset, "buy", self.size, "limit", price=opp["long_px"])
        short_task = self._exchange(opp["short_ex"]).create_order(
            self.asset, "sell", self.size, "limit", price=opp["short_px"])
        long_order, short_order = await asyncio.gather(long_task, short_task)
        # confirm fills + handle one-side fill
        # ...
        self.position = {...}

    async def check_exit(self, prices: dict):
        if not self.position:
            return False
        long_px = prices[self.position["long_ex"]]
        short_px = prices[self.position["short_ex"]]
        spread_bps = (short_px - long_px) / long_px * 10000
        return spread_bps <= self.exit_spread_bps

    async def close(self):
        long_task = self._exchange(self.position["long_ex"]).close_position(self.asset)
        short_task = self._exchange(self.position["short_ex"]).close_position(self.asset)
        await asyncio.gather(long_task, short_task)
        # PnL calculation + logging
        self.position = None

    async def run(self):
        while True:
            try:
                prices = await self.fetch_prices()
                if self.position is None:
                    opp = await self.detect_opportunity(prices)
                    if opp:
                        await self.open(opp)
                else:
                    if await self.check_exit(prices):
                        await self.close()
            except Exception as e:
                await notify(f"[arb] error: {e}")
            await asyncio.sleep(5)
```

## Critical details

### 1) Two-sided simultaneous fill

The biggest risk in this kind of arb: **only one side fills**. Sudden directional exposure.

Mitigations:
- IOC (Immediate-or-Cancel) orders — auto-cancel if not matched
- After one side fills, if the other hasn't filled within N seconds → market-close the filled side
- Maker/taker split: one side maker (slow OK), other taker (immediate)

### 2) Real executable price

Mid price isn't the truth. Walking the book has slippage.

```python
def estimate_execution_price(order_book, side, qty):
    levels = order_book["asks"] if side == "buy" else order_book["bids"]
    remaining = qty
    total_cost = 0
    for px, sz in levels:
        take = min(sz, remaining)
        total_cost += take * px
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        return None  # not enough liquidity
    return total_cost / qty
```

### 3) Capital allocation

How to split capital across N exchanges? My rules:
- At least 3–5x minimum size at each
- Pair structure means one leg negative is offset by the other → liquidation risk modest
- But if one exchange halts withdrawals, big problem → don't concentrate
- My rule: max 25% of total capital at any one exchange

### 4) Exchange trust tiers

Newly launched venues carry withdrawal-halt / hack risk. My tiers:
- **Tier 1** (trusted): Binance, OKX, Bybit, [Hyperliquid](https://miracletrade.com/?ref=coinmage), Coinbase
- **Tier 2**: well-known DEXs (Lighter, dYdX, GMX)
- **Tier 3**: new DEXs, small exchanges

For Tier 3, keep arb size small (≤ 5% of capital).

### 5) Monitoring

Once entered, hold until convergence. During hold, anomaly detection matters:
- One exchange's price spikes → funding could surge
- One exchange goes down → can't close
- Total market vol spikes → temporary exposure risk

→ check both mark prices + funding every 30 seconds, alert on threshold breach.

## What I've actually run

### Successes (intermittent)

- BTC basis arb: Hyperliquid vs Binance, carry trade when funding positive. Steady.
- ETH funding arb: when 0.05% funding gap appears between exchanges.

### Failures (lessons)

- Small-exchange arb: cancel-after-fill on one side → ghost. Keep size tiny.
- TradFi time-gap betting: doesn't fill on big news. Backtest was over-generalized.
- Tokenized gold arb: liquidity too shallow for practical size.

## Conclusion

Cross-exchange arb is appealing but trades off capital efficiency / operational complexity / exchange risk. I don't run it as a main strategy — only as a side track.

**My main strategy remains pair trading (BTC vs ETH within one exchange) for capital efficiency and operational simplicity.**

For arb, my best practice is alerting on rare windows of dislocation and entering manually.

## Next chapter

Next: operational infrastructure + principles. VPS, monitoring, logging, backups, restarts, security — what keeps the bot running 24/7.
