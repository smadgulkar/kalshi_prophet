#!/usr/bin/env python3
"""
fav_notify.py  --  send the favorite-taker report (+ longshot-maker snapshot) to Telegram.
Creds: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env (gitignored; loaded by config.py).
Scheduled by kalshi-fav-notify.timer.
"""

import os
import subprocess
import sys

import requests

import config  # noqa: F401 -- side effect: loads .env into os.environ

MAX_LEN = 4000  # Telegram hard limit is 4096


def run(script: str) -> str:
    r = subprocess.run([sys.executable, *script.split()], capture_output=True, text=True, timeout=300)
    return (r.stdout + r.stderr).strip()


def main() -> int:
    text = "Kalshi paper tests — daily check\n\n" + run("fav_logger.py report") + "\n\n" + run("fwd_report.py")
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 20] + "\n...[truncated]"
    r = requests.post(f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                      json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=15)
    ok = r.ok and r.json().get("ok")
    print("telegram sent" if ok else f"telegram FAILED: {r.status_code} {r.text[:200]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
