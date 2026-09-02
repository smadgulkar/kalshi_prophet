"""
Kalshi public market-data client — broad discovery across ALL categories
(unlike longtail's kalshi.py, which is scoped to ~40 weather series).

No auth required for market data (mirrors longtail's design note).

DESIGN NOTE (2026-09-02): discovery uses /events?with_nested_markets=true, NOT
/markets. Measured directly: the flat /markets endpoint is ~98.5% dominated by
Kalshi's auto-generated multivariate/parlay combo markets (KXMVE*-prefixed —
e.g. "Wake Forest wins AND Missouri wins AND ... AND Utah wins" as one
four-way-conjunction contract), which drowned out real single-event markets
under any reasonable page cap. /events does not surface these at all, gives
category and series_ticker directly on the event (no guesswork), and nests
every market's title/rules/bid-ask inline — one paginated call gets everything.
Also measured: /markets' `category` filter and /events' `min_close_ts`/
`max_close_ts` filters are silently no-ops (verified against real responses,
not assumed) — don't rely on server-side filtering here; always filter
client-side on the fields actually returned.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from config import KALSHI_BASE_URL, REQUEST_TIMEOUT, USER_AGENT

log = logging.getLogger("prophet.kalshi")

_session = requests.Session()
_session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})


@dataclass
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str
    category: str
    title: str
    rules_primary: str
    rules_secondary: str
    yes_bid: float
    yes_ask: float
    close_time: str
    status: str
    result: str                # "", "yes", "no"
    volume_24h: int

    @property
    def hours_to_close(self) -> float:
        try:
            closes = datetime.fromisoformat(self.close_time.replace("Z", "+00:00"))
            return (closes - datetime.now(timezone.utc)).total_seconds() / 3600
        except ValueError:
            return 0.0


def _parse_market(raw: dict, series_ticker: str, category: str) -> Optional[Market]:
    try:
        return Market(
            ticker=raw["ticker"],
            event_ticker=raw.get("event_ticker", ""),
            series_ticker=series_ticker,
            category=category,
            title=raw.get("title") or "",
            rules_primary=raw.get("rules_primary") or "",
            rules_secondary=raw.get("rules_secondary") or "",
            yes_bid=float(raw.get("yes_bid_dollars") or 0),
            yes_ask=float(raw.get("yes_ask_dollars") or 0),
            close_time=raw.get("close_time", ""),
            status=raw.get("status", ""),
            result=raw.get("result") or "",
            volume_24h=int(float(raw.get("volume_24h_fp") or 0)),
        )
    except (KeyError, ValueError, TypeError) as e:
        log.warning("unparseable market %s: %s", raw.get("ticker"), e)
        return None


def _get(path: str, params: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = _session.get(f"{KALSHI_BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            log.warning("GET %s failed (attempt %d): %s", path, attempt + 1, e)
            time.sleep(1 + attempt)
    return {}


def fetch_open_markets(max_pages: int = 100) -> list[Market]:
    """Every open market across every category, via /events (see module
    docstring for why not /markets). Measured 2026-09-02: ~13,000 open events /
    ~107,000 nested markets total, fully paginated in ~66 pages — no close-time
    filter applied here (that param is a no-op server-side); callers filter
    client-side on each Market's own close_time / hours_to_close."""
    out: list[Market] = []
    cursor = None
    pages = 0
    for pages in range(1, max_pages + 1):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        data = _get("/events", params)
        for ev in data.get("events", []):
            series_ticker = ev.get("series_ticker", "")
            category = ev.get("category", "Unknown")
            for raw in ev.get("markets", []):
                if raw.get("status") != "active":
                    continue
                m = _parse_market(raw, series_ticker, category)
                if m:
                    out.append(m)
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.05)
    else:
        log.warning("fetch_open_markets hit max_pages=%d with more data available "
                     "(cursor still non-empty) — results are TRUNCATED this cycle", max_pages)
    log.info("fetched %d open markets across all categories (%d event pages)", len(out), pages)
    return out


def fetch_markets_by_tickers(tickers: list[str], chunk_size: int = 20) -> dict[str, dict]:
    """Bulk lookup by ticker for settle — /markets?tickers=A,B,C works fine here
    (verified 2026-09-02), this is unaffected by the combo-market issue above
    since we're asking for specific tickers we already forecasted, not browsing."""
    out: dict[str, dict] = {}
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        data = _get("/markets", {"tickers": ",".join(chunk)})
        for raw in data.get("markets", []):
            out[raw["ticker"]] = raw
        time.sleep(0.1)
    return out


def fetch_series_catalog(max_pages: int = 200) -> dict[str, dict]:
    """Full /series catalog: series_ticker -> {fee_type, fee_multiplier}.
    ~13,700 series as of 2026-09-02 — call this rarely (main.py caches it to disk).
    Category also lives here but we now get category directly from /events, so
    this is only needed for the fee fields."""
    out: dict[str, dict] = {}
    cursor = None
    for _ in range(max_pages):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/series", params)
        for s in data.get("series", []):
            out[s["ticker"]] = {
                "fee_type": s.get("fee_type", "quadratic"),
                "fee_multiplier": float(s.get("fee_multiplier", 1.0)),
            }
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.1)
    log.info("fetched %d series", len(out))
    return out
