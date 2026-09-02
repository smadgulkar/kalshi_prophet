"""
Kalshi Prophet — zero-capital shadow-tester for LLM-forecast + proper-betting,
per Gu, Kagan, Sun, Wu, Xu, "When do prophets profit in prediction markets?"
(arXiv:2607.06166). Places NO real orders. Spends real money only on the
Claude API calls used to generate forecasts (see MAX_DAILY_API_SPEND_USD).
"""

import os
from pathlib import Path

_ENV_PATH = Path(__file__).parent / ".env"


def _load_env() -> None:
    """ANTHROPIC_API_KEY comes from .env (gitignored) — same pattern as longtail/notify.py."""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


_load_env()

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT = 30
USER_AGENT = "kalshi-prophet/0.1 (calibration research)"

DB_PATH = "data/prophet.db"
LOG_PATH = "logs/prophet.log"
SERIES_CACHE_PATH = "data/series_cache.json"
SERIES_CACHE_MAX_AGE_DAYS = 7

# --- Eligibility filter (paper Section D.7.2 + D.7.1) ---
# Halt forecasting within this many hours of close — paper's own reason:
# "reduced informational advantage of LLMs close to resolution time."
MIN_HOURS_TO_CLOSE = 3.0
# Paper's live-trading cap was 14 days for capital-turnover reasons, which
# don't apply to a shadow tester; kept as the default anyway since it's the
# one horizon the paper actually validated. Raise it deliberately, not by default.
MAX_DAYS_TO_CLOSE = 14
EXCLUDED_CATEGORIES = {"Mentions"}  # paper: LLMs perform substantially worse on these

# --- Re-forecast cadence (cost control, not in the paper — their live agent
# used this same 10-cent-move heuristic to limit trading fee/variance exposure;
# we repurpose it to limit API spend on redundant re-forecasts) ---
REFORECAST_MIN_PRICE_MOVE = 0.10
REFORECAST_MAX_AGE_HOURS = 24.0

# --- Forecaster (Claude API) ---
FORECASTER_MODEL = "claude-opus-5"
FORECASTER_EFFORT = "high"          # paper's Gemini deployment used high-effort thinking
WEB_SEARCH_MAX_USES = 3
FORECASTER_MAX_TOKENS = 4000

# claude-opus-5 pricing, $/1M tokens (see ref_kalshi_research memory / claude-api skill, cached 2026-06-24)
PRICE_INPUT_PER_MTOK = 5.00
PRICE_OUTPUT_PER_MTOK = 25.00

# Hard spend caps — independent safety valves, both enforced every cycle.
MAX_DAILY_API_SPEND_USD = 5.00
MAX_FORECASTS_PER_CYCLE = 60

# --- Shadow position sizing (risk-management free parameter — NOT part of the
# proven theorem, see proper_betting.size_contracts docstring) ---
SHADOW_BANKROLL_USD = 200.0   # mirrors the paper's live-deployment starting bankroll
MAX_BET_FRACTION = 0.02       # standing house rule: 2% of bankroll per bet, see memory
