# 09. [Polymarket](https://polymarket.com/?ref=coinmage) Bot

A bot track separate from Perp DEXs. Auto-trading on prediction markets.

## What is Polymarket

- **Polygon-based prediction market**
- Binary outcome markets: "will event X happen?" → YES / NO tokens
- Price = probability (0–1 USD)
- On event resolution, the winning token resolves to 1 USD, the other to 0
- Ranges from massive markets (e.g., presidential elections) to small ones ("ETH price this week")

### Polymarket data quirks

- **CLOB (central limit order book)** — on Polygon, but matched off-chain, settled on-chain
- **No fees** (only USDC swap costs + small gas). 
- **Liquidity wildly variable** — Trump market deep, niche markets shallow

### Bot tracks

1. **expiry_snipe** — buy tokens whose price drops near expiry
2. **hedge_arb** — when YES + NO sums to < 0.99 (or via cross-venue), capture the gap
3. **weather** — weather markets only (currently disabled)
4. **predict_snipe** — [Predict.fun](https://predict.fun/?ref=coinmage) (similar BSC-based prediction market)

## Why prediction markets are attractive

- **Inefficient market** — Polymarket has crypto traders + political bettors mixed; interesting dynamics
- **Clear outcomes** — price resolves to 0 or 1 (at expiry)
- **No liquidation, no funding** — hold indefinitely
- **Arb opportunities** — gaps with other prediction markets (Predict.fun, Kalshi)

## Why it's hard (failure cases)

A Polymarket subaccount once dropped roughly 60% in eight days. Lessons learned:

### 1) Adverse selection

- A limit buy is placed at mid-price
- For the order to fill, someone must sell at that price
- Sellers are bots / informationally advantaged participants
- → Fills happen on losing trades

Data: 14% fill rate, 29% WR on fills. The fill itself is a negative signal.

**Lesson**: directional limit-order betting without market-making or info edge is essentially impossible.

### 2) Polymarket order book structure trap

- `/book` API shows bid 0.01–0.10 / ask 0.90–0.99 only (because of complementary token matching)
- Real mid is around 0.50
- "ask" looks like 0.90 — buying at "ask" means filling at expensive levels → no fill

**Fix**: use `/price?side=buy` API to get actual executable price.

### 3) verify_order_fill + cancel = ghost position

A common mistake:
1. Order placed, check fill within N seconds
2. Decide unfilled → cancel
3. But it actually filled (CLOB matching delay)
4. DB has nothing → ghost position
5. At resolution, the tokens exist on-chain but uncatalogued → effectively a loss

**Lesson**: **never cancel an order after placing it.** Instead, post-verify with `get_order()` at resolution time.

### 4) Weather market ghost positions

`expiry_time=""` empty string saved → DB query `expiry <= 0` excludes it forever → position slot permanently held.

**Fix**:
- Make sure weather opp dict includes `expiry_ts`
- Force-close trades with expiry=0 older than 24 hours (safety net)

### 5) PnL formula bug

The DB `size` is dollar cost, but the formula treated it as shares.
- Before (wrong): `WIN = (1 - ep) * size * 0.9`
- Fixed: `WIN = size * (1/ep - 1)`

This bug made DB PnL diverge from actual balance change. **Don't trust DB PnL. The on-chain balance is the source of truth.**

### 6) Predict.fun gas shortage

If the Signer EOA runs out of BNB, claims fail. BNB needs to be on the **Signer address**, not the Predict Account.

**Fix**: detect gas shortage → immediately halt the claim loop → recheck balance next cycle before resuming.

## Settled patterns

### Live strategies (two)

1. **hedge_arb**: same-market YES + NO sum < 0.93. Risk-free arb. Low frequency.
2. **predict_snipe** (Predict.fun): near-expiry + volatility-based pricing model when price is mispriced.

### Shadow

`expiry_snipe` is disabled live, only tracked in shadow (DB-logged but no real orders). Data accumulates; if a pattern emerges, revisit live.

### Safety

- **Circuit breaker**: daily -$30 loss → halt new entries (monitoring continues)
- **Balance snapshots**: USDC balance every 10 min → real PnL tracking
- **Auto claim**: settled markets auto-redeemed (`auto_claimer.py`, every 120s)
- **WAL checkpoint**: SQLite every 30 min (DB corruption protection)
- **API retry**: every external call has 3 retries + exponential backoff
- **API timeout**: 30 seconds enforced

### Code hardening

Defensive logic added through experience:

```python
# 1. price guard
def place_order(price, ...):
    if price < 0.001:
        raise ValueError("Polymarket min price is 0.001")
    # ...

# 2. RSI edge case
def rsi(prices, period=14):
    gains, losses = ...
    if gains == losses == 0:
        return 50  # neutral, not 100
    # ...

# 3. log_trade defense
def log_trade(**kwargs):
    try:
        # DB insert
    except Exception as e:
        logger.error(f"log_trade failed: {e}")
        # don't crash the bot

# 4. expiry sanity
def open_weather_position(opp):
    assert opp["expiry_ts"] > 0
    log_trade(expiry_time=str(opp["expiry_ts"]), ...)
```

## Predict.fun integration

Predict.fun is a BSC-based prediction market. Similar pattern, with differences:

- **Wallet structure**: Signer EOA + Predict Account (smart contract). Gas on Signer EOA in BNB.
- **API**: REST + WebSocket. Has its own SDK.
- **Assets**: BTC / ETH / SOL / BNB markets, many.
- **Short expiries**: 1-minute to 1-hour markets common → fast cycles.

Integration details:
- Integrated into main.py as `_predict_loop()` (no separate process)
- DB schema gets `strategy_name="predict_snipe"`
- Telegram alerts / claims / resolves all unified
- `.env` for dynamic params (no restart for tuning)

### Predict.fun probability model

Linear models are inaccurate. The approach used is a **volatility-based normal CDF**:

```python
import scipy.stats as stats
import math

def predict_probability(asset, current_price, target_price, minutes_left):
    sigma_per_minute = {
        "BTC": 0.0012,  # 0.12%
        "ETH": 0.0018,  # 0.18%
        "SOL": 0.0025,  # 0.25%
        "BNB": 0.0015,  # 0.15%
    }[asset]
    
    sigma = sigma_per_minute * math.sqrt(minutes_left)
    log_diff = math.log(target_price / current_price)
    z = log_diff / sigma
    return 1 - stats.norm.cdf(z)  # P(price > target)
```

This model + per-asset edge buffers (BTC 0%, ETH 3%, SOL 3%, BNB 2%) drives entry decisions.

### Current params

```bash
PREDICT_BET_SIZE=3
PREDICT_MAX_ENTRY_PRICE=0.70
PREDICT_MIN_EDGE=0.04
PREDICT_MAX_MINUTES=5  # 2-5 min sweet spot, WR 85%+
```

## Reporting

Daily checks:

```bash
python poly_report.py --days 7  # last 7 days
python poly_report.py --live    # today
python poly_report.py --date 2026-04-20
```

Sample format (specific numbers withheld):
```
=== YYYY-MM-DD Polymarket daily ===

By strategy:
  hedge_arb     +X.XX  (n=X, WR XX%)
  predict_snipe +X.XX  (n=X, WR XX%)

USDC change:  $XXX → $XXX
Net PnL: $±X.XX

Active positions: N
Unresolved markets: M
```

## Operational lessons (summary)

1. **DB PnL ≠ real PnL**. Balance snapshots are the truth.
2. **Limit orders carry adverse selection risk**. Without MM or info edge, dangerous.
3. **Don't cancel after placing**. Ghost positions arise.
4. **All external API calls need retry + timeout**.
5. **Circuit breaker mandatory** — prevents unbounded loss.
6. **Use shadow mode** — verify safety before going live.
7. **Verify fill rate before scaling size** — scaling unfilled orders only grows losses.

## Next chapter

Next: gold cross-exchange arb — gold ETFs, tokenized gold, perp gold market arb. The generalized cross-exchange pattern.
