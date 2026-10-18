#!/usr/bin/env python3
"""
fwd_report.py  --  periodic status snapshot of the two Kalshi longshot-maker forward paper tests
(kalshi-fwd-1-6h.service: 1-6h-to-close hypothesis arm; kalshi-fwd-control.service: full 1-72h
window, the arm the 60-day backtest already failed). Reads each engine's sqlite state directly
(read-only, so it never contends with the live process) -- no network calls, no Kalshi API keys.

Run manually:  python fwd_report.py
Scheduled by:  kalshi-fwd-report.timer (daily) -> appends to logs/fwd_report.log
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

ARMS = [
    ("1-6h (hypothesis)", "data/kalshi_fwd_1_6h.db", "kalshi-fwd-1-6h.service"),
    ("1-72h (control)", "data/kalshi_fwd_control.db", "kalshi-fwd-control.service"),
]


def kv(conn, key, default=None):
    r = conn.execute("select v from kv where k=?", (key,)).fetchall()
    return json.loads(r[0][0]) if r else default


def systemd_active(unit):
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", unit],
                              capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception as e:
        return f"unknown ({e})"


def report_arm(label, path, unit):
    lines = [f"-- {label}  [{path}]  service: {systemd_active(unit)} --"]
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError as e:
        lines.append(f"  could not open db: {e}")
        return "\n".join(lines)

    cash = kv(conn, "paper_cash")
    pos = kv(conn, "paper_pos", {})
    halted = kv(conn, "halted")
    if cash is None:
        lines.append("  no data yet (engine hasn't completed a cycle)")
        conn.close()
        return "\n".join(lines)

    eq = conn.execute("select ts, total from equity order by ts").fetchall()
    counts = dict(conn.execute("select kind, count(*) from events group by kind").fetchall())
    pnl_rows = conn.execute(
        "select pnl from events where kind like '%SETTLE%' or kind like '%EXIT%'").fetchall()
    settled_pnl = sum(r[0] or 0 for r in pnl_rows)

    cost_in_positions = sum(v[2] for v in pos.values())
    lines.append(f"  cash {cash:.2f}  +  {len(pos)} open position(s) at cost {cost_in_positions:.2f}"
                 f"  =  {cash + cost_in_positions:.2f} (not mark-to-market)")
    if eq:
        start_ts, start_total = eq[0]
        end_ts, end_total = eq[-1]
        days = (datetime.fromisoformat(end_ts) - datetime.fromisoformat(start_ts)).total_seconds() / 86400
        lines.append(f"  equity {start_total:.2f} at {start_ts[:16]}  ->  {end_total:.2f} at {end_ts[:16]}"
                     f"  ({days:.1f} days)")
    else:
        lines.append("  no equity snapshots yet")
    lines.append(f"  events: {counts or 'none'}")
    lines.append(f"  settled P&L (paper, realized only): {settled_pnl:+.2f}")
    if halted:
        lines.append(f"  *** HALTED: {halted} ***")
    conn.close()
    return "\n".join(lines)


def main():
    header = f"=== fwd_report {datetime.now(timezone.utc).isoformat(timespec='seconds')} ==="
    body = [header] + [report_arm(label, path, unit) for label, path, unit in ARMS]
    out = "\n".join(body) + "\n"
    print(out)
    return out


if __name__ == "__main__":
    main()
