"""
Kalshi Prophet — zero-capital shadow-tester for LLM-forecast + proper-betting.
Places NO real orders. Spends real money only on Claude API calls (capped,
see config.MAX_DAILY_API_SPEND_USD).

Usage:
    python main.py series-refresh  # (re)fetch the /series category+fee catalog
    python main.py cycle           # discover eligible markets, forecast, log shadow trades
    python main.py settle          # reconcile outcomes for closed markets
    python main.py report          # calibration + shadow-P&L report: us vs the market
"""

import argparse
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

from config import (
    EXCLUDED_CATEGORIES, FORECASTER_MODEL, LOG_PATH, MAX_BET_FRACTION,
    MAX_DAILY_API_SPEND_USD, MAX_DAYS_TO_CLOSE, MAX_FORECASTS_PER_CYCLE,
    MAX_TOTAL_API_SPEND_USD, MIN_HOURS_TO_CLOSE, REFORECAST_MAX_AGE_HOURS,
    REFORECAST_MIN_PRICE_MOVE, SERIES_CACHE_MAX_AGE_DAYS, SERIES_CACHE_PATH,
    SHADOW_BANKROLL_USD,
)
from forecaster import forecast_market
from kalshi import fetch_markets_by_tickers, fetch_open_markets, fetch_series_catalog
from proper_betting import decide_trade, realized_pnl, size_contracts, brier_score, bregman_divergence_brier, taker_fee_dollars
from store import (
    db, insert_forecast, last_forecast_for_ticker, mark_settled,
    today_api_spend_usd, total_api_spend_usd,
)

log = logging.getLogger("prophet")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def load_series_cache(force: bool = False) -> dict:
    if not force and os.path.exists(SERIES_CACHE_PATH):
        with open(SERIES_CACHE_PATH) as f:
            cached = json.load(f)
        fetched = datetime.fromisoformat(cached["fetched_ts"])
        if datetime.now(timezone.utc) - fetched < timedelta(days=SERIES_CACHE_MAX_AGE_DAYS):
            return cached["series"]
    log.info("refreshing series catalog (~13k series, may take a minute)...")
    series = fetch_series_catalog()
    os.makedirs(os.path.dirname(SERIES_CACHE_PATH), exist_ok=True)
    with open(SERIES_CACHE_PATH, "w") as f:
        json.dump({"fetched_ts": datetime.now(timezone.utc).isoformat(), "series": series}, f)
    log.info("series catalog refreshed: %d series", len(series))
    return series


def cmd_series_refresh() -> None:
    load_series_cache(force=True)


def cmd_cycle() -> None:
    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    series_cache = load_series_cache()

    now = datetime.now(timezone.utc)
    max_days_ts = int((now + timedelta(days=MAX_DAYS_TO_CLOSE)).timestamp())
    markets = fetch_open_markets()

    eligible = []
    for m in markets:
        if m.category in EXCLUDED_CATEGORIES:
            continue
        if m.yes_bid <= 0 and m.yes_ask <= 0:
            continue  # empty book, nothing to trade against
        if m.hours_to_close < MIN_HOURS_TO_CLOSE:
            continue
        if datetime.fromisoformat(m.close_time.replace("Z", "+00:00")).timestamp() > max_days_ts:
            continue
        eligible.append(m)

    # Random shuffle before capping to MAX_FORECASTS_PER_CYCLE — pagination order
    # is NOT representative (Sports alone is ~27k of ~35k eligible markets most
    # cycles; without this, the daily forecast/API budget would be spent almost
    # entirely on Sports and never reach thinner categories like Politics/Elections).
    random.shuffle(eligible)
    log.info("cycle: %d open markets fetched, %d eligible after filtering", len(markets), len(eligible))

    forecasted = skipped_cadence = errors = 0
    today = run_ts[:10]

    with db() as conn:
        spend_today = today_api_spend_usd(conn, today)
        spend_total = total_api_spend_usd(conn)

        for m in eligible:
            series_ticker, category = m.series_ticker, m.category
            if forecasted >= MAX_FORECASTS_PER_CYCLE:
                log.info("hit MAX_FORECASTS_PER_CYCLE (%d), stopping", MAX_FORECASTS_PER_CYCLE)
                break
            if spend_today >= MAX_DAILY_API_SPEND_USD:
                log.info("hit MAX_DAILY_API_SPEND_USD ($%.2f), stopping", MAX_DAILY_API_SPEND_USD)
                break
            if spend_total >= MAX_TOTAL_API_SPEND_USD:
                log.info("hit MAX_TOTAL_API_SPEND_USD ($%.2f) — lifetime cap, stopping "
                          "for good until config is raised", MAX_TOTAL_API_SPEND_USD)
                break

            last = last_forecast_for_ticker(conn, m.ticker)
            if last is not None:
                last_ts = datetime.fromisoformat(last["run_ts"])
                age_h = (now - last_ts).total_seconds() / 3600
                price_moved = (abs(m.yes_bid - last["yes_bid"]) >= REFORECAST_MIN_PRICE_MOVE
                               or abs(m.yes_ask - last["yes_ask"]) >= REFORECAST_MIN_PRICE_MOVE)
                if age_h < REFORECAST_MAX_AGE_HOURS and not price_moved:
                    skipped_cadence += 1
                    continue

            try:
                fc = forecast_market(m)
            except Exception as e:  # noqa: BLE001 — one bad market must not kill the whole cycle
                log.warning("forecast failed for %s: %s", m.ticker, e)
                errors += 1
                continue

            spend_today += fc.cost_usd
            spend_total += fc.cost_usd
            fee_multiplier = series_cache.get(series_ticker, {}).get("fee_multiplier", 1.0) if series_ticker else 1.0

            if fc.resolution_ambiguous:
                side = ref_price = None
                edge = 0.0
                contracts = 0
            else:
                sig = decide_trade(fc.p_yes, m.yes_bid, m.yes_ask)
                side, ref_price, edge = sig.side, sig.ref_price, sig.edge
                contracts = (
                    size_contracts(edge, ref_price, SHADOW_BANKROLL_USD, MAX_BET_FRACTION)
                    if side else 0
                )

            insert_forecast(conn, {
                "run_ts": run_ts,
                "ticker": m.ticker,
                "event_ticker": m.event_ticker,
                "series_ticker": series_ticker,
                "category": category,
                "title": m.title,
                "close_time": m.close_time,
                "hours_to_close": round(m.hours_to_close, 2),
                "yes_bid": m.yes_bid,
                "yes_ask": m.yes_ask,
                "p_yes": fc.p_yes,
                "rationale": fc.rationale,
                "resolution_ambiguous": 1 if fc.resolution_ambiguous else 0,
                "ambiguity_reason": fc.ambiguity_reason,
                "side": side,
                "ref_price": ref_price,
                "edge": edge,
                "contracts": contracts,
                "fee_multiplier": fee_multiplier,
                "model": FORECASTER_MODEL,
                "input_tokens": fc.input_tokens,
                "output_tokens": fc.output_tokens,
                "api_cost_usd": fc.cost_usd,
            })
            conn.commit()  # commit per-row: a paid-for forecast must survive a mid-cycle
                            # crash/kill, or the spend ledger silently undercounts real cost
            forecasted += 1

    log.info("cycle done: %d forecasted, %d skipped (cadence), %d errors, "
              "$%.4f spent today, $%.4f spent lifetime (cap $%.2f)",
              forecasted, skipped_cadence, errors, spend_today, spend_total, MAX_TOTAL_API_SPEND_USD)


def cmd_settle() -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as conn:
        tickers = [r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM forecasts WHERE settled = 0"
        ).fetchall()]
        if not tickers:
            log.info("settle: nothing pending")
            return

        settled_markets = fetch_markets_by_tickers(tickers)
        updated = 0
        for ticker, m in settled_markets.items():
            result = m.get("result") or ""
            if result not in ("yes", "no"):
                continue
            outcome_yes = result == "yes"
            outcome = 1 if outcome_yes else 0

            rows = conn.execute(
                "SELECT * FROM forecasts WHERE ticker = ? AND settled = 0", (ticker,)
            ).fetchall()
            for r in rows:
                score_gap = divergence = fee_paid = pnl = None
                if r["side"] and r["contracts"] and r["ref_price"] is not None:
                    score_gap = brier_score(r["p_yes"], outcome_yes) - brier_score(r["ref_price"], outcome_yes)
                    divergence = bregman_divergence_brier(r["ref_price"], r["p_yes"])
                    fee_paid = taker_fee_dollars(r["contracts"], r["ref_price"], r["fee_multiplier"])
                    pnl = realized_pnl(r["side"], r["contracts"], r["ref_price"], outcome_yes, r["fee_multiplier"])
                mark_settled(conn, ticker, outcome, now, score_gap, divergence, fee_paid, pnl)
                updated += 1
        log.info("settle done: %d forecast rows resolved", updated)


def cmd_report() -> None:
    from report import report
    with db() as conn:
        print(report(conn))


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi Prophet — LLM-forecast + proper-betting shadow tester")
    parser.add_argument("command", choices=["series-refresh", "cycle", "settle", "report"])
    args = parser.parse_args()
    setup_logging()
    {
        "series-refresh": cmd_series_refresh,
        "cycle": cmd_cycle,
        "settle": cmd_settle,
        "report": cmd_report,
    }[args.command]()


if __name__ == "__main__":
    main()
