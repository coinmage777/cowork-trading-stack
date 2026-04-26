#!/usr/bin/env node
// Rise.trade subprocess bridge — uses official risex-client SDK (v0.1.4).
// Protocol: JSON-RPC line-by-line over stdin/stdout.
//   Request  : {"id": <int>, "method": <str>, "params": {...}}
//   Response : {"id": <int>, "result": <any>} | {"id": <int>, "error": <str>}
// Methods: init, get_mark_price, get_collateral, get_balance, get_position,
//          create_order, close_position, cancel_orders, get_open_orders,
//          update_leverage, close
//
// Signer selection priority:
//   1. env RISEX_SIGNER_KEY (explicit session/api key)
//   2. params.api_key at init()
//   3. env WALLET_A_PK (account key used as signer)
//   4. params.private_key at init()
//
// If accountKey is supplied (WALLET_A_PK / params.private_key) and differs
// from the signerKey, the SDK can self-register the signer when isSignerRegistered()
// returns false. Otherwise we assume the session key is already Active on RISE.

import readline from 'node:readline';
import { appendFileSync } from 'node:fs';

import {
  ExchangeClient,
  InfoClient,
  Side,
  OrderType,
  TimeInForce,
  StpMode,
} from 'risex-client';

// --- utility: append JSONL trace (for live-trade forensics) ---------------
const TRACE_PATH = process.env.RISEX_TRACE_PATH || '/tmp/risex_bridge_trace.jsonl';
function trace(event, obj) {
  try {
    const line = JSON.stringify({ ts: Date.now(), event, ...obj });
    appendFileSync(TRACE_PATH, line + '\n');
  } catch { /* ignore */ }
}

// --- stderr helpers -------------------------------------------------------
function logErr(msg, err) {
  try {
    const errStr = err ? (err.stack || err.message || String(err)) : '';
    process.stderr.write(`[rise_bridge] ${msg}${errStr ? ' :: ' + errStr : ''}\n`);
  } catch { /* noop */ }
}

// --- global client state --------------------------------------------------
let client = null;          // ExchangeClient
let info = null;            // InfoClient
let markets = [];           // cached markets
let symbolToMarket = new Map();  // "BTC" -> Market
let marketIdToMarket = new Map();

function normalizeKey(k) {
  if (!k) return null;
  k = String(k).trim();
  if (!k) return null;
  if (!k.startsWith('0x') && !k.startsWith('0X')) k = '0x' + k;
  return k;
}

function symbolKey(sym) {
  if (sym === undefined || sym === null) return '';
  const s = String(sym).toUpperCase();
  // "BTC-PERP" -> "BTC", "BTC/USDC" -> "BTC"
  return s.replace(/-PERP$/, '').replace(/\/USDC$/, '').replace(/\/USD$/, '');
}

function resolveMarket(sym) {
  const key = symbolKey(sym);
  const m = symbolToMarket.get(key);
  if (!m) throw new Error(`unknown symbol: ${sym} (resolved=${key})`);
  return m;
}

function resolveMarketByIdOrSym(arg) {
  // arg may be market_id (string/number) or symbol
  if (arg == null) return null;
  if (typeof arg === 'number' || /^\d+$/.test(String(arg))) {
    const m = marketIdToMarket.get(String(arg));
    if (m) return m;
  }
  return resolveMarket(arg);
}

function marketMeta(m) {
  const cfg = m.config || {};
  return {
    market_id: Number(m.market_id),
    display_name: m.display_name,
    base: symbolKey(m.base_asset_symbol || m.display_name),
    quote: m.quote_asset_symbol || 'USDC',
    step_size: cfg.step_size,
    step_price: cfg.step_price,
    min_order_size: cfg.min_order_size,
    max_leverage: cfg.max_leverage,
    last_price: m.last_price,
    mark_price: m.mark_price,
    index_price: m.index_price,
  };
}

function sizeToSteps(m, amount) {
  const step = Number(m.config?.step_size || '0');
  if (!step || step <= 0) throw new Error(`bad step_size for ${m.display_name}`);
  const steps = Math.floor(Number(amount) / step + 1e-9);
  return steps;
}

function priceToTicks(m, price) {
  const tick = Number(m.config?.step_price || '0');
  if (!tick || tick <= 0) throw new Error(`bad step_price for ${m.display_name}`);
  const ticks = Math.round(Number(price) / tick);
  return ticks;
}

function sideToInt(s) {
  if (s == null) return Side.Long;
  const str = String(s).toLowerCase();
  if (['buy', 'long', 'bid'].includes(str)) return Side.Long;
  if (['sell', 'short', 'ask'].includes(str)) return Side.Short;
  if (Number(s) === 0 || Number(s) === 1) return Number(s);
  return Side.Long;
}

// --- RPC methods ----------------------------------------------------------

async function rpcInit(params) {
  const baseUrl = params.base_url || 'https://api.rise.trade';
  const wsUrl = params.ws_url || 'wss://ws.rise.trade/ws';
  const accountAddress = (params.wallet_address || params.account || '').trim();

  const signerKey = normalizeKey(
    process.env.RISEX_SIGNER_KEY || params.api_key || params.signer_key ||
    process.env.WALLET_A_PK || params.private_key
  );
  const accountKey = normalizeKey(
    process.env.WALLET_A_PK || params.private_key || params.account_key
  );

  if (!signerKey) throw new Error('no signer key provided (api_key / private_key / WALLET_A_PK)');
  if (!accountAddress) throw new Error('wallet_address required');

  const opts = {
    baseUrl,
    wsUrl,
    timeout: Number(params.timeout || 20000),
    logLevel: params.log_level || 'warn',
    account: accountAddress,
    signerKey,
  };
  if (accountKey) opts.accountKey = accountKey;

  info = new InfoClient({ baseUrl, wsUrl, timeout: opts.timeout, logLevel: opts.logLevel });
  client = new ExchangeClient(opts);
  await client.init();

  markets = await info.getMarkets();
  symbolToMarket.clear();
  marketIdToMarket.clear();
  for (const m of markets) {
    // base_asset_symbol on Rise can be "BTC/USDC" (combined pair) — normalize.
    const baseRaw = String(m.base_asset_symbol || '').toUpperCase();
    const baseSym = symbolKey(baseRaw);
    if (baseSym) symbolToMarket.set(baseSym, m);
    const dispSym = symbolKey(m.display_name);
    if (dispSym) symbolToMarket.set(dispSym, m);
    marketIdToMarket.set(String(m.market_id), m);
  }

  // Try to register signer if not already — ignore failures (most common: already active).
  let registered = null;
  try {
    registered = await client.isSignerRegistered();
    if (!registered && accountKey && accountKey !== signerKey) {
      try {
        const r = await client.registerSigner('mpdex-risex-bridge');
        trace('register_signer', { result: r });
        registered = true;
      } catch (e) {
        logErr('registerSigner failed (continuing anyway)', e);
      }
    }
  } catch (e) {
    logErr('isSignerRegistered check failed (continuing)', e);
  }

  trace('init', {
    account: accountAddress,
    signer: client.signer,
    signer_registered: registered,
    market_count: markets.length,
  });

  return {
    account: accountAddress,
    signer: client.signer,
    signer_registered: registered,
    chain_id: Number(client?.domain?.chainId || 0) || undefined,
    markets: markets.map(marketMeta),
  };
}

async function rpcGetMarkPrice(params) {
  const m = resolveMarketByIdOrSym(params.symbol ?? params.market_id);
  // refresh one market cheaply via getMarkets (Rise has no single-market endpoint documented)
  try {
    markets = await info.getMarkets();
    for (const mk of markets) {
      const baseSym = symbolKey(String(mk.base_asset_symbol || ''));
      if (baseSym) symbolToMarket.set(baseSym, mk);
      const dispSym = symbolKey(mk.display_name);
      if (dispSym) symbolToMarket.set(dispSym, mk);
      marketIdToMarket.set(String(mk.market_id), mk);
    }
  } catch (e) {
    logErr('getMarkets refresh failed', e);
  }
  const fresh = marketIdToMarket.get(String(m.market_id)) || m;
  const mp = fresh.mark_price || fresh.index_price || fresh.last_price;
  const v = mp ? Number(mp) : null;
  if (!v || !Number.isFinite(v) || v <= 0) return null;
  return v;
}

async function rpcGetOrderbook(params) {
  const m = resolveMarketByIdOrSym(params.symbol ?? params.market_id);
  return await info.getOrderbook(Number(m.market_id), Number(params.limit || 10));
}

async function rpcGetBalance() {
  const bal = await info.getBalance(client.account);
  return { USDC: Number(bal) };
}

async function rpcGetCollateral() {
  const bal = await info.getBalance(client.account);
  return { USDC: Number(bal) };
}

async function rpcGetPosition(params) {
  const m = resolveMarketByIdOrSym(params.symbol ?? params.market_id);
  const pos = await info.getPosition(Number(m.market_id), client.account);
  if (!pos) return null;
  const sizeNum = Number(pos.size || 0);
  if (!sizeNum) return null;
  const sideInt = Number(pos.side);
  // unified schema: amt positive for long, negative for short
  const amt = sideInt === Side.Short ? -Math.abs(sizeNum) : Math.abs(sizeNum);
  return {
    symbol: symbolKey(m.base_asset_symbol || m.display_name),
    market_id: Number(m.market_id),
    amt,
    size: Math.abs(sizeNum),
    side: sideInt === Side.Long ? 'long' : 'short',
    entry_price: Number(pos.entry_price || 0),
    mark_price: pos.mark_price != null ? Number(pos.mark_price) : null,
    unrealized_pnl: pos.unrealized_pnl != null ? Number(pos.unrealized_pnl) : null,
    leverage: pos.leverage != null ? Number(pos.leverage) : null,
    raw: pos,
  };
}

async function rpcGetAllPositions() {
  const list = await info.getAllPositions(client.account);
  return list.map(p => ({
    market_id: Number(p.market_id),
    size: Math.abs(Number(p.size || 0)),
    side: Number(p.side) === Side.Long ? 'long' : 'short',
    entry_price: Number(p.entry_price || 0),
    raw: p,
  }));
}

async function rpcCreateOrder(params) {
  const m = resolveMarketByIdOrSym(params.symbol ?? params.market_id);
  const amount = Number(params.amount);
  if (!amount || amount <= 0) throw new Error(`bad amount: ${params.amount}`);
  const side = sideToInt(params.side);
  const isMarket = (String(params.order_type || 'market').toLowerCase() === 'market');
  const reduceOnly = Boolean(params.is_reduce_only || params.reduce_only);
  const sizeSteps = sizeToSteps(m, amount);
  if (sizeSteps <= 0) throw new Error(`size_steps <= 0 (amount=${amount}, step=${m.config?.step_size})`);

  trace('create_order_request', {
    market_id: Number(m.market_id), side, amount, size_steps: sizeSteps,
    order_type: isMarket ? 'market' : 'limit', reduce_only: reduceOnly,
    price: params.price,
  });

  let resp;
  if (isMarket) {
    if (side === Side.Long) {
      resp = await client.marketBuy(Number(m.market_id), sizeSteps);
    } else {
      resp = await client.marketSell(Number(m.market_id), sizeSteps, reduceOnly);
    }
  } else {
    const price = Number(params.price);
    if (!price || price <= 0) throw new Error(`limit order needs price (got ${params.price})`);
    const priceTicks = priceToTicks(m, price);
    const postOnly = Boolean(params.post_only);
    if (side === Side.Long) {
      resp = await client.limitBuy(Number(m.market_id), sizeSteps, priceTicks, postOnly);
    } else {
      resp = await client.limitSell(Number(m.market_id), sizeSteps, priceTicks, postOnly);
    }
  }

  trace('create_order_response', { resp });
  return {
    order_id: resp.order_id,
    sc_order_id: resp.sc_order_id,
    tx_hash: resp.tx_hash,
    raw: resp,
  };
}

async function rpcClosePosition(params) {
  const m = resolveMarketByIdOrSym(params.symbol ?? params.market_id);
  trace('close_position_request', { market_id: Number(m.market_id) });
  const resp = await client.closePosition(Number(m.market_id));
  trace('close_position_response', { resp });
  if (!resp) return null;
  return {
    order_id: resp.order_id,
    sc_order_id: resp.sc_order_id,
    tx_hash: resp.tx_hash,
    raw: resp,
  };
}

async function rpcGetOpenOrders(params) {
  const m = params.symbol ? resolveMarketByIdOrSym(params.symbol) : null;
  const list = await info.getOpenOrders(client.account, m ? Number(m.market_id) : undefined);
  return list.map(o => ({
    order_id: o.order_id,
    resting_order_id: o.resting_order_id,
    market_id: Number(o.market_id),
    side: Number(o.side) === Side.Long ? 'buy' : 'sell',
    price_ticks: Number(o.price_ticks),
    size_steps: Number(o.size_steps),
    order_type: Number(o.order_type),
    post_only: Boolean(o.post_only),
    reduce_only: Boolean(o.reduce_only),
    raw: o,
  }));
}

async function rpcCancelOrders(params) {
  const m = params.symbol ? resolveMarketByIdOrSym(params.symbol) : null;
  if (m) {
    const resp = await client.cancelAllOrders(Number(m.market_id));
    return { success: Boolean(resp?.success), tx_hash: resp?.tx_hash, raw: resp };
  }
  const resp = await client.cancelAllOrders();
  return { success: Boolean(resp?.success), tx_hash: resp?.tx_hash, raw: resp };
}

async function rpcUpdateLeverage(params) {
  const m = resolveMarketByIdOrSym(params.symbol ?? params.market_id);
  const lev = BigInt(Math.max(1, Math.floor(Number(params.leverage || 1))));
  const resp = await client.updateLeverage(Number(m.market_id), lev);
  return { leverage: Number(lev), raw: resp };
}

async function rpcClose() {
  client = null;
  info = null;
  markets = [];
  symbolToMarket.clear();
  marketIdToMarket.clear();
  return 'ok';
}

// --- dispatcher -----------------------------------------------------------
const METHODS = {
  init: rpcInit,
  ping: async () => 'pong',
  get_mark_price: rpcGetMarkPrice,
  get_orderbook: rpcGetOrderbook,
  get_balance: rpcGetBalance,
  get_collateral: rpcGetCollateral,
  get_position: rpcGetPosition,
  get_all_positions: rpcGetAllPositions,
  create_order: rpcCreateOrder,
  close_position: rpcClosePosition,
  get_open_orders: rpcGetOpenOrders,
  cancel_orders: rpcCancelOrders,
  update_leverage: rpcUpdateLeverage,
  close: rpcClose,
};

async function handle(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: 0, error: `bad JSON: ${e.message}` }) + '\n');
    return;
  }
  const id = req.id ?? 0;
  const method = req.method;
  const params = req.params || {};

  const fn = METHODS[method];
  if (!fn) {
    process.stdout.write(JSON.stringify({ id, error: `unknown method: ${method}` }) + '\n');
    return;
  }

  try {
    const result = await fn(params);
    process.stdout.write(JSON.stringify({ id, result }) + '\n');
  } catch (e) {
    const msg = e?.message || String(e);
    logErr(`method=${method} failed`, e);
    trace('error', { method, error: msg });
    process.stdout.write(JSON.stringify({ id, error: msg }) + '\n');
  }
}

// --- stdin loop -----------------------------------------------------------
const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
  line = line.trim();
  if (!line) return;
  handle(line).catch((e) => logErr('handle loop error', e));
});
rl.on('close', () => {
  process.exit(0);
});

process.on('uncaughtException', (e) => logErr('uncaughtException', e));
process.on('unhandledRejection', (e) => logErr('unhandledRejection', e));

process.stderr.write('[rise_bridge] ready\n');
