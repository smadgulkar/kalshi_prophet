#!/usr/bin/env python3
"""
fav_logger.py  --  forward PAPER test of "buy the favorite as taker" on Kalshi.
Places NO orders, needs NO credentials, spends nothing.

Hypothesis (favorite-longshot bias, measured 2026-10-18 on ~2.3k settled markets,
Oct 12-18): buying the 80-98c side at the posted ask earns a positive return net
of Kalshi's taker fee, across categories. If real, a $50 bankroll compounded at
5-10% per bet grows ~10x in ~1k bets; if not, nothing here should go live.

Method (fixed up front, do not change mid-test -- add columns, never re-pick):
  - scan: every open market RESOLVING within MAX_HOURS, where resolve_by =
    min(close_time, expected_expiration_time) -- sports set close_time ~2 weeks
    out and close early when decided, so close_time alone hides them. A side qualifies if its
    ask is in [MIN_PRICE, MAX_PRICE] and the opposite bid is real (>0).
  - ONE paper bet per market, logged the FIRST time it qualifies (no cherry-
    picking a later, better-looking price). If both sides qualify (only possible
    on a wide book) the cheaper side is taken.
  - Size: STAKE_USD notional -> floor(STAKE_USD/price) contracts (>=1), real fee
    formula with the series' fee_multiplier. Book depth at the ask is recorded;
    bets where the ask can't fill our size are excluded from the gate.
  - settle: outcome from Kalshi's own market `result` (voids -> refunded, excluded).

PRE-REGISTERED GATE (2026-10-18) -- before any real money:
  >= 300 settled, depth-fillable bets spanning >= 10 distinct settle days, AND
  fee-adjusted ROI 95% bootstrap CI fully > 0 under BOTH event clustering and
  settle-DAY clustering (added 2026-10-18 before any bet settled: one game spawns
  moneyline/spread/total/prop markets under different event tickers, so event
  clustering alone overstates independence).
  Live sizing if passed: <= 10% of bankroll per bet, reconcile via Kalshi
  /portfolio/settlements only.

Usage:
  python fav_logger.py scan      # one scan pass (systemd timer runs this every 5 min)
  python fav_logger.py settle
  python fav_logger.py report
"""

import argparse
import logging
import math
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from kalshi import fetch_book_depth, fetch_markets_by_tickers, fetch_open_markets
from main import load_series_cache
from proper_betting import taker_fee_dollars

DB_PATH = "data/fav_logger.db"
LOG_PATH = "logs/fav_logger.log"

MIN_PRICE = 0.80
MAX_PRICE = 0.98
MAX_HOURS = 24.0
STAKE_USD = 5.0

GATE_MIN_BETS = 300
GATE_MIN_DAYS = 10

log = logging.getLogger("fav")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    ticker TEXT PRIMARY KEY,           -- one bet per market, ever
    event_ticker TEXT NOT NULL,
    series_ticker TEXT,
    category TEXT,
    title TEXT,
    placed_ts TEXT NOT NULL,
    close_time TEXT NOT NULL,
    resolve_by TEXT NOT NULL,          -- min(close_time, expected_expiration_time)
    hours_to_close REAL NOT NULL,      -- hours to resolve_by at entry
    side TEXT NOT NULL,                -- "yes" | "no"
    price REAL NOT NULL,               -- ask paid
    contracts INTEGER NOT NULL,
    fee REAL NOT NULL,
    fee_multiplier REAL NOT NULL,
    yes_bid REAL NOT NULL,
    yes_ask REAL NOT NULL,
    volume_24h INTEGER,
    depth_contracts REAL,              -- size resting at our ask when logged
    result TEXT,                       -- NULL until settled; "yes" | "no" | "void"
    pnl REAL,
    settled_ts TEXT
);
CREATE INDEX IF NOT EXISTS bets_unsettled ON bets(result) WHERE result IS NULL;
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def qualifying_side(m):
    """(side, ask) for the favorite side priced in [MIN_PRICE, MAX_PRICE], else None.
    YES ask is real only if a NO bid rests behind it (yes_ask < 1); NO ask = 1 - yes_bid
    is real only if yes_bid > 0 -- a 0 bid would read as a fake $1.00 NO ask."""
    sides = []
    if 0 < m.yes_ask < 1 and MIN_PRICE <= m.yes_ask <= MAX_PRICE:
        sides.append(("yes", m.yes_ask))
    if m.yes_bid > 0 and MIN_PRICE <= 1 - m.yes_bid <= MAX_PRICE:
        sides.append(("no", round(1 - m.yes_bid, 4)))
    return min(sides, key=lambda s: s[1]) if sides else None


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def resolve_by(m) -> str:
    times = [t for t in (m.close_time, m.expected_expiration_time) if t]
    return min(times, key=parse_ts)


def cmd_scan() -> None:
    series = load_series_cache()
    markets = fetch_open_markets()
    conn = connect()
    seen = {r[0] for r in conn.execute("SELECT ticker FROM bets")}
    new = 0
    now = datetime.now(timezone.utc)
    for m in markets:
        if m.ticker in seen or not m.close_time:
            continue
        rb = resolve_by(m)
        hours = (parse_ts(rb) - now).total_seconds() / 3600
        if not 0 < hours <= MAX_HOURS:
            continue
        q = qualifying_side(m)
        if not q:
            continue
        side, price = q
        mult = series.get(m.series_ticker, {}).get("fee_multiplier", 1.0)
        contracts = max(1, math.floor(STAKE_USD / price))
        try:
            depth, _ = fetch_book_depth(m.ticker, side)
        except Exception as e:  # depth is advisory; log the bet anyway, flagged unknown
            log.warning("depth fetch failed %s: %s", m.ticker, e)
            depth = None
        conn.execute(
            "INSERT OR IGNORE INTO bets (ticker, event_ticker, series_ticker, category, title, placed_ts,"
            " close_time, resolve_by, hours_to_close, side, price, contracts, fee, fee_multiplier, yes_bid,"
            " yes_ask, volume_24h, depth_contracts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m.ticker, m.event_ticker, m.series_ticker, m.category, m.title, now_iso(), m.close_time, rb,
             round(hours, 3), side, price, contracts, taker_fee_dollars(contracts, price, mult),
             mult, m.yes_bid, m.yes_ask, m.volume_24h, depth))
        conn.commit()  # per-row: a crash mid-scan keeps what was logged
        new += 1
        time.sleep(0.05)
    log.info("scan: %d open markets, %d new paper bets", len(markets), new)


def cmd_settle() -> None:
    conn = connect()
    now = datetime.now(timezone.utc)
    pending = [r for r in conn.execute(
        "SELECT ticker, side, price, contracts, fee, resolve_by FROM bets WHERE result IS NULL")
        if parse_ts(r[5]) <= now]
    raw = fetch_markets_by_tickers([r[0] for r in pending])
    done = 0
    for ticker, side, price, contracts, fee, _ in pending:
        m = raw.get(ticker)
        if not m or m.get("status") not in ("settled", "finalized"):
            continue  # past expected resolution but not determined yet (delays happen); retry
        result = m.get("result") or ""
        if result in ("yes", "no"):
            pnl = contracts * ((1.0 if result == side else 0.0) - price) - fee
        else:
            result, pnl = "void", 0.0
        conn.execute("UPDATE bets SET result=?, pnl=?, settled_ts=? WHERE ticker=?",
                     (result, round(pnl, 4), now_iso(), ticker))
        done += 1
    conn.commit()
    log.info("settle: %d past close, %d settled now", len(pending), done)


def clustered_roi_ci(rows, n_boot=1000, seed=0):
    """ROI = sum(pnl)/sum(cost), bootstrap resampling whole EVENTS (buckets of one
    event, and one game's markets, settle on the same fact -- not independent)."""
    by_ev = defaultdict(lambda: [0.0, 0.0])
    for ev, pnl, cost in rows:
        by_ev[ev][0] += pnl
        by_ev[ev][1] += cost
    evs = list(by_ev.values())
    roi = sum(e[0] for e in evs) / sum(e[1] for e in evs)
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        s = [rng.choice(evs) for _ in evs]
        boots.append(sum(e[0] for e in s) / sum(e[1] for e in s))
    boots.sort()
    return roi, boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1], len(evs)


def cmd_report() -> None:
    conn = connect()
    total, open_ = conn.execute("SELECT count(*), sum(result IS NULL) FROM bets").fetchone()
    rows = conn.execute(
        "SELECT event_ticker, category, price, hours_to_close, contracts, fee, pnl, settled_ts, "
        "depth_contracts, placed_ts, resolve_by FROM bets WHERE result IN ('yes','no')").fetchall()
    print(f"=== fav_logger report {now_iso()} ===")
    print(f"paper bets logged {total or 0}, open {open_ or 0}, settled (non-void) {len(rows)}")
    fillable = [r for r in rows if r[8] is not None and r[8] >= r[4]]
    if not fillable:
        print("no settled fillable bets yet")
        return

    def line(label, rs):
        if len(rs) < 2:
            return
        roi, lo, hi, ne = clustered_roi_ci([(r[0], r[6], r[4] * r[2] + r[5]) for r in rs])
        wins = sum(r[6] > 0 for r in rs)
        print(f"  {label:28s} n={len(rs):5d} events={ne:4d} win={wins/len(rs):6.1%} "
              f"ROI {roi:+6.2%} [{lo:+.2%}, {hi:+.2%}]")

    days = {r[7][:10] for r in fillable}
    roi, lo, hi, ne = clustered_roi_ci([(r[0], r[6], r[4] * r[2] + r[5]) for r in fillable])
    _, dlo, dhi, _ = clustered_roi_ci([(r[7][:10], r[6], r[4] * r[2] + r[5]) for r in fillable])
    print(f"\nGATE: fillable bets {len(fillable)}/{GATE_MIN_BETS}, settle days {len(days)}/{GATE_MIN_DAYS}, "
          f"ROI {roi:+.2%}  event-CI [{lo:+.2%}, {hi:+.2%}] ({ne} events)  day-CI [{dlo:+.2%}, {dhi:+.2%}]")
    passed = len(fillable) >= GATE_MIN_BETS and len(days) >= GATE_MIN_DAYS and lo > 0 and dlo > 0
    print("  -> PASS" if passed else "  -> NOT PASSED (no real money)")

    print("\nby category:")
    for cat in sorted({r[1] for r in fillable}):
        line(cat or "?", [r for r in fillable if r[1] == cat])
    print("by price:")
    for a, b in ((0.80, 0.90), (0.90, 0.95), (0.95, 0.981)):
        line(f"{a:.2f}-{min(b, .98):.2f}", [r for r in fillable if a <= r[2] < b])
    print("by hours to close at entry:")
    for a, b in ((0, 1), (1, 6), (6, 24.01)):
        line(f"{a}-{int(b)}h", [r for r in fillable if a <= r[3] < b])
    print("by settle day:")
    for d in sorted(days):
        line(d, [r for r in fillable if r[7][:10] == d])

    # Compounding replay: $50 start, stake FRAC of current cash at placement, in placement
    # order, capital locked until resolve_by (approximate: actual payout can lag). A preview of the live plan, not part of the gate.
    for frac in (0.05, 0.10):
        cash, locked = 50.0, []  # locked: (close_time, payout)
        for r in sorted(fillable, key=lambda r: r[9]):
            for c in [x for x in locked if parse_ts(x[0]) <= parse_ts(r[9])]:
                cash += c[1]
                locked.remove(c)
            stake = frac * (cash + sum(x[2] for x in locked))
            unit = r[2] + r[5] / r[4]                # cost per contract incl. fee share
            n = math.floor(min(stake, cash) / unit)
            if n < 1:
                continue
            cash -= n * unit
            payout = n * unit + r[6] / r[4] * n      # cost back + per-contract pnl
            locked.append((r[10], payout, n * unit))
        cash += sum(x[1] for x in locked)
        print(f"compounding replay @ {frac:.0%}/bet: $50 -> ${cash:,.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "settle", "report"])
    args = ap.parse_args()
    if args.cmd != "report":
        setup_logging()
    {"scan": cmd_scan, "settle": cmd_settle, "report": cmd_report}[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
