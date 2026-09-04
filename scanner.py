"""
Longshot payoff scanner — pure discovery tool, no forecasting, no API key,
no orders. Ranks open Kalshi markets by payout multiple (1/price of the
cheaper side), across every category EXCEPT the ones the house already runs
dedicated systems for or has ruled out (see project memory):
  - Crypto      (pm_edge)
  - Sports      (pm_edge)
  - Climate and Weather (longtail, archived but still "ours")
  - Economics   (macro data releases — different edge shape, out of scope here)

CAVEAT (see ref_kalshi_research memory, Whelan et al.): cheap longshot
contracts show a clear favorite-longshot bias and lose money after fees on
average. A high multiple here is NOT an edge signal — it's raw 1/price. This
tool surfaces candidates for a human to look at, same way pm_edge's scanner.py
did before any bet sizing was attached. Do not wire this into an order path.

Usage:
    python scanner.py [--min-volume N] [--max-days D] [--min-hours H] [--limit N]
"""

import argparse
import time
from dataclasses import dataclass

from kalshi import Market, fetch_book_depth, fetch_open_markets

EXCLUDED_CATEGORIES = {"Crypto", "Sports", "Economics", "Climate and Weather"}

# Below this, yes_bid/yes_ask are usually stale placeholders (no real book),
# not genuine 1c/99c quotes — division by them produces meaningless multiples.
MIN_PRICE = 0.01
MAX_PRICE = 0.99


@dataclass
class Candidate:
    market: Market
    side: str        # "yes" or "no"
    price: float      # cost to buy $1 of payout on that side
    multiple: float   # 1 / price
    depth_contracts: float = -1.0   # -1 = not checked; set by add_depth()
    depth_dollars: float = -1.0


def add_depth(candidates: list[Candidate], sleep: float = 0.05) -> None:
    """Mutates each candidate in place with real order-book depth at its
    quoted price (see kalshi.fetch_book_depth) — one API call per candidate,
    so call this AFTER narrowing to a shortlist, not on the raw scan."""
    for c in candidates:
        contracts, dollars = fetch_book_depth(c.market.ticker, c.side)
        c.depth_contracts = contracts
        c.depth_dollars = dollars
        time.sleep(sleep)


def _best_side(m: Market) -> Candidate | None:
    """Cheaper side wins (bigger payout). NO side is priced off yes_bid —
    Kalshi's complementary pricing means no_ask = 1 - yes_bid (see kalshi.py
    module note: no separate no_bid/no_ask field is returned)."""
    yes_price = m.yes_ask
    no_price = 1.0 - m.yes_bid

    candidates = []
    if MIN_PRICE <= yes_price <= MAX_PRICE:
        candidates.append(Candidate(m, "yes", yes_price, 1.0 / yes_price))
    if MIN_PRICE <= no_price <= MAX_PRICE:
        candidates.append(Candidate(m, "no", no_price, 1.0 / no_price))
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.multiple)


def scan(min_volume: int, max_days: float, min_hours: float, sort_by: str = "multiple",
         min_multiple: float = 0, max_multiple: float = float("inf"),
         excluded_categories: set[str] = EXCLUDED_CATEGORIES) -> list[Candidate]:
    markets = fetch_open_markets()
    out = []
    for m in markets:
        if m.category in excluded_categories:
            continue
        if m.volume_24h < min_volume:
            continue
        h = m.hours_to_close
        if h < min_hours or h > max_days * 24:
            continue
        c = _best_side(m)
        if c and min_multiple <= c.multiple <= max_multiple:
            out.append(c)
    if sort_by == "volume":
        out.sort(key=lambda c: (c.market.volume_24h, c.multiple), reverse=True)
    else:
        # Kalshi's 1c price floor means the multiple saturates at 100x for
        # thousands of markets (see docstring) — break ties by volume so the
        # top of the list is liquid enough to actually be filled, not arbitrary.
        out.sort(key=lambda c: (c.multiple, c.market.volume_24h), reverse=True)
    return out


def _fmt_row(rank: int, c: Candidate) -> str:
    m = c.market
    title = m.title[:60]
    days = m.hours_to_close / 24
    depth = f"${c.depth_dollars:>7,.0f}" if c.depth_dollars >= 0 else "    n/a"
    return (f"{rank:>3}  {c.multiple:>7.1f}x  {c.price:>5.2f}  {c.side.upper():<3}  "
            f"{m.volume_24h:>8}  {depth}  {days:>6.1f}d  {m.category:<22}  {title}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=int, default=10,
                     help="minimum 24h contract volume, filters dead/stale quotes (default 10)")
    ap.add_argument("--max-days", type=float, default=90,
                     help="exclude markets closing further out than this (default 90)")
    ap.add_argument("--min-hours", type=float, default=1,
                     help="exclude markets closing sooner than this (default 1)")
    ap.add_argument("--limit", type=int, default=40, help="rows to print (default 40)")
    ap.add_argument("--sort", choices=["multiple", "volume"], default="multiple",
                     help="rank by payout multiple (default) or by 24h volume")
    ap.add_argument("--min-payout", type=float, default=0,
                     help="minimum payout multiple, e.g. 80 for 80x (default 0)")
    ap.add_argument("--max-payout", type=float, default=float("inf"),
                     help="maximum payout multiple, e.g. 100 for 100x (default unbounded)")
    ap.add_argument("--exclude", default=",".join(sorted(EXCLUDED_CATEGORIES)),
                     help="comma-separated categories to exclude "
                          f"(default: {','.join(sorted(EXCLUDED_CATEGORIES))})")
    ap.add_argument("--min-depth", type=float, default=0,
                     help="minimum real order-book depth in dollars at the quoted "
                          "price (default 0 = skip the depth check entirely, since "
                          "it costs one API call per candidate). Set this to your "
                          "intended bet size (or higher) to filter out stub quotes.")
    args = ap.parse_args()
    excluded = {c.strip() for c in args.exclude.split(",") if c.strip()}

    results = scan(args.min_volume, args.max_days, args.min_hours, args.sort,
                    args.min_payout, args.max_payout, excluded)
    # Depth check runs only on the post-filter shortlist (top --limit, capped
    # at 100) — checking depth on thousands of raw candidates would mean
    # thousands of orderbook calls for rows nobody will ever look at.
    shortlist = results[:min(args.limit, 100)]
    if args.min_depth > 0:
        add_depth(shortlist)
        shortlist = [c for c in shortlist if c.depth_dollars >= args.min_depth]
        shortlist.sort(key=lambda c: (c.multiple, c.market.volume_24h), reverse=True)

    print(f"{len(results)} candidates after filters "
          f"(excluded categories: {', '.join(sorted(excluded)) or 'none'})")
    if args.min_depth > 0:
        print(f"{len(shortlist)} remain after requiring >=${args.min_depth:,.0f} real depth "
              f"at quoted price (checked top {min(args.limit, 100)} by rank)\n")
    else:
        print()
    header = f"{'#':>3}  {'payout':>8}  {'price':>5}  {'sd':<3}  {'vol24h':>8}  {'depth':>8}  {'close':>7}  {'category':<22}  title"
    print(header)
    print("-" * len(header))
    for i, c in enumerate(shortlist[:args.limit], 1):
        print(_fmt_row(i, c))


if __name__ == "__main__":
    main()
