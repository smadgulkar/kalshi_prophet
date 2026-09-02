"""
Proper betting math for binary Kalshi markets, per Gu, Kagan, Sun, Wu, Xu
"When do prophets profit in prediction markets?" (arXiv:2607.06166).

Core result (Theorem 1/2): for the Brier/quadratic scoring rule, the ONLY
robustly-profitable betting strategy is s_G(p,q) = grad G(p) - grad G(q) with
G(p) = ||p||^2. Kelly and highest-margin betting are both proven NOT robustly
profitable (their Examples 1-2) even when the forecast genuinely beats the
market — do not reintroduce either.

Binary reduction (K=2, p = (p_yes, 1-p_yes), q likewise): the vector formula
collapses to a single scalar edge, s_yes = 2*(p_yes - q_yes).

Real bid-ask spreads (paper Section 3.3.4): decompose each outcome into two
priced sides, q+ = yes_ask (cost to buy YES) and q- = 1 - yes_bid (cost to buy
NO). Proper betting buys YES when p > yes_ask, buys NO when p < yes_bid, and
does not trade when yes_bid <= p <= yes_ask (no actionable margin — this is
the paper's own live-deployment rule, Section D.7.1).
"""

import math
from dataclasses import dataclass
from typing import Optional

# Kalshi's published taker fee: fee = ceil(100 * multiplier * 0.07 * C * P * (1-P)) / 100,
# rounded UP to the next cent, applied once per ORDER (not per contract).
# Source: Kalshi fee schedule (verified 2026-09-02 via web search); per-series
# fee_multiplier (0, 0.5, or 1) comes from Kalshi's own /series metadata — see kalshi.py.
TAKER_FEE_RATE = 0.07


def taker_fee_dollars(contracts: int, price: float, fee_multiplier: float = 1.0) -> float:
    """Dollar fee for one taker order of `contracts` at `price` (0..1)."""
    if contracts <= 0 or fee_multiplier <= 0:
        return 0.0
    raw = 100 * fee_multiplier * TAKER_FEE_RATE * contracts * price * (1 - price)
    # round off float dust (e.g. 100@$0.50 should be exactly $1.75, not $1.76)
    # before ceiling, or IEEE-754 imprecision inflates the fee by a cent.
    return math.ceil(round(raw, 6)) / 100.0


def brier_score(p_yes: float, outcome_yes: bool) -> float:
    """S(p,y) = -(y-p)^2, the standard scalar Brier score for ONE side of a
    real bid-ask-spread market. This is NOT Table 1's ||1_y - p||^2 (that's the
    full 2-coordinate simplex vector, used only in the frictionless/zero-spread
    case of Theorem 1). Our trades are single-sided (decide_trade only ever
    buys one of YES/NO, never both) — the correct decomposition for that is
    Corollary 3 ("bid-ask-aware", Eq. 5)'s PER-COORDINATE reduction, which uses
    the plain scalar convention with no factor of 2. Verified by hand: with
    p=0.7, q=0.55 (ask crossed), y=yes: edge*(1-q) = 2*(0.15)*(0.45) = 0.135,
    and [S(p,y)-S(q,y)] + D_G(q,p) = [(0.2025-0.09)] + 0.0225 = 0.135. Match.
    Using the doubled Table-1 convention here would silently overstate every
    score-gap and divergence figure in `report` by 2x — do not reintroduce it."""
    err = (1.0 if outcome_yes else 0.0) - p_yes
    return -(err * err)


def bregman_divergence_brier(q_yes: float, p_yes: float) -> float:
    """D_G(q,p) = (q-p)^2 for the single-coordinate binary reduction (Corollary 3).
    Always >= 0. See brier_score's docstring for why there's no factor of 2 here."""
    d = q_yes - p_yes
    return d * d


@dataclass
class TradeSignal:
    side: Optional[str]     # "yes" | "no" | None (inside the spread — no actionable margin)
    ref_price: Optional[float]  # the crossed price: yes_ask for "yes", (1-yes_bid) for "no"
    edge: float              # s_G(p, ref_price) = 2*(p - ref_price), the proper-betting signal magnitude


def decide_trade(p_yes: float, yes_bid: float, yes_ask: float) -> TradeSignal:
    """Paper Section 3.3.4 / D.7.1: buy YES if p > ask, buy NO if p < bid, else no trade."""
    if p_yes > yes_ask:
        return TradeSignal("yes", yes_ask, 2.0 * (p_yes - yes_ask))
    if p_yes < yes_bid:
        no_price = 1.0 - yes_bid
        return TradeSignal("no", no_price, 2.0 * (yes_bid - p_yes))
    return TradeSignal(None, None, 0.0)


def size_contracts(
    edge: float,
    ref_price: float,
    bankroll: float,
    max_bet_fraction: float,
    full_edge_at: float = 0.5,
) -> int:
    """Dollar-scale a proper-betting signal into a contract count.

    IMPORTANT: only the DIRECTION and RELATIVE size of s_G(p,q) are proven
    robustly profitable (Remark 5: "larger bets on events where prediction
    deviates more from market"). The absolute dollar scale is a risk-management
    free parameter, not part of the theorem — this applies the house's
    standing 2%-per-bet discipline (see ref_kalshi_research memory), scaling
    linearly from 0 up to the full max_bet_fraction as |p-q| goes from 0 to
    `full_edge_at` (0.5 = the maximum possible probability gap).
    """
    if ref_price is None or ref_price <= 0 or ref_price >= 1 or edge == 0:
        return 0
    scale = min(1.0, abs(edge) / (2.0 * full_edge_at))  # edge = 2*(p-q), so |p-q| = |edge|/2
    dollars = bankroll * max_bet_fraction * scale
    return int(dollars // ref_price)


def realized_pnl(side: str, contracts: int, ref_price: float, outcome_yes: bool, fee_multiplier: float = 1.0) -> float:
    """Cash P&L of a filled taker order, net of the real Kalshi fee."""
    won = outcome_yes if side == "yes" else (not outcome_yes)
    payout = contracts * 1.0 if won else 0.0
    cost = contracts * ref_price
    fee = taker_fee_dollars(contracts, ref_price, fee_multiplier)
    return payout - cost - fee
