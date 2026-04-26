"""Stoikov MM Fill Simulator — retrospective PnL from quote log.

Reads `data/mm_quotes.jsonl` (written by mm_strategy.py shadow loop) and
estimates whether each quoted bid/ask would have been filled based on the
subsequent mid-price path of the SAME market. No actual orders involved.

Design
------
- Polymarket CLOB fill semantics we approximate:
    * our BID (we buy UP at `bid`) fills when the market ASK drops to our bid,
      i.e. at least one later snapshot has ask_market <= our bid. We don't
      have orderbook snapshots here — only `mid` — so we use the inequality
      `min(mid_future) <= our_bid` as a conservative "market crossed us"
      proxy. Mid is closer to ask than to bid on thin books, so this is
      slightly pessimistic (realistic for shadow accounting).
    * our ASK (we sell UP at `ask`) fills when mid rises to our ask:
      `max(mid_future) >= our_ask`.
- Only snapshots where `action_bid == "placed"` (and eventually `action_ask`)
  count as "on the book". `held` and `skipped` rows are telemetry only but
  still used for mid-path / resolution tracking.
- For each (market_id, quote_ts) we look ahead inside the same market_id's
  snapshot stream until (a) the quote is superseded by the next `placed`
  snapshot for that market (assume previous quote cancelled and replaced),
  or (b) market_id data ends. The first crossing wins.
- Resolution: this log doesn't carry resolution outcomes, and TTCs are still
  positive at the last snapshot — so we can't book terminal PnL. Instead we
  mark-to-market remaining inventory at the LAST observed mid.
- Fees: Polymarket CLOB maker/taker currently 0% → gross ≈ net.

P(UP) calibration — only meaningful once prior differentiates. Right now
prob_up is ~0.5 for the whole log, so calibration buckets will be mostly
empty but the pipeline is wired up.

Usage
-----
    python sim_mm_fills.py
    python sim_mm_fills.py --quotes data/mm_quotes.jsonl --out data/mm_sim_fills.jsonl

Writes:
    data/mm_sim_fills.jsonl        — one fill event per line
    data/mm_sim_summary.md         — human-readable summary
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict, Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


# ---------- constants ---------------------------------------------------------

MAKER_FEE = 0.0  # Polymarket CLOB
TAKER_FEE = 0.0
# Per MM strategy, when action_bid='placed' we simulate an order on the book.
# We don't track action_ask='placed' yet because in the current log window
# all ask actions are 'skipped' (accumulation mode). Logic supports both.
PLACED_STATES = {"placed"}


# ---------- data structures ---------------------------------------------------

@dataclass
class Fill:
    ts: float
    market_id: str
    side: str               # "BUY" (our bid hit) or "SELL" (our ask hit)
    price: float            # our quote price
    trigger_mid: float      # mid that triggered the fill
    size_usd: float         # $ invested if BUY (assumed = $1 test unit);
                            # $ received if SELL (shares * price)
    shares: float           # share count (size_usd / price for BUY)
    quote_ts: float         # when we posted the quote
    lookahead_sec: float    # quote_ts → fill ts


@dataclass
class MarketSim:
    market_id: str
    inventory_shares: float = 0.0
    inventory_cost_usd: float = 0.0   # total $ spent buying shares
    inventory_revenue_usd: float = 0.0  # total $ from selling shares
    fills: list = field(default_factory=list)
    quotes_placed_bid: int = 0
    quotes_placed_ask: int = 0
    last_mid: float = 0.0
    last_ts: float = 0.0
    first_ts: float = 0.0
    prob_up_samples: list = field(default_factory=list)


# ---------- core simulation ---------------------------------------------------

def load_quotes(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda q: (q.get("market_id", ""), q.get("ts", 0.0)))
    return rows


def simulate(
    quotes: list[dict],
    bet_size_usd: float = 1.0,
) -> tuple[dict[str, MarketSim], list[Fill]]:
    """Run per-market fill sim.

    For each market, walk snapshots in time order. When we see a 'placed' bid
    (or ask) at quote_ts, we set that as the "active" quote for that side.
    Subsequent snapshots' mids are checked for crossing; the first cross
    produces a Fill. A new 'placed' on the same side supersedes the prior.
    """
    by_market: dict[str, list[dict]] = defaultdict(list)
    for q in quotes:
        by_market[q["market_id"]].append(q)

    sims: dict[str, MarketSim] = {}
    all_fills: list[Fill] = []

    for market_id, rows in by_market.items():
        sim = MarketSim(market_id=market_id)
        sims[market_id] = sim
        rows.sort(key=lambda r: r["ts"])
        sim.first_ts = rows[0]["ts"]

        # Active resting quotes — None = no quote on that side
        active_bid: Optional[dict] = None  # {"ts","price","idx"}
        active_ask: Optional[dict] = None

        for idx, q in enumerate(rows):
            ts = q["ts"]
            mid = q.get("mid") or 0.0
            sim.last_mid = mid
            sim.last_ts = ts
            if "prob_up" in q and q.get("bayes_enabled"):
                sim.prob_up_samples.append(q["prob_up"])

            # 1) first: check if pre-existing resting quotes got crossed by this mid
            if active_bid is not None:
                # BUY when market drops to our bid.
                # Guard: only cross if mid is strictly <= bid (tie = no fill, conservative).
                if mid <= active_bid["price"] and ts > active_bid["ts"]:
                    price = active_bid["price"]
                    shares = bet_size_usd / price if price > 0 else 0.0
                    fill = Fill(
                        ts=ts, market_id=market_id, side="BUY",
                        price=price, trigger_mid=mid, size_usd=bet_size_usd,
                        shares=shares, quote_ts=active_bid["ts"],
                        lookahead_sec=ts - active_bid["ts"],
                    )
                    sim.fills.append(fill)
                    all_fills.append(fill)
                    sim.inventory_shares += shares
                    sim.inventory_cost_usd += bet_size_usd * (1 + TAKER_FEE)
                    active_bid = None  # one-shot; next 'placed' replaces

            if active_ask is not None:
                if mid >= active_ask["price"] and ts > active_ask["ts"]:
                    price = active_ask["price"]
                    # Sell min(inventory_shares, bet_size_usd/price) — if no inventory,
                    # short selling not allowed on Polymarket → skip.
                    desired_shares = bet_size_usd / price if price > 0 else 0.0
                    shares = min(desired_shares, sim.inventory_shares)
                    if shares > 0:
                        revenue = shares * price
                        fill = Fill(
                            ts=ts, market_id=market_id, side="SELL",
                            price=price, trigger_mid=mid, size_usd=revenue,
                            shares=shares, quote_ts=active_ask["ts"],
                            lookahead_sec=ts - active_ask["ts"],
                        )
                        sim.fills.append(fill)
                        all_fills.append(fill)
                        sim.inventory_shares -= shares
                        sim.inventory_revenue_usd += revenue * (1 - TAKER_FEE)
                    active_ask = None

            # 2) then: register new 'placed' quotes from this snapshot
            if q.get("action_bid") in PLACED_STATES:
                bid_price = q.get("bid")
                if bid_price and bid_price > 0:
                    active_bid = {"ts": ts, "price": float(bid_price), "idx": idx}
                    sim.quotes_placed_bid += 1
            if q.get("action_ask") in PLACED_STATES:
                ask_price = q.get("ask")
                if ask_price and ask_price > 0:
                    active_ask = {"ts": ts, "price": float(ask_price), "idx": idx}
                    sim.quotes_placed_ask += 1

    return sims, all_fills


def pnl_from_sim(sims: dict[str, MarketSim]) -> dict[str, float]:
    """Compute realized + mark-to-market PnL.

    Realized = inventory_revenue_usd - inventory_cost_usd only for SHARES
    that were sold. But cost_usd above tracks total buys; we need to
    allocate cost per SOLD share. We use average cost basis.
    Unrealized = inventory_shares * last_mid - remaining_cost_basis.
    """
    total_realized = 0.0
    total_unrealized = 0.0
    total_invested = 0.0
    total_shares = 0.0
    for s in sims.values():
        # Reconstruct per-fill average cost basis
        total_bought_shares = sum(f.shares for f in s.fills if f.side == "BUY")
        total_bought_usd = sum(f.size_usd for f in s.fills if f.side == "BUY")
        sold_shares = sum(f.shares for f in s.fills if f.side == "SELL")
        sold_revenue = sum(f.size_usd for f in s.fills if f.side == "SELL")
        avg_cost = (total_bought_usd / total_bought_shares) if total_bought_shares > 0 else 0.0
        realized = sold_revenue - sold_shares * avg_cost
        remaining_shares = s.inventory_shares
        remaining_cost = remaining_shares * avg_cost
        unrealized = remaining_shares * s.last_mid - remaining_cost
        total_realized += realized
        total_unrealized += unrealized
        total_invested += total_bought_usd
        total_shares += total_bought_shares
    return {
        "realized": total_realized,
        "unrealized_mtm": total_unrealized,
        "net": total_realized + total_unrealized,
        "invested_usd": total_invested,
        "total_shares_bought": total_shares,
    }


def prob_up_calibration(quotes: list[dict], sims: dict[str, MarketSim]) -> list[dict]:
    """Bucket prob_up by 0.05 bins and count how many markets in that bucket
    resolved UP. Right now we don't know resolution — we use `mid at last
    snapshot >= 0.5` as a weak proxy ("would have resolved UP if frozen").
    This is only valid as a sanity sketch; real calibration needs post-
    resolution outcomes.
    """
    # map market -> last prob_up observation and last mid
    last_p_by_market: dict[str, float] = {}
    last_mid_by_market: dict[str, float] = {}
    for q in quotes:
        if q.get("bayes_enabled") and "prob_up" in q:
            last_p_by_market[q["market_id"]] = q["prob_up"]
        last_mid_by_market[q["market_id"]] = q.get("mid", 0.5)
    buckets = [(0.0, 0.45), (0.45, 0.50), (0.50, 0.55), (0.55, 0.60),
               (0.60, 0.70), (0.70, 0.85), (0.85, 1.01)]
    out = []
    for lo, hi in buckets:
        members = [m for m, p in last_p_by_market.items() if lo <= p < hi]
        up_count = sum(1 for m in members if last_mid_by_market.get(m, 0.5) >= 0.5)
        out.append({
            "bucket": f"[{lo:.2f},{hi:.2f})",
            "n_markets": len(members),
            "proxy_up_rate": (up_count / len(members)) if members else None,
        })
    return out


# ---------- reporting ---------------------------------------------------------

def min_sample_for_significance(p: float = 0.55, margin: float = 0.05, z: float = 1.96) -> int:
    """Normal-approx sample size to detect p ≠ 0.50 at 95% CI half-width `margin`.
    n ≈ z^2 * p(1-p) / margin^2 . For p=0.55, margin=0.05 → n≈380.
    """
    return int(math.ceil((z ** 2) * p * (1 - p) / (margin ** 2)))


def write_outputs(
    sims: dict[str, MarketSim],
    fills: list[Fill],
    pnl: dict[str, float],
    calibration: list[dict],
    quotes: list[dict],
    fills_path: Path,
    summary_path: Path,
) -> None:
    fills_path.parent.mkdir(parents=True, exist_ok=True)
    with fills_path.open("w", encoding="utf-8") as f:
        for fill in fills:
            f.write(json.dumps(asdict(fill)) + "\n")

    n_quotes = len(quotes)
    n_markets = len(sims)
    n_placed_bid = sum(s.quotes_placed_bid for s in sims.values())
    n_placed_ask = sum(s.quotes_placed_ask for s in sims.values())
    n_fills_buy = sum(1 for f in fills if f.side == "BUY")
    n_fills_sell = sum(1 for f in fills if f.side == "SELL")
    bid_fill_rate = (n_fills_buy / n_placed_bid) if n_placed_bid else 0.0
    ask_fill_rate = (n_fills_sell / n_placed_ask) if n_placed_ask else 0.0

    ts_min = min((s.first_ts for s in sims.values() if s.first_ts > 0), default=0.0)
    ts_max = max((s.last_ts for s in sims.values()), default=0.0)
    span_hr = (ts_max - ts_min) / 3600 if ts_max > ts_min else 0.0

    lines: list[str] = []
    lines.append("# MM Fill Simulation Summary\n")
    lines.append(f"- quote rows scanned: **{n_quotes}**")
    lines.append(f"- unique markets: **{n_markets}**")
    lines.append(f"- time span: **{span_hr:.2f} h**  ({ts_min:.0f} → {ts_max:.0f})")
    lines.append("")
    lines.append("## Quotes posted (shadow)\n")
    lines.append(f"- bid placements: **{n_placed_bid}**")
    lines.append(f"- ask placements: **{n_placed_ask}**")
    lines.append("")
    lines.append("## Hypothetical fills\n")
    lines.append(f"- BUY fills: **{n_fills_buy}**  (bid fill-rate **{bid_fill_rate:.1%}**)")
    lines.append(f"- SELL fills: **{n_fills_sell}**  (ask fill-rate **{ask_fill_rate:.1%}**)")
    lines.append("")
    lines.append("## Synthetic PnL ($1 per bid fill)\n")
    lines.append(f"- realized:       **${pnl['realized']:+.4f}**")
    lines.append(f"- unrealized MTM: **${pnl['unrealized_mtm']:+.4f}**")
    lines.append(f"- net:            **${pnl['net']:+.4f}**")
    lines.append(f"- invested:       **${pnl['invested_usd']:.2f}**  "
                 f"({pnl['total_shares_bought']:.2f} shares)")
    lines.append("")
    lines.append("## P(UP) calibration — PROXY ONLY (no real resolutions in log)\n")
    lines.append("| bucket | n_markets | proxy_up_rate (last_mid ≥ 0.5) |")
    lines.append("|---|---|---|")
    for row in calibration:
        rate = row["proxy_up_rate"]
        rate_s = f"{rate:.1%}" if rate is not None else "–"
        lines.append(f"| {row['bucket']} | {row['n_markets']} | {rate_s} |")
    lines.append("")
    lines.append("## Per-market detail\n")
    lines.append("| market_id (16) | bids_placed | fills | inv_shares | last_mid | "
                 "invested | realized | last_prob_up |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in sorted(sims.values(), key=lambda x: -len(x.fills)):
        inv_cost = sum(f.size_usd for f in s.fills if f.side == "BUY")
        inv_shares = sum(f.shares for f in s.fills if f.side == "BUY") \
                     - sum(f.shares for f in s.fills if f.side == "SELL")
        realized_market = (
            sum(f.size_usd for f in s.fills if f.side == "SELL")
            - (sum(f.shares for f in s.fills if f.side == "SELL")
               * (inv_cost / sum(f.shares for f in s.fills if f.side == "BUY"))
               if any(f.side == "BUY" for f in s.fills) else 0.0)
        )
        last_p = s.prob_up_samples[-1] if s.prob_up_samples else float("nan")
        lines.append(
            f"| {s.market_id[:16]}.. | {s.quotes_placed_bid} | {len(s.fills)} | "
            f"{inv_shares:.2f} | {s.last_mid:.3f} | ${inv_cost:.2f} | "
            f"${realized_market:+.4f} | {last_p:.3f} |"
        )
    lines.append("")
    lines.append("## Statistical-significance target\n")
    n_req = min_sample_for_significance(p=0.55, margin=0.05)
    n_req_tight = min_sample_for_significance(p=0.55, margin=0.03)
    lines.append(
        f"- to detect an edge of p=0.55 vs 0.50 at 95% CI half-width 5%: "
        f"**~{n_req} fills** required.")
    lines.append(
        f"- for ±3% half-width: **~{n_req_tight}** fills.")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")


# ---------- main --------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quotes", default="data/mm_quotes.jsonl")
    ap.add_argument("--out", default="data/mm_sim_fills.jsonl")
    ap.add_argument("--summary", default="data/mm_sim_summary.md")
    ap.add_argument("--bet-size-usd", type=float, default=1.0,
                    help="notional per bid-fill (default $1, matches MM accumulation unit)")
    args = ap.parse_args()

    qpath = Path(args.quotes)
    if not qpath.exists():
        print(f"[sim] quotes file not found: {qpath}", file=sys.stderr)
        return 1

    quotes = load_quotes(qpath)
    if not quotes:
        print("[sim] no quotes parsed", file=sys.stderr)
        return 1

    sims, fills = simulate(quotes, bet_size_usd=args.bet_size_usd)
    pnl = pnl_from_sim(sims)
    calibration = prob_up_calibration(quotes, sims)

    write_outputs(
        sims=sims, fills=fills, pnl=pnl, calibration=calibration, quotes=quotes,
        fills_path=Path(args.out), summary_path=Path(args.summary),
    )

    # console tl;dr
    n_bid = sum(s.quotes_placed_bid for s in sims.values())
    n_ask = sum(s.quotes_placed_ask for s in sims.values())
    n_buy = sum(1 for f in fills if f.side == "BUY")
    n_sell = sum(1 for f in fills if f.side == "SELL")
    print(f"[sim] quotes={len(quotes)} markets={len(sims)} "
          f"bid_placed={n_bid} ask_placed={n_ask} "
          f"BUY_fills={n_buy} SELL_fills={n_sell} "
          f"net_pnl=${pnl['net']:+.4f}")
    print(f"[sim] wrote {args.out}")
    print(f"[sim] wrote {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
