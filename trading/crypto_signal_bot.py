import sys
"""
crypto_signal_bot.py
====================
Two loops, both using StretchScore — the highest WR strategy from the factory:
  1hr  — StretchScore (70.4% WR on ETH)
  4hr  — StretchScore (77.8% WR on ETH)

IMPROVEMENTS:
  1. Time-of-day filter  — skip signals during high-volatility hours (US open, Asia open)
  2. Volatility filter   — skip if ATR has spiked (news event in progress)
  3. BTC lead indicator  — BTC signal must agree before betting altcoins
  4. Loss streak protect — pause after 3 consecutive losses, resume next candle

PAPER MODE: No real bets placed. Logs to crypto_signals_paper.csv + tracks results.
LIVE MODE:  Set PAPER_MODE = False.

RUN:
    python crypto_signal_bot.py
"""

import time
import threading
import logging
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone
import os
import requests
import json

# Fix Unicode output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

PAPER_MODE = True       # flip to False for live betting
BET_SIZE   = 1         # shares per bet — cost = 1 × price (e.g. $0.52 at 0.52 odds = minimum possible)

ASSETS_15M = ["BNB/USDT", "BTC/USDT", "SOL/USDT", "ETH/USDT", "XRP/USDT", "DOGE/USDT"]
ASSETS_1H  = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT"]  # BNB cut: 39% WR on 1h
ASSETS_4H  = ["BTC/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT"]  # SOL(40%WR) + ETH(43%WR) cut from 4h
ASSETS_1D  = ["BNB/USDT", "XRP/USDT", "DOGE/USDT"]  # Donchian 1d winners

# ── Improvement 1: Time-of-day filter ─────────────────────────
# UTC hours to SKIP — high volatility, mean reversion fails
# US market open: 13:30-14:30 UTC | Asia open: 00:00-01:00 UTC
SKIP_HOURS_UTC = set()   # no time filter — bot only runs 9am-9pm Ireland time

# ── Improvement 2: Volatility filter ──────────────────────────
# If current ATR > ATR_MULT × average ATR → spike in progress → skip
ATR_SPIKE_MULT = 2.0    # current ATR must be < 2x average ATR to trade

# ── Improvement 3: BTC lead indicator ─────────────────────────
# For ETH/SOL/BNB signals — BTC must show the same direction (or no signal)
# If BTC disagrees, skip the altcoin signal
USE_BTC_LEAD = True

# Improvement 4: Loss streak protection — removed (cross-timeframe interference)

OUTPUT_DIR = "D:/Desktop/Trading Folder"
PAPER_CSV  = f"{OUTPUT_DIR}/crypto_signals_paper.csv"
LOG_FILE   = f"{OUTPUT_DIR}/crypto_signal_bot.log"

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

csv_lock = threading.Lock()

exchange            = ccxt.binance({'enableRateLimit': True})
exchange_bybit_perp = ccxt.bybit({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

# Assets that aren't listed on Binance spot — use fallback exchange
EXCHANGE_OVERRIDE = {
    "HYPE/USDT": exchange_bybit_perp,   # HYPE only exists as a perp on Bybit
}


# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

def fetch_candles(symbol: str, timeframe: str, limit: int = 300, retries: int = 3) -> pd.DataFrame | None:
    ex = EXCHANGE_OVERRIDE.get(symbol, exchange)
    for attempt in range(1, retries + 1):
        try:
            ohlcv = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < 50:
                return None
            df = pd.DataFrame(ohlcv, columns=['ts','Open','High','Low','Close','Volume'])
            df['ts'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.set_index('ts').astype(float)
            return df.iloc[:-1]   # drop still-forming candle
        except Exception as e:
            if attempt < retries:
                log.debug(f"Fetch retry {attempt}/{retries} {symbol} {timeframe}: {e}")
                time.sleep(2 ** attempt)   # 2s, 4s backoff
            else:
                log.warning(f"Fetch failed {symbol} {timeframe} after {retries} attempts: {e}")
    return None


# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────

def ema(series, n):
    return pd.Series(series).ewm(span=n, adjust=False).mean().values

def sma(series, n):
    return pd.Series(series).rolling(n).mean().values

def bollinger(series, n=20, k=2):
    s = pd.Series(series)
    mid = s.rolling(n).mean()
    std = s.rolling(n).std()
    return mid.values, (mid + k*std).values, (mid - k*std).values

def rsi(series, n=14):
    s = pd.Series(series)
    d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    rs = g / l.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).values

def atr(high, low, close, n=14):
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean().values

def adx(high, low, close, n=14):
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr  = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    pdm = (h - h.shift()).clip(lower=0).where((h - h.shift()) > (l.shift() - l), 0)
    ndm = (l.shift() - l).clip(lower=0).where((l.shift() - l) > (h - h.shift()), 0)
    atr14 = tr.rolling(n).mean()
    pdi   = 100 * pdm.rolling(n).mean() / atr14.replace(0, np.nan)
    ndi   = 100 * ndm.rolling(n).mean() / atr14.replace(0, np.nan)
    dx    = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.rolling(n).mean().values

def macd_calc(series, fast=12, slow=26, sig=9):
    s = pd.Series(series)
    ml = s.ewm(span=fast).mean() - s.ewm(span=slow).mean()
    return ml.values, ml.ewm(span=sig).mean().values

def stochastic(high, low, close, k_n=14, d_n=3):
    h = pd.Series(high).rolling(k_n).max()
    l = pd.Series(low).rolling(k_n).min()
    k = 100 * (pd.Series(close) - l) / (h - l + 1e-10)
    return k.values, k.rolling(d_n).mean().values

def supertrend(high, low, close, n=10, mult=3):
    atr_v = atr(high, low, close, n)
    h, l, c = np.array(high), np.array(low), np.array(close)
    hl2 = (h + l) / 2
    upper = hl2 + mult * atr_v
    lower = hl2 - mult * atr_v
    direction = np.zeros(len(c)); direction[0] = 1
    for i in range(1, len(c)):
        if np.isnan(atr_v[i]): direction[i] = direction[i-1]
        elif c[i] > upper[i-1]: direction[i] = 1
        elif c[i] < lower[i-1]: direction[i] = -1
        else: direction[i] = direction[i-1]
    return direction

def parabolic_sar(high, low, af_start=0.02, af_step=0.02, af_max=0.2):
    h, l = np.array(high), np.array(low)
    n = len(h)
    sar = np.full(n, np.nan); trend = np.ones(n); ep = np.zeros(n); af = np.zeros(n)
    sar[0]=l[0]; ep[0]=h[0]; af[0]=af_start; trend[0]=1
    for i in range(1, n):
        ps,pe,pa,pt = sar[i-1],ep[i-1],af[i-1],trend[i-1]
        ns = ps + pa*(pe-ps)
        if pt == 1:
            ns = min(ns, l[i-1], l[max(0,i-2)])
            if l[i] < ns: trend[i]=-1; sar[i]=pe; ep[i]=l[i]; af[i]=af_start
            else:
                trend[i]=1; sar[i]=ns
                ep[i]=h[i] if h[i]>pe else pe
                af[i]=min(pa+af_step,af_max) if h[i]>pe else pa
        else:
            ns = max(ns, h[i-1], h[max(0,i-2)])
            if h[i] > ns: trend[i]=1; sar[i]=pe; ep[i]=h[i]; af[i]=af_start
            else:
                trend[i]=-1; sar[i]=ns
                ep[i]=l[i] if l[i]<pe else pe
                af[i]=min(pa+af_step,af_max) if l[i]<pe else pa
    return sar, trend


# ─────────────────────────────────────────────
# IMPROVEMENT 1: TIME-OF-DAY FILTER
# ─────────────────────────────────────────────

def is_safe_hour() -> bool:
    """Returns False during high-volatility hours (UTC)."""
    hour = datetime.now(timezone.utc).hour
    if hour in SKIP_HOURS_UTC:
        log.info(f"[FILTER] Time-of-day block — UTC hour {hour} is high-volatility, skipping")
        return False
    return True


# ─────────────────────────────────────────────
# IMPROVEMENT 2: VOLATILITY FILTER
# ─────────────────────────────────────────────

def is_volatility_normal(df: pd.DataFrame, n=14) -> bool:
    """Returns False if ATR has spiked above ATR_SPIKE_MULT × average."""
    if len(df) < n * 2:
        return True
    atr_vals = atr(df['High'].values, df['Low'].values, df['Close'].values, n)
    if np.isnan(atr_vals[-1]):
        return True
    current_atr = atr_vals[-1]
    avg_atr     = np.nanmean(atr_vals[-n*2:-1])
    if current_atr > avg_atr * ATR_SPIKE_MULT:
        log.info(f"[FILTER] Volatility spike — ATR {current_atr:.4f} > {ATR_SPIKE_MULT}x avg {avg_atr:.4f}, skipping")
        return False
    return True


# ─────────────────────────────────────────────
# IMPROVEMENT 3: BTC LEAD INDICATOR
# ─────────────────────────────────────────────

def get_adx_regime(asset: str, timeframe: str) -> tuple[float, str]:
    """
    Returns (adx_value, regime_label) for an asset.
    Uses 1h candles for 15m bets, 4h candles for 1h/4h/1d bets.
    Regime: TRENDING (>25), NEUTRAL (20-25), RANGING (<20).
    Soft gate only — for logging. Does not block bets.
    """
    check_tf = '1h' if timeframe == '15m' else '4h'
    df = fetch_candles(asset, check_tf, limit=60)
    if df is None or len(df) < 20:
        return (0.0, 'UNKNOWN')
    adx_vals = adx(df['High'].values, df['Low'].values, df['Close'].values, n=14)
    val = adx_vals[-1]
    if np.isnan(val):
        return (0.0, 'UNKNOWN')
    val = round(float(val), 1)
    if val > 25:
        label = 'TRENDING'
    elif val < 20:
        label = 'RANGING'
    else:
        label = 'NEUTRAL'
    return (val, label)


def get_btc_signal(timeframe: str, signal_fn) -> str | None:
    """
    Fetches BTC candles and runs the same signal function.
    Returns "UP", "DOWN", or None.
    """
    df_btc = fetch_candles("BTC/USDT", timeframe, limit=300)
    if df_btc is None:
        return None
    return signal_fn(df_btc)




# ─────────────────────────────────────────────
# STRATEGIES
# ─────────────────────────────────────────────

def signal_bollinger_reversion(df: pd.DataFrame) -> str | None:
    """15min — Bollinger Reversion + 200 EMA trend filter (65-67% WR)"""
    if len(df) < 210:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2)
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(e200[-1]):
        return None
    above_200 = close > e200[-1]
    if close < lower[-1] and above_200:
        return "UP"
    if close > upper[-1] and not above_200:
        return "DOWN"
    return None


def signal_meanreversion_bb_200ema(df: pd.DataFrame) -> str | None:
    """1hr — MeanReversion BB + 200 EMA (62-64% WR)"""
    if len(df) < 210:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2)
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(e200[-1]):
        return None
    if close < lower[-1] and close > e200[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_session_15m(df: pd.DataFrame) -> str | None:
    """
    15min — BB_Session_8_20_UTC (session-filtered Bollinger reversion)
    BTC 72.6% WR | BNB 70.8% WR | ETH 68.0% WR | SOL 67.0% WR
    Session filter replaces is_safe_hour — only trades 08:00-20:00 UTC.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_stretch_score_fast(df: pd.DataFrame) -> str | None:
    """
    1hr — StretchScore_Fast_EMA50 (no volume filter, EMA50 trend, RSI 30/70)
    BNB 78.6% WR | BTC 69.6% WR | ETH 66.7% WR | SOL 64.3% WR
    """
    if len(df) < 55:
        return None
    mid, up, dn = bollinger(df['Close'].values, 20, 2)
    rsi_v = rsi(df['Close'].values, 14)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(up[-1]) or np.isnan(e50[-1]):
        return None
    above = close > e50[-1]
    band_up = up[-1] - mid[-1]
    band_dn = mid[-1] - dn[-1]
    std_up = (close - mid[-1]) / (band_up + 1e-10)
    std_dn = (mid[-1] - close) / (band_dn + 1e-10)
    if std_dn >= 0.80 and rsi_v[-1] < 30 and above:
        return "UP"
    if std_up >= 0.80 and rsi_v[-1] > 70 and not above:
        return "DOWN"
    return None


def signal_stretch_score(df: pd.DataFrame) -> str | None:
    """
    4hr — StretchScore (custom, 77.8% WR on ETH, 61.8% on SOL)
    All 3 must agree: price stretched + volume fading + RSI exhausted
    200 EMA trend filter.
    """
    if len(df) < 210:
        return None
    mid, up, dn = bollinger(df['Close'].values, 20, 2)
    rsi_v   = rsi(df['Close'].values, 14)
    vol_ma  = sma(df['Volume'].values, 10)
    e200    = ema(df['Close'].values, 200)
    close   = df['Close'].iloc[-1]

    if np.isnan(up[-1]) or np.isnan(e200[-1]):
        return None

    above = close > e200[-1]

    std_dist_up = (close - mid[-1]) / (up[-1]  - mid[-1] + 1e-10)
    std_dist_dn = (mid[-1] - close) / (mid[-1] - dn[-1]  + 1e-10)
    stretched_up = std_dist_up >= 0.85
    stretched_dn = std_dist_dn >= 0.85

    vol_fading       = df['Volume'].iloc[-1] < vol_ma[-1]
    rsi_oversold     = rsi_v[-1] < 35
    rsi_overbought   = rsi_v[-1] > 65

    if stretched_dn and vol_fading and rsi_oversold and above:
        return "UP"
    if stretched_up and vol_fading and rsi_overbought and not above:
        return "DOWN"
    return None


def signal_adaptive_bb(df: pd.DataFrame) -> str | None:
    """
    1h/4h — AdaptiveBB_ATR_Reversion (best new strategy from factory round 2)
    Dynamic BB width: 2.5 std in high vol, 1.5 std in calm.
    BTC 1h 70.6% | ETH 1h 68.4% | SOL 1h 68.1% | BNB 1h 65.8% | BTC 4h 75.0%
    """
    if len(df) < 55:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]): return None
    k      = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper  = mid[-1] + k * std[-1]
    lower  = mid[-1] - k * std[-1]
    above  = close > e50[-1]
    if close < lower and above:
        return "UP"
    if close > upper and not above:
        return "DOWN"
    return None


def signal_adaptive_bb_long_only(df: pd.DataFrame) -> str | None:
    """
    1h — AdaptiveBB_LongOnly (longs only, no shorts)
    BTC 1h 79.0% (Test 76.7%) | ETH 1h 71.0% (Test 80.0%) | BNB 1h 69.8% (Test 81.8%)
    Crypto trends up long-term — only buy oversold bounces from adaptive bands.
    """
    if len(df) < 55:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]): return None
    k     = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    lower = mid[-1] - k * std[-1]
    above = close > e50[-1]
    if close < lower and above:
        return "UP"
    return None


def signal_adaptive_bb_session_8_20(df: pd.DataFrame) -> str | None:
    """
    1h — AdaptiveBB_Session_8_20 (8-20 UTC EU/US hours only)
    BTC 1h 72.2% (Test 76.2%) | ETH 1h 71.0% (Test 72.3%) | BNB 1h 70.4% (Test 74.3%)
    Same adaptive BB logic but cuts Asian-session noise.
    """
    if len(df) < 55:
        return None
    hour = df.index[-1].hour if hasattr(df.index[-1], 'hour') else pd.Timestamp(df.index[-1]).hour
    if not (8 <= hour < 20):
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]): return None
    k     = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper = mid[-1] + k * std[-1]
    lower = mid[-1] - k * std[-1]
    above = close > e50[-1]
    if close < lower and above:
        return "UP"
    if close > upper and not above:
        return "DOWN"
    return None


def signal_adaptive_bb_us_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h — AdaptiveBB_US_Session_1320 (13-20 UTC US hours only)
    SOL 15m 81.5% (Train 81.7% / Test 81.0%) | ETH 15m 78.3% (Test 76.2%)
    DOGE 15m 74.0% (Test 82.1%) | BNB 15m 71.6% (Test 75.0%)
    Best walk-forward consistency of any 15m strategy in the full run.
    """
    if len(df) < 55:
        return None
    hour = df.index[-1].hour if hasattr(df.index[-1], 'hour') else pd.Timestamp(df.index[-1]).hour
    if not (13 <= hour < 20):
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]): return None
    k      = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper  = mid[-1] + k * std[-1]
    lower  = mid[-1] - k * std[-1]
    above  = close > e50[-1]
    if close < lower and above:
        return "UP"
    if close > upper and not above:
        return "DOWN"
    return None


def signal_capitul_rsi40(df: pd.DataFrame) -> str | None:
    """
    1h — StretchScore_Capitul_RSI40 (relaxed RSI threshold: <40 buy, >60 sell)
    ETH 1h 75.9% (Train 71.7% / Test 91.7%) — highest test WR in entire file.
    More signals than strict <35 version while keeping strong edge.
    """
    if len(df) < 25:
        return None
    c      = df['Close'].values
    vol    = df['Volume'].values
    mid    = pd.Series(c).rolling(20).mean().values
    std    = pd.Series(c).rolling(20).std().values
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    rsi_v  = rsi(c, 14)
    vol_ma = pd.Series(vol).rolling(20).mean().values
    e50    = ema(c, 50)
    close  = c[-1]
    if np.isnan(upper[-1]) or np.isnan(e50[-1]) or np.isnan(rsi_v[-1]): return None
    above     = close > e50[-1]
    band_dn   = (mid[-1] - close) / (mid[-1] - lower[-1] + 1e-10)
    band_up   = (close - mid[-1]) / (upper[-1] - mid[-1] + 1e-10)
    vol_spike = vol[-1] > vol_ma[-1] * 1.5
    if band_dn >= 0.80 and vol_spike and rsi_v[-1] < 40 and above:
        return "UP"
    if band_up >= 0.80 and vol_spike and rsi_v[-1] > 60 and not above:
        return "DOWN"
    return None


def signal_stretch_capitulation(df: pd.DataFrame) -> str | None:
    """
    1h — StretchScore_Capitul_Session (Capitulation + 8-20 UTC session filter)
    BTC 1h 77.3% (Train 76.5% / Test 80.0%) | ETH 1h 76.7% (Test 85.7%)
    Session filter removes Asian-hour low-quality capitulation spikes.
    Upgraded from raw Capitulation — walk-forward holds up better.
    """
    if len(df) < 25:
        return None
    hour  = df.index[-1].hour if hasattr(df.index[-1], 'hour') else pd.Timestamp(df.index[-1]).hour
    if not (8 <= hour < 20):
        return None
    c      = df['Close'].values
    vol    = df['Volume'].values
    mid    = pd.Series(c).rolling(20).mean().values
    std    = pd.Series(c).rolling(20).std().values
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    rsi_v  = rsi(c, 14)
    vol_ma = pd.Series(vol).rolling(20).mean().values
    e50    = ema(c, 50)
    close  = c[-1]
    if np.isnan(upper[-1]) or np.isnan(e50[-1]) or np.isnan(rsi_v[-1]): return None
    above     = close > e50[-1]
    band_dn   = (mid[-1] - close) / (mid[-1] - lower[-1] + 1e-10)
    band_up   = (close - mid[-1]) / (upper[-1] - mid[-1] + 1e-10)
    vol_spike = vol[-1] > vol_ma[-1] * 1.5
    if band_dn >= 0.80 and vol_spike and rsi_v[-1] < 35 and above:
        return "UP"
    if band_up >= 0.80 and vol_spike and rsi_v[-1] > 65 and not above:
        return "DOWN"
    return None


def signal_vwap_zscore(df: pd.DataFrame) -> str | None:
    """
    1h — VWAP_ZScore_Reversion (massive signal count at 62-64% WR)
    XRP 1h 64.2% (865 trades) | ETH 1h 64.1% (875) | BTC 1h 62.2% (839)
    Price 1.5+ std below rolling VWAP = buy signal.
    """
    if len(df) < 25:
        return None
    c   = df['Close'].values
    v   = df['Volume'].values
    vwap = (pd.Series(c * v).rolling(20).sum() / (pd.Series(v).rolling(20).sum() + 1e-10)).values
    std  = pd.Series(c).rolling(20).std().values
    e200 = ema(c, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(vwap[-1]) or np.isnan(std[-1]) or np.isnan(e200[-1]): return None
    z     = (close - vwap[-1]) / (std[-1] + 1e-10)
    above = close > e200[-1]
    if z < -1.5 and above:
        return "UP"
    if z > 1.5 and not above:
        return "DOWN"
    return None


def signal_adaptive_bb_atr20(df: pd.DataFrame) -> str | None:
    """
    4h — AdaptiveBB_ATR20 (slower ATR period 20, smoother volatility)
    BTC 4h 71.9% (Test 76.5%) | BNB 4h 58.5% (Test 83.3%) | SOL 4h 59.6% (Test 63.6%)
    Less reactive than ATR14 — fewer false signals in choppy 4h markets.
    """
    if len(df) < 55:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 20)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]): return None
    k     = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper = mid[-1] + k * std[-1]
    lower = mid[-1] - k * std[-1]
    above = close > e50[-1]
    if close < lower and above:
        return "UP"
    if close > upper and not above:
        return "DOWN"
    return None


def signal_consensus_adaptive_vwap(df: pd.DataFrame) -> str | None:
    """
    4h — Consensus_Adaptive_AND_VWAP (AdaptiveBB + VWAP ZScore must both agree)
    BTC 4h 75.0% (Test 73.3%) | SOL 4h 63.3% (Test 72.7%) | DOGE 4h 63.5% (Test 63.6%)
    Two independent signals confirming same direction = higher conviction than either alone.
    """
    if len(df) < 55:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    v      = df['Volume'].values
    vwap   = (pd.Series(df['Close'].values * v).rolling(20).sum() / (pd.Series(v).rolling(20).sum() + 1e-10)).values
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]) or np.isnan(vwap[-1]): return None
    k     = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper = mid[-1] + k * std[-1]
    lower = mid[-1] - k * std[-1]
    z     = (close - vwap[-1]) / (std[-1] + 1e-10)
    above = close > e50[-1]
    if close < lower and z < -1.0 and above:
        return "UP"
    if close > upper and z > 1.0 and not above:
        return "DOWN"
    return None


def signal_bb_session_period15(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Session_Period15 (BB with n=15, 8-20 UTC)
    BTC 15m 72.1% (384 trades) | ETH 15m 71.0% (427) | BNB 15m 70.1% (368)
    Shorter period = more responsive, catches faster reversions.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 20:
        return None
    _, upper, lower = bollinger(df['Close'].values, 15, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_session_period25(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Session_Period25 (BB with n=25, 8-20 UTC)
    BNB 15m 72.1% (315 trades) | BTC 15m 71.3% (324) | ETH 15m 68.5% (356)
    Longer period = smoother bands, fewer but higher-quality signals.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 30:
        return None
    _, upper, lower = bollinger(df['Close'].values, 25, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_adaptive_bb_atr10(df: pd.DataFrame) -> str | None:
    """
    15m/1h — AdaptiveBB_ATR10 (faster ATR period 10)
    SOL 15m 69.1% (236 trades) | SOL 1h 64.9% (57) | ETH 1h 77.4% (53)
    More reactive than ATR14 — catches sharper reversions faster.
    """
    if len(df) < 35:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 10)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]) or np.isnan(e50[-1]): return None
    k     = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper = mid[-1] + k * std[-1]
    lower = mid[-1] - k * std[-1]
    above = close > e50[-1]
    if close < lower and above:
        return "UP"
    if close > upper and not above:
        return "DOWN"
    return None


def signal_vwap_session_8_20(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — VWAP_Session_8_20 (VWAP ZScore during 8-20 UTC only)
    DOGE 15m 68.4% (433 trades) | XRP 15m 64.6% (426) | SOL 15m 63.2% (440)
    Session filter removes overnight VWAP drift, keeps EU/US hours only.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    c    = df['Close'].values
    v    = df['Volume'].values
    vwap = (pd.Series(c * v).rolling(20).sum() / (pd.Series(v).rolling(20).sum() + 1e-10)).values
    std  = pd.Series(c).rolling(20).std().values
    e200 = ema(c, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(vwap[-1]) or np.isnan(std[-1]) or np.isnan(e200[-1]): return None
    z     = (close - vwap[-1]) / (std[-1] + 1e-10)
    above = close > e200[-1]
    if z < -1.5 and above:
        return "UP"
    if z > 1.5 and not above:
        return "DOWN"
    return None


def signal_stretch_capitul_rsi40(df: pd.DataFrame) -> str | None:
    """
    15m/1h — StretchScore_Capitul_RSI40 (StretchScore + RSI<40 capitulation agree)
    DOGE 15m 70.5% (44 trades) | BNB 15m 62.1% (29) | BNB Sharpe +0.027
    Both stretched AND capitulation RSI confirms = highest conviction reversals.
    """
    if len(df) < 55:
        return None
    c      = df['Close'].values
    vol    = df['Volume'].values
    mid    = pd.Series(c).rolling(20).mean().values
    std    = pd.Series(c).rolling(20).std().values
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    rsi_v  = rsi(c, 14)
    vol_ma = pd.Series(vol).rolling(20).mean().values
    e50    = ema(c, 50)
    close  = c[-1]
    if np.isnan(upper[-1]) or np.isnan(e50[-1]) or np.isnan(rsi_v[-1]): return None
    above     = close > e50[-1]
    band_dn   = (mid[-1] - close) / (mid[-1] - lower[-1] + 1e-10)
    band_up   = (close - mid[-1]) / (upper[-1] - mid[-1] + 1e-10)
    vol_spike = vol[-1] > vol_ma[-1] * 1.5
    stretched_dn = band_dn >= 0.80
    stretched_up = band_up >= 0.80
    if stretched_dn and vol_spike and rsi_v[-1] < 40 and above:
        return "UP"
    if stretched_up and vol_spike and rsi_v[-1] > 60 and not above:
        return "DOWN"
    return None


def signal_consensus_bb_adaptive(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Consensus_BB_AND_Adaptive (BB_Session + AdaptiveBB both agree)
    SOL 15m 65.2% Sharpe +0.751 | DOGE 15m 68.7% | ETH 1h 75.0% | BTC 1h 71.4%
    Best Sharpe ratio of any 15m strategy — two unrelated signals confirming.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 55:
        return None
    # BB component
    _, upper_bb, lower_bb = bollinger(df['Close'].values, 20, 2)
    # AdaptiveBB component
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    close  = df['Close'].iloc[-1]
    if np.isnan(lower_bb[-1]) or np.isnan(atr_v[-1]) or np.isnan(e50[-1]): return None
    k      = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper_ada = mid[-1] + k * std[-1]
    lower_ada = mid[-1] - k * std[-1]
    above  = close > e50[-1]
    bb_up  = close < lower_bb[-1]
    bb_dn  = close > upper_bb[-1]
    ada_up = close < lower_ada and above
    ada_dn = close > upper_ada and not above
    if bb_up and ada_up:
        return "UP"
    if bb_dn and ada_dn:
        return "DOWN"
    return None


def _connors_rsi(close_arr, rsi_n=3, streak_n=2, rank_n=100):
    """ConnorsRSI = avg(RSI3, RSI_of_streak, PercentRank_of_returns)."""
    c = pd.Series(close_arr)
    rsi3 = pd.Series(rsi(close_arr, rsi_n))
    streak = np.zeros(len(c))
    for i in range(1, len(c)):
        if c.iloc[i] > c.iloc[i-1]:
            streak[i] = streak[i-1] + 1 if streak[i-1] > 0 else 1
        elif c.iloc[i] < c.iloc[i-1]:
            streak[i] = streak[i-1] - 1 if streak[i-1] < 0 else -1
    ret = c.pct_change()
    pct_rank = ret.rolling(rank_n).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100, raw=False)
    return ((rsi3 + pd.Series(rsi(streak, streak_n)) + pct_rank) / 3).values


def signal_connors_rsi_adaptive(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Consensus_ConnorsRSI_Adaptive (ConnorsRSI + AdaptiveBB agree)
    SOL 15m 63.2% (182 trades) | BTC 1h 70.6% (51) | BNB 1h 65.5% (58)
    ConnorsRSI<10 = extreme oversold + AdaptiveBB lower band = double confirmation.
    """
    if len(df) < 110:
        return None
    crsi   = _connors_rsi(df['Close'].values)
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    mid    = sma(df['Close'].values, 20)
    std    = pd.Series(df['Close'].values).rolling(20).std().values
    e50    = ema(df['Close'].values, 50)
    close  = df['Close'].iloc[-1]
    if np.isnan(crsi[-1]) or np.isnan(atr_v[-1]) or np.isnan(e50[-1]): return None
    k     = 2.5 if atr_v[-1] > atr_ma[-1] else 1.5
    upper = mid[-1] + k * std[-1]
    lower = mid[-1] - k * std[-1]
    above = close > e50[-1]
    if crsi[-1] < 10 and close < lower and above:
        return "UP"
    if crsi[-1] > 90 and close > upper and not above:
        return "DOWN"
    return None


def signal_fibonacci_382_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Fibonacci_382_LongOnly (38.2% retracement + EMA200 + engulfing)
    BTC 15m 72.5% (171 trades) | BNB 15m 70.3% (182) | ETH 15m 67.0% (206)
    Price pulls back to 38.2% of recent swing = trend still very strong.
    """
    if len(df) < 55:
        return None
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(e200[-1]) or close <= e200[-1]:
        return None
    recent_high = df['High'].iloc[-50:].max()
    recent_low  = df['Low'].iloc[-50:].min()
    if recent_high <= recent_low:
        return None
    swing  = recent_high - recent_low
    fib382 = recent_high - 0.382 * swing
    fib50  = recent_high - 0.500 * swing
    in_zone = fib50 <= close <= fib382
    engulf  = (df['Close'].iloc[-1] > df['Open'].iloc[-1] and
               df['Close'].iloc[-2] < df['Open'].iloc[-2])
    if in_zone and engulf:
        return "UP"
    return None


def signal_fibonacci_golden_zone_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Fibonacci_GoldenZone_LongOnly (50-61.8% zone + EMA200 + engulfing)
    BTC 15m 68.1% (141 trades) | BNB 15m 67.8% (149)
    Deeper retracement = bigger bounce, golden zone is the classic entry.
    """
    if len(df) < 55:
        return None
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(e200[-1]) or close <= e200[-1]:
        return None
    recent_high = df['High'].iloc[-50:].max()
    recent_low  = df['Low'].iloc[-50:].min()
    if recent_high <= recent_low:
        return None
    swing   = recent_high - recent_low
    fib500  = recent_high - 0.500 * swing
    fib618  = recent_high - 0.618 * swing
    in_zone = fib618 <= close <= fib500
    engulf  = (df['Close'].iloc[-1] > df['Open'].iloc[-1] and
               df['Close'].iloc[-2] < df['Open'].iloc[-2])
    if in_zone and engulf:
        return "UP"
    return None


def signal_three_bar_pattern(df: pd.DataFrame) -> str | None:
    """
    15m/1h — ThreeBar_Pattern (igniting bar → small pullback → confirmation close)
    BNB 15m 71.3% (157 trades) | ETH 15m 66.9% (163) | DOGE 15m 66.2% (154)
    Large igniting candle + tight pullback (<50% body) + close beyond pullback.
    """
    if len(df) < 10:
        return None
    atr_v = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    e50   = ema(df['Close'].values, 50)
    if np.isnan(atr_v[-1]) or np.isnan(e50[-1]):
        return None
    close = df['Close'].iloc[-1]
    above = close > e50[-1]
    ign_body = abs(df['Close'].iloc[-3] - df['Open'].iloc[-3])
    pb_body  = abs(df['Close'].iloc[-2] - df['Open'].iloc[-2])
    if ign_body < 1.5 * atr_v[-3]:
        return None
    if pb_body >= 0.5 * ign_body:
        return None
    if df['Close'].iloc[-3] > df['Open'].iloc[-3] and close > df['High'].iloc[-2] and above:
        return "UP"
    if df['Close'].iloc[-3] < df['Open'].iloc[-3] and close < df['Low'].iloc[-2] and not above:
        return "DOWN"
    return None


def signal_order_block_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — OrderBlock_LongOnly_EMA50 (last bearish candle before 1.5x ATR impulse)
    BTC 1h 71.2% (52 trades) | ETH 1h 69.7% (33) | BTC 15m 66.2% (210)
    Price retraces into the order block zone = institutional re-entry level.
    """
    if len(df) < 40:
        return None
    atr_v = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(atr_v[-1]) or np.isnan(e50[-1]) or close <= e50[-1]:
        return None
    for i in range(2, 31):
        if i + 1 >= len(df):
            break
        ob_o = df['Open'].iloc[-(i+1)]; ob_c = df['Close'].iloc[-(i+1)]
        ob_h = df['High'].iloc[-(i+1)]; ob_l = df['Low'].iloc[-(i+1)]
        imp_o = df['Open'].iloc[-i];    imp_c = df['Close'].iloc[-i]
        imp_body = abs(imp_c - imp_o)
        if (ob_c < ob_o and imp_c > imp_o and imp_body >= 1.5 * atr_v[-1]):
            if ob_l <= close <= ob_h:
                return "UP"
    return None


def signal_fair_value_gap_long(df: pd.DataFrame) -> str | None:
    """
    4h/1h — FairValueGap_LongOnly_EMA50 (3-candle imbalance, enter on 50% retrace)
    BTC 4h 65.7% (35 trades) | Best for 4h — large gaps more meaningful there.
    Bullish FVG: low[i] > high[i-2] = price gapped up, retrace to 50% midpoint.
    """
    if len(df) < 25:
        return None
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(e50[-1]) or close <= e50[-1]:
        return None
    for i in range(3, 21):
        if i + 2 >= len(df):
            break
        fvg_lo = df['High'].iloc[-(i+2)]
        fvg_hi = df['Low'].iloc[-i]
        if fvg_hi > fvg_lo:
            mid50 = (fvg_lo + fvg_hi) / 2
            if fvg_lo <= close <= mid50:
                return "UP"
    return None


def signal_stochastic_crossback_200ema(df: pd.DataFrame) -> str | None:
    """
    1h — Stochastic_Crossback_200EMA (K% crosses BACK inside OB/OS zone + 200 EMA)
    GOOGL 1h 61.5% Sharpe +0.017 | IWM 1h 68.6% | XRP 1h 60.2% (259 trades)
    Crossback = price already reversing confirmed, more selective than plain stoch cross.
    """
    if len(df) < 55:
        return None
    k, _ = stochastic(df['High'].values, df['Low'].values, df['Close'].values, 14, 3)
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(k[-1]) or np.isnan(e200[-1]):
        return None
    above = close > e200[-1]
    cross_back_up = k[-2] < 20 and k[-1] >= 20
    cross_back_dn = k[-2] > 80 and k[-1] <= 80
    if cross_back_up and above:
        return "UP"
    if cross_back_dn and not above:
        return "DOWN"
    return None


def signal_triple_ema_pullback(df: pd.DataFrame) -> str | None:
    """
    1h — TripleEMA_Pullback_25_50_100 (EMAs stacked, pullback to 25 then closes above)
    TSLA 1h 60.9% Sharpe +0.150 | Trend-continuation entry after healthy pullback.
    EMAs stacked 25>50>100 = uptrend. Pullback to 25, hold above 100, close back above 25.
    """
    if len(df) < 110:
        return None
    e25  = ema(df['Close'].values, 25)
    e50  = ema(df['Close'].values, 50)
    e100 = ema(df['Close'].values, 100)
    if np.isnan(e100[-1]):
        return None
    close = df['Close'].iloc[-1]
    if e25[-1] > e50[-1] > e100[-1]:
        was_below = df['Close'].iloc[-2] < e25[-2]
        now_above = close > e25[-1]
        held_100  = df['Low'].iloc[-2] > e100[-2] * 0.995
        if was_below and now_above and held_100:
            return "UP"
    if e25[-1] < e50[-1] < e100[-1]:
        was_above = df['Close'].iloc[-2] > e25[-2]
        now_below = close < e25[-1]
        held_100  = df['High'].iloc[-2] < e100[-2] * 1.005
        if was_above and now_below and held_100:
            return "DOWN"
    return None


def signal_rsi_bb_combo(df: pd.DataFrame) -> str | None:
    """
    15m/1h — RSI + BB Combo: RSI oversold AND price at lower BB = bounce.
    RSI overbought AND price at upper BB = drop.
    Both conditions must be true simultaneously.
    """
    if len(df) < 25:
        return None
    close  = df['Close'].values
    high   = df['High'].values
    low    = df['Low'].values
    r      = rsi(close, 14)
    mid    = pd.Series(close).rolling(20).mean().values
    std    = pd.Series(close).rolling(20).std().values
    upper  = mid + 2.0 * std
    lower  = mid - 2.0 * std
    if any(np.isnan(x) for x in [r[-1], upper[-1], lower[-1]]):
        return None
    p = close[-1]
    if r[-1] < 30 and p <= lower[-1] * 1.003:
        return "UP"
    if r[-1] > 70 and p >= upper[-1] * 0.997:
        return "DOWN"
    return None


def keltner(series_high, series_low, series_close, n=20, mult=1.5):
    """Keltner Channel: EMA-based midline + ATR-based bands."""
    mid   = pd.Series(series_close).ewm(span=n, adjust=False).mean()
    a     = pd.Series(atr(series_high, series_low, series_close, n))
    return mid.values, (mid + mult * a).values, (mid - mult * a).values


def signal_keltner_reversion_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — Keltner_Reversion_Session (ATR bands + 8-20 UTC)
    BTC 15m 70.8% (1,434 trades) | BNB 15m 71.5% (1,438) | ETH 15m 69.4% (1,431)
    Biggest sample of any strategy in factory — 1,400+ trades = most statistically reliable.
    ATR-based bands naturally widen in volatile markets → fewer bad signals than BB.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    km, ku, kd = keltner(df['High'].values, df['Low'].values, df['Close'].values, 20, 1.5)
    close = df['Close'].iloc[-1]
    if np.isnan(ku[-1]):
        return None
    if close < kd[-1]:
        return "UP"
    if close > ku[-1]:
        return "DOWN"
    return None


def signal_double_bb_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h — DoubleBB_Session_Confirm (BB(20,2) AND BB(10,1.5) both agree + 8-20 UTC + EMA50)
    BTC 15m | ETH 15m | BNB 15m — 12 winners
    Two independent band definitions both flagging extreme = highest conviction entry.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, up1, dn1 = bollinger(df['Close'].values, 20, 2.0)
    _, up2, dn2 = bollinger(df['Close'].values, 10, 1.5)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(dn1[-1]) or np.isnan(dn2[-1]) or np.isnan(e50[-1]):
        return None
    above = close > e50[-1]
    if close < dn1[-1] and close < dn2[-1] and above:
        return "UP"
    if close > up1[-1] and close > up2[-1] and not above:
        return "DOWN"
    return None


def signal_double_bb_session_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — DoubleBB_Session_Long (same as above but longs only)
    10 winners. Only buys most extreme oversold conditions, no short-side risk.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, _, dn1 = bollinger(df['Close'].values, 20, 2.0)
    _, _, dn2 = bollinger(df['Close'].values, 10, 1.5)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(dn1[-1]) or np.isnan(dn2[-1]) or np.isnan(e50[-1]):
        return None
    if close < dn1[-1] and close < dn2[-1] and close > e50[-1]:
        return "UP"
    return None


def signal_consecutive_4_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Consecutive_4_Session (4 same-direction candles = exhaustion → fade + 8-20 UTC + EMA50)
    37 winners — most winners of any Round 8 strategy.
    Pure price action — no indicators. Session-filtered exhaustion mean reversion.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    n = 4
    if len(df) < n + 2:
        return None
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(e50[-1]):
        return None
    above  = close > e50[-1]
    closes = list(df['Close'].iloc[-(n+1):-1])
    opens  = list(df['Open'].iloc[-(n+1):-1])
    n_down = all(closes[i] < opens[i] for i in range(n))
    n_up   = all(closes[i] > opens[i] for i in range(n))
    if n_down and above:
        return "UP"
    if n_up and not above:
        return "DOWN"
    return None


def signal_consecutive_5_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Consecutive_5_Session (5 same-direction candles = even more exhausted)
    25 winners. More selective than 4-candle version — higher WR, fewer signals.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    n = 5
    if len(df) < n + 2:
        return None
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(e50[-1]):
        return None
    above  = close > e50[-1]
    closes = list(df['Close'].iloc[-(n+1):-1])
    opens  = list(df['Open'].iloc[-(n+1):-1])
    n_down = all(closes[i] < opens[i] for i in range(n))
    n_up   = all(closes[i] > opens[i] for i in range(n))
    if n_down and above:
        return "UP"
    if n_up and not above:
        return "DOWN"
    return None


def signal_bb_volume_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Volume_Session (BB touch + volume > 1.2x avg + 8-20 UTC + EMA50)
    12 winners. Volume confirming at the band = real pressure not noise.
    Simpler than StretchScore — no RSI/stretch ratio, just band + volume + session.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2)
    vol_ma = sma(df['Volume'].values, 20)
    e50    = ema(df['Close'].values, 50)
    close  = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(vol_ma[-1]) or np.isnan(e50[-1]):
        return None
    above       = close > e50[-1]
    vol_confirm = df['Volume'].iloc[-1] > vol_ma[-1] * 1.2
    if close < lower[-1] and vol_confirm and above:
        return "UP"
    if close > upper[-1] and vol_confirm and not above:
        return "DOWN"
    return None


def signal_bb_volume_session_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Volume_Session_Long (same but long only)
    7 winners. Only buys volume-confirmed oversold bounces during session hours.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, _, lower = bollinger(df['Close'].values, 20, 2)
    vol_ma = sma(df['Volume'].values, 20)
    e50    = ema(df['Close'].values, 50)
    close  = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(vol_ma[-1]) or np.isnan(e50[-1]):
        return None
    vol_confirm = df['Volume'].iloc[-1] > vol_ma[-1] * 1.2
    if close < lower[-1] and vol_confirm and close > e50[-1]:
        return "UP"
    return None


def signal_bb_session_period10(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Session_Period10 (n=10 BB, 8-20 UTC)
    25 winners. Faster bands = more signals, catches sharper short-term reversions.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 15:
        return None
    _, upper, lower = bollinger(df['Close'].values, 10, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_session_period30(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Session_Period30 (n=30 BB, 8-20 UTC)
    14 winners. Slow bands = only most extreme moves trigger. Fewer but higher-quality.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 35:
        return None
    _, upper, lower = bollinger(df['Close'].values, 30, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_session_ema50_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Session_EMA50_LongOnly (BB n=20 + EMA50 trend + longs only + 8-20 UTC)
    10 winners. Classic BB_Session enhanced with trend filter, shorts removed.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 55:
        return None
    mid, _, lower = bollinger(df['Close'].values, 20, 2)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(e50[-1]):
        return None
    if close < lower[-1] and close > e50[-1]:
        return "UP"
    return None


def signal_bb_session_ema200_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_Session_EMA200_LongOnly (BB n=20 + EMA200 trend + longs only + 8-20 UTC)
    22 winners. Stronger trend filter than EMA50 — only longs in confirmed long-term uptrend.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 210:
        return None
    _, _, lower = bollinger(df['Close'].values, 20, 2)
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(e200[-1]):
        return None
    if close < lower[-1] and close > e200[-1]:
        return "UP"
    return None


def signal_consecutive_3_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — Consecutive_3_Session (3 same-direction candles + 8-20 UTC + EMA50)
    95 winners — highest of any strategy ever tested in factory.
    More trades than 4/5-candle versions while WR still holds.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    n = 3
    if len(df) < n + 2:
        return None
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(e50[-1]):
        return None
    above  = close > e50[-1]
    closes = list(df['Close'].iloc[-(n+1):-1])
    opens  = list(df['Open'].iloc[-(n+1):-1])
    n_down = all(closes[i] < opens[i] for i in range(n))
    n_up   = all(closes[i] > opens[i] for i in range(n))
    if n_down and above:
        return "UP"
    if n_up and not above:
        return "DOWN"
    return None


def signal_keltner_ema200_long(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — Keltner_EMA200_Long (Keltner + EMA200 trend + longs only + 8-20 UTC)
    69 winners — best Keltner variant, beats BB_Session_EMA200 (22W) by 3x.
    Only buys Keltner oversold touches in confirmed long-term uptrend.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 210:
        return None
    km, ku, kd = keltner(df['High'].values, df['Low'].values, df['Close'].values, 20, 1.5)
    e200  = ema(df['Close'].values, 200)
    close = df['Close'].iloc[-1]
    if np.isnan(ku[-1]) or np.isnan(e200[-1]):
        return None
    if close < kd[-1] and close > e200[-1]:
        return "UP"
    return None


def signal_bb_session_narrow(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — BB_Session_Narrow_k15 (BB n=20 k=1.5 + 8-20 UTC)
    55 winners. Narrower bands = more signals, WR still holds.
    Completes the k sweep: k=1.5 / k=2.0 (base) / k=2.5.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    mid, upper, lower = bollinger(df['Close'].values, 20, 1.5)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_afternoon_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — BB_Afternoon_14_20_UTC (US hours only, 14-20 UTC)
    45 winners. Afternoon slightly edges morning — US session drives most of the edge.
    """
    hour = datetime.now(timezone.utc).hour
    if not (14 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    mid, upper, lower = bollinger(df['Close'].values, 20, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_session_wide(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — BB_Session_Wide_k25 (BB n=20 k=2.5 + 8-20 UTC)
    42 winners. Wider = rarer signals but still wins across assets and timeframes.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    mid, upper, lower = bollinger(df['Close'].values, 20, 2.5)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_rsi_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — RSI_Session_30_70 (RSI < 30 / > 70 + 8-20 UTC + EMA50, no BB)
    40 winners. Confirms the session filter is the real edge — even plain RSI works inside it.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 20:
        return None
    r    = rsi(df['Close'].values, 14)
    e50  = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(r[-1]) or np.isnan(e50[-1]):
        return None
    above = close > e50[-1]
    if r[-1] < 30 and above:
        return "UP"
    if r[-1] > 70 and not above:
        return "DOWN"
    return None


def signal_bb_morning_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — BB_Morning_8_14_UTC (EU hours only, 8-14 UTC)
    39 winners. Morning session works independently — EU hours carry real edge.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 14):
        return None
    if len(df) < 25:
        return None
    mid, upper, lower = bollinger(df['Close'].values, 20, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_vwap_consecutive(df: pd.DataFrame) -> str | None:
    """
    15m/1h/4h — VWAP_Consecutive_Close_3 (3 consecutive closes above/below rolling VWAP)
    51 winners — already in factory but was missing from live bot.
    Pure momentum: 3 closes above VWAP = sustained buying = long.
    """
    if len(df) < 25:
        return None
    c    = df['Close'].values
    v    = df['Volume'].values
    vwap = (pd.Series(c * v).rolling(20).sum() / (pd.Series(v).rolling(20).sum() + 1e-10)).values
    if np.isnan(vwap[-1]):
        return None
    all_above = all(c[-i] > vwap[-i] for i in range(1, 4))
    all_below = all(c[-i] < vwap[-i] for i in range(1, 4))
    if all_above:
        return "UP"
    if all_below:
        return "DOWN"
    return None


def signal_engulfing_candle(df: pd.DataFrame) -> str | None:
    """
    15m/1h — Engulfing Candle + 200 EMA trend filter.
    Current candle fully swallows previous one.
    Bullish engulf in uptrend = UP. Bearish engulf in downtrend = DOWN.
    """
    if len(df) < 205:
        return None
    e200 = ema(df['Close'].values, 200)
    if np.isnan(e200[-1]):
        return None
    o = df['Open']; c = df['Close']
    p = c.iloc[-1]
    prev_bull = c.iloc[-2] > o.iloc[-2]
    prev_bear = c.iloc[-2] < o.iloc[-2]
    bull_engulf = (c.iloc[-1] > o.iloc[-1]) and prev_bear and (c.iloc[-1] > o.iloc[-2]) and (o.iloc[-1] < c.iloc[-2])
    bear_engulf = (c.iloc[-1] < o.iloc[-1]) and prev_bull and (c.iloc[-1] < o.iloc[-2]) and (o.iloc[-1] > c.iloc[-2])
    if bull_engulf and p > e200[-1]:
        return "UP"
    if bear_engulf and p < e200[-1]:
        return "DOWN"
    return None


def signal_donchian_ema200_long(df: pd.DataFrame) -> str | None:
    """
    Donchian_EMA200_Long — 20-bar Donchian breakout, long only above 200 EMA.
    Round 11 — NEW strategy. Dominates crypto 4h/1d and stocks.
    Crypto winners: BNB 1d 90% | XRP 1d 87% | DOGE 1d 82.9% | DOGE 4h 80.9%
                    HYPE 4h 78.4% | XRP 4h 76% | BNB 4h 67.6% | DOGE 1h 60.2%
    """
    if len(df) < 210:
        return None
    e200      = ema(df['Close'].values, 200)
    dc_high   = pd.Series(df['High'].values).rolling(20).max().values
    dc_low    = pd.Series(df['Low'].values).rolling(20).min().values
    close     = df['Close'].iloc[-1]
    if np.isnan(e200[-1]) or np.isnan(dc_high[-2]):
        return None
    if close > e200[-1] and close >= dc_high[-2]:
        return "UP"
    return None


def signal_bb_london_open(df: pd.DataFrame) -> str | None:
    """
    15m — BB_London_Open_6_9_UTC (plain BB during London open 6-9 UTC)
    BTC 79.1% | DOGE 80.4% | SOL 78.9% | BNB 73.5% | ETH 71.4% | XRP 69.2%
    London open liquidity flush = sharpest mean reversion window of the day.
    """
    hour = datetime.now(timezone.utc).hour
    if not (6 <= hour < 9):
        return None
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_hammer_shooting_star(df: pd.DataFrame) -> str | None:
    """
    1d — Hammer_ShootingStar_200EMA (hammer/shooting star candle + EMA200 trend filter)
    DOGE 1d 78% | BNB 1d 71.4% | ETH 1d 68.4% | SOL 1d 68.3%
    Hammer: lower shadow >= 2x body. Shooting star: upper shadow >= 2x body.
    """
    if len(df) < 205:
        return None
    e200 = ema(df['Close'].values, 200)
    if np.isnan(e200[-1]):
        return None
    c = df['Close'].iloc[-1]; o = df['Open'].iloc[-1]
    h = df['High'].iloc[-1];  l = df['Low'].iloc[-1]
    body         = abs(c - o)
    upper_shadow = h - max(c, o)
    lower_shadow = min(c, o) - l
    if body < 1e-10:
        return None
    if lower_shadow >= 2 * body and upper_shadow <= 0.5 * body and c > e200[-1]:
        return "UP"
    if upper_shadow >= 2 * body and lower_shadow <= 0.5 * body and c < e200[-1]:
        return "DOWN"
    return None


def signal_bb_us_session(df: pd.DataFrame) -> str | None:
    """
    15m/1h — BB_US_Session_1320_UTC (plain BB n=20 k=2 during US session 13-20 UTC)
    BNB 15m 73.5% 994 trades | BTC 15m 70.7% 992 trades
    Simpler/different from AdaptiveBB version — fixed bands, US session only.
    """
    hour = datetime.now(timezone.utc).hour
    if not (13 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_stretch_strict_25std(df: pd.DataFrame) -> str | None:
    """
    15m — StretchScore_Strict_25std (k=2.5 wider bands, stretch ratio >= 0.9, RSI<30/>70)
    BTC 15m 72.2% | ETH 15m 68% | SOL 15m 63.8%
    Rarest signals — only fires when price truly deep outside wide bands.
    """
    if len(df) < 55:
        return None
    mid, up, dn = bollinger(df['Close'].values, 20, 2.5)
    rsi_v = rsi(df['Close'].values, 14)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(up[-1]) or np.isnan(e50[-1]):
        return None
    above  = close > e50[-1]
    std_dn = (mid[-1] - close)  / (mid[-1] - dn[-1] + 1e-10)
    std_up = (close  - mid[-1]) / (up[-1]  - mid[-1] + 1e-10)
    if std_dn >= 0.90 and rsi_v[-1] < 30 and above:
        return "UP"
    if std_up >= 0.90 and rsi_v[-1] > 70 and not above:
        return "DOWN"
    return None


def signal_stretch_relaxed(df: pd.DataFrame) -> str | None:
    """
    1h/4h — StretchScore_1h_Relaxed (stretch ratio >= 0.70, RSI<40/>60, EMA50)
    ETH 1h 71.4% | BTC 1h 67.7% | SOL 4h 65.2% | BNB 4h 62.1%
    Relaxed thresholds = more signals while still holding an edge.
    """
    if len(df) < 55:
        return None
    mid, up, dn = bollinger(df['Close'].values, 20, 2)
    rsi_v = rsi(df['Close'].values, 14)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(up[-1]) or np.isnan(e50[-1]):
        return None
    above  = close > e50[-1]
    std_dn = (mid[-1] - close)  / (mid[-1] - dn[-1] + 1e-10)
    std_up = (close  - mid[-1]) / (up[-1]  - mid[-1] + 1e-10)
    if std_dn >= 0.70 and rsi_v[-1] < 40 and above:
        return "UP"
    if std_up >= 0.70 and rsi_v[-1] > 60 and not above:
        return "DOWN"
    return None


def signal_asian_range_breakout(df: pd.DataFrame) -> str | None:
    """
    1h — Asian_Range_Breakout_0104 (breakout of 0-4 UTC range, trade 4-20 UTC)
    BTC 1h 68% 460+ trades | ETH 1h 66% | SOL 1h 63% | DOGE 1h 58%
    Asian range sets up the directional play for EU/US sessions.
    """
    hour = datetime.now(timezone.utc).hour
    if not (4 <= hour < 20):
        return None
    if len(df) < 10:
        return None
    now   = df.index[-1]
    today = pd.Timestamp(now).normalize()
    asian = df[(df.index >= today) & (df.index < today + pd.Timedelta(hours=4))]
    if len(asian) < 2:
        return None
    asian_high = asian['High'].max()
    asian_low  = asian['Low'].min()
    close      = df['Close'].iloc[-1]
    prev_close = df['Close'].iloc[-2]
    if prev_close <= asian_high and close > asian_high:
        return "UP"
    if prev_close >= asian_low and close < asian_low:
        return "DOWN"
    return None


def signal_bb_rsi_long_only(df: pd.DataFrame) -> str | None:
    """
    15m — BB_RSI_LongOnly (RSI<30 + lower BB touch + above EMA50, longs only)
    BTC 15m 68% | SOL 15m 64% | ETH 15m 59%
    Long-only version of RSI_BB_Combo — removes shorts for cleaner crypto edge.
    """
    if len(df) < 55:
        return None
    c = df['Close'].values
    r = rsi(c, 14)
    _, _, lower = bollinger(c, 20, 2)
    e50 = ema(c, 50)
    if np.isnan(r[-1]) or np.isnan(lower[-1]) or np.isnan(e50[-1]):
        return None
    if r[-1] < 30 and c[-1] <= lower[-1] * 1.003 and c[-1] > e50[-1]:
        return "UP"
    return None


def signal_asian_volume_surge(df: pd.DataFrame) -> str | None:
    """
    1h/4h — Asian_Volume_Surge_2x (volume 2x avg during Asian session 0-8 UTC)
    ETH 1h 62% | BTC 1h 62% | SOL 4h 61%
    Asian volume surge predicts EU session direction.
    """
    hour = datetime.now(timezone.utc).hour
    if not (0 <= hour < 8):
        return None
    if len(df) < 25:
        return None
    vol_ma = sma(df['Volume'].values, 20)
    close  = df['Close'].iloc[-1]
    e50    = ema(df['Close'].values, 50)
    if np.isnan(vol_ma[-1]) or np.isnan(e50[-1]):
        return None
    if df['Volume'].iloc[-1] > vol_ma[-1] * 2.0:
        if close > e50[-1]:
            return "UP"
        if close < e50[-1]:
            return "DOWN"
    return None


def signal_connors_rsi_session(df: pd.DataFrame) -> str | None:
    """
    1h/4h — ConnorsRSI_Session_8_20 (ConnorsRSI extreme during EU/US session only)
    BTC 1h 62% | ETH 4h 61% | SOL 1h 60%
    Pure ConnorsRSI < 10 / > 90 in session — no AdaptiveBB requirement.
    """
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 110:
        return None
    crsi  = _connors_rsi(df['Close'].values)
    e50   = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(crsi[-1]) or np.isnan(e50[-1]):
        return None
    above = close > e50[-1]
    if crsi[-1] < 10 and above:
        return "UP"
    if crsi[-1] > 90 and not above:
        return "DOWN"
    return None


def signal_ema50_pullback(df: pd.DataFrame) -> str | None:
    """
    1d — EMA50_Pullback (price pulls back to EMA50 then closes above it, above EMA200)
    XRP 1d 76.1% | SOL 1d 71.1% | DOGE 1d 68.1%
    Classic pullback-to-trend entry on daily charts.
    """
    if len(df) < 210:
        return None
    e50  = ema(df['Close'].values, 50)
    e200 = ema(df['Close'].values, 200)
    close  = df['Close'].iloc[-1]
    prev_c = df['Close'].iloc[-2]
    low    = df['Low'].iloc[-1]
    if np.isnan(e50[-1]) or np.isnan(e200[-1]):
        return None
    # Pullback: prev close below EMA50, current low touches it, closes back above
    if prev_c < e50[-2] and low <= e50[-1] * 1.005 and close > e50[-1] and close > e200[-1]:
        return "UP"
    return None


def signal_fair_value_gap_1d(df: pd.DataFrame) -> str | None:
    """
    1d — FairValueGap_EMA200 (3-candle FVG fill + above EMA200)
    DOGE 1d 78% | BNB 1d 71.4% | ETH 1d 68.4% | SOL 1d 68.3% | XRP 1d 67.1%
    Large daily FVGs attract price back for a fill before continuation.
    """
    if len(df) < 205:
        return None
    e200 = ema(df['Close'].values, 200)
    if np.isnan(e200[-1]):
        return None
    # FVG: candle[-3] high < candle[-1] low = bullish gap (price fills down into it)
    # or:  candle[-3] low > candle[-1] high = bearish gap
    h3 = df['High'].iloc[-3]; l3 = df['Low'].iloc[-3]
    h1 = df['High'].iloc[-1]; l1 = df['Low'].iloc[-1]
    c  = df['Close'].iloc[-1]
    if l3 > h1 and c < e200[-1]:    # bearish FVG, price below EMA200 → DOWN
        return "DOWN"
    if h3 < l1 and c > e200[-1]:    # bullish FVG, price above EMA200 → UP
        return "UP"
    return None


def signal_heikin_ashi_trend(df: pd.DataFrame) -> str | None:
    """
    1d — HeikinAshi_Trend (3 consecutive same-direction HA candles + EMA200 filter)
    SOL 1d ~74% | HYPE 1d ~73% | DOGE 1d ~71% | ETH 1d ~70%
    HA candles smooth noise — 3 in a row = clean trend continuation signal.
    """
    if len(df) < 205:
        return None
    e200 = ema(df['Close'].values, 200)
    if np.isnan(e200[-1]):
        return None
    # Compute Heikin-Ashi for last 4 bars
    ha_close = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4
    ha_open  = ha_close.copy()
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
    # Check last 3 HA candles
    bullish = all(ha_close.iloc[i] > ha_open.iloc[i] for i in [-3, -2, -1])
    bearish = all(ha_close.iloc[i] < ha_open.iloc[i] for i in [-3, -2, -1])
    c = df['Close'].iloc[-1]
    if bullish and c > e200[-1]:
        return "UP"
    if bearish and c < e200[-1]:
        return "DOWN"
    return None


def signal_bb_wide_25std(df: pd.DataFrame) -> str | None:
    """BB_Wide_25std — k=2.5 wide bands, no session filter. BTC 15m 69.9% | BNB 68.3%"""
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2.5)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_bb_session_wide_25std(df: pd.DataFrame) -> str | None:
    """BB_Session_Wide_25std — k=2.5 wide bands + 8-20 UTC session. BTC 15m 73.2% | BNB 69.6% | ETH 69.6%"""
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 2.5)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


def signal_stretch_session_8_20(df: pd.DataFrame) -> str | None:
    """StretchScore_Session_8_20 — stretch ratio >= 0.80 + session filter only (no volume/cap req).
    ETH 15m 75.7% | SOL 15m 78% | DOGE 15m 72.3% | BNB 1h 78.4%"""
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 55:
        return None
    mid, up, dn = bollinger(df['Close'].values, 20, 2)
    e50  = ema(df['Close'].values, 50)
    close = df['Close'].iloc[-1]
    if np.isnan(up[-1]) or np.isnan(e50[-1]):
        return None
    above  = close > e50[-1]
    std_dn = (mid[-1] - close)  / (mid[-1] - dn[-1] + 1e-10)
    std_up = (close  - mid[-1]) / (up[-1]  - mid[-1] + 1e-10)
    if std_dn >= 0.80 and above:
        return "UP"
    if std_up >= 0.80 and not above:
        return "DOWN"
    return None


def signal_stretch_capitulation_raw(df: pd.DataFrame) -> str | None:
    """StretchScore_Capitulation — capitulation (stretch+volume+RSI), no session filter.
    BTC 1h 84% | ETH 1h 80.6%"""
    if len(df) < 25:
        return None
    c      = df['Close'].values
    vol    = df['Volume'].values
    mid    = pd.Series(c).rolling(20).mean().values
    std    = pd.Series(c).rolling(20).std().values
    upper  = mid + 2 * std
    lower  = mid - 2 * std
    rsi_v  = rsi(c, 14)
    vol_ma = pd.Series(vol).rolling(20).mean().values
    e50    = ema(c, 50)
    close  = c[-1]
    if np.isnan(upper[-1]) or np.isnan(e50[-1]) or np.isnan(rsi_v[-1]): return None
    above     = close > e50[-1]
    band_dn   = (mid[-1] - close) / (mid[-1] - lower[-1] + 1e-10)
    band_up   = (close - mid[-1]) / (upper[-1] - mid[-1] + 1e-10)
    vol_spike = vol[-1] > vol_ma[-1] * 1.5
    if band_dn >= 0.80 and vol_spike and rsi_v[-1] < 35 and above:
        return "UP"
    if band_up >= 0.80 and vol_spike and rsi_v[-1] > 65 and not above:
        return "DOWN"
    return None


def signal_stretch_custom(df: pd.DataFrame) -> str | None:
    """StretchScore_Custom — ATR-normalized stretch, no trend filter. ETH 4h 77.8% | ETH 1h 70.4% | DOGE 1h 63.9%"""
    if len(df) < 30:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    atr_ma = sma(atr_v, 20)
    close  = df['Close'].iloc[-1]
    mid    = sma(df['Close'].values, 20)
    if np.isnan(atr_v[-1]) or np.isnan(atr_ma[-1]): return None
    stretch = (abs(close - mid[-1])) / (atr_v[-1] + 1e-10)
    if stretch < 1.5:
        return None
    if close < mid[-1]:
        return "UP"
    if close > mid[-1]:
        return "DOWN"
    return None


def signal_keltner_volume_session(df: pd.DataFrame) -> str | None:
    """Keltner_Volume_Session — Keltner touch + volume surge + session. ETH 15m 90% (20t)"""
    hour = datetime.now(timezone.utc).hour
    if not (8 <= hour < 20):
        return None
    if len(df) < 25:
        return None
    atr_v  = atr(df['High'].values, df['Low'].values, df['Close'].values, 14)
    mid    = ema(df['Close'].values, 20)
    upper  = mid + 1.5 * atr_v
    lower  = mid - 1.5 * atr_v
    vol_ma = sma(df['Volume'].values, 20)
    close  = df['Close'].iloc[-1]
    if np.isnan(lower[-1]) or np.isnan(vol_ma[-1]): return None
    vol_surge = df['Volume'].iloc[-1] > vol_ma[-1] * 1.5
    if close < lower[-1] and vol_surge:
        return "UP"
    if close > upper[-1] and vol_surge:
        return "DOWN"
    return None


def signal_asian_bb_narrow(df: pd.DataFrame) -> str | None:
    """
    15m — Asian_BB_Narrow_k15 (BB n=20 k=1.5 during Asian session 0-8 UTC)
    BTC 15m 63.6% 3600 trades | ETH 15m 62.4% 3578 | BNB 15m 60.2% 1800+ | SOL 15m 58.7%
    Narrow k=1.5 bands fire much more often in Asian chop — highest signal volume strategy.
    """
    hour = datetime.now(timezone.utc).hour
    if not (0 <= hour < 8):
        return None
    if len(df) < 25:
        return None
    _, upper, lower = bollinger(df['Close'].values, 20, 1.5)
    close = df['Close'].iloc[-1]
    if np.isnan(lower[-1]):
        return None
    if close < lower[-1]:
        return "UP"
    if close > upper[-1]:
        return "DOWN"
    return None


# ─────────────────────────────────────────────
# TREND-FOLLOWING STRATEGIES (round 12)
# Fire WITH momentum — solve the "trending day" problem
# ─────────────────────────────────────────────

def signal_ema_cross(df: pd.DataFrame) -> str | None:
    """
    EMA_Cross_8_21 — EMA8 crosses EMA21, trade in direction of cross.
    Both UP and DOWN. Pure trend-following, fires on momentum shifts.
    """
    if len(df) < 25:
        return None
    e8  = ema(df['Close'].values, 8)
    e21 = ema(df['Close'].values, 21)
    if np.isnan(e8[-2]) or np.isnan(e21[-2]):
        return None
    prev_above = e8[-2] > e21[-2]
    curr_above = e8[-1] > e21[-1]
    if not prev_above and curr_above:
        return "UP"
    if prev_above and not curr_above:
        return "DOWN"
    return None


def signal_macd_cross(df: pd.DataFrame) -> str | None:
    """
    MACD_Cross — MACD histogram crosses zero (MACD line crosses signal line).
    Both UP and DOWN. Classic momentum crossover.
    """
    if len(df) < 35:
        return None
    macd_line, signal_line = macd_calc(df['Close'].values)
    hist = macd_line - signal_line
    if np.isnan(hist[-1]) or np.isnan(hist[-2]):
        return None
    if hist[-2] < 0 and hist[-1] > 0:
        return "UP"
    if hist[-2] > 0 and hist[-1] < 0:
        return "DOWN"
    return None


def signal_supertrend_flip(df: pd.DataFrame) -> str | None:
    """
    Supertrend_Flip — fires on the candle where Supertrend direction flips.
    ATR-based trailing stop (n=10, mult=3). Both UP and DOWN.
    Only fires on the flip candle — not on continuation.
    """
    if len(df) < 15:
        return None
    direction = supertrend(df['High'].values, df['Low'].values, df['Close'].values, n=10, mult=3)
    if direction[-2] == -1 and direction[-1] == 1:
        return "UP"
    if direction[-2] == 1 and direction[-1] == -1:
        return "DOWN"
    return None


def signal_hhll_trend(df: pd.DataFrame) -> str | None:
    """
    HH_HL_Trend — Higher Highs + Higher Lows = UP, Lower Lows + Lower Highs = DOWN.
    3 consecutive bars confirming the structure. Pure price action trend.
    """
    if len(df) < 5:
        return None
    highs = df['High'].values
    lows  = df['Low'].values
    hh = highs[-1] > highs[-2] > highs[-3]
    hl = lows[-1]  > lows[-2]  > lows[-3]
    ll = lows[-1]  < lows[-2]  < lows[-3]
    lh = highs[-1] < highs[-2] < highs[-3]
    if hh and hl:
        return "UP"
    if ll and lh:
        return "DOWN"
    return None


def signal_volume_breakout(df: pd.DataFrame) -> str | None:
    """
    Volume_Breakout_20 — price breaks 20-bar high/low on 2x avg volume.
    Both UP and DOWN. Volume confirms the breakout is real, not a fake-out.
    """
    if len(df) < 25:
        return None
    close   = df['Close'].iloc[-1]
    vol     = df['Volume'].iloc[-1]
    vol_ma  = df['Volume'].iloc[-21:-1].mean()
    high_20 = df['High'].iloc[-21:-1].max()
    low_20  = df['Low'].iloc[-21:-1].min()
    if vol_ma == 0:
        return None
    vol_surge = vol > vol_ma * 2.0
    if close > high_20 and vol_surge:
        return "UP"
    if close < low_20 and vol_surge:
        return "DOWN"
    return None


def signal_tf_confluence_1h4h(df: pd.DataFrame) -> str | None:
    """
    TF_Confluence_1h4h — checks VWAP_Consecutive_3 on both 1h AND 4h.
    If both agree on direction, bet that direction on 15m.
    Uses the asset tag stored in df.attrs['asset'] set by the scan loop.
    Both UP and DOWN — pure higher-timeframe bias trade.
    """
    asset = getattr(df, 'attrs', {}).get('asset')
    if not asset:
        return None

    exchange_obj = ccxt.binance({'enableRateLimit': True})
    signals = []
    for tf in ('1h', '4h'):
        try:
            bars = exchange_obj.fetch_ohlcv(asset, tf, limit=50)
            if not bars or len(bars) < 5:
                continue
            df_tf = pd.DataFrame(bars, columns=['timestamp','Open','High','Low','Close','Volume'])
            df_tf['timestamp'] = pd.to_datetime(df_tf['timestamp'], unit='ms', utc=True)
            df_tf.set_index('timestamp', inplace=True)
            sig = signal_vwap_consecutive(df_tf)
            if sig:
                signals.append(sig)
        except Exception:
            continue

    if len(signals) == 2 and signals[0] == signals[1]:
        return signals[0]
    return None


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# DISABLED COMBOS — (strategy_name, asset, direction)
# Re-enabled strategies that had good asset/direction subsets but bad overall WR.
# Only the losing combos are suppressed here; winners fire normally.
# Live WR in brackets. Updated 2026-04-30.
# ─────────────────────────────────────────────

DISABLED_COMBOS: set[tuple[str, str, str]] = {
    # BB_US_Session_1320 — ETH/BTC good (64-78%), bad combos suppressed
    ('signal_bb_us_session', 'SOL/USDT',  'UP'),    # 9%
    ('signal_bb_us_session', 'XRP/USDT',  'UP'),    # 36%
    ('signal_bb_us_session', 'BNB/USDT',  'UP'),    # 40%
    ('signal_bb_us_session', 'BNB/USDT',  'DOWN'),  # 20%
    ('signal_bb_us_session', 'HYPE/USDT', 'DOWN'),  # 25%

    # MACD_Cross — XRP DOWN 79%, BTC UP 62% good. Bad combos suppressed.
    ('signal_macd_cross', 'BNB/USDT',  'UP'),   # 27%
    ('signal_macd_cross', 'ETH/USDT',  'UP'),   # 33%
    ('signal_macd_cross', 'HYPE/USDT', 'UP'),   # 30%
    ('signal_macd_cross', 'SOL/USDT',  'UP'),   # 36%

    # HH_HL_Trend — DOWN direction is edge (58-75%). UP is consistently bad.
    ('signal_hhll_trend', 'BNB/USDT',  'UP'),   # 38%
    ('signal_hhll_trend', 'BTC/USDT',  'UP'),   # 46%
    ('signal_hhll_trend', 'DOGE/USDT', 'UP'),   # 50% — borderline, suppress
    ('signal_hhll_trend', 'ETH/USDT',  'UP'),   # 44%
    ('signal_hhll_trend', 'SOL/USDT',  'UP'),   # 46%
    ('signal_hhll_trend', 'XRP/USDT',  'UP'),   # 38%

    # ── 1h live data cuts (N>=10, WR<45%) ────────────────────────────────────
    # VWAP_Consecutive_3 — UP direction bad across all assets
    ('signal_vwap_consecutive', 'BTC/USDT',  'UP'),    # 44%
    ('signal_vwap_consecutive', 'BTC/USDT',  'DOWN'),  # 43%
    ('signal_vwap_consecutive', 'ETH/USDT',  'DOWN'),  # 45%
    ('signal_vwap_consecutive', 'XRP/USDT',  'UP'),    # 40%
    ('signal_vwap_consecutive', 'DOGE/USDT', 'DOWN'),  # 42%
    ('signal_vwap_consecutive', 'HYPE/USDT', 'UP'),    # 46%
    ('signal_vwap_consecutive', 'BNB/USDT',  'UP'),    # 46%
    ('signal_vwap_consecutive', 'SOL/USDT',  'UP'),    # 47%

    # BB_Session_Narrow_k15 — DOWN bad on ETH/SOL/XRP
    ('signal_bb_session_narrow', 'ETH/USDT', 'DOWN'),  # 42%
    ('signal_bb_session_narrow', 'SOL/USDT', 'UP'),    # 45%
    ('signal_bb_session_narrow', 'SOL/USDT', 'DOWN'),  # 48%
    ('signal_bb_session_narrow', 'XRP/USDT', 'UP'),    # 44%

    # Keltner_Reversion_Session — bad on SOL/BNB/XRP UP
    ('signal_keltner_reversion_session', 'SOL/USDT', 'UP'),    # 29%
    ('signal_keltner_reversion_session', 'SOL/USDT', 'DOWN'),  # 40%
    ('signal_keltner_reversion_session', 'BNB/USDT', 'UP'),    # 36%
    ('signal_keltner_reversion_session', 'XRP/USDT', 'UP'),    # 42%
    ('signal_keltner_reversion_session', 'ETH/USDT', 'DOWN'),  # 44%

    # StretchScore_Custom — bad on SOL UP / BNB UP / HYPE DOWN
    ('signal_stretch_custom', 'SOL/USDT',  'UP'),    # 39%
    ('signal_stretch_custom', 'BNB/USDT',  'UP'),    # 32%
    ('signal_stretch_custom', 'HYPE/USDT', 'DOWN'),  # 43%

    # HH_HL_Trend — DOWN also bad on SOL/ETH/BTC
    ('signal_hhll_trend', 'SOL/USDT',  'DOWN'),  # 41%
    ('signal_hhll_trend', 'ETH/USDT',  'DOWN'),  # 37%
    ('signal_hhll_trend', 'BTC/USDT',  'DOWN'),  # 46%

    # RSI_BB_Combo — DOWN bad on ETH
    ('signal_rsi_bb_combo', 'ETH/USDT', 'DOWN'),  # 41%

    # BB_Session_8_20 — DOWN bad on ETH/XRP/SOL
    ('signal_bb_session_15m', 'ETH/USDT', 'DOWN'),  # 43%
    ('signal_bb_session_15m', 'SOL/USDT', 'DOWN'),  # 39%

    # BB_Session_Period15 — DOWN bad on SOL/BTC
    ('signal_bb_session_period15', 'SOL/USDT', 'DOWN'),  # 30%
    ('signal_bb_session_period15', 'BTC/USDT', 'DOWN'),  # 44%

    # BB_Session_Period25 — DOWN bad on SOL
    ('signal_bb_session_period25', 'SOL/USDT', 'DOWN'),  # 30%

    # BB_US_Session_1320 — DOWN bad on XRP/SOL
    ('signal_bb_us_session', 'XRP/USDT', 'DOWN'),  # 30%
    ('signal_bb_us_session', 'SOL/USDT', 'DOWN'),  # 20%

    # VWAP_ZScore — UP bad on ETH/SOL/XRP
    ('signal_vwap_zscore', 'ETH/USDT', 'UP'),  # 39%
    ('signal_vwap_zscore', 'XRP/USDT', 'UP'),  # 42%

    # Donchian_EMA200_Long — UP bad on DOGE
    ('signal_donchian_ema200_long', 'DOGE/USDT', 'UP'),  # 30%

    # BB_Session_Period30 — DOWN bad on SOL/ETH
    ('signal_bb_session_period30', 'SOL/USDT', 'DOWN'),  # 36%
    ('signal_bb_session_period30', 'ETH/USDT', 'DOWN'),  # 44%

    # BB_Wide_25std — both directions bad on SOL
    ('signal_bb_wide_25std', 'SOL/USDT', 'UP'),    # 20%
    ('signal_bb_wide_25std', 'SOL/USDT', 'DOWN'),  # 17%

    # BB_Session_Period10 — both directions bad on SOL, DOWN bad on BTC
    ('signal_bb_session_period10', 'SOL/USDT', 'UP'),    # 20%
    ('signal_bb_session_period10', 'SOL/USDT', 'DOWN'),  # 20%
    ('signal_bb_session_period10', 'BTC/USDT', 'DOWN'),  # 22%

    # BB_Afternoon_14_20 — DOWN bad on SOL/BTC/XRP
    ('signal_bb_afternoon_session', 'SOL/USDT', 'UP'),    # 25%
    ('signal_bb_afternoon_session', 'SOL/USDT', 'DOWN'),  # 22%
    ('signal_bb_afternoon_session', 'BTC/USDT', 'DOWN'),  # 29%
    ('signal_bb_afternoon_session', 'XRP/USDT', 'DOWN'),  # 33%
    ('signal_bb_afternoon_session', 'XRP/USDT', 'UP'),    # 40%

    # BB_US_Session_1320 — DOWN bad on BTC/ETH
    ('signal_bb_us_session', 'BTC/USDT', 'DOWN'),  # 25%
    ('signal_bb_us_session', 'ETH/USDT', 'DOWN'),  # 22%

    # BB_Session_Period25 — UP bad on BNB
    ('signal_bb_session_period25', 'BNB/USDT', 'UP'),  # 17%

    # BB_Session_Narrow_k15 — UP bad on BNB
    ('signal_bb_session_narrow', 'BNB/USDT', 'UP'),  # 38%
}

# SIGNAL REGISTRIES — cast wide in paper mode
# ─────────────────────────────────────────────

SIGNALS_15M = [
    (signal_bb_session_15m,            'BB_Session_8_20'),            # 61% (336)
    (signal_bb_session_period10,       'BB_Session_Period10'),        # 52% (161)
    (signal_bb_session_period15,       'BB_Session_Period15'),        # 60% (297)
    (signal_bb_session_period25,       'BB_Session_Period25'),        # 59% (362)
    (signal_bb_session_period30,       'BB_Session_Period30'),        # 54% (323)
    (signal_bb_session_ema50_long,     'BB_Session_EMA50_LongOnly'),  # 100% (1)
    (signal_bb_session_ema200_long,    'BB_Session_EMA200_LongOnly'), # 29% (14) — watching
    (signal_bb_session_narrow,         'BB_Session_Narrow_k15'),      # 57% (642)
    (signal_bb_session_wide,           'BB_Session_Wide_k25'),        # 61% (99)
    (signal_bb_morning_session,        'BB_Morning_8_14'),            # 58% (150)
    (signal_bb_afternoon_session,      'BB_Afternoon_14_20'),         # 51% (132)
    (signal_keltner_reversion_session, 'Keltner_Reversion_Session'),  # 55% (578)
    (signal_keltner_ema200_long,       'Keltner_EMA200_Long'),        # 54% (28)
    (signal_double_bb_session,         'DoubleBB_Session_Confirm'),   # 100% (4)
    (signal_double_bb_session_long,    'DoubleBB_Session_Long'),      # 100% (1)
    # (signal_consecutive_3_session,   'Consecutive_3_Session'),      # REMOVED 37% (153)
    (signal_consecutive_4_session,     'Consecutive_4_Session'),      # 50% (58) — watching
    (signal_consecutive_5_session,     'Consecutive_5_Session'),      # 56% (16)
    (signal_rsi_session,               'RSI_Session_30_70'),          # 50% (22)
    (signal_bb_volume_session,         'BB_Volume_Session'),          # 100% (4)
    (signal_bb_volume_session_long,    'BB_Volume_Session_Long'),     # 100% (1)
    (signal_adaptive_bb_us_session,    'AdaptiveBB_US_Session_1320'), # 67% (3)
    (signal_adaptive_bb_session_8_20,  'AdaptiveBB_Session_8_20'),   # 56% (18)
    (signal_adaptive_bb_atr10,         'AdaptiveBB_ATR10'),           # 49% (37) — watching
    (signal_adaptive_bb_atr20,         'AdaptiveBB_ATR20'),           # 50% (46) — watching
    # (signal_adaptive_bb_long_only,   'AdaptiveBB_LongOnly'),        # REMOVED 46% (13)
    (signal_adaptive_bb,               'AdaptiveBB_ATR_Reversion'),   # 48% (29) — watching
    (signal_vwap_session_8_20,         'VWAP_Session_8_20'),          # 59% (160)
    (signal_stretch_capitul_rsi40,     'StretchScore_Capitul_RSI40'), # 80% (5)
    (signal_consensus_bb_adaptive,     'Consensus_BB_AND_Adaptive'),  # 67% (3)
    (signal_consensus_adaptive_vwap,   'Consensus_Adaptive_AND_VWAP'),# 50% (42)
    # (signal_connors_rsi_adaptive,    'Consensus_ConnorsRSI_Adaptive'), # REMOVED 14% (7)
    # (signal_fibonacci_382_long,      'Fibonacci_382_LongOnly'),     # REMOVED 42% (91)
    # (signal_fibonacci_golden_zone_long,'Fibonacci_GoldenZone_LongOnly'), # REMOVED 40% (68)
    # (signal_three_bar_pattern,       'ThreeBar_Pattern'),           # REMOVED 19% (16)
    # (signal_order_block_long,        'OrderBlock_LongOnly_EMA50'),  # REMOVED 42% (111)
    (signal_rsi_bb_combo,              'RSI_BB_Combo'),               # 56% (622)
    # (signal_engulfing_candle,        'Engulfing_Candle_200EMA'),    # REMOVED 47% (87)
    (signal_stretch_relaxed,           'StretchScore_1h_Relaxed'),    # 59% (37)
    (signal_stretch_capitulation,      'StretchScore_Capitul_Session'),# 100% (2)
    # (signal_bb_london_open,          'BB_London_Open_6_9'),         # REMOVED 44% (27)
    # (signal_bb_us_session,           'BB_US_Session_1320'),         # REMOVED 49% (114)
    (signal_stretch_strict_25std,      'StretchScore_Strict_25std'),  # 100% (2)
    (signal_bb_rsi_long_only,          'BB_RSI_LongOnly'),            # 67% (3)
    (signal_asian_bb_narrow,           'Asian_BB_Narrow_k15'),        # 53% (153)
    (signal_bb_wide_25std,             'BB_Wide_25std'),              # 52% (127)
    (signal_bb_session_wide_25std,     'BB_Session_Wide_25std'),      # 58% (66)
    (signal_stretch_session_8_20,      'StretchScore_Session_8_20'),  # 70% (10)
    (signal_stretch_capitulation_raw,  'StretchScore_Capitulation'),  # 67% (3)
    (signal_stretch_custom,            'StretchScore_Custom'),        # 53% (907)
    (signal_keltner_volume_session,    'Keltner_Volume_Session'),     # 54% (123)
    # ── round 12: trend-following ─────────────────────────────────────────
    # (signal_ema_cross,               'EMA_Cross_8_21'),             # REMOVED 47% (131)
    (signal_macd_cross,                'MACD_Cross'),                 # re-enabled: XRP DOWN 79%, BTC UP 62% — bad combos in DISABLED_COMBOS
    (signal_supertrend_flip,           'Supertrend_Flip'),            # 67% (6)
    (signal_hhll_trend,                'HH_HL_Trend'),                # re-enabled: DOWN direction edge (58-75%) — UP combos in DISABLED_COMBOS
    (signal_volume_breakout,           'Volume_Breakout_20'),         # 55% (115)
    (signal_tf_confluence_1h4h,        'TF_Confluence_1h4h'),         # 50% (42)
    (signal_bb_us_session,             'BB_US_Session_1320'),         # re-enabled: ETH 64-78%, BTC UP 64% — bad combos in DISABLED_COMBOS
]

SIGNALS_1D = [
    (signal_donchian_ema200_long,      'Donchian_EMA200_Long'),       # BNB 90% | XRP 87% | DOGE 82.9%
    (signal_hammer_shooting_star,      'Hammer_ShootingStar_200EMA'), # DOGE 78% | BNB 71.4% | ETH 68.4%
    # (signal_vwap_consecutive,        'VWAP_Consecutive_3'),         # REMOVED 46% live (1,388)
    (signal_ema50_pullback,            'EMA50_Pullback'),              # XRP 76.1% | SOL 71.1% | DOGE 68.1%
    (signal_fair_value_gap_1d,         'FairValueGap_EMA200_1D'),     # DOGE 78% | BNB 71.4% | ETH 68.4%
    (signal_heikin_ashi_trend,         'HeikinAshi_Trend'),           # SOL ~74% | HYPE ~73% | DOGE ~71%
    (signal_bb_session_15m,            'BB_Session_8_20'),
    (signal_bb_session_period15,       'BB_Session_Period15'),
    (signal_bb_session_period25,       'BB_Session_Period25'),
    (signal_adaptive_bb,               'AdaptiveBB_ATR_Reversion'),
    # (signal_adaptive_bb_long_only,   'AdaptiveBB_LongOnly'),        # REMOVED 46% live (13)
    (signal_adaptive_bb_session_8_20,  'AdaptiveBB_Session_8_20'),
    (signal_keltner_reversion_session, 'Keltner_Reversion_Session'),
    # (signal_consecutive_3_session,   'Consecutive_3_Session'),      # REMOVED 37% live (153)
    (signal_consecutive_4_session,     'Consecutive_4_Session'),
    (signal_consecutive_5_session,     'Consecutive_5_Session'),
    (signal_consensus_adaptive_vwap,   'Consensus_Adaptive_AND_VWAP'),
    (signal_consensus_bb_adaptive,     'Consensus_BB_AND_Adaptive'),
    # (signal_engulfing_candle,        'Engulfing_Candle_200EMA'),    # REMOVED 47% live (87)
    # (signal_three_bar_pattern,       'ThreeBar_Pattern'),           # REMOVED 19% live (16)
    # (signal_order_block_long,        'OrderBlock_LongOnly_EMA50'),  # REMOVED 42% live (111)
    # (signal_fibonacci_382_long,      'Fibonacci_382_LongOnly'),     # REMOVED 42% live (91)
    (signal_stretch_score,             'StretchScore'),
]

SIGNALS_1H = [
    (signal_bb_session_15m,            'BB_Session_8_20'),
    (signal_bb_session_period10,       'BB_Session_Period10'),
    (signal_bb_session_period15,       'BB_Session_Period15'),
    (signal_bb_session_period25,       'BB_Session_Period25'),
    (signal_bb_session_period30,       'BB_Session_Period30'),
    (signal_bb_session_ema50_long,     'BB_Session_EMA50_LongOnly'),
    (signal_bb_session_ema200_long,    'BB_Session_EMA200_LongOnly'),
    (signal_bb_session_narrow,         'BB_Session_Narrow_k15'),
    (signal_bb_session_wide,           'BB_Session_Wide_k25'),
    (signal_bb_morning_session,        'BB_Morning_8_14'),
    (signal_bb_afternoon_session,      'BB_Afternoon_14_20'),
    (signal_keltner_reversion_session, 'Keltner_Reversion_Session'),
    (signal_keltner_ema200_long,       'Keltner_EMA200_Long'),
    (signal_double_bb_session,         'DoubleBB_Session_Confirm'),
    (signal_double_bb_session_long,    'DoubleBB_Session_Long'),
    # (signal_consecutive_3_session,   'Consecutive_3_Session'),      # REMOVED 37% live (153)
    (signal_consecutive_4_session,     'Consecutive_4_Session'),
    (signal_consecutive_5_session,     'Consecutive_5_Session'),
    (signal_rsi_session,               'RSI_Session_30_70'),
    # (signal_vwap_consecutive,        'VWAP_Consecutive_3'),         # REMOVED 46% live (1,388)
    (signal_bb_volume_session,         'BB_Volume_Session'),
    (signal_bb_volume_session_long,    'BB_Volume_Session_Long'),
    (signal_adaptive_bb,               'AdaptiveBB_ATR_Reversion'),
    # (signal_adaptive_bb_long_only,   'AdaptiveBB_LongOnly'),        # REMOVED 46% live (13)
    (signal_adaptive_bb_session_8_20,  'AdaptiveBB_Session_8_20'),
    (signal_adaptive_bb_us_session,    'AdaptiveBB_US_Session_1320'),
    (signal_adaptive_bb_atr10,         'AdaptiveBB_ATR10'),
    (signal_adaptive_bb_atr20,         'AdaptiveBB_ATR20'),
    (signal_stretch_capitulation,      'StretchScore_Capitul_Session'),
    (signal_capitul_rsi40,             'Capitul_RSI40'),
    (signal_vwap_zscore,               'VWAP_ZScore'),
    (signal_vwap_session_8_20,         'VWAP_Session_8_20'),
    (signal_stretch_capitul_rsi40,     'StretchScore_Capitul_RSI40'),
    (signal_consensus_bb_adaptive,     'Consensus_BB_AND_Adaptive'),
    (signal_consensus_adaptive_vwap,   'Consensus_Adaptive_AND_VWAP'),
    # (signal_connors_rsi_adaptive,    'Consensus_ConnorsRSI_Adaptive'), # REMOVED 14% live (7)
    # (signal_fibonacci_382_long,      'Fibonacci_382_LongOnly'),     # REMOVED 42% live (91)
    # (signal_fibonacci_golden_zone_long,'Fibonacci_GoldenZone_LongOnly'), # REMOVED 40% live (68)
    # (signal_three_bar_pattern,       'ThreeBar_Pattern'),           # REMOVED 19% live (16)
    # (signal_order_block_long,        'OrderBlock_LongOnly_EMA50'),  # REMOVED 42% live (111)
    # (signal_stochastic_crossback_200ema,'Stochastic_Crossback_200EMA'), # REMOVED 41% live (54)
    # (signal_triple_ema_pullback,     'TripleEMA_Pullback'),         # REMOVED 49% live (41)
    (signal_rsi_bb_combo,              'RSI_BB_Combo'),
    # (signal_engulfing_candle,        'Engulfing_Candle_200EMA'),    # REMOVED 47% live (87)
    # ── round 11 ──────────────────────────────────────────────────────────
    (signal_donchian_ema200_long,      'Donchian_EMA200_Long'),  # DOGE 1h 60.2%
    # (signal_bb_london_open,          'BB_London_Open_6_9'),         # REMOVED 44% live (27)
    # (signal_bb_us_session,           'BB_US_Session_1320'),         # REMOVED 49% live (114)
    (signal_stretch_relaxed,           'StretchScore_1h_Relaxed'),
    (signal_stretch_score_fast,        'StretchScore_Fast_EMA50'),
    (signal_stretch_session_8_20,      'StretchScore_Session_8_20'),
    (signal_stretch_capitulation_raw,  'StretchScore_Capitulation'),
    (signal_stretch_custom,            'StretchScore_Custom'),
    (signal_bb_wide_25std,             'BB_Wide_25std'),
    (signal_bb_session_wide_25std,     'BB_Session_Wide_25std'),
    (signal_asian_range_breakout,      'Asian_Range_Breakout_0104'),
    (signal_asian_volume_surge,        'Asian_Volume_Surge_2x'),
    (signal_connors_rsi_session,       'ConnorsRSI_Session_8_20'),
    # ── round 12: trend-following ─────────────────────────────────────────
    # (signal_ema_cross,               'EMA_Cross_8_21'),             # REMOVED 47% live (131)
    (signal_macd_cross,                'MACD_Cross'),                 # re-enabled: XRP DOWN 79%, BTC UP 62%
    (signal_supertrend_flip,           'Supertrend_Flip'),
    (signal_hhll_trend,                'HH_HL_Trend'),                # re-enabled: DOWN direction edge
    (signal_volume_breakout,           'Volume_Breakout_20'),
    (signal_tf_confluence_1h4h,        'TF_Confluence_1h4h'),
    (signal_bb_us_session,             'BB_US_Session_1320'),         # re-enabled: ETH/BTC good
]

SIGNALS_4H = [
    (signal_keltner_reversion_session, 'Keltner_Reversion_Session'),
    (signal_keltner_ema200_long,       'Keltner_EMA200_Long'),
    # (signal_consecutive_3_session,   'Consecutive_3_Session'),      # REMOVED 37% live (153)
    (signal_consecutive_4_session,     'Consecutive_4_Session'),
    (signal_consecutive_5_session,     'Consecutive_5_Session'),
    (signal_rsi_session,               'RSI_Session_30_70'),
    # (signal_vwap_consecutive,        'VWAP_Consecutive_3'),         # REMOVED 46% live (1,388)
    (signal_bb_afternoon_session,      'BB_Afternoon_14_20'),
    (signal_bb_morning_session,        'BB_Morning_8_14'),
    (signal_adaptive_bb,               'AdaptiveBB_ATR_Reversion'),
    (signal_adaptive_bb_atr10,         'AdaptiveBB_ATR10'),
    (signal_adaptive_bb_atr20,         'AdaptiveBB_ATR20'),
    (signal_adaptive_bb_session_8_20,  'AdaptiveBB_Session_8_20'),   # BTC 4h 75%
    # (signal_adaptive_bb_long_only,   'AdaptiveBB_LongOnly'),        # REMOVED 46% live (13)
    (signal_vwap_zscore,               'VWAP_ZScore'),
    (signal_vwap_session_8_20,         'VWAP_Session_8_20'),
    (signal_stretch_score,             'StretchScore'),
    (signal_consensus_adaptive_vwap,   'Consensus_Adaptive_AND_VWAP'),
    (signal_consensus_bb_adaptive,     'Consensus_BB_AND_Adaptive'),
    # (signal_connors_rsi_adaptive,    'Consensus_ConnorsRSI_Adaptive'), # REMOVED 14% live (7)
    (signal_fair_value_gap_long,       'FairValueGap_LongOnly_EMA50'),
    # (signal_order_block_long,        'OrderBlock_LongOnly_EMA50'),  # REMOVED 42% live (111)
    # (signal_fibonacci_382_long,      'Fibonacci_382_LongOnly'),     # REMOVED 42% live (91)
    # (signal_three_bar_pattern,       'ThreeBar_Pattern'),           # REMOVED 19% live (16)
    (signal_donchian_ema200_long,      'Donchian_EMA200_Long'),
    (signal_stretch_relaxed,           'StretchScore_1h_Relaxed'),
    (signal_stretch_session_8_20,      'StretchScore_Session_8_20'),
    (signal_stretch_capitulation_raw,  'StretchScore_Capitulation'),
    (signal_stretch_custom,            'StretchScore_Custom'),
    (signal_bb_wide_25std,             'BB_Wide_25std'),
    (signal_bb_session_wide_25std,     'BB_Session_Wide_25std'),
    (signal_asian_volume_surge,        'Asian_Volume_Surge_2x'),
    (signal_connors_rsi_session,       'ConnorsRSI_Session_8_20'),
    # (signal_engulfing_candle,        'Engulfing_Candle_200EMA'),    # REMOVED 47% live (87)
    # ── round 12: trend-following ─────────────────────────────────────────
    # (signal_ema_cross,               'EMA_Cross_8_21'),             # REMOVED 47% live (131)
    (signal_macd_cross,                'MACD_Cross'),                 # re-enabled: XRP DOWN 79%, BTC UP 62%
    (signal_supertrend_flip,           'Supertrend_Flip'),
    (signal_hhll_trend,                'HH_HL_Trend'),                # re-enabled: DOWN direction edge
    (signal_volume_breakout,           'Volume_Breakout_20'),
    (signal_bb_us_session,             'BB_US_Session_1320'),         # re-enabled: ETH/BTC good
]


# ─────────────────────────────────────────────
# CORE SIGNAL PROCESSOR
# ─────────────────────────────────────────────

def process_signal(asset, timeframe, signal_fn, resolve_seconds):
    """
    Runs all filters then fires signal for one asset.
    Returns True if signal was logged, False otherwise.
    """
    # Filter 1: time of day (shared — already checked at loop level)

    df = fetch_candles(asset, timeframe, limit=300)
    if df is None:
        return False

    # Tag asset so TF_Confluence_1h4h can fetch higher-TF candles
    df.attrs['asset'] = asset

    # Filter 3: volatility spike
    if not is_volatility_normal(df):
        return False

    signal = signal_fn(df)
    if not signal:
        log.info(f"  [{timeframe}] {asset} | No signal")
        return False

    # Filter: disabled asset/direction combos (strategy had good subsets but bad overall)
    if (signal_fn.__name__, asset, signal) in DISABLED_COMBOS:
        log.info(f"  [{timeframe}] {asset} | {signal_fn.__name__} | {signal} — suppressed (disabled combo)")
        return False

    # Filter 4: BTC lead — if asset is not BTC, check BTC agrees
    if USE_BTC_LEAD and asset != "BTC/USDT":
        btc_signal = get_btc_signal(timeframe, signal_fn)
        if btc_signal and btc_signal != signal:
            log.info(f"  [{timeframe}] {asset} | Signal:{signal} blocked — BTC says {btc_signal}")
            return False
        elif btc_signal is None:
            log.info(f"  [{timeframe}] {asset} | BTC has no signal — proceeding anyway")

    price        = df['Close'].iloc[-1]
    now_ts       = time.time()
    resolve_ts   = ((int(now_ts // resolve_seconds)) * resolve_seconds) + resolve_seconds
    resolve_time = datetime.fromtimestamp(resolve_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    adx_val, adx_label = get_adx_regime(asset, timeframe)

    log.info(f"  [{timeframe}] {asset} | {signal_fn.__name__} | {signal} | "
             f"Price:{price:.4f} | Resolves:{resolve_time} | ADX:{adx_val} ({adx_label})")

    log_signal(asset, timeframe, signal_fn.__name__, signal, price, resolve_time,
               adx_val=adx_val, adx_label=adx_label)

    if not PAPER_MODE:
        place_polymarket_bet(asset, signal, timeframe)

    return True


# ─────────────────────────────────────────────
# PAPER TRADE LOGGER
# ─────────────────────────────────────────────

def log_signal(asset, timeframe, strategy, signal, price, resolve_time, poly_price=None, adx_val=0.0, adx_label=''):
    row = {
        'logged_at':      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        'asset':          asset,
        'timeframe':      timeframe,
        'strategy':       strategy,
        'signal':         signal,
        'price_entry':    round(price, 4),
        'poly_bet_price': round(poly_price, 4) if poly_price is not None else '',
        'poly_price_high': '',
        'poly_price_low':  '',
        'resolve_time':   resolve_time,
        'resolved_price': '',
        'correct':        '',
        'paper':          PAPER_MODE,
        'adx':            adx_val,
        'adx_regime':     adx_label,
    }
    with csv_lock:
        df = pd.DataFrame([row])
        write_header = not os.path.exists(PAPER_CSV)
        df.to_csv(PAPER_CSV, mode='a', header=write_header, index=False)


def update_resolutions():
    """Background thread — fills in resolved_price + correct once resolve_time passes."""
    while True:
        try:
            if os.path.exists(PAPER_CSV):
                with csv_lock:
                    df = pd.read_csv(PAPER_CSV, dtype=str, on_bad_lines='warn')

                now     = datetime.now(timezone.utc)
                updated = False

                for idx, row in df.iterrows():
                    if str(row['correct']) not in ('', 'nan', 'None'):
                        continue
                    if pd.isna(row['resolve_time']) or row['resolve_time'] == '':
                        continue
                    resolve_dt = pd.to_datetime(row['resolve_time'], utc=True)

                    # Still open — poll current Polymarket price and track high/low
                    if now < resolve_dt:
                        try:
                            cur = fetch_poly_price(row['asset'], row['signal'], row['timeframe'])
                            if cur is not None:
                                cur_high = row.get('poly_price_high', '')
                                cur_low  = row.get('poly_price_low', '')
                                new_high = cur if cur_high in ('', 'nan', 'None') else max(float(cur_high), cur)
                                new_low  = cur if cur_low  in ('', 'nan', 'None') else min(float(cur_low),  cur)
                                df.at[idx, 'poly_price_high'] = round(new_high, 4)
                                df.at[idx, 'poly_price_low']  = round(new_low,  4)
                                updated = True
                        except Exception:
                            pass
                        continue
                    try:
                        ex = EXCHANGE_OVERRIDE.get(row['asset'], exchange)
                        ticker   = ex.fetch_ticker(row['asset'])
                        resolved = float(ticker['last']) if ticker and ticker.get('last') else None
                        if not resolved:
                            log.warning(f"Resolution skip — no price for {row['asset']}")
                            continue
                        entry    = float(row['price_entry'])
                        actual   = "UP" if resolved > entry else "DOWN"
                        correct  = (actual == row['signal'])
                        df.at[idx, 'resolved_price'] = str(round(resolved, 4))
                        df.at[idx, 'correct']        = str(correct)
                        updated  = True
                        # Only count streak once per candle (asset+timeframe+resolve_time)
                        poly_str = ""
                        pbp = str(row.get('poly_bet_price', ''))
                        if pbp and pbp not in ('', 'nan', 'None'):
                            poly_str = f" | PolyBet:{pbp}"
                        log.info(f"RESOLVED | {row['asset']} {row['timeframe']} | "
                                 f"Signal:{row['signal']} Actual:{actual} | "
                                 f"{'CORRECT' if correct else 'WRONG'} | "
                                 f"Entry:{entry} Resolved:{resolved:.4f}{poly_str}")
                    except Exception as e:
                        log.warning(f"Resolution fetch failed: {e}")

                if updated:
                    with csv_lock:
                        df.to_csv(PAPER_CSV, index=False)
        except Exception as e:
            log.warning(f"Resolution updater error: {e}")

        time.sleep(300)


# ─────────────────────────────────────────────
# POLYMARKET — price fetch + live placeholder
# ─────────────────────────────────────────────

# Slug prefix per asset (used in 15m / 4h timestamp-based slugs)
_POLY_TICKER = {
    "BTC/USDT":  "btc",
    "ETH/USDT":  "eth",
    "SOL/USDT":  "sol",
    "BNB/USDT":  "bnb",
    "XRP/USDT":  "xrp",
    "DOGE/USDT": "doge",
}

# Full name per asset (used in 1h date-based slugs)
_POLY_FULLNAME = {
    "BTC/USDT":  "bitcoin",
    "ETH/USDT":  "ethereum",
    "SOL/USDT":  "solana",
    "BNB/USDT":  "bnb",
    "XRP/USDT":  "xrp",
    "DOGE/USDT": "dogecoin",
}

def _poly_slug(asset: str, timeframe: str) -> str | None:
    """Build the Polymarket market slug for the current window."""
    now = int(time.time())
    ticker   = _POLY_TICKER.get(asset)
    fullname = _POLY_FULLNAME.get(asset)
    if not ticker or not fullname:
        return None

    if timeframe == "15m":
        # e.g. btc-updown-15m-1776685500
        ts = (now // 900) * 900
        return f"{ticker}-updown-15m-{ts}"

    if timeframe == "4h":
        # e.g. btc-updown-4h-1776672000
        ts = (now // 14400) * 14400
        return f"{ticker}-updown-4h-{ts}"

    if timeframe == "1h":
        # e.g. bitcoin-up-or-down-april-20-2026-8am-et
        from datetime import datetime, timezone, timedelta
        et = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=-4)))  # EDT
        month = et.strftime("%B").lower()   # april
        day   = et.day                       # 20
        year  = et.year                      # 2026
        h     = et.hour
        ampm  = "am" if h < 12 else "pm"
        h12   = h if 1 <= h <= 12 else (12 if h == 0 else h - 12)
        return f"{fullname}-up-or-down-{month}-{day}-{year}-{h12}{ampm}-et"

    return None


def fetch_poly_price(asset: str, signal: str, timeframe: str = "15m") -> float | None:
    """
    Fetch the live Polymarket bet price for this asset/direction/timeframe.
    Returns the cost to bet on the signal direction (e.g. 0.52 = 52¢ per $1).
    Signal 'UP'  → outcomePrices[0]  (price of the UP outcome)
    Signal 'DOWN'→ outcomePrices[1]  (price of the DOWN outcome)
    Returns None if no market found or request fails.
    """
    slug = _poly_slug(asset, timeframe)
    if not slug:
        return None
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/markets?slug={slug}",
            timeout=5
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data:
            return None
        m      = data[0]
        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            import json as _json
            prices = _json.loads(prices)
        if len(prices) < 2:
            return None
        # prices[0] = UP outcome, prices[1] = DOWN outcome
        idx = 0 if signal == "UP" else 1
        result = round(float(prices[idx]), 4)
        log.debug(f"[POLY] {asset} {timeframe} {signal} → {result:.2f} | {m.get('question','')[:60]}")
        return result
    except Exception as e:
        log.debug(f"[POLY] Price fetch failed for {asset} {timeframe} {signal}: {e}")
        return None


def place_polymarket_bet(asset: str, signal: str, timeframe: str):
    log.info(f"[LIVE] Would bet {signal} on {asset} {timeframe} — not implemented yet")


# ─────────────────────────────────────────────
# SIGNAL LOOPS
# ─────────────────────────────────────────────

def _scan_assets(assets, tf, signals_registry, resolve_secs, offset_secs):
    """
    Shared scan loop — fetch df once per asset, run every signal in the registry,
    log each independently. Paper mode logs all; live mode places a bet per signal.
    """
    next_boundary = (int(time.time() // resolve_secs) + 1) * resolve_secs
    wait = next_boundary - time.time()
    log.info(f"[{tf}] Next candle in {wait:.0f}s")
    time.sleep(wait + offset_secs)

    log.info(f"[{tf}] Scanning {len(assets)} assets × {len(signals_registry)} strategies...")
    for asset in assets:
        df = fetch_candles(asset, tf, limit=300)
        if df is None:
            continue
        if not is_volatility_normal(df):
            continue

        price  = df['Close'].iloc[-1]
        fired  = []
        now_ts = time.time()
        resolve_ts   = ((int(now_ts // resolve_secs)) * resolve_secs) + resolve_secs
        resolve_time = datetime.fromtimestamp(resolve_ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        poly_price_cache = {}   # fetch once per asset per scan, reuse for all strategies

        adx_val, adx_label = get_adx_regime(asset, tf)

        for fn, name in signals_registry:
            try:
                sig = fn(df)
            except Exception as e:
                log.warning(f"  [{tf}] {asset} | {name} error: {e}")
                continue
            if sig:
                if sig not in poly_price_cache:
                    poly_price_cache[sig] = fetch_poly_price(asset, sig, timeframe=tf)
                poly_p = poly_price_cache[sig]
                log_signal(asset, tf, name, sig, price, resolve_time, poly_price=poly_p,
                           adx_val=adx_val, adx_label=adx_label)
                poly_str = f" | PolyBet:{poly_p:.2f}" if poly_p is not None else ""
                fired.append(f"{name}:{sig}{poly_str}")
                if not PAPER_MODE:
                    place_polymarket_bet(asset, sig, tf)

        if fired:
            log.info(f"  [{tf}] {asset} @ {price:.4f} | {len(fired)} signal(s): {', '.join(fired)}")
        else:
            log.info(f"  [{tf}] {asset} @ {price:.4f} | No signal")


def run_15min_signals():
    log.info("15min loop started")
    while True:
        try:
            _scan_assets(ASSETS_15M, '15m', SIGNALS_15M, 900, offset_secs=2)
        except Exception as e:
            log.error(f"[15m] Loop error: {e}")
            time.sleep(30)


def run_1hr_signals():
    log.info("1hr loop started")
    while True:
        try:
            if not is_safe_hour():
                time.sleep(60)
                continue
            _scan_assets(ASSETS_1H, '1h', SIGNALS_1H, 3600, offset_secs=7)
        except Exception as e:
            log.error(f"[1hr] Loop error: {e}")
            time.sleep(60)


def run_4hr_signals():
    log.info("4hr loop started")
    while True:
        try:
            if not is_safe_hour():
                time.sleep(60)
                continue
            _scan_assets(ASSETS_4H, '4h', SIGNALS_4H, 14400, offset_secs=13)
        except Exception as e:
            log.error(f"[4hr] Loop error: {e}")
            time.sleep(120)


def run_1d_signals():
    log.info("1d loop started")
    while True:
        try:
            _scan_assets(ASSETS_1D, '1d', SIGNALS_1D, 86400, offset_secs=20)
        except Exception as e:
            log.error(f"[1d] Loop error: {e}")
            time.sleep(300)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    mode = "PAPER" if PAPER_MODE else "LIVE"
    log.info("=" * 60)
    log.info(f"Crypto Signal Bot | MODE: {mode}")
    log.info(f"15m assets : {ASSETS_15M}")
    log.info(f"1h  assets : {ASSETS_1H}")
    log.info(f"4h  assets : {ASSETS_4H}")
    log.info(f"15m : BB_Session + AdaptiveBB + AdaptiveBB_US(13-20 UTC) | best: SOL 81.5% WR")
    log.info(f"1h  : AdaptiveBB + VWAP + Capitul_Session + Capitul_RSI40 + AdaptiveLong + AdaptiveSess + AdaptiveUS | best: ETH Test 91.7%")
    log.info(f"4h  : AdaptiveBB + AdaptiveATR20 + ConsensusVWAP + StretchScore | best: BTC ATR20 Test 76.5%")
    log.info(f"Filters: time-of-day | vol spike | BTC lead | loss streak")
    log.info(f"Skip hours (UTC): {sorted(SKIP_HOURS_UTC)}")
    log.info(f"ATR spike mult  : {ATR_SPIKE_MULT}x")
    log.info(f"BTC lead        : {USE_BTC_LEAD}")
    log.info(f"Paper CSV       : {PAPER_CSV}")
    log.info("=" * 60)

    threads = [
        threading.Thread(target=run_15min_signals,  daemon=True, name="15m-loop"),
        threading.Thread(target=run_1hr_signals,    daemon=True, name="1hr-loop"),
        threading.Thread(target=run_4hr_signals,    daemon=True, name="4hr-loop"),
        threading.Thread(target=run_1d_signals,     daemon=True, name="1d-loop"),
        threading.Thread(target=update_resolutions, daemon=True, name="resolver"),
    ]
    for t in threads:
        t.start()
        log.info(f"Thread started: {t.name}")

    try:
        while True:
            time.sleep(60)
            now = datetime.now()
            if now.minute == 0:
                if os.path.exists(PAPER_CSV):
                    df       = pd.read_csv(PAPER_CSV, on_bad_lines='warn')
                    total    = len(df)
                    resolved = df[df['correct'].astype(str).isin(['True','False'])]
                    if len(resolved) > 0:
                        wr = resolved['correct'].astype(str).eq('True').mean() * 100
                        log.info(f"[STATUS] Signals:{total} | Resolved:{len(resolved)} | "
                                 f"WinRate:{wr:.1f}%")
    except KeyboardInterrupt:
        log.info("Bot stopped.")


if __name__ == "__main__":
    main()
