"""
hip3_funding_bot.py
===================
Scans Hyperliquid HIP-3 assets (oil, gold, silver, stocks) for extreme
funding rates and paper trades them.

Logic:
  - Extreme NEGATIVE funding → longs are being PAID to hold → go LONG
  - Extreme POSITIVE funding → shorts are being PAID to hold → go SHORT
  - This is passive income: every hour you hold, you earn the funding rate
  - Exit when: funding normalises back to normal OR price stop loss hit

Why this works:
  - Extreme funding means the market is heavily one-sided
  - You get paid to hold the unpopular side
  - Wall Street cannot access hourly funding rates on commodities — crypto native edge

Example: Oil at -200% APR funding
  → You earn 200/365 = 0.55% PER DAY just for holding a long
  → On $1,000 position = $5.50/day passive

Thresholds:
  ENTRY:  |APR| >= 100%  (extreme — get paid massively)
  EXIT:   |APR| < 30%    (funding normalised — carry gone)
  STOP:   5% price move against us

Paper mode only — logs to hip3_funding_paper.csv

RUN:
    python hip3_funding_bot.py
"""

import sys
import time
import logging
import csv
import json
import os
import requests
from datetime import datetime, timezone

# Fix Unicode output on Windows (cp1252 → utf-8)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 3600      # scan every hour (funding updates hourly)
ENTRY_THRESHOLD_APR = 100.0     # enter when |APR| >= 100%
EXIT_THRESHOLD_APR  = 30.0      # exit when |APR| drops below 30% (carry gone)
STOP_LOSS_PCT       = 0.05      # 5% price stop loss

# HIP-3 assets to watch — expand as Hyperliquid adds more
HIP3_ASSETS = {
    "GOLD", "SILVER", "OIL", "GOOG", "NVDA",
    "XYZ100", "NATGAS", "COPPER", "PLATINUM"
}

OUTPUT_DIR = "D:/Desktop/Trading Folder"
PAPER_CSV  = f"{OUTPUT_DIR}/hip3_funding_paper.csv"
LOG_FILE   = f"{OUTPUT_DIR}/hip3_funding_bot.log"
STATE_FILE = f"{OUTPUT_DIR}/hip3_funding_state.json"

HL_API = "https://api.hyperliquid.xyz/info"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger()

CSV_HEADERS = [
    "trade_id", "entry_timestamp", "coin", "direction",
    "entry_price", "entry_funding_apr",
    "exit_timestamp", "exit_price", "exit_funding_apr",
    "hours_held", "funding_earned_pct", "price_pnl_pct",
    "total_pnl_pct", "exit_reason", "status"
]


# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

def init_csv():
    if not os.path.exists(PAPER_CSV):
        with open(PAPER_CSV, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writeheader()
        logger.info(f"Created {PAPER_CSV}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"open_trades": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────

def get_funding_rates():
    """
    Fetch all asset funding rates from Hyperliquid.
    Returns dict: {coin: {funding_hourly, funding_apr, mid_price}}
    """
    try:
        resp = requests.post(
            HL_API,
            json={"type": "metaAndAssetCtxs"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        meta   = data[0]["universe"]   # list of {name, ...}
        ctxs   = data[1]               # list of {funding, markPx, ...}

        rates = {}
        for asset, ctx in zip(meta, ctxs):
            coin = asset["name"]
            funding_hourly = float(ctx.get("funding", 0))
            funding_apr    = funding_hourly * 24 * 365 * 100
            mid_price      = float(ctx.get("markPx", 0) or ctx.get("midPx", 0) or 0)

            rates[coin] = {
                "funding_hourly": funding_hourly,
                "funding_apr":    funding_apr,
                "mid_price":      mid_price,
            }

        return rates

    except Exception as e:
        logger.error(f"Funding rate fetch failed: {e}")
        return {}


def get_price(coin, all_rates):
    """Get current mid price from already-fetched rates."""
    return all_rates.get(coin, {}).get("mid_price", 0)


# ─────────────────────────────────────────────
# PAPER TRADE LOGGING
# ─────────────────────────────────────────────

def log_entry(trade):
    with open(PAPER_CSV, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(trade)
    logger.info(
        f"[ENTRY] {trade['coin']} {trade['direction'].upper()} "
        f"@ {trade['entry_price']} | funding APR={trade['entry_funding_apr']:.1f}% "
        f"(earning {abs(trade['entry_funding_apr']):.1f}%/yr to hold)"
    )


def log_exit(trade_id, update):
    rows = []
    with open(PAPER_CSV, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["trade_id"] == trade_id:
                row.update(update)
                row["status"] = "closed"
            rows.append(row)

    with open(PAPER_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(
        f"[EXIT] {trade_id} | reason={update['exit_reason']} | "
        f"funding earned={update['funding_earned_pct']}% | "
        f"price pnl={update['price_pnl_pct']}% | "
        f"total={update['total_pnl_pct']}%"
    )


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

def main():
    init_csv()
    state = load_state()
    open_trades = state["open_trades"]  # {coin: trade_dict}

    logger.info("=== HIP-3 Funding Rate Bot started ===")
    logger.info(
        f"Entry: |APR| >= {ENTRY_THRESHOLD_APR}% | "
        f"Exit: |APR| < {EXIT_THRESHOLD_APR}% | "
        f"Stop: {STOP_LOSS_PCT*100:.0f}%"
    )
    logger.info(f"Watching: {', '.join(sorted(HIP3_ASSETS))}")

    while True:
        try:
            now = datetime.now(timezone.utc)
            rates = get_funding_rates()

            if not rates:
                logger.warning("No rate data — retrying next cycle")
                time.sleep(SCAN_INTERVAL_SEC)
                continue

            # ── SCAN FOR NEW ENTRIES ──────────────────────────
            for coin in HIP3_ASSETS:
                if coin not in rates:
                    continue
                if coin in open_trades:
                    continue  # already in a trade on this coin

                r = rates[coin]
                apr    = r["funding_apr"]
                price  = r["mid_price"]

                if abs(apr) < ENTRY_THRESHOLD_APR:
                    continue  # not extreme enough

                if price == 0:
                    logger.warning(f"{coin} — no price data, skipping")
                    continue

                # Negative APR → longs paid → go LONG
                # Positive APR → shorts paid → go SHORT
                direction = "long" if apr < 0 else "short"

                trade_id = f"{coin}_{int(now.timestamp())}"
                trade = {
                    "trade_id":          trade_id,
                    "entry_timestamp":   now.isoformat(),
                    "coin":              coin,
                    "direction":         direction,
                    "entry_price":       price,
                    "entry_funding_apr": round(apr, 2),
                    "exit_timestamp":    "",
                    "exit_price":        "",
                    "exit_funding_apr":  "",
                    "hours_held":        "",
                    "funding_earned_pct": "",
                    "price_pnl_pct":     "",
                    "total_pnl_pct":     "",
                    "exit_reason":       "",
                    "status":            "open"
                }

                log_entry(trade)
                open_trades[coin] = trade

            # ── CHECK EXITS ───────────────────────────────────
            to_close = []

            for coin, trade in open_trades.items():
                if coin not in rates:
                    continue

                r          = rates[coin]
                current_apr   = r["funding_apr"]
                current_price = r["mid_price"]
                entry_price   = float(trade["entry_price"])
                entry_apr     = float(trade["entry_funding_apr"])
                direction     = trade["direction"]

                if current_price == 0 or entry_price == 0:
                    continue

                # Hours held
                entry_dt   = datetime.fromisoformat(trade["entry_timestamp"])
                hours_held = (now - entry_dt).total_seconds() / 3600

                # Funding earned (hourly rate * hours held, applied to our side)
                # Each hour we earn |hourly_rate| on our position
                hourly_rate    = abs(float(r["funding_hourly"]))
                funding_earned = hourly_rate * hours_held * 100  # as %

                # Price P&L
                if direction == "long":
                    price_pnl = (current_price - entry_price) / entry_price
                    stop_hit  = current_price <= entry_price * (1 - STOP_LOSS_PCT)
                else:
                    price_pnl = (entry_price - current_price) / entry_price
                    stop_hit  = current_price >= entry_price * (1 + STOP_LOSS_PCT)

                total_pnl = price_pnl + (funding_earned / 100)

                # Log current status
                logger.info(
                    f"[OPEN] {coin} {direction.upper()} | "
                    f"held={hours_held:.1f}h | "
                    f"funding earned={funding_earned:.3f}% | "
                    f"price pnl={price_pnl*100:.2f}% | "
                    f"current APR={current_apr:.1f}%"
                )

                # Exit conditions
                exit_reason = None

                if stop_hit:
                    exit_reason = "stop_loss"
                elif abs(current_apr) < EXIT_THRESHOLD_APR:
                    exit_reason = "funding_normalised"

                if exit_reason:
                    update = {
                        "exit_timestamp":    now.isoformat(),
                        "exit_price":        str(current_price),
                        "exit_funding_apr":  str(round(current_apr, 2)),
                        "hours_held":        str(round(hours_held, 2)),
                        "funding_earned_pct": str(round(funding_earned, 4)),
                        "price_pnl_pct":     str(round(price_pnl * 100, 4)),
                        "total_pnl_pct":     str(round(total_pnl * 100, 4)),
                        "exit_reason":       exit_reason,
                    }
                    log_exit(trade["trade_id"], update)
                    to_close.append(coin)

            for coin in to_close:
                open_trades.pop(coin, None)

            # ── SUMMARY ──────────────────────────────────────
            save_state({"open_trades": open_trades})

            # Print all HIP-3 rates for visibility
            logger.info("-- HIP-3 Funding Snapshot --")
            for coin in sorted(HIP3_ASSETS):
                if coin in rates:
                    r = rates[coin]
                    apr = r["funding_apr"]
                    flag = ""
                    if abs(apr) >= ENTRY_THRESHOLD_APR:
                        flag = " ← EXTREME"
                    elif abs(apr) >= 50:
                        flag = " ← elevated"
                    logger.info(f"  {coin:10s} APR={apr:+.1f}%{flag}")

            logger.info(
                f"Open trades: {len(open_trades)} | "
                f"Next scan in {SCAN_INTERVAL_SEC//60} min"
            )

        except Exception as e:
            logger.error(f"Main loop error: {e}")

        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
