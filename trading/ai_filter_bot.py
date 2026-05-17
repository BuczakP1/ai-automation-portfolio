import sys
"""
ai_filter_bot.py
================
Uses Claude AI as a confirmation filter on top of live bot signals.

How it works:
  1. Every 15 minutes, reads live_trades.csv for signals just fired
  2. For each signal, asks Claude if it agrees with the direction
  3. Logs "confirmed" if both agree, "rejected" if AI disagrees
  4. Tracks WR of confirmed vs rejected separately

This answers: does adding AI confirmation improve WR over raw signals?

PAPER ONLY — no real bets placed.

RUN:
    python ai_filter_bot.py
"""

import time
import threading
import logging
import os
import csv
import ccxt
import pandas as pd
import numpy as np
import anthropic
from datetime import datetime, timezone

# Fix Unicode output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

MODEL           = "claude-haiku-4-5-20251001"
MIN_CONFIDENCE  = 60
RESOLVE_SECONDS = 900   # 15 minutes
LOOKBACK_SECS   = 120   # how far back to look for new live bot signals (2 min window)

OUTPUT_DIR  = "D:/Desktop/Trading Folder"
LIVE_CSV    = f"{OUTPUT_DIR}/live_trades.csv"
FILTER_CSV  = f"{OUTPUT_DIR}/ai_filter_paper.csv"
LOG_FILE    = f"{OUTPUT_DIR}/ai_filter_bot.log"

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger()

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

csv_lock       = threading.Lock()
exchange       = ccxt.binance({'enableRateLimit': True})
exchange_bybit = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
EXCHANGE_OVERRIDE = {"HYPE/USDT": exchange_bybit}

client = anthropic.Anthropic(api_key=API_KEY)

CSV_HEADER = [
    'logged_at', 'asset', 'timeframe', 'live_signal', 'ai_signal',
    'ai_confidence', 'verdict',   # CONFIRMED or REJECTED
    'price_entry', 'resolve_time',
    'resolved_price', 'correct', 'ai_reason'
]

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_candles(symbol: str, timeframe: str, limit: int) -> pd.DataFrame | None:
    ex = EXCHANGE_OVERRIDE.get(symbol, exchange)
    for attempt in range(1, 4):
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < 10:
                return None
            df = pd.DataFrame(ohlcv, columns=['ts', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
            df = df.set_index('ts').astype(float)
            return df.iloc[:-1]
        except Exception as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                log.warning(f"Fetch failed {symbol} {timeframe}: {e}")
    return None


def format_candles(df: pd.DataFrame, label: str) -> str:
    lines = [f"{label} candles (OHLCV):"]
    for ts, row in df.tail(20).iterrows():
        lines.append(
            f"  {ts.strftime('%m-%d %H:%M')}  "
            f"O:{row['Open']:.4f} H:{row['High']:.4f} "
            f"L:{row['Low']:.4f} C:{row['Close']:.4f} V:{row['Volume']:.0f}"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────
# AI CONFIRMATION
# ─────────────────────────────────────────────

def ask_claude(asset: str, live_signal: str,
               df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame
               ) -> tuple[str, int, str]:
    """
    Ask Claude if it agrees with the live bot's signal.
    Returns (ai_signal, confidence, reason).
    """
    ticker = asset.replace("/USDT", "")
    price  = df_15m['Close'].iloc[-1]

    prompt = f"""You are a professional crypto trader. The live trading bot just fired a {live_signal} signal on {ticker}.

Current price: {price:.4f}

{format_candles(df_4h, '4H')}

{format_candles(df_1h, '1H')}

{format_candles(df_15m, '15M')}

The bot predicts the next 15-minute candle will go {live_signal}. Do you agree?

Analyse price action, momentum, volume and structure across all 3 timeframes.

Respond in EXACTLY this format (no other text):
SIGNAL: UP or DOWN or SKIP
CONFIDENCE: 0-100
REASON: one sentence

Only use UP or DOWN if confidence >= 60. Use SKIP if unclear."""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()

        signal     = "SKIP"
        confidence = 0
        reason     = ""

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("SIGNAL:"):
                val = line.split(":", 1)[1].strip().upper()
                if val in ("UP", "DOWN", "SKIP"):
                    signal = val
            elif line.startswith("CONFIDENCE:"):
                try:
                    confidence = int(line.split(":", 1)[1].strip())
                except:
                    confidence = 0
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        if confidence < MIN_CONFIDENCE:
            signal = "SKIP"

        return signal, confidence, reason

    except Exception as e:
        log.warning(f"Claude error {asset}: {e}")
        return "SKIP", 0, str(e)


# ─────────────────────────────────────────────
# CSV LOGGING
# ─────────────────────────────────────────────

def log_filter(asset, timeframe, live_signal, ai_signal, ai_confidence,
               verdict, price, resolve_time, ai_reason):
    row = {
        'logged_at':      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'asset':          asset,
        'timeframe':      timeframe,
        'live_signal':    live_signal,
        'ai_signal':      ai_signal,
        'ai_confidence':  ai_confidence,
        'verdict':        verdict,
        'price_entry':    round(price, 4),
        'resolve_time':   resolve_time,
        'resolved_price': '',
        'correct':        '',
        'ai_reason':      ai_reason,
    }
    with csv_lock:
        write_header = not os.path.exists(FILTER_CSV)
        with open(FILTER_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
            if write_header:
                writer.writeheader()
            writer.writerow(row)


# ─────────────────────────────────────────────
# RESOLVER
# ─────────────────────────────────────────────

def update_resolutions():
    """Background thread — fills resolved_price + correct."""
    while True:
        try:
            if not os.path.exists(FILTER_CSV):
                time.sleep(60)
                continue

            now = datetime.now(timezone.utc)

            with csv_lock:
                df = pd.read_csv(FILTER_CSV, dtype=str)

            pending = []
            for idx, row in df.iterrows():
                if str(row.get('correct', '')).strip() not in ('', 'nan', 'None'):
                    continue
                resolve_str = str(row.get('resolve_time', ''))
                if not resolve_str or resolve_str in ('nan', 'None', ''):
                    continue
                try:
                    resolve_dt = datetime.strptime(resolve_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                except:
                    continue
                if now < resolve_dt:
                    continue
                pending.append((idx, row))

            if not pending:
                time.sleep(60)
                continue

            log.info(f"[RESOLVE] {len(pending)} row(s) to resolve...")

            updates = {}
            for idx, row in pending:
                asset = row['asset']
                try:
                    resolve_dt = datetime.strptime(
                        str(row['resolve_time']), '%Y-%m-%d %H:%M:%S'
                    ).replace(tzinfo=timezone.utc)
                    cur_df = fetch_candles(asset, '15m', limit=100)
                    if cur_df is None:
                        continue
                    target_open = resolve_dt - pd.Timedelta(minutes=15)
                    candle = cur_df[cur_df.index == target_open]
                    if candle.empty:
                        before = cur_df[cur_df.index <= resolve_dt]
                        if before.empty:
                            continue
                        candle = before.iloc[[-1]]
                    resolved_price = float(candle['Close'].iloc[-1])
                    entry_price    = float(row['price_entry'])
                    live_signal    = str(row['live_signal']).upper()
                    if live_signal == 'UP':
                        correct = resolved_price > entry_price
                    elif live_signal == 'DOWN':
                        correct = resolved_price < entry_price
                    else:
                        continue
                    updates[idx] = (str(round(resolved_price, 4)), str(correct))
                    log.info(f"[RESOLVE] {asset} {live_signal} ({row['verdict']}) => "
                             f"{'WIN' if correct else 'LOSS'} "
                             f"(entry:{entry_price:.4f} → resolved:{resolved_price:.4f})")
                except Exception as e:
                    log.warning(f"[RESOLVE] Error {asset}: {e}")

            if updates:
                with csv_lock:
                    df2 = pd.read_csv(FILTER_CSV, dtype=str)
                    for idx, (res_price, correct) in updates.items():
                        if idx < len(df2):
                            df2.at[idx, 'resolved_price'] = res_price
                            df2.at[idx, 'correct']        = correct
                    df2.to_csv(FILTER_CSV, index=False)
                log.info(f"[RESOLVE] Saved {len(updates)} resolution(s).")

        except Exception as e:
            log.error(f"[RESOLVE] Thread error: {e}")
        time.sleep(60)


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

def scan():
    """Read live_trades.csv for signals fired this candle, ask Claude to filter."""
    if not os.path.exists(LIVE_CSV):
        log.info("[FILTER] live_trades.csv not found, skipping")
        return

    now_ts     = time.time()
    cutoff     = now_ts - LOOKBACK_SECS
    cutoff_dt  = datetime.fromtimestamp(cutoff, tz=timezone.utc)

    resolve_ts   = ((int(now_ts // RESOLVE_SECONDS)) * RESOLVE_SECONDS) + RESOLVE_SECONDS
    resolve_time = datetime.fromtimestamp(resolve_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    try:
        live_df = pd.read_csv(LIVE_CSV)
    except Exception as e:
        log.warning(f"[FILTER] Could not read live_trades.csv: {e}")
        return

    # All UP/DOWN signals (including paper/skipped) logged in the last LOOKBACK_SECS
    live_df['logged_at'] = pd.to_datetime(live_df['logged_at'], utc=True, errors='coerce')
    recent = live_df[
        (live_df['logged_at'] >= cutoff_dt) &
        (live_df['signal'].isin(['UP', 'DOWN']))
    ]

    if recent.empty:
        log.info("[FILTER] No live signals this candle")
        return

    log.info(f"[FILTER] {len(recent)} live signal(s) to evaluate...")

    # Deduplicate — one evaluation per (asset, signal)
    seen = set()
    for _, row in recent.iterrows():
        asset       = row['asset']
        live_signal = str(row['signal']).upper()
        timeframe   = str(row['timeframe'])
        key         = (asset, live_signal)
        if key in seen:
            continue
        seen.add(key)

        log.info(f"  [FILTER] {asset} live={live_signal} — fetching candles...")

        df_15m = fetch_candles(asset, '15m', limit=35)
        df_1h  = fetch_candles(asset, '1h',  limit=25)
        df_4h  = fetch_candles(asset, '4h',  limit=15)

        if df_15m is None or df_1h is None or df_4h is None:
            log.warning(f"  [FILTER] {asset} — fetch failed, skipping")
            continue

        price = df_15m['Close'].iloc[-1]

        ai_signal, ai_confidence, ai_reason = ask_claude(
            asset, live_signal, df_15m, df_1h, df_4h
        )

        if ai_signal == live_signal:
            verdict = "CONFIRMED"
        elif ai_signal == "SKIP":
            verdict = "SKIP"
        else:
            verdict = "REJECTED"

        log.info(f"  [FILTER] {asset} live={live_signal} ai={ai_signal} "
                 f"conf={ai_confidence}% → {verdict} | {ai_reason}")

        log_filter(asset, timeframe, live_signal, ai_signal, ai_confidence,
                   verdict, price, resolve_time, ai_reason)


def main():
    log.info("=" * 60)
    log.info(f"AI Filter Bot | PAPER MODE | Model: {MODEL}")
    log.info(f"Reads live signals, asks Claude to confirm or reject")
    log.info("=" * 60)

    t = threading.Thread(target=update_resolutions, daemon=True)
    t.start()

    while True:
        try:
            now        = time.time()
            next_candle = ((int(now // RESOLVE_SECONDS)) + 1) * RESOLVE_SECONDS
            wait        = next_candle - now
            log.info(f"[FILTER] Next candle in {wait:.0f}s — sleeping...")
            time.sleep(wait + 5)  # +5 so live bot logs first
            scan()
        except Exception as e:
            log.error(f"[FILTER] Loop error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
