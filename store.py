"""SQLite persistence for forecasts and settlement outcomes."""

import logging
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

log = logging.getLogger("prophet.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY,
    run_ts TEXT NOT NULL,              -- UTC ISO timestamp of forecast cycle
    ticker TEXT NOT NULL,
    event_ticker TEXT NOT NULL,
    series_ticker TEXT,                -- NULL if resolution against /series catalog failed
    category TEXT,
    title TEXT NOT NULL,
    close_time TEXT NOT NULL,
    hours_to_close REAL NOT NULL,

    -- market state at forecast time
    yes_bid REAL NOT NULL,
    yes_ask REAL NOT NULL,

    -- our forecast
    p_yes REAL NOT NULL,
    rationale TEXT,
    resolution_ambiguous INTEGER NOT NULL DEFAULT 0,
    ambiguity_reason TEXT,

    -- trade signal (proper_betting.decide_trade / size_contracts)
    side TEXT,                         -- "yes" | "no" | NULL (no actionable margin)
    ref_price REAL,                    -- crossed price used for sizing/scoring
    edge REAL NOT NULL,                -- s_G(p, ref_price) = 2*(p - ref_price)
    contracts INTEGER NOT NULL DEFAULT 0,
    fee_multiplier REAL NOT NULL DEFAULT 1.0,

    -- API cost accounting
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    api_cost_usd REAL,

    -- settlement (filled by settle command)
    settled INTEGER NOT NULL DEFAULT 0,
    outcome INTEGER,                   -- 1 yes / 0 no
    settled_ts TEXT,
    score_gap REAL,                    -- brier_score(p,y) - brier_score(ref_price,y), only if traded
    divergence REAL,                   -- bregman_divergence_brier(ref_price, p), only if traded
    fee_paid_usd REAL,
    realized_pnl_usd REAL,

    UNIQUE(run_ts, ticker)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_unsettled ON forecasts(settled);
CREATE INDEX IF NOT EXISTS idx_forecasts_ticker ON forecasts(ticker);
CREATE INDEX IF NOT EXISTS idx_forecasts_run_ts ON forecasts(run_ts);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_forecast(conn: sqlite3.Connection, row: dict) -> None:
    cols = ", ".join(row)
    ph = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT OR IGNORE INTO forecasts ({cols}) VALUES ({ph})",
        list(row.values()),
    )


def last_forecast_for_ticker(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT run_ts, yes_bid, yes_ask FROM forecasts WHERE ticker = ? ORDER BY run_ts DESC LIMIT 1",
        (ticker,),
    ).fetchone()


def today_api_spend_usd(conn: sqlite3.Connection, date_str: str) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(api_cost_usd), 0) AS s FROM forecasts WHERE run_ts LIKE ?",
        (f"{date_str}%",),
    ).fetchone()
    return row["s"]


def mark_settled(conn: sqlite3.Connection, ticker: str, outcome: int, settled_ts: str,
                  score_gap, divergence, fee_paid_usd, realized_pnl_usd) -> int:
    cur = conn.execute(
        "UPDATE forecasts SET settled = 1, outcome = ?, settled_ts = ?, "
        "score_gap = ?, divergence = ?, fee_paid_usd = ?, realized_pnl_usd = ? "
        "WHERE ticker = ? AND settled = 0",
        (outcome, settled_ts, score_gap, divergence, fee_paid_usd, realized_pnl_usd, ticker),
    )
    return cur.rowcount
