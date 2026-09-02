"""Calibration + shadow-P&L report: us vs the market, per Corollary 2/3 decomposition."""

import math
import sqlite3


def _mean_se(xs: list[float]) -> tuple[float, float, int]:
    n = len(xs)
    if n == 0:
        return 0.0, 0.0, 0
    mean = sum(xs) / n
    if n < 2:
        return mean, 0.0, n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    se = math.sqrt(var / n)
    return mean, se, n


def report(conn: sqlite3.Connection) -> str:
    lines = []

    total = conn.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
    settled = conn.execute("SELECT COUNT(*) c FROM forecasts WHERE settled = 1").fetchone()["c"]
    ambiguous = conn.execute("SELECT COUNT(*) c FROM forecasts WHERE resolution_ambiguous = 1").fetchone()["c"]
    days = conn.execute("SELECT COUNT(DISTINCT substr(run_ts,1,10)) c FROM forecasts").fetchone()["c"]
    total_cost = conn.execute("SELECT COALESCE(SUM(api_cost_usd),0) c FROM forecasts").fetchone()["c"]
    lines.append(f"Forecasts issued: {total}  |  settled: {settled}  |  distinct days: {days}  |  "
                 f"ambiguous (excluded from trading): {ambiguous}")
    lines.append(f"Cumulative API spend: ${total_cost:.2f}")
    lines.append("")

    # --- Calibration: bin p_yes, compare to realized frequency (ALL settled forecasts, traded or not) ---
    rows = conn.execute(
        "SELECT p_yes, outcome FROM forecasts WHERE settled = 1 AND resolution_ambiguous = 0"
    ).fetchall()
    if rows:
        bins = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
                (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.0)]
        lines.append(f"{'bin':>12}{'n':>8}{'avg p':>10}{'observed':>10}")
        for lo, hi in bins:
            in_bin = [r for r in rows if lo <= r["p_yes"] < hi or (hi == 1.0 and r["p_yes"] == 1.0)]
            if not in_bin:
                continue
            avg_p = sum(r["p_yes"] for r in in_bin) / len(in_bin)
            observed = sum(r["outcome"] for r in in_bin) / len(in_bin)
            lines.append(f"{lo:>5.0%}-{hi:<5.0%}{len(in_bin):>8}{avg_p:>10.3f}{observed:>10.3f}")
        lines.append("")

    # --- Shadow trading: only rows where a side was signaled ---
    traded = conn.execute(
        "SELECT * FROM forecasts WHERE settled = 1 AND side IS NOT NULL AND contracts > 0"
    ).fetchall()
    if not traded:
        lines.append("No settled shadow trades yet (need side IS NOT NULL, contracts > 0, settled).")
        return "\n".join(lines)

    n_traded = len(traded)
    staked = sum(r["contracts"] * r["ref_price"] for r in traded)
    fees = sum(r["fee_paid_usd"] or 0.0 for r in traded)
    pnl = sum(r["realized_pnl_usd"] or 0.0 for r in traded)
    wins = sum(1 for r in traded if (r["realized_pnl_usd"] or 0) > 0)
    roi = pnl / staked if staked else 0.0

    score_gaps = [r["score_gap"] for r in traded if r["score_gap"] is not None]
    divergences = [r["divergence"] for r in traded if r["divergence"] is not None]
    sg_mean, sg_se, sg_n = _mean_se(score_gaps)
    div_mean, _, _ = _mean_se(divergences)

    lines.append(f"SHADOW TRADES: {n_traded}  |  staked: ${staked:.2f}  |  fees: ${fees:.2f}  |  "
                 f"win rate: {wins/n_traded:.1%}")
    lines.append(f"Shadow P&L: ${pnl:+.2f}  |  ROI: {roi:+.1%}")
    lines.append(f"Score-gap (ours - market, per Corollary 3): mean {sg_mean:+.4f} "
                 f"+/- {1.96*sg_se:.4f} (95% normal-approx CI, n={sg_n}, NOT event-clustered — "
                 "see caveat below)")
    lines.append(f"Bregman divergence (always >=0, contributes to ROI independent of accuracy): "
                 f"mean {div_mean:.4f}")
    lines.append("")
    lines.append("CAVEAT: unlike longtail's clustered bootstrap, this CI treats rows as independent. "
                 "Markets on the same underlying event (e.g. multiple buckets of one election) are "
                 "correlated — treat the CI as optimistic until event-clustering is added.")

    # category breakdown
    lines.append("")
    lines.append(f"{'category':<24}{'n':>6}{'staked':>10}{'pnl':>10}{'roi':>8}")
    cats = {}
    for r in traded:
        c = r["category"] or "Unknown"
        cats.setdefault(c, []).append(r)
    for c, rs in sorted(cats.items(), key=lambda kv: -len(kv[1])):
        s = sum(r["contracts"] * r["ref_price"] for r in rs)
        p = sum(r["realized_pnl_usd"] or 0.0 for r in rs)
        lines.append(f"{c:<24}{len(rs):>6}{s:>10.2f}{p:>+10.2f}{(p/s if s else 0):>8.1%}")

    return "\n".join(lines)
