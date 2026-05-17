"""
factory.py
==========
Tests all 35 strategies across crypto AND stocks.
Single cache — never re-runs completed tests.
Results split automatically by use case.

DATA SOURCES:
    Crypto : CCXT / Binance  — BTC/ETH/SOL/BNB/XRP/DOGE/HYPE — 5m / 15m / 1h / 4h / 1d
    Stocks : yfinance        — 21 US stocks/ETFs/forex — 15m / 1h / 4h / 1d
    (15m = 60d only; removed AAPL/TSLA/PLTR; added COIN/MSTR/IBIT/DIA/XLK/JPM/GLD/USO + 4 forex)

OUTPUT:
    Backtest Results/
        Polymarket/winners_polymarket.csv   WinRate >= 58%  (Polymarket bets)
        Trading/winners_trading.csv         Return > 0 + Sharpe > 0  (signal bots)
        Stocks/stocks_full_DATE.csv         raw stock results
        crypto_full_DATE.csv                raw crypto results
        master_all_results.csv              everything combined
        factory_cache.csv                   tested log (skip on re-run)

CACHE:
    Every completed (Source, Asset, Timeframe, Strategy) combo is logged.
    Re-run after adding new strategies or assets — only new tests run.

RUN:
    python factory.py
"""

import os, warnings, time
from datetime import datetime, timedelta
from itertools import combinations

import pandas as pd
import numpy as np
import ccxt
import yfinance as yf
from backtesting import Backtest, Strategy
from backtesting.lib import crossover

warnings.filterwarnings('ignore')


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

CRYPTO_ASSETS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT", "HYPE/USDT"]
CRYPTO_TFS    = {"1d":"1d", "4h":"4h", "1h":"1h", "15m":"15m", "5m":"5m"}

STOCK_ASSETS  = [
    # ── survivors from round 1 ─────────────────────────────────────────────
    "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AMD", "NFLX",
    "SPY", "QQQ", "IWM",
    # ── new: high-volatility crypto-adjacent + sector ETFs ────────────────
    "COIN", "MSTR", "IBIT",          # crypto-proxy stocks
    "DIA", "XLK",                    # Dow ETF + tech sector ETF
    "JPM", "GLD", "USO",             # bank, gold, oil
    # ── forex via yfinance (symbol format: EURUSD=X) ──────────────────────
    "EURUSD=X", "USDJPY=X", "GBPUSD=X", "AUDUSD=X",
    # ── futures (Lucid Trading challenge) ────────────────────────────────
    "ES=F", "NQ=F", "YM=F", "GC=F", "CL=F", "RTY=F",
]
STOCK_TFS     = ["15m", "1h", "4h", "1d"]   # 15m = 60d data; others = 730d

# Per-timeframe history depth — shorter TFs decay faster, use less history
TF_YEARS = {"5m": 0.25, "15m": 0.5, "1h": 1, "4h": 2, "1d": 4}
YEARS_DATA = 2   # fallback for stocks (1h/4h); 15m uses 60d from yfinance anyway
MIN_TRADES         = 20
MIN_BARS           = 200
MIN_SPAN_DAYS      = 200  # lowered to allow HYPE (282 days on Bybit)
WIN_POLYMARKET_WR   = 58.0   # WinRate >= 58% -> Polymarket folder
WIN_TRADING_ANN_RET = 30.0   # Annualised return >= 30%/yr -> Trading folder
WIN_TRADING_SHARPE  = 0.5    # AND Sharpe >= 0.5 (filters fluky one-off wins)

OUTPUT_DIR     = "D:/Desktop/Trading Folder/Backtest Results"
POLYMARKET_DIR = f"{OUTPUT_DIR}/Polymarket"
TRADING_DIR    = f"{OUTPUT_DIR}/Trading"
STOCKS_DIR     = f"{OUTPUT_DIR}/Stocks"
CACHE_FILE     = f"{OUTPUT_DIR}/factory_cache.csv"
MASTER_FILE    = f"{OUTPUT_DIR}/master_all_results.csv"
DATE_STR       = datetime.now().strftime("%Y-%m-%d_%H-%M")

for d in [OUTPUT_DIR, POLYMARKET_DIR, TRADING_DIR, STOCKS_DIR]:
    os.makedirs(d, exist_ok=True)


# ─────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────

def load_cache() -> set:
    if not os.path.exists(CACHE_FILE):
        return set()
    try:
        return set(pd.read_csv(CACHE_FILE)["key"].tolist())
    except Exception:
        return set()

def save_to_cache(source: str, asset: str, tf: str, strategy: str, status: str):
    key = f"{source}|{asset}|{tf}|{strategy}"
    row = pd.DataFrame([{
        "key": key, "source": source, "asset": asset,
        "timeframe": tf, "strategy": strategy, "status": status,
        "tested_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }])
    write_header = not os.path.exists(CACHE_FILE)
    row.to_csv(CACHE_FILE, mode="a", header=write_header, index=False)


# ─────────────────────────────────────────────
# DATA — CRYPTO (CCXT)
# ─────────────────────────────────────────────

_crypto_cache: dict = {}

def fetch_crypto(symbol: str, timeframe: str) -> pd.DataFrame | None:
    key = f"{symbol}_{timeframe}"
    if key in _crypto_cache:
        return _crypto_cache[key]
    try:
        # HYPE not on Binance — use Bybit instead, fetch all available history
        if symbol == 'HYPE/USDT':
            exchange = ccxt.bybit({'enableRateLimit': True})
            since_ms = exchange.parse8601('2020-01-01T00:00:00Z')  # fetch all available
        else:
            exchange = ccxt.binance({'enableRateLimit': True})
            years = TF_YEARS.get(timeframe, YEARS_DATA)
            days  = int(365 * years)
            since_ms = exchange.parse8601(
                (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')
            )
        all_ohlcv = []
        while True:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
            if not batch: break
            all_ohlcv.extend(batch)
            if len(batch) < 1000: break
            since_ms = batch[-1][0] + 1
            time.sleep(0.3)
        if not all_ohlcv:
            _crypto_cache[key] = None
            return None
        df = pd.DataFrame(all_ohlcv, columns=['Date','Open','High','Low','Close','Volume'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df = df.drop_duplicates('Date').set_index('Date').astype(float)
        result = df if len(df) >= MIN_BARS else None
        _crypto_cache[key] = result
        return result
    except Exception as e:
        print(f"    [WARN] crypto fetch failed {symbol} {timeframe}: {e}")
        _crypto_cache[key] = None
        return None


# ─────────────────────────────────────────────
# DATA — STOCKS (yfinance)
# ─────────────────────────────────────────────

_stock_cache: dict = {}

def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def fetch_stock(ticker: str, interval: str) -> pd.DataFrame | None:
    key = f"{ticker}_{interval}"
    if key in _stock_cache:
        return _stock_cache[key]
    try:
        if interval == "15m":
            # yfinance max for 15m = 60 days
            raw = yf.download(ticker, period="60d", interval="15m",
                              auto_adjust=True, progress=False)
            if raw.empty:
                _stock_cache[key] = None
                return None
            df = _flatten(raw)[["Open","High","Low","Close","Volume"]].copy()
        elif interval == "4h":
            raw = yf.download(ticker, period=f"{int(365*TF_YEARS.get('4h',2))}d", interval="1h",
                              auto_adjust=True, progress=False)
            if raw.empty:
                _stock_cache[key] = None
                return None
            raw = _flatten(raw)[["Open","High","Low","Close","Volume"]]
            raw.index = pd.to_datetime(raw.index)
            df = raw.resample("4h").agg({
                "Open":"first","High":"max","Low":"min",
                "Close":"last","Volume":"sum"
            }).dropna()
        elif interval == "1h":
            raw = yf.download(ticker, period=f"{int(365*TF_YEARS.get('1h',1))}d", interval="1h",
                              auto_adjust=True, progress=False)
            if raw.empty:
                _stock_cache[key] = None
                return None
            df = _flatten(raw)[["Open","High","Low","Close","Volume"]].copy()
        else:  # 1d
            raw = yf.download(ticker, period="max", interval="1d",
                              auto_adjust=True, progress=False)
            if raw.empty:
                _stock_cache[key] = None
                return None
            df = _flatten(raw)[["Open","High","Low","Close","Volume"]].copy()

        df.index = pd.to_datetime(df.index)
        df = df.dropna()

        # For 15m only 60d available — skip the YEARS_DATA cutoff
        if interval != "15m":
            cutoff = pd.Timestamp.now(tz=df.index.tz) - pd.DateOffset(years=YEARS_DATA)
            df = df[df.index >= cutoff]

        # Forex pairs have zero volume — fill with 1 so strategies don't break
        if ticker.endswith("=X"):
            df["Volume"] = df["Volume"].replace(0, 1).fillna(1)

        # 15m needs fewer bars (60d = ~1500 bars for 15m during market hours)
        min_b = 100 if interval == "15m" else MIN_BARS
        result = df if len(df) >= min_b else None
        _stock_cache[key] = result
        return result
    except Exception as e:
        print(f"    [WARN] stock fetch failed {ticker} {interval}: {e}")
        _stock_cache[key] = None
        return None


# ─────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────

def sma(series, n):
    return pd.Series(series).rolling(n).mean().values

def ema(series, n):
    return pd.Series(series).ewm(span=n, adjust=False).mean().values

def dema(series, n):
    """Double EMA — more responsive than regular EMA, less lag."""
    s = pd.Series(series)
    e1 = s.ewm(span=n, adjust=False).mean()
    return (2 * e1 - e1.ewm(span=n, adjust=False).mean()).values

def parabolic_sar(high, low, af_start=0.02, af_step=0.02, af_max=0.2):
    """Parabolic SAR — returns (sar_values, trend_direction: 1=up/-1=down)."""
    h, l = np.array(high), np.array(low)
    n = len(h)
    sar = np.full(n, np.nan)
    trend = np.ones(n)
    ep = np.zeros(n)
    af = np.zeros(n)
    sar[0] = l[0]; ep[0] = h[0]; af[0] = af_start; trend[0] = 1
    for i in range(1, n):
        ps, pe, pa, pt = sar[i-1], ep[i-1], af[i-1], trend[i-1]
        ns = ps + pa * (pe - ps)
        if pt == 1:
            ns = min(ns, l[i-1], l[max(0,i-2)])
            if l[i] < ns:
                trend[i]=-1; sar[i]=pe; ep[i]=l[i]; af[i]=af_start
            else:
                trend[i]=1; sar[i]=ns
                ep[i]=h[i] if h[i]>pe else pe
                af[i]=min(pa+af_step,af_max) if h[i]>pe else pa
        else:
            ns = max(ns, h[i-1], h[max(0,i-2)])
            if h[i] > ns:
                trend[i]=1; sar[i]=pe; ep[i]=h[i]; af[i]=af_start
            else:
                trend[i]=-1; sar[i]=ns
                ep[i]=l[i] if l[i]<pe else pe
                af[i]=min(pa+af_step,af_max) if l[i]<pe else pa
    return sar, trend

def alligator_lines(high, low):
    """Williams Alligator: Jaw(13+8), Teeth(8+5), Lips(5+3) as smoothed shifted MAs."""
    hl2 = (pd.Series(high) + pd.Series(low)) / 2
    def smma(s, n):
        r = np.full(len(s), np.nan)
        idx = n - 1
        if idx >= len(s): return r
        r[idx] = s.iloc[:n].mean()
        for i in range(idx+1, len(s)):
            r[i] = (r[i-1] * (n-1) + s.iloc[i]) / n
        return r
    jaw   = smma(hl2, 13)   # blue — shift 8
    teeth = smma(hl2, 8)    # red  — shift 5
    lips  = smma(hl2, 5)    # green — shift 3
    return lips, teeth, jaw

def rsi(series, n=14):
    s = pd.Series(series); d = s.diff()
    g = d.clip(lower=0).rolling(n).mean()
    l = (-d.clip(upper=0)).rolling(n).mean()
    return (100 - 100 / (1 + g / l.replace(0, np.nan))).values

def macd_calc(series, fast=12, slow=26, sig=9):
    s = pd.Series(series)
    ml = s.ewm(span=fast).mean() - s.ewm(span=slow).mean()
    return ml.values, ml.ewm(span=sig).mean().values

def atr_calc(high, low, close, n=14):
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean().values

def bollinger(series, n=20, k=2):
    s = pd.Series(series); mid = s.rolling(n).mean(); std = s.rolling(n).std()
    return mid.values, (mid+k*std).values, (mid-k*std).values

def keltner(high, low, close, n=20, mult=1.5):
    mid = pd.Series(close).ewm(span=n).mean()
    a = pd.Series(atr_calc(high, low, close, n))
    return mid.values, (mid+mult*a).values, (mid-mult*a).values

def donchian(high, low, n=20):
    return pd.Series(high).rolling(n).max().values, pd.Series(low).rolling(n).min().values

def stoch_rsi(series, rsi_n=14, stoch_n=14, smooth_k=3, smooth_d=3):
    r = pd.Series(rsi(series, rsi_n))
    k = 100*(r - r.rolling(stoch_n).min()) / (r.rolling(stoch_n).max() - r.rolling(stoch_n).min() + 1e-10)
    k_s = k.rolling(smooth_k).mean()
    return k_s.values, k_s.rolling(smooth_d).mean().values

def adx_calc(high, low, close, n=14):
    h, l, c = np.array(high), np.array(low), np.array(close)
    plus_dm  = np.where((h[1:]-h[:-1])>(l[:-1]-l[1:]), np.maximum(h[1:]-h[:-1],0), 0)
    minus_dm = np.where((l[:-1]-l[1:])>(h[1:]-h[:-1]), np.maximum(l[:-1]-l[1:],0), 0)
    tr_arr   = np.maximum(h[1:]-l[1:], np.maximum(abs(h[1:]-c[:-1]), abs(l[1:]-c[:-1])))
    def wilder(arr, n):
        out = np.full(len(arr), np.nan)
        if len(arr) >= n:
            out[n-1] = arr[:n].sum()
            for i in range(n, len(arr)): out[i] = out[i-1] - out[i-1]/n + arr[i]
        return out
    tr_s=wilder(tr_arr,n); pdm_s=wilder(plus_dm,n); mdm_s=wilder(minus_dm,n)
    pdi=100*pdm_s/(tr_s+1e-10); mdi=100*mdm_s/(tr_s+1e-10)
    dx=100*abs(pdi-mdi)/(pdi+mdi+1e-10)
    pad=np.array([np.nan])
    return np.concatenate([pad,wilder(np.nan_to_num(dx),n)]), np.concatenate([pad,pdi]), np.concatenate([pad,mdi])

def cci_calc(high, low, close, n=20):
    tp=(pd.Series(high)+pd.Series(low)+pd.Series(close))/3; ma=tp.rolling(n).mean()
    md=tp.rolling(n).apply(lambda x: np.mean(np.abs(x-x.mean())), raw=True)
    return ((tp-ma)/(0.015*md)).values

def williams_r(high, low, close, n=14):
    h=pd.Series(high).rolling(n).max(); l=pd.Series(low).rolling(n).min()
    return (-100*(h-pd.Series(close))/(h-l+1e-10)).values

def roc(series, n=12):
    s=pd.Series(series); return ((s-s.shift(n))/s.shift(n)*100).values

def obv_calc(close, volume):
    c=np.array(close); v=np.array(volume); obv=np.zeros(len(c))
    for i in range(1,len(c)):
        obv[i]=obv[i-1]+(v[i] if c[i]>c[i-1] else -v[i] if c[i]<c[i-1] else 0)
    return obv

def heikin_ashi_trend(open_, high, low, close):
    o,h,l,c=np.array(open_),np.array(high),np.array(low),np.array(close)
    ha_c=(o+h+l+c)/4; ha_o=np.zeros(len(c)); ha_o[0]=(o[0]+c[0])/2
    for i in range(1,len(c)): ha_o[i]=(ha_o[i-1]+ha_c[i-1])/2
    return np.where(ha_c>ha_o,1,-1).astype(float)

def supertrend(high, low, close, n=10, mult=3):
    atr_v=atr_calc(high,low,close,n); h,l,c=np.array(high),np.array(low),np.array(close)
    hl2=(h+l)/2; upper=hl2+mult*atr_v; lower=hl2-mult*atr_v
    direction=np.zeros(len(c)); direction[0]=1
    for i in range(1,len(c)):
        if np.isnan(atr_v[i]): direction[i]=direction[i-1]
        elif c[i]>upper[i-1]: direction[i]=1
        elif c[i]<lower[i-1]: direction[i]=-1
        else: direction[i]=direction[i-1]
    return direction

def ichimoku_cloud(high, low):
    h,l=pd.Series(high),pd.Series(low)
    t=(h.rolling(9).max()+l.rolling(9).min())/2; k=(h.rolling(26).max()+l.rolling(26).min())/2
    return ((t+k)/2).shift(1).values, ((h.rolling(52).max()+l.rolling(52).min())/2).shift(1).values

def zscore(series, n=20):
    s=pd.Series(series)
    return ((s-s.rolling(n).mean())/(s.rolling(n).std()+1e-10)).values

def bb_pct_b(series, n=20, k=2):
    s=pd.Series(series); mid=s.rolling(n).mean(); std=s.rolling(n).std()
    return ((s-(mid-k*std))/((mid+k*std)-(mid-k*std)+1e-10)).values

def stochastic(high, low, close, k_n=14, d_n=3):
    h=pd.Series(high).rolling(k_n).max(); l=pd.Series(low).rolling(k_n).min()
    k=100*(pd.Series(close)-l)/(h-l+1e-10)
    return k.values, k.rolling(d_n).mean().values

def vwap_rolling(close, volume, n=20):
    c=pd.Series(close); v=pd.Series(volume)
    return ((c*v).rolling(n).sum()/(v.rolling(n).sum()+1e-10)).values

def connors_rsi(close, rsi_n=3, streak_n=2, rank_n=100):
    c=pd.Series(close); rsi3=pd.Series(rsi(close,rsi_n))
    streak=np.zeros(len(c))
    for i in range(1,len(c)):
        if c.iloc[i]>c.iloc[i-1]: streak[i]=streak[i-1]+1 if streak[i-1]>0 else 1
        elif c.iloc[i]<c.iloc[i-1]: streak[i]=streak[i-1]-1 if streak[i-1]<0 else -1
    ret=c.pct_change()
    pct_rank=ret.rolling(rank_n).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1]*100, raw=False)
    return ((rsi3+pd.Series(rsi(streak,streak_n))+pct_rank)/3).values


# ─────────────────────────────────────────────
# STRATEGIES (35)
# ─────────────────────────────────────────────

class GoldenCross(Strategy):
    def init(self): self.f=self.I(sma,self.data.Close,50); self.s=self.I(sma,self.data.Close,200)
    def next(self):
        if crossover(self.f,self.s) and not self.position.is_long: self.position.close(); self.buy()
        elif crossover(self.s,self.f) and not self.position.is_short: self.position.close(); self.sell()

class EMAGoldenCross(Strategy):
    def init(self): self.f=self.I(ema,self.data.Close,50); self.s=self.I(ema,self.data.Close,200)
    def next(self):
        if crossover(self.f,self.s) and not self.position.is_long: self.position.close(); self.buy()
        elif crossover(self.s,self.f) and not self.position.is_short: self.position.close(); self.sell()

class TripleEMATrend(Strategy):
    def init(self):
        self.e1=self.I(ema,self.data.Close,8); self.e2=self.I(ema,self.data.Close,21)
        self.e3=self.I(ema,self.data.Close,55)
    def next(self):
        p=self.data.Close[-1]
        if p>self.e1[-1]>self.e2[-1]>self.e3[-1] and not self.position.is_long: self.position.close(); self.buy()
        elif p<self.e1[-1]<self.e2[-1]<self.e3[-1] and not self.position.is_short: self.position.close(); self.sell()

class RSI2MeanReversion(Strategy):
    def init(self): self.r=self.I(rsi,self.data.Close,2); self.m=self.I(sma,self.data.Close,200)
    def next(self):
        if self.data.Close[-1]>self.m[-1] and self.r[-1]<10 and not self.position.is_long:
            self.buy(tp=self.data.Close[-1]*1.03, sl=self.data.Close[-1]*0.97)
        elif self.r[-1]>90 and self.position.is_long: self.position.close()

class RSIMATrend(Strategy):
    def init(self):
        self.r=self.I(rsi,self.data.Close,14); self.f=self.I(sma,self.data.Close,50)
        self.s=self.I(sma,self.data.Close,200)
    def next(self):
        if self.f[-1]>self.s[-1] and self.r[-1]>50 and not self.position.is_long: self.position.close(); self.buy()
        elif self.f[-1]<self.s[-1] and self.r[-1]<50 and not self.position.is_short: self.position.close(); self.sell()

class RSIDivergenceSimple(Strategy):
    def init(self): self.r=self.I(rsi,self.data.Close,14); self.m=self.I(ema,self.data.Close,200)
    def next(self):
        if self.data.Close[-1]>self.m[-1] and self.r[-2]<35 and self.r[-1]>self.r[-2] and not self.position.is_long:
            self.position.close(); self.buy(sl=self.data.Close[-1]*0.96,tp=self.data.Close[-1]*1.08)
        elif self.data.Close[-1]<self.m[-1] and self.r[-2]>65 and self.r[-1]<self.r[-2] and not self.position.is_short:
            self.position.close(); self.sell(sl=self.data.Close[-1]*1.04,tp=self.data.Close[-1]*0.92)

class MACDCrossover(Strategy):
    def init(self): self.ml,self.sl=self.I(macd_calc,self.data.Close)
    def next(self):
        if crossover(self.ml,self.sl) and self.ml[-1]>0 and not self.position.is_long: self.position.close(); self.buy()
        elif crossover(self.sl,self.ml) and self.ml[-1]<0 and not self.position.is_short: self.position.close(); self.sell()

class MACD200EMA(Strategy):
    def init(self): self.ml,self.sl=self.I(macd_calc,self.data.Close); self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        above=self.data.Close[-1]>self.e200[-1]
        if crossover(self.ml,self.sl) and above and not self.position.is_long: self.position.close(); self.buy()
        elif crossover(self.sl,self.ml) and not above and not self.position.is_short: self.position.close(); self.sell()

class BollingerReversion(Strategy):
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class MeanReversionBB(Strategy):
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        p=self.data.Close[-1]
        if np.isnan(self.dn[-1]): return
        if p>self.e200[-1] and p<self.dn[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class SqueezeMomentum(Strategy):
    def init(self):
        self.b_mid,self.b_up,self.b_dn=self.I(bollinger,self.data.Close,20,2)
        self.k_mid,self.k_up,self.k_dn=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,1.5)
        self.mom=self.I(lambda c: pd.Series(c).diff(12).values,self.data.Close)
    def next(self):
        if np.isnan(self.b_up[-1]) or np.isnan(self.k_up[-1]): return
        was_sq=self.b_up[-2]<self.k_up[-2] and self.b_dn[-2]>self.k_dn[-2]
        now_out=self.b_up[-1]>self.k_up[-1] or self.b_dn[-1]<self.k_dn[-1]
        if was_sq and now_out:
            if self.mom[-1]>0 and not self.position.is_long: self.position.close(); self.buy()
            elif self.mom[-1]<0 and not self.position.is_short: self.position.close(); self.sell()

class DonchianBreakout(Strategy):
    def init(self): self.up,self.dn=self.I(donchian,self.data.High,self.data.Low,20)
    def next(self):
        if np.isnan(self.up[-2]): return
        if self.data.Close[-1]>self.up[-2] and not self.position.is_long: self.buy()
        elif self.data.Close[-1]<self.dn[-2] and self.position.is_long: self.position.close()

class SupertrendSingle(Strategy):
    def init(self): self.st=self.I(supertrend,self.data.High,self.data.Low,self.data.Close,10,3)
    def next(self):
        if self.st[-1]==1 and not self.position.is_long: self.position.close(); self.buy()
        elif self.st[-1]==-1 and not self.position.is_short: self.position.close(); self.sell()

class SupertrendTriple(Strategy):
    def init(self):
        self.s1=self.I(supertrend,self.data.High,self.data.Low,self.data.Close,3,12)
        self.s2=self.I(supertrend,self.data.High,self.data.Low,self.data.Close,1,10)
        self.s3=self.I(supertrend,self.data.High,self.data.Low,self.data.Close,2,11)
    def next(self):
        if self.s1[-1]==1 and self.s2[-1]==1 and self.s3[-1]==1 and not self.position.is_long:
            self.position.close(); self.buy()
        elif self.s1[-1]==-1 and self.s2[-1]==-1 and self.s3[-1]==-1 and not self.position.is_short:
            self.position.close(); self.sell()

class IchimokuKumo(Strategy):
    def init(self): self.l1,self.l2=self.I(ichimoku_cloud,self.data.High,self.data.Low)
    def next(self):
        p=self.data.Close[-1]
        if np.isnan(self.l1[-1]): return
        top=max(self.l1[-1],self.l2[-1]); bot=min(self.l1[-1],self.l2[-1])
        if p>top and not self.position.is_long: self.position.close(); self.buy()
        elif p<bot and not self.position.is_short: self.position.close(); self.sell()

class ADXTrend(Strategy):
    def init(self): self.adx,self.pdi,self.mdi=self.I(adx_calc,self.data.High,self.data.Low,self.data.Close,14)
    def next(self):
        if np.isnan(self.adx[-1]): return
        if self.adx[-1]>25 and crossover(self.pdi,self.mdi) and not self.position.is_long:
            self.position.close(); self.buy()
        elif self.adx[-1]>25 and crossover(self.mdi,self.pdi) and not self.position.is_short:
            self.position.close(); self.sell()

class StochRSIStrategy(Strategy):
    def init(self): self.k,self.d=self.I(stoch_rsi,self.data.Close)
    def next(self):
        if np.isnan(self.k[-1]): return
        if crossover(self.k,self.d) and self.k[-1]<30 and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.06)
        elif crossover(self.d,self.k) and self.k[-1]>70 and self.position.is_long: self.position.close()

class WilliamsRStrategy(Strategy):
    def init(self): self.wr=self.I(williams_r,self.data.High,self.data.Low,self.data.Close,14); self.e=self.I(ema,self.data.Close,200)
    def next(self):
        if self.data.Close[-1]>self.e[-1] and self.wr[-1]<-80 and self.wr[-2]<-80 and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.05)
        elif self.wr[-1]>-20 and self.position.is_long: self.position.close()

class CCIStrategy(Strategy):
    def init(self): self.c=self.I(cci_calc,self.data.High,self.data.Low,self.data.Close,20)
    def next(self):
        if np.isnan(self.c[-1]): return
        if self.c[-2]<100 and self.c[-1]>100 and not self.position.is_long: self.position.close(); self.buy()
        elif self.c[-2]>-100 and self.c[-1]<-100 and not self.position.is_short: self.position.close(); self.sell()

class ROCMomentum(Strategy):
    def init(self): self.r=self.I(roc,self.data.Close,12)
    def next(self):
        if np.isnan(self.r[-1]): return
        if crossover(self.r,np.zeros(len(self.r))) and not self.position.is_long: self.position.close(); self.buy()
        elif crossover(np.zeros(len(self.r)),self.r) and not self.position.is_short: self.position.close(); self.sell()

class OBVTrend(Strategy):
    def init(self):
        self.obv=self.I(obv_calc,self.data.Close,self.data.Volume)
        self.sig=self.I(ema,obv_calc(self.data.Close,self.data.Volume),20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        price_above=self.data.Close[-1]>self.e50[-1]
        if self.obv[-1]>self.sig[-1] and price_above and not self.position.is_long: self.position.close(); self.buy()
        elif self.obv[-1]<self.sig[-1] and not self.position.is_short: self.position.close(); self.sell()

class HeikinAshiTrend(Strategy):
    def init(self): self.hat=self.I(heikin_ashi_trend,self.data.Open,self.data.High,self.data.Low,self.data.Close)
    def next(self):
        if self.hat[-1]==1 and self.hat[-2]==1 and not self.position.is_long: self.position.close(); self.buy()
        elif self.hat[-1]==-1 and self.hat[-2]==-1 and not self.position.is_short: self.position.close(); self.sell()

class EMA50Pullback(Strategy):
    def init(self): self.e=self.I(ema,self.data.Close,50); self.r=self.I(rsi,self.data.Close,14)
    def next(self):
        if self.data.Low[-1]<=self.e[-1]*1.005 and self.data.Close[-1]>self.e[-1] and self.r[-1]>40 and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.06)

class VWAPMeanReversion(Strategy):
    def init(self):
        tp=(self.data.High+self.data.Low+self.data.Close)/3; vol=self.data.Volume
        vwap=pd.Series(tp*vol).cumsum()/pd.Series(vol).cumsum()
        self.vwap=self.I(lambda: vwap.values); self.r=self.I(rsi,self.data.Close,14)
    def next(self):
        p=self.data.Close[-1]; v=self.vwap[-1]
        if np.isnan(v): return
        if p<v*0.99 and self.r[-1]<40 and not self.position.is_long: self.buy(tp=v,sl=p*0.97)
        elif p>v*1.01 and self.position.is_long: self.position.close()

class KeltnerBreakout(Strategy):
    def init(self): self.km,self.ku,self.kd=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,2.0)
    def next(self):
        p=self.data.Close[-1]
        if np.isnan(self.ku[-1]): return
        if p>self.ku[-1] and not self.position.is_long: self.position.close(); self.buy()
        elif p<self.kd[-1] and not self.position.is_short: self.position.close(); self.sell()

class EMACloud(Strategy):
    def init(self):
        self.e1=self.I(ema,self.data.Close,8); self.e2=self.I(ema,self.data.Close,13)
        self.e3=self.I(ema,self.data.Close,21); self.e4=self.I(ema,self.data.Close,55)
    def next(self):
        if self.e1[-1]>self.e2[-1]>self.e3[-1]>self.e4[-1] and not self.position.is_long: self.position.close(); self.buy()
        elif self.e1[-1]<self.e2[-1]<self.e3[-1]<self.e4[-1] and not self.position.is_short: self.position.close(); self.sell()

class ChandelierExit(Strategy):
    atr_n=22; atr_mult=3.0
    def init(self): self.atr_v=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,self.atr_n)
    def next(self):
        if np.isnan(self.atr_v[-1]): return
        h_max=max(self.data.High[-self.atr_n:]); l_min=min(self.data.Low[-self.atr_n:])
        ce_long=h_max-self.atr_mult*self.atr_v[-1]; ce_short=l_min+self.atr_mult*self.atr_v[-1]
        p=self.data.Close[-1]
        if p>ce_short and not self.position.is_long: self.position.close(); self.buy()
        elif p<ce_long and not self.position.is_short: self.position.close(); self.sell()

class PriceMomentum(Strategy):
    n=20
    def init(self):
        self.mom=self.I(lambda c: np.array(c)-np.concatenate([np.full(self.n,np.nan),np.array(c)[:-self.n]]),self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.mom[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        if above and self.mom[-1]>0 and not self.position.is_long: self.position.close(); self.buy()
        elif not above and self.mom[-1]<0 and not self.position.is_short: self.position.close(); self.sell()

class HigherHighStrategy(Strategy):
    n=10
    def init(self): pass
    def next(self):
        if len(self.data.High)<self.n*3: return
        h=list(self.data.High); l=list(self.data.Low)
        if h[-1]>h[-self.n]>h[-self.n*2] and not self.position.is_long: self.position.close(); self.buy()
        elif l[-1]<l[-self.n]<l[-self.n*2] and not self.position.is_short: self.position.close(); self.sell()

class VolumeSurge(Strategy):
    def init(self):
        self.vol_avg=self.I(sma,self.data.Volume,20)
        self.hi20=self.I(lambda h: pd.Series(h).rolling(20).max().values,self.data.High)
    def next(self):
        if np.isnan(self.vol_avg[-1]): return
        if self.data.Volume[-1]>self.vol_avg[-1]*2 and self.data.Close[-1]>self.hi20[-2] and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.06)
        elif self.position.is_long and self.data.Close[-1]<self.hi20[-2]: self.position.close()

class ConnorsRSIStrategy(Strategy):
    def init(self): self.crsi=self.I(connors_rsi,self.data.Close); self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.crsi[-1]) or np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        if self.crsi[-1]<10 and above and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.04)
        elif self.crsi[-1]>90 and not above and not self.position.is_short:
            self.sell(sl=self.data.Close[-1]*1.03,tp=self.data.Close[-1]*0.96)
        elif self.position.is_long and self.crsi[-1]>50: self.position.close()
        elif self.position.is_short and self.crsi[-1]<50: self.position.close()

class ZScoreReversion(Strategy):
    def init(self): self.z=self.I(zscore,self.data.Close,20); self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.z[-1]) or np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        if self.z[-1]<-2 and above and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.04)
        elif self.z[-1]>2 and not above and not self.position.is_short:
            self.sell(sl=self.data.Close[-1]*1.03,tp=self.data.Close[-1]*0.96)
        elif self.position.is_long and self.z[-1]>=0: self.position.close()
        elif self.position.is_short and self.z[-1]<=0: self.position.close()

class BBPercentB(Strategy):
    def init(self): self.pctb=self.I(bb_pct_b,self.data.Close,20,2); self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.pctb[-1]) or np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        if self.pctb[-1]<0 and above and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.04)
        elif self.pctb[-1]>1 and not above and not self.position.is_short:
            self.sell(sl=self.data.Close[-1]*1.03,tp=self.data.Close[-1]*0.96)
        elif self.position.is_long and self.pctb[-1]>=0.5: self.position.close()
        elif self.position.is_short and self.pctb[-1]<=0.5: self.position.close()

class StochasticCrossover(Strategy):
    def init(self):
        self.k,self.d=self.I(stochastic,self.data.High,self.data.Low,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.k[-1]) or np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        if crossover(self.k,self.d) and self.k[-1]<30 and above and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.05)
        elif crossover(self.d,self.k) and self.k[-1]>70 and not above and not self.position.is_short:
            self.sell(sl=self.data.Close[-1]*1.03,tp=self.data.Close[-1]*0.95)
        elif self.position.is_long and self.k[-1]>80: self.position.close()
        elif self.position.is_short and self.k[-1]<20: self.position.close()

class StretchScore(Strategy):
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.vol_ma=self.I(sma,self.data.Volume,10)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e200[-1]): return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        vol_fading=self.data.Volume[-1]<self.vol_ma[-1]
        if std_dn>=0.85 and vol_fading and self.rsi_v[-1]<35 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.85 and vol_fading and self.rsi_v[-1]>65 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class StretchScoreFast(Strategy):
    """StretchScore tuned for 15m/1h — no volume filter (too noisy), EMA50 trend,
    wider RSI thresholds (30/70), slightly relaxed BB stretch (0.80)."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        if std_dn>=0.80 and self.rsi_v[-1]<30 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.80 and self.rsi_v[-1]>70 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BollingerReversionTrend(Strategy):
    """Bollinger Reversion with EMA200 trend filter — only longs in uptrend,
    only shorts in downtrend. Fewer signals but cleaner WR."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e200[-1]): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and p>self.e200[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and p<self.e200[-1] and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BollingerRSIConfirm(Strategy):
    """BB touch + RSI confirmation. Price at lower band AND RSI oversold = stronger
    reversal signal. Best combo for high-frequency 15m mean reversion."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.rsi_v[-1]): return
        p=self.data.Close[-1]
        if p<=self.dn[-1] and self.rsi_v[-1]<35 and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>=self.up[-1] and self.rsi_v[-1]>65 and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class StretchScoreNoTrend(Strategy):
    """StretchScore without EMA trend filter — pure BB stretch + RSI exhaustion
    + volume fade. No trend bias, fires both directions equally. More signals on 15m."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.vol_ma=self.I(sma,self.data.Volume,10)
    def next(self):
        if np.isnan(self.up[-1]): return
        p=self.data.Close[-1]
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        vol_fading=self.data.Volume[-1]<self.vol_ma[-1]
        if std_dn>=0.85 and vol_fading and self.rsi_v[-1]<35 and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.85 and vol_fading and self.rsi_v[-1]>65 and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

# ── CRACK 70% WR ON 15m ───────────────────────────────────────────────────────

class BollingerReversionRSILong(Strategy):
    """BB lower band + RSI < 35 + EMA50 trend, LONG ONLY.
    Fixes BollingerRSI_Confirm — removes the short side that dragged WR down in bull market."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]
        if p<=self.dn[-1] and self.rsi_v[-1]<35 and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BollingerReversionWide(Strategy):
    """BB at 2.5 std — only the most extreme band touches.
    Fewer signals but each one is a more genuine overextension."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2.5)
    def next(self):
        if np.isnan(self.dn[-1]): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class BollingerReversionSession(Strategy):
    """BB Reversion 8:00-20:00 UTC only — cuts Asian session noise.
    0-8 UTC is thin market with random BB touches that don't follow through."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        p=self.data.Close[-1]
        if not (8<=hour<20): return
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class StretchScoreFastStrict(Strategy):
    """StretchScore_Fast with tighter thresholds: BB 2.5 std + RSI 25/75.
    Targeting 70%+ WR — extreme overextension only."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2.5)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        if std_dn>=0.80 and self.rsi_v[-1]<25 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.80 and self.rsi_v[-1]>75 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BollingerReversionDoubleClose(Strategy):
    """2 consecutive closes below lower BB before entry.
    Double confirmation = price genuinely overextended, not just a wick."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.dn[-2]): return
        p=self.data.Close[-1]
        if (self.data.Close[-2]<self.dn[-2] and self.data.Close[-1]<self.dn[-1]
                and not self.position.is_long):
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BollingerReversionRejection(Strategy):
    """Wick touched lower BB but candle CLOSED above it = rejection / hammer.
    Price showed strength by reclaiming the band — strongest single-candle reversal signal."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        p=self.data.Close[-1]
        if (self.data.Low[-1]<=self.dn[-1] and self.data.Close[-1]>self.dn[-1]
                and not self.position.is_long):
            self.buy(tp=self.mid[-1],sl=self.data.Low[-1]*0.99)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BollingerReversionStochRSI(Strategy):
    """BB touch + StochRSI < 10 (extreme oversold — more selective than RSI 35).
    Only fires when price AND momentum are both at extremes."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.k,self.d=self.I(stoch_rsi,self.data.Close)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.k[-1]): return
        p=self.data.Close[-1]
        if p<=self.dn[-1] and self.k[-1]<10 and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>=self.up[-1] and self.k[-1]>90 and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BollingerReversionWideSession(Strategy):
    """Combination: BB 2.5 std + 8-20 UTC only + long only.
    Stack the two best filters together — most selective, targeting highest WR."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2.5)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BollingerSessionRSILong(Strategy):
    """Session filter + RSI < 35 + long only.
    Stacks the two strongest individual improvements — targeting 75%+ WR."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and self.rsi_v[-1]<35 and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BollingerUSSession(Strategy):
    """13:00-20:00 UTC only — peak US/EU overlap, highest crypto liquidity.
    More selective than 8-20 UTC, should reduce false signals further."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (13<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class BollingerSessionRejection(Strategy):
    """Session filter + rejection hammer — wick touched band, candle closed back above.
    Price already started reversing before entry. Strongest confirmation + cleanest session."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if (self.data.Low[-1]<=self.dn[-1] and self.data.Close[-1]>self.dn[-1]
                and not self.position.is_long):
            self.buy(tp=self.mid[-1],sl=self.data.Low[-1]*0.99)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BollingerSessionWide(Strategy):
    """Session filter + 2.5 std bands — no EMA filter (keeps trade count healthy).
    Simple clean stack of two best individual improvements."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2.5)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()


# ── NEW: 1h/4h IMPROVEMENTS ──────────────────────────────────────────────────

class StretchScoreSession(Strategy):
    """StretchScore_Fast with 8-20 UTC session filter.
    Applies the same EU/US session logic that lifted 15m WR to 70%+.
    Targets 1h and 4h — cuts Asian-hour false reversals."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        if std_dn>=0.80 and self.rsi_v[-1]<30 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.80 and self.rsi_v[-1]>70 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class StretchScoreCapitulation(Strategy):
    """StretchScore with VOLUME SURGE instead of volume fade.
    A volume spike at the BB band = panic/capitulation = strongest reversal signal.
    Opposite of StretchScore_Custom which requires fading volume."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.vol_ma=self.I(sma,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        vol_spike=self.data.Volume[-1]>self.vol_ma[-1]*1.5
        if std_dn>=0.80 and vol_spike and self.rsi_v[-1]<35 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.80 and vol_spike and self.rsi_v[-1]>65 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class VWAPZScoreReversion(Strategy):
    """Rolling VWAP ZScore reversion — price deviating 1.5+ std from 20-bar VWAP.
    VWAP is volume-weighted so it's a stronger anchor than plain SMA.
    Best for 1h/4h where volume is meaningful."""
    def init(self):
        self.vwap=self.I(vwap_rolling,self.data.Close,self.data.Volume,20)
        self.std=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.vwap[-1]) or np.isnan(self.std[-1]) or np.isnan(self.e200[-1]): return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        z=(p-self.vwap[-1])/(self.std[-1]+1e-10)
        if z<-1.5 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.vwap[-1])
        elif z>1.5 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.vwap[-1])
        elif self.position.is_long and p>=self.vwap[-1]: self.position.close()
        elif self.position.is_short and p<=self.vwap[-1]: self.position.close()

class ConnorsRSISession(Strategy):
    """ConnorsRSI + 8-20 UTC session filter.
    ConnorsRSI already wins on 4h (65% BNB, 62% DOGE) — adding session filter
    should push it toward 68%+ by removing low-quality Asian-hour signals."""
    def init(self):
        self.crsi=self.I(connors_rsi,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.crsi[-1]) or np.isnan(self.e200[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        above=self.data.Close[-1]>self.e200[-1]
        if self.crsi[-1]<10 and above and not self.position.is_long:
            self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.04)
        elif self.crsi[-1]>90 and not above and not self.position.is_short:
            self.sell(sl=self.data.Close[-1]*1.03,tp=self.data.Close[-1]*0.96)
        elif self.position.is_long and self.crsi[-1]>50: self.position.close()
        elif self.position.is_short and self.crsi[-1]<50: self.position.close()

class AdaptiveBBReversion(Strategy):
    """ATR-adaptive Bollinger Band width — widens in high volatility, narrows in calm.
    High ATR: use 2.5 std (avoid false signals in choppy markets).
    Low ATR: use 1.5 std (catch smaller but genuine reversals).
    Dynamic threshold = more precise entries across all timeframes."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.atr_ma[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        upper=self.mid20[-1]+k*self.std20[-1]
        lower=self.mid20[-1]-k*self.std20[-1]
        above=p>self.e50[-1]
        if p<lower and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif p>upper and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

class StretchScore1hRelaxed(Strategy):
    """StretchScore with relaxed thresholds for 1h — std 0.70, RSI 40/60.
    Current StretchScore_Custom fires rarely on 1h (low signal count).
    Relaxing thresholds generates more signals while keeping edge above noise."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
        self.vol_ma=self.I(sma,self.data.Volume,10)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        vol_fading=self.data.Volume[-1]<self.vol_ma[-1]
        if std_dn>=0.70 and vol_fading and self.rsi_v[-1]<40 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.70 and vol_fading and self.rsi_v[-1]>60 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()


# ── ROUND 4: FINAL STRATEGIES ────────────────────────────────────────────────

class BollingerLondonOpen(Strategy):
    """BB Reversion 6-9 UTC London open session only.
    London open sets the day's direction — BB touches during this window
    often mark the day's high or low. Untested window, high potential."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (6<=hour<9): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        if p<self.dn[-1] and above and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and not above and not self.position.is_short: self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class ConsensusAdaptiveVWAP(Strategy):
    """AdaptiveBB AND VWAP ZScore must both agree on direction.
    Two independent signals confirming same direction = much higher confidence.
    AdaptiveBB 1h: 68-70% WR. VWAP 1h: 62-64% WR. Combined should hit 72%+."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.vwap=self.I(vwap_rolling,self.data.Close,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.vwap[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        lower=self.mid20[-1]-k*self.std20[-1]; upper=self.mid20[-1]+k*self.std20[-1]
        z=(p-self.vwap[-1])/(self.std20[-1]+1e-10)
        if p<lower and z<-1.0 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif p>upper and z>1.0 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

class ConsensusConnorsAdaptive(Strategy):
    """ConnorsRSI < 10 AND AdaptiveBB lower band — double confirmation for 4h.
    ConnorsRSI wins 4h (65% BNB, 62% DOGE). AdaptiveBB wins 4h BTC (75%).
    Both agreeing should eliminate noise and push WR toward 70%+ on 4h."""
    def init(self):
        self.crsi=self.I(connors_rsi,self.data.Close)
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.crsi[-1]) or np.isnan(self.atr[-1]) or np.isnan(self.e200[-1]): return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        lower=self.mid20[-1]-k*self.std20[-1]; upper=self.mid20[-1]+k*self.std20[-1]
        if self.crsi[-1]<10 and p<lower and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif self.crsi[-1]>90 and p>upper and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

class StretchScoreCapitulationRelaxed(Strategy):
    """Capitulation with relaxed RSI < 40 (vs strict < 35).
    Gets more signals. 84% WR at RSI<35 — curious if RSI<40 trades more with same edge."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.vol_ma=self.I(sma,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        vol_spike=self.data.Volume[-1]>self.vol_ma[-1]*1.5
        if std_dn>=0.80 and vol_spike and self.rsi_v[-1]<40 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.80 and vol_spike and self.rsi_v[-1]>60 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class AdaptiveBBLongOnly(Strategy):
    """AdaptiveBB longs only — no shorts. Crypto trends up long-term.
    Only buying oversold bounces from adaptive bands. No directional risk on short side."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        lower=self.mid20[-1]-k*self.std20[-1]
        if p<lower and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()

# ── PARAMETER VARIANTS ────────────────────────────────────────────────────────

class BBSession_Period15(Strategy):
    """BB_Session_8_20_UTC with faster BB period 15 — more signals, earlier entries."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,15,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class BBSession_Period25(Strategy):
    """BB_Session_8_20_UTC with slower BB period 25 — fewer signals, cleaner extremes."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,25,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class AdaptiveBB_ATR10(Strategy):
    """AdaptiveBB with faster ATR period 10 — more reactive to volatility shifts."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,10)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,10),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        upper=self.mid20[-1]+k*self.std20[-1]; lower=self.mid20[-1]-k*self.std20[-1]
        if p<lower and above and not self.position.is_long: self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif p>upper and not above and not self.position.is_short: self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

class AdaptiveBB_ATR20(Strategy):
    """AdaptiveBB with slower ATR period 20 — smoother volatility, less noise."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,20)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,20),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        upper=self.mid20[-1]+k*self.std20[-1]; lower=self.mid20[-1]-k*self.std20[-1]
        if p<lower and above and not self.position.is_long: self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif p>upper and not above and not self.position.is_short: self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

# ── ROUND 3: STACK WINNERS + CONSENSUS ───────────────────────────────────────

class AdaptiveBBSession(Strategy):
    """AdaptiveBB_ATR_Reversion + 8-20 UTC session filter.
    Stacks the two best improvements: dynamic BB width + clean session hours.
    Targets 1h/4h — should push AdaptiveBB from 70% toward 74%+."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.atr_ma[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        upper=self.mid20[-1]+k*self.std20[-1]
        lower=self.mid20[-1]-k*self.std20[-1]
        above=p>self.e50[-1]
        if p<lower and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif p>upper and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

class AdaptiveBBUSSession(Strategy):
    """AdaptiveBB_ATR_Reversion + 13-20 UTC US session only.
    Peak liquidity window — tighter than 8-20, should clean up signals further.
    Fewer trades but targeting 75%+ WR on 1h."""
    def init(self):
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr[-1]) or np.isnan(self.atr_ma[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (13<=hour<20): return
        p=self.data.Close[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        upper=self.mid20[-1]+k*self.std20[-1]
        lower=self.mid20[-1]-k*self.std20[-1]
        above=p>self.e50[-1]
        if p<lower and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid20[-1])
        elif p>upper and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid20[-1])
        elif self.position.is_long and p>=self.mid20[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid20[-1]: self.position.close()

class StretchScoreCapitulationSess(Strategy):
    """StretchScore_Capitulation + 8-20 UTC session filter.
    Capitulation (volume spike at BB) already hits 84% on BTC 1h.
    Adding session filter removes Asian-hour low-quality capitulation spikes."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.rsi_v=self.I(rsi,self.data.Close,14)
        self.vol_ma=self.I(sma,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.up[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        std_dn=(self.mid[-1]-p)/(self.mid[-1]-self.dn[-1]+1e-10)
        std_up=(p-self.mid[-1])/(self.up[-1]-self.mid[-1]+1e-10)
        vol_spike=self.data.Volume[-1]>self.vol_ma[-1]*1.5
        if std_dn>=0.80 and vol_spike and self.rsi_v[-1]<35 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif std_up>=0.80 and vol_spike and self.rsi_v[-1]>65 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class VWAPSession(Strategy):
    """VWAP_ZScore_Reversion + 8-20 UTC session filter.
    VWAP already shows massive signal counts (800-3000/asset) at 60-64% WR.
    Session filter trades fewer signals but should push WR toward 67%+."""
    def init(self):
        self.vwap=self.I(vwap_rolling,self.data.Close,self.data.Volume,20)
        self.std=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.vwap[-1]) or np.isnan(self.std[-1]) or np.isnan(self.e200[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        z=(p-self.vwap[-1])/(self.std[-1]+1e-10)
        if z<-1.5 and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.vwap[-1])
        elif z>1.5 and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.vwap[-1])
        elif self.position.is_long and p>=self.vwap[-1]: self.position.close()
        elif self.position.is_short and p<=self.vwap[-1]: self.position.close()

class ConsensusBBAdaptive(Strategy):
    """Consensus: BB band touch AND AdaptiveBB agree on same direction.
    Both signals must fire simultaneously — eliminates conflicting signals.
    Fewer trades but targeting 75%+ WR as only the strongest setups pass both filters."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.atr=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.atr_ma=self.I(sma,atr_calc(self.data.High,self.data.Low,self.data.Close,14),20)
        self.mid20=self.I(lambda c: pd.Series(c).rolling(20).mean().values,self.data.Close)
        self.std20=self.I(lambda c: pd.Series(c).rolling(20).std().values,self.data.Close)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.atr[-1]) or np.isnan(self.e50[-1]): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        k=2.5 if self.atr[-1]>self.atr_ma[-1] else 1.5
        ada_lower=self.mid20[-1]-k*self.std20[-1]
        ada_upper=self.mid20[-1]+k*self.std20[-1]
        bb_long  = p<self.dn[-1]
        bb_short = p>self.up[-1]
        ada_long  = p<ada_lower
        ada_short = p>ada_upper
        if bb_long and ada_long and above and not self.position.is_long:
            self.buy(sl=p*0.97,tp=self.mid[-1])
        elif bb_short and ada_short and not above and not self.position.is_short:
            self.sell(sl=p*1.03,tp=self.mid[-1])
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()


# ── ROUND 5: PRICE ACTION — FIBONACCI / FVG / ORDER BLOCKS ───────────────────

class FibonacciGoldenZone(Strategy):
    """Fibonacci Golden Zone (50-61.8% retracement) + 200 EMA trend filter.
    Swing high/low over 50 bars. Long when price pulls back into 50-61.8% zone
    in an uptrend AND a bullish engulfing candle confirms the bounce.
    SL below swing low, TP at swing high. Best for 1h/4h trending markets."""
    n = 50
    def init(self):
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if len(self.data.Close)<self.n+5: return
        if self.position: return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        if above:
            recent_high=max(self.data.High[-self.n:])
            recent_low =min(self.data.Low[-self.n:])
            if recent_high<=recent_low: return
            swing=recent_high-recent_low
            fib50 =recent_high-0.500*swing
            fib618=recent_high-0.618*swing
            in_zone=fib618<=p<=fib50
            engulf=(self.data.Close[-1]>self.data.Open[-1] and
                    self.data.Close[-2]<self.data.Open[-2])
            if in_zone and engulf and not self.position.is_long:
                self.buy(sl=recent_low*0.99,tp=recent_high)
        else:
            recent_high=max(self.data.High[-self.n:])
            recent_low =min(self.data.Low[-self.n:])
            if recent_high<=recent_low: return
            swing=recent_high-recent_low
            fib50 =recent_low+0.500*swing
            fib618=recent_low+0.618*swing
            in_zone=fib50<=p<=fib618
            engulf=(self.data.Close[-1]<self.data.Open[-1] and
                    self.data.Close[-2]>self.data.Open[-2])
            if in_zone and engulf and not self.position.is_short:
                self.sell(sl=recent_high*1.01,tp=recent_low)

class FibonacciGoldenZoneLong(Strategy):
    """Fibonacci Golden Zone — LONG ONLY.
    Same as FibonacciGoldenZone but only longs.
    Crypto/stocks trend up long-term — removing short side should improve WR."""
    n = 50
    def init(self):
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if len(self.data.Close)<self.n+5: return
        if self.position: return
        p=self.data.Close[-1]
        if p<=self.e200[-1]: return
        recent_high=max(self.data.High[-self.n:])
        recent_low =min(self.data.Low[-self.n:])
        if recent_high<=recent_low: return
        swing=recent_high-recent_low
        fib50 =recent_high-0.500*swing
        fib618=recent_high-0.618*swing
        in_zone=fib618<=p<=fib50
        engulf=(self.data.Close[-1]>self.data.Open[-1] and
                self.data.Close[-2]<self.data.Open[-2])
        if in_zone and engulf and not self.position.is_long:
            self.buy(sl=recent_low*0.99,tp=recent_high)
        elif self.position.is_long and p>=recent_high: self.position.close()

class FibonacciRetracement382(Strategy):
    """Fibonacci 38.2% retracement entry — shallower pullback, stronger trend.
    38.2% means the trend barely pulled back = trend very strong.
    Tighter SL (recent swing low minus 1%), TP at swing high.
    EMA200 trend filter. Engulfing confirmation."""
    n = 50
    def init(self):
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if len(self.data.Close)<self.n+5: return
        if self.position: return
        p=self.data.Close[-1]
        if p<=self.e200[-1]: return
        recent_high=max(self.data.High[-self.n:])
        recent_low =min(self.data.Low[-self.n:])
        if recent_high<=recent_low: return
        swing=recent_high-recent_low
        fib382=recent_high-0.382*swing
        fib50 =recent_high-0.500*swing
        in_zone=fib50<=p<=fib382
        engulf=(self.data.Close[-1]>self.data.Open[-1] and
                self.data.Close[-2]<self.data.Open[-2])
        if in_zone and engulf and not self.position.is_long:
            self.buy(sl=recent_low*0.99,tp=recent_high)
        elif self.position.is_long and p>=recent_high: self.position.close()

class FairValueGap(Strategy):
    """Fair Value Gap (FVG): 3-candle imbalance where middle candle's body leaves a gap.
    Bullish FVG: candle[i].low > candle[i-2].high → price gaps up, leaving unfilled zone.
    Entry when price retraces to the 50% midpoint of the gap (partial fill).
    EMA200 trend filter — only trade FVGs in trend direction. Best for 1h/4h."""
    def init(self):
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if len(self.data.Close)<25: return
        if self.position: return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        for i in range(3,21):
            if i+2>=len(self.data.Close): break
            # Bullish FVG: Low[i] > High[i+2] (gap between candle 3 ago and now)
            fvg_lo=self.data.High[-(i+2)]
            fvg_hi=self.data.Low[-i]
            if fvg_hi>fvg_lo and above:
                mid50=(fvg_lo+fvg_hi)/2
                if fvg_lo<=p<=mid50 and not self.position.is_long:
                    self.buy(sl=fvg_lo*0.99,tp=p+(fvg_hi-fvg_lo)*2)
                    return
            # Bearish FVG: High[i] < Low[i+2]
            fvg_hi2=self.data.Low[-(i+2)]
            fvg_lo2=self.data.High[-i]
            if fvg_lo2<fvg_hi2 and not above:
                mid50=(fvg_lo2+fvg_hi2)/2
                if mid50<=p<=fvg_hi2 and not self.position.is_short:
                    self.sell(sl=fvg_hi2*1.01,tp=p-(fvg_hi2-fvg_lo2)*2)
                    return

class FairValueGapLong(Strategy):
    """Fair Value Gap — LONG ONLY + EMA50 trend (faster trend filter than 200).
    Only buys bullish FVG retraces when price > EMA50.
    Removes short side. 50-period faster trend to catch more setups in bull runs."""
    def init(self):
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        if len(self.data.Close)<25: return
        if self.position: return
        p=self.data.Close[-1]
        if p<=self.e50[-1]: return
        for i in range(3,21):
            if i+2>=len(self.data.Close): break
            fvg_lo=self.data.High[-(i+2)]
            fvg_hi=self.data.Low[-i]
            if fvg_hi>fvg_lo:
                mid50=(fvg_lo+fvg_hi)/2
                if fvg_lo<=p<=mid50 and not self.position.is_long:
                    self.buy(sl=fvg_lo*0.99,tp=p+(fvg_hi-fvg_lo)*2)
                    return
        if self.position.is_long and p>self.e50[-1]*1.03: self.position.close()

class OrderBlock(Strategy):
    """Order Block: last candle before a strong impulse move (body >= 1.5x ATR).
    Bullish OB: last bearish candle before a strong bullish impulse.
    Entry when price retraces back into the OB candle's range.
    200 EMA trend filter. Targets 1h/4h — enough data for meaningful order blocks."""
    atr_n=14; impulse_mult=1.5; lookback=30
    def init(self):
        self.atr_v=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,self.atr_n)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.atr_v[-1]) or np.isnan(self.e200[-1]): return
        if len(self.data.Close)<self.lookback+5: return
        if self.position: return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        for i in range(2,self.lookback+1):
            if i+1>=len(self.data.Close): break
            ob_o=self.data.Open[-(i+1)]; ob_c=self.data.Close[-(i+1)]
            ob_h=self.data.High[-(i+1)]; ob_l=self.data.Low[-(i+1)]
            imp_o=self.data.Open[-i];    imp_c=self.data.Close[-i]
            imp_body=abs(imp_c-imp_o)
            # Bullish OB: bearish candle → strong bullish impulse
            if (ob_c<ob_o and imp_c>imp_o and
                    imp_body>=self.impulse_mult*self.atr_v[-1] and above):
                if ob_l<=p<=ob_h and not self.position.is_long:
                    self.buy(sl=ob_l*0.99,tp=p+imp_body)
                    return
            # Bearish OB: bullish candle → strong bearish impulse
            if (ob_c>ob_o and imp_c<imp_o and
                    imp_body>=self.impulse_mult*self.atr_v[-1] and not above):
                if ob_l<=p<=ob_h and not self.position.is_short:
                    self.sell(sl=ob_h*1.01,tp=p-imp_body)
                    return

class OrderBlockLong(Strategy):
    """Order Block — LONG ONLY + EMA50 trend.
    Same as OrderBlock but only takes bullish OB setups when price > EMA50.
    Faster trend filter + no shorts = more trades in bull markets."""
    atr_n=14; impulse_mult=1.5; lookback=30
    def init(self):
        self.atr_v=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,self.atr_n)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr_v[-1]) or np.isnan(self.e50[-1]): return
        if len(self.data.Close)<self.lookback+5: return
        if self.position: return
        p=self.data.Close[-1]
        if p<=self.e50[-1]: return
        for i in range(2,self.lookback+1):
            if i+1>=len(self.data.Close): break
            ob_o=self.data.Open[-(i+1)]; ob_c=self.data.Close[-(i+1)]
            ob_h=self.data.High[-(i+1)]; ob_l=self.data.Low[-(i+1)]
            imp_o=self.data.Open[-i];    imp_c=self.data.Close[-i]
            imp_body=abs(imp_c-imp_o)
            if (ob_c<ob_o and imp_c>imp_o and
                    imp_body>=self.impulse_mult*self.atr_v[-1]):
                if ob_l<=p<=ob_h and not self.position.is_long:
                    self.buy(sl=ob_l*0.99,tp=p+imp_body)
                    return
        if self.position.is_long and p>self.e50[-1]*1.05: self.position.close()


# ── ROUND 6: DATA TRADER + TRADINGLAB STRATEGIES ─────────────────────────────

class MACDParabolicSAR200EMA(Strategy):
    """MACD + Parabolic SAR + 200 EMA — 70% WR (Data Trader, 100 trades).
    All three must align: price above 200 EMA, MACD crosses signal, SAR below price.
    Triple confirmation = very high quality entries."""
    def init(self):
        self.ml,self.sl_=self.I(macd_calc,self.data.Close)
        self.sar,self.sar_trend=self.I(parabolic_sar,self.data.High,self.data.Low)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]) or np.isnan(self.sar[-1]): return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        macd_cross_up=self.ml[-2]<self.sl_[-2] and self.ml[-1]>self.sl_[-1]
        macd_cross_dn=self.ml[-2]>self.sl_[-2] and self.ml[-1]<self.sl_[-1]
        sar_up=self.sar_trend[-1]==1   # SAR below price
        sar_dn=self.sar_trend[-1]==-1  # SAR above price
        if macd_cross_up and sar_up and above and not self.position.is_long:
            self.position.close(); self.buy(sl=self.data.Close[-1]*0.97,tp=self.data.Close[-1]*1.06)
        elif macd_cross_dn and sar_dn and not above and not self.position.is_short:
            self.position.close(); self.sell(sl=self.data.Close[-1]*1.03,tp=self.data.Close[-1]*0.94)
        elif self.position.is_long and self.sar_trend[-1]==-1: self.position.close()
        elif self.position.is_short and self.sar_trend[-1]==1: self.position.close()

class MACD200EMA_ZeroLine(Strategy):
    """MACD + 200 EMA with zero-line condition — 86% WR (TradingLab, 100 trades).
    MACD200EMA already in factory. THIS variant: crossover must occur BELOW zero (longs)
    or ABOVE zero (shorts). Zero-line requirement filters out momentum-less crossovers."""
    def init(self):
        self.ml,self.sl_=self.I(macd_calc,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        cross_up=self.ml[-2]<self.sl_[-2] and self.ml[-1]>self.sl_[-1]
        cross_dn=self.ml[-2]>self.sl_[-2] and self.ml[-1]<self.sl_[-1]
        if cross_up and self.ml[-1]<0 and above and not self.position.is_long:
            self.position.close(); self.buy()
        elif cross_dn and self.ml[-1]>0 and not above and not self.position.is_short:
            self.position.close(); self.sell()

class DEMASupertrendLong(Strategy):
    """DEMA(200) + Supertrend — +130% in 2 months (TradingLab).
    DEMA is more responsive than EMA — faster trend detection.
    Long only: buy on Supertrend BUY signal when price above DEMA200.
    Exit on Supertrend SELL signal. Trailing stop built into Supertrend."""
    def init(self):
        self.dm=self.I(dema,self.data.Close,200)
        self.st=self.I(supertrend,self.data.High,self.data.Low,self.data.Close,12,3)
    def next(self):
        if np.isnan(self.dm[-1]): return
        p=self.data.Close[-1]
        if self.st[-1]==1 and self.st[-2]==-1 and p>self.dm[-1] and not self.position.is_long:
            self.buy(sl=p*0.95)
        elif self.position.is_long and self.st[-1]==-1:
            self.position.close()

class StochasticCrossback200EMA(Strategy):
    """Stochastic crossback + 200 EMA — more selective entry than standard StochCross.
    Wait for K% to CROSS BACK inside overbought/oversold zone (not just reach it).
    Crossback = price already reversing, confirming the signal. SL at swing, TP 2:1."""
    def init(self):
        self.k,self.d=self.I(stochastic,self.data.High,self.data.Low,self.data.Close,14,3)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.k[-1]) or np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        # Crossback: K was below 20, now crosses above 20
        cross_back_up = self.k[-2]<20 and self.k[-1]>=20
        cross_back_dn = self.k[-2]>80 and self.k[-1]<=80
        p=self.data.Close[-1]
        if cross_back_up and above and not self.position.is_long:
            sl=min(self.data.Low[-3:])*0.99
            self.buy(sl=sl,tp=p+(p-sl)*2)
        elif cross_back_dn and not above and not self.position.is_short:
            sl=max(self.data.High[-3:])*1.01
            self.sell(sl=sl,tp=p-(sl-p)*2)
        elif self.position.is_long and self.k[-1]>80: self.position.close()
        elif self.position.is_short and self.k[-1]<20: self.position.close()

class ParabolicSAR200EMA(Strategy):
    """Parabolic SAR + 200 EMA — simple high win rate (Data Trader).
    SAR below price = uptrend signal. Only take when aligned with 200 EMA.
    Exit immediately on SAR flip (built-in trailing stop)."""
    def init(self):
        self.sar,self.sar_trend=self.I(parabolic_sar,self.data.High,self.data.Low)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]) or np.isnan(self.sar[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        flip_up=self.sar_trend[-2]==-1 and self.sar_trend[-1]==1
        flip_dn=self.sar_trend[-2]==1 and self.sar_trend[-1]==-1
        if flip_up and above and not self.position.is_long:
            self.position.close(); self.buy()
        elif flip_dn and not above and not self.position.is_short:
            self.position.close(); self.sell()
        elif self.position.is_long and self.sar_trend[-1]==-1: self.position.close()
        elif self.position.is_short and self.sar_trend[-1]==1: self.position.close()

class SupertrendEMA200(Strategy):
    """Supertrend + 200 EMA — clean two-indicator system (Data Trader).
    Supertrend = short-to-medium trend. 200 EMA = long-term trend.
    Only take Supertrend signals aligned with 200 EMA direction."""
    def init(self):
        self.st=self.I(supertrend,self.data.High,self.data.Low,self.data.Close,10,3)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        if self.st[-1]==1 and self.st[-2]==-1 and above and not self.position.is_long:
            self.position.close(); self.buy()
        elif self.st[-1]==-1 and self.st[-2]==1 and not above and not self.position.is_short:
            self.position.close(); self.sell()
        elif self.position.is_long and self.st[-1]==-1: self.position.close()
        elif self.position.is_short and self.st[-1]==1: self.position.close()

class TripleConfirmStochRSIMACD(Strategy):
    """Triple confirmation: Stochastic oversold + RSI above 50 + MACD cross.
    All 3 must align on the same bar. Very selective = fewer but higher quality trades.
    Stochastic K&D both < 20, RSI > 50, MACD crosses up = strong long signal."""
    def init(self):
        self.k,self.d=self.I(stochastic,self.data.High,self.data.Low,self.data.Close,14,3)
        self.r=self.I(rsi,self.data.Close,14)
        self.ml,self.sl_=self.I(macd_calc,self.data.Close)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.k[-1]) or np.isnan(self.r[-1]) or np.isnan(self.e200[-1]): return
        above=self.data.Close[-1]>self.e200[-1]
        macd_up=self.ml[-2]<self.sl_[-2] and self.ml[-1]>self.sl_[-1]
        macd_dn=self.ml[-2]>self.sl_[-2] and self.ml[-1]<self.sl_[-1]
        p=self.data.Close[-1]
        if self.k[-1]<20 and self.d[-1]<20 and self.r[-1]>50 and macd_up and above and not self.position.is_long:
            sl=min(self.data.Low[-3:])*0.99
            self.buy(sl=sl,tp=p+(p-sl)*1.5)
        elif self.k[-1]>80 and self.d[-1]>80 and self.r[-1]<50 and macd_dn and not above and not self.position.is_short:
            sl=max(self.data.High[-3:])*1.01
            self.sell(sl=sl,tp=p-(sl-p)*1.5)
        elif self.position.is_long and self.k[-1]>80: self.position.close()
        elif self.position.is_short and self.k[-1]<20: self.position.close()

class TripleEMAPullback(Strategy):
    """Triple EMA Pullback (25/50/100) — tested scalping approach (TradingLab).
    EMAs stacked 25>50>100 = uptrend. Wait for pullback to 25 or 50 EMA.
    Pullback must NOT break 100 EMA. Entry on close back above 25 EMA.
    SL at 50 EMA, TP 1.5x stop. Works on 1h/4h as trend-continuation."""
    def init(self):
        self.e25=self.I(ema,self.data.Close,25)
        self.e50=self.I(ema,self.data.Close,50)
        self.e100=self.I(ema,self.data.Close,100)
    def next(self):
        if np.isnan(self.e100[-1]): return
        if self.position: return
        p=self.data.Close[-1]
        # Uptrend: EMAs stacked 25>50>100
        if self.e25[-1]>self.e50[-1]>self.e100[-1]:
            # Price pulled back to zone (below 25 or near 50) then closes above 25
            was_below_25=self.data.Close[-2]<self.e25[-2]
            now_above_25=p>self.e25[-1]
            held_100=self.data.Low[-2]>self.e100[-2]*0.995  # didn't break 100 EMA
            if was_below_25 and now_above_25 and held_100 and not self.position.is_long:
                sl=self.e50[-1]*0.99
                self.buy(sl=sl,tp=p+(p-sl)*1.5)
        # Downtrend: EMAs stacked 100>50>25
        elif self.e25[-1]<self.e50[-1]<self.e100[-1]:
            was_above_25=self.data.Close[-2]>self.e25[-2]
            now_below_25=p<self.e25[-1]
            held_100=self.data.High[-2]<self.e100[-2]*1.005
            if was_above_25 and now_below_25 and held_100 and not self.position.is_short:
                sl=self.e50[-1]*1.01
                self.sell(sl=sl,tp=p-(sl-p)*1.5)

class ThreeBarPattern(Strategy):
    """3-Bar Pattern: Large igniting bar → small pullback (<50% of igniting) → confirmation close.
    Confirmation candle closes beyond pullback high/low = momentum continues.
    ATR confirms igniting bar is genuinely large. EMA50 trend filter."""
    def init(self):
        self.atr_v=self.I(atr_calc,self.data.High,self.data.Low,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.atr_v[-1]) or np.isnan(self.e50[-1]): return
        if len(self.data.Close)<5: return
        if self.position: return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        ign_body=abs(self.data.Close[-3]-self.data.Open[-3])
        pb_body =abs(self.data.Close[-2]-self.data.Open[-2])
        if ign_body<1.5*self.atr_v[-3]: return
        if pb_body>=0.5*ign_body: return
        # Bullish
        if (self.data.Close[-3]>self.data.Open[-3] and
                p>self.data.High[-2] and above and not self.position.is_long):
            self.buy(sl=self.data.Low[-2]*0.99,tp=p+ign_body)
        # Bearish
        elif (self.data.Close[-3]<self.data.Open[-3] and
                p<self.data.Low[-2] and not above and not self.position.is_short):
            self.sell(sl=self.data.High[-2]*1.01,tp=p-ign_body)

class ABCPatternBreakout(Strategy):
    """ABC Pattern: A=swing low, B=bounce high, C=pullback (C must be above A).
    Buy breakout above B. Every major market move starts with this structure.
    SL at C level, TP = full A-B range projected from B."""
    n=40
    def init(self):
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        if len(self.data.Close)<self.n+5: return
        if self.position: return
        p=self.data.Close[-1]
        # Only in potential uptrend zones
        lows=list(self.data.Low[-self.n:])
        highs=list(self.data.High[-self.n:])
        a_idx=int(np.argmin(lows))
        a_price=lows[a_idx]
        if a_idx>=len(highs)-3: return
        b_idx=a_idx+int(np.argmax(highs[a_idx:]))
        b_price=highs[b_idx]
        if b_idx>=len(lows)-2: return
        c_idx=b_idx+int(np.argmin(lows[b_idx:]))
        c_price=lows[c_idx]
        if c_price<=a_price: return    # C must be above A
        if c_idx>=self.n-1: return     # C must not be the last bar
        if b_price<=a_price: return
        # Current price breaking above B
        if p>b_price and not self.position.is_long:
            self.buy(sl=c_price*0.99,tp=p+(b_price-a_price))

class WilliamsAlligator(Strategy):
    """Williams Alligator — trend detection via 3 smoothed MAs (Jaw/Teeth/Lips).
    Lips(5) crosses above Teeth(8) and Jaw(13) = uptrend (alligator eating).
    Exit when lines converge (alligator sleeping). Filters ranging markets."""
    def init(self):
        self.lips,self.teeth,self.jaw=self.I(alligator_lines,self.data.High,self.data.Low)
    def next(self):
        if np.isnan(self.jaw[-1]) or np.isnan(self.teeth[-1]) or np.isnan(self.lips[-1]): return
        # Lines spreading upward = buying
        lips_up=self.lips[-1]>self.lips[-2]
        teeth_up=self.teeth[-1]>self.teeth[-2]
        jaw_up=self.jaw[-1]>self.jaw[-2]
        # Lines spreading downward = selling
        lips_dn=self.lips[-1]<self.lips[-2]
        teeth_dn=self.teeth[-1]<self.teeth[-2]
        jaw_dn=self.jaw[-1]<self.jaw[-2]
        aligned_up=self.lips[-1]>self.teeth[-1]>self.jaw[-1]
        aligned_dn=self.lips[-1]<self.teeth[-1]<self.jaw[-1]
        converging=(abs(self.lips[-1]-self.jaw[-1])<abs(self.lips[-2]-self.jaw[-2]))
        if aligned_up and lips_up and teeth_up and jaw_up and not self.position.is_long:
            self.position.close(); self.buy()
        elif aligned_dn and lips_dn and teeth_dn and jaw_dn and not self.position.is_short:
            self.position.close(); self.sell()
        elif converging and self.position: self.position.close()

class RSIHiddenDivergence(Strategy):
    """RSI Hidden Divergence — TREND CONTINUATION signal (not reversal).
    Bullish: price makes higher low, RSI makes lower low → still going up.
    Bearish: price makes lower high, RSI makes higher high → still going down.
    200 EMA confirms trend direction. Targets 1h/4h."""
    lookback=20
    def init(self):
        self.r=self.I(rsi,self.data.Close,14)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.r[-1]) or np.isnan(self.e200[-1]): return
        if len(self.data.Close)<self.lookback+5: return
        if self.position: return
        p=self.data.Close[-1]; above=p>self.e200[-1]
        # Bullish hidden divergence: higher price low + lower RSI low (in uptrend)
        if above:
            price_hl=(min(self.data.Low[-5:-1])>min(self.data.Low[-self.lookback:-5]))
            rsi_ll=(min(self.r[-5:-1])<min(self.r[-self.lookback:-5]))
            if price_hl and rsi_ll and self.r[-1]<50 and not self.position.is_long:
                self.buy(sl=p*0.97,tp=p*1.05)
        # Bearish hidden divergence: lower price high + higher RSI high (in downtrend)
        else:
            price_lh=(max(self.data.High[-5:-1])<max(self.data.High[-self.lookback:-5]))
            rsi_hh=(max(self.r[-5:-1])>max(self.r[-self.lookback:-5]))
            if price_lh and rsi_hh and self.r[-1]>50 and not self.position.is_short:
                self.sell(sl=p*1.03,tp=p*0.95)


# ─────────────────────────────────────────────
# ROUND 7: NEW STRATEGIES
# ─────────────────────────────────────────────

class EngulfingCandle(Strategy):
    """Bullish/Bearish Engulfing — current candle fully swallows previous one.
    200 EMA confirms trend direction. Long only in uptrend, short only in downtrend."""
    def init(self):
        self.e200 = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if self.position: return
        o, c = self.data.Open, self.data.Close
        h, l = self.data.High, self.data.Low
        prev_bull = c[-2] > o[-2]
        prev_bear = c[-2] < o[-2]
        bull_engulf = (c[-1] > o[-1]) and prev_bear and (c[-1] > o[-2]) and (o[-1] < c[-2])
        bear_engulf = (c[-1] < o[-1]) and prev_bull and (c[-1] < o[-2]) and (o[-1] > c[-2])
        p = c[-1]
        if bull_engulf and p > self.e200[-1]:
            self.buy(sl=l[-1] * 0.997, tp=p * 1.015)
        elif bear_engulf and p < self.e200[-1]:
            self.sell(sl=h[-1] * 1.003, tp=p * 0.985)


class HammerShootingStar(Strategy):
    """Hammer (bullish reversal) and Shooting Star (bearish reversal) pin bar patterns.
    Body must be small, wick must be 2x+ the body. 200 EMA for trend context."""
    min_wick_ratio = 2.0
    def init(self):
        self.e200 = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if self.position: return
        o, c = self.data.Open[-1], self.data.Close[-1]
        h, l = self.data.High[-1], self.data.Low[-1]
        body  = abs(c - o)
        if body == 0: return
        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)
        p = c
        # Hammer: long lower wick, small upper wick, price near lows → bullish
        if lower_wick >= body * self.min_wick_ratio and upper_wick < body and p < self.e200[-1] * 1.02:
            self.buy(sl=l * 0.997, tp=p * 1.015)
        # Shooting Star: long upper wick, small lower wick, price near highs → bearish
        elif upper_wick >= body * self.min_wick_ratio and lower_wick < body and p > self.e200[-1] * 0.98:
            self.sell(sl=h * 1.003, tp=p * 0.985)


class EMARibbon(Strategy):
    """EMA Ribbon — 5 EMAs (8, 13, 21, 34, 55).
    When all 5 are in perfect order (8>13>21>34>55) = strong uptrend → long.
    When all 5 are in reverse order = strong downtrend → short.
    Exit when ribbon starts to tangle."""
    def init(self):
        self.e8  = self.I(ema, self.data.Close, 8)
        self.e13 = self.I(ema, self.data.Close, 13)
        self.e21 = self.I(ema, self.data.Close, 21)
        self.e34 = self.I(ema, self.data.Close, 34)
        self.e55 = self.I(ema, self.data.Close, 55)
    def next(self):
        if any(np.isnan(x) for x in [self.e8[-1], self.e13[-1], self.e21[-1], self.e34[-1], self.e55[-1]]): return
        bull = self.e8[-1] > self.e13[-1] > self.e21[-1] > self.e34[-1] > self.e55[-1]
        bear = self.e8[-1] < self.e13[-1] < self.e21[-1] < self.e34[-1] < self.e55[-1]
        p = self.data.Close[-1]
        if not self.position:
            if bull:
                self.buy(sl=self.e55[-1] * 0.99, tp=p * 1.02)
            elif bear:
                self.sell(sl=self.e55[-1] * 1.01, tp=p * 0.98)
        else:
            # Exit when ribbon loses order
            if self.position.is_long and not bull:
                self.position.close()
            elif self.position.is_short and not bear:
                self.position.close()


class PivotPointBounce(Strategy):
    """Daily Pivot Point Bounce — calculates pivot from previous bar's H/L/C.
    Buys when price touches S1 support and bounces. Sells at R1 resistance."""
    def init(self):
        self.e50 = self.I(ema, self.data.Close, 50)
    def next(self):
        if len(self.data.Close) < 3: return
        if self.position: return
        ph = self.data.High[-2]; pl = self.data.Low[-2]; pc = self.data.Close[-2]
        pivot = (ph + pl + pc) / 3
        s1 = 2 * pivot - ph
        r1 = 2 * pivot - pl
        p  = self.data.Close[-1]
        l  = self.data.Low[-1]
        h  = self.data.High[-1]
        # Bounce off S1 support (bullish)
        if l <= s1 * 1.002 and p > s1 and p > self.e50[-1]:
            self.buy(sl=s1 * 0.995, tp=pivot * 0.999)
        # Rejection at R1 resistance (bearish)
        elif h >= r1 * 0.998 and p < r1 and p < self.e50[-1]:
            self.sell(sl=r1 * 1.005, tp=pivot * 1.001)


class VWAPConsecutiveClose(Strategy):
    """VWAP Consecutive Close — 3 consecutive closes above VWAP = momentum long.
    3 consecutive closes below VWAP = momentum short.
    Simple, clean, no over-engineering."""
    n_consec = 3
    vwap_period = 20
    def init(self):
        self.vwap = self.I(vwap_rolling, self.data.Close, self.data.Volume, self.vwap_period)
    def next(self):
        if np.isnan(self.vwap[-1]): return
        if self.position: return
        if len(self.data.Close) < self.n_consec + 1: return
        closes = self.data.Close
        vwap   = self.vwap
        all_above = all(closes[-i] > vwap[-i] for i in range(1, self.n_consec + 1))
        all_below = all(closes[-i] < vwap[-i] for i in range(1, self.n_consec + 1))
        p = closes[-1]
        if all_above:
            self.buy(sl=vwap[-1] * 0.995, tp=p * 1.015)
        elif all_below:
            self.sell(sl=vwap[-1] * 1.005, tp=p * 0.985)


class RSIBBCombo(Strategy):
    """RSI + Bollinger Band Combo — RSI oversold AND price at/below lower BB = high prob bounce.
    RSI overbought AND price at/above upper BB = high prob drop.
    Both conditions must be true simultaneously."""
    bb_period = 20
    bb_std    = 2.0
    rsi_ob    = 70
    rsi_os    = 30
    def init(self):
        self.r   = self.I(rsi, self.data.Close, 14)
        mid      = self.I(sma, self.data.Close, self.bb_period)
        std      = self.I(lambda x: pd.Series(x).rolling(self.bb_period).std().values, self.data.Close)
        self.upper = self.I(lambda x, m, s: m + self.bb_std * s, self.data.Close, mid, std)
        self.lower = self.I(lambda x, m, s: m - self.bb_std * s, self.data.Close, mid, std)
        self.e200  = self.I(ema, self.data.Close, 200)
    def next(self):
        if any(np.isnan(x) for x in [self.r[-1], self.upper[-1], self.lower[-1]]): return
        if self.position: return
        p = self.data.Close[-1]
        oversold_at_lower  = self.r[-1] < self.rsi_os and p <= self.lower[-1] * 1.003
        overbought_at_upper = self.r[-1] > self.rsi_ob and p >= self.upper[-1] * 0.997
        if oversold_at_lower:
            self.buy(sl=p * 0.97, tp=p * 1.02)
        elif overbought_at_upper:
            self.sell(sl=p * 1.03, tp=p * 0.98)


class GapFill(Strategy):
    """Gap Fill — price gaps up or down at open vs previous close.
    Bets on gap filling back. Requires gap >= 0.3% to be meaningful.
    Works best on higher timeframes where gaps form cleanly."""
    min_gap_pct = 0.003
    def init(self):
        self.e50 = self.I(ema, self.data.Close, 50)
    def next(self):
        if len(self.data.Close) < 3: return
        if self.position: return
        prev_close = self.data.Close[-2]
        curr_open  = self.data.Open[-1]
        curr_close = self.data.Close[-1]
        gap_pct = (curr_open - prev_close) / prev_close
        # Gap up — expect fill back down
        if gap_pct >= self.min_gap_pct and curr_close > prev_close:
            self.sell(sl=curr_close * 1.005, tp=prev_close * 1.001)
        # Gap down — expect fill back up
        elif gap_pct <= -self.min_gap_pct and curr_close < prev_close:
            self.buy(sl=curr_close * 0.995, tp=prev_close * 0.999)


# ── ROUND 8: NEW STRATEGIES + SMART MODIFICATIONS ───────────────────────────

class KeltnerReversionSession(Strategy):
    """Keltner Channel reversion + 8-20 UTC session filter.
    KC uses ATR for band width (not std dev) — adapts to volatility naturally.
    Same mean-reversion logic as BB_Session but smoother, fewer false signals."""
    def init(self):
        self.km,self.ku,self.kd=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,1.5)
    def next(self):
        if np.isnan(self.ku[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.kd[-1] and not self.position.is_long: self.buy(tp=self.km[-1],sl=p*0.97)
        elif p>self.ku[-1] and self.position.is_long: self.position.close()

class KeltnerReversionSessionLong(Strategy):
    """Keltner reversion + session + long only + EMA50 trend filter.
    Crypto trends up — only buy KC oversold bounces in uptrend during EU/US hours."""
    def init(self):
        self.km,self.ku,self.kd=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,1.5)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.ku[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.kd[-1] and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.km[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.km[-1]: self.position.close()

class DoubleBBSession(Strategy):
    """Double Bollinger Band confirmation + session filter.
    Price must close below BOTH BB(20,2) lower band AND BB(10,1.5) lower band.
    Two independent band definitions both flagging oversold = highest conviction entry."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.mid2,self.up2,self.dn2=self.I(bollinger,self.data.Close,10,1.5)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.dn2[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        if p<self.dn[-1] and p<self.dn2[-1] and above and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and p>self.up2[-1] and not above and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class DoubleBBSessionLong(Strategy):
    """Double BB confirmation + session + long only.
    Only buying the most extreme oversold conditions. No short side risk."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.mid2,self.up2,self.dn2=self.I(bollinger,self.data.Close,10,1.5)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.dn2[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and p<self.dn2[-1] and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class ConsecutiveCandleSession(Strategy):
    """4+ consecutive same-direction candles = exhaustion → bet reversal.
    Pure price action, no indicators. Session filtered 8-20 UTC.
    Tests whether the edge is in BB math or just in the session + exhaustion concept."""
    n_candles=4
    def init(self): self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        if len(self.data.Close)<self.n_candles+1: return
        closes=list(self.data.Close[-self.n_candles-1:])
        opens=list(self.data.Open[-self.n_candles-1:])
        p=closes[-1]; above=p>self.e50[-1]
        n_down=all(closes[i]<opens[i] for i in range(self.n_candles))
        n_up  =all(closes[i]>opens[i] for i in range(self.n_candles))
        if n_down and above and not self.position.is_long:
            self.buy(tp=p*1.015,sl=p*0.97)
        elif n_up and not above and not self.position.is_short:
            self.sell(tp=p*0.985,sl=p*1.03)
        elif self.position.is_long and p>p*1.015: self.position.close()
        elif self.position.is_short and p<p*0.985: self.position.close()

class ConsecutiveCandle5Session(Strategy):
    """5+ consecutive candles variant — even more selective than 4."""
    n_candles=5
    def init(self): self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        if len(self.data.Close)<self.n_candles+1: return
        closes=list(self.data.Close[-self.n_candles-1:])
        opens=list(self.data.Open[-self.n_candles-1:])
        p=closes[-1]; above=p>self.e50[-1]
        n_down=all(closes[i]<opens[i] for i in range(self.n_candles))
        n_up  =all(closes[i]>opens[i] for i in range(self.n_candles))
        if n_down and above and not self.position.is_long:
            self.buy(tp=p*1.015,sl=p*0.97)
        elif n_up and not above and not self.position.is_short:
            self.sell(tp=p*0.985,sl=p*1.03)
        elif self.position.is_long and p>p*1.015: self.position.close()
        elif self.position.is_short and p<p*0.985: self.position.close()

class RSIExtremeSession(Strategy):
    """Pure RSI extremes (< 15 oversold / > 85 overbought) + 8-20 UTC session + EMA50.
    No BB at all — tests if the edge is in the indicator or just the session filter.
    Stricter thresholds than standard RSI30/70 to keep WR high."""
    def init(self):
        self.r=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.r[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        if self.r[-1]<15 and above and not self.position.is_long:
            self.buy(tp=p*1.02,sl=p*0.97)
        elif self.r[-1]>85 and not above and not self.position.is_short:
            self.sell(tp=p*0.98,sl=p*1.03)
        elif self.position.is_long and self.r[-1]>50: self.position.close()
        elif self.position.is_short and self.r[-1]<50: self.position.close()

class RSIExtremeSessionLong(Strategy):
    """RSI extreme + session + long only. RSI < 15 in uptrend during EU/US hours."""
    def init(self):
        self.r=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.r[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if self.r[-1]<15 and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=p*1.02,sl=p*0.97)
        elif self.position.is_long and self.r[-1]>50: self.position.close()

class BBVolumeSession(Strategy):
    """BB touch + volume above average + session filter. Simple, clean.
    Volume confirming the move at the band = real buying/selling pressure, not noise.
    Simpler than StretchScore — no RSI or stretch ratio, just band + volume + session."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.vol_ma=self.I(sma,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.vol_ma[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        vol_confirm=self.data.Volume[-1]>self.vol_ma[-1]*1.2
        if p<self.dn[-1] and vol_confirm and above and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and vol_confirm and not above and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BBVolumeSessionLong(Strategy):
    """BB + volume + session + long only. Only buying confirmed oversold bounces."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.vol_ma=self.I(sma,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.vol_ma[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        vol_confirm=self.data.Volume[-1]>self.vol_ma[-1]*1.2
        if p<self.dn[-1] and vol_confirm and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BBSession_Period10(Strategy):
    """BB period 10 + session. Even shorter period = more signals, faster response.
    Comparison vs period 15/20/25 to find the sweet spot."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,10,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class BBSession_Period30(Strategy):
    """BB period 30 + session. Very slow bands = only the most extreme moves trigger.
    Fewer signals, targeting highest possible WR at the cost of trade count."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,30,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and self.position.is_long: self.position.close()

class BBSession_EMA50_LongOnly(Strategy):
    """BB_Session_8_20 + EMA50 trend filter + long only.
    The best-performing session strategy, enhanced with trend bias.
    Removes shorts (which underperform in bull crypto market)."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()

class BBSession_EMA200_LongOnly(Strategy):
    """BB_Session_8_20 + EMA200 trend filter + long only.
    Stronger trend filter than EMA50 — only longs when in confirmed long-term uptrend."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e200[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and p>self.e200[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()


# ─────────────────────────────────────────────
# ROUND 9 STRATEGIES
# ─────────────────────────────────────────────

class KeltnerVolumeSession(Strategy):
    """Keltner Channel + volume confirm (>1.2x avg) + 8-20 UTC session.
    Combines two independently proven winners: Keltner reversion + volume surge.
    Volume at the band = real pressure not noise, should raise WR above plain Keltner."""
    def init(self):
        self.km,self.ku,self.kd=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,1.5)
        self.vol_ma=self.I(sma,self.data.Volume,20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.ku[-1]) or np.isnan(self.vol_ma[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        vol_ok=self.data.Volume[-1]>self.vol_ma[-1]*1.2
        if p<self.kd[-1] and vol_ok and above and not self.position.is_long:
            self.buy(tp=self.km[-1],sl=p*0.97)
        elif p>self.ku[-1] and vol_ok and not above and not self.position.is_short:
            self.sell(tp=self.km[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.km[-1]: self.position.close()
        elif self.position.is_short and p<=self.km[-1]: self.position.close()

class KeltnerEMA200Long(Strategy):
    """Keltner reversion + EMA200 strong trend filter + long only + 8-20 UTC.
    BB_Session_EMA200_Long had 22 winners — does Keltner version beat it?
    EMA200 = only longs in confirmed long-term uptrend."""
    def init(self):
        self.km,self.ku,self.kd=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,1.5)
        self.e200=self.I(ema,self.data.Close,200)
    def next(self):
        if np.isnan(self.ku[-1]) or np.isnan(self.e200[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.kd[-1] and p>self.e200[-1] and not self.position.is_long:
            self.buy(tp=self.km[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.km[-1]: self.position.close()

class KeltnerEMA50Long(Strategy):
    """Keltner reversion + EMA50 trend filter + long only + 8-20 UTC.
    Lighter trend filter than EMA200 — more trades, slightly lower bar."""
    def init(self):
        self.km,self.ku,self.kd=self.I(keltner,self.data.High,self.data.Low,self.data.Close,20,1.5)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.ku[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.kd[-1] and p>self.e50[-1] and not self.position.is_long:
            self.buy(tp=self.km[-1],sl=p*0.97)
        elif self.position.is_long and p>=self.km[-1]: self.position.close()

class BBMorningSession(Strategy):
    """BB reversion 8-14 UTC only (morning session — EU hours, pre-US open).
    Split the 8-20 window to find which half drives the edge.
    If morning WR >> afternoon WR, can tighten the filter further."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<14): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and not self.position.is_short: self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BBAfternoonSession(Strategy):
    """BB reversion 14-20 UTC only (afternoon session — US hours).
    Pair with BBMorningSession — one of these should be carrying the 8-20 edge."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (14<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and not self.position.is_short: self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class ConsecutiveCandle3Session(Strategy):
    """3 same-direction candles = exhaustion → fade + 8-20 UTC + EMA50.
    We have 4 (37 winners) and 5 (25 winners). Does 3 still hold with more trades?
    More trades but possibly lower WR — fills the picture of the exhaustion curve."""
    def init(self): self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        if len(self.data.Close)<5: return
        closes=list(self.data.Close[-4:-1]); opens=list(self.data.Open[-4:-1])
        p=self.data.Close[-1]; above=p>self.e50[-1]
        n_down=all(closes[i]<opens[i] for i in range(3))
        n_up  =all(closes[i]>opens[i] for i in range(3))
        if n_down and above and not self.position.is_long:
            self.buy(tp=p*1.015,sl=p*0.97)
        elif n_up and not above and not self.position.is_short:
            self.sell(tp=p*0.985,sl=p*1.03)
        elif self.position.is_long and p>=p*1.015: self.position.close()
        elif self.position.is_short and p<=p*0.985: self.position.close()

class BBSessionWide(Strategy):
    """BB n=20 k=2.5 (wider bands) + 8-20 UTC session.
    We swept n (10/15/20/25/30). Now sweep k. Wider = rarer signals, higher conviction.
    Tests if the edge improves when only trading the most extreme band touches."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2.5)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and not self.position.is_short: self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class BBSessionNarrow(Strategy):
    """BB n=20 k=1.5 (narrower bands) + 8-20 UTC session.
    Narrower = more signals, lower bar. Completes the k sweep alongside k=2.5."""
    def init(self): self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,1.5)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long: self.buy(tp=self.mid[-1],sl=p*0.97)
        elif p>self.up[-1] and not self.position.is_short: self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class ConsecutiveBBConfirm(Strategy):
    """4 consecutive same-direction candles AND price at BB band + 8-20 UTC + EMA50.
    Double confirmation: exhaustion pattern AND technically at extreme.
    Should be rare but very high conviction — expect fewer trades, higher WR."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,2)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.dn[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        if len(self.data.Close)<6: return
        closes=list(self.data.Close[-5:-1]); opens=list(self.data.Open[-5:-1])
        p=self.data.Close[-1]; above=p>self.e50[-1]
        n_down=all(closes[i]<opens[i] for i in range(4))
        n_up  =all(closes[i]>opens[i] for i in range(4))
        at_lower=p<self.dn[-1]; at_upper=p>self.up[-1]
        if n_down and at_lower and above and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.97)
        elif n_up and at_upper and not above and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.03)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()

class RSISession(Strategy):
    """Pure RSI (< 30 / > 70) + 8-20 UTC session + EMA50. No bands at all.
    Tests whether the edge is in the session filter alone — any indicator will do.
    If this wins, the insight is: session filter > indicator choice."""
    def init(self):
        self.r=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.r[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (8<=hour<20): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        if self.r[-1]<30 and above and not self.position.is_long:
            self.buy(tp=p*1.02,sl=p*0.97)
        elif self.r[-1]>70 and not above and not self.position.is_short:
            self.sell(tp=p*0.98,sl=p*1.03)
        elif self.position.is_long and self.r[-1]>50: self.position.close()
        elif self.position.is_short and self.r[-1]<50: self.position.close()


# ── round 10: asian session strategies (00:00–08:00 UTC) ─────────────────────

class AsianRangeBreakout(Strategy):
    """Mark the high/low of the pre-Asia consolidation window (23:00–01:00 UTC).
    Bet the breakout when Tokyo opens (01:00–04:00 UTC).
    Asian session 'first move' — opposite to mean reversion, catches direction early.
    Low volume means real breakouts are rare but high conviction when they happen."""
    def init(self):
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (1<=hour<4): return
        if len(self.data.High)<24: return
        # Range = last 2 hours of prior session (23:00-01:00 = ~8 bars on 15m)
        lookback=8
        range_high=max(self.data.High[-lookback-1:-1])
        range_low =min(self.data.Low[-lookback-1:-1])
        p=self.data.Close[-1]
        if p>range_high and not self.position.is_long:
            self.buy(tp=p*1.015,sl=range_low)
        elif p<range_low and not self.position.is_short:
            self.sell(tp=p*0.985,sl=range_high)
        elif self.position.is_long and p>=p*1.015: self.position.close()
        elif self.position.is_short and p<=p*0.985: self.position.close()


class AsianBBNarrow(Strategy):
    """BB mean reversion with tighter bands (k=1.5) during Asian hours (00:00–08:00 UTC).
    Asian session = low volume, price drifts and ranges rather than trends.
    Narrower bands catch smaller moves that are more common in low-liquidity Asia.
    Hypothesis: tighter k works in Asia where k=2.0 never gets touched."""
    def init(self):
        self.mid,self.up,self.dn=self.I(bollinger,self.data.Close,20,1.5)
    def next(self):
        if np.isnan(self.dn[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (0<=hour<8): return
        p=self.data.Close[-1]
        if p<self.dn[-1] and not self.position.is_long:
            self.buy(tp=self.mid[-1],sl=p*0.98)
        elif p>self.up[-1] and not self.position.is_short:
            self.sell(tp=self.mid[-1],sl=p*1.02)
        elif self.position.is_long and p>=self.mid[-1]: self.position.close()
        elif self.position.is_short and p<=self.mid[-1]: self.position.close()


class TokyoOpenRSI(Strategy):
    """RSI extreme reversion specifically at Tokyo open (01:00–03:00 UTC).
    Your data shows 02:00–03:00 UTC has 60.5% and 56.1% WR — best Asian hours.
    This isolates that window with RSI to see if the edge is time-based or RSI-based.
    If wins: Tokyo open has structural mean reversion edge worth exploiting."""
    def init(self):
        self.r=self.I(rsi,self.data.Close,14)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.r[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (1<=hour<4): return
        p=self.data.Close[-1]; above=p>self.e50[-1]
        if self.r[-1]<25 and above and not self.position.is_long:
            self.buy(tp=p*1.015,sl=p*0.98)
        elif self.r[-1]>75 and not above and not self.position.is_short:
            self.sell(tp=p*0.985,sl=p*1.02)
        elif self.position.is_long and self.r[-1]>50: self.position.close()
        elif self.position.is_short and self.r[-1]<50: self.position.close()


class AsianVWAPFade(Strategy):
    """VWAP deviation fade during Asian hours (00:00–08:00 UTC).
    When price moves 1.5+ std from VWAP in Asia, fade it back to VWAP.
    Asian low-volume means VWAP deviations snap back faster than in NY session.
    Smaller TP target (1%) reflects the tighter Asian range."""
    def init(self):
        self.vwap=self.I(lambda c,v: np.where(
            np.cumsum(v)>0, np.cumsum(c*v)/np.cumsum(v), c
        ), self.data.Close, self.data.Volume)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.vwap[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (0<=hour<8): return
        if len(self.data.Close)<20: return
        p=self.data.Close[-1]
        recent=np.array(self.data.Close[-20:])
        std=recent.std()
        if std==0: return
        dev=(p-self.vwap[-1])/std
        if dev<-1.5 and not self.position.is_long:
            self.buy(tp=self.vwap[-1],sl=p*0.98)
        elif dev>1.5 and not self.position.is_short:
            self.sell(tp=self.vwap[-1],sl=p*1.02)
        elif self.position.is_long and p>=self.vwap[-1]: self.position.close()
        elif self.position.is_short and p<=self.vwap[-1]: self.position.close()


class AsianVolumeSurge(Strategy):
    """Volume surge breakout during Asian hours (00:00–08:00 UTC).
    Baseline Asian volume is low — when it 2x+ spikes, bet the direction of that spike.
    Works specifically on high-retail alts (XRP, DOGE) where Asian retail drives moves.
    Volume spike = conviction behind the move, not just random drift."""
    def init(self):
        self.vol_ma=self.I(ema,self.data.Volume.astype(float),20)
        self.e50=self.I(ema,self.data.Close,50)
    def next(self):
        if np.isnan(self.vol_ma[-1]) or np.isnan(self.e50[-1]): return
        hour=pd.Timestamp(self.data.index[-1]).hour
        if not (0<=hour<8): return
        if self.vol_ma[-1]==0: return
        p=self.data.Close[-1]
        vol_ratio=self.data.Volume[-1]/self.vol_ma[-1]
        is_bullish=self.data.Close[-1]>self.data.Open[-1]
        is_bearish=self.data.Close[-1]<self.data.Open[-1]
        above=p>self.e50[-1]
        if vol_ratio>2.0 and is_bullish and above and not self.position.is_long:
            self.buy(tp=p*1.015,sl=p*0.98)
        elif vol_ratio>2.0 and is_bearish and not above and not self.position.is_short:
            self.sell(tp=p*0.985,sl=p*1.02)
        elif self.position.is_long and p>=p*1.015: self.position.close()
        elif self.position.is_short and p<=p*0.985: self.position.close()


# ── round 11: new strategies inspired by stock winners ───────────────────────

class DonchianEMA200Long(Strategy):
    """Donchian channel breakout filtered to long-only above 200 EMA.
    Donchian 20 high = buy signal. Exit on Donchian low touch.
    Works on trending stocks/crypto — ride breakouts, ignore mean reversion."""
    def init(self):
        self.dc_high = self.I(lambda h: pd.Series(h).rolling(20).max().values, self.data.High)
        self.dc_low  = self.I(lambda l: pd.Series(l).rolling(20).min().values, self.data.Low)
        self.e200    = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]) or np.isnan(self.dc_high[-2]): return
        p = self.data.Close[-1]
        if p > self.e200[-1]:
            if p >= self.dc_high[-2] and not self.position:
                self.buy(sl=self.dc_low[-1], tp=p * 1.04)
            elif self.position.is_long and p <= self.dc_low[-1]:
                self.position.close()


class ATRBandEMA200Long(Strategy):
    """ATR-based channel breakout above 200 EMA (long only).
    Entry when price breaks above EMA + 2*ATR (momentum confirmation).
    Stop below EMA - 1*ATR. Good for volatile stocks like NVDA, MSTR."""
    def init(self):
        self.e200 = self.I(ema, self.data.Close, 200)
        self.atr  = self.I(atr_calc, self.data.High, self.data.Low, self.data.Close, 14)
    def next(self):
        if np.isnan(self.e200[-1]) or np.isnan(self.atr[-1]): return
        p = self.data.Close[-1]
        band_up = self.e200[-1] + 2 * self.atr[-1]
        sl      = self.e200[-1] - 1 * self.atr[-1]
        if p > self.e200[-1] and p > band_up and not self.position:
            self.buy(sl=max(sl, p * 0.95), tp=p * 1.06)
        elif self.position.is_long and p < self.e200[-1]:
            self.position.close()


class InsideBarEMA200Long(Strategy):
    """Inside bar breakout filtered above 200 EMA (long only).
    Inside bar = current bar range inside prior bar = compression before expansion.
    Buy when price breaks above mother bar high. Good on daily/4h."""
    def init(self):
        self.e200 = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        p = self.data.Close[-1]
        if len(self.data.Close) < 3: return
        # Inside bar: bar[-2] inside bar[-3]
        mother_high = self.data.High[-3]
        mother_low  = self.data.Low[-3]
        inside_high = self.data.High[-2]
        inside_low  = self.data.Low[-2]
        is_inside = inside_high <= mother_high and inside_low >= mother_low
        if is_inside and p > mother_high and p > self.e200[-1] and not self.position:
            sl = mother_low
            tp = p + (p - sl) * 2  # 2:1 R:R
            self.buy(sl=sl, tp=tp)
        elif self.position.is_long and p < self.e200[-1]:
            self.position.close()


class EMACross8_21EMA200Long(Strategy):
    """Fast EMA crossover (8/21) confirmed by 200 EMA trend filter (long only).
    8 EMA crosses above 21 EMA while both are above 200 EMA = entry.
    Exits when 8 EMA crosses back below 21 EMA."""
    def init(self):
        self.e8   = self.I(ema, self.data.Close, 8)
        self.e21  = self.I(ema, self.data.Close, 21)
        self.e200 = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]) or np.isnan(self.e21[-2]): return
        p = self.data.Close[-1]
        cross_up   = self.e8[-2] <= self.e21[-2] and self.e8[-1] > self.e21[-1]
        cross_down = self.e8[-2] >= self.e21[-2] and self.e8[-1] < self.e21[-1]
        if cross_up and p > self.e200[-1] and not self.position:
            self.buy(sl=self.e21[-1] * 0.98, tp=p * 1.06)
        elif cross_down and self.position.is_long:
            self.position.close()


class ORBSession(Strategy):
    """Opening Range Breakout — first 30 min (2 bars on 15m) sets the range.
    Breakout above range high = buy. Breakout below range low = sell.
    Session: 14:30 UTC (NYSE open). Classic institutional momentum strategy."""
    def init(self):
        self.e50 = self.I(ema, self.data.Close, 50)
    def next(self):
        if np.isnan(self.e50[-1]): return
        ts = pd.Timestamp(self.data.index[-1])
        hour, minute = ts.hour, ts.minute
        # Session: 14:30–15:00 UTC = first 2x15m bars = opening range
        # Trade window: 15:00–18:00 UTC
        if not (15 <= hour < 18): return
        # Find today's opening range (14:30–15:00 bar high/low)
        today = ts.date()
        idx = self.data.index
        orb_bars = [i for i, t in enumerate(idx)
                    if pd.Timestamp(t).date() == today and
                    14 <= pd.Timestamp(t).hour < 15]
        if len(orb_bars) < 1: return
        orb_high = max(self.data.High[i] for i in orb_bars)
        orb_low  = min(self.data.Low[i]  for i in orb_bars)
        p = self.data.Close[-1]
        if p > orb_high and not self.position.is_long:
            self.buy(sl=orb_low, tp=p + (orb_high - orb_low) * 2)
        elif p < orb_low and not self.position.is_short:
            self.sell(tp=p - (orb_high - orb_low) * 2, sl=orb_high)
        elif self.position.is_long and hour >= 20:
            self.position.close()  # close at end of session
        elif self.position.is_short and hour >= 20:
            self.position.close()


class Consecutive4EMA200Long(Strategy):
    """4 consecutive down closes (oversold) → buy, only above 200 EMA.
    Based on the Consecutive_3_Session winner but stricter (4 bars) + EMA200 filter.
    Long-only: trend is your friend, only catch dips in uptrends."""
    def init(self):
        self.e200 = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if len(self.data.Close) < 5: return
        p = self.data.Close[-1]
        down4 = all(self.data.Close[-(i+2)] < self.data.Close[-(i+3)]
                    for i in range(4))
        up4   = all(self.data.Close[-(i+2)] > self.data.Close[-(i+3)]
                    for i in range(4))
        above200 = p > self.e200[-1]
        if down4 and above200 and not self.position:
            self.buy(sl=p * 0.97, tp=p * 1.04)
        elif up4 and self.position.is_long:
            self.position.close()


class Consecutive5EMA200Long(Strategy):
    """5 consecutive down closes → buy, only above 200 EMA. Stricter entry = higher WR."""
    def init(self):
        self.e200 = self.I(ema, self.data.Close, 200)
    def next(self):
        if np.isnan(self.e200[-1]): return
        if len(self.data.Close) < 6: return
        p = self.data.Close[-1]
        down5 = all(self.data.Close[-(i+2)] < self.data.Close[-(i+3)]
                    for i in range(5))
        up3   = all(self.data.Close[-(i+2)] > self.data.Close[-(i+3)]
                    for i in range(3))
        above200 = p > self.e200[-1]
        if down5 and above200 and not self.position:
            self.buy(sl=p * 0.96, tp=p * 1.06)
        elif up3 and self.position.is_long:
            self.position.close()


# ─────────────────────────────────────────────
# ROUND 12 — TREND-FOLLOWING 15m (both directions)
# Designed to profit on trending days, not just mean-reversion
# ─────────────────────────────────────────────

class EMACross8_21_Both(Strategy):
    """EMA8 crosses EMA21 — both long and short. No EMA200 filter. Pure momentum."""
    def init(self):
        self.e8  = self.I(ema, self.data.Close, 8)
        self.e21 = self.I(ema, self.data.Close, 21)
    def next(self):
        if np.isnan(self.e8[-1]) or np.isnan(self.e21[-1]): return
        cross_up   = self.e8[-2] <= self.e21[-2] and self.e8[-1] > self.e21[-1]
        cross_down = self.e8[-2] >= self.e21[-2] and self.e8[-1] < self.e21[-1]
        p = self.data.Close[-1]
        if cross_up and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif cross_down and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif cross_down and self.position.is_long:
            self.position.close()
        elif cross_up and self.position.is_short:
            self.position.close()


class EMACross8_21_Session(Strategy):
    """EMA8/21 cross — both directions, 8-20 UTC session only."""
    def init(self):
        self.e8  = self.I(ema, self.data.Close, 8)
        self.e21 = self.I(ema, self.data.Close, 21)
    def next(self):
        if np.isnan(self.e8[-1]) or np.isnan(self.e21[-1]): return
        h = self.data.index[-1].hour
        if not (8 <= h < 20): return
        cross_up   = self.e8[-2] <= self.e21[-2] and self.e8[-1] > self.e21[-1]
        cross_down = self.e8[-2] >= self.e21[-2] and self.e8[-1] < self.e21[-1]
        p = self.data.Close[-1]
        if cross_up and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif cross_down and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif cross_down and self.position.is_long:
            self.position.close()
        elif cross_up and self.position.is_short:
            self.position.close()


class MACDCross_Both(Strategy):
    """MACD histogram crosses zero — both long and short. Momentum crossover."""
    def init(self):
        self.macd_line, self.sig_line = self.I(macd_calc, self.data.Close)
    def next(self):
        hist_prev = self.macd_line[-2] - self.sig_line[-2]
        hist_curr = self.macd_line[-1] - self.sig_line[-1]
        if np.isnan(hist_curr) or np.isnan(hist_prev): return
        p = self.data.Close[-1]
        if hist_prev < 0 and hist_curr > 0 and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif hist_prev > 0 and hist_curr < 0 and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif hist_curr < 0 and self.position.is_long:
            self.position.close()
        elif hist_curr > 0 and self.position.is_short:
            self.position.close()


class MACDCross_Session(Strategy):
    """MACD histogram cross — both directions, 8-20 UTC session only."""
    def init(self):
        self.macd_line, self.sig_line = self.I(macd_calc, self.data.Close)
    def next(self):
        h = self.data.index[-1].hour
        if not (8 <= h < 20): return
        hist_prev = self.macd_line[-2] - self.sig_line[-2]
        hist_curr = self.macd_line[-1] - self.sig_line[-1]
        if np.isnan(hist_curr) or np.isnan(hist_prev): return
        p = self.data.Close[-1]
        if hist_prev < 0 and hist_curr > 0 and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif hist_prev > 0 and hist_curr < 0 and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif hist_curr < 0 and self.position.is_long:
            self.position.close()
        elif hist_curr > 0 and self.position.is_short:
            self.position.close()


class SupertrendFlip_Both(Strategy):
    """Supertrend direction flip — both long and short. Fires on flip candle only."""
    def init(self):
        self.st = self.I(supertrend, self.data.High, self.data.Low, self.data.Close, 10, 3)
    def next(self):
        if np.isnan(self.st[-1]) or np.isnan(self.st[-2]): return
        p = self.data.Close[-1]
        if self.st[-2] == -1 and self.st[-1] == 1 and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif self.st[-2] == 1 and self.st[-1] == -1 and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif self.st[-1] == -1 and self.position.is_long:
            self.position.close()
        elif self.st[-1] == 1 and self.position.is_short:
            self.position.close()


class SupertrendFlip_Session(Strategy):
    """Supertrend flip — both directions, 8-20 UTC session only."""
    def init(self):
        self.st = self.I(supertrend, self.data.High, self.data.Low, self.data.Close, 10, 3)
    def next(self):
        h = self.data.index[-1].hour
        if not (8 <= h < 20): return
        if np.isnan(self.st[-1]) or np.isnan(self.st[-2]): return
        p = self.data.Close[-1]
        if self.st[-2] == -1 and self.st[-1] == 1 and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif self.st[-2] == 1 and self.st[-1] == -1 and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif self.st[-1] == -1 and self.position.is_long:
            self.position.close()
        elif self.st[-1] == 1 and self.position.is_short:
            self.position.close()


class HHLL_Trend_Both(Strategy):
    """3 consecutive HH+HL = long, 3 consecutive LL+LH = short. Pure price structure."""
    def init(self): pass
    def next(self):
        if len(self.data.Close) < 5: return
        highs = self.data.High
        lows  = self.data.Low
        hh = highs[-1] > highs[-2] > highs[-3]
        hl = lows[-1]  > lows[-2]  > lows[-3]
        ll = lows[-1]  < lows[-2]  < lows[-3]
        lh = highs[-1] < highs[-2] < highs[-3]
        p  = self.data.Close[-1]
        if hh and hl and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif ll and lh and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif (ll or lh) and self.position.is_long:
            self.position.close()
        elif (hh or hl) and self.position.is_short:
            self.position.close()


class VolumeBreakout_Both(Strategy):
    """Price breaks 20-bar high/low on 2x avg volume — both directions."""
    def init(self):
        self.vol_ma = self.I(lambda v: pd.Series(v).rolling(20).mean().values, self.data.Volume)
    def next(self):
        if len(self.data.Close) < 22: return
        if np.isnan(self.vol_ma[-1]): return
        close   = self.data.Close[-1]
        vol     = self.data.Volume[-1]
        high_20 = max(self.data.High[-21:-1])
        low_20  = min(self.data.Low[-21:-1])
        surge   = vol > self.vol_ma[-1] * 2.0
        p = close
        if close > high_20 and surge and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif close < low_20 and surge and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif close < high_20 * 0.99 and self.position.is_long:
            self.position.close()
        elif close > low_20 * 1.01 and self.position.is_short:
            self.position.close()


class ADXTrend_Both(Strategy):
    """ADX > 25 (trending) + DI direction — both long and short."""
    def init(self):
        self.adx_v, self.pdi, self.mdi = self.I(
            adx_calc, self.data.High, self.data.Low, self.data.Close, 14
        )
    def next(self):
        if np.isnan(self.adx_v[-1]): return
        trending = self.adx_v[-1] > 25
        p = self.data.Close[-1]
        if trending and self.pdi[-1] > self.mdi[-1] and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif trending and self.mdi[-1] > self.pdi[-1] and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif self.mdi[-1] > self.pdi[-1] and self.position.is_long:
            self.position.close()
        elif self.pdi[-1] > self.mdi[-1] and self.position.is_short:
            self.position.close()


class ADXTrend_Session(Strategy):
    """ADX > 25 + DI direction — both directions, 8-20 UTC session only."""
    def init(self):
        self.adx_v, self.pdi, self.mdi = self.I(
            adx_calc, self.data.High, self.data.Low, self.data.Close, 14
        )
    def next(self):
        h = self.data.index[-1].hour
        if not (8 <= h < 20): return
        if np.isnan(self.adx_v[-1]): return
        trending = self.adx_v[-1] > 25
        p = self.data.Close[-1]
        if trending and self.pdi[-1] > self.mdi[-1] and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif trending and self.mdi[-1] > self.pdi[-1] and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif self.mdi[-1] > self.pdi[-1] and self.position.is_long:
            self.position.close()
        elif self.pdi[-1] > self.mdi[-1] and self.position.is_short:
            self.position.close()


class ROCMomentum_Both(Strategy):
    """Rate of Change crosses zero — both directions. Simplest momentum signal."""
    def init(self):
        self.roc_v = self.I(roc, self.data.Close, 12)
    def next(self):
        if np.isnan(self.roc_v[-1]) or np.isnan(self.roc_v[-2]): return
        p = self.data.Close[-1]
        if self.roc_v[-2] < 0 and self.roc_v[-1] > 0 and not self.position:
            self.buy(sl=p*0.97, tp=p*1.03)
        elif self.roc_v[-2] > 0 and self.roc_v[-1] < 0 and not self.position:
            self.sell(sl=p*1.03, tp=p*0.97)
        elif self.roc_v[-1] < 0 and self.position.is_long:
            self.position.close()
        elif self.roc_v[-1] > 0 and self.position.is_short:
            self.position.close()


# ─────────────────────────────────────────────
# STRATEGY REGISTRY
# ─────────────────────────────────────────────

STRATEGIES = [
    (GoldenCross,          "GoldenCross_SMA_50_200",      300),
    (EMAGoldenCross,       "EMAGoldenCross_50_200",        300),
    (TripleEMATrend,       "TripleEMA_8_21_55",            100),
    (RSI2MeanReversion,    "RSI2_MeanReversion",           300),
    (RSIMATrend,           "RSI_MA_Trend",                 300),
    (RSIDivergenceSimple,  "RSI_Divergence_200EMA",        300),
    (MACDCrossover,        "MACD_Crossover",               100),
    (MACD200EMA,           "MACD_200EMA_Filter",           300),
    (BollingerReversion,   "Bollinger_Reversion",           50),
    (MeanReversionBB,      "MeanReversion_BB_200EMA",      300),
    (SqueezeMomentum,      "Squeeze_Momentum_TTM",         100),
    (DonchianBreakout,     "Donchian_Breakout_20",          50),
    (SupertrendSingle,     "Supertrend_10_3",               50),
    (SupertrendTriple,     "Supertrend_Triple",             50),
    (IchimokuKumo,         "Ichimoku_Kumo",                100),
    (ADXTrend,             "ADX_DI_Trend",                 100),
    (StochRSIStrategy,     "StochRSI_Crossover",           100),
    (WilliamsRStrategy,    "WilliamsR_200EMA",             300),
    (CCIStrategy,          "CCI_100_Cross",                 50),
    (ROCMomentum,          "ROC_Momentum_12",               50),
    (OBVTrend,             "OBV_EMA_Trend",                100),
    (HeikinAshiTrend,      "HeikinAshi_Trend",              50),
    (EMA50Pullback,        "EMA50_Pullback",               100),
    (VWAPMeanReversion,    "VWAP_MeanReversion",           100),
    (KeltnerBreakout,      "Keltner_Breakout",             100),
    (EMACloud,             "EMA_Cloud_8_13_21_55",         100),
    (ChandelierExit,       "Chandelier_Exit",              100),
    (PriceMomentum,        "Price_Momentum_20_200",        300),
    (HigherHighStrategy,   "MarketStructure_HH_LL",        100),
    (VolumeSurge,          "Volume_Surge_Breakout",         50),
    (ConnorsRSIStrategy,   "ConnorsRSI_MeanReversion",     300),
    (ZScoreReversion,      "ZScore_Reversion_200EMA",      300),
    (BBPercentB,           "BB_PercentB_200EMA",           300),
    (StochasticCrossover,  "Stochastic_Crossover_200EMA",  300),
    (StretchScore,              "StretchScore_Custom",          300),
    (StretchScoreFast,              "StretchScore_Fast_EMA50",        50),
    (BollingerReversionTrend,       "Bollinger_Reversion_Trend",      50),
    (BollingerRSIConfirm,           "Bollinger_RSI_Confirm",          50),
    (StretchScoreNoTrend,           "StretchScore_NoTrend",           50),
    # ── crack 70% WR on 15m ──────────────────────────────────────────
    (BollingerReversionRSILong,     "BB_RSI_LongOnly",                50),
    (BollingerReversionWide,        "BB_Wide_25std",                  50),
    (BollingerReversionSession,     "BB_Session_8_20_UTC",            50),
    (StretchScoreFastStrict,        "StretchScore_Strict_25std",      50),
    (BollingerReversionDoubleClose, "BB_DoubleClose",                 50),
    (BollingerReversionRejection,   "BB_Rejection_Hammer",            50),
    (BollingerReversionStochRSI,    "BB_StochRSI_Extreme",            50),
    (BollingerReversionWideSession, "BB_Wide_Session_Combined",       50),
    # ── final round — stack best filters ─────────────────────────────
    (BollingerSessionRSILong,       "BB_Session_RSI_Long",            50),
    (BollingerUSSession,            "BB_US_Session_1320_UTC",         50),
    (BollingerSessionRejection,     "BB_Session_Rejection",           50),
    (BollingerSessionWide,          "BB_Session_Wide_25std",          50),
    # ── new: 1h/4h improvements ──────────────────────────────────────
    (StretchScoreSession,           "StretchScore_Session_8_20",      50),
    (StretchScoreCapitulation,      "StretchScore_Capitulation",      50),
    (VWAPZScoreReversion,           "VWAP_ZScore_Reversion",          50),
    (ConnorsRSISession,             "ConnorsRSI_Session_8_20",        50),
    (AdaptiveBBReversion,           "AdaptiveBB_ATR_Reversion",       50),
    (StretchScore1hRelaxed,         "StretchScore_1h_Relaxed",        50),
    # ── round 3: stack winners + consensus ───────────────────────────
    (AdaptiveBBSession,             "AdaptiveBB_Session_8_20",        50),
    (AdaptiveBBUSSession,           "AdaptiveBB_US_Session_1320",     50),
    (StretchScoreCapitulationSess,  "StretchScore_Capitul_Session",   50),
    (VWAPSession,                   "VWAP_Session_8_20",              50),
    (ConsensusBBAdaptive,           "Consensus_BB_AND_Adaptive",      50),
    # ── round 4: final + param variants ──────────────────────────────
    (BollingerLondonOpen,           "BB_London_Open_6_9_UTC",         50),
    (ConsensusAdaptiveVWAP,         "Consensus_Adaptive_AND_VWAP",    50),
    (ConsensusConnorsAdaptive,      "Consensus_ConnorsRSI_Adaptive",  50),
    (StretchScoreCapitulationRelaxed, "StretchScore_Capitul_RSI40",   50),
    (AdaptiveBBLongOnly,            "AdaptiveBB_LongOnly",            50),
    (BBSession_Period15,            "BB_Session_Period15",            50),
    (BBSession_Period25,            "BB_Session_Period25",            50),
    (AdaptiveBB_ATR10,              "AdaptiveBB_ATR10",               50),
    (AdaptiveBB_ATR20,              "AdaptiveBB_ATR20",               50),
    # ── round 5: price action — fibonacci / fvg / order blocks ───────
    (FibonacciGoldenZone,           "Fibonacci_GoldenZone_50_618",   100),
    (FibonacciGoldenZoneLong,       "Fibonacci_GoldenZone_LongOnly", 100),
    (FibonacciRetracement382,       "Fibonacci_382_LongOnly",        100),
    (FairValueGap,                  "FairValueGap_EMA200",            50),
    (FairValueGapLong,              "FairValueGap_LongOnly_EMA50",    50),
    (OrderBlock,                    "OrderBlock_EMA200",             100),
    (OrderBlockLong,                "OrderBlock_LongOnly_EMA50",     100),
    # ── round 6: data trader + tradinglab strategies ──────────────────
    (MACDParabolicSAR200EMA,        "MACD_SAR_200EMA_Triple",        100),
    (MACD200EMA_ZeroLine,           "MACD_200EMA_ZeroLine",          100),
    (DEMASupertrendLong,            "DEMA200_Supertrend_LongOnly",   200),
    (StochasticCrossback200EMA,     "Stochastic_Crossback_200EMA",   100),
    (ParabolicSAR200EMA,            "ParabolicSAR_200EMA",           100),
    (SupertrendEMA200,              "Supertrend_200EMA",             100),
    (TripleConfirmStochRSIMACD,     "TripleConfirm_Stoch_RSI_MACD",  100),
    (TripleEMAPullback,             "TripleEMA_Pullback_25_50_100",  100),
    (ThreeBarPattern,               "ThreeBar_Pattern",               50),
    (ABCPatternBreakout,            "ABC_Pattern_Breakout",          100),
    (WilliamsAlligator,             "Williams_Alligator",            100),
    (RSIHiddenDivergence,           "RSI_Hidden_Divergence",         100),
    # ── round 7: new strategies ───────────────────────────────────────
    (EngulfingCandle,               "Engulfing_Candle_200EMA",        50),
    (HammerShootingStar,            "Hammer_ShootingStar_200EMA",     50),
    (EMARibbon,                     "EMA_Ribbon_8_13_21_34_55",       50),
    (PivotPointBounce,              "PivotPoint_S1_R1_Bounce",        50),
    (VWAPConsecutiveClose,          "VWAP_Consecutive_Close_3",       50),
    (RSIBBCombo,                    "RSI_BB_Combo_Dual_Confirm",      50),
    (GapFill,                       "GapFill_0.3pct_EMA50",           50),
    # ── round 8: new strategies + smart modifications ─────────────────
    (KeltnerReversionSession,       "Keltner_Reversion_Session",      50),
    (KeltnerReversionSessionLong,   "Keltner_Reversion_Session_Long", 50),
    (DoubleBBSession,               "DoubleBB_Session_Confirm",       50),
    (DoubleBBSessionLong,           "DoubleBB_Session_Long",          50),
    (ConsecutiveCandleSession,      "Consecutive_4_Session",          50),
    (ConsecutiveCandle5Session,     "Consecutive_5_Session",          50),
    (RSIExtremeSession,             "RSI_Extreme_15_85_Session",      50),
    (RSIExtremeSessionLong,         "RSI_Extreme_Session_Long",       50),
    (BBVolumeSession,               "BB_Volume_Session",              50),
    (BBVolumeSessionLong,           "BB_Volume_Session_Long",         50),
    (BBSession_Period10,            "BB_Session_Period10",            50),
    (BBSession_Period30,            "BB_Session_Period30",            50),
    (BBSession_EMA50_LongOnly,      "BB_Session_EMA50_LongOnly",      50),
    (BBSession_EMA200_LongOnly,     "BB_Session_EMA200_LongOnly",     50),
    # ── round 9: keltner variants + session splits + consecutive combos ──
    (KeltnerVolumeSession,          "Keltner_Volume_Session",         50),
    (KeltnerEMA200Long,             "Keltner_EMA200_Long",            50),
    (BBMorningSession,              "BB_Morning_8_14_UTC",            50),
    (BBAfternoonSession,            "BB_Afternoon_14_20_UTC",         50),
    (ConsecutiveCandle3Session,     "Consecutive_3_Session",          50),
    (BBSessionWide,                 "BB_Session_Wide_k25",            50),
    (BBSessionNarrow,               "BB_Session_Narrow_k15",          50),
    (ConsecutiveBBConfirm,          "Consecutive_BB_Confirm",         50),
    (RSISession,                    "RSI_Session_30_70",              50),
    (KeltnerEMA50Long,              "Keltner_EMA50_Long",             50),
    # ── round 10: asian session strategies (00:00–08:00 UTC) ─────────────
    (AsianRangeBreakout,            "Asian_Range_Breakout_0104",      50),
    (AsianBBNarrow,                 "Asian_BB_Narrow_k15",            50),
    (TokyoOpenRSI,                  "Tokyo_Open_RSI_0103",            50),
    (AsianVWAPFade,                 "Asian_VWAP_Fade_1p5std",         50),
    (AsianVolumeSurge,              "Asian_Volume_Surge_2x",          50),
    # ── round 11: trend breakout + price action (stocks + forex) ─────────
    (DonchianEMA200Long,            "Donchian_EMA200_Long",          200),
    (ATRBandEMA200Long,             "ATR_Band_EMA200_Long",          200),
    (InsideBarEMA200Long,           "InsideBar_EMA200_Long",         200),
    (EMACross8_21EMA200Long,        "EMA_Cross_8_21_EMA200_Long",    200),
    (ORBSession,                    "ORB_Session_1430_UTC",           50),
    (Consecutive4EMA200Long,        "Consecutive_4_EMA200_Long",     200),
    (Consecutive5EMA200Long,        "Consecutive_5_EMA200_Long",     200),
    # ── round 12: trend-following both directions (15m focus) ─────────────
    (EMACross8_21_Both,             "EMA_Cross_8_21_Both",            50),
    (EMACross8_21_Session,          "EMA_Cross_8_21_Session",         50),
    (MACDCross_Both,                "MACD_Cross_Both",                50),
    (MACDCross_Session,             "MACD_Cross_Session",             50),
    (SupertrendFlip_Both,           "Supertrend_Flip_Both",           50),
    (SupertrendFlip_Session,        "Supertrend_Flip_Session",        50),
    (HHLL_Trend_Both,               "HH_HL_Trend_Both",               50),
    (VolumeBreakout_Both,           "Volume_Breakout_Both",           50),
    (ADXTrend_Both,                 "ADX_Trend_Both",                 50),
    (ADXTrend_Session,              "ADX_Trend_Session",              50),
    (ROCMomentum_Both,              "ROC_Momentum_Both",              50),
]


# ─────────────────────────────────────────────
# BACKTEST RUNNER — all metrics
# ─────────────────────────────────────────────

def _safe(stats, key, default=0):
    val = stats.get(key, default)
    try:
        return default if (val is None or (isinstance(val, float) and np.isnan(val))) else val
    except Exception:
        return default

def run_bt(df: pd.DataFrame, cls, source: str, asset: str, tf: str) -> dict | None:
    try:
        if len(df) < MIN_BARS:
            return None
        cash = 1_000_000 if 'BTC' in asset else 100_000
        bt = Backtest(df, cls, cash=cash, commission=0.001, exclusive_orders=True)
        stats = bt.run()
        n_trades  = int(_safe(stats, '# Trades', 0))
        span_days = (df.index[-1] - df.index[0]).days
        if n_trades < MIN_TRADES or span_days < MIN_SPAN_DAYS:
            return None
        bh  = (df['Close'].iloc[-1] / df['Close'].iloc[0] - 1) * 100
        ret = float(_safe(stats, 'Return [%]', 0))

        # ── Walk-forward: 75% train / 25% test ──────────────────────
        wr_train = wr_test = trades_train = trades_test = 0
        split = int(len(df) * 0.75)
        df_train, df_test = df.iloc[:split], df.iloc[split:]
        try:
            if len(df_train) >= MIN_BARS:
                st = Backtest(df_train, cls, cash=cash, commission=0.001, exclusive_orders=True).run()
                trades_train = int(_safe(st, '# Trades', 0))
                if trades_train >= 10:
                    wr_train = round(float(_safe(st, 'Win Rate [%]', 0) or 0), 1)
        except Exception: pass
        try:
            if len(df_test) >= 50:
                st = Backtest(df_test, cls, cash=cash, commission=0.001, exclusive_orders=True).run()
                trades_test = int(_safe(st, '# Trades', 0))
                if trades_test >= 5:
                    wr_test = round(float(_safe(st, 'Win Rate [%]', 0) or 0), 1)
        except Exception: pass

        # ── Monthly + day-of-week breakdown ─────────────────────────
        worst_month_wr = best_month_wr = avg_month_wr = 0.0
        best_dow = worst_dow = ''
        try:
            tdf = stats._trades.copy()
            if len(tdf) >= 10:
                tdf['Win']   = tdf['ReturnPct'] > 0
                tdf['Month'] = pd.to_datetime(tdf['EntryTime']).dt.to_period('M')
                tdf['DOW']   = pd.to_datetime(tdf['EntryTime']).dt.day_name()
                monthly = tdf.groupby('Month')['Win'].mean() * 100
                worst_month_wr = round(float(monthly.min()), 1)
                best_month_wr  = round(float(monthly.max()), 1)
                avg_month_wr   = round(float(monthly.mean()), 1)
                dow = tdf.groupby('DOW')['Win'].mean() * 100
                best_dow  = str(dow.idxmax())
                worst_dow = str(dow.idxmin())
        except Exception: pass

        return {
            'Source':           source,
            'Strategy':         cls.__name__,
            'Asset':            asset,
            'Timeframe':        tf,
            'Return_%':         round(ret, 2),
            'Return_Ann_%':     round(float(_safe(stats, 'Return (Ann.) [%]', 0)), 2),
            'BuyHold_%':        round(bh, 2),
            'vs_BuyHold':       round(ret - bh, 2),
            'Sharpe':           round(float(_safe(stats, 'Sharpe Ratio', 0) or 0), 3),
            'Sortino':          round(float(_safe(stats, 'Sortino Ratio', 0) or 0), 3),
            'Calmar':           round(float(_safe(stats, 'Calmar Ratio', 0) or 0), 3),
            'Volatility_Ann_%': round(float(_safe(stats, 'Volatility (Ann.) [%]', 0) or 0), 2),
            'MaxDD_%':          round(float(_safe(stats, 'Max. Drawdown [%]', 0)), 2),
            'AvgDD_%':          round(float(_safe(stats, 'Avg. Drawdown [%]', 0) or 0), 2),
            'MaxDD_Duration':   str(_safe(stats, 'Max. Drawdown Duration', '')),
            'AvgDD_Duration':   str(_safe(stats, 'Avg. Drawdown Duration', '')),
            'WinRate_%':        round(float(_safe(stats, 'Win Rate [%]', 0) or 0), 1),
            'Trades':           n_trades,
            'BestTrade_%':      round(float(_safe(stats, 'Best Trade [%]', 0) or 0), 2),
            'WorstTrade_%':     round(float(_safe(stats, 'Worst Trade [%]', 0) or 0), 2),
            'AvgTrade_%':       round(float(_safe(stats, 'Avg. Trade [%]', 0) or 0), 3),
            'AvgWinTrade_%':    round(float(_safe(stats, 'Avg. Winning Trade [%]', 0) or 0), 2),
            'AvgLossTrade_%':   round(float(_safe(stats, 'Avg. Losing Trade [%]', 0) or 0), 2),
            'MaxTradeDuration': str(_safe(stats, 'Max. Trade Duration', '')),
            'AvgTradeDuration': str(_safe(stats, 'Avg. Trade Duration', '')),
            'ProfitFactor':     round(float(_safe(stats, 'Profit Factor', 0) or 0), 3),
            'Expectancy_%':     round(float(_safe(stats, 'Expectancy [%]', 0) or 0), 3),
            'SQN':              round(float(_safe(stats, 'SQN', 0) or 0), 3),
            'CompoundPerTrade_%': round(((1 + ret/100) ** (1/max(n_trades,1)) - 1) * 100, 4),
            'Span_Days':        span_days,
            'Start':            str(df.index[0].date()),
            'End':              str(df.index[-1].date()),
            # ── Walk-forward ────────────────────────────────────────
            'WR_Train_%':       wr_train,
            'Trades_Train':     trades_train,
            'WR_Test_%':        wr_test,
            'Trades_Test':      trades_test,
            # ── Monthly / DOW breakdown ──────────────────────────────
            'Worst_Month_WR_%': worst_month_wr,
            'Best_Month_WR_%':  best_month_wr,
            'Avg_Month_WR_%':   avg_month_wr,
            'Best_DOW':         best_dow,
            'Worst_DOW':        worst_dow,
            # ── Reality check note ───────────────────────────────────
            'Entry_Note':       'signal-bar close (real entry = next open, ~0.5-1% worse)',
        }
    except Exception:
        return None


# ─────────────────────────────────────────────
# WINNER CLASSIFICATION
# ─────────────────────────────────────────────

def is_polymarket_winner(r: dict) -> bool:
    return r['WinRate_%'] >= WIN_POLYMARKET_WR and r['Trades'] >= MIN_TRADES

def is_trading_winner(r: dict) -> bool:
    """Best PnL strategies — annualised return >= 30%/yr AND Sharpe >= 0.5.
    Uses Return_Ann_% so a fast 15m strategy is judged fairly against a slow 1d one."""
    return (r['Return_Ann_%'] >= WIN_TRADING_ANN_RET and
            r['Sharpe'] >= WIN_TRADING_SHARPE and
            r['Trades'] >= MIN_TRADES)

def is_winner(r: dict) -> bool:
    return is_polymarket_winner(r) or is_trading_winner(r)


# ─────────────────────────────────────────────
# SAVE HELPERS
# ─────────────────────────────────────────────

def update_master(new_rows: list):
    if not new_rows:
        return
    df_new = pd.DataFrame(new_rows)
    if os.path.exists(MASTER_FILE):
        df_existing = pd.read_csv(MASTER_FILE)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined = df_combined.drop_duplicates(
            subset=['Source','Strategy','Asset','Timeframe'], keep='last')
    else:
        df_combined = df_new
    df_combined.sort_values(['Source','WinRate_%'], ascending=[True,False], inplace=True)
    df_combined.to_csv(MASTER_FILE, index=False)

NEAR_WINNER_WR  = 55.0   # Near-miss threshold — saved for future review

def save_winners(all_results: list):
    if not all_results:
        return
    df = pd.DataFrame(all_results)

    pm   = df[df.apply(is_polymarket_winner, axis=1)].sort_values('WinRate_%', ascending=False)
    trd  = df[df.apply(is_trading_winner,    axis=1)].sort_values('Sharpe',    ascending=False)
    near = df[(df['WinRate_%'] >= NEAR_WINNER_WR) &
              (df['WinRate_%'] <  WIN_POLYMARKET_WR) &
              (df['Trades']    >= MIN_TRADES)].sort_values('WinRate_%', ascending=False)

    pm_path   = f"{POLYMARKET_DIR}/winners_polymarket.csv"
    trd_path  = f"{TRADING_DIR}/winners_trading.csv"
    near_path = f"{OUTPUT_DIR}/near_winners.csv"

    # Merge with existing so both crypto and stocks winners accumulate
    for path, new_df, sort_col in [
        (pm_path,   pm,   'WinRate_%'),
        (trd_path,  trd,  'CompoundPerTrade_%'),
        (near_path, near, 'WinRate_%'),
    ]:
        if os.path.exists(path) and not new_df.empty:
            existing = pd.read_csv(path)
            new_df = pd.concat([existing, new_df]).drop_duplicates(
                subset=['Source','Strategy','Asset','Timeframe'], keep='last'
            ).sort_values(sort_col, ascending=False)
        if not new_df.empty:
            new_df.to_csv(path, index=False)
            label = ("Polymarket" if "polymarket" in path else
                     "Near-miss"  if "near"       in path else "Trading")
            print(f"  {label:10s}: {path}  ({len(new_df)} entries)")


# ─────────────────────────────────────────────
# SECTION RUNNER
# ─────────────────────────────────────────────

def run_section(source: str, assets, timeframes, fetch_fn, tested_cache: set) -> list:
    """Runs all strategy/asset/timeframe combos for one section (crypto or stocks)."""
    results  = []
    total    = len(assets) * len(timeframes) * len(STRATEGIES)
    skipped  = sum(
        1 for a in assets for tf in timeframes
        for _, name, _ in STRATEGIES
        if f"{source}|{a}|{tf}|{name}" in tested_cache
    )
    print(f"\n{'='*80}")
    print(f"{source.upper()}")
    print(f"  {len(assets)} assets x {len(timeframes)} timeframes x {len(STRATEGIES)} strategies = {total} tests")
    print(f"  Cached: {skipped} skipped | To run: {total - skipped}")
    print('='*80)

    done = 0
    for asset in assets:
        for tf in timeframes:
            tf_key = tf if source == "Stocks" else tf  # crypto uses dict lookup below
            print(f"\n  Fetching {asset} {tf}...", end=" ", flush=True)

            if source == "Crypto":
                df = fetch_fn(asset, CRYPTO_TFS[tf])
            else:
                df = fetch_fn(asset, tf)

            if df is None:
                print("SKIP (no data)")
                done += len(STRATEGIES)
                continue
            print(f"{len(df)} bars ({df.index[0].date()} -> {df.index[-1].date()})")

            for cls, name, min_bars in STRATEGIES:
                done += 1
                cache_key = f"{source}|{asset}|{tf}|{name}"
                if cache_key in tested_cache:
                    continue
                if len(df) < min_bars:
                    save_to_cache(source, asset, tf, name, "skip_bars")
                    tested_cache.add(cache_key)
                    continue

                r = run_bt(df, cls, source, asset, tf)
                if r:
                    r['Strategy'] = name
                    results.append(r)
                    pm  = is_polymarket_winner(r)
                    trd = is_trading_winner(r)
                    tag = (" [PM+TRADE]" if pm and trd else
                           " [POLYMARKET]" if pm else
                           " [TRADING]" if trd else "")
                    save_to_cache(source, asset, tf, name,
                                  "winner" if (pm or trd) else "tested")
                else:
                    save_to_cache(source, asset, tf, name, "no_trades")

                tested_cache.add(cache_key)

                if r:
                    pct = done / total * 100
                    print(f"    [{pct:4.0f}%] {name:<35} | "
                          f"AnnRet:{r['Return_Ann_%']:>6.1f}% | "
                          f"CPT:{r['CompoundPerTrade_%']:>6.3f}% | "
                          f"Sharpe:{r['Sharpe']:>5.2f} | "
                          f"WR:{r['WinRate_%']:>5.1f}% | DD:{r['MaxDD_%']:>6.1f}% | "
                          f"PF:{r['ProfitFactor']:>5.2f} | T:{r['Trades']:>3}{tag}")
    return results


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    tested_cache = load_cache()
    print(f"\nFactory — single cache with {len(tested_cache)} completed tests")
    print(f"Polymarket filter : WinRate >= {WIN_POLYMARKET_WR}%")
    print(f"Trading filter    : Annualised return >= {WIN_TRADING_ANN_RET}%/yr AND Sharpe >= {WIN_TRADING_SHARPE}")

    # ── CRYPTO ───────────────────────────────────────────────────────
    crypto_results = run_section(
        "Crypto", CRYPTO_ASSETS, list(CRYPTO_TFS.keys()),
        fetch_crypto, tested_cache
    )

    if crypto_results:
        path = f"{OUTPUT_DIR}/crypto_full_{DATE_STR}.csv"
        pd.DataFrame(crypto_results).sort_values('WinRate_%', ascending=False).to_csv(path, index=False)
        print(f"\nCrypto full results: {path}  ({len(crypto_results)} rows)")

    # ── STOCKS ───────────────────────────────────────────────────────
    stock_results = run_section(
        "Stocks", STOCK_ASSETS, STOCK_TFS,
        fetch_stock, tested_cache
    )

    if stock_results:
        path = f"{STOCKS_DIR}/stocks_full_{DATE_STR}.csv"
        pd.DataFrame(stock_results).sort_values('WinRate_%', ascending=False).to_csv(path, index=False)
        print(f"\nStocks full results: {path}  ({len(stock_results)} rows)")

    # ── COMBINED SAVE ────────────────────────────────────────────────
    all_results = crypto_results + stock_results
    print(f"\nSaving winners...")
    save_winners(all_results)
    update_master(all_results)

    # ── SUMMARY ──────────────────────────────────────────────────────
    if all_results:
        df = pd.DataFrame(all_results)
        pm_count  = df[df.apply(is_polymarket_winner, axis=1)].shape[0]
        trd_count = df[df.apply(is_trading_winner,    axis=1)].shape[0]
        print(f"\n{'='*80}")
        print(f"TOTAL THIS RUN: {len(all_results)} new tests | "
              f"{pm_count} Polymarket | {trd_count} Trading winners")
        top = df[df.apply(is_winner, axis=1)].sort_values('CompoundPerTrade_%', ascending=False)
        if not top.empty:
            cols = ['Source','Strategy','Asset','Timeframe',
                    'WinRate_%','Return_Ann_%','CompoundPerTrade_%',
                    'Sharpe','ProfitFactor','Trades']
            print(top[cols].head(20).to_string(index=False))

    print(f"\nOutput:")
    print(f"  Polymarket : {POLYMARKET_DIR}/winners_polymarket.csv")
    print(f"  Trading    : {TRADING_DIR}/winners_trading.csv")
    print(f"  Stocks     : {STOCKS_DIR}/")
    print(f"  Master     : {MASTER_FILE}")
    print(f"  Cache      : {CACHE_FILE}")
    print(f"\nAdd strategies to STRATEGIES list or assets to CRYPTO/STOCK_ASSETS")
    print(f"and re-run — only new combos will be tested.")


if __name__ == "__main__":
    main()
