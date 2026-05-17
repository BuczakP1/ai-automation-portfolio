"""
coin_monitor.py
===============
Actively monitors coins flagged by the CEX listing bot and wallet tracker.
Fetches 5-minute candles every 5 minutes per coin.
On restart, automatically backfills any gap since last recorded candle.
Tracks peak price, % from entry, and archives each coin after 24h.

Data saved to: price_history/{SYMBOL}_5m.csv

RUN:
    python coin_monitor.py
"""

import os
import json
import time
import logging
import pandas as pd
import ccxt
from datetime import datetime, timezone, timedelta

logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

OUTPUT_DIR   = "D:/Desktop/Trading Folder"
HISTORY_DIR  = f"{OUTPUT_DIR}/price_history"
LOG_FILE     = f"{OUTPUT_DIR}/coin_monitor.log"

# Crypto-only CSVs — fetch from Binance
CRYPTO_CSVS = [
    f"{OUTPUT_DIR}/cex_listing_paper.csv",
    f"{OUTPUT_DIR}/wallet_tracker_paper.csv",
]

# Stock CSVs — fetch from yfinance
STOCK_CSVS = [
    f"{OUTPUT_DIR}/fda_paper.csv",
    f"{OUTPUT_DIR}/sec_8k_paper.csv",
    f"{OUTPUT_DIR}/gov_news_paper.csv",
    f"{OUTPUT_DIR}/insider_paper.csv",
]

# All CSVs combined for cross-signal check
SIGNAL_CSVS = CRYPTO_CSVS + STOCK_CSVS

POLL_SECONDS       = 300      # fetch candles every 5 minutes
TRACK_HOURS        = 48       # monitor each coin for 48 hours after signal
CANDLE_LIMIT       = 1000     # max candles to backfill in one request
CROSS_SIGNAL_HOURS = 24       # window to check for cross-bot confirmation

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

os.makedirs(HISTORY_DIR, exist_ok=True)

exchange = ccxt.binance({'enableRateLimit': True})

# ─────────────────────────────────────────────
# LOAD ACTIVE COINS FROM SIGNAL CSVS
# ─────────────────────────────────────────────

def load_active_coins() -> dict:
    """
    Returns {symbol: {'entry_price': float, 'signal_time': datetime, 'source': str, 'is_crypto': bool}}
    Only coins with BULLISH signal logged within the last TRACK_HOURS.
    """
    coins  = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=TRACK_HOURS)

    for path in SIGNAL_CSVS:
        is_crypto = path in CRYPTO_CSVS
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path, dtype=str)
        except:
            continue

        # Normalise ticker column name (wallet_tracker uses 'token_symbol')
        ticker_col = ('ticker' if 'ticker' in df.columns
                      else 'token_symbol' if 'token_symbol' in df.columns
                      else None)
        if ticker_col is None:
            continue

        signal_col = 'signal' if 'signal' in df.columns else None
        time_col   = 'logged_at' if 'logged_at' in df.columns else None
        price_col  = 'price_entry' if 'price_entry' in df.columns else None

        for _, row in df.iterrows():
            if signal_col and str(row.get(signal_col, '')).upper() != 'BULLISH':
                continue

            symbol = str(row.get(ticker_col, '')).strip().upper()
            if not symbol or symbol in ('', 'NAN', 'NONE'):
                continue

            # Parse signal time
            try:
                sig_time = datetime.strptime(
                    str(row.get(time_col, '')), '%Y-%m-%d %H:%M:%S'
                ).replace(tzinfo=timezone.utc)
            except:
                continue

            if sig_time < cutoff:
                continue  # too old

            entry = 0.0
            try:
                entry = float(row.get(price_col, 0) or 0)
            except:
                pass

            # Keep the most recent signal per symbol
            if symbol not in coins or sig_time > coins[symbol]['signal_time']:
                coins[symbol] = {
                    'entry_price': entry,
                    'signal_time': sig_time,
                    'source':      os.path.basename(path),
                    'is_crypto':   is_crypto,
                }

    return coins

# ─────────────────────────────────────────────
# CANDLE FILE HELPERS
# ─────────────────────────────────────────────

def candle_path(symbol: str) -> str:
    return f"{HISTORY_DIR}/{symbol}_5m.csv"


def load_candles(symbol: str) -> pd.DataFrame:
    path = candle_path(symbol)
    if not os.path.exists(path):
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    try:
        return pd.read_csv(path, dtype=str)
    except:
        return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])


def save_candles(symbol: str, df: pd.DataFrame):
    df = df.drop_duplicates(subset=['timestamp']).sort_values('timestamp')
    df.to_csv(candle_path(symbol), index=False)

# ─────────────────────────────────────────────
# FETCH FROM BINANCE
# ─────────────────────────────────────────────

def fetch_stock_candles(symbol: str, since_ms: int = None) -> pd.DataFrame:
    """Fetch 5m candles for a stock via yfinance."""
    try:
        import yfinance as yf
        t    = yf.Ticker(symbol)
        hist = t.history(period='5d', interval='5m')
        if hist.empty:
            return pd.DataFrame()
        if hist.index.tzinfo is None:
            hist.index = hist.index.tz_localize('UTC')
        else:
            hist.index = hist.index.tz_convert(timezone.utc)
        rows = []
        for ts, row in hist.iterrows():
            ts_ms = int(ts.timestamp() * 1000)
            if since_ms and ts_ms <= since_ms:
                continue
            rows.append({
                'timestamp': str(ts_ms),
                'open':      str(row['Open']),
                'high':      str(row['High']),
                'low':       str(row['Low']),
                'close':     str(row['Close']),
                'volume':    str(row['Volume']),
            })
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        log.warning(f"[yfinance] {symbol}: {e}")
        return pd.DataFrame()


def fetch_candles(symbol: str, since_ms: int = None) -> pd.DataFrame:
    """Fetch 5m candles from Binance. Backfills from since_ms if provided."""
    pairs = [f"{symbol}/USDT", f"{symbol}/USDC", f"{symbol}/USD"]
    rows  = []

    for pair in pairs:
        try:
            ohlcv = exchange.fetch_ohlcv(
                pair, '5m',
                since=since_ms,
                limit=CANDLE_LIMIT
            )
            if ohlcv:
                for c in ohlcv:
                    rows.append({
                        'timestamp': c[0],
                        'open':      c[1],
                        'high':      c[2],
                        'low':       c[3],
                        'close':     c[4],
                        'volume':    c[5],
                    })
                break
        except:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df['timestamp'] = df['timestamp'].astype(str)
    df['open']      = df['open'].astype(str)
    df['high']      = df['high'].astype(str)
    df['low']       = df['low'].astype(str)
    df['close']     = df['close'].astype(str)
    df['volume']    = df['volume'].astype(str)
    return df

# ─────────────────────────────────────────────
# PEAK PRICE — update back into signal CSVs
# ─────────────────────────────────────────────

def update_peak_in_csvs(symbol: str, peak: float, current: float):
    """Write peak_price and current_price back into every signal CSV that has this ticker."""
    for path in SIGNAL_CSVS:
        if not os.path.exists(path):
            continue
        try:
            df = pd.read_csv(path, dtype=str)
            ticker_col = ('ticker' if 'ticker' in df.columns
                          else 'token_symbol' if 'token_symbol' in df.columns
                          else None)
            if ticker_col is None:
                continue

            mask = df[ticker_col].str.upper() == symbol.upper()
            if not mask.any():
                continue

            # Add columns if missing
            for col in ['price_peak', 'price_current', 'pct_peak', 'pct_current']:
                if col not in df.columns:
                    df[col] = ''

            for idx in df[mask].index:
                entry_str = str(df.at[idx, 'price_entry']).strip()
                try:
                    entry = float(entry_str)
                except:
                    entry = 0

                df.at[idx, 'price_peak']    = str(round(peak, 8))
                df.at[idx, 'price_current'] = str(round(current, 8))
                if entry > 0:
                    df.at[idx, 'pct_peak']    = str(round((peak - entry) / entry * 100, 1))
                    df.at[idx, 'pct_current'] = str(round((current - entry) / entry * 100, 1))

            df.to_csv(path, index=False)
        except Exception as e:
            log.warning(f"[PEAK] Could not update {path}: {e}")


def print_summary(symbol: str, entry_price: float, signal_time: datetime):
    df = load_candles(symbol)
    if df.empty:
        return

    try:
        highs   = df['high'].astype(float)
        closes  = df['close'].astype(float)
        peak    = highs.max()
        current = closes.iloc[-1]
        candles_n   = len(df)
        hours_ago   = round(candles_n * 5 / 60, 1)

        if entry_price > 0:
            pct_peak    = round((peak - entry_price) / entry_price * 100, 1)
            pct_current = round((current - entry_price) / entry_price * 100, 1)
            log.info(
                f"[{symbol}] entry:{entry_price:.6f} "
                f"| current:{current:.6f} ({'+' if pct_current >= 0 else ''}{pct_current}%) "
                f"| peak:{peak:.6f} ({'+' if pct_peak >= 0 else ''}{pct_peak}%) "
                f"| {candles_n} candles ({hours_ago}h tracked)"
            )
        else:
            log.info(f"[{symbol}] current:{current:.6f} | peak:{peak:.6f} | {hours_ago}h tracked")

        # Write peak back into signal CSVs
        update_peak_in_csvs(symbol, peak, current)

    except:
        pass


# ─────────────────────────────────────────────
# CROSS-SIGNAL CONFIRMATION
# ─────────────────────────────────────────────

def check_cross_signals():
    """
    Find coins flagged BULLISH by 2+ different bots within CROSS_SIGNAL_HOURS.
    Logs a warning — these are highest conviction signals.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CROSS_SIGNAL_HOURS)
    seen   = {}  # {symbol: [source1, source2, ...]}

    for path in SIGNAL_CSVS:
        if not os.path.exists(path):
            continue
        try:
            df         = pd.read_csv(path, dtype=str)
            ticker_col = ('ticker' if 'ticker' in df.columns
                          else 'token_symbol' if 'token_symbol' in df.columns
                          else None)
            if ticker_col is None:
                continue

            for _, row in df.iterrows():
                if str(row.get('signal', '')).upper() != 'BULLISH':
                    continue
                symbol = str(row.get(ticker_col, '')).strip().upper()
                if not symbol or symbol in ('', 'NAN', 'NONE'):
                    continue
                try:
                    sig_time = datetime.strptime(
                        str(row.get('logged_at', '')), '%Y-%m-%d %H:%M:%S'
                    ).replace(tzinfo=timezone.utc)
                except:
                    continue
                if sig_time < cutoff:
                    continue

                source = os.path.basename(path).replace('_paper.csv', '').replace('_bot', '')
                seen.setdefault(symbol, [])
                if source not in seen[symbol]:
                    seen[symbol].append(source)
        except:
            continue

    for symbol, sources in seen.items():
        if len(sources) >= 2:
            log.warning(
                f"[CROSS-SIGNAL] *** HIGH CONVICTION *** {symbol} flagged by "
                f"{len(sources)} bots: {', '.join(sources)}"
            )

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────

_fail_counts:   dict[str, int] = {}   # consecutive fetch failures per symbol
_fail_reported: set[str]       = set()  # symbols already warned about — suppress repeats
MAX_CONSECUTIVE_FAILS = 3            # skip symbol after this many in a row

FAIL_COUNTS_FILE = f"{OUTPUT_DIR}/coin_monitor_fails.json"

def _load_fail_counts():
    """Persist failed symbols across restarts so they don't re-spam on startup."""
    global _fail_counts, _fail_reported
    if not os.path.exists(FAIL_COUNTS_FILE):
        return
    try:
        with open(FAIL_COUNTS_FILE) as f:
            data = json.load(f)
        _fail_counts   = data.get('counts', {})
        _fail_reported = set(data.get('reported', []))
    except:
        pass

def _save_fail_counts():
    try:
        with open(FAIL_COUNTS_FILE, 'w') as f:
            json.dump({'counts': _fail_counts, 'reported': list(_fail_reported)}, f)
    except:
        pass

def run_once(active_coins: dict):
    now = datetime.now(timezone.utc)

    for symbol, info in active_coins.items():
        # Skip symbols that have repeatedly returned no data (delisted/unavailable)
        if _fail_counts.get(symbol, 0) >= MAX_CONSECUTIVE_FAILS:
            log.debug(f"[{symbol}] Skipping — already failed {MAX_CONSECUTIVE_FAILS}x")
            continue

        existing   = load_candles(symbol)
        since_ms   = None

        if not existing.empty:
            last_ts  = int(existing['timestamp'].astype(int).max())
            since_ms = last_ts + (5 * 60 * 1000)
            log.info(f"[{symbol}] Backfilling from {datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}...")
        else:
            since_ms = int(info['signal_time'].timestamp() * 1000)
            log.debug(f"[{symbol}] First fetch from signal time {info['signal_time'].strftime('%Y-%m-%d %H:%M')}...")

        if info.get('is_crypto', True):
            new_candles = fetch_candles(symbol, since_ms=since_ms)
        else:
            new_candles = fetch_stock_candles(symbol, since_ms=since_ms)

        if new_candles.empty:
            _fail_counts[symbol] = _fail_counts.get(symbol, 0) + 1
            fails = _fail_counts[symbol]
            if fails >= MAX_CONSECUTIVE_FAILS and symbol not in _fail_reported:
                log.warning(f"[{symbol}] No candle data after {fails} attempts — skipping (delisted/unavailable)")
                _fail_reported.add(symbol)
                _save_fail_counts()
            elif fails < MAX_CONSECUTIVE_FAILS:
                log.info(f"[{symbol}] No candle data ({fails}/{MAX_CONSECUTIVE_FAILS})")
            continue

        # Successful fetch — reset failure count and reported flag
        if symbol in _fail_reported:
            _fail_reported.discard(symbol)
        _fail_counts[symbol] = 0

        combined = pd.concat([existing, new_candles], ignore_index=True)
        save_candles(symbol, combined)

        log.info(f"[{symbol}] +{len(new_candles)} candles -> {len(combined)} total")
        print_summary(symbol, info['entry_price'], info['signal_time'])

        time.sleep(1)  # rate limit


def main():
    log.info("=" * 60)
    log.info("Coin Monitor | 5m candles | auto-backfill on restart")
    log.info("=" * 60)
    _load_fail_counts()
    if _fail_counts:
        log.info(f"[MONITOR] Loaded {len(_fail_counts)} known failed symbols — skipping silently")

    while True:
        try:
            active_coins = load_active_coins()

            if active_coins:
                log.info(f"[MONITOR] Tracking {len(active_coins)} active coins")
                run_once(active_coins)
            else:
                log.info("[MONITOR] No active signals yet — waiting for bots to fire")

            check_cross_signals()

        except Exception as e:
            log.error(f"[MONITOR] Loop error: {e}")

        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
