"""
scanner_bot.py
==============
Polymarket Wallet Scanner Bot.

WHAT IT DOES:
  - Mode 1 (Leaderboard): Scrapes top wallets by P&L from Polymarket leaderboard
  - Mode 2 (Parameter): Pulls wallets from active markets in each category,
    scores against parameters, surfaces hidden gems regardless of rank
  - Flags INSIDER_SIGNAL wallets: < 3 months old, > $500 avg bet, > 65% win rate, > 20 trades
  - Outputs scored wallet lists to wallets/slow/ and wallets/fast/ subfolders
  - Runs daily or weekly — refreshes and removes stale wallets

OUTPUT STRUCTURE:
  wallets/
  ├── slow/
  │   ├── weather.json
  │   ├── sports.json
  │   ├── politics.json
  │   ├── economics.json
  │   ├── esports.json
  │   ├── culture.json
  │   └── tech.json
  └── fast/
      ├── 15min.json
      ├── 1h.json
      └── 4h.json

SETUP:
  pip install requests

USAGE:
  python scanner_bot.py             # run full scan
  python scanner_bot.py --mode 1    # leaderboard only
  python scanner_bot.py --mode 2    # parameter scan only
  python scanner_bot.py --review    # print current wallet lists for manual review
"""

import os
import sys
import json
import time
import logging
import warnings
import datetime
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Force UTF-8 output on Windows (prevents UnicodeEncodeError in terminal)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR      = "D:/Desktop/Trading Folder"
WALLETS_DIR   = f"{BASE_DIR}/wallets"
LOG_FILE      = f"{BASE_DIR}/scanner_bot.log"

# Slow wallet filters (weather, sports, politics, economics, culture, tech, esports)
SLOW_FILTERS = {
    "min_trade_count":          100,
    "min_win_rate":             0.55,
    "min_pnl_vol_ratio":        0.05,   # P&L/Volume > 5%
    "min_category_consistency": 0.70,
    "max_days_since_active":    30,
    "min_pnl":                  500,
}

# Fast wallet filters (15min, 1h, 4h crypto markets — high-freq bots)
FAST_FILTERS = {
    "min_trade_count":          300,    # high-freq bots should have many trades
    "min_win_rate":             0.52,   # lower — volume compensates
    "min_pnl_vol_ratio":        0.02,   # tight margins are normal
    "min_category_consistency": 0.30,   # trade across crypto timeframes
    "max_days_since_active":    7,      # must be very recently active
    "min_pnl":                  1000,
}

# Insider signal thresholds — active trading behaviour only, no age filter
INSIDER_MIN_AVG_BET   = 500   # > $500 average bet size
INSIDER_MIN_WIN_RATE  = 0.65  # > 65% win rate
INSIDER_MIN_TRADES    = 20    # > 20 trades (not a fluke)

# API endpoints
GAMMA_API   = "https://gamma-api.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"

# ─────────────────────────────────────────────────────────────────────────────
# SEED WALLETS — known wallets from research (Mode 1 bootstrap)
# Category → Polymarket tag mapping
SLOW_CATEGORIES = {
    "weather":   ["weather"],
    "sports":    ["sports", "soccer", "nfl", "nba", "tennis", "golf", "f1", "ufc", "cricket", "nhl", "esports"],
    "politics":  ["politics", "elections", "geopolitics"],
    "economics": ["economics", "crypto", "finance", "commodities"],
    "culture":   ["culture", "entertainment", "awards", "music", "tv"],
    "tech":      ["tech", "ai", "science"],
    "esports":   ["esports", "gaming"],
}

FAST_CATEGORIES = {
    "15min": ["crypto"],
    "1h":    ["crypto"],
    "4h":    ["crypto"],
}

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("scanner_bot")

# ─────────────────────────────────────────────────────────────────────────────
# API HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get(url, params=None, retries=3):
    """GET request with retries."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                logger.warning(f"Request failed: {url} | {e}")
                return None


def fetch_leaderboard(period="monthly", limit=100):
    """
    Returns seed wallets as leaderboard entries.
    Polymarket leaderboard is not publicly accessible via API —
    we use our researched seed list instead and discover more via Mode 2.
    """
    logger.info(f"Using {len(SEED_WALLETS)} seed wallets as leaderboard (Polymarket API not public)")
    return [{"address": addr} for addr in SEED_WALLETS]


def fetch_wallet_profile(address: str) -> dict:
    """
    Fetch profile stats for a single wallet using Data API activity endpoint.
    Builds a profile by aggregating activity data.
    """
    # Get recent activity to build profile stats
    data = _get(f"{DATA_API}/activity", params={"user": address, "limit": 500})
    if not data or not isinstance(data, list):
        return {}

    trades = [d for d in data if d.get("type") == "TRADE"]
    if not trades:
        return {}

    # Build profile from activity
    total_volume = sum(float(t.get("usdcSize", 0)) for t in trades)
    name = trades[0].get("name", address[:8]) if trades else address[:8]

    # Estimate account age from earliest trade timestamp
    earliest = None
    for t in trades:
        ts = t.get("timestamp") or t.get("createdAt")
        dt = parse_date(ts)
        if dt and (earliest is None or dt < earliest):
            earliest = dt
    created_at = earliest.strftime("%Y-%m-%dT%H:%M:%SZ") if earliest else ""

    return {
        "address":     address,
        "name":        name,
        "volume":      total_volume,
        "tradesCount": len(trades),
        "trades_raw":  trades,  # pass through for scoring
        "createdAt":   created_at,
    }


def fetch_wallet_activity(address: str, max_records=2000) -> tuple:
    """
    Fetch paginated activity for a wallet.
    Returns (trades, redeems) separately — both needed for accurate P&L.
    Redemptions = winning positions resolving → counted as income.
    """
    all_trades  = []
    all_redeems = []
    offset = 0
    page_size = 500
    while len(all_trades) + len(all_redeems) < max_records:
        data = _get(f"{DATA_API}/activity", params={
            "user": address, "limit": page_size, "offset": offset
        })
        if not data or not isinstance(data, list):
            break
        all_trades.extend([d for d in data if d.get("type") == "TRADE"])
        all_redeems.extend([d for d in data if d.get("type") == "REDEEM"])
        if len(data) < page_size:
            break
        offset += page_size
    return all_trades, all_redeems


def fetch_wallet_trades(address: str) -> list:
    """Compatibility wrapper — returns just trades."""
    trades, _ = fetch_wallet_activity(address)
    return trades


def fetch_wallet_positions(address: str) -> list:
    """Fetch current open positions for a wallet."""
    data = _get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": "0.1"})
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("data", data.get("results", []))


def fetch_active_markets_by_tag(tag: str, limit=100) -> list:
    """Fetch active markets for a given tag — used in Mode 2 to find wallets."""
    data = _get(f"{GAMMA_API}/events", params={"tag_slug": tag, "active": "true", "limit": limit})
    if not data:
        return []
    if isinstance(data, list):
        return data
    return data.get("data", [])


def _parse_clob_ids(raw) -> list:
    """Parse clobTokenIds whether it comes as a list or JSON string."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        import json as _json
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    return []


def fetch_token_traders(token_id: str, limit=100) -> list:
    """Fetch wallets that traded a specific clobTokenId via data-api."""
    data = _get(f"{DATA_API}/trades", params={"asset_id": token_id, "limit": limit})
    if not data:
        return []
    if isinstance(data, dict):
        data = data.get("data", data.get("results", []))
    if not isinstance(data, list) or not data:
        return []

    seen = set()
    users = []
    for trade in data:
        addr = trade.get("proxyWallet", "")
        if addr and isinstance(addr, str) and addr.startswith("0x") and addr not in seen:
            seen.add(addr)
            users.append({"user": addr})
    return users


def fetch_market_holders(condition_id: str, clob_token_ids=None, limit=50) -> list:
    """
    Fetch wallets that have traded in a market using clobTokenIds.
    Samples 2 YES tokens (every other entry) to keep API calls low.
    """
    ids = _parse_clob_ids(clob_token_ids) if clob_token_ids else []
    if not ids:
        return []

    # YES tokens are typically at even indices; sample max 6
    sample = ids[::2][:6]
    seen = set()
    users = []
    for token_id in sample:
        if not token_id or not isinstance(token_id, str):
            continue
        for u in fetch_token_traders(str(token_id), limit=limit):
            addr = u.get("user", "")
            if addr and addr not in seen:
                seen.add(addr)
                users.append(u)
    return users

# ─────────────────────────────────────────────────────────────────────────────
# WALLET SCORING
# ─────────────────────────────────────────────────────────────────────────────

def parse_date(date_str):
    """Parse various date formats to datetime. Handles strings and Unix timestamps."""
    if not date_str:
        return None
    # Unix timestamp (int or float)
    if isinstance(date_str, (int, float)):
        try:
            return datetime.datetime.utcfromtimestamp(date_str)
        except Exception:
            return None
    # String formats
    for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]:
        try:
            return datetime.datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    # Try as numeric string
    try:
        return datetime.datetime.utcfromtimestamp(float(date_str))
    except Exception:
        return None


def account_age_days(created_at: str) -> int:
    """Return account age in days. Returns 9999 if unknown."""
    dt = parse_date(created_at)
    if not dt:
        return 9999
    return (datetime.datetime.utcnow() - dt).days


def detect_category(trades: list) -> tuple[str, float]:
    """
    Detect primary market category from trade history.
    Returns (category, consistency_ratio).
    """
    if not trades:
        return "unknown", 0.0

    category_counts = {}
    for trade in trades:
        # Activity endpoint gives title and slug directly
        tags = []
        title = (trade.get("title", "") or trade.get("slug", "") or "").lower()

        cat = "other"
        if any(t in ["weather"] for t in tags) or "temperature" in title or "weather" in title:
            cat = "weather"
        elif any(t in ["sports", "soccer", "nfl", "nba", "tennis", "golf", "f1", "ufc", "cricket", "nhl"] for t in tags) or \
             any(w in title for w in ["vs", "match", "game", "win", "championship", "league", "cup", "tournament"]):
            cat = "sports"
        elif any(t in ["esports", "gaming"] for t in tags) or any(w in title for w in ["esport", "gaming", "dota", "csgo", "valorant"]):
            cat = "esports"
        elif any(t in ["politics", "elections"] for t in tags) or any(w in title for w in ["president", "election", "vote", "congress", "senate", "trump", "biden"]):
            cat = "politics"
        elif any(t in ["economics", "finance", "commodities"] for t in tags) or any(w in title for w in ["rate", "inflation", "gdp", "fed", "oil", "gold", "price"]):
            cat = "economics"
        elif any(t in ["crypto"] for t in tags) or any(w in title for w in ["btc", "eth", "sol", "bitcoin", "ethereum", "crypto", "above", "below", "up", "down"]):
            # Detect timeframe for fast category
            if "5 min" in title or "5min" in title or "15 min" in title or "15min" in title:
                cat = "15min"
            elif "1 hour" in title or "1h" in title or "1-hour" in title:
                cat = "1h"
            elif "4 hour" in title or "4h" in title or "4-hour" in title:
                cat = "4h"
            else:
                cat = "economics"
        elif any(t in ["culture", "entertainment"] for t in tags) or any(w in title for w in ["award", "oscar", "grammy", "album", "movie", "show"]):
            cat = "culture"
        elif any(t in ["tech", "ai", "science"] for t in tags) or any(w in title for w in ["ai", "gpt", "apple", "google", "release", "launch"]):
            cat = "tech"

        category_counts[cat] = category_counts.get(cat, 0) + 1

    if not category_counts:
        return "unknown", 0.0

    top_cat = max(category_counts, key=category_counts.get)
    consistency = category_counts[top_cat] / len(trades)
    return top_cat, consistency


def calculate_win_rate(trades: list) -> float:
    """
    Estimate win rate from trades.
    Sells at price > 0.9 = likely winner. Sells at price < 0.1 = likely loser.
    """
    if not trades:
        return 0.0
    sells = [t for t in trades if t.get("side") == "SELL"]
    if not sells:
        return 0.0
    wins = sum(1 for t in sells if float(t.get("price", 0.5) or 0.5) > 0.9)
    losses = sum(1 for t in sells if float(t.get("price", 0.5) or 0.5) < 0.1)
    decided = wins + losses
    return wins / decided if decided > 0 else 0.5


def score_wallet(address: str, profile: dict, trades: list, redeems: list = None, positions: list = None, source_category: str = None) -> dict | None:
    """
    Score a wallet against all parameters.
    Returns scored wallet dict or None if it doesn't qualify.
    """
    if not profile:
        return None

    # Extract stats from profile
    username    = profile.get("name", address[:8])
    created     = profile.get("createdAt", "")
    trade_count = len(trades)

    # P&L via net flow:
    # spent = total USDC on BUY trades
    # received = SELL trades + REDEEM transactions (winning positions resolving)
    redeems = redeems or []
    spent    = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "BUY")
    received = sum(float(t.get("usdcSize", 0) or 0) for t in trades if t.get("side") == "SELL")
    received += sum(float(r.get("usdcSize", 0) or 0) for r in redeems)
    pnl      = received - spent
    volume   = spent

    # Win rate — use positions (more accurate than sell-price heuristic)
    if positions is None:
        positions = fetch_wallet_positions(address)
    win_rate = 0.0
    if positions:
        closed = [p for p in positions if float(p.get("curPrice", 1) or 1) in (0.0, 1.0)]
        if closed:
            wins = sum(1 for p in closed if float(p.get("cashPnl", 0) or 0) > 0)
            win_rate = wins / len(closed)
        if volume == 0:
            volume = sum(float(p.get("initialValue", 0) or 0) for p in positions)
    if win_rate == 0.0:
        win_rate = calculate_win_rate(trades)  # fallback

    pnl = float(pnl)

    # Skip wallets with no meaningful activity
    if pnl <= 0 or volume <= 0:
        return None

    # Calculate scores
    pnl_vol_ratio = pnl / volume if volume > 0 else 0
    age_days      = account_age_days(created)
    avg_bet       = volume / trade_count if trade_count > 0 else 0
    # win_rate already calculated from positions above — don't overwrite it

    # Detect category — use source_category if wallet was found via Mode 2 market scan
    if source_category:
        primary_category, cat_consistency = source_category, 1.0
    else:
        primary_category, cat_consistency = detect_category(trades)

    # Last active
    last_trade_date = None
    if trades:
        timestamps = []
        for t in trades:
            ts = t.get("timestamp") or t.get("createdAt")
            if isinstance(ts, (int, float)):
                timestamps.append(datetime.datetime.utcfromtimestamp(ts))
            elif isinstance(ts, str) and ts:
                parsed = parse_date(ts)
                if parsed:
                    timestamps.append(parsed)
        if timestamps:
            last_trade_date = max(timestamps)

    days_since_active = (datetime.datetime.utcnow() - last_trade_date).days if last_trade_date else 9999

    # ── Select filter set based on wallet type ──
    fast_cats = {"15min", "1h", "4h"}
    filters = FAST_FILTERS if primary_category in fast_cats else SLOW_FILTERS

    # ── Standard qualification ──
    effective_consistency = cat_consistency if trade_count < 500 else filters["min_category_consistency"]
    standard_qualify = (
        pnl_vol_ratio >= filters["min_pnl_vol_ratio"] and
        win_rate >= filters["min_win_rate"] and
        trade_count >= filters["min_trade_count"] and
        effective_consistency >= filters["min_category_consistency"] and
        days_since_active <= filters["max_days_since_active"] and
        pnl >= filters["min_pnl"]
    )

    # ── Relaxed: good wallets where P&L calc may be imprecise ──
    relaxed_qualify = (
        trade_count >= filters["min_trade_count"] // 2 and
        days_since_active <= filters["max_days_since_active"] and
        win_rate >= filters["min_win_rate"] and
        pnl >= filters["min_pnl"]
    )

    if not standard_qualify and not relaxed_qualify:
        return None

    # ── Insider signal: high avg bet + high win rate + sufficient trades ──
    insider_signal = (
        avg_bet >= INSIDER_MIN_AVG_BET and
        win_rate >= INSIDER_MIN_WIN_RATE and
        trade_count >= INSIDER_MIN_TRADES
    )

    # ── Build score (0-100) ──
    score = 0
    score += min(30, pnl_vol_ratio * 200)          # up to 30 pts for efficiency
    score += min(30, win_rate * 40)                 # up to 30 pts for win rate
    score += min(25, min(trade_count, 1000) / 40)   # up to 25 pts for trade count
    score += min(15, cat_consistency * 15)           # up to 15 pts for consistency
    score += 10 if insider_signal else 0             # 10 pt bonus for insider signal

    return {
        "address":           address,
        "username":          username,
        "pnl":               round(pnl, 2),
        "volume":            round(volume, 2),
        "pnl_vol_ratio":     round(pnl_vol_ratio * 100, 2),  # as %
        "win_rate":          round(win_rate * 100, 2),         # as %
        "trade_count":       trade_count,
        "avg_bet":           round(avg_bet, 2),
        "account_age_days":  age_days,
        "days_since_active": days_since_active,
        "primary_category":  primary_category,
        "cat_consistency":   round(cat_consistency * 100, 2),  # as %
        "score":             round(score, 1),
        "insider_signal":    insider_signal,
        "last_scanned":      datetime.datetime.utcnow().isoformat(),
    }

# ─────────────────────────────────────────────────────────────────────────────
# MODE 1 — LEADERBOARD SCAN
# ─────────────────────────────────────────────────────────────────────────────

def run_leaderboard_scan() -> list[dict]:
    """
    Mode 1: Fetch top wallets from leaderboard, score each one.
    Returns list of qualified scored wallets.
    """
    logger.info("=" * 60)
    logger.info("MODE 1 — Leaderboard Scan")
    logger.info("=" * 60)

    all_wallets = {}

    for period in LEADERBOARD_PERIODS:
        entries = fetch_leaderboard(period=period, limit=100)
        logger.info(f"  {period}: {len(entries)} entries fetched")

        for entry in entries:
            address = entry.get("address", entry.get("wallet", ""))
            if not address or address in all_wallets:
                continue
            all_wallets[address] = entry

    logger.info(f"Total unique leaderboard wallets: {len(all_wallets)}")

    # Score each wallet
    qualified = []
    for i, (address, entry) in enumerate(all_wallets.items(), 1):
        logger.info(f"  Scoring [{i}/{len(all_wallets)}] {address[:10]}...")

        profile          = fetch_wallet_profile(address) or entry
        trades, redeems  = fetch_wallet_activity(address)
        positions        = fetch_wallet_positions(address)
        time.sleep(0.3)  # rate limit

        scored = score_wallet(address, profile, trades, redeems, positions)
        if scored:
            qualified.append(scored)
            flag = " [INSIDER]" if scored["insider_signal"] else ""
            logger.info(
                f"    [QUALIFIED] {scored['username']} | "
                f"score={scored['score']} | P&L=${scored['pnl']:,.0f} | "
                f"efficiency={scored['pnl_vol_ratio']}% | "
                f"win={scored['win_rate']}% | cat={scored['primary_category']}{flag}"
            )
        else:
            logger.debug(f"    ✗ Did not qualify")

    logger.info(f"Mode 1 complete: {len(qualified)} wallets qualified")
    return qualified

# ─────────────────────────────────────────────────────────────────────────────
# MODE 2 — PARAMETER SCAN (hidden gems)
# ─────────────────────────────────────────────────────────────────────────────

def scan_category_for_wallets(category: str, tags: list) -> list[str]:
    """
    Pull unique wallet addresses from active markets in a category.
    """
    addresses = set()

    for tag in tags:
        events = fetch_active_markets_by_tag(tag, limit=50)
        logger.info(f"  [{category}/{tag}] gamma API returned {len(events)} events")

        # Log first event structure for debugging
        if events:
            first = events[0]
            sub = first.get("markets", [first])
            if sub:
                m = sub[0]
                cid = m.get("conditionId") or m.get("condition_id") or m.get("id") or "NOT FOUND"
                clobs = m.get("clobTokenIds", m.get("clob_token_ids", []))
                logger.info(f"  [DEBUG] first market conditionId={cid[:20] if cid else 'none'}... clobTokenIds count={len(clobs)}")

        for event in events:
            sub_markets = event.get("markets", [event])
            for market in sub_markets:
                clob_ids = market.get("clobTokenIds", market.get("clob_token_ids", []))
                condition_id = (
                    market.get("conditionId") or
                    market.get("condition_id") or
                    market.get("id") or ""
                )
                if not clob_ids and not condition_id:
                    continue
                holders = fetch_market_holders(condition_id, clob_token_ids=clob_ids, limit=50)
                for h in holders:
                    addr = h.get("user", h.get("address", ""))
                    if addr:
                        addresses.add(addr)

        time.sleep(0.5)

    logger.info(f"  [{category}] Found {len(addresses)} unique wallet addresses")
    return list(addresses)


def run_parameter_scan() -> list[dict]:
    """
    Mode 2: Pull wallets from active markets per category, score against parameters.
    Returns list of qualified scored wallets (hidden gems).
    """
    logger.info("=" * 60)
    logger.info("MODE 2 — Parameter Scan (Hidden Gems)")
    logger.info("=" * 60)

    all_categories = {**SLOW_CATEGORIES, **FAST_CATEGORIES}
    qualified = []
    seen = set()

    for category, tags in all_categories.items():
        logger.info(f"Scanning category: {category} (tags: {tags})")
        addresses = scan_category_for_wallets(category, tags)

        for i, address in enumerate(addresses, 1):
            if address in seen:
                continue
            seen.add(address)

            logger.debug(f"  [{category}] Scoring {address[:10]}... ({i}/{len(addresses)})")
            profile          = fetch_wallet_profile(address)
            trades, redeems  = fetch_wallet_activity(address)
            positions        = fetch_wallet_positions(address)
            time.sleep(0.3)

            scored = score_wallet(address, profile, trades, redeems, positions, source_category=category)
            if scored:
                qualified.append(scored)
                flag = " [INSIDER]" if scored["insider_signal"] else ""
                logger.info(
                    f"  ✓ HIDDEN GEM | {scored['username']} | "
                    f"score={scored['score']} | P&L=${scored['pnl']:,.0f} | "
                    f"efficiency={scored['pnl_vol_ratio']}% | "
                    f"cat={scored['primary_category']}{flag}"
                )

    logger.info(f"Mode 2 complete: {len(qualified)} hidden gems qualified")
    return qualified

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORISE AND SAVE
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_TO_FOLDER = {
    # Slow
    "weather":   "slow/weather",
    "sports":    "slow/sports",
    "esports":   "slow/esports",
    "politics":  "slow/politics",
    "economics": "slow/economics",
    "culture":   "slow/culture",
    "tech":      "slow/tech",
    # Fast
    "15min":     "fast/15min",
    "1h":        "fast/1h",
    "4h":        "fast/4h",
    # Default
    "other":     "slow/culture",
    "unknown":   "slow/culture",
}


def save_wallets(wallets: list[dict]):
    """
    Sort wallets into category subfolders and save as JSON.
    Insiders go to top of each list.
    Merges with existing wallet files (keeps wallets not seen this scan).
    """
    # Group by category
    by_category = {}
    for w in wallets:
        cat = w.get("primary_category", "unknown")
        folder = CATEGORY_TO_FOLDER.get(cat, "slow/culture")
        if folder not in by_category:
            by_category[folder] = []
        by_category[folder].append(w)

    for folder, wallet_list in by_category.items():
        path = f"{WALLETS_DIR}/{folder}/wallets.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if "manual" in folder:
            continue  # never overwrite hand-curated wallets

        # Full reset each scan — only wallets from THIS run survive
        merged = sorted(
            wallet_list,
            key=lambda x: (not x.get("insider_signal", False), -x.get("score", 0))
        )

        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)

        insider_count = sum(1 for w in merged if w.get("insider_signal"))
        logger.info(f"  Saved {len(merged)} wallets -> {path} ({insider_count} insiders)")


def print_wallet_review():
    """Print all wallet lists for manual review."""
    print("\n" + "=" * 70)
    print("WALLET REVIEW - Current Lists")
    print("=" * 70)

    for root, dirs, files in os.walk(WALLETS_DIR):
        for fname in files:
            if fname != "wallets.json":
                continue
            path = os.path.join(root, fname)
            folder = path.replace(WALLETS_DIR + "/", "").replace("\\", "/").replace("/wallets.json", "")

            try:
                with open(path, "r", encoding="utf-8") as f:
                    wallets = json.load(f)
            except Exception:
                continue

            print(f"\n-- {folder.upper()} ({len(wallets)} wallets) --")
            print(f"  {'Username':<25} {'Score':>5} {'P&L':>10} {'Eff%':>6} {'Win%':>6} {'Trades':>7} {'Age(d)':>7} {'Flag'}")
            print(f"  {'-'*25} {'-'*5} {'-'*10} {'-'*6} {'-'*6} {'-'*7} {'-'*7} {'-'*10}")

            for w in wallets[:20]:  # show top 20
                flag = "INSIDER" if w.get("insider_signal") else ""
                print(
                    f"  {w.get('username','?'):<25} "
                    f"{w.get('score',0):>5.1f} "
                    f"${w.get('pnl',0):>9,.0f} "
                    f"{w.get('pnl_vol_ratio',0):>5.1f}% "
                    f"{w.get('win_rate',0):>5.1f}% "
                    f"{w.get('trade_count',0):>7} "
                    f"{w.get('account_age_days',0):>7} "
                    f"{flag}"
                )

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Wallet Scanner")
    parser.add_argument("--review", action="store_true", help="Print current wallet lists and exit")
    args = parser.parse_args()

    if args.review:
        print_wallet_review()
        return

    logger.info("=" * 60)
    logger.info("scanner_bot.py — Polymarket Wallet Scanner")
    logger.info(f"Run started: {datetime.datetime.now().isoformat()}")
    logger.info("=" * 60)

    # Ensure manual wallets folder exists (never overwritten by scanner)
    manual_path = f"{WALLETS_DIR}/manual/wallets.json"
    if not os.path.exists(manual_path):
        os.makedirs(os.path.dirname(manual_path), exist_ok=True)
        with open(manual_path, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        logger.info(f"Created manual wallets file: {manual_path} — add your hand-picked wallets here")

    all_qualified = run_parameter_scan()

    logger.info(f"\nTotal qualified wallets: {len(all_qualified)}")
    logger.info(f"  Insiders: {sum(1 for w in all_qualified if w.get('insider_signal'))}")
    logger.info(f"  New accounts: {sum(1 for w in all_qualified if w.get('new_account'))}")

    if all_qualified:
        save_wallets(all_qualified)
        logger.info("\nWallet lists updated. Run with --review to inspect.")
    else:
        logger.warning("No wallets qualified this scan. Check API endpoints or lower thresholds.")

    logger.info(f"\nScan complete: {datetime.datetime.now().isoformat()}")


SCAN_INTERVAL_HOURS = 24

if __name__ == "__main__":
    # Check for --review flag — single run, no loop
    if "--review" in sys.argv:
        main()
    else:
        logger.info("Scanner Bot | running every 24h | Ctrl+C to stop")
        while True:
            try:
                main()
            except Exception as e:
                logger.error(f"[SCANNER] Loop error: {e}")
            logger.info(f"[SCANNER] Next scan in {SCAN_INTERVAL_HOURS}h")
            time.sleep(SCAN_INTERVAL_HOURS * 3600)
