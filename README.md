# Kalshi Prophet

Zero-capital shadow-tester for LLM-forecast + proper-betting on Kalshi, per
Gu, Kagan, Sun, Wu, Xu, ["When do prophets profit in prediction markets?"](https://arxiv.org/abs/2607.06166)
(arXiv:2607.06166).

Places **no real orders**. The only real money spent is on Claude API calls
used to generate forecasts, hard-capped by `config.MAX_DAILY_API_SPEND_USD`
and `config.MAX_FORECASTS_PER_CYCLE`.

## Why this exists

Longtail (weather-forecast calibration) and pm_edge (crypto/NBA) both failed
their pre-registered gates — see project memory. This paper found a real,
different angle: on a broad real-market sample, Claude models had a genuine
(if small) accuracy edge over Kalshi's own prices, and — separately — proved
that naive bet sizing (Kelly, highest-margin) can lose money even when a
forecast genuinely beats the market. The fix is "proper betting":
`s_G(p,q) = 2*(p-q)` for the Brier rule — see `proper_betting.py`.

This project measures whether that edge is real for *us*, on live Kalshi
data, before risking a cent of trading capital.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
```

## Usage

```bash
venv/bin/python main.py series-refresh  # fee/category catalog, refresh weekly
venv/bin/python main.py cycle           # discover eligible markets, forecast, log shadow trades
venv/bin/python main.py settle          # reconcile outcomes for closed markets
venv/bin/python main.py report          # calibration + shadow-P&L report: us vs the market
```

## Design notes (see code comments for the full derivations)

- **Discovery** uses `/events?with_nested_markets=true`, not `/markets` — the
  flat `/markets` endpoint is ~98.5% dominated by Kalshi's auto-generated
  multivariate/parlay combo markets (measured 2026-09-02). `/events` doesn't
  surface these and gives category + series_ticker directly, no guessing.
- **Eligibility** (paper Section D.7.2): excludes the `Mentions` category
  (LLMs perform worse there), halts forecasting within 3 hours of close, caps
  at 14 days out. Markets with an ambiguous/discretionary resolution rule are
  flagged by the forecaster itself and excluded from trading and calibration
  stats, not silently forced into a probability.
- **Re-forecast cadence**: skip a market already forecasted in the last 24h
  unless price moved >=10c — cost control on API spend, not in the paper
  (which used this same heuristic for live-trading fee/variance control).
- **Fees**: real Kalshi taker formula `ceil(100 * fee_multiplier * 0.07 * C *
  P * (1-P)) / 100`, with the actual per-series `fee_multiplier` from
  `/series` (0, 0.5, or 1 — NOT flat 0.07 for everything).
- **Position sizing** (`proper_betting.size_contracts`): only the *direction*
  and *relative* size of `s_G(p,q)` are proven robustly profitable. The
  absolute dollar scale is a risk-management free parameter — this applies
  the house's standing 2%-per-bet cap, not part of the paper's theorem.
- **Report caveat**: the score-gap confidence interval is a plain
  mean +/- 1.96*SE, NOT event-clustered like longtail's bootstrap. Markets on
  the same underlying event can be correlated — treat early CIs as optimistic.

## What "done" looks like before real money

Per house discipline (see `feedback_strategy_validation` memory): a real
sample of settled shadow trades, fee-adjusted ROI with a genuinely positive
CI, checked against category breakdown (don't let one lucky category carry
the whole result) — before any of this touches a live order.
