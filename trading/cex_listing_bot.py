import sys
"""
cex_listing_bot.py
==================
Monitors Binance and Coinbase for new coin listing announcements.
When a new listing is detected, Claude extracts the ticker and assesses the pump potential.
Logs paper trades to cex_listing_paper.csv and tracks 1h/4h/1d price outcomes.

Sources:
  - Binance: official announcements API (catalogId=48 = New Listings)
  - Coinbase: Exchange products API (tracks newly added trading pairs)

PAPER MODE ONLY — no real trades.

RUN:
    python cex_listing_bot.py
"""

import os
import csv
import json
import time
import logging
import threading
import requests
import yfinance as yf
import anthropic
import ccxt
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# Fix Unicode output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ET_TZ = ZoneInfo('America/New_York')

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

MODEL          = "claude-haiku-4-5-20251001"
MIN_CONFIDENCE = 65
POLL_SECONDS   = 60     # check every 60 seconds — speed matters here

OUTPUT_DIR = "D:/Desktop/Trading Folder"
PAPER_CSV  = f"{OUTPUT_DIR}/cex_listing_paper.csv"
SEEN_FILE  = f"{OUTPUT_DIR}/cex_listing_seen.json"
LOG_FILE   = f"{OUTPUT_DIR}/cex_listing_bot.log"

API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Keywords in Binance titles that signal a genuine new spot listing
BINANCE_LISTING_KEYWORDS = [
    'will list', 'now listed', 'listing', 'will add', 'spot trading'
]
# Keywords to ignore (futures, margin, airdrops, pairs only)
BINANCE_SKIP_KEYWORDS = [
    'futures', 'margin', 'perpetual', 'pre-market', 'hodler', 'airdrop',
    'earn', 'loan', 'convert', 'vip', 'trading bots', 'new pairs'
]

MAX_RESOLVE_PER_CYCLE = 10
RESOLVE_SLEEP = 2.0

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

csv_lock = threading.Lock()
client   = anthropic.Anthropic(api_key=API_KEY)

CSV_HEADER = [
    'logged_at', 'exchange', 'symbol', 'ticker',
    'announcement_title', 'signal', 'confidence', 'reason',
    'price_entry',
    'price_1h',  'correct_1h',
    'price_4h',  'correct_4h',
    'price_1d',  'correct_1d',
    'source_url',
]

# ─────────────────────────────────────────────
# SEEN
# ─────────────────────────────────────────────

def load_seen() -> dict:
    if not os.path.exists(SEEN_FILE):
        return {'binance': [], 'coinbase': []}
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except:
        return {'binance': [], 'coinbase': []}


def save_seen(seen: dict):
    with open(SEEN_FILE, 'w') as f:
        json.dump(seen, f)

# ─────────────────────────────────────────────
# PRICE HELPERS
# ─────────────────────────────────────────────

def get_crypto_price(symbol: str) -> float | None:
    """Get current price from Binance via CCXT."""
    try:
        exchange = ccxt.binance({'enableRateLimit': True})
        ticker = exchange.fetch_ticker(f"{symbol}/USDT")
        return round(float(ticker['last']), 6)
    except:
        pass
    # Fallback: try Hyperliquid
    try:
        exchange = ccxt.hyperliquid({'enableRateLimit': True})
        ticker = exchange.fetch_ticker(f"{symbol}/USDC")
        return round(float(ticker['last']), 6)
    except:
        return None


def get_crypto_price_at(symbol: str, target_time: datetime) -> float | None:
    """Get historical price from Binance OHLCV."""
    try:
        exchange = ccxt.binance({'enableRateLimit': True})
        since    = int((target_time - timedelta(hours=2)).timestamp() * 1000)
        ohlcv    = exchange.fetch_ohlcv(f"{symbol}/USDT", '1h', since=since, limit=5)
        if not ohlcv:
            return None
        # Find closest bar to target
        target_ms = int(target_time.timestamp() * 1000)
        closest   = min(ohlcv, key=lambda x: abs(x[0] - target_ms))
        return round(float(closest[4]), 6)  # close price
    except:
        return None

# ─────────────────────────────────────────────
# BINANCE FEED
# ─────────────────────────────────────────────

def fetch_binance_listings() -> list[dict]:
    """Fetch recent Binance listing announcements."""
    try:
        url = 'https://www.binance.com/bapi/composite/v1/public/cms/article/list/query'
        params = {'type': 1, 'pageNo': 1, 'pageSize': 20, 'catalogId': 48}
        r = requests.get(url, params=params, timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        data = r.json()
        articles = data['data']['catalogs'][0]['articles']
        return [
            {
                'id':       str(a['id']),
                'title':    a['title'],
                'ts':       a['releaseDate'],
                'url':      f"https://www.binance.com/en/support/announcement/{a['code']}",
                'exchange': 'Binance',
            }
            for a in articles
        ]
    except Exception as e:
        log.warning(f"[BINANCE] Feed error: {e}")
        return []


def is_binance_spot_listing(title: str) -> bool:
    """Returns True if this looks like a genuine new spot listing."""
    title_lower = title.lower()
    if any(k in title_lower for k in BINANCE_SKIP_KEYWORDS):
        return False
    return any(k in title_lower for k in BINANCE_LISTING_KEYWORDS)

# ─────────────────────────────────────────────
# COINBASE FEED
# ─────────────────────────────────────────────

def fetch_coinbase_products() -> set[str]:
    """Returns set of all currently listed Coinbase product IDs (e.g. BTC-USD)."""
    try:
        r = requests.get('https://api.exchange.coinbase.com/products', timeout=10,
                         headers={'User-Agent': 'Mozilla/5.0'})
        products = r.json()
        return {p['id'] for p in products if p.get('status') == 'online'}
    except Exception as e:
        log.warning(f"[COINBASE] Products fetch error: {e}")
        return set()

# ─────────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────────

def ask_claude(exchange: str, title: str, symbol: str = '') -> tuple[str, str, int, str]:
    """
    Returns (ticker, signal, confidence, reason).
    signal: BULLISH or SKIP
    """
    prompt = f"""You are a crypto trader analysing a new exchange listing announcement.

Exchange: {exchange}
Announcement: {title}
{f'Symbol detected: {symbol}' if symbol else ''}

A new listing on a major CEX (Coinbase/Binance) typically causes a 20-100% price pump within hours.

Your job:
1. Extract the crypto ticker/symbol from the announcement (e.g. BTC, ETH, PEPE)
2. Assess if this is a genuine NEW spot listing (not futures, not margin, not an airdrop)
3. Estimate pump potential — small/unknown coins pump more than established ones

Rules:
- BULLISH if: genuine new spot listing of a small/mid cap coin not yet widely available
- SKIP if: futures only, margin only, already listed everywhere, stable coin, or major coin (BTC/ETH/BNB)
- SKIP if: this is just adding new trading pairs for existing coins

Respond in this exact format:
TICKER: XXX
SIGNAL: BULLISH or SKIP
CONFIDENCE: 0-100
REASON: one sentence"""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()

        ticker     = symbol
        signal     = 'SKIP'
        confidence = 0
        reason     = ''

        for line in text.splitlines():
            if line.startswith('TICKER:'):
                ticker = line.split(':', 1)[1].strip().upper()
            elif line.startswith('SIGNAL:'):
                signal = line.split(':', 1)[1].strip().upper()
            elif line.startswith('CONFIDENCE:'):
                try:
                    confidence = int(line.split(':', 1)[1].strip())
                except:
                    pass
            elif line.startswith('REASON:'):
                reason = line.split(':', 1)[1].strip()

        return ticker, signal, confidence, reason

    except Exception as e:
        log.error(f"[CLAUDE] Error: {e}")
        return symbol, 'SKIP', 0, str(e)

# ─────────────────────────────────────────────
# CSV LOGGING
# ─────────────────────────────────────────────

def log_signal(exchange: str, symbol: str, ticker: str, title: str,
               signal: str, confidence: int, reason: str, price, url: str):
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    row = {
        'logged_at':           now_str,
        'exchange':            exchange,
        'symbol':              symbol,
        'ticker':              ticker,
        'announcement_title':  title,
        'signal':              signal,
        'confidence':          confidence,
        'reason':              reason,
        'price_entry':         price if price is not None else '',
        'price_1h':  '', 'correct_1h': '',
        'price_4h':  '', 'correct_4h': '',
        'price_1d':  '', 'correct_1d': '',
        'source_url':          url,
    }

    write_header = not os.path.exists(PAPER_CSV)
    with csv_lock:
        with open(PAPER_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    log.info(f"[LOG] {exchange} | {ticker} | {signal} {confidence}% | {reason}")

# ─────────────────────────────────────────────
# RESOLVER
# ─────────────────────────────────────────────

def update_resolutions():
    while True:
        try:
            if not os.path.exists(PAPER_CSV):
                time.sleep(300)
                continue

            import pandas as pd
            with csv_lock:
                df = pd.read_csv(PAPER_CSV, dtype=str)

            now        = datetime.now(timezone.utc)
            updated    = False
            calls_made = 0

            for idx, row in df.iterrows():
                if calls_made >= MAX_RESOLVE_PER_CYCLE:
                    break

                signal = str(row.get('signal', '')).upper()
                if signal != 'BULLISH':
                    continue

                ticker = str(row.get('ticker', '')).strip()
                if not ticker:
                    continue

                try:
                    base_time = datetime.strptime(
                        str(row.get('logged_at', '')), '%Y-%m-%d %H:%M:%S'
                    ).replace(tzinfo=timezone.utc)
                except:
                    continue

                entry_str = str(row.get('price_entry', '')).strip()
                if entry_str in ('', 'nan', 'None') or entry_str == '0':
                    ep = get_crypto_price(ticker)
                    calls_made += 1
                    time.sleep(RESOLVE_SLEEP)
                    if ep:
                        df.at[idx, 'price_entry'] = str(ep)
                        entry_str = str(ep)
                        updated = True
                    else:
                        continue

                try:
                    entry_price = float(entry_str)
                except:
                    continue
                if entry_price == 0:
                    continue

                for label, hours, price_col, correct_col in [
                    ('1h',  1,  'price_1h',  'correct_1h'),
                    ('4h',  4,  'price_4h',  'correct_4h'),
                    ('1d',  24, 'price_1d',  'correct_1d'),
                ]:
                    if calls_made >= MAX_RESOLVE_PER_CYCLE:
                        break
                    if str(row.get(correct_col, '')).strip() not in ('', 'nan', 'None'):
                        continue
                    target = base_time + timedelta(hours=hours)
                    if now < target:
                        continue

                    hist_price = get_crypto_price_at(ticker, target)
                    calls_made += 1
                    time.sleep(RESOLVE_SLEEP)
                    if hist_price is None:
                        continue

                    correct = hist_price > entry_price
                    df.at[idx, price_col]   = str(hist_price)
                    df.at[idx, correct_col] = str(correct)
                    updated = True
                    log.info(f"[RESOLVE] {ticker} {label} => "
                             f"{'WIN' if correct else 'LOSS'} "
                             f"(entry:{entry_price} -> @{label}:{hist_price})")

            if updated:
                with csv_lock:
                    df.to_csv(PAPER_CSV, index=False)

        except Exception as e:
            log.error(f"[RESOLVE] Error: {e}")

        time.sleep(300)

# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

def scan(seen: dict, coinbase_known: set) -> tuple[dict, set]:
    now = datetime.now(timezone.utc)

    # ── Binance ──────────────────────────────
    articles = fetch_binance_listings()
    for article in articles:
        uid = article['id']
        if uid in seen['binance']:
            continue
        seen['binance'].append(uid)

        title = article['title']
        if not is_binance_spot_listing(title):
            log.info(f"[BINANCE] Skip (not spot listing): {title[:60]}")
            continue

        log.info(f"[BINANCE] New listing detected: {title[:80]}")

        ticker, signal, confidence, reason = ask_claude('Binance', title)

        if signal == 'SKIP' or not ticker:
            log.info(f"[BINANCE] SKIP: {reason}")
            continue
        if confidence < MIN_CONFIDENCE:
            log.info(f"[BINANCE] Low confidence ({confidence}%) — skipping")
            continue

        price = get_crypto_price(ticker)
        log_signal('Binance', ticker, ticker, title, signal, confidence, reason, price, article['url'])

    # Keep seen list manageable
    seen['binance'] = seen['binance'][-200:]

    # ── Coinbase ─────────────────────────────
    current_products = fetch_coinbase_products()
    if coinbase_known:
        new_products = current_products - coinbase_known
        for product_id in new_products:
            # product_id format: BTC-USD, PEPE-USDC etc
            parts = product_id.split('-')
            if len(parts) < 2:
                continue
            symbol = parts[0]
            if symbol in ('USD', 'USDC', 'USDT', 'BTC', 'ETH', 'BNB'):
                continue

            if product_id in seen.get('coinbase', []):
                continue
            seen.setdefault('coinbase', []).append(product_id)

            title = f"Coinbase lists {product_id}"
            log.info(f"[COINBASE] New listing detected: {product_id}")

            ticker, signal, confidence, reason = ask_claude('Coinbase', title, symbol)

            if signal == 'SKIP' or not ticker:
                log.info(f"[COINBASE] SKIP: {reason}")
                continue
            if confidence < MIN_CONFIDENCE:
                log.info(f"[COINBASE] Low confidence ({confidence}%) — skipping")
                continue

            price = get_crypto_price(ticker)
            url   = f"https://www.coinbase.com/price/{symbol.lower()}"
            log_signal('Coinbase', product_id, ticker, title, signal, confidence, reason, price, url)

    save_seen(seen)
    return seen, current_products


def main():
    log.info("=" * 60)
    log.info("CEX Listing Bot | PAPER MODE")
    log.info("Monitors Binance + Coinbase for new coin listings")
    log.info(f"Polling every {POLL_SECONDS}s")
    log.info("=" * 60)

    seen = load_seen()

    # Seed Coinbase known products on first run (don't alert on existing listings)
    log.info("[COINBASE] Seeding known products list...")
    coinbase_known = fetch_coinbase_products()
    log.info(f"[COINBASE] {len(coinbase_known)} products already listed — watching for new ones")

    # Start resolver thread
    t = threading.Thread(target=update_resolutions, daemon=True)
    t.start()

    while True:
        try:
            seen, coinbase_known = scan(seen, coinbase_known)
        except Exception as e:
            log.error(f"[CEX] Loop error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
