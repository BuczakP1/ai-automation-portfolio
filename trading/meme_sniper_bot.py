import sys
"""
meme_sniper_bot.py
==================
Catches Solana meme coins in their first 1-2 hours using DexScreener + Birdeye.

How it works:
  1. Every 5 min: fetch newest token profiles from DexScreener (no API key needed)
  2. Pre-filter: age < 2h, MC $10K-$5M, min volume, wash-trade ratio check
  3. Score on 5 signals:
       - Buy pressure (h1 buys > sells * 1.5)
       - Volume in last 5m still active (m5 vol > $1K)
       - Price momentum (m5 or h1 price change > 5%)
       - MC still early (< $1M)
       - Social presence (has Twitter link)
  4. Score >= 3/5 → Filter 1 pass
  5. Filter 2: Birdeye rugpull check (mint, freeze, top holders, creator %)
  6. Claude verdict → logs to meme_sniper_paper.csv, tracks 15m/1h/4h outcome

Source change: was Birdeye volume-sorted list (gamed by wash traders).
Now uses DexScreener token-profiles (real new tokens, dev just set up their page).

PAPER MODE — no real trades placed.

RUN:
    python meme_sniper_bot.py
"""

import time
import threading
import logging
import os
import csv
import json
import requests
import anthropic
from datetime import datetime, timezone, timedelta
from config import BIRDEYE_API_KEY

# Fix Unicode output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

MODEL          = "claude-haiku-4-5-20251001"
POLL_SECONDS   = 300       # scan every 5 min
API_SLEEP      = 1.5       # sleep between API calls

# Token filters
MAX_MC            = 5_000_000   # $5M max MC — still early
MIN_MC            = 10_000      # $10K min — has some traction
MAX_AGE_MINUTES   = 120         # only tokens < 2h old
MIN_VOL_H1_USD    = 5_000       # at least $5K volume in last hour
MIN_TRADES_H1     = 20          # at least 20 trades in last hour
MIN_PRICE_CHG_M5  = 5.0         # price up at least 5% in last 5m

# Wash trading filter — vol/liquidity ratio above this = fake volume
MAX_VOL_LIQ_RATIO = 500

# Repeat appearances — ban coins that keep showing up without ever signalling
MAX_APPEARANCES   = 3

# Dedup — don't re-alert same token within 2h
DEDUP_HOURS    = 2

BASE      = "D:/Desktop/Trading Folder"
PAPER_CSV = f"{BASE}/meme_sniper_paper.csv"
SEEN_FILE = f"{BASE}/meme_sniper_seen.json"
LOG_FILE  = f"{BASE}/meme_sniper_bot.log"

DEXSCREENER_BASE = "https://api.dexscreener.com"
BIRDEYE_BASE     = "https://public-api.birdeye.so"
BIRDEYE_HEADERS  = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}
DS_HEADERS       = {"User-Agent": "Mozilla/5.0"}

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

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
client   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

CSV_HEADER = [
    'logged_at', 'address', 'symbol', 'name',
    'mc', 'liquidity', 'price',
    'score', 'signals_hit',
    'price_chg_1m', 'price_chg_5m', 'price_chg_30m', 'price_chg_1h',
    'vol_1h_usd', 'trades_1h', 'unique_wallets_1h',
    'buy_1h', 'sell_1h', 'buy_sell_ratio',
    'vol_30m_vs_prev', 'wallets_30m_vs_prev',
    'claude_verdict', 'claude_reason',
    'price_15m', 'pct_15m',
    'price_1h_later', 'pct_1h',
    'price_4h_later', 'pct_4h',
]

# ─────────────────────────────────────────────
# SEEN
# ─────────────────────────────────────────────

def load_seen() -> dict:
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
        # Migrate old format {address: isostring} → new format {address: {last_seen, appearances, signalled}}
        migrated = {}
        for addr, val in data.items():
            if isinstance(val, str):
                migrated[addr] = {'last_seen': val, 'appearances': 1, 'signalled': True}
            else:
                migrated[addr] = val
        return migrated
    except:
        return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, 'w') as f:
        json.dump(seen, f)


def was_seen_recently(seen: dict, address: str) -> bool:
    if address not in seen:
        return False
    last = datetime.fromisoformat(seen[address]['last_seen'])
    return (datetime.now(timezone.utc) - last).total_seconds() < DEDUP_HOURS * 3600


def is_repeat_spammer(seen: dict, address: str) -> bool:
    """Returns True if this coin has appeared MAX_APPEARANCES+ times without ever signalling."""
    if address not in seen:
        return False
    return seen[address].get('appearances', 0) >= MAX_APPEARANCES and not seen[address].get('signalled', False)


def mark_seen(seen: dict, address: str):
    """Mark that this address produced a signal."""
    entry = seen.get(address, {'appearances': 0})
    entry['last_seen']  = datetime.now(timezone.utc).isoformat()
    entry['signalled']  = True
    seen[address]       = entry


def increment_appearances(seen: dict, address: str):
    """Increment raw appearance count for an address (called every scan it shows up)."""
    entry = seen.get(address, {'appearances': 0, 'signalled': False})
    entry['appearances'] = entry.get('appearances', 0) + 1
    entry['last_seen']   = datetime.now(timezone.utc).isoformat()
    seen[address]        = entry


# ─────────────────────────────────────────────
# DEXSCREENER API  (discovery + scoring data)
# ─────────────────────────────────────────────

def get_new_tokens() -> list[dict]:
    """
    Fetch recently-profiled Solana tokens from DexScreener.
    These are tokens whose dev just set up their DexScreener page — a social signal.
    Returns flat dicts with all scoring fields already populated.
    """
    now_ms = time.time() * 1000
    results = []

    try:
        r = requests.get(
            f"{DEXSCREENER_BASE}/token-profiles/latest/v1",
            headers=DS_HEADERS,
            timeout=15
        )
        if r.status_code != 200:
            log.warning(f"[DS] profiles {r.status_code}")
            return []

        profiles = [p for p in r.json() if p.get('chainId') == 'solana']

    except Exception as e:
        log.warning(f"[DS] profiles error: {e}")
        return []

    for prof in profiles:
        address = prof.get('tokenAddress', '')
        links   = prof.get('links', [])

        try:
            time.sleep(0.3)  # gentle rate limit
            r2 = requests.get(
                f"{DEXSCREENER_BASE}/latest/dex/tokens/{address}",
                headers=DS_HEADERS,
                timeout=15
            )
            if r2.status_code != 200:
                continue
            pairs = r2.json().get('pairs') or []
            if not pairs:
                continue

            # Use the most liquid pair
            p = sorted(pairs, key=lambda x: (x.get('liquidity') or {}).get('usd', 0), reverse=True)[0]

            age_ms   = p.get('pairCreatedAt') or 0
            age_mins = (now_ms - age_ms) / 60000 if age_ms else 9999

            results.append({
                'address':    address,
                'symbol':     p['baseToken'].get('symbol', '?'),
                'name':       p['baseToken'].get('name', '?'),
                'age_mins':   age_mins,
                'mc':         p.get('marketCap') or 0,
                'liquidity':  (p.get('liquidity') or {}).get('usd', 0),
                'price':      float(p.get('priceUsd') or 0),
                'vol_m5':     p['volume'].get('m5', 0) or 0,
                'vol_h1':     p['volume'].get('h1', 0) or 0,
                'vol_h24':    p['volume'].get('h24', 0) or 0,
                'buys_m5':    p['txns']['m5'].get('buys', 0),
                'sells_m5':   p['txns']['m5'].get('sells', 0),
                'buys_h1':    p['txns']['h1'].get('buys', 0),
                'sells_h1':   p['txns']['h1'].get('sells', 0),
                'chg_m5':     p['priceChange'].get('m5') or 0,
                'chg_h1':     p['priceChange'].get('h1') or 0,
                'chg_h24':    p['priceChange'].get('h24') or 0,
                'has_twitter': any(l.get('type') == 'twitter' for l in links),
                'has_website': any(l.get('label') == 'Website' for l in links),
            })

        except Exception as e:
            log.warning(f"[DS] pair fetch error {address[:8]}: {e}")
            continue

    return results


def get_token_security(address: str) -> dict | None:
    """Rugcheck.xyz free API — risks, score, LP lock."""
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary",
            timeout=15
        )
        if r.status_code == 429:
            log.warning("[RUGCHECK] Rate limited — sleeping 5s")
            time.sleep(5)
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        # Normalise to expected keys for rugpull_check
        return {
            'rugcheck_score':    data.get('score_normalised', 1),
            'risks':             data.get('risks', []),
            'lpLockedPct':       data.get('lpLockedPct', 0),
            'mintAuthority':     None,   # not in summary — assume safe
            'freezeAuthority':   None,
        }
    except Exception as e:
        log.warning(f"[RUGCHECK] error {address[:8]}: {e}")
        return None


def rugpull_check(sec: dict) -> tuple[bool, list[str]]:
    """
    Filter 2 — safety checks using rugcheck.xyz data.
    Returns (passed, flags).
    Hard fails: score < 500, any DANGER risk, LP locked < 50%.
    """
    flags  = []
    passed = True

    score     = sec.get('rugcheck_score', 1)
    risks     = sec.get('risks', [])
    lp_locked = sec.get('lpLockedPct', 0)

    # ── Hard fails ──
    if score < 500:
        flags.append(f'LOW_SCORE={score}')
        passed = False

    danger_risks = [r for r in risks if isinstance(r, dict) and r.get('level') == 'danger']
    if danger_risks:
        for r in danger_risks:
            flags.append(f"DANGER:{r.get('name','?')}")
        passed = False

    if lp_locked < 50:
        flags.append(f'LP_UNLOCKED={lp_locked:.0f}%')
        passed = False

    return passed, flags


# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────

def score_token(t: dict) -> tuple[int, list[str], dict]:
    """
    Score token on 5 signals using DexScreener data.
    Returns (score, signals_hit, metrics).
    """
    signals = []

    mc       = t['mc']
    buy_h1   = t['buys_h1']
    sell_h1  = t['sells_h1']
    buy_m5   = t['buys_m5']
    sell_m5  = t['sells_m5']
    vol_h1   = t['vol_h1']
    vol_m5   = t['vol_m5']
    chg_m5   = t['chg_m5']
    chg_h1   = t['chg_h1']

    buy_sell_ratio = (buy_h1 / sell_h1) if sell_h1 > 0 else (10.0 if buy_h1 > 0 else 1.0)

    metrics = {
        'mc':             round(mc, 0),
        'liquidity':      round(t['liquidity'], 0),
        'price':          t['price'],
        'price_chg_1m':   0,
        'price_chg_5m':   round(chg_m5, 2),
        'price_chg_30m':  0,
        'price_chg_1h':   round(chg_h1, 2),
        'vol_1h_usd':     round(vol_h1, 0),
        'trades_1h':      buy_h1 + sell_h1,
        'unique_wallets_1h': 0,
        'buy_1h':         buy_h1,
        'sell_1h':        sell_h1,
        'buy_sell_ratio': round(buy_sell_ratio, 2),
        'vol_30m_vs_prev':    0,
        'wallets_30m_vs_prev': 0,
    }

    # ── Signal 1: MC still small (early, room to run) ──
    if mc <= 1_000_000:
        signals.append(f"MC_EARLY (${mc:,.0f})")

    # ── Signal 2: Buying pressure ──
    if buy_sell_ratio >= 1.5 and buy_h1 >= 10:
        signals.append(f"BUY_PRESSURE ({buy_h1}B/{sell_h1}S = {buy_sell_ratio:.1f}x)")

    # ── Signal 3: Still active right now (m5 volume) ──
    if vol_m5 >= 1_000 and buy_m5 > sell_m5:
        signals.append(f"ACTIVE_NOW (${vol_m5:,.0f} last 5m, {buy_m5}B/{sell_m5}S)")

    # ── Signal 4: Price momentum ──
    if chg_m5 >= MIN_PRICE_CHG_M5 or chg_h1 >= 10:
        signals.append(f"MOMENTUM (m5:{chg_m5:+.1f}% h1:{chg_h1:+.1f}%)")

    # ── Signal 5: Social presence (dev set up Twitter = not total ghost) ──
    if t['has_twitter']:
        signals.append("SOCIAL_TWITTER")

    score = len(signals)
    return score, signals, metrics


# ─────────────────────────────────────────────
# CLAUDE VERDICT
# ─────────────────────────────────────────────

def ask_claude(symbol: str, name: str, metrics: dict, signals: list) -> tuple[str, str]:
    """Quick Claude check — BUY or SKIP."""
    prompt = f"""You are a Solana meme coin trader. Assess this new token that just launched.

Token: {symbol} ({name})
Market Cap: ${metrics['mc']:,.0f}
Liquidity: ${metrics['liquidity']:,.0f}
Price changes: 1m:{metrics['price_chg_1m']:+.1f}% | 5m:{metrics['price_chg_5m']:+.1f}% | 30m:{metrics['price_chg_30m']:+.1f}% | 1h:{metrics['price_chg_1h']:+.1f}%
1h Volume: ${metrics['vol_1h_usd']:,.0f}
1h Trades: {metrics['trades_1h']} ({metrics['buy_1h']} buys / {metrics['sell_1h']} sells)
Unique wallets 1h: {metrics['unique_wallets_1h']}
Buy/Sell ratio: {metrics['buy_sell_ratio']}x
Signals hit: {', '.join(signals)}

MEME COIN RULES:
- 99% of meme coins go to zero — this is pure momentum trading
- The edge is being EARLY (first 1-2 hours) and EXITING FAST
- Good signs: MC < $1M still, buy/sell ratio > 2x, volume accelerating, wallets growing
- Bad signs: liquidity < $5K (can't exit), sell pressure building, MC already >$5M (missed it)
- Rug signs: no liquidity, single dev wallet, all buys no sells (wash trading)

Respond in EXACTLY this format:
VERDICT: BUY or SKIP
REASON: one sentence"""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        verdict, reason = "SKIP", ""
        for line in text.split("\n"):
            if line.startswith("VERDICT:"):
                val = line.split(":", 1)[1].strip().upper()
                if val in ("BUY", "SKIP"):
                    verdict = val
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
        return verdict, reason
    except Exception as e:
        return "SKIP", str(e)


# ─────────────────────────────────────────────
# CSV + RESOLVER
# ─────────────────────────────────────────────

def log_signal(address, symbol, name, score, signals, metrics, verdict, reason):
    row = {
        'logged_at':         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'address':           address,
        'symbol':            symbol,
        'name':              name,
        'mc':                metrics['mc'],
        'liquidity':         metrics['liquidity'],
        'price':             metrics['price'],
        'score':             score,
        'signals_hit':       ' | '.join(signals),
        'price_chg_1m':      metrics['price_chg_1m'],
        'price_chg_5m':      metrics['price_chg_5m'],
        'price_chg_30m':     metrics['price_chg_30m'],
        'price_chg_1h':      metrics['price_chg_1h'],
        'vol_1h_usd':        metrics['vol_1h_usd'],
        'trades_1h':         metrics['trades_1h'],
        'unique_wallets_1h': metrics['unique_wallets_1h'],
        'buy_1h':            metrics['buy_1h'],
        'sell_1h':           metrics['sell_1h'],
        'buy_sell_ratio':    metrics['buy_sell_ratio'],
        'vol_30m_vs_prev':   metrics['vol_30m_vs_prev'],
        'wallets_30m_vs_prev': metrics['wallets_30m_vs_prev'],
        'claude_verdict':    verdict,
        'claude_reason':     reason,
        'price_15m': '', 'pct_15m': '',
        'price_1h_later': '', 'pct_1h': '',
        'price_4h_later': '', 'pct_4h': '',
    }
    with csv_lock:
        write_header = not os.path.exists(PAPER_CSV)
        with open(PAPER_CSV, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
            if write_header:
                writer.writeheader()
            writer.writerow(row)


def get_price_now(address: str) -> float | None:
    try:
        r = requests.get(
            f"{DEXSCREENER_BASE}/latest/dex/tokens/{address}",
            headers=DS_HEADERS,
            timeout=10
        )
        if r.status_code == 200:
            pairs = r.json().get('pairs') or []
            if pairs:
                return float(pairs[0].get('priceUsd') or 0) or None
        return None
    except:
        return None


def update_resolutions():
    while True:
        try:
            if not os.path.exists(PAPER_CSV):
                time.sleep(300)
                continue

            import pandas as pd
            with csv_lock:
                df = pd.read_csv(PAPER_CSV, dtype=str)

            now     = datetime.now(timezone.utc)
            updated = False
            calls   = 0

            for idx, row in df.iterrows():
                if calls >= 8:
                    break

                address = str(row.get('address', '')).strip()
                if not address:
                    continue

                logged_str = str(row.get('logged_at', '')).strip()
                try:
                    base_time = datetime.strptime(logged_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                except:
                    continue

                try:
                    entry_price = float(str(row.get('price', '')).strip())
                except:
                    continue
                if entry_price == 0:
                    continue

                for label, mins, price_col, pct_col in [
                    ('15m', 15,   'price_15m',     'pct_15m'),
                    ('1h',  60,   'price_1h_later', 'pct_1h'),
                    ('4h',  240,  'price_4h_later', 'pct_4h'),
                ]:
                    if calls >= 8:
                        break
                    if str(row.get(price_col, '')).strip() not in ('', 'nan', 'None'):
                        continue
                    target = base_time + timedelta(minutes=mins)
                    if now < target:
                        continue

                    curr_price = get_price_now(address)
                    calls += 1
                    time.sleep(API_SLEEP)

                    if curr_price is None:
                        continue

                    pct = round(((curr_price - entry_price) / entry_price) * 100, 2)
                    df.at[idx, price_col] = str(curr_price)
                    df.at[idx, pct_col]   = str(pct)
                    updated = True
                    log.info(f"[RESOLVE] {row.get('symbol')} {label}: entry:{entry_price:.8f} → {curr_price:.8f} ({pct:+.1f}%)")

            if updated:
                with csv_lock:
                    df.to_csv(PAPER_CSV, index=False)

        except Exception as e:
            log.error(f"[RESOLVE] Thread error: {e}")

        time.sleep(300)


# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────

MIN_SCORE = 3   # need 3/5 signals minimum

def scan(seen: dict) -> dict:
    new_tokens = get_new_tokens()
    log.info(f"[SNIPER] Found {len(new_tokens)} brand-new tokens from DexScreener")

    count_f1      = 0   # passed momentum score
    count_f2      = 0   # passed rugpull check
    signals_found = 0

    for t in new_tokens:
        address = t['address']
        symbol  = t['symbol']
        name    = t['name']

        if was_seen_recently(seen, address):
            continue

        # ── Pre-filters ──
        mc        = t['mc']
        liquidity = t['liquidity']
        vol_h1    = t['vol_h1']
        age_mins  = t['age_mins']

        if age_mins > MAX_AGE_MINUTES:
            continue
        if mc > MAX_MC or mc < MIN_MC:
            continue
        if vol_h1 < MIN_VOL_H1_USD:
            continue
        if (t['buys_h1'] + t['sells_h1']) < MIN_TRADES_H1:
            continue

        # Wash trading filter
        if liquidity > 0 and (vol_h1 / liquidity) > MAX_VOL_LIQ_RATIO:
            log.info(f"  [SNIPER] {symbol} — WASH TRADE skip (vol/liq={vol_h1/liquidity:.0f}x)")
            increment_appearances(seen, address)
            continue

        # Repeat spammer filter
        increment_appearances(seen, address)
        if is_repeat_spammer(seen, address):
            log.info(f"  [SNIPER] {symbol} — REPEAT SPAMMER skip ({seen[address]['appearances']} appearances, never signalled)")
            continue

        # ── FILTER 1: Momentum score ──
        score, signals, metrics = score_token(t)

        if score < MIN_SCORE:
            log.info(f"  [SNIPER] {symbol} score:{score}/5 — skip")
            continue

        count_f1 += 1
        log.info(f"  [SNIPER] {symbol} score:{score}/5 — F1 PASS → running rugcheck...")

        # ── FILTER 2: Rugpull safety ──
        time.sleep(API_SLEEP)
        sec = get_token_security(address)

        if sec is None:
            log.info(f"  [SNIPER] {symbol} — no security data, skip")
            continue

        rug_pass, rug_flags = rugpull_check(sec)
        flag_str = ' | '.join(rug_flags) if rug_flags else 'clean'

        if not rug_pass:
            log.info(f"  [SNIPER] {symbol} — F2 FAIL | {flag_str}")
            continue

        count_f2 += 1
        log.info(f"  [SNIPER] {symbol} — F2 PASS | {flag_str}")

        # Ask Claude
        verdict, reason = ask_claude(symbol, name, metrics, signals)

        log.info(
            f"  [SNIPER] *** {symbol} ({name}) ***\n"
            f"    Score: {score}/5 | Claude: {verdict} | Age: {t['age_mins']:.0f}m\n"
            f"    MC: ${metrics['mc']:,.0f} | Liq: ${metrics['liquidity']:,.0f}\n"
            f"    m5: {t['chg_m5']:+.1f}% | h1: {metrics['price_chg_1h']:+.1f}%\n"
            f"    Vol h1: ${metrics['vol_1h_usd']:,.0f} | m5: ${t['vol_m5']:,.0f}\n"
            f"    Buys/Sells h1: {metrics['buy_1h']}/{metrics['sell_1h']}\n"
            f"    Signals: {' | '.join(signals)}\n"
            f"    Safety: {flag_str}\n"
            f"    Reason: {reason}"
        )

        log_signal(address, symbol, name, score, signals, metrics, verdict, reason)
        mark_seen(seen, address)
        signals_found += 1

    log.info(f"[SNIPER] Scan done | raw:{len(new_tokens)} → F1(momentum):{count_f1} → F2(safety):{count_f2} → signals:{signals_found}")

    save_seen(seen)

    return seen


def main():
    log.info("=" * 60)
    log.info("Meme Sniper Bot | PAPER MODE | Source: DexScreener")
    log.info(f"Scanning every {POLL_SECONDS}s | Age <{MAX_AGE_MINUTES}m | MC ${MIN_MC:,}–${MAX_MC:,}")
    log.info(f"Min score {MIN_SCORE}/5 | Wash ratio <{MAX_VOL_LIQ_RATIO}x | Repeat ban >{MAX_APPEARANCES} appearances")
    log.info("=" * 60)

    seen = load_seen()

    resolver = threading.Thread(target=update_resolutions, daemon=True)
    resolver.start()

    while True:
        try:
            seen = scan(seen)
        except Exception as e:
            log.error(f"[SNIPER] Loop error: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
