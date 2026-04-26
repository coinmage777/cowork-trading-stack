# 07. Kimchi Premium + Cross-Venue Arb

**Kimchi Premium** — the phenomenon where BTC (and other coins) trade at a higher price on Korean exchanges (Upbit, Bithumb, Coinone) than on global exchanges (Binance, OKX) on a USD-equivalent basis.

This chapter covers automating that gap, and the broader cross-venue arb pattern.

## What is kimchi premium

Mechanism:
- South Korea has capital controls; freely moving USDT/USD in and out is hard
- Korean buy demand > supply → KRW-denominated price exceeds USD-equivalent
- Typically +1–3%, +10–20% in overheated markets

### How to compute it

```
Kimchi % = (Korean KRW price / (Global USD price × USD/KRW)) - 1) × 100
```

Example:
- Upbit BTC: 100,000,000 KRW
- Binance BTC: 70,000 USD
- USD/KRW: 1380
- → Kimchi = (100,000,000 / (70,000 × 1380) - 1) × 100 = +3.5%

Sites like [kimpga.com](https://kimpga.com) or [coinview.io](https://coinview.io) show this in real time.

## Two directions

### 1) Premium high — flow into Korea

- Buy coin with USDT on global exchange
- Transfer to Korean exchange
- Sell for KRW (sells at the premium)
- Need to convert KRW back to USDT to exit — **this is the hard part**

**Problem**: Korean exchanges don't let you buy/withdraw USD/USDT directly. KRW exit channels are limited.

Traditional methods:
- Buy a different coin with KRW → transfer out → sell on global (loss if reverse premium)
- OTC / P2P → currency exchange (fees + risk)

### 2) Premium low (reverse) — flow out of Korea

- Pre-stockpile coin in Korea (when it's cheap)
- Sell for KRW (at normal premium time)
- Profit

This requires a long time horizon and reverse-premium windows are rare and short.

### My realistic take

Pure kimchi arb is hard. The reason is **asymmetric capital flow** — leaving Korea is hard, which is exactly why the gap survives. You face the same friction.

So the more practical form for me is:

## Cross-venue arb — the general pattern

Kimchi is a special case of cross-venue arb. More generally:

- Exchange A price ≠ Exchange B price
- Capital pre-distributed to both (no transfer required)
- Buy A + sell B (or short A + long B)
- Hold until convergence

### What price gaps can you target

1. **CEX vs CEX** — rare but appear in big moves (e.g., Binance -1% / OKX +0.5%)
2. **CEX vs DEX** — [Hyperliquid](https://miracletrade.com/?ref=coinmage) / dYdX vs Binance during liquidation cascades
3. **Spot vs Perp basis** — buy spot + short perp
4. **Funding-rate differential** — same asset, different funding on two exchanges

### Funding arbitrage — what I run most

Same asset, different funding by exchange. Example:
- Binance BTC perp funding: +0.05% (longs pay shorts)
- Hyperliquid BTC perp funding: -0.02% (shorts pay longs)
- Differential: 0.07% / 8h = 0.21% / day

Trade:
- Binance BTC short (collects +0.05%)
- Hyperliquid BTC long (collects +0.02%)
- Price exposure ≈ 0
- Funding accrues: 0.21% × 365 = ~76% APR (theoretical)

### Risks of funding arb

- **Funding is not fixed** — flips next round, immediate loss
- **Prices don't track tightly** — basis-driven exposure on top
- **Exchange risk** — one exchange halts withdrawals or liquidates
- **Capital lock-up** — both legs tie up capital → factor cost-of-capital into ROI

Minimum I require:
- Same asset, |funding diff| > 0.03% / 8h sustained 6+ hours
- Exchange stability verified
- High-beta-1 asset (BTC, ETH)
- Size ≤ 30–40% of capital (impact + liq buffer)

### CEX vs DEX basis on big moves

During large liquidation cascades (e.g., BTC -5%):
- DEXs (Hyperliquid) have liquidation indexes from different price feeds → temporary overshoot
- CEXs (Binance) more stable

→ Buy DEX + sell CEX → close on index convergence.

To automate:
- Both exchanges' mark price via WebSocket
- Threshold trigger (e.g., 0.5% gap)
- Exit on recovery (≤ 0.1%)
- Sufficient liquidity on both sides

I've only paper-tested this pattern. Big moves are rare and synchronized two-sided fills are tough.

## Real-world kimchi automation

For genuinely automated kimchi arb, two operators is more efficient:
- **Global side**: USDT → coin → transfer to Korea
- **Korean side**: receive coin → sell for KRW → hold KRW for next round

Solo, the KRW → USDT conversion becomes the bottleneck.

### What I actually run

I don't run a full kimchi bot. Instead, I have an **alert when premium is unusually high** → manual judgment to enter. Big premiums are rare enough that the ROI on automation isn't justified.

Alert bot (simple):

```python
async def kimchi_monitor():
    while True:
        upbit_btc = await fetch_upbit("KRW-BTC")
        binance_btc = await fetch_binance("BTCUSDT")
        usdkrw = await fetch_fx_rate()
        
        kimchi = (upbit_btc / (binance_btc * usdkrw) - 1) * 100
        
        if kimchi > 5:  # alert if 5%+
            await notify(f"🚨 Kimchi {kimchi:.2f}% — manual review")
        
        await asyncio.sleep(60)
```

That's about as far as my kimchi automation goes pragmatically.

## More practical: funding monitor

What I actually run more often is a funding-rate monitor:

```python
async def funding_monitor():
    exchanges = ["binance", "bybit", "okx", "hyperliquid", "lighter"]
    while True:
        rows = []
        for ex in exchanges:
            for sym in ["BTC", "ETH", "SOL"]:
                fr = await get_funding_rate(ex, sym)
                rows.append({"ex": ex, "sym": sym, "fr": fr})
        
        for sym in ["BTC", "ETH", "SOL"]:
            sym_rates = [r for r in rows if r["sym"] == sym]
            mx = max(sym_rates, key=lambda x: x["fr"])
            mn = min(sym_rates, key=lambda x: x["fr"])
            diff = mx["fr"] - mn["fr"]
            if diff > 0.01:
                await notify(f"{sym}: {mx['ex']}({mx['fr']:.4f}) vs {mn['ex']}({mn['fr']:.4f})")
        
        await asyncio.sleep(300)
```

Polled every 5 minutes, this surfaces funding-arb candidates. Whether to enter is my call.

## Next chapter

Next: backtesting. How to validate strategies before live deployment, using a Minara-style workflow.
