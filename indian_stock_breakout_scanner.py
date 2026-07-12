"""
╔══════════════════════════════════════════════════════════════════════════════╗
║        NSE F&O SCANNER  (Kite API)  — v6.0                                  ║
║        BREAKOUT  |  BOS CONTINUATION  |  CHoCH REVERSAL                     ║
║        Intraday / Options Refined                                            ║
║                                                                              ║
║  Data source: Kite Connect historical_data + SQLite cache                    ║
║  Replaces yfinance with kiteconnect for real-time scanning.                  ║
║                                                                              ║
║  THREE DISPLAY TABLES per run:                                               ║
║                                                                              ║
║  TABLE 1 — BREAKOUT  (Momentum entries)                                      ║
║    ORB breach + volume surge happening NOW                                   ║
║    Best for: aggressive CE/PE on confirmation, scalps                        ║
║    Risk: IV expanding, wider stops needed                                    ║
║                                                                              ║
║  TABLE 2 — BOS CONTINUATION  (Pullback entries)                             ║
║    Confirmed BOS (1–5 bars ago) + price pulled back to broken structure     ║
║    + resumption signal firing                                                ║
║    Best for: options at low IV, tight stop at BOS level, 1:3+ R:R           ║
║    Risk: BOS invalidation if price closes below level                        ║
║                                                                              ║
║  TABLE 3 — CHoCH REVERSAL  (Counter-trend entries)                          ║
║    Prior downtrend broken: first higher high after sequence of lower highs  ║
║    + 5-min structure shift + volume confirmation                             ║
║    Best for: reversal CE, catching bottom of intraday selloff                ║
║    Risk: counter-trend, needs strict stop at CHoCH low                      ║
║                                                                              ║
║  INTRADAY / OPTIONS REFINEMENTS:                                             ║
║    • Proper ZigZag swing point detection (lookback=3)                       ║
║    • BOS level stored → PBS within 1.5×ATR → resumption confirmed          ║
║    • CHoCH: 5-min HL + HH flip after LH+LL sequence                        ║
║    • Intraday RVOL: time-normalised vs 20-day avg                           ║
║    • IV guard: penalty if day move > 3.5% (options already expensive)      ║
║    • Time gate: prime 09:30–11:30 & 13:30–14:00 IST, decay after 14:00    ║
║    • Supertrend (daily): hard filter for BOS/Breakout, inverted for CHoCH   ║
║    • CHoCH requires prior bearish structure (LH+LL on 5m) to be valid      ║
║    • BOS invalidation: close below BOS level = false breakout, skipped     ║
║    • VWAP: position-state filter (above/below), not cross-event             ║
║    • VWAP stretch penalty: ATR-relative, not flat percentage                ║
╚══════════════════════════════════════════════════════════════════════════════╝

Requires:
    pip install kiteconnect pandas numpy tabulate colorama

Run:
    python indian_stock_breakout_scanner.py                       # ALL_FNO, intraday
    python indian_stock_breakout_scanner.py --list BANKNIFTY
    python indian_stock_breakout_scanner.py --list NIFTY50 --top 15
    python indian_stock_breakout_scanner.py --swing                # swing/positional mode
    python indian_stock_breakout_scanner.py --min-score 5
"""

import argparse
import os
import sqlite3
import time
import warnings
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
from tabulate import tabulate
from colorama import Fore, Style, init

warnings.filterwarnings("ignore")
init(autoreset=True)

# ── BACKGROUND LOOP CONFIGURATION ────────────────────────────────────────────
BACKGROUND_RUN = False  # Set to True to run the scanner in a loop continuously

# ─────────────────────────────────────────────────────────────────────────────
# KITE CONNECTION  (uses same tradesignal_cache.db as the Flask backend)
# ─────────────────────────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(__file__), 'app', 'backend', 'tradesignal_cache.db')

_kite = None   # lazy-init KiteConnect client

def _get_kite():
    """Return the global KiteConnect instance, creating it if needed."""
    global _kite
    if _kite is not None:
        return _kite
    try:
        from kiteconnect import KiteConnect
        api_key = os.environ.get('KITE_API_KEY', '')
        access_token = os.environ.get('KITE_ACCESS_TOKEN', '')
        if not api_key:
            # Try reading from Flask session file (shared DB)
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            # Fallback: the scanner can still work from cached data alone
            conn.close()
            return None
        _kite = KiteConnect(api_key=api_key)
        _kite.set_access_token(access_token)
    except Exception as e:
        print(Fore.YELLOW + f"  ⚠ Kite init failed: {e}" + Style.RESET_ALL)
        _kite = None
    return _kite

def _get_db():
    """Open a fresh SQLite connection (caller must close)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _resolve_token(symbol: str, exchange: str = 'NSE') -> int | None:
    """Look up instrument_token from the SQLite instruments table."""
    try:
        conn = _get_db()
        row = conn.execute(
            'SELECT instrument_token FROM instruments '
            'WHERE tradingsymbol = ? AND exchange = ? LIMIT 1',
            (symbol, exchange)
        ).fetchone()
        conn.close()
        return row['instrument_token'] if row else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ① UNIVERSE
# ─────────────────────────────────────────────────────────────────────────────

ALL_FNO = [
    "360ONE","ABB","ABCAPITAL","ADANIENSOL","ADANIENT","ADANIGREEN",
    "ADANIPORTS","ADANIPOWER","ALKEM","AMBER","AMBUJACEM","ANGELONE",
    "APLAPOLLO","APOLLOHOSP","ASHOKLEY","ASIANPAINT","ASTRAL","AUBANK",
    "AUROPHARMA","AXISBANK",
    "BAJAJ-AUTO","BAJAJFINSV","BAJAJHLDNG","BAJFINANCE","BANDHANBNK",
    "BANKBARODA","BANKINDIA","BDL","BEL","BHARATFORG","BHARTIARTL",
    "BHEL","BIOCON","BLUESTARCO","BOSCHLTD","BPCL","BRITANNIA","BSE",
    "CAMS","CANBK","CDSL","CGPOWER","CHOLAFIN","CIPLA","COALINDIA",
    "COCHINSHIP","COFORGE","COLPAL","CONCOR","CROMPTON","CUMMINSIND",
    "DABUR","DALBHARAT","DELHIVERY","DIVISLAB","DIXON","DLF","DMART","DRREDDY",
    "EICHERMOT","ETERNAL","EXIDEIND",
    "FEDERALBNK","FORCEMOT","FORTIS",
    "GAIL","GLENMARK","GMRAIRPORT","GODFRYPHLP","GODREJCP","GODREJPROP","GRASIM",
    "HAL","HAVELLS","HCLTECH","HDFCAMC","HDFCBANK","HDFCLIFE",
    "HEROMOTOCO","HINDALCO","HINDPETRO","HINDUNILVR","HINDZINC","HYUNDAI",
    "ICICIBANK","ICICIGI","ICICIPRULI","IDEA","IDFCFIRSTB","IEX",
    "INDHOTEL","INDIANB","INDIGO","INDUSINDBK","INDUSTOWER","INFY",
    "INOXWIND","IOC","IREDA","IRFC","ITC",
    "JINDALSTEL","JIOFIN","JSWENERGY","JSWSTEEL","JUBLFOOD",
    "KALYANKJIL","KAYNES","KEI","KFINTECH","KOTAKBANK","KPITTECH",
    "LAURUSLABS","LICHSGFIN","LICI","LODHA","LT","LTF","LTM","LUPIN",
    "M&M","MANAPPURAM","MANKIND","MARICO","MARUTI","MAXHEALTH",
    "MAZDOCK","MCX","MFSL","MOTHERSON","MOTILALOFS","MPHASIS","MUTHOOTFIN",
    "NAM-INDIA","NATIONALUM","NAUKRI","NBCC","NESTLEIND","NHPC","NMDC",
    "NTPC","NUVAMA","NYKAA",
    "OBEROIRLTY","OFSS","OIL","ONGC",
    "PAGEIND","PERSISTENT","PETRONET","PIDILITIND","PIIND","PNB",
    "POLICYBZR","POLYCAB","POONAWALLA","POWERGRID","PRESTIGE",
    "RECLTD","RELIANCE",
    "SAIL","SBICARD","SBILIFE","SBIN","SHREECEM","SHRIRAMFIN","SIEMENS",
    "SJVN","SOLARINDS","SONACOMS","SUNPHARMA","SUNTV","SUPREMEIND",
    "TATACHEM","TATACOMM","TATACONSUM","TATAMOTORS","TATAPOWER","TATASTEEL",
    "TCS","TECHM","TITAN","TORNTPHARM","TORNTPOWER","TRENT","TVSMOTOR",
    "UBL","ULTRACEMCO","UNIONBANK","UPL",
    "VBL","VEDL","VMM","VOLTAS","WIPRO","YESBANK","ZEEL","ZYDUSLIFE",
]

NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
    "BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BHARTIARTL","BPCL",
    "BRITANNIA","CIPLA","COALINDIA","DIVISLAB","DRREDDY","EICHERMOT",
    "ETERNAL","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE","HEROMOTOCO",
    "HINDALCO","HINDUNILVR","ICICIBANK","INDUSINDBK","INFY","ITC",
    "JSWSTEEL","KOTAKBANK","LT","M&M","MARUTI","NESTLEIND","NTPC",
    "ONGC","POWERGRID","RELIANCE","SBIN","SBILIFE","SHRIRAMFIN",
    "SUNPHARMA","TATAMOTORS","TATACONSUM","TATASTEEL","TCS","TECHM",
    "TITAN","TRENT","ULTRACEMCO","WIPRO",
]
BANKNIFTY   = ["AUBANK","AXISBANK","BANDHANBNK","FEDERALBNK","HDFCBANK",
               "ICICIBANK","IDFCFIRSTB","INDUSINDBK","KOTAKBANK","PNB","SBIN","CANBK"]
FINNIFTY    = ["AXISBANK","BAJFINANCE","BAJAJFINSV","CHOLAFIN","HDFCAMC",
               "HDFCBANK","HDFCLIFE","ICICIBANK","ICICIGI","ICICIPRULI",
               "IDFCFIRSTB","KOTAKBANK","LTF","MFSL","RECLTD","SBICARD",
               "SBILIFE","SBIN","SHRIRAMFIN"]
NIFTYIT     = ["COFORGE","HCLTECH","INFY","KPITTECH","LTM","MPHASIS",
               "OFSS","PERSISTENT","TCS","TECHM","WIPRO"]
NIFTYPHARMA = ["ALKEM","APOLLOHOSP","AUROPHARMA","BIOCON","CIPLA","DIVISLAB",
               "DRREDDY","GLENMARK","LAURUSLABS","LUPIN","MANKIND","SUNPHARMA",
               "TORNTPHARM","ZYDUSLIFE"]
NIFTYMETAL  = ["ADANIENT","AMBUJACEM","COALINDIA","HINDALCO","HINDPETRO",
               "HINDZINC","JINDALSTEL","JSWSTEEL","NATIONALUM","NMDC",
               "SAIL","TATASTEEL","VEDL"]
NIFTYAUTO   = ["ASHOKLEY","BAJAJ-AUTO","BOSCHLTD","EICHERMOT","EXIDEIND",
               "HEROMOTOCO","HYUNDAI","M&M","MARUTI","MOTHERSON","TATAMOTORS","TVSMOTOR"]
NIFTYFMCG   = ["BRITANNIA","COLPAL","DABUR","GODREJCP","HINDUNILVR",
               "ITC","MARICO","NESTLEIND","TATACONSUM","UBL","VBL"]
NIFTYREALTY = ["DLF","GODREJPROP","LODHA","OBEROIRLTY","PRESTIGE"]
NIFTYPSE    = ["BEL","BHEL","BPCL","CANBK","COALINDIA","CONCOR","GAIL",
               "HAL","IOC","IREDA","IRFC","NHPC","NMDC","NTPC","OIL",
               "ONGC","POWERGRID","RECLTD","SAIL","SBIN","SJVN"]

STOCK_LISTS = {
    "ALL_FNO":ALL_FNO,"NIFTY50":NIFTY50,"BANKNIFTY":BANKNIFTY,
    "FINNIFTY":FINNIFTY,"NIFTYIT":NIFTYIT,"NIFTYPHARMA":NIFTYPHARMA,
    "NIFTYMETAL":NIFTYMETAL,"NIFTYAUTO":NIFTYAUTO,"NIFTYFMCG":NIFTYFMCG,
    "NIFTYREALTY":NIFTYREALTY,"NIFTYPSE":NIFTYPSE,
}

# Kite uses tradingsymbol directly — no .NS suffix needed
def get_ticker(sym): return sym


# ─────────────────────────────────────────────────────────────────────────────
# ② INDICATORS
# ─────────────────────────────────────────────────────────────────────────────

def _s(x): return x.iloc[:,0] if isinstance(x, pd.DataFrame) else x

def calc_rsi(close, period=14):
    d = close.diff()
    g = d.clip(lower=0).rolling(period).mean()
    l = (-d.clip(upper=0)).rolling(period).mean()
    return 100 - (100 / (1 + g / l.replace(0, np.nan)))

def calc_macd(close, fast=12, slow=26, sig=9):
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    ml = ef - es; sl = ml.ewm(span=sig, adjust=False).mean()
    return ml, sl, ml - sl

def calc_atr(high, low, close, period=14):
    tr = pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],
                   axis=1).max(axis=1)
    return tr.rolling(period).mean()

def calc_bollinger(close, period=20, n=2):
    ma=close.rolling(period).mean(); std=close.rolling(period).std()
    up=ma+n*std; lo=ma-n*std
    return up, ma, lo, (up-lo)/ma.replace(0,np.nan), (close-lo)/(up-lo+1e-9)

def calc_supertrend(high, low, close, period=10, mult=3.0):
    hl2=(high+low)/2
    tr=pd.concat([high-low,(high-close.shift()).abs(),(low-close.shift()).abs()],
                 axis=1).max(axis=1)
    atr=tr.ewm(span=period,adjust=False).mean()
    ubr=hl2+mult*atr; lbr=hl2-mult*atr
    n=len(close); ub=ubr.copy(); lb=lbr.copy()
    st=pd.Series(np.nan,index=close.index); bull=pd.Series(True,index=close.index)
    for i in range(1,n):
        ub.iloc[i]=(ubr.iloc[i] if(ubr.iloc[i]<ub.iloc[i-1] or close.iloc[i-1]>ub.iloc[i-1]) else ub.iloc[i-1])
        lb.iloc[i]=(lbr.iloc[i] if(lbr.iloc[i]>lb.iloc[i-1] or close.iloc[i-1]<lb.iloc[i-1]) else lb.iloc[i-1])
        if np.isnan(st.iloc[i-1]):
            st.iloc[i]=lb.iloc[i]; bull.iloc[i]=True
        elif st.iloc[i-1]==ub.iloc[i-1]:
            bull.iloc[i]=close.iloc[i]>ub.iloc[i]
            st.iloc[i]=lb.iloc[i] if bull.iloc[i] else ub.iloc[i]
        else:
            bull.iloc[i]=close.iloc[i]>=lb.iloc[i]
            st.iloc[i]=lb.iloc[i] if bull.iloc[i] else ub.iloc[i]
    return bull

def calc_vwap(high, low, close, volume):
    tp=(high+low+close)/3
    return (tp*volume).cumsum()/volume.cumsum().replace(0,np.nan)


# ─────────────────────────────────────────────────────────────────────────────
# ③ STRUCTURE ENGINE  —  Swing Points / BOS / CHoCH / PBS
# ─────────────────────────────────────────────────────────────────────────────

def find_swing_points(high: pd.Series, low: pd.Series,
                      lookback: int = 3) -> tuple[list, list]:
    """
    ZigZag-style swing detection.
    Returns (swing_highs, swing_lows) as lists of (bar_index, price).
    A bar is a swing high if its high is the highest in ±lookback bars.
    """
    n = len(high)
    sh, sl = [], []
    for i in range(lookback, n - lookback):
        if float(high.iloc[i]) == float(high.iloc[i-lookback:i+lookback+1].max()):
            sh.append((i, float(high.iloc[i])))
        if float(low.iloc[i])  == float(low.iloc[i-lookback:i+lookback+1].min()):
            sl.append((i, float(low.iloc[i])))
    return sh, sl


def detect_bos(close: pd.Series, high: pd.Series,
               swing_highs: list, swing_lows: list,
               lookback_bars: int = 8) -> dict:
    """
    Bullish BOS: a daily/intraday close that crossed ABOVE a prior swing high.
    Bearish BOS: a close that crossed BELOW a prior swing low.

    Returns:
      bos_up       bool   bullish BOS detected in last `lookback_bars`
      bos_down     bool   bearish BOS detected
      bos_level    float  the exact swing level that was breached
      bos_bar_ago  int    bars since the BOS bar (0 = current bar)
      confirmed    bool   BOS is 0–5 bars old and not yet invalidated
      invalidated  bool   price closed back below bos_level after BOS
    """
    n = len(close)
    out = dict(bos_up=False, bos_down=False, bos_level=None,
               bos_bar_ago=None, confirmed=False, invalidated=False)

    # ── Bullish BOS scan ─────────────────────────────────────────────
    for bi in range(max(1, n - lookback_bars), n):
        c_now  = float(close.iloc[bi])
        c_prev = float(close.iloc[bi - 1])
        for sh_pos, sh_price in reversed(swing_highs):
            if sh_pos >= bi:
                continue                          # swing must precede bar
            if c_prev < sh_price <= c_now:        # close crossed above
                bars_ago = n - 1 - bi
                if bars_ago > 5:
                    break
                # Invalidation: any subsequent close below sh_price
                invalid = any(
                    float(close.iloc[j]) < sh_price * 0.998
                    for j in range(bi + 1, n)
                )
                out.update(bos_up=True, bos_level=sh_price,
                           bos_bar_ago=bars_ago,
                           confirmed=not invalid,
                           invalidated=invalid)
                return out

    # ── Bearish BOS scan ─────────────────────────────────────────────
    for bi in range(max(1, n - lookback_bars), n):
        c_now  = float(close.iloc[bi])
        c_prev = float(close.iloc[bi - 1])
        for sl_pos, sl_price in reversed(swing_lows):
            if sl_pos >= bi:
                continue
            if c_prev > sl_price >= c_now:
                bars_ago = n - 1 - bi
                if bars_ago > 5:
                    break
                invalid = any(
                    float(close.iloc[j]) > sl_price * 1.002
                    for j in range(bi + 1, n)
                )
                out.update(bos_down=True, bos_level=sl_price,
                           bos_bar_ago=bars_ago,
                           confirmed=not invalid,
                           invalidated=invalid)
                return out

    return out


def detect_pbs(close: pd.Series, high: pd.Series, low: pd.Series,
               volume: pd.Series, bos_level: float,
               atr_val: float) -> dict:
    """
    Pullback-to-Broken-Structure (PBS) detection for BOS continuation entry.

    Valid PBS requires ALL of:
      1. Price has pulled back to within 1.5×ATR ABOVE the BOS level
         (not below — that invalidates the BOS)
      2. Pullback bars show DECLINING volume  (no distribution, just digestion)
      3. Current bar shows RESUMPTION: bullish candle + volume picking up
      4. BOS level not violated (no close below it after the BOS bar)

    Returns pbs_valid, pbs_score (0–10), quality label, and component flags.
    """
    n   = len(close)
    ltp = float(close.iloc[-1])
    out = dict(pbs_valid=False, pbs_score=0, quality="—",
               at_level=False, vol_declining=False,
               resuming=False, invalidated=False,
               dist_pct=None, stop_level=round(bos_level * 0.998, 2))

    if bos_level is None or n < 4:
        return out

    dist     = ltp - bos_level
    dist_pct = dist / bos_level * 100
    out["dist_pct"] = round(dist_pct, 2)

    # Invalidation: any close below BOS level since the BOS
    for i in range(1, min(6, n)):
        if float(close.iloc[-i]) < bos_level * 0.998:
            out["invalidated"] = True
            return out

    # 1. At the level
    at_level = 0 <= dist <= atr_val * 1.5
    out["at_level"] = at_level

    # 2. Volume declining on pullback (last 3 bars vs 3 before that)
    if n >= 6:
        vol_recent = float(volume.iloc[-3:].mean())
        vol_prior  = float(volume.iloc[-6:-3].mean())
        out["vol_declining"] = vol_recent < vol_prior * 0.85
    else:
        out["vol_declining"] = False

    # 3. Resumption: current close > prior close + volume rising
    curr_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    bar_vol    = float(volume.iloc[-1])
    avg_vol3   = float(volume.iloc[-4:-1].mean()) if n >= 4 else bar_vol
    out["resuming"] = (curr_close > prev_close) and (bar_vol > avg_vol3 * 1.1)

    # Score
    s = 0
    if at_level:               s += 3
    if out["vol_declining"]:   s += 2
    if out["resuming"]:        s += 3
    if 0 <= dist_pct <= 0.5:   s += 2
    elif dist_pct <= 1.0:      s += 1
    out["pbs_score"] = s
    out["pbs_valid"] = at_level and not out["invalidated"]

    if   s >= 8: out["quality"] = "🎯 Tier-1"
    elif s >= 6: out["quality"] = "⬆ Tier-2"
    elif s >= 4: out["quality"] = "Tier-3"
    else:        out["quality"] = "Weak"

    return out


def detect_choch(high: pd.Series, low: pd.Series,
                 close: pd.Series, volume: pd.Series,
                 swing_highs: list, swing_lows: list,
                 atr_val: float) -> dict:
    """
    CHoCH (Change of Character) — Bullish Reversal Detection.

    A valid bullish CHoCH requires:
      1. Prior bearish structure: at least 2 consecutive Lower Highs (LH)
         on the series (confirmed via swing_highs sequence)
      2. A swing low that is HIGHER than the prior swing low (HL formed)
         → this is the CHoCH bar
      3. Price has since closed ABOVE the most recent swing high
         → structure has flipped from bearish to bullish
      4. Volume on the CHoCH breakout bar is above average
      5. The CHoCH occurred within the last `lookback` bars (fresh signal)

    Also detects bearish CHoCH for PE / short setups:
      Prior bullish structure (HH sequence) → LH formed → close below swing low

    Returns choch_bull, choch_bear, level, bars_ago, quality flags.
    """
    n   = len(close)
    out = dict(choch_bull=False, choch_bear=False,
               choch_level=None, choch_bar_ago=None,
               prior_structure=False, hl_formed=False,
               vol_confirmed=False, quality="—",
               stop_level=None)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return out

    avg_vol = float(volume.rolling(20).mean().iloc[-1])

    # ── BULLISH CHoCH ─────────────────────────────────────────────────
    # Step 1: confirm prior bearish structure = last 2 swing highs are LH
    sh_prices = [p for _, p in swing_highs[-3:]]
    lh_sequence = len(sh_prices) >= 2 and all(
        sh_prices[i] < sh_prices[i-1] for i in range(1, len(sh_prices))
    )
    out["prior_structure"] = lh_sequence

    if lh_sequence:
        # Step 2: last two swing lows — check if HL formed
        sl_prices = [p for _, p in swing_lows[-2:]]
        hl_formed = sl_prices[-1] > sl_prices[-2] if len(sl_prices) >= 2 else False
        out["hl_formed"] = hl_formed

        if hl_formed:
            # Step 3: price closed above most recent swing high
            last_sh_price = swing_highs[-1][1]
            choch_cross   = float(close.iloc[-1]) > last_sh_price

            if choch_cross:
                # Step 4: find the breakout bar
                for bi in range(max(1, n-8), n):
                    c_now  = float(close.iloc[bi])
                    c_prev = float(close.iloc[bi-1])
                    if c_prev < last_sh_price <= c_now:
                        bars_ago = n - 1 - bi
                        if bars_ago > 5:
                            break
                        # Volume confirmation on CHoCH bar
                        bar_vol = float(volume.iloc[bi])
                        vol_ok  = bar_vol > avg_vol * 0.8
                        out.update(
                            choch_bull=True,
                            choch_level=last_sh_price,
                            choch_bar_ago=bars_ago,
                            vol_confirmed=vol_ok,
                            stop_level=round(float(low.iloc[-3:].min()) * 0.998, 2)
                        )
                        # Quality
                        q_score = 0
                        if hl_formed:          q_score += 2
                        if vol_ok:             q_score += 2
                        if bars_ago == 0:      q_score += 2
                        elif bars_ago <= 2:    q_score += 1
                        # RSI check: not overbought
                        rsi_now = float(calc_rsi(close).iloc[-1])
                        if rsi_now < 70:       q_score += 1
                        if   q_score >= 6: out["quality"] = "🔄 Strong CHoCH"
                        elif q_score >= 4: out["quality"] = "🔄 Moderate CHoCH"
                        else:              out["quality"] = "Weak CHoCH"
                        return out

    # ── BEARISH CHoCH (for PE / short setups) ────────────────────────
    sl_prices_all = [p for _, p in swing_lows[-3:]]
    hl_sequence   = len(sl_prices_all) >= 2 and all(
        sl_prices_all[i] > sl_prices_all[i-1] for i in range(1, len(sl_prices_all))
    )
    if hl_sequence:
        sh_prices_2 = [p for _, p in swing_highs[-2:]]
        lh_formed   = sh_prices_2[-1] < sh_prices_2[-2] if len(sh_prices_2) >= 2 else False
        if lh_formed:
            last_sl_price = swing_lows[-1][1]
            if float(close.iloc[-1]) < last_sl_price:
                for bi in range(max(1, n-8), n):
                    c_now  = float(close.iloc[bi])
                    c_prev = float(close.iloc[bi-1])
                    if c_prev > last_sl_price >= c_now:
                        bars_ago = n - 1 - bi
                        if bars_ago > 5: break
                        bar_vol = float(volume.iloc[bi])
                        vol_ok  = bar_vol > avg_vol * 0.8
                        out.update(
                            choch_bear=True,
                            choch_level=last_sl_price,
                            choch_bar_ago=bars_ago,
                            vol_confirmed=vol_ok,
                            quality="🔻 Bear CHoCH",
                            stop_level=round(float(high.iloc[-3:].max()) * 1.002, 2)
                        )
                        return out
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ④ DATA FETCH  (Kite API + SQLite cache)
# ─────────────────────────────────────────────────────────────────────────────

_cache: dict = {}

def _kite_historical(token: int, from_dt: str, to_dt: str,
                     interval: str = 'day') -> list:
    """Fetch historical data from Kite API (live only, no cache fallback).
    Returns list of dicts with open/high/low/close/volume/date keys.
    Results are cached in SQLite for the backend's benefit."""
    kite = _get_kite()
    if not kite:
        return []   # No Kite = no data (never fall back to stale cache)

    try:
        raw = kite.historical_data(int(token), from_dt, to_dt, interval)
        if not raw:
            return []

        # Cache in SQLite for backend use
        try:
            conn = _get_db()
            now_iso = datetime.now().isoformat()
            is_intraday = interval in ('5minute', '15minute', '30minute', '60minute', 'minute')
            data_tuples = []
            for c in raw:
                date_val = c.get('date', '')
                if hasattr(date_val, 'isoformat'):
                    date_val = date_val.isoformat()
                date_str = str(date_val) if is_intraday else str(date_val).split('T')[0]
                data_tuples.append((
                    token, date_str,
                    c.get('open', 0), c.get('high', 0), c.get('low', 0),
                    c.get('close', 0), c.get('volume', 0),
                    interval, now_iso
                ))
            if data_tuples:
                conn.executemany(
                    'INSERT OR REPLACE INTO ohlcv '
                    '(instrument_token, date, open, high, low, close, volume, interval, fetched_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    data_tuples
                )
                conn.commit()
            conn.close()
        except Exception:
            pass  # Cache write failure is non-fatal

        # Return live data
        return [{'date': str(c.get('date','')), 'open': c.get('open',0),
                 'high': c.get('high',0), 'low': c.get('low',0),
                 'close': c.get('close',0), 'volume': c.get('volume',0)}
                for c in raw]
    except Exception as e:
        print(Fore.YELLOW + f"  ⚠ Kite fetch failed for token {token}: {e}" + Style.RESET_ALL)
        return []


def get_nifty_20d_return() -> float:
    """Get Nifty 50 20-day return using Kite or SQLite cache."""
    if "nifty" in _cache:
        return _cache["nifty"]
    try:
        # Nifty 50 instrument token = 256265
        nifty_token = _resolve_token('NIFTY 50', 'NSE') or 256265
        from_dt = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')
        to_dt = datetime.now().strftime('%Y-%m-%d')
        candles = _kite_historical(nifty_token, from_dt, to_dt, 'day')
        if candles and len(candles) >= 20:
            closes = [c['close'] for c in candles]
            _cache["nifty"] = float((closes[-1] - closes[-20]) / closes[-20] * 100)
        else:
            _cache["nifty"] = 0.0
    except Exception:
        _cache["nifty"] = 0.0
    return _cache["nifty"]


def fetch_daily(symbol: str) -> pd.DataFrame | None:
    """Fetch ~1 year of daily candles via Kite + cache."""
    token = _resolve_token(symbol)
    if not token:
        return None
    try:
        from_dt = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
        to_dt = datetime.now().strftime('%Y-%m-%d')
        candles = _kite_historical(token, from_dt, to_dt, 'day')
        if not candles or len(candles) < 60:
            return None
        df = pd.DataFrame(candles)
        df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                                'close': 'Close', 'volume': 'Volume'})
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].apply(pd.to_numeric, errors='coerce')
        return df.dropna().reset_index(drop=True)
    except Exception:
        return None


def fetch_intraday(symbol: str, interval: str = '5minute', min_bars: int = 3) -> pd.DataFrame | None:
    """Fetch intraday candles for today's session via Kite + cache."""
    token = _resolve_token(symbol)
    if not token:
        return None
    try:
        from_dt = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
        to_dt = datetime.now().strftime('%Y-%m-%d')
        candles = _kite_historical(token, from_dt, to_dt, interval)
        if not candles:
            return None
        df = pd.DataFrame(candles)
        df['date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                                'close': 'Close', 'volume': 'Volume'})
        df = df[['date', 'Open', 'High', 'Low', 'Close', 'Volume']].dropna()
        df = df.set_index('date')
        df = df.apply(pd.to_numeric, errors='coerce')
        # Filter to today's session only
        ist = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(ist).date()
        sess = df[df.index.date == today] if hasattr(df.index, 'date') else df
        if len(sess) < min_bars:
            # Fallback to last available day
            if hasattr(df.index, 'date') and len(df) > 0:
                last_day = df.index.date[-1]
                sess = df[df.index.date == last_day]
        return sess if len(sess) >= min_bars else None
    except Exception:
        return None


def fetch_5min(symbol: str) -> pd.DataFrame | None:
    """Fetch 5-minute candles for today's session via Kite + cache."""
    return fetch_intraday(symbol, '5minute', min_bars=3)


def fetch_15min(symbol: str) -> pd.DataFrame | None:
    """Fetch 15-minute candles for today's session via Kite + cache."""
    return fetch_intraday(symbol, '15minute', min_bars=5)


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ TIME HELPER
# ─────────────────────────────────────────────────────────────────────────────

def ist_hour() -> float:
    ist = timezone(timedelta(hours=5, minutes=30))
    t   = datetime.now(ist)
    return t.hour + t.minute / 60.0


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ SHARED BASE METRICS  (common to all three scanners)
# ─────────────────────────────────────────────────────────────────────────────

def base_metrics(symbol: str, mode: str) -> dict | None:
    """
    Fetches and computes metrics common to all three tables.
    Returns None if data unavailable or hard filters fail.
    mode: 'intraday' | 'swing'
    """
    dfd = fetch_daily(symbol)
    if dfd is None: return None

    cd = _s(dfd["Close"]); hd = _s(dfd["High"])
    ld = _s(dfd["Low"]);   vd = _s(dfd["Volume"])
    od = _s(dfd["Open"])

    ltp      = float(cd.iloc[-1])
    prev_cls = float(cd.iloc[-2])
    pct_chg  = (ltp - prev_cls) / prev_cls * 100

    atr_val  = float(calc_atr(hd, ld, cd).iloc[-1])
    atr_pct  = atr_val / ltp * 100
    rsi_now  = float(calc_rsi(cd).iloc[-1])
    st_bull  = bool(calc_supertrend(hd, ld, cd).iloc[-1])
    st_prv   = bool(calc_supertrend(hd, ld, cd).iloc[-2])
    st_flip  = st_bull and not st_prv

    avg_vol20 = float(vd.rolling(20).mean().iloc[-1])

    # Swing point detection on daily (lookback=3)
    sh_d, sl_d = find_swing_points(hd, ld, lookback=3)

    # BOS on daily
    bos_d = detect_bos(cd, hd, sh_d, sl_d, lookback_bars=8)

    # CHoCH on daily
    choch_d = detect_choch(hd, ld, cd, vd, sh_d, sl_d, atr_val)

    # 20d RS vs Nifty
    rs = float((ltp - cd.iloc[-20]) / cd.iloc[-20] * 100)

    # Daily MA
    ma20  = float(cd.rolling(20).mean().iloc[-1])
    ma50  = float(cd.rolling(50).mean().iloc[-1])
    ma200 = float(cd.rolling(200).mean().iloc[-1])
    ma_stack = ltp > ma20 > 0 and ltp > ma50 > 0 and ltp > ma200 > 0

    iv_risk = abs(pct_chg) > 3.5

    # 5-min data (intraday mode)
    df5 = None
    if mode == "intraday":
        df5 = fetch_5min(symbol)

    return dict(
        ltp=ltp, prev_cls=prev_cls, pct_chg=pct_chg,
        atr_val=atr_val, atr_pct=atr_pct,
        rsi_now=rsi_now, st_bull=st_bull, st_flip=st_flip,
        avg_vol20=avg_vol20,
        sh_d=sh_d, sl_d=sl_d,
        bos_d=bos_d, choch_d=choch_d,
        rs=rs, ma_stack=ma_stack,
        iv_risk=iv_risk,
        cd=cd, hd=hd, ld=ld, od=od, vd=vd,
        df5=df5,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑦-A  BREAKOUT SCANNER
#       Price breaking structure RIGHT NOW — momentum entry
# ─────────────────────────────────────────────────────────────────────────────

def score_breakout(symbol: str, bm: dict, vol_mult: float,
                   nifty_ret: float, mode: str) -> dict | None:
    """
    Scores a stock for BREAKOUT entry.
    Intraday: ORB breach + VWAP acceptance + 5-min momentum
    Swing:    20-day or 50-day high breach + volume + MA stack
    """
    ltp     = bm["ltp"]
    rsi_now = bm["rsi_now"]
    st_bull = bm["st_bull"]
    iv_risk = bm["iv_risk"]
    atr_pct = bm["atr_pct"]
    atr_val = bm["atr_val"]
    pct_chg = bm["pct_chg"]
    cd      = bm["cd"]; hd = bm["hd"]; ld = bm["ld"]
    vd      = bm["vd"]; od = bm["od"]

    # Hard filter: supertrend must be bullish for breakout entries
    if not st_bull:
        return None

    score = 0; sigs = []; warns = []

    if mode == "intraday":
        df5 = bm["df5"]
        if df5 is None or len(df5) < 4:
            return None

        c5 = _s(df5["Close"]); h5 = _s(df5["High"])
        l5 = _s(df5["Low"]);   o5 = _s(df5["Open"]); v5 = _s(df5["Volume"])

        day_open  = float(o5.iloc[0])
        orb_bars  = min(3, len(df5))
        orb_high  = float(h5.iloc[:orb_bars].max())
        orb_low   = float(l5.iloc[:orb_bars].min())
        orb_bo    = ltp > orb_high * 1.005
        orb_margin= (ltp - orb_high) / orb_high * 100 if orb_high > 0 else 0

        # VWAP
        vwap     = calc_vwap(h5, l5, c5, v5)
        vwap_now = float(vwap.iloc[-1])
        vwap_prv = float(vwap.iloc[-2]) if len(vwap) > 1 else vwap_now
        above_vwap  = ltp > vwap_now
        vwap_slope  = vwap_now > vwap_prv

        # FIX ISSUE 1: VWAP treated as position-state filter, not cross-event
        # Captures gap-up continuations and trend trades, not just fresh crosses
        above_vwap_ok = ltp > vwap_now  # relaxed: position above VWAP is valid

        # Intraday RVOL (time-normalised)
        frac    = len(df5) / 75
        rvol    = float(v5.sum()) / (bm["avg_vol20"] * frac + 1e-9)
        vol_ok  = rvol >= vol_mult * 0.75
        bar_spk = float(v5.iloc[-1]) > float(v5.rolling(5).mean().iloc[-1]) * 1.5

        # 5m microstructure
        n4   = min(4, len(df5)-1)
        hh5  = all(float(h5.iloc[-i])>float(h5.iloc[-i-1]) for i in range(1,n4+1))
        hl5  = all(float(l5.iloc[-i])>float(l5.iloc[-i-1]) for i in range(1,n4+1))
        micro= hh5 and hl5

        # 5m EMA
        e9  = c5.ewm(span=9,  adjust=False).mean()
        e20 = c5.ewm(span=20, adjust=False).mean()
        ema_x5 = (float(e9.iloc[-2])<=float(e20.iloc[-2]) and
                  float(e9.iloc[-1])> float(e20.iloc[-1]))
        ema_b5 = float(e9.iloc[-1]) > float(e20.iloc[-1])

        # 5m MACD
        if len(c5) >= 26:
            ml5,sl5,ht5 = calc_macd(c5)
            macd_x5 = float(ml5.iloc[-2])<float(sl5.iloc[-2]) and float(ml5.iloc[-1])>=float(sl5.iloc[-1])
            macd_b5 = float(ml5.iloc[-1])>float(sl5.iloc[-1])
            hist_r5 = float(ht5.iloc[-1])>float(ht5.iloc[-2])
        else:
            macd_x5=macd_b5=hist_r5=False

        # Time gate
        ih = ist_hour()
        prime = (9.5<=ih<=11.5) or (13.5<=ih<=14.0)
        late  = ih > 14.0

        pct_from_open = (ltp - day_open) / day_open * 100
        close_pos     = (ltp-float(l5.iloc[-1])) / (float(h5.iloc[-1])-float(l5.iloc[-1])+1e-9)

        # SCORING — intraday breakout
        # 1. RVOL
        if   rvol>=vol_mult*2.0: score+=5; sigs.append(f"🔥 RVOL {rvol:.1f}x")
        elif rvol>=vol_mult*1.5: score+=4; sigs.append(f"🔥 RVOL {rvol:.1f}x")
        elif rvol>=vol_mult:     score+=3; sigs.append(f"📈 RVOL {rvol:.1f}x")
        elif vol_ok:             score+=1; sigs.append(f"RVOL {rvol:.1f}x")
        else:                    warns.append("⚠ Low RVOL")
        if bar_spk: score+=1; sigs.append("Bar spike")

        # 2. ORB
        if orb_bo and vol_ok:
            if   orb_margin>=1.0: score+=5; sigs.append(f"🚀 ORB +{orb_margin:.1f}%")
            elif orb_margin>=0.5: score+=4; sigs.append(f"⬆ ORB +{orb_margin:.1f}%")
            else:                 score+=2; sigs.append(f"ORB +{orb_margin:.1f}%")
        elif orb_bo:              score+=1; warns.append("⚠ ORB w/o RVOL")
        elif ltp < orb_low:       score-=1; warns.append("⚠ Below ORB")

        # 3. VWAP — FIX ISSUE 2: position-state scoring, ATR-relative stretch control
        # Removes cross-event dependency; rewards holding above VWAP with momentum/volume.
        # Stretch penalty is ATR-relative (consistent with PBS 1.5×ATR gate elsewhere).
        vwap_distance = abs(ltp - vwap_now) / vwap_now if vwap_now > 0 else 0

        if above_vwap and vwap_slope and vol_ok:
            score += 4; sigs.append("VWAP Hold+↑")
        elif above_vwap and vol_ok:
            score += 3; sigs.append("VWAP Hold")
        elif above_vwap:
            score += 1; sigs.append("Above VWAP")
        else:
            score -= 1; warns.append("⚠ Below VWAP")

        # ATR-relative stretch penalty — avoids late/extended options entries
        # Uses same ATR scaling as PBS distance gate for consistency
        atr_stretch_threshold = (atr_val / vwap_now) * 1.5 if vwap_now > 0 else 0.02
        if vwap_distance > atr_stretch_threshold:
            score -= 2; warns.append("⚠ VWAP Stretch")

        # 4. Daily BOS alignment bonus
        if bm["bos_d"]["bos_up"] and bm["bos_d"]["bos_bar_ago"]==0:
            score+=3; sigs.append("🔑 Daily BOS today")
        elif bm["bos_d"]["bos_up"] and bm["bos_d"]["bos_bar_ago"]<=2:
            score+=2; sigs.append(f"Daily BOS {bm['bos_d']['bos_bar_ago']}d ago")

        # 5. RSI
        if   rsi_now>75:         score-=1; warns.append(f"RSI {rsi_now:.0f} OB⚠")
        elif 55<=rsi_now<=74:    score+=2; sigs.append(f"RSI {rsi_now:.0f}")
        elif 50<=rsi_now<55:     score+=1; sigs.append(f"RSI {rsi_now:.0f}")
        else:                    sigs.append(f"RSI {rsi_now:.0f}")

        # 6. 5m MACD
        if macd_x5:              score+=3; sigs.append("5m MACD✕↑")
        elif macd_b5 and hist_r5:score+=2; sigs.append("5m MACD↑")
        elif macd_b5:            score+=1; sigs.append("5m MACD+")

        # 7. 5m structure
        if micro:  score+=2; sigs.append("5m HH+HL")
        if ema_x5: score+=3; sigs.append("5m EMA✕↑")
        elif ema_b5: score+=1; sigs.append("5m EMA↑")

        # 8. Intraday momentum
        if 1.5<=pct_from_open<=4.0: score+=2; sigs.append(f"+{pct_from_open:.1f}% fr.open")
        elif pct_from_open>=0.5:    score+=1; sigs.append(f"+{pct_from_open:.1f}% fr.open")

        # 9. RS
        rs_net = bm["rs"] - nifty_ret
        if rs_net>=5: score+=1; sigs.append(f"RS+{rs_net:.0f}%")

        # 10. Close position
        if close_pos>=0.75: score+=1; sigs.append("Top close")

        # 11. Time gate
        if prime: score+=1; sigs.append("⏰ Prime")
        if late:  score-=1; warns.append("⚠ Late")

        # 12. IV guard
        if iv_risk: score-=2; warns.append(f"⚠ IV risk ({pct_chg:+.1f}%)")

        if bm["st_flip"]: score+=1; sigs.append("ST flip↑")

        score = max(score, 0)
        sig_str = " | ".join(sigs) + ("  "+" ".join(warns) if warns else "")

        return dict(
            Symbol=symbol, Close=round(ltp,2), Chg_pct=round(pct_chg,2),
            RVOL=round(rvol,2),
            ORB=f"+{orb_margin:.1f}%" if orb_bo else "—",
            VWAP=round(vwap_now,2), AbvVWAP="✓" if above_vwap else "✗",
            RSI=round(rsi_now,1), ATR_pct=round(atr_pct,2),
            ST="▲" if st_bull else "▼",
            IVRisk="⚠" if iv_risk else "✓",
            Score=score, Entry=round(ltp,2),
            Stop=round(orb_low*0.998,2),
            Target=round(ltp + 2.5*bm["atr_val"],2),
            Signals=sig_str,
        )

    else:
        # ── SWING BREAKOUT ────────────────────────────────────────────
        vol_today = float(vd.iloc[-1])
        vol_ratio = vol_today / bm["avg_vol20"] if bm["avg_vol20"]>0 else 0
        vol_ok    = vol_ratio >= vol_mult * 0.75

        prev20_high = float(hd.iloc[-21:-1].max())
        prev50_high = float(hd.iloc[-51:-1].max())
        consol_bo   = ltp > prev20_high
        base_bo     = ltp > prev50_high
        high_52w    = float(hd.tail(252).max())
        at_52w      = ltp >= high_52w * 0.995
        near_52w    = ltp >= high_52w * 0.97

        day_hi   = float(hd.iloc[-1]); day_lo = float(ld.iloc[-1])
        day_open = float(od.iloc[-1])
        day_rng  = day_hi - day_lo
        close_pos= (ltp-day_lo)/(day_rng+1e-9)
        body     = abs(ltp-day_open)
        bull_c   = ltp>day_open and (body/(day_rng+1e-9))>0.55

        ml,sl,ht = calc_macd(cd)
        macd_x   = float(ml.iloc[-2])<float(sl.iloc[-2]) and float(ml.iloc[-1])>=float(sl.iloc[-1])
        macd_b   = float(ml.iloc[-1])>float(sl.iloc[-1])
        hist_r   = float(ht.iloc[-1])>float(ht.iloc[-2])

        bb_up,_,_,bb_bw,bb_pb = calc_bollinger(cd)
        bb_bo    = ltp > float(bb_up.iloc[-1])
        bw_mean  = float(bb_bw.rolling(50).mean().iloc[-1])
        bb_sq    = float(bb_bw.iloc[-1]) < bw_mean

        hh3 = all(float(hd.iloc[-i])>float(hd.iloc[-i-1]) for i in range(1,4))
        hl3 = all(float(ld.iloc[-i])>float(ld.iloc[-i-1]) for i in range(1,4))

        dist_52w = (high_52w-ltp)/high_52w*100
        rs_net   = bm["rs"]-nifty_ret
        novol_bo = consol_bo and vol_ratio<1.2

        # 1. Volume
        if   vol_ratio>=vol_mult*2.0: score+=5; sigs.append(f"🔥 Vol {vol_ratio:.1f}x")
        elif vol_ratio>=vol_mult*1.5: score+=4; sigs.append(f"🔥 Vol {vol_ratio:.1f}x")
        elif vol_ratio>=vol_mult:     score+=3; sigs.append(f"📈 Vol {vol_ratio:.1f}x")
        elif vol_ok:                  score+=1; sigs.append(f"Vol {vol_ratio:.1f}x")
        else:                         warns.append("⚠ Low vol")

        # 2. Breakout type
        if at_52w and base_bo and vol_ok:    score+=6; sigs.append("🚀 52W+Base BO")
        elif at_52w and vol_ok:              score+=5; sigs.append("🚀 52W High")
        elif near_52w and consol_bo and vol_ok: score+=4; sigs.append("⬆ ~52W Consol BO")
        elif base_bo and vol_ok:             score+=4; sigs.append("⬆ 10W Base BO")
        elif consol_bo and vol_ok:           score+=3; sigs.append("⬆ 20D Consol BO")
        elif near_52w:                       score+=2; sigs.append("Near 52W Hi")

        # 3. Quality
        if close_pos>=0.75 and (consol_bo or base_bo or at_52w): score+=1; sigs.append("Strong close")
        if close_pos<0.5 and consol_bo: score-=1; warns.append("⚠ Weak close")

        # 4. ST flip
        if bm["st_flip"]: score+=2; sigs.append("ST flip↑")
        else:             sigs.append("ST▲")

        # 5. RSI
        if   rsi_now>75:      score-=1; warns.append(f"RSI {rsi_now:.0f} OB⚠")
        elif 55<=rsi_now<=74: score+=2; sigs.append(f"RSI {rsi_now:.0f}↑")
        elif 50<=rsi_now<55:  score+=1; sigs.append(f"RSI {rsi_now:.0f}")
        else:                 sigs.append(f"RSI {rsi_now:.0f}")

        # 6. MACD
        if macd_x:             score+=3; sigs.append("MACD✕↑")
        elif macd_b and hist_r:score+=2; sigs.append("MACD↑")
        elif macd_b:           score+=1; sigs.append("MACD+")

        # 7. BB
        if bb_bo and vol_ok:   score+=2; sigs.append("BB BO")
        elif bb_sq:            score+=1; sigs.append("BB coil")

        # 8. MA stack
        if bm["ma_stack"]:      score+=2; sigs.append("MA stack✓")

        # 9. Structure
        if hh3 and hl3:        score+=1; sigs.append("HH+HL")
        if bull_c:             score+=1; sigs.append("Bull candle")

        # 10. RS + novol penalty
        if rs_net>=5:          score+=2; sigs.append(f"RS+{rs_net:.0f}%")
        elif rs_net>=2:        score+=1; sigs.append(f"RS+{rs_net:.0f}%")
        if novol_bo:           score-=2; warns.append("⚠ BO w/o vol")
        if pct_chg>=3:         score+=2; sigs.append(f"+{pct_chg:.1f}%")
        elif pct_chg>=1.5:     score+=1; sigs.append(f"+{pct_chg:.1f}%")

        score = max(score,0)
        sig_str = " | ".join(sigs)+("  "+" ".join(warns) if warns else "")

        return dict(
            Symbol=symbol, Close=round(ltp,2), Chg_pct=round(pct_chg,2),
            Vol_x=round(vol_ratio,2),
            BO_Type=("52W+Base" if at_52w and base_bo else
                     "52W Hi"   if at_52w else
                     "10W Base" if base_bo else
                     "20D"      if consol_bo else "—"),
            RSI=round(rsi_now,1), ATR_pct=round(atr_pct,2),
            Dist52W=round(float((float(hd.tail(252).max())-ltp)/float(hd.tail(252).max())*100),1),
            MAStk="✓" if bm["ma_stack"] else "", ST="▲",
            Score=score, Entry=round(ltp,2),
            Stop=round(float(ld.iloc[-1])*0.998,2),
            Target=round(ltp+2.5*bm["atr_val"],2),
            Signals=sig_str,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ⑦-A2  ACCUMULATION WATCHLIST
#       High-liquidity stocks coiling below breakout
# ─────────────────────────────────────────────────────────────────────────────

def score_accumulation(symbol: str, bm: dict, mode: str) -> dict | None:
    """
    Detect liquid stocks in 15-minute accumulation/consolidation.

    Heuristic:
      - High traded value liquidity on daily data.
      - Current 15m session has a tight multi-bar range.
      - LTP is still below the 15m range high, so it is "yet to breakout".
      - 15m volume is quiet/controlled, not already expanded.
      - Prefer constructive context near VWAP/range high for options watchlist.
    """
    cd = bm["cd"]; hd = bm["hd"]; ld = bm["ld"]; vd = bm["vd"]
    if len(cd) < 60:
        return None

    ltp = bm["ltp"]
    avg_vol20 = bm["avg_vol20"]
    avg_value_cr = (avg_vol20 * ltp) / 10_000_000
    if avg_value_cr < 25:
        return None

    vol_today = float(vd.iloc[-1])
    vol_x = vol_today / avg_vol20 if avg_vol20 > 0 else 0
    if vol_x > 1.35:
        return None

    ma20 = float(cd.rolling(20).mean().iloc[-1])
    ma50 = float(cd.rolling(50).mean().iloc[-1])
    if not (ltp >= ma50 * 0.98 or bm["st_bull"]):
        return None

    df15 = fetch_15min(symbol)
    if df15 is None or len(df15) < 5:
        return None

    c15 = _s(df15["Close"]); h15 = _s(df15["High"])
    l15 = _s(df15["Low"]);   v15 = _s(df15["Volume"])
    vwap15 = calc_vwap(h15, l15, c15, v15)
    vwap_now = float(vwap15.iloc[-1]) if len(vwap15) else ltp

    best = None
    max_lookback = min(16, len(df15))
    for bars in range(max_lookback, 4, -1):
        window_h = h15.iloc[-bars:]
        window_l = l15.iloc[-bars:]
        window_c = c15.iloc[-bars:]
        window_v = v15.iloc[-bars:]
        range_high = float(window_h.max())
        range_low = float(window_l.min())
        if range_low <= 0:
            continue

        range_pct = (range_high - range_low) / range_low * 100
        recent_vol = float(window_v.tail(min(3, len(window_v))).mean())
        base_vol = float(window_v.mean())
        vol_x_15m = recent_vol / base_vol if base_vol > 0 else 0

        tight_enough = range_pct <= max(1.8, min(3.2, bm["atr_pct"] * 0.9))
        below_breakout = ltp < range_high * 0.998
        close_to_trigger = ltp >= range_high * 0.975
        above_floor = ltp >= range_low * 1.006
        quiet_volume = vol_x_15m <= 1.15
        no_bar_breakout = float(window_c.max()) < range_high * 0.999

        if tight_enough and below_breakout and close_to_trigger and above_floor and quiet_volume and no_bar_breakout:
            best = (bars, range_high, range_low, range_pct, vol_x_15m)
            break

    if not best:
        return None

    bars, range_high, range_low, range_pct, vol_x_15m = best
    breakout_gap_pct = (range_high - ltp) / ltp * 100 if ltp > 0 else 0
    if breakout_gap_pct <= 0:
        return None

    dry_volume = vol_x <= 0.85 and vol_x_15m <= 0.95
    constructive = (ltp >= ma20 or bm["st_bull"]) and ltp >= vwap_now * 0.995
    score = 0
    if bars >= 12: score += 4
    elif bars >= 8: score += 3
    elif bars >= 6: score += 2
    else: score += 1
    if avg_value_cr >= 100: score += 3
    elif avg_value_cr >= 50: score += 2
    else: score += 1
    if dry_volume: score += 2
    if constructive: score += 2
    if breakout_gap_pct <= 3: score += 2
    elif breakout_gap_pct <= 5: score += 1

    return dict(
        Symbol=symbol,
        LTP=round(ltp, 2),
        Close=round(ltp, 2),
        Timeframe='15m',
        Accumulation_Bars=int(bars),
        Accumulation_Days=int(bars),
        Accumulation_Time=f"{bars * 15}m",
        Range_High=round(range_high, 2),
        Range_Low=round(range_low, 2),
        Range_pct=round(range_pct, 2),
        Breakout_Above=round(range_high, 2),
        Gap_To_Breakout_pct=round(breakout_gap_pct, 2),
        Avg_Value_Cr=round(avg_value_cr, 1),
        Vol_x=round(vol_x, 2),
        Vol15_x=round(vol_x_15m, 2),
        VWAP15=round(vwap_now, 2),
        Score=score,
        Signals=" | ".join([
            f"15m accumulation {bars} bars ({bars * 15}m)",
            f"15m range {range_pct:.1f}%",
            f"breakout above ₹{range_high:.2f}",
            "dry volume" if dry_volume else "quiet 15m volume",
            "constructive trend" if constructive else "base building",
        ]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑦-B  BOS CONTINUATION SCANNER
#       Confirmed BOS + pullback to level + resumption
# ─────────────────────────────────────────────────────────────────────────────

def score_bos(symbol: str, bm: dict, nifty_ret: float, mode: str) -> dict | None:
    """
    Scores a stock for BOS CONTINUATION entry.
    Requires: confirmed daily BOS (1-5 bars ago) + price pulled back
    to broken level + resumption signal.
    """
    bos = bm["bos_d"]

    # Must have a fresh confirmed bullish BOS
    if not bos["bos_up"] or not bos["confirmed"] or bos["invalidated"]:
        return None
    if bos["bos_bar_ago"] is None or bos["bos_bar_ago"] == 0:
        return None  # BOS happening today → that's breakout table

    ltp      = bm["ltp"]
    atr_val  = bm["atr_val"]
    atr_pct  = bm["atr_pct"]
    rsi_now  = bm["rsi_now"]
    st_bull  = bm["st_bull"]
    iv_risk  = bm["iv_risk"]
    pct_chg  = bm["pct_chg"]
    cd       = bm["cd"]; hd = bm["hd"]; ld = bm["ld"]; vd = bm["vd"]

    bos_level = bos["bos_level"]

    # PBS detection
    pbs = detect_pbs(cd, hd, ld, vd, bos_level, atr_val)

    if not pbs["pbs_valid"]:
        return None  # price not near the BOS level yet

    score = 0; sigs = []; warns = []

    # 1. PBS quality is the primary signal
    pbs_s = pbs["pbs_score"]
    if   pbs_s >= 8: score+=6; sigs.append(f"🎯 PBS Tier-1 ({pbs_s}/10)")
    elif pbs_s >= 6: score+=4; sigs.append(f"⬆ PBS Tier-2 ({pbs_s}/10)")
    elif pbs_s >= 4: score+=2; sigs.append(f"PBS Tier-3 ({pbs_s}/10)")
    else:            score+=1; sigs.append(f"PBS weak ({pbs_s}/10)")

    # 2. BOS freshness
    bag = bos["bos_bar_ago"]
    if   bag == 1: score+=3; sigs.append("BOS 1d ago")
    elif bag == 2: score+=2; sigs.append("BOS 2d ago")
    elif bag <= 4: score+=1; sigs.append(f"BOS {bag}d ago")

    # 3. Resumption confirmation
    if pbs["resuming"]:  score+=3; sigs.append("Resumption↑")
    if pbs["vol_declining"]: score+=2; sigs.append("PB vol↓ (healthy)")

    # 4. Supertrend alignment (bullish = confirms direction)
    if st_bull: score+=1; sigs.append("ST▲(D)")
    else: score-=1; warns.append("⚠ ST▼(D)")

    # 5. RSI — ideal: 45–60 on pullback (not overbought, not collapsed)
    if   rsi_now>75:      score-=1; warns.append(f"RSI {rsi_now:.0f} OB⚠")
    elif 45<=rsi_now<=65: score+=2; sigs.append(f"RSI {rsi_now:.0f} (pullback zone)")
    elif rsi_now>40:      score+=1; sigs.append(f"RSI {rsi_now:.0f}")
    else:                 warns.append(f"RSI {rsi_now:.0f} weak")

    # 6. Intraday 5m confirmation (intraday mode)
    if mode == "intraday" and bm["df5"] is not None:
        df5 = bm["df5"]
        if len(df5) >= 6:
            c5 = _s(df5["Close"]); h5 = _s(df5["High"])
            l5 = _s(df5["Low"]);   v5 = _s(df5["Volume"])
            sh5, sl5 = find_swing_points(h5, l5, lookback=2)
            # 5m CHoCH confirms pullback is ending
            choch5 = detect_choch(h5, l5, c5, v5, sh5, sl5, atr_val)
            if choch5["choch_bull"]:
                score+=3; sigs.append("5m CHoCH✓ (PB ending)")
            vwap5    = calc_vwap(h5,l5,c5,v5)
            if float(c5.iloc[-1]) > float(vwap5.iloc[-1]):
                score+=2; sigs.append("AbvVWAP(5m)")

    # 7. RS
    rs_net = bm["rs"] - nifty_ret
    if rs_net>=3: score+=1; sigs.append(f"RS+{rs_net:.0f}%")

    # 8. IV guard
    if iv_risk: score-=2; warns.append(f"⚠ IV risk ({pct_chg:+.1f}%)")

    # 9. Time gate (intraday)
    if mode == "intraday":
        ih = ist_hour()
        if (9.5<=ih<=11.5) or (13.5<=ih<=14.0): score+=1; sigs.append("⏰ Prime")
        if ih>14.0: score-=1; warns.append("⚠ Late")

    score = max(score,0)
    sig_str = " | ".join(sigs)+("  "+" ".join(warns) if warns else "")

    # R:R estimation
    entry  = round(ltp, 2)
    stop   = pbs["stop_level"]
    risk   = entry - stop
    target = round(entry + 3.0 * risk, 2) if risk > 0 else round(entry + 2*atr_val, 2)
    rr     = round(3.0 * risk / risk, 1) if risk > 0 else 3.0

    return dict(
        Symbol=symbol, Close=round(ltp,2), Chg_pct=round(pct_chg,2),
        BOS_Level=round(bos_level,2), BOS_Ago=f"{bag}d",
        Dist_BOS=f"{pbs['dist_pct']:+.2f}%" if pbs["dist_pct"] else "—",
        PBS=pbs["quality"], RSI=round(rsi_now,1), ATR_pct=round(atr_pct,2),
        ST="▲" if st_bull else "▼", IVRisk="⚠" if iv_risk else "✓",
        Score=score, Entry=entry, Stop=stop, Target=target,
        RR=f"1:{rr}", Signals=sig_str,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑦-C  CHoCH REVERSAL SCANNER
#       Counter-trend: prior downmove structure flipping bullish
# ─────────────────────────────────────────────────────────────────────────────

def score_choch(symbol: str, bm: dict, nifty_ret: float, mode: str) -> dict | None:
    """
    Scores a stock for CHoCH REVERSAL entry.
    Requires: confirmed bearish structure on daily → first HL + HH flip
    → close above last swing high → volume confirmation.
    """
    choch = bm["choch_d"]

    # Need a fresh bullish CHoCH
    if not choch["choch_bull"]:
        return None
    if choch["choch_bar_ago"] is None or choch["choch_bar_ago"] > 4:
        return None

    ltp     = bm["ltp"]
    atr_val = bm["atr_val"]
    atr_pct = bm["atr_pct"]
    rsi_now = bm["rsi_now"]
    st_bull = bm["st_bull"]
    iv_risk = bm["iv_risk"]
    pct_chg = bm["pct_chg"]
    cd      = bm["cd"]; hd = bm["hd"]; ld = bm["ld"]; vd = bm["vd"]

    choch_level = choch["choch_level"]

    score = 0; sigs = []; warns = []

    # 1. CHoCH quality
    q = choch["quality"]
    if "Strong"   in str(q): score+=6; sigs.append(f"🔄 Strong CHoCH")
    elif "Moderate" in str(q): score+=4; sigs.append(f"🔄 Moderate CHoCH")
    else:                    score+=2; sigs.append(f"CHoCH (weak)")

    # 2. Prior structure confirmation
    if choch["prior_structure"]: score+=2; sigs.append("LH+LL prior ✓")

    # 3. HL formed (key reversal signal)
    if choch["hl_formed"]:  score+=2; sigs.append("HL formed ✓")

    # 4. Volume on CHoCH bar
    if choch["vol_confirmed"]: score+=2; sigs.append("Vol confirmed ✓")
    else: warns.append("⚠ Low vol on CHoCH")

    # 5. CHoCH freshness
    bag = choch["choch_bar_ago"]
    if   bag == 0: score+=3; sigs.append("CHoCH today")
    elif bag == 1: score+=2; sigs.append("CHoCH 1d ago")
    elif bag <= 3: score+=1; sigs.append(f"CHoCH {bag}d ago")

    # 6. Supertrend
    # For CHoCH, ST is often still bearish (that's fine — it's a reversal)
    # ST bullish flip is extra confirmation
    if bm["st_flip"]:   score+=3; sigs.append("ST flip↑ aligns!")
    elif st_bull:       score+=1; sigs.append("ST▲ already bullish")
    else:               sigs.append("ST▼ (reversal context)")

    # 7. RSI: ideal for reversal = 30–55 (oversold bounce or momentum pick-up)
    if   rsi_now > 70:         score-=1; warns.append(f"RSI {rsi_now:.0f} OB⚠")
    elif 30 <= rsi_now <= 55:  score+=3; sigs.append(f"RSI {rsi_now:.0f} (reversal zone)")
    elif 55 < rsi_now <= 70:   score+=1; sigs.append(f"RSI {rsi_now:.0f}")
    else:                      sigs.append(f"RSI {rsi_now:.0f}")

    # 8. MACD on daily — crossover confirms reversal
    ml,sl,ht = calc_macd(cd)
    macd_x   = float(ml.iloc[-2])<float(sl.iloc[-2]) and float(ml.iloc[-1])>=float(sl.iloc[-1])
    macd_b   = float(ml.iloc[-1])>float(sl.iloc[-1])
    if macd_x:          score+=3; sigs.append("MACD✕↑")
    elif macd_b:        score+=1; sigs.append("MACD bull")

    # 9. 5m CHoCH confirmation (intraday mode)
    if mode == "intraday" and bm["df5"] is not None:
        df5 = bm["df5"]
        if len(df5) >= 8:
            c5=_s(df5["Close"]); h5=_s(df5["High"]); l5=_s(df5["Low"]); v5=_s(df5["Volume"])
            sh5,sl5 = find_swing_points(h5,l5,lookback=2)
            choch5  = detect_choch(h5,l5,c5,v5,sh5,sl5,atr_val)
            if choch5["choch_bull"]:
                score+=3; sigs.append("5m CHoCH aligns!")
            vwap5 = calc_vwap(h5,l5,c5,v5)
            if float(c5.iloc[-1])>float(vwap5.iloc[-1]):
                score+=2; sigs.append("AbvVWAP(5m)")
            elif float(c5.iloc[-1]) < float(vwap5.iloc[-1]):
                warns.append("⚠ Below VWAP(5m)")

    # 10. RS (for reversals, even neutral RS acceptable)
    rs_net = bm["rs"] - nifty_ret
    if rs_net >= 0: score+=1; sigs.append(f"RS{rs_net:+.0f}%")

    # 11. IV guard
    if iv_risk: score-=2; warns.append(f"⚠ IV risk ({pct_chg:+.1f}%)")

    # 12. Time gate
    if mode == "intraday":
        ih = ist_hour()
        if (9.5<=ih<=11.5) or (13.5<=ih<=14.0): score+=1; sigs.append("⏰ Prime")
        if ih>14.0: score-=1; warns.append("⚠ Late")

    score = max(score,0)
    sig_str = " | ".join(sigs)+("  "+" ".join(warns) if warns else "")

    # R:R estimation
    entry  = round(ltp,2)
    stop   = choch["stop_level"] or round(ltp - 2*atr_val, 2)
    risk   = entry - stop
    target = round(entry + 3.0*risk, 2) if risk > 0 else round(entry + 2*atr_val, 2)

    return dict(
        Symbol=symbol, Close=round(ltp,2), Chg_pct=round(pct_chg,2),
        CHoCH_Level=round(choch_level,2) if choch_level else 0,
        CHoCH_Ago=f"{bag}d", Quality=choch["quality"],
        RSI=round(rsi_now,1), ATR_pct=round(atr_pct,2),
        ST="▲" if st_bull else "▼", IVRisk="⚠" if iv_risk else "✓",
        Score=score, Entry=entry, Stop=stop, Target=target,
        RR=f"1:{round(3.0,1)}", Signals=sig_str,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ DISPLAY
# ─────────────────────────────────────────────────────────────────────────────

def fmt_score(s):
    if s>=14: return Fore.GREEN+Style.BRIGHT+f"{s:2d}"+Style.RESET_ALL
    if s>=10: return Fore.GREEN+f"{s:2d}"+Style.RESET_ALL
    if s>= 6: return Fore.YELLOW+f"{s:2d}"+Style.RESET_ALL
    return Fore.WHITE+f"{s:2d}"+Style.RESET_ALL

def _sep(w=165): return "═"*w

def print_breakout_table(results, top_n, list_name, mode):
    if not results:
        print(Fore.YELLOW+"\n  [BREAKOUT] No candidates.\n"); return
    top = sorted(results, key=lambda x: x["Score"], reverse=True)[:top_n]
    ts  = datetime.now().strftime("%d %b %Y  %H:%M")
    print(f"\n{_sep()}")
    print(Fore.GREEN+Style.BRIGHT+
          f"  🚀  TABLE 1 — BREAKOUT ENTRIES  [{mode.upper()}]  |  "
          f"{list_name}  ({len(results)} hits)  |  {ts}")
    print(_sep()+Style.RESET_ALL)

    if mode=="intraday":
        hdrs=["Symbol","Close","Chg%","RVOL","ORB","VWAP","AbvV","RSI","ATR%","ST","IVRisk","Score","Entry","Stop","Target","Signals"]
        rows=[]
        for r in top:
            gc=Fore.GREEN if r["Chg_pct"]>=0 else Fore.RED
            vc=(Fore.GREEN+Style.BRIGHT if r["RVOL"]>=3 else Fore.YELLOW if r["RVOL"]>=2 else Fore.WHITE)
            ic=Fore.RED if r["IVRisk"]=="⚠" else Fore.GREEN
            rows.append([
                Fore.CYAN+f"{r['Symbol']:<13}"+Style.RESET_ALL,
                f"₹{r['Close']:>9,.2f}",
                gc+f"{r['Chg_pct']:>+6.2f}%"+Style.RESET_ALL,
                vc+f"{r['RVOL']:>5.1f}x"+Style.RESET_ALL,
                f"{r['ORB']:>8}",
                f"₹{r['VWAP']:>9,.2f}",
                r["AbvVWAP"],
                f"{r['RSI']:>5.1f}",
                f"{r['ATR_pct']:>5.2f}%",
                r["ST"],
                ic+r["IVRisk"]+Style.RESET_ALL,
                fmt_score(r["Score"]),
                f"₹{r['Entry']:>9,.2f}",
                f"₹{r['Stop']:>9,.2f}",
                f"₹{r['Target']:>9,.2f}",
                r["Signals"][:60],
            ])
    else:
        hdrs=["Symbol","Close","Chg%","Vol/Avg","BO Type","RSI","ATR%","Dist52W","MAStk","ST","Score","Entry","Stop","Target","Signals"]
        rows=[]
        for r in top:
            gc=Fore.GREEN if r["Chg_pct"]>=0 else Fore.RED
            vc=(Fore.GREEN+Style.BRIGHT if r["Vol_x"]>=3 else Fore.YELLOW if r["Vol_x"]>=2 else Fore.WHITE)
            rows.append([
                Fore.CYAN+f"{r['Symbol']:<13}"+Style.RESET_ALL,
                f"₹{r['Close']:>9,.2f}",
                gc+f"{r['Chg_pct']:>+6.2f}%"+Style.RESET_ALL,
                vc+f"{r['Vol_x']:>5.1f}x"+Style.RESET_ALL,
                r["BO_Type"],
                f"{r['RSI']:>5.1f}",
                f"{r['ATR_pct']:>5.2f}%",
                f"{r['Dist52W']:>5.1f}%",
                r["MAStk"], r["ST"],
                fmt_score(r["Score"]),
                f"₹{r['Entry']:>9,.2f}",
                f"₹{r['Stop']:>9,.2f}",
                f"₹{r['Target']:>9,.2f}",
                r["Signals"][:60],
            ])

    print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    print(Fore.WHITE+Style.DIM+
          "Entry=current price  |  Stop=ORB low (intraday) or day low (swing)  |  "
          "Target=Entry + 2.5×ATR  |  IVRisk ⚠ = day move >3.5%\n"+Style.RESET_ALL)


def print_bos_table(results, top_n, list_name, mode):
    if not results:
        print(Fore.YELLOW+"\n  [BOS] No pullback candidates at broken structure.\n"); return
    top = sorted(results, key=lambda x: x["Score"], reverse=True)[:top_n]
    ts  = datetime.now().strftime("%d %b %Y  %H:%M")
    print(f"\n{_sep()}")
    print(Fore.CYAN+Style.BRIGHT+
          f"  🎯  TABLE 2 — BOS CONTINUATION  (Pullback to Broken Structure)  [{mode.upper()}]  |  "
          f"{list_name}  ({len(results)} hits)  |  {ts}")
    print(_sep()+Style.RESET_ALL)

    hdrs=["Symbol","Close","Chg%","BOS Level","BOS Ago","Dist","PBS Quality","RSI","ATR%","ST","IVRisk","Score","Entry","Stop","Target","R:R","Signals"]
    rows=[]
    for r in top:
        gc=Fore.GREEN if r["Chg_pct"]>=0 else Fore.RED
        ic=Fore.RED if r["IVRisk"]=="⚠" else Fore.GREEN
        sc=Fore.GREEN if r["ST"]=="▲" else Fore.RED
        rows.append([
            Fore.CYAN+f"{r['Symbol']:<13}"+Style.RESET_ALL,
            f"₹{r['Close']:>9,.2f}",
            gc+f"{r['Chg_pct']:>+6.2f}%"+Style.RESET_ALL,
            f"₹{r['BOS_Level']:>9,.2f}",
            r["BOS_Ago"],
            r["Dist_BOS"],
            Fore.GREEN+r["PBS"]+Style.RESET_ALL,
            f"{r['RSI']:>5.1f}",
            f"{r['ATR_pct']:>5.2f}%",
            sc+r["ST"]+Style.RESET_ALL,
            ic+r["IVRisk"]+Style.RESET_ALL,
            fmt_score(r["Score"]),
            f"₹{r['Entry']:>9,.2f}",
            f"₹{r['Stop']:>9,.2f}",
            f"₹{r['Target']:>9,.2f}",
            r["RR"],
            r["Signals"][:55],
        ])
    print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    print(Fore.WHITE+Style.DIM+
          "BOS Level = broken swing high  |  Dist = distance from BOS level  |  "
          "PBS Tier-1 = at level + vol declining + resuming  |  "
          "Stop = just below BOS level  |  Target = 3×Risk\n"+Style.RESET_ALL)


def print_choch_table(results, top_n, list_name, mode):
    if not results:
        print(Fore.YELLOW+"\n  [CHoCH] No reversal setups detected.\n"); return
    top = sorted(results, key=lambda x: x["Score"], reverse=True)[:top_n]
    ts  = datetime.now().strftime("%d %b %Y  %H:%M")
    print(f"\n{_sep()}")
    print(Fore.MAGENTA+Style.BRIGHT+
          f"  🔄  TABLE 3 — CHoCH REVERSAL ENTRIES  [{mode.upper()}]  |  "
          f"{list_name}  ({len(results)} hits)  |  {ts}")
    print(_sep()+Style.RESET_ALL)

    hdrs=["Symbol","Close","Chg%","CHoCH Level","CHoCH Ago","Quality","RSI","ATR%","ST","IVRisk","Score","Entry","Stop","Target","R:R","Signals"]
    rows=[]
    for r in top:
        gc=Fore.GREEN if r["Chg_pct"]>=0 else Fore.RED
        ic=Fore.RED if r["IVRisk"]=="⚠" else Fore.GREEN
        rows.append([
            Fore.CYAN+f"{r['Symbol']:<13}"+Style.RESET_ALL,
            f"₹{r['Close']:>9,.2f}",
            gc+f"{r['Chg_pct']:>+6.2f}%"+Style.RESET_ALL,
            f"₹{r['CHoCH_Level']:>9,.2f}",
            r["CHoCH_Ago"],
            Fore.MAGENTA+str(r["Quality"])+Style.RESET_ALL,
            f"{r['RSI']:>5.1f}",
            f"{r['ATR_pct']:>5.2f}%",
            r["ST"],
            ic+r["IVRisk"]+Style.RESET_ALL,
            fmt_score(r["Score"]),
            f"₹{r['Entry']:>9,.2f}",
            f"₹{r['Stop']:>9,.2f}",
            f"₹{r['Target']:>9,.2f}",
            r["RR"],
            r["Signals"][:55],
        ])
    print(tabulate(rows, headers=hdrs, tablefmt="rounded_outline"))
    print(Fore.WHITE+Style.DIM+
          "CHoCH Level = swing high broken to confirm reversal  |  "
          "Stop = recent swing low (below reversal structure)  |  "
          "Target = 3×Risk  |  Counter-trend: use smaller size vs BOS/Breakout\n"+Style.RESET_ALL)


# ─────────────────────────────────────────────────────────────────────────────
# ⑨-A  PROGRAMMATIC API  (imported by Flask backend)
# ─────────────────────────────────────────────────────────────────────────────

def scan(universe: str = 'NIFTY50', mode: str = 'intraday',
         min_score: int = 6, vol_multiplier: float = None,
         top_n: int = 15, progress_callback=None) -> dict:
    """Run the full 3-table scan and return structured results.

    This is the entry point used by the Flask backend (/api/fno-alerts/run).
    Can also be called from any Python script for batch processing.

    Args:
        universe:       Key from STOCK_LISTS (e.g. 'ALL_FNO', 'NIFTY50')
        mode:           'intraday' or 'swing'
        min_score:      Minimum score threshold for inclusion
        vol_multiplier: RVOL threshold (default: 1.5 intraday, 2.0 swing)
        top_n:          Max results per table

    Returns:
        {
          'breakout':   [list of row dicts sorted by score desc],
          'bos':        [list of row dicts],
          'choch':      [list of row dicts],
          'accumulation': [high-liquidity stocks coiling below breakout],
          'skipped':    [list of symbol strings],
          'scanned_at': 'HH:MM' IST string,
          'universe':   universe key,
          'mode':       mode,
          'total':      int,
          'summary': {
              'breakoutCount': int, 'bosCount': int, 'chochCount': int
          }
        }
    """
    if vol_multiplier is None:
        vol_multiplier = 1.5 if mode == 'intraday' else 2.0

    if universe == 'ALL_EQUITY':
        try:
            conn = _get_db()
            rows = conn.execute("SELECT DISTINCT tradingsymbol, exchange FROM instruments WHERE instrument_type = 'EQ' AND exchange IN ('NSE', 'BSE')").fetchall()
            conn.close()
            
            kite = _get_kite()
            if not kite:
                symbols = [r['tradingsymbol'] for r in rows]
                print(f"  ⚠ No Kite client: loaded all {len(symbols)} equity symbols (cannot filter by price).")
            else:
                all_keys = [f"{r['exchange']}:{r['tradingsymbol']}" for r in rows]
                filtered_symbols = []
                for i in range(0, len(all_keys), 400):
                    batch = all_keys[i:i+400]
                    try:
                        quotes = kite.quote(batch)
                        for key, val in quotes.items():
                            if val.get('last_price', 0) > 300 and val.get('volume', 0) >= 100000:
                                filtered_symbols.append(key.split(':')[1])
                    except Exception as e:
                        pass
                symbols = list(set(filtered_symbols))
                print(f"  Loaded {len(symbols)} equity symbols with price > 300.")
        except Exception as e:
            print(f"  ⚠ Failed to load ALL_EQUITY from DB: {e}")
            symbols = []  # Don't fallback silently to FNO so we can see the error
    else:
        symbols = STOCK_LISTS.get(universe, NIFTY50)

    nifty_ret = get_nifty_20d_return()

    bo_results, bos_results, choch_results, acc_results, skipped = [], [], [], [], []

    import time
    total_symbols = len(symbols)
    for i, sym in enumerate(symbols):
        if progress_callback: progress_callback(i + 1, total_symbols, f"Scanning {sym} ({i+1}/{total_symbols})...")
        if universe == 'ALL_EQUITY':
            time.sleep(0.35)  # Enforce Kite rate limit of 3 req/sec only for massive equity scans
        bm = base_metrics(sym, mode)
        if bm is None:
            skipped.append(sym)
            continue

        r_bo = score_breakout(sym, bm, vol_multiplier, nifty_ret, mode)
        if r_bo and r_bo.get('Score', 0) >= min_score:
            bo_results.append(r_bo)

        r_bos = score_bos(sym, bm, nifty_ret, mode)
        if r_bos and r_bos.get('Score', 0) >= min_score:
            bos_results.append(r_bos)

        r_ch = score_choch(sym, bm, nifty_ret, mode)
        if r_ch and r_ch.get('Score', 0) >= min_score:
            choch_results.append(r_ch)

        r_acc = score_accumulation(sym, bm, mode)
        if r_acc:
            acc_results.append(r_acc)

    # Sort by score descending, cap at top_n
    bo_results.sort(key=lambda x: x.get('Score', 0), reverse=True)
    bos_results.sort(key=lambda x: x.get('Score', 0), reverse=True)
    choch_results.sort(key=lambda x: x.get('Score', 0), reverse=True)
    acc_results.sort(key=lambda x: (x.get('Accumulation_Days', 0), x.get('Avg_Value_Cr', 0)), reverse=True)

    if progress_callback: progress_callback(total_symbols, total_symbols, "Finalizing results...")
    ist = timezone(timedelta(hours=5, minutes=30))
    ts = datetime.now(ist).strftime('%H:%M')

    return {
        'breakout':   bo_results[:top_n],
        'bos':        bos_results[:top_n],
        'choch':      choch_results[:top_n],
        'accumulation': acc_results[:top_n],
        'skipped':    skipped,
        'scanned_at': ts,
        'universe':   universe,
        'mode':       mode,
        'total':      len(bo_results) + len(bos_results) + len(choch_results),
        'summary': {
            'breakoutCount': len(bo_results),
            'bosCount':      len(bos_results),
            'chochCount':    len(choch_results),
            'accumulationCount': len(acc_results),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# ⑨-B  CLI MAIN  (standalone batch execution)
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="NSE F&O Scanner v5.1 — Breakout | BOS Continuation | CHoCH Reversal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tables:
  TABLE 1 BREAKOUT   — ORB / volume surge / VWAP acceptance (above/below)
  TABLE 2 BOS        — Confirmed BOS 1-5 bars ago + pullback to level + resuming
  TABLE 3 CHoCH      — Prior downtrend structure flipped bullish (reversal)

Modes:
  intraday (default) — ORB, RVOL, 5-min structure, VWAP, IV guard, time gate
  swing (--swing)    — Daily breakouts, 52W highs, MA stack, swing structure

Lists: ALL_FNO / NIFTY50 / BANKNIFTY / FINNIFTY / NIFTYIT /
       NIFTYPHARMA / NIFTYMETAL / NIFTYAUTO / NIFTYFMCG / NIFTYREALTY / NIFTYPSE

Risk notes:
  Breakout → widest stop, highest IV, fastest decay. Size: 0.5×
  BOS      → tightest stop, lowest IV, best R:R.    Size: 1×
  CHoCH    → counter-trend, use 0.5× size, strict stop at CHoCH low
        """
    )
    parser.add_argument("--swing",          action="store_true",
                        help="Swing/positional mode (default: intraday)")
    parser.add_argument("--list",           default="ALL_FNO",
                        choices=list(STOCK_LISTS.keys()) + ["ALL_EQUITY"])
    parser.add_argument("--vol-multiplier", type=float, default=None)
    parser.add_argument("--top",            type=int,   default=15)
    parser.add_argument("--min-score",      type=int,   default=6)
    parser.add_argument("--no-save",        action="store_true")
    parser.add_argument("--loop",           action="store_true",
                        help="Run continuously in a loop")
    parser.add_argument("--loop-interval",  type=int,   default=300,
                        help="Seconds to wait between loop runs (default: 300)")
    args = parser.parse_args()

    is_loop = BACKGROUND_RUN or args.loop
    mode = "swing" if args.swing else "intraday"
    if args.vol_multiplier is None:
        args.vol_multiplier = 1.5 if mode=="intraday" else 2.0

    try:
        while True:
            if args.list == 'ALL_EQUITY':
                try:
                    conn = _get_db()
                    rows = conn.execute("SELECT DISTINCT tradingsymbol, exchange FROM instruments WHERE instrument_type = 'EQ' AND exchange IN ('NSE', 'BSE')").fetchall()
                    conn.close()
                    
                    kite = _get_kite()
                    if not kite:
                        symbols = [r['tradingsymbol'] for r in rows]
                        print(f"  ⚠ No Kite client: loaded all {len(symbols)} equity symbols (cannot filter by price).")
                    else:
                        all_keys = [f"{r['exchange']}:{r['tradingsymbol']}" for r in rows]
                        filtered_symbols = []
                        for i in range(0, len(all_keys), 400):
                            batch = all_keys[i:i+400]
                            try:
                                quotes = kite.quote(batch)
                                for key, val in quotes.items():
                                    if val.get('last_price', 0) > 300 and val.get('volume', 0) >= 100000:
                                        filtered_symbols.append(key.split(':')[1])
                            except Exception as e:
                                pass
                        symbols = list(set(filtered_symbols))
                        print(f"  Loaded {len(symbols)} equity symbols with price > 300.")
                except Exception as e:
                    print(f"  ⚠ Failed to load ALL_EQUITY from DB: {e}")
                    symbols = []
            else:
                symbols = STOCK_LISTS[args.list]
            total   = len(symbols)

            print(Fore.CYAN+Style.BRIGHT+
                  f"\n⚡  NSE F&O Scanner v5.1  |  BREAKOUT + BOS + CHoCH\n"
                  f"    Mode={mode.upper()}  List={args.list}  "
                  f"{total} stocks  RVOL≥{args.vol_multiplier}x  score≥{args.min_score}\n")

            print(Fore.WHITE+Style.DIM+"  Fetching Nifty50 20d return...")
            nifty_ret = get_nifty_20d_return()
            print(Fore.WHITE+Style.DIM+f"  Nifty50 20d: {nifty_ret:+.2f}%\n"+Style.RESET_ALL)

            bo_results   = []
            bos_results  = []
            choch_results= []
            skipped      = []

            for i, sym in enumerate(symbols, 1):
                pct = i/total*100
                bar = "█"*int(pct/4)+"░"*(25-int(pct/4))
                print(f"  [{bar}] {pct:5.1f}%  {sym:<15}", end="\r")

                bm = base_metrics(sym, mode)
                if bm is None:
                    skipped.append(sym); time.sleep(0.25); continue

                # Table 1 — Breakout
                r_bo = score_breakout(sym, bm, args.vol_multiplier, nifty_ret, mode)
                if r_bo and r_bo["Score"] >= args.min_score:
                    bo_results.append(r_bo)

                # Table 2 — BOS continuation
                r_bos = score_bos(sym, bm, nifty_ret, mode)
                if r_bos and r_bos["Score"] >= args.min_score:
                    bos_results.append(r_bos)

                # Table 3 — CHoCH reversal
                r_ch = score_choch(sym, bm, nifty_ret, mode)
                if r_ch and r_ch["Score"] >= args.min_score:
                    choch_results.append(r_ch)

                time.sleep(0.25)

            print(" "*72)
            if skipped:
                print(Fore.YELLOW+Style.DIM+
                      f"  ⚠ Skipped: {', '.join(skipped)}\n"+Style.RESET_ALL)

            # Print all three tables
            print_breakout_table(bo_results,    args.top, args.list, mode)
            print_bos_table     (bos_results,   args.top, args.list, mode)
            print_choch_table   (choch_results, args.top, args.list, mode)

            # Summary
            print(Fore.CYAN+Style.BRIGHT+
                  f"\n  📊  SUMMARY  |  "
                  f"Breakout: {len(bo_results)}  |  "
                  f"BOS Continuation: {len(bos_results)}  |  "
                  f"CHoCH Reversal: {len(choch_results)}\n")
            print(Fore.YELLOW+
                  "  Score: "+
                  Fore.GREEN+Style.BRIGHT+"≥14 Exceptional  "+
                  Fore.GREEN+"10–13 Strong  "+
                  Fore.YELLOW+"6–9 Moderate  "+
                  Fore.WHITE+"<6 Weak\n")
            print(Fore.WHITE+Style.DIM+
                  "  Position sizing: BOS=1× | CHoCH=0.5× | Breakout=0.5×\n"+
                  Style.RESET_ALL)

            if not args.no_save:
                ts = datetime.now().strftime("%Y%m%d_%H%M")
                for tag, data in [("breakout",bo_results),("bos",bos_results),("choch",choch_results)]:
                    if data:
                        fname = f"fno_{tag}_{args.list}_{ts}.csv"
                        pd.DataFrame(data).to_csv(fname, index=False)
                        print(Fore.GREEN+f"  💾 {fname}")
                print()

            if not is_loop:
                break
            print(Fore.CYAN + f"  [Loop Mode] Sleeping for {args.loop_interval} seconds... (Press Ctrl+C to exit)\n" + Style.RESET_ALL)
            time.sleep(args.loop_interval)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n  Loop stopped by user. Exiting." + Style.RESET_ALL)


if __name__ == "__main__":
    main()
