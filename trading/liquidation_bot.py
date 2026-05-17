import sys
"""
liquidation_bot.py
==================
Monitors Binance futures liquidation feed in real-time.
When large leveraged positions are force-closed, price momentum
typically continues in that direction for the next candle.

STRATEGY:
  LONG liquidated (exchange SELLS to close) → price dropped → bet DOWN
  SHORT liquidated (exchange BUYS to close) → price rose   → bet UP
  Single event ≥ $1M                        → exhaustion   → bet OPPOSITE (reversal)

SIZE → TIMEFRAME:
  $25k  – $100k → 15m Polymarket market
  $100k – $500k → 1h  Polymarket market
  $500k+        → 4h  Polymarket market

CASCADE:
  Multiple liquidations on the same asset within 60s are summed.
  If the total exceeds CASCADE_MIN_USD, fires a combined signal.
  The dominant direction (by $) determines the bet.

ASSETS: BTC, ETH, SOL, BNB, XRP, DOGE, HYPE (all have Polymarket up/down markets)
DATA:   Binance futures liquidation WebSocket — free, no API key needed
OUTPUT: liquidation_paper.csv

RUN: python liquidation_bot.py
"""

import asyncio
import websockets
import json
import csv
import os
import time
import logging
import threading
import requests
import ccxt
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# Fix Unicode output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

PAPER_MODE       = True
BASE             = "D:/Desktop/Trading Folder"
LOG_FILE         = f"{BASE}/liquidation_paper.csv"

LIQ_MIN_USD      = 25_000      # ignore events below this
LIQ_REVERSAL_USD = 1_000_000   # single event above this → reversal signal
CASCADE_WINDOW_S = 60          # seconds to accumulate cascade
CASCADE_MIN_USD  = 75_000      # cascade total must reach this to fire

# Size → Polymarket timeframe (checked top to bottom, first match wins)
SIZE_TIERS = [
    (500_000, "4h"),
    (100_000, "1h"),
    (25_000,  "15m"),
]

# Assets we track — all have active Polymarket up/down markets
ASSET_MAP = {
    "BTCUSDT":  {"ticker": "btc",  "fullname": "bitcoin",  "ccxt": "BTC/USDT",  "exch": "binance"},
    "ETHUSDT":  {"ticker": "eth",  "fullname": "ethereum", "ccxt": "ETH/USDT",  "exch": "binance"},
    "SOLUSDT":  {"ticker": "sol",  "fullname": "solana",   "ccxt": "SOL/USDT",  "exch": "binance"},
    "BNBUSDT":  {"ticker": "bnb",  "fullname": "bnb",      "ccxt": "BNB/USDT",  "exch": "binance"},
    "XRPUSDT":  {"ticker": "xrp",  "fullname": "xrp",      "ccxt": "XRP/USDT",  "exch": "binance"},
    "DOGEUSDT": {"ticker": "doge", "fullname": "dogecoin",  "ccxt": "DOGE/USDT", "exch": "binance"},
    "HYPEUSDT": {"ticker": "hype", "fullname": "hype",      "ccxt": "HYPE/USDT", "exch": "bybit"},
}

MONTH_NAMES = ["", "january", "february", "march", "april", "may", "june",
               "july", "august", "september", "october", "november", "december"]

BINANCE_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LIQ] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(f"{BASE}/liquidation_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ─────────────────────────────────────────────
# EXCHANGES (price resolution only)
# ─────────────────────────────────────────────

_exchange_binance = ccxt.binance({"enableRateLimit": True})
_exchange_bybit   = ccxt.bybit({"enableRateLimit": True})

def _get_exchange(exch: str):
    return _exchange_bybit if exch == "bybit" else _exchange_binance

def get_current_price(symbol: str) -> float | None:
    asset = ASSET_MAP.get(symbol)
    if not asset:
        return None
    try:
        ex     = _get_exchange(asset["exch"])
        ticker = ex.fetch_ticker(asset["ccxt"])
        return float(ticker["last"])
    except Exception:
        return None

# ─────────────────────────────────────────────
# SLUG BUILDERS
# ─────────────────────────────────────────────

def candle_start(ts: int, tf: str) -> int:
    intervals = {"15m": 900, "1h": 3600, "4h": 14400}
    s = intervals.get(tf, 900)
    return (ts // s) * s

def candle_end(ts: int, tf: str) -> int:
    intervals = {"15m": 900, "1h": 3600, "4h": 14400}
    return candle_start(ts, tf) + intervals.get(tf, 900)

def build_slug(ticker: str, fullname: str, tf: str, ts: int) -> str:
    cs = candle_start(ts, tf)
    if tf == "15m":
        return f"{ticker}-updown-15m-{cs}"
    if tf == "4h":
        return f"{ticker}-updown-4h-{cs}"
    if tf == "1h":
        # 1h markets use EDT (UTC-4), 12-hour clock
        dt_utc = datetime.fromtimestamp(cs, tz=timezone.utc)
        dt_edt = dt_utc - timedelta(hours=4)
        h24    = dt_edt.hour
        h12    = h24 % 12 or 12
        ampm   = "am" if h24 < 12 else "pm"
        month  = MONTH_NAMES[dt_edt.month]
        return (f"{fullname}-up-or-down-{month}-{dt_edt.day}"
                f"-{dt_edt.year}-{h12}{ampm}-et")
    return ""

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def size_to_tf(usd: float) -> str | None:
    for threshold, tf in SIZE_TIERS:
        if usd >= threshold:
            return tf
    return None

def dedup_key(symbol: str, tf: str, ts: int) -> str:
    return f"{symbol}_{tf}_{candle_start(ts, tf)}"

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

_fired          = set()                    # {dedup_key} — one bet per candle per asset
_positions_lock = threading.Lock()

# Cascade accumulator: {symbol: [(ts, size_usd, direction), ...]}
_cascade      = defaultdict(list)
_cascade_lock = threading.Lock()

# ─────────────────────────────────────────────
# SIGNAL LOGGING
# ─────────────────────────────────────────────

CSV_FIELDS = [
    "timestamp", "source", "symbol", "timeframe", "direction",
    "signal_type", "size_usd", "entry_price", "slug",
    "candle_start", "resolve_at", "exit_price", "pct_move", "result",
]

def _write_row(row: dict):
    file_exists = os.path.exists(LOG_FILE)
    with _positions_lock:
        with open(LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)


def log_signal(symbol: str, tf: str, direction: str,
               size_usd: float, signal_type: str, ts: int,
               source: str = "LIVE"):
    """Write one paper signal row to CSV."""
    asset = ASSET_MAP.get(symbol)
    if not asset:
        return

    entry_price = get_current_price(symbol)
    slug        = build_slug(asset["ticker"], asset["fullname"], tf, ts)
    resolve_at  = candle_end(ts, tf)

    row = {
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source":       source,
        "symbol":       symbol,
        "timeframe":    tf,
        "direction":    direction,
        "signal_type":  signal_type,
        "size_usd":     round(size_usd),
        "entry_price":  entry_price if entry_price else "",
        "slug":         slug,
        "candle_start": candle_start(ts, tf),
        "resolve_at":   resolve_at,
        "exit_price":   "",
        "pct_move":     "",
        "result":       "",
    }

    _write_row(row)

    entry_str = f"{entry_price:.5f}" if entry_price else "n/a"
    log.info(
        f"  [{'PAPER' if PAPER_MODE else 'LIVE'}] {signal_type:8s} | "
        f"{symbol:10s} {tf:3s} {direction:4s} | "
        f"${size_usd:>10,.0f} | "
        f"entry={entry_str} | "
        f"slug={slug}"
    )

# ─────────────────────────────────────────────
# SIGNAL PROCESSING
# ─────────────────────────────────────────────

def process_liquidation(symbol: str, side: str, size_usd: float, ts: int):
    """
    Evaluate a single liquidation event.

    side='SELL' → long position closed → price dropped → momentum DOWN
    side='BUY'  → short position closed → price rose  → momentum UP

    If size ≥ LIQ_REVERSAL_USD → exhaustion reversal → flip direction.
    """
    if symbol not in ASSET_MAP:
        return
    if size_usd < LIQ_MIN_USD:
        return

    # Base direction: follow the momentum
    direction = "DOWN" if side == "SELL" else "UP"

    # Reversal: single massive event = capitulation, expect bounce
    signal_type = "REVERSAL" if size_usd >= LIQ_REVERSAL_USD else "MOMENTUM"
    if signal_type == "REVERSAL":
        direction = "UP" if direction == "DOWN" else "DOWN"

    tf = size_to_tf(size_usd)
    if tf is None:
        return

    key = dedup_key(symbol, tf, ts)
    if key in _fired:
        return

    _fired.add(key)
    log_signal(symbol, tf, direction, size_usd, signal_type, ts)


def process_cascade(symbol: str, ts: int):
    """
    Evaluate accumulated liquidations for this symbol over the last
    CASCADE_WINDOW_S seconds. If the combined total exceeds CASCADE_MIN_USD,
    fire a cascade signal in the dominant direction.
    """
    with _cascade_lock:
        cutoff = ts - CASCADE_WINDOW_S
        events = [(t, s, d) for t, s, d in _cascade[symbol] if t >= cutoff]
        _cascade[symbol] = events

        if not events:
            return

        total_usd  = sum(s for _, s, _ in events)
        if total_usd < CASCADE_MIN_USD:
            return

        down_usd = sum(s for _, s, d in events if d == "DOWN")
        up_usd   = sum(s for _, s, d in events if d == "UP")
        direction = "DOWN" if down_usd >= up_usd else "UP"

    tf = size_to_tf(total_usd)
    if tf is None:
        return

    key = dedup_key(symbol, tf, ts)
    if key in _fired:
        return

    _fired.add(key)
    log_signal(symbol, tf, direction, total_usd, "CASCADE", ts)

# ─────────────────────────────────────────────
# RESOLUTION THREAD
# ─────────────────────────────────────────────

def _resolve_pass():
    """Resolve any signals whose candle has closed."""
    if not os.path.exists(LOG_FILE):
        return

    now = int(time.time())
    rows = []

    with _positions_lock:
        with open(LOG_FILE, "r", newline="") as f:
            reader    = csv.DictReader(f)
            rows      = [dict(r) for r in reader]
    fieldnames = CSV_FIELDS

    updated = 0
    for row in rows:
        if row.get("result"):
            continue
        try:
            resolve_at = int(row["resolve_at"])
        except (ValueError, KeyError):
            continue
        if now < resolve_at:
            continue

        entry_raw = row.get("entry_price", "")
        if not entry_raw:
            continue
        try:
            entry = float(entry_raw)
        except ValueError:
            continue

        # Fetch the actual candle close — not current price
        asset = ASSET_MAP.get(row["symbol"])
        if not asset:
            continue
        try:
            ex      = _get_exchange(asset["exch"])
            cs      = int(row["candle_start"])
            tf      = row["timeframe"]
            candles = ex.fetch_ohlcv(asset["ccxt"], tf, since=cs * 1000, limit=1)
            if not candles:
                continue
            exit_p = float(candles[0][4])   # close price of that candle
        except Exception as e:
            log.debug(f"  OHLCV fetch error ({row['symbol']}): {e}")
            continue

        direction = row.get("direction", "")
        if direction == "UP":
            result = "WIN" if exit_p > entry else "LOSS"
        elif direction == "DOWN":
            result = "WIN" if exit_p < entry else "LOSS"
        else:
            continue

        pct_move = round((exit_p - entry) / entry * 100, 4)

        row["exit_price"] = round(exit_p, 6)
        row["pct_move"]   = pct_move
        row["result"]     = result
        updated += 1

        log.info(
            f"  RESOLVED {result:4s} | {row['symbol']:10s} {row['timeframe']:3s} "
            f"{direction:4s} | entry={entry:.5f} exit={exit_p:.5f} "
            f"move={pct_move:+.3f}%"
        )

    if updated > 0:
        with _positions_lock:
            with open(LOG_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
        log.info(f"  {updated} signal(s) resolved.")


def resolution_loop():
    """Run _resolve_pass every 5 minutes."""
    while True:
        time.sleep(300)
        try:
            _resolve_pass()
        except Exception as e:
            log.error(f"Resolution error: {e}")

# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────

def print_stats():
    """Print win rate summary from CSV."""
    if not os.path.exists(LOG_FILE):
        log.info("No data yet.")
        return
    rows = []
    with open(LOG_FILE, "r", newline="") as f:
        rows = list(csv.DictReader(f))

    resolved = [r for r in rows if r.get("result")]
    if not resolved:
        log.info(f"Stats: {len(rows)} signals logged, 0 resolved yet.")
        return

    wins   = sum(1 for r in resolved if r["result"] == "WIN")
    total  = len(resolved)
    wr     = wins / total * 100

    log.info(f"{'='*50}")
    log.info(f"STATS | {total} resolved | WR: {wr:.1f}% ({wins}W / {total-wins}L)")

    # By signal type
    for stype in ["MOMENTUM", "REVERSAL", "CASCADE"]:
        sub = [r for r in resolved if r["signal_type"] == stype]
        if not sub:
            continue
        w = sum(1 for r in sub if r["result"] == "WIN")
        log.info(f"  {stype:8s}: {len(sub):3d} trades | WR {w/len(sub)*100:.1f}%")

    # By timeframe
    for tf in ["15m", "1h", "4h"]:
        sub = [r for r in resolved if r["timeframe"] == tf]
        if not sub:
            continue
        w = sum(1 for r in sub if r["result"] == "WIN")
        log.info(f"  {tf:3s}: {len(sub):3d} trades | WR {w/len(sub)*100:.1f}%")

    log.info(f"{'='*50}")

# ─────────────────────────────────────────────
# BACKFILL — replay missed liquidations on startup
# ─────────────────────────────────────────────

BINANCE_REST = "https://fapi.binance.com/fapi/v1/allForceOrders"

def _fetch_historical_result(ccxt_symbol: str, exch_name: str,
                              tf: str, ts: int, direction: str):
    """
    Fetch the OHLCV candle containing this timestamp.
    Returns (entry_price, exit_price, result) or (None, None, None).
    """
    try:
        ex      = _get_exchange(exch_name)
        cs      = candle_start(ts, tf)
        candles = ex.fetch_ohlcv(ccxt_symbol, tf, since=cs * 1000, limit=2)
        if not candles:
            return None, None, None
        _, open_p, _, _, close_p, _ = candles[0]
        entry  = float(open_p)
        exit_p = float(close_p)
        if direction == "UP":
            result = "WIN" if exit_p > entry else "LOSS"
        else:
            result = "WIN" if exit_p < entry else "LOSS"
        return entry, exit_p, result
    except Exception as e:
        log.debug(f"OHLCV fetch error ({ccxt_symbol} {tf}): {e}")
        return None, None, None


def backfill_history(days_back: int = 7):
    """
    On startup, fetch up to `days_back` days of liquidations from
    Binance REST API for each tracked asset. Replay through the same
    signal logic and resolve immediately using historical OHLCV.
    Results are tagged source=BACKFILL so you can analyse separately.
    """
    log.info(f"{'='*55}")
    log.info(f"BACKFILL: fetching last {days_back} days of liquidations...")
    log.info(f"{'='*55}")

    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - days_back * 86_400_000
    total    = 0

    for symbol, asset in ASSET_MAP.items():
        # Binance futures doesn't list HYPE — skip
        if symbol == "HYPEUSDT":
            continue

        orders = None
        for attempt in range(1, 4):
            try:
                r = requests.get(
                    BINANCE_REST,
                    params={"symbol": symbol, "limit": 1000, "startTime": start_ms},
                    timeout=15,
                )
                r.raise_for_status()
                orders = r.json()
                break
            except requests.exceptions.HTTPError as e:
                # 4xx = permanent error (geo-block, bad endpoint) — no point retrying
                log.debug(f"  Backfill {symbol} skipped: {e}")
                break
            except Exception as e:
                if attempt < 3:
                    log.warning(f"  Backfill {symbol} attempt {attempt}/3 failed — retrying in 3s: {e}")
                    time.sleep(3)
                else:
                    log.warning(f"  Backfill {symbol} failed after 3 attempts — skipping")
        if orders is None:
            continue

        if not orders:
            log.info(f"  {symbol}: no liquidations in last {days_back}d")
            continue

        log.info(f"  {symbol}: {len(orders)} historical liquidations")
        fired_this = 0

        for order in orders:
            try:
                side     = order.get("S", "")
                qty      = float(order.get("q", 0))
                avg_px   = float(order.get("ap", 0) or order.get("p", 0))
                size_usd = qty * avg_px
                ts       = int(order.get("T", 0)) // 1000

                if size_usd < LIQ_MIN_USD:
                    continue

                direction   = "DOWN" if side == "SELL" else "UP"
                signal_type = "REVERSAL" if size_usd >= LIQ_REVERSAL_USD else "MOMENTUM"
                if signal_type == "REVERSAL":
                    direction = "UP" if direction == "DOWN" else "DOWN"

                tf = size_to_tf(size_usd)
                if tf is None:
                    continue

                # Skip candles that haven't closed yet
                if candle_end(ts, tf) > int(time.time()):
                    continue

                key = dedup_key(symbol, tf, ts)
                if key in _fired:
                    continue
                _fired.add(key)

                # Resolve immediately using historical OHLCV
                entry, exit_p, result = _fetch_historical_result(
                    asset["ccxt"], asset["exch"], tf, ts, direction
                )

                slug = build_slug(asset["ticker"], asset["fullname"], tf, ts)
                row = {
                    "timestamp":    datetime.fromtimestamp(ts, tz=timezone.utc)
                                        .strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "source":       "BACKFILL",
                    "symbol":       symbol,
                    "timeframe":    tf,
                    "direction":    direction,
                    "signal_type":  signal_type,
                    "size_usd":     round(size_usd),
                    "entry_price":  round(entry, 6) if entry else "",
                    "slug":         slug,
                    "candle_start": candle_start(ts, tf),
                    "resolve_at":   candle_end(ts, tf),
                    "exit_price":   round(exit_p, 6) if exit_p else "",
                    "pct_move":     round((exit_p - entry) / entry * 100, 4) if entry and exit_p else "",
                    "result":       result or "",
                }
                _write_row(row)
                fired_this += 1
                total += 1

            except Exception as e:
                log.debug(f"  Backfill order error: {e}")

        log.info(f"  {symbol}: {fired_this} signals logged from backfill")
        time.sleep(0.3)   # gentle rate limit

    log.info(f"Backfill complete — {total} historical signals logged.")
    log.info(f"{'='*55}")
    print_stats()


# ─────────────────────────────────────────────
# WEBSOCKET LISTENER
# ─────────────────────────────────────────────

async def listen():
    log.info(f"Connecting: {BINANCE_WS}")
    while True:
        try:
            async with websockets.connect(
                BINANCE_WS,
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                log.info("Connected. Listening for liquidations...")
                async for message in ws:
                    try:
                        data = json.loads(message)
                        o    = data.get("o", {})

                        symbol   = o.get("s", "")
                        side     = o.get("S", "")           # BUY or SELL
                        qty      = float(o.get("q", 0))
                        avg_px   = float(o.get("ap", 0) or o.get("p", 0))
                        size_usd = qty * avg_px
                        ts       = int(data.get("E", time.time() * 1000)) // 1000

                        if symbol not in ASSET_MAP:
                            continue
                        if size_usd < LIQ_MIN_USD:
                            continue

                        log.info(
                            f"LIQ {symbol:10s} {side:4s} "
                            f"${size_usd:>10,.0f} "
                            f"| qty={qty} @ ${avg_px:.4f}"
                        )

                        # Cascade accumulator
                        direction = "DOWN" if side == "SELL" else "UP"
                        with _cascade_lock:
                            _cascade[symbol].append((ts, size_usd, direction))

                        # Single-event signal
                        process_liquidation(symbol, side, size_usd, ts)

                        # Cascade signal
                        process_cascade(symbol, ts)

                    except Exception as e:
                        log.debug(f"Parse error: {e}")

        except Exception as e:
            log.error(f"WebSocket disconnected: {e} — reconnecting in 5s")
            await asyncio.sleep(5)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info(f"liquidation_bot.py  |  {'PAPER MODE' if PAPER_MODE else '*** LIVE ***'}")
    log.info(f"Assets  : {', '.join(ASSET_MAP.keys())}")
    log.info(f"Tiers   : <$100k=15m | $100k-$500k=1h | >$500k=4h")
    log.info(f"Min     : ${LIQ_MIN_USD:,}  |  Reversal: ${LIQ_REVERSAL_USD:,}")
    log.info(f"Cascade : ${CASCADE_MIN_USD:,} within {CASCADE_WINDOW_S}s")
    log.info(f"Log     : {LOG_FILE}")
    log.info("=" * 55)

    # Backfill missed liquidations from last 7 days
    try:
        backfill_history(days_back=7)
    except Exception as e:
        log.error(f"Backfill error: {e}")

    # Start resolution thread
    t = threading.Thread(target=resolution_loop, daemon=True, name="liq-resolve")
    t.start()
    log.info("Resolution thread started (every 5 min)")

    # Run WebSocket listener (blocks until stopped)
    asyncio.run(listen())


if __name__ == "__main__":
    main()
