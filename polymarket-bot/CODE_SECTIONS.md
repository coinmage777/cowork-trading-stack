# Polymarket Bot — Code Sections Analysis

## 1. _balance_snapshot_loop (Lines 346–376)

**Location**: `main.py:346-376`

```python
async def _balance_snapshot_loop(self):
    """10분마다 USDC 잔고를 기록 — 온체인 기준 진짜 수익 추적"""
    await asyncio.sleep(30)
    snapshot_file = Path(__file__).parent / "balance_snapshots.json"
    logger.info("[BALANCE] Snapshot loop started (every 600s)")
    while self._running:
        try:
            balance = await self.scanner.check_balance()
            if balance > 0:
                import json as _json
                from datetime import datetime, timezone
                entry = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "usdc": round(balance, 2),
                }
                # Append to JSON array file
                snapshots = []
                if snapshot_file.exists():
                    try:
                        snapshots = _json.loads(snapshot_file.read_text())
                    except Exception:
                        snapshots = []
                snapshots.append(entry)
                # Keep last 7 days (1008 entries at 10min intervals)
                if len(snapshots) > 1008:
                    snapshots = snapshots[-1008:]
                snapshot_file.write_text(_json.dumps(snapshots))
                logger.info(f"[BALANCE] Snapshot: ${balance:.2f}")
        except Exception as exc:
            logger.warning(f"[BALANCE] Snapshot error: {exc}")
        await asyncio.sleep(600)
```

**Key Features**:
- Runs every 600 seconds (10 minutes)
- Stores USDC balance snapshots in JSON array format
- Maintains only last 1008 entries (~7 days at 10min intervals)
- Uses UTC timestamps in ISO format
- Safe file I/O with exception handling

---

## 2. _weather_loop (Lines 436–530)

**Location**: `main.py:436-530`

```python
async def _weather_loop(self):
    """날씨 마켓 스캔 루프 — 기존 크립토 전략과 독립적으로 동작"""
    await asyncio.sleep(10)
    logger.info("[WEATHER] Weather strategy loop started")
    while self._running and self._weather is not None:
        try:
            opps = await self._weather.scan()
            if opps:
                for opp in opps[:3]:  # 상위 3개만 로그
                    logger.info(
                        f"[WEATHER] {opp['side']} {opp['city']} | "
                        f"forecast={opp['model_prob']:.1%} vs market={opp['market_prob']:.1%} | "
                        f"edge={opp['edge']:.1%} | ${opp['bet_size']:.2f} | "
                        f"{opp['question'][:50]}"
                    )
                # Paper trade: log to DB
                if self.mode == "paper":
                    for opp in opps[:1]:  # 최고 엣지 1건만 페이퍼 트레이드
                        self.db_logger.log_trade(
                            market_id=opp["market_id"],
                            market_question=opp["question"],
                            side=opp["side"],
                            entry_price=opp["entry_price"],
                            size=opp["bet_size"],
                            signal_values={"city": opp["city"], "threshold": opp.get("threshold", 0), "forecast_prob": round(opp["model_prob"], 4), "market_prob": round(opp["market_prob"], 4), "source": opp.get("source", ""), "token_id": opp.get("token_id", ""), "metric": opp.get("metric", "high_temp"), "direction": opp.get("direction", "above")},
                            model_prob=opp["model_prob"],
                            market_prob=opp["market_prob"],
                            edge=opp["edge"],
                            kelly_fraction=0,
                            expiry_time=str(opp.get("expiry_ts", 0) or ""),
                            strategy_name=opp["strategy"],
                            mode=self.mode,
                            market_group="weather",
                            asset_symbol="WX",
                            token_id=opp.get("token_id", ""),
                        )
                        logger.info(
                            f"[WEATHER] Paper trade placed: {opp['side']} ${opp['bet_size']:.2f} @ {opp['entry_price']:.2f} "
                            f"edge={opp['edge']:.1%} on {opp['question'][:40]}"
                        )
                # Live trade: weather has its own risk limits (not shared with crypto)
                elif self.mode == "live":
                    # Count current weather positions + collect open market_ids for dedup
                    open_wx_trades = self.db_logger.get_open_trades(market_group="weather")
                    weather_open = len(open_wx_trades)
                    open_wx_market_ids = {t.get("market_id", "") for t in open_wx_trades}
                    max_wx = self.config.weather_max_positions
                    for opp in opps[:3]:
                        if weather_open >= max_wx:
                            logger.info(f"[WEATHER] Max weather positions reached ({weather_open}/{max_wx})")
                            break
                        # Skip if we already have a position on this market
                        if opp["market_id"] in open_wx_market_ids:
                            continue
                        bet_size = min(opp["bet_size"], self.config.weather_max_bet)
                        if bet_size < self.config.weather_min_bet:
                            continue
                        logger.info(
                            f"[WEATHER] Placing LIVE order: {opp['side']} ${bet_size:.2f} @ {opp['entry_price']:.3f} "
                            f"edge={opp['edge']:.1%} | {opp['question'][:50]}"
                        )
                        order_id = await self.scanner.place_order(
                            opp["token_id"], opp["side"],
                            bet_size, opp["entry_price"], mode="live",
                        )
                        if order_id:
                            self.db_logger.log_trade(
                                market_id=opp["market_id"],
                                market_question=opp["question"],
                                side=opp["side"],
                                entry_price=opp["entry_price"],
                                size=bet_size,
                                signal_values={"city": opp["city"], "threshold": opp.get("threshold", 0), "forecast_prob": round(opp["model_prob"], 4), "market_prob": round(opp["market_prob"], 4), "source": opp.get("forecast_source", ""), "token_id": opp.get("token_id", ""), "metric": opp.get("metric", "high_temp"), "direction": opp.get("direction", "above")},
                                model_prob=opp["model_prob"],
                                market_prob=opp["market_prob"],
                                edge=opp["edge"],
                                kelly_fraction=0,
                                expiry_time=str(opp.get("expiry_ts", 0) or ""),
                                order_id=str(order_id),
                                mode="live",
                                strategy_name=opp["strategy"],
                                market_group="weather",
                                asset_symbol="WX",
                                token_id=opp.get("token_id", ""),
                            )
                            weather_open += 1
                            open_wx_market_ids.add(opp["market_id"])
                            logger.info(f"[WEATHER] Order FILLED: {order_id} ({weather_open}/{max_wx})")
                        else:
                            logger.warning(f"[WEATHER] Order rejected: {opp['question'][:40]}")
            status = self._weather.format_status()
            self.dashboard.set_trade_info(f"[WX] {status}")
        except Exception as exc:
            logger.error(f"[WEATHER] Loop error: {exc}", exc_info=True)
        await asyncio.sleep(self.config.weather_scan_interval_sec)
```

**Key Features**:
- Scans weather-based betting opportunities
- Independent from crypto strategies
- Deduplication: prevents multiple positions on same market
- Separate position limits: `weather_max_positions`, `weather_max_bet`, `weather_min_bet`
- Both paper and live modes supported
- Logs signal values: city, threshold, forecast_prob, market_prob, metric, direction

---

## 3. _predict_loop (Lines 609–641)

**Location**: `main.py:609-641`

```python
async def _predict_loop(self):
    """Predict.fun crypto up/down expiry snipe loop"""
    await asyncio.sleep(5)
    if self._predict_client is None or self._predict_sniper is None:
        return
    for attempt in range(3):
        try:
            await self._predict_client.connect()
            break
        except Exception as exc:
            logger.error(f"[PREDICT] Client connect failed (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                logger.error("[PREDICT] Giving up after 3 connect attempts")
                return
    mode_label = "PAPER" if self._predict_sniper.paper else "LIVE"
    logger.info(f"[PREDICT] Predict.fun sniper loop started ({mode_label})")
    last_claim_time = 0.0
    while self._running:
        try:
            await self._predict_sniper.scan_and_snipe()
            stats = self._predict_sniper.get_stats()
            if stats["trades"] > 0 and stats["trades"] % 5 == 0:
                logger.info(f"[PREDICT] Stats: {stats['trades']} trades, active={stats['active_markets']}")
            # Auto-claim resolved positions
            now = time.time()
            if not self._predict_sniper.paper and now - last_claim_time >= self.config.predict_claim_interval_sec:
                last_claim_time = now
                await self._predict_sniper.claim_resolved()
        except Exception as exc:
            logger.error(f"[PREDICT] Loop error: {exc}", exc_info=True)
        await asyncio.sleep(self.config.predict_scan_interval_sec)
```

**Key Features**:
- 3-attempt connection retry with exponential backoff
- Calls `scan_and_snipe()` on each cycle
- Logs stats every 5 trades
- Auto-claims resolved positions on interval
- Paper vs live mode detection
- Only claims in live mode (not in paper)

---

## 4. _close_trade_if_resolved (Lines 1258–1334)

**Location**: `main.py:1258-1334`

```python
async def _close_trade_if_resolved(self, trade: dict[str, Any], *, now: float | None = None, force_close: bool = False, reason: str = "") -> bool:
    now = now if now is not None else time.time()
    expiry = float(trade.get("expiry_time", 0) or 0)
    if expiry <= 0:
        # Safety: force-close trades with no expiry after 24h
        ts_str = str(trade.get("timestamp", "") or "")
        if ts_str:
            try:
                created = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if now - created > 86400:  # 24 hours
                    logger.warning(f"[SAFETY] Force-closing #{trade['id']} — no expiry, open >24h")
                    return await self._force_close_as_loss(trade, reason="no_expiry_24h")
            except Exception:
                pass
        return False
    if not force_close and now < expiry + 30:
        return False
    won = await self._resolve_trade(trade)
    resolution = "resolved"
    if won is None:
        if now > expiry + self._stale_trade_grace_sec:
            won = False
            resolution = "stale_grace"
        else:
            return False
    exit_price = 1.0 if won else 0.0
    entry_price = float(trade.get("entry_price") or 0.0)
    size = float(trade.get("size") or 0.0)  # dollar cost
    trade_mode = str(trade.get("mode") or "paper")
    trade_id = int(trade["id"])

    # For live trades: verify the order actually filled on-chain
    if trade_mode == "live" and trade.get("order_id"):
        try:
            client = self.scanner._get_clob_client()
            order = await asyncio.get_event_loop().run_in_executor(
                None, lambda: client.get_order(trade["order_id"])
            )
            if order:
                size_matched = float(order.get("size_matched", 0) or 0)
                if size_matched == 0:
                    # Order never filled — close with pnl=0
                    self.db_logger.close_trade(trade_id, 0.0, 0.0, update_daily=False)
                    self._active_market_ids.discard(str(trade["market_id"]))
                    logger.info(f"[LIVE] UNFILLED #{trade_id} (order never matched) | {trade.get('market_question', '')[:30]}")
                    return True
        except Exception as verify_err:
            logger.debug(f"Order verify failed #{trade_id}: {verify_err}")

    if won:
        # shares = size / entry_price, payout = shares * $1
        pnl = size * (1.0 / entry_price - 1.0) if entry_price > 0 else 0.0
    else:
        pnl = -size
    self.db_logger.close_trade(trade_id, exit_price, pnl, update_daily=trade_mode != "shadow")

    # Finalize MAE/MFE tracking
    ml_suffix = ""
    if self.ml_model is not None:
        metrics = self.ml_model.finish_tracking(trade_id, exit_price, pnl)
        if metrics:
            self.db_logger.update_trade_mae_mfe(trade_id, metrics["mae"], metrics["mfe"])
            ml_suffix = f" mae={metrics['mae']:.3f} mfe={metrics['mfe']:.3f} left={metrics['left_on_table']:.3f}"

    if trade_mode == "shadow":
        self._active_shadow_keys.discard(self._shadow_key(str(trade["market_id"]), str(trade.get("side", ""))))
    else:
        self._active_market_ids.discard(str(trade["market_id"]))
    suffix = f" ({reason})" if reason else ""
    logger.info(
        f"[{trade_mode.upper()}] CLOSED #{trade_id} {('WIN' if pnl > 0 else 'LOSS')} ${pnl:+.2f} | "
        f"{trade.get('market_group', '-')}/{trade.get('market_question', '')[:30]} "
        f"entry={entry_price:.3f} via={resolution}{suffix}{ml_suffix}"
    )
    self.risk_manager.check_daily_stop_loss()
    self._refresh_dashboard_status()
    return True
```

**Key Features**:
- Safety net: force-closes trades with no expiry after 24 hours
- Waits until `now >= expiry + 30` seconds before attempting resolution
- Resolves via `_resolve_trade()` (tries CLOB → gamma API → Binance fallback)
- Stale grace period: if can't resolve, waits `_stale_trade_grace_sec` then marks as loss
- **Unfilled order check**: verifies `size_matched=0` means order never filled → close with pnl=0
- PnL formula: `WIN: size * (1/entry - 1)`, `LOSS: -size`
- Tracks MAE/MFE metrics if ML model enabled
- Updates dashboard and checks daily stop loss
- Handles shadow vs live vs paper trades differently

---

## 5. hedge_arb Strategy Code (Lines 533–554)

**Location**: `market_scanner.py:533-554`

```python
# --- Feature 6: Both-sides hedge arbitrage ---
if config.hedge_enabled:
    combined = market.up_price + market.down_price
    if combined < config.hedge_max_combined_price and combined > 0:
        profit_per_share = 1.0 - combined
        profit_pct = profit_per_share / combined
        if profit_pct >= config.hedge_min_profit_pct:
            # Buy the cheaper side (more profit on that side)
            cheaper_side = "UP" if market.up_price <= market.down_price else "DOWN"
            cheaper_price = min(market.up_price, market.down_price)
            token_id = market.up_token_id if cheaper_side == "UP" else market.down_token_id
            opportunities.append({
                "market": market,
                "side": cheaper_side,
                "token_id": token_id,
                "model_prob": 1.0 / combined,  # guaranteed payout
                "market_prob": cheaper_price,
                "edge": profit_pct,
                "entry_price": cheaper_price,
                "strategy": "hedge_arb",
                "hedge_combined": combined,
            })
            continue
```

**Key Features**:
- Requires `config.hedge_enabled=true` to activate
- Checks if `up_price + down_price < hedge_max_combined_price` (usually 0.99–1.00)
- Profit guarantee: you buy both UP and DOWN tokens at $X + $Y, redeem for $1
- Profit per share: `1.0 - combined`
- Profit percentage: `(1.0 - combined) / combined`
- Buys the **cheaper side** only to maximize arbitrage return
- Sets `model_prob = 1.0 / combined` (risk-free payout)
- **Currently DISABLED in live mode** (not in `LIVE_ONLY_STRATEGIES`)

---

## 6. Configuration Reference

**Location**: `config.py:131`

```python
live_only_strategies: str = field(default_factory=lambda: os.getenv("LIVE_ONLY_STRATEGIES", "expiry_snipe,hedge_arb"))
```

**Current Setting** (from CLAUDE.md):
```
LIVE_ONLY_STRATEGIES=hedge_arb
```

Only **hedge_arb** can run in live mode. **expiry_snipe is disabled** due to reverse selection problem (体质 体质 29% win rate on fills).

---

## Summary Table

| Section | File | Lines | Purpose | Current Status |
|---------|------|-------|---------|-----------------|
| `_balance_snapshot_loop` | main.py | 346–376 | Record USDC balance every 10min | Active |
| `_weather_loop` | main.py | 436–530 | Weather market trading (paper/live) | Disabled in config |
| `_predict_loop` | main.py | 609–641 | [Predict.fun](https://predict.fun?ref=5302B) crypto sniper | Active (live) |
| `_close_trade_if_resolved` | main.py | 1258–1334 | Resolution & PnL settlement | Active |
| `hedge_arb strategy` | market_scanner.py | 533–554 | Risk-free arbitrage (up+down<1.00) | Whitelisted (not in live) |

