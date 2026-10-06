#!/usr/bin/env python3
"""GM League Season 4 — Render Keep-Alive Pinger.

Pings the deployed Render web application every 15 seconds to prevent
the free-tier instance from sleeping (Render spins down after 15 minutes of inactivity).

Usage:
    python pinger.py https://your-app.onrender.com
    # Or set the PING_URL environment variable:
    # export PING_URL=https://your-app.onrender.com
    # python pinger.py
"""

import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEFAULT_INTERVAL = 5  # seconds


def get_target_url():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        url = sys.argv[1].strip()
    else:
        url = os.getenv("PING_URL", "").strip()

    if not url:
        print("[!] No target URL provided.")
        print("    Usage: python pinger.py <URL>")
        print("    Example: python pinger.py https://gml-s4.onrender.com")
        sys.exit(1)

    url = url.rstrip("/")
    if not url.endswith("/ping") and not url.endswith("/health"):
        url = f"{url}/ping"
    return url


def ping_once(url, timeout=10):
    start = time.time()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "GM-League-KeepAlive-Pinger/1.0 (+https://gmleague.org)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = (time.time() - start) * 1000
            return resp.status, elapsed, None
    except urllib.error.HTTPError as e:
        elapsed = (time.time() - start) * 1000
        return e.code, elapsed, str(e)
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return 0, elapsed, str(e)


def main():
    target_url = get_target_url()
    interval = int(os.getenv("PING_INTERVAL", DEFAULT_INTERVAL))

    print("=" * 65)
    print(f"🚀 GM League 24/7 Keep-Alive Pinger")
    print(f"🎯 Target:   {target_url}")
    print(f"⏱️  Interval: {interval} seconds")
    print("=" * 65)

    failures = 0
    total_pings = 0

    try:
        while True:
            total_pings += 1
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            status, latency, err = ping_once(target_url)

            if status == 200:
                print(f"[{now_str}] Ping #{total_pings} -> HTTP {status} OK ({latency:.1f}ms)")
                failures = 0
            else:
                failures += 1
                msg = f"HTTP {status}" if status else f"ERROR: {err}"
                print(f"[{now_str}] Ping #{total_pings} -> ⚠️  {msg} ({latency:.1f}ms) [consecutive failures: {failures}]")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[i] Pinger stopped by user.")


if __name__ == "__main__":
    main()
