"""
OI Scanner — Blueprint routes for TradeSignal server.py
Reuses get_kite() from server context. Mounted at /api/oi
"""

import json, math, time, threading, logging
from datetime import datetime, timedelta, date
from flask import Blueprint, request, jsonify

log = logging.getLogger("OIScanner")

oi_scanner_bp = Blueprint("oi_scanner", __name__, url_prefix="/api/oi")

# ─── Constants ────────────────────────────────────────────────────────────────
RISK_FREE_RATE  = 0.065
MIN_OI          = 500
MIN_VOLUME      = 100
MAX_SPREAD_PCT  = 0.50

# NSE F&O universe — options trade on NFO segment
FNO_SYMBOLS = [
    # ── NSE Indices (NFO) ──
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    # ── Banking & Finance ──
    "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK",
    "BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "ICICIPRULI",
    "ICICIGI", "CHOLAFIN", "M&MFIN", "MUTHOOTFIN", "SHRIRAMFIN",
    "MANAPPURAM", "PNBHOUSING", "CANFINHOME", "LTIM", "FEDERALBNK",
    "PNB", "BANKBARODA", "IDFCFIRSTB", "RBLBANK", "AUBANK",
    # ── IT / Tech ──
    "INFY", "TCS", "WIPRO", "HCLTECH", "TECHM", "LTTS", "MPHASIS",
    "PERSISTENT", "COFORGE", "KPITTECH",
    # ── Oil & Gas / Energy ──
    "RELIANCE", "ONGC", "BPCL", "IOC", "HINDPETRO", "GAIL",
    "POWERGRID", "NTPC", "TATAPOWER", "ADANIGREEN", "ADANIPORTS",
    "ADANIENT", "ADANIPOWER", "CESC", "TORNTPOWER",
    # ── Auto ──
    "TATAMOTORS", "MARUTI", "M&M", "BAJAJ-AUTO", "HEROMOTOCO",
    "EICHERMOT", "ASHOKLEY", "TVSMOTOR", "BALKRISIND", "MRF",
    "BHARATFORG", "MOTHERSON", "BOSCHLTD",
    # ── Metal & Mining ──
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "COALINDIA",
    "NMDC", "NATIONALUM", "HINDCOPPER", "SAIL",
    # ── FMCG ──
    "HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR",
    "MARICO", "COLPAL", "GODREJCP", "EMAMILTD", "VBL",
    # ── Pharma / Healthcare ──
    "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN",
    "AUROPHARMA", "BIOCON", "ALKEM", "TORNTPHARM", "ABBOTINDIA",
    "IPCALAB", "APOLLOHOSP", "MAXHEALTH",
    # ── Infra / Capital Goods ──
    "LT", "LTTS", "ABB", "SIEMENS", "BHEL", "CUMMINSIND",
    "HAVELLS", "VOLTAS", "POLYCAB", "APLAPOLLO",
    # ── Cement ──
    "ULTRACEMCO", "GRASIM", "AMBUJACEM", "ACC", "SHREECEM",
    "JKCEMENT", "DALMIABL",
    # ── Real Estate ──
    "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "BRIGADE",
    # ── Telecom ──
    "BHARTIARTL", "IDEA",
    # ── Chemicals ──
    "PIDILITIND", "DEEPAKNTR", "NAVINFLUOR", "ATUL",
    "TATACHEMICALS", "GNFC",
    # ── Consumer Durables / Retail ──
    "TITAN", "TRENT", "DMART", "NYKAA", "ZOMATO", "PAYTM",
    "NAUKRI", "INDIAMART", "POLICYBZR",
    # ── Others ──
    "INDUSINDBK", "YESBANK", "UPL", "PIIND", "ASIANPAINT",
    "BERGERPAINTS", "PAGEIND", "BATAINDIA", "CONCOR",
    "INDIGO", "SPICEJET", "IRCTC", "RVNL", "HAL", "BEL",
    "MCDOWELL-N", "UNITDSPR", "RADICO",
]

# BSE F&O universe — SENSEX and BANKEX options trade on BFO segment
BFO_SYMBOLS = ["SENSEX", "BANKEX"]

# All scannable symbols = NFO + BFO
ALL_SCAN_SYMBOLS = FNO_SYMBOLS + BFO_SYMBOLS

# Which Kite exchange segment each symbol uses for quote calls
EXCHANGE_MAP = {s: "BFO" for s in BFO_SYMBOLS}  # rest default to NFO

# Kite LTP symbol map (indices need explicit mapping; stocks default to NSE:<sym>)
SPOT_MAP = {
    "NIFTY":      "NSE:NIFTY 50",
    "BANKNIFTY":  "NSE:NIFTY BANK",
    "FINNIFTY":   "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "SENSEX":     "BSE:SENSEX",
    "BANKEX":     "BSE:BANKEX",
}

# Topbar tickers
WATCHLIST_TICKERS = [
    "NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:INDIA VIX",
    "NSE:NIFTY FIN SERVICE", "NSE:NIFTY MID SELECT",
    "BSE:SENSEX", "BSE:BANKEX",
    "NSE:RELIANCE", "NSE:HDFCBANK", "NSE:ICICIBANK",
    "NSE:INFY", "NSE:TCS", "NSE:SBIN",
]

_inst_cache  = {"data": None, "ts": 0}   # NFO instruments
_bfo_cache   = {"data": None, "ts": 0}   # BFO instruments
INST_TTL     = 4 * 3600
_chain_cache = {}
_state_lock  = threading.Lock()

# ─── Time helpers ─────────────────────────────────────────────────────────────

def ist_now():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)

def is_market_open():
    now = ist_now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    return ((h > 9) or (h == 9 and m >= 15)) and ((h < 15) or (h == 15 and m < 30))

def trading_time_to_expiry(expiry_dt):
    now          = ist_now().replace(tzinfo=None)
    expiry_close = datetime.combine(expiry_dt, datetime.min.time()).replace(hour=15, minute=30)
    if now >= expiry_close:
        return 1e-6
    total_mins, cursor = 0, now.date()
    while cursor <= expiry_dt:
        if cursor.weekday() >= 5:
            cursor += timedelta(days=1); continue
        day_open  = datetime.combine(cursor, datetime.min.time()).replace(hour=9,  minute=15)
        day_close = datetime.combine(cursor, datetime.min.time()).replace(hour=15, minute=30)
        start = max(now, day_open) if cursor == now.date() else day_open
        end   = day_close if cursor < expiry_dt else expiry_close
        total_mins += max(0, int((end - start).total_seconds() / 60))
        cursor += timedelta(days=1)
    return max(total_mins, 1) / (252.0 * 375.0)

def find_monthly_expiry(expiries):
    for exp in sorted(expiries, reverse=True):
        y, m = exp.year, exp.month
        last_day = date(y+1,1,1) - timedelta(days=1) if m == 12 else date(y, m+1, 1) - timedelta(days=1)
        last_thu = last_day - timedelta(days=(last_day.weekday() - 3) % 7)
        if exp == last_thu:
            return exp
    return None

# ─── Greeks ───────────────────────────────────────────────────────────────────

def _ncdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def _npdf(x): return math.exp(-0.5*x*x) / math.sqrt(2.0*math.pi)

def calculate_greeks(S, K, T, r, sigma, opt_type="CE"):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, iv=round(sigma*100,2), bs_price=0.0)
    try:
        sq = math.sqrt(T)
        d1 = (math.log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sq)
        d2 = d1 - sigma*sq
        df = math.exp(-r*T)
        if opt_type == "CE":
            bs    = S*_ncdf(d1) - K*df*_ncdf(d2)
            delta = _ncdf(d1)
            theta = (-(S*_npdf(d1)*sigma)/(2.0*sq) - r*K*df*_ncdf(d2)) / 365.0
        else:
            bs    = K*df*_ncdf(-d2) - S*_ncdf(-d1)
            delta = _ncdf(d1) - 1.0
            theta = (-(S*_npdf(d1)*sigma)/(2.0*sq) + r*K*df*_ncdf(-d2)) / 365.0
        gamma = _npdf(d1) / (S*sigma*sq)
        vega  = S*_npdf(d1)*sq / 100.0
        return dict(delta=round(delta,4), gamma=round(gamma,6), theta=round(theta,4),
                    vega=round(vega,4), iv=round(sigma*100.0,2), bs_price=round(bs,2))
    except (ValueError, ZeroDivisionError, OverflowError):
        return dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, iv=round(sigma*100,2), bs_price=0.0)

def implied_vol(market_px, S, K, T, r, opt_type="CE"):
    if T <= 0 or market_px <= 0 or S <= 0 or K <= 0: return 0.0
    intrinsic = max(0.0, (S-K) if opt_type=="CE" else (K-S))
    if market_px < intrinsic - 0.01: return 0.0
    sigma = 0.30
    for _ in range(60):
        g    = calculate_greeks(S, K, T, r, sigma, opt_type)
        diff = g["bs_price"] - market_px
        vega = g["vega"] * 100.0
        if abs(diff) < 5e-4: return round(sigma*100.0, 2)
        if abs(vega) < 1e-8: break
        sigma -= diff/vega
        sigma  = max(0.005, min(sigma, 10.0))
    lo, hi = 0.005, 10.0
    for _ in range(120):
        mid  = (lo+hi)*0.5
        diff = calculate_greeks(S, K, T, r, mid, opt_type)["bs_price"] - market_px
        if abs(diff) < 5e-4: return round(mid*100.0, 2)
        if diff < 0: lo = mid
        else:        hi = mid
    return round(((lo+hi)*0.5)*100.0, 2)

def bs_reprice(S_new, K, T, r, sigma, opt_type):
    return max(0.0, calculate_greeks(S_new, K, T, r, sigma, opt_type)["bs_price"])

def premium_gain_pct(ltp, S, K, T, r, sigma, opt_type, spot_move_pct):
    if ltp <= 0 or T <= 0: return 0.0
    sign  = +1.0 if opt_type == "CE" else -1.0
    S_new = S * (1.0 + sign*spot_move_pct/100.0)
    new_px = bs_reprice(S_new, K, T, r, sigma, opt_type)
    return round(((new_px - ltp)/ltp)*100.0, 1) if ltp > 0 else 0.0

def prob_score(delta, gamma, iv, T_days, gain_2pct, spread_pct=0.0, oi=0, volume=0):
    d = abs(delta)
    if   0.20 <= d <= 0.45: d_score = 85
    elif 0.10 <= d <  0.20: d_score = 50
    elif 0.45 <  d <= 0.65: d_score = 65
    else:                   d_score = 20
    g_score  = min(100.0, gamma * 70000.0)
    iv_score = 90 if iv<13 else 75 if iv<18 else 55 if iv<25 else 35 if iv<35 else 12
    dte_score = 95 if 1<=T_days<=2 else 82 if T_days<=5 else 65 if T_days<=10 else 45 if T_days<=21 else 25
    pot_score = min(100.0, gain_2pct/4.0)
    base = d_score*0.22 + g_score*0.20 + iv_score*0.18 + dte_score*0.20 + pot_score*0.20
    spread_pen = min(35.0, spread_pct*120.0)
    oi_bonus   = 12.0 if oi>100000 else 6.0 if oi>20000 else 0.0
    vol_bonus  =  6.0 if volume>50000 else 2.0 if volume>10000 else 0.0
    return round(min(95.0, max(5.0, base - spread_pen + oi_bonus + vol_bonus)), 1)

# ─── Kite helpers ─────────────────────────────────────────────────────────────

def get_instruments(kite):
    """Fetch NFO instruments (NSE F&O stocks + indices) from SQLite."""
    from db_instruments import get_cached_instruments
    return get_cached_instruments("NFO")

def get_bfo_instruments(kite):
    """Fetch BFO instruments (BSE F&O — SENSEX, BANKEX options) from SQLite."""
    from db_instruments import get_cached_instruments
    return get_cached_instruments("BFO")

def get_all_instruments(kite):
    """Merged NFO + BFO instrument list."""
    return get_instruments(kite) + get_bfo_instruments(kite)

def inst_prefix(sym):
    """Return the Kite exchange prefix for a symbol's options."""
    return EXCHANGE_MAP.get(sym, "NFO")

def kite_quote_chunked(kite, symbols, chunk=500):
    result = {}
    for i in range(0, len(symbols), chunk):
        try:    result.update(kite.quote(symbols[i:i+chunk]))
        except Exception as e: log.warning(f"Quote chunk {i}: {e}")
    return result


# ─── Routes ───────────────────────────────────────────────────────────────────

@oi_scanner_bp.route("/health")
def oi_health():
    from server import get_kite
    kite = get_kite()
    return jsonify({
        "status":            "ok",
        "kite_connected":    kite is not None,
        "market_open":       is_market_open(),
        "instruments_cached": _inst_cache["data"] is not None,
        "chain_cache":       list(_chain_cache.keys()),
    })


@oi_scanner_bp.route("/tickers")
def oi_tickers():
    from server import get_kite
    kite = get_kite()
    if not kite:
        return jsonify({"error": "Kite not connected"}), 401
    try:
        data = kite.ltp(WATCHLIST_TICKERS[:15])
        return jsonify({"data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@oi_scanner_bp.route("/options-chain")
def options_chain():
    from server import get_kite
    kite = get_kite()
    if not kite:
        return jsonify({"error": "Kite not connected"}), 401

    symbol   = request.args.get("symbol", "NIFTY").strip().upper()
    expiry_s = request.args.get("expiry", "").strip()

    try:
        instruments = get_all_instruments(kite)
        filtered    = [i for i in instruments
                       if i["name"] == symbol and i["instrument_type"] in ("CE","PE")]
        pfx = inst_prefix(symbol)
        if not filtered:
            return jsonify({"error": f"No {pfx} contracts for {symbol}"}), 404

        today    = date.today()
        expiries = sorted({i["expiry"] for i in filtered if i["expiry"] and i["expiry"] >= today})
        if not expiries:
            return jsonify({"error": "No upcoming expiries"}), 404

        target = datetime.strptime(expiry_s, "%Y-%m-%d").date() if expiry_s else expiries[0]

        chain = {}
        for inst in filtered:
            if inst["expiry"] != target: continue
            k = float(inst["strike"])
            chain.setdefault(k, {"strike": k, "CE": None, "PE": None})
            chain[k][inst["instrument_type"]] = inst

        strikes_sorted = sorted(chain.values(), key=lambda x: x["strike"])

        spot_sym   = SPOT_MAP.get(symbol, f"NSE:{symbol}")
        spot_price = 0.0
        try:
            spot_price = kite.ltp([spot_sym])[spot_sym]["last_price"]
        except Exception: pass

        atm_idx = (min(range(len(strikes_sorted)),
                       key=lambda i: abs(strikes_sorted[i]["strike"] - spot_price))
                   if spot_price else len(strikes_sorted)//2)
        nearby = strikes_sorted[max(0, atm_idx-12): atm_idx+13]

        tokens = [f"{pfx}:{s[t]['tradingsymbol']}"
                  for s in nearby for t in ("CE","PE") if s[t]]
        quotes = kite_quote_chunked(kite, tokens + [spot_sym]) if tokens else {}
        
        spot_q = quotes.get(spot_sym, {})
        spot_prev = float(spot_q.get('ohlc', {}).get('close', spot_q.get('close_price', spot_price)))
        if not spot_prev: spot_prev = spot_price

        T      = trading_time_to_expiry(target)
        T_days = (target - today).days
        r      = RISK_FREE_RATE
        enriched = []

        for s in nearby:
            row = {"strike": s["strike"], "T_days": T_days}
            for opt_type in ("CE","PE"):
                inst = s[opt_type]
                if not inst:
                    row[opt_type] = None; continue
                key  = f"{pfx}:{inst['tradingsymbol']}"
                q    = quotes.get(key, {})
                ltp  = float(q.get("last_price",0) or 0)
                oi   = int(q.get("oi",0) or 0)
                oi_day_low = int(q.get("oi_day_low",0) or 0)
                vol  = int(q.get("volume",0) or 0)
                depth = q.get("depth",{})
                bid   = float((depth.get("buy")  or [{}])[0].get("price",0) or 0)
                ask   = float((depth.get("sell") or [{}])[0].get("price",0) or 0)
                spread_pct = ((ask-bid)/ltp) if ltp>0 and ask>bid else 0.0
                
                prev_px = float(q.get('ohlc', {}).get('close', q.get('close_price', ltp)))
                iv_prev = 0

                if ltp>0 and spot_price>0:
                    iv_pct = implied_vol(ltp, spot_price, s["strike"], T, r, opt_type)
                    sigma  = max(iv_pct/100.0, 0.05)
                else:
                    sigma = max((float(q.get("iv",0) or 0))/100.0, 0.15)
                    iv_pct = sigma * 100.0
                
                if prev_px > 0 and spot_prev > 0:
                    iv_prev = implied_vol(prev_px, spot_prev, s["strike"], T, r, opt_type)

                greeks = calculate_greeks(spot_price, s["strike"], T, r, sigma, opt_type)
                gains  = {f"gain_{p}pct": premium_gain_pct(ltp, spot_price, s["strike"], T, r, sigma, opt_type, p)
                          for p in (1,2,3)}
                
                oi_chg_from_low = oi - oi_day_low
                oi_chg = int(q.get("oi_day_high",0) or 0) - oi
                
                oi_trend = "Increasing" if oi_chg_from_low > (oi_day_low * 0.02) else "Reducing" if oi_chg_from_low < -(oi_day_low * 0.02) else "Flat"
                if oi_day_low == 0: oi_trend = "Increasing" if oi_chg_from_low > 0 else "Flat"
                px_trend = "Rising" if ltp > (prev_px * 1.01) else "Falling" if ltp < (prev_px * 0.99) else "Flat"
                if prev_px == 0: px_trend = "Flat"
                iv_trend = "Rising" if iv_pct > (iv_prev * 1.01) else "Falling" if iv_pct < (iv_prev * 0.99) else "Flat"
                if iv_prev == 0: iv_trend = "Flat"

                row[opt_type] = {
                    "tradingsymbol": inst["tradingsymbol"],
                    "ltp": round(ltp,2), "bid": round(bid,2), "ask": round(ask,2),
                    "spread_pct": round(spread_pct*100,1), "oi": oi, "oi_change": oi_chg, "volume": vol,
                    "oi_trend": oi_trend, "px_trend": px_trend, "iv_trend": iv_trend,
                    **greeks, **gains,
                    "prob_score": prob_score(greeks["delta"], greeks["gamma"], greeks["iv"],
                                             T_days, gains["gain_2pct"], spread_pct, oi, vol),
                }
            enriched.append(row)

        final_retail_action = ""
        final_h_action = ""
        highest_conviction = 0

        sorted_rows = sorted(enriched, key=lambda x: abs(x['strike'] - spot_price))
        for i, r_data in enumerate(sorted_rows):
            loc_str = "Close" if i <= 8 else "Far"
            is_above = r_data['strike'] >= spot_price
            is_below = r_data['strike'] < spot_price
            
            for opt_type in ('CE', 'PE'):
                inst = r_data.get(opt_type)
                if not inst: continue
                
                oi_d = inst.get('oi_trend', 'Flat')
                px_d = inst.get('px_trend', 'Flat')
                iv_d = inst.get('iv_trend', 'Flat')
                
                action = ""
                h_action = ""
                
                if oi_d == "Reducing" and px_d == "Rising" and iv_d == "Rising":
                    if opt_type == 'CE' and is_above and i <= 4:
                        h_action = "⭐⭐⭐⭐⭐ CE BUY immediately"
                    elif opt_type == 'PE' and is_below and i <= 4:
                        h_action = "⭐⭐⭐⭐⭐ PE BUY immediately"
                elif oi_d == "Increasing" and px_d == "Rising" and iv_d == "Rising":
                    if opt_type == 'CE' and is_above and i <= 6:
                        h_action = "⭐⭐⭐⭐ CE BUY"
                    elif opt_type == 'PE' and is_below and i <= 6:
                        h_action = "⭐⭐⭐⭐ PE BUY"
                elif oi_d == "Increasing" and px_d == "Falling" and iv_d == "Falling":
                    if opt_type == 'PE' and is_below and i <= 8:
                        h_action = "⭐⭐⭐ WAIT — floor building"
                    elif opt_type == 'CE' and is_above and i <= 8:
                        h_action = "⭐⭐⭐ WAIT — wall building"
                elif oi_d == "Flat" and px_d != "Flat" and iv_d == "Flat":
                    h_action = "❌ IGNORE"
                elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Rising":
                    h_action = "⚠️ WAIT — retail trap likely"

                if opt_type == 'CE':
                    if oi_d == "Increasing" and px_d == "Falling" and iv_d == "Falling":
                        if is_above and loc_str == "Close": action = "WAIT (Building wall)"
                        elif is_above and loc_str == "Far": action = "IGNORE"
                        elif is_below and loc_str == "Close": action = "PE BUY"
                    elif oi_d == "Increasing" and px_d == "Rising" and iv_d == "Rising":
                        if is_above and loc_str == "Close": action = "CE BUY"
                        elif is_above and loc_str == "Far": action = "IGNORE"
                        elif is_below and loc_str == "Close": action = "CE BUY"
                    elif oi_d == "Reducing" and px_d == "Rising" and iv_d == "Rising":
                        if is_above and loc_str == "Close": action = "CE BUY"
                        elif is_below and loc_str == "Close": action = "IGNORE" # ITM unwinding
                    elif oi_d == "Reducing" and px_d == "Falling" and iv_d == "Falling":
                        if is_above and loc_str == "Close": action = "IGNORE"
                        elif is_below and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Reducing" and px_d == "Rising" and iv_d == "Falling":
                        if is_above and loc_str == "Close": action = "CE BUY — cautious"
                        elif is_below and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Reducing" and px_d == "Falling" and iv_d == "Rising":
                        if is_above and loc_str == "Close": action = "WAIT"
                        elif is_below and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Rising":
                        if is_above and loc_str == "Close": action = "WAIT"
                    elif oi_d == "Flat" and px_d == "Falling" and iv_d == "Falling":
                        if is_above and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Flat":
                        if is_above and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Flat" and px_d == "Flat" and iv_d == "Falling":
                        if is_above and loc_str == "Close": action = "PE BUY consideration"
                    elif oi_d == "Flat" and px_d == "Flat" and iv_d == "Rising":
                        if is_above and loc_str == "Close": action = "WAIT"
                
                elif opt_type == 'PE':
                    if oi_d == "Increasing" and px_d == "Falling" and iv_d == "Falling":
                        if is_below and loc_str == "Close": action = "WAIT (Building floor)"
                        elif is_below and loc_str == "Far": action = "IGNORE"
                        elif is_above and loc_str == "Close": action = "CE BUY"
                    elif oi_d == "Increasing" and px_d == "Rising" and iv_d == "Rising":
                        if is_below and loc_str == "Close": action = "PE BUY"
                        elif is_below and loc_str == "Far": action = "IGNORE"
                        elif is_above and loc_str == "Close": action = "PE BUY"
                    elif oi_d == "Reducing" and px_d == "Rising" and iv_d == "Rising":
                        if is_below and loc_str == "Close": action = "PE BUY"
                        elif is_above and loc_str == "Close": action = "IGNORE" # ITM unwinding
                    elif oi_d == "Reducing" and px_d == "Falling" and iv_d == "Falling":
                        if is_below and loc_str == "Close": action = "IGNORE"
                        elif is_above and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Reducing" and px_d == "Rising" and iv_d == "Falling":
                        if is_below and loc_str == "Close": action = "PE BUY — cautious"
                        elif is_above and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Reducing" and px_d == "Falling" and iv_d == "Rising":
                        if is_below and loc_str == "Close": action = "WAIT"
                        elif is_above and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Rising":
                        if is_below and loc_str == "Close": action = "WAIT"
                    elif oi_d == "Flat" and px_d == "Falling" and iv_d == "Falling":
                        if is_below and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Flat":
                        if is_below and loc_str == "Close": action = "IGNORE"
                    elif oi_d == "Flat" and px_d == "Flat" and iv_d == "Falling":
                        if is_below and loc_str == "Close": action = "CE BUY consideration"
                    elif oi_d == "Flat" and px_d == "Flat" and iv_d == "Rising":
                        if is_below and loc_str == "Close": action = "WAIT"

                if action and "IGNORE" not in action and not final_retail_action:
                    final_retail_action = action
                if h_action and "IGNORE" not in h_action and not final_h_action:
                    final_h_action = h_action

        result = {"symbol": symbol, "spot": round(spot_price,2), "expiry": str(target),
                  "T_days": T_days, "available_expiries": [str(e) for e in expiries[:8]],
                  "retail_action": final_retail_action or "WAIT (No clear setup)",
                  "h_action": final_h_action,
                  "strikes": enriched}
        with _state_lock:
            _chain_cache[symbol] = result
        return jsonify(result)
    except Exception as e:
        log.exception("options-chain error")
        return jsonify({"error": str(e)}), 500


@oi_scanner_bp.route("/oi-analysis")
def oi_analysis():
    symbol = request.args.get("symbol", "NIFTY").upper()
    chain  = _chain_cache.get(symbol)
    if not chain:
        return jsonify({"error": f"Load chain first: /api/oi/options-chain?symbol={symbol}"}), 404
    spot     = chain.get("spot", 0)
    analysis = []
    for s in chain["strikes"]:
        ce = s.get("CE") or {}
        pe = s.get("PE") or {}
        ce_oi = ce.get("oi",0) or 0
        pe_oi = pe.get("oi",0) or 0
        if ce_oi==0 and pe_oi==0: continue
        pcr    = round(pe_oi/ce_oi,2) if ce_oi>0 else 999
        signal = "BULLISH" if pcr>1.3 else "BEARISH" if pcr<0.7 else "NEUTRAL"
        analysis.append({
            "strike": s["strike"], "ce_oi": ce_oi, "pe_oi": pe_oi,
            "ce_price": ce.get("ltp",0), "pe_price": pe.get("ltp",0),
            "ce_iv": ce.get("iv",0), "pe_iv": pe.get("iv",0),
            "ce_vol": ce.get("volume",0), "pe_vol": pe.get("volume",0),
            "pcr": pcr, "signal": signal,
            "dist_pct": round(abs(s["strike"]-spot)/spot*100,2) if spot else 0,
        })
    return jsonify({"symbol": symbol, "spot": spot, "expiry": chain.get("expiry"),
                    "analysis": analysis, "updated": datetime.now().isoformat()})


@oi_scanner_bp.route("/scan", methods=["POST"])
def realtime_scan():
    from server import get_kite
    kite = get_kite()
    if not kite:
        return jsonify({"error": "Kite not connected — re-login required"}), 401
    if not is_market_open():
        t = ist_now()
        return jsonify({"error": f"Market closed. IST: {t.strftime('%H:%M %A')}. Hours: Mon–Fri 09:15–15:30 IST."}), 400

    body        = request.get_json() or {}
    capital     = float(body.get("capital",   50000))
    min_prob_th = float(body.get("min_prob",  40))
    risk_mode   = body.get("risk_mode", "aggressive")
    expiry_pref = body.get("expiry",    "weekly")
    scan_start  = time.time()
    candidates  = []

    # Step 1: Spot prices — chunked to handle full F&O universe
    spot_nse  = [SPOT_MAP.get(s, f"NSE:{s}") for s in ALL_SCAN_SYMBOLS]
    spot_prices = {}
    for chunk_start in range(0, len(spot_nse), 50):
        chunk_syms = spot_nse[chunk_start:chunk_start+50]
        chunk_fnos = ALL_SCAN_SYMBOLS[chunk_start:chunk_start+50]
        try:
            ltp_data = kite.ltp(chunk_syms)
            for nse, fno in zip(chunk_syms, chunk_fnos):
                if nse in ltp_data:
                    spot_prices[fno] = ltp_data[nse]["last_price"]
        except Exception as e:
            log.warning(f"Spot LTP chunk {chunk_start}: {e}")

    vix = 15.0
    try:
        vix = kite.ltp(["NSE:INDIA VIX"]).get("NSE:INDIA VIX",{}).get("last_price",15.0)
    except Exception: pass

    # Step 2: Instruments (cached)
    try:
        all_insts = get_all_instruments(kite)
    except Exception as e:
        return jsonify({"error": f"Instruments unavailable: {e}"}), 500

    today    = date.today()
    inst_map = {}
    for inst in all_insts:
        if inst["name"] not in ALL_SCAN_SYMBOLS: continue
        if inst["instrument_type"] not in ("CE","PE"): continue
        if not inst["expiry"] or inst["expiry"] < today: continue
        inst_map.setdefault(inst["name"],{}).setdefault(inst["expiry"],[]).append(inst)

    # Step 3: Per-symbol scan
    for fno_sym in ALL_SCAN_SYMBOLS:
        spot = spot_prices.get(fno_sym, 0.0)
        if spot <= 0 or fno_sym not in inst_map: continue
        expiries = sorted(inst_map[fno_sym].keys())
        if not expiries: continue

        if expiry_pref == "weekly":      target_exp = expiries[0]
        elif expiry_pref == "next_week": target_exp = expiries[1] if len(expiries)>1 else expiries[0]
        elif expiry_pref == "monthly":
            monthly = find_monthly_expiry(expiries)
            target_exp = monthly if monthly else expiries[-1]
        else: target_exp = expiries[0]

        T      = trading_time_to_expiry(target_exp)
        T_days = (target_exp - today).days
        if T <= 1e-5: continue

        r     = RISK_FREE_RATE
        insts = inst_map[fno_sym][target_exp]
        strikes    = sorted({float(i["strike"]) for i in insts})
        atm_strike = min(strikes, key=lambda x: abs(x-spot))
        atm_idx    = strikes.index(atm_strike)
        otm_range  = {"aggressive":3,"moderate":2,"conservative":1}.get(risk_mode,3)
        call_strikes = set(strikes[atm_idx+1: atm_idx+1+otm_range])
        put_strikes  = set(strikes[max(0, atm_idx-otm_range): atm_idx])

        cand_insts = [i for i in insts
                      if float(i["strike"]) in call_strikes or float(i["strike"]) in put_strikes]
        tokens = [f"{inst_prefix(fno_sym)}:{i['tradingsymbol']}" for i in cand_insts]
        if not tokens: continue
        try:
            quotes = kite_quote_chunked(kite, tokens)
        except Exception as e:
            log.warning(f"Quotes {fno_sym}: {e}"); continue

        def _score_opt(inst, opt_type, strike_val,
                       _spot=spot, _quotes=quotes, _T=T, _T_days=T_days, _r=r):
            key  = f"{inst_prefix(fno_sym)}:{inst['tradingsymbol']}"
            q    = _quotes.get(key,{})
            ltp  = float(q.get("last_price",0) or 0)
            oi   = int(q.get("oi",0) or 0)
            vol  = int(q.get("volume",0) or 0)
            if ltp<=0 or oi<MIN_OI or vol<MIN_VOLUME: return None
            depth      = q.get("depth",{})
            bid        = float((depth.get("buy")  or [{}])[0].get("price",0) or 0)
            ask        = float((depth.get("sell") or [{}])[0].get("price",0) or 0)
            spread_pct = ((ask-bid)/ltp) if ltp>0 and ask>bid else 0.0
            if spread_pct > MAX_SPREAD_PCT: return None
            iv_pct = implied_vol(ltp, _spot, strike_val, _T, _r, opt_type)
            sigma  = max(iv_pct/100.0, 0.05) if iv_pct>0 else max(vix/100.0, 0.10)
            greeks = calculate_greeks(_spot, strike_val, _T, _r, sigma, opt_type)
            gains  = {f"gain_{p}pct": premium_gain_pct(ltp, _spot, strike_val, _T, _r, sigma, opt_type, p)
                      for p in (1,2,3)}
            score      = prob_score(greeks["delta"], greeks["gamma"], greeks["iv"],
                                    _T_days, gains["gain_2pct"], spread_pct, oi, vol)
            oi_day_hi  = int(q.get("oi_day_high", oi) or oi)
            oi_signal  = ("BUILDUP" if oi>oi_day_hi*0.9 else "UNWIND" if oi<oi_day_hi*0.5 else "NEUTRAL")
            momentum   = "HIGH" if vol>50000 else "MED" if vol>10000 else "LOW"
            lot_size   = int(inst.get("lot_size") or 0) or 50
            cap_req    = ltp * lot_size
            lots_poss  = max(1, int(capital/cap_req)) if cap_req>0 else 1
            sign  = +1.0 if opt_type=="CE" else -1.0
            t1_px = round(bs_reprice(_spot*(1+sign*0.015), strike_val, _T, _r, sigma, opt_type),1)
            sl_px = round(ltp*0.40, 1)
            return {
                "symbol": fno_sym, "tradingsymbol": inst["tradingsymbol"],
                "expiry": str(target_exp), "T_days": _T_days, "opt_type": opt_type,
                "spot": round(_spot,2), "strike": strike_val, "ltp": round(ltp,2),
                "bid": round(bid,2), "ask": round(ask,2), "spread_pct": round(spread_pct*100,1),
                "oi": oi, "oi_change": oi-oi_day_hi, "oi_signal": oi_signal,
                "volume": vol, "momentum": momentum, "lot_size": lot_size,
                "lots_possible": lots_poss, "capital_req": round(cap_req,0),
                "entry_premium": round(ltp,1), "target1": t1_px, "sl_premium": sl_px,
                **gains, "prob_score": score, **greeks,
            }

        for inst in insts:
            sv = float(inst["strike"])
            ot = inst["instrument_type"]
            if ot=="CE" and sv in call_strikes:
                c = _score_opt(inst,"CE",sv)
                if c and c["prob_score"]>=min_prob_th: candidates.append(c)
            elif ot=="PE" and sv in put_strikes:
                c = _score_opt(inst,"PE",sv)
                if c and c["prob_score"]>=min_prob_th: candidates.append(c)

    candidates.sort(key=lambda x: x["prob_score"], reverse=True)
    top = candidates[:15]
    if not top:
        return jsonify({
            "success": True, "candidates": [], "top_3": [], "total_scanned": len(candidates),
            "message": "No liquid candidates met the threshold. Try: lower min_prob or change expiry.",
            "scan_time_ms": round((time.time()-scan_start)*1000), "vix": vix,
        })

    # Bias from VIX
    market_bias = "SIDEWAYS"
    if vix < 14: market_bias = "BULLISH"
    elif vix > 20: market_bias = "BEARISH"
    vix_comment = (f"VIX {vix:.1f} — low volatility, options cheap, gamma plays favoured"
                   if vix < 14 else
                   f"VIX {vix:.1f} — elevated volatility, prefer closer strikes"
                   if vix > 20 else f"VIX {vix:.1f} — moderate, selective entries")

    top3 = []
    for c in top[:3]:
        top3.append({
            "tradingsymbol": c["tradingsymbol"], "opt_type": c["opt_type"],
            "entry_condition": f"Entry near ₹{c['entry_premium']} on momentum confirmation",
            "premium_entry": c["entry_premium"], "premium_target": c["target1"],
            "premium_sl": c["sl_premium"],
            "position_size": f"{c['lots_possible']} lot{'s' if c['lots_possible']>1 else ''}",
            "conviction": "HIGH" if c["prob_score"]>=70 else "MEDIUM" if c["prob_score"]>=55 else "LOW",
            "reason": (f"Delta {c['delta']}, IV {c['iv']}%, OI {c['oi_signal']}, "
                       f"Gain@2%={c['gain_2pct']}% — Score {c['prob_score']}")
        })

    result = {
        "success": True, "candidates": top, "top_3": top3,
        "market_bias": market_bias, "vix": vix, "vix_comment": vix_comment,
        "total_scanned": len(candidates),
        "scan_time_ms": round((time.time()-scan_start)*1000),
        "timestamp": datetime.now().isoformat(),
    }
    return jsonify(result)


@oi_scanner_bp.route("/positions")
def oi_positions():
    from server import get_kite
    kite = get_kite()
    if not kite: return jsonify({"error": "Kite not connected"}), 401
    try:    return jsonify(kite.positions())
    except Exception as e: return jsonify({"error": str(e)}), 500


# ─── OI Spurt Equity Scanner ──────────────────────────────────────────────────
# Baseline state (captured once at/after 9:30 AM, resets via POST /api/oi/scanner/reset)
_oi_baseline      = {}   # { "NSE:SYMBOL": oi_value }
_oi_baseline_time = None
_oi_baseline_lock = threading.Lock()

SECTOR_MAP = {
    "HDFCBANK":"Banking","ICICIBANK":"Banking","SBIN":"Banking","AXISBANK":"Banking",
    "KOTAKBANK":"Banking","PNB":"Banking","BANKBARODA":"Banking","IDFCFIRSTB":"Banking",
    "FEDERALBNK":"Banking","RBLBANK":"Banking","INDUSINDBK":"Banking","AUBANK":"Banking",
    "BAJFINANCE":"NBFC","BAJAJFINSV":"NBFC","CHOLAFIN":"NBFC","MUTHOOTFIN":"NBFC",
    "SHRIRAMFIN":"NBFC","MANAPPURAM":"NBFC","HDFCLIFE":"Insurance","SBILIFE":"Insurance",
    "ICICIPRULI":"Insurance","ICICIGI":"Insurance","INFY":"IT","TCS":"IT","WIPRO":"IT",
    "HCLTECH":"IT","TECHM":"IT","LTIM":"IT","MPHASIS":"IT","PERSISTENT":"IT","COFORGE":"IT",
    "RELIANCE":"Energy","ONGC":"Energy","BPCL":"Energy","IOC":"Energy","GAIL":"Energy",
    "TATAMOTORS":"Auto","MARUTI":"Auto","M&M":"Auto","BAJAJ-AUTO":"Auto","HEROMOTOCO":"Auto",
    "EICHERMOT":"Auto","ASHOKLEY":"Auto","TVSMOTOR":"Auto","BALKRISIND":"Auto","MRF":"Auto",
    "TATASTEEL":"Metals","JSWSTEEL":"Metals","HINDALCO":"Metals","VEDL":"Metals",
    "COALINDIA":"Commodities","NMDC":"Metals","SAIL":"Metals",
    "HINDUNILVR":"FMCG","ITC":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG","DABUR":"FMCG",
    "MARICO":"FMCG","GODREJCP":"FMCG","COLPAL":"FMCG",
    "SUNPHARMA":"Pharma","DRREDDY":"Pharma","CIPLA":"Pharma","DIVISLAB":"Pharma",
    "LUPIN":"Pharma","AUROPHARMA":"Pharma","BIOCON":"Pharma","ALKEM":"Pharma",
    "TORNTPHARM":"Pharma","APOLLOHOSP":"Healthcare","MAXHEALTH":"Healthcare",
    "LT":"Infra","SIEMENS":"CapGoods","ABB":"CapGoods","BHEL":"CapGoods","HAVELLS":"Electricals",
    "POLYCAB":"Electricals","VOLTAS":"Electricals",
    "NTPC":"Utilities","POWERGRID":"Utilities","TATAPOWER":"Utilities","ADANIGREEN":"Utilities",
    "ADANIPORTS":"Infra","ADANIENT":"Conglomerate",
    "ULTRACEMCO":"Cement","GRASIM":"Cement","AMBUJACEM":"Cement","ACC":"Cement",
    "BHARTIARTL":"Telecom","IDEA":"Telecom",
    "PIDILITIND":"Chemicals","DEEPAKNTR":"Chemicals",
    "TITAN":"Consumer","TRENT":"Retail","DMART":"Retail","ZOMATO":"ConsTech",
    "NAUKRI":"ConsTech","IRCTC":"Travel","INDIGO":"Aviation",
    "NIFTY":"Index","BANKNIFTY":"Index","FINNIFTY":"Index","MIDCPNIFTY":"Index",
}

def _capture_oi_baseline(kite, symbols):
    """Capture OI for all symbols in one pass; skip if already captured today."""
    global _oi_baseline, _oi_baseline_time
    
    if not is_market_open():
        return
        
    now = ist_now()
    # Only capture after 9:30 AM
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return
    with _oi_baseline_lock:
        if _oi_baseline_time:
            if _oi_baseline_time.date() == now.date():
                return
    # Get NFO Futures for OI
    try:
        all_insts = get_all_instruments(kite)
        today = date.today()
        sym_to_fut = {}
        for sym in symbols:
            futs = [i for i in all_insts if i["name"] == sym and i["instrument_type"] == "FUT" and i["expiry"] and i["expiry"] >= today]
            if futs:
                futs.sort(key=lambda x: x["expiry"])
                sym_to_fut[sym] = f"{inst_prefix(sym)}:{futs[0]['tradingsymbol']}"
        
        nfo_keys = list(sym_to_fut.values())
        quotes = kite_quote_chunked(kite, nfo_keys)
        
        baseline = {}
        for sym, fut_key in sym_to_fut.items():
            baseline[sym] = quotes.get(fut_key, {}).get("oi") or 0
            
        with _oi_baseline_lock:
            _oi_baseline      = baseline
            _oi_baseline_time = now
        log.info(f"[OISpurt] Baseline captured for {len(baseline)} symbols at {now.strftime('%H:%M:%S')}")
    except Exception as e:
        log.warning(f"[OISpurt] Baseline capture error: {e}")


def _compute_signal(price_chg, oi_spurt, price_thresh, logic):
    if logic == "price_oi":
        if price_chg >= price_thresh and oi_spurt > 0:  return "CALL"
        if price_chg <= -price_thresh and oi_spurt > 0: return "PUT"
        if price_chg >= price_thresh:                   return "CALL"
        if price_chg <= -price_thresh:                  return "PUT"
    elif logic == "price_only":
        if price_chg >= price_thresh:  return "CALL"
        if price_chg <= -price_thresh: return "PUT"
    elif logic == "oi_only":
        return "CALL" if oi_spurt >= 0 else "PUT"
    return "NEUTRAL"


def _compute_confidence(oi_spurt, price_chg, vol_ratio, oi_thresh, price_thresh, vol_mult):
    conf = 0
    if abs(oi_spurt)    >= oi_thresh:          conf += 1
    if abs(oi_spurt)    >= oi_thresh * 1.5:    conf += 1
    if abs(price_chg)   >= price_thresh:       conf += 1
    if abs(price_chg)   >= price_thresh * 1.5: conf += 1
    if vol_ratio        >= vol_mult:           conf += 1
    return max(1, min(5, conf))


@oi_scanner_bp.route("/scanner")
def oi_spurt_scanner():
    """
    GET /api/oi/scanner
    Equity OI Spurt scanner — scans NSE F&O universe for OI/price spurts.
    Params: oiThresh (15), priceThresh (7), minOI (100), volMult (1.5),
            signalLogic (price_oi), confLogic (combined)
    """
    from server import get_kite
    kite = get_kite()
    if not kite:
        return jsonify({"error": "Kite not connected — authenticate via TradeSignal first"}), 401

    oi_thresh    = float(request.args.get("oiThresh",    15))
    price_thresh = float(request.args.get("priceThresh", 7))
    min_oi       = float(request.args.get("minOI",       100))
    vol_mult     = float(request.args.get("volMult",     1.5))
    signal_logic = request.args.get("signalLogic", "price_oi")

    scan_syms = FNO_SYMBOLS  # ~170 NSE stocks + indices

    # Capture baseline (no-op if already done recently)
    _capture_oi_baseline(kite, scan_syms)

    # Fetch live quotes (NSE for price, NFO for OI)
    nse_keys = [f"NSE:{s}" for s in scan_syms]
    
    # Get futures keys
    all_insts = get_all_instruments(kite)
    today = date.today()
    sym_to_fut = {}
    for sym in scan_syms:
        futs = [i for i in all_insts if i["name"] == sym and i["instrument_type"] == "FUT" and i["expiry"] and i["expiry"] >= today]
        if futs:
            futs.sort(key=lambda x: x["expiry"])
            sym_to_fut[sym] = f"{inst_prefix(sym)}:{futs[0]['tradingsymbol']}"
            
    nfo_keys = list(sym_to_fut.values())
    
    try:
        all_quotes = kite_quote_chunked(kite, nse_keys + nfo_keys)
    except Exception as e:
        return jsonify({"error": f"Quote fetch failed: {e}"}), 500

    alerts = []
    for sym in scan_syms:
        nse_key = f"NSE:{sym}"
        fut_key = sym_to_fut.get(sym)
        
        q_nse = all_quotes.get(nse_key, {})
        q_fut = all_quotes.get(fut_key, {}) if fut_key else {}
        
        if not q_nse and not q_fut:
            continue

        ltp      = float(q_nse.get("last_price") or q_fut.get("last_price") or 0)
        ohlc     = q_nse.get("ohlc") or q_fut.get("ohlc") or {}
        close    = float(ohlc.get("close") or ltp or 1)
        curr_oi  = int(q_fut.get("oi") or 0)
        volume   = int(q_nse.get("volume") or q_fut.get("volume") or 0)
        avg_px   = float(q_nse.get("average_traded_price") or q_fut.get("average_traded_price") or ltp or 1)

        # Price change from previous close
        price_chg = round((ltp - close) / close * 100, 2) if close else 0

        # OI spurt vs baseline (fall back to oi_day_low if no baseline)
        with _oi_baseline_lock:
            base_oi = _oi_baseline.get(sym, 0)
        if base_oi == 0:
            base_oi = int(q_fut.get("oi_day_low") or 0)
        oi_spurt = round((curr_oi - base_oi) / base_oi * 100, 2) if base_oi > 0 else 0

        oi_lots   = round(curr_oi / 100, 0)
        vol_ratio = round(volume / (avg_px * 1e5), 2) if avg_px > 0 else 0

        oi_triggered    = abs(oi_spurt)  >= oi_thresh
        price_triggered = abs(price_chg) >= price_thresh
        oi_above_min    = oi_lots        >= min_oi

        if not (oi_triggered or price_triggered):
            continue
        if not oi_above_min:
            continue

        signal     = _compute_signal(price_chg, oi_spurt, price_thresh, signal_logic)
        confidence = _compute_confidence(oi_spurt, price_chg, vol_ratio,
                                         oi_thresh, price_thresh, vol_mult)
        atm        = round(ltp / 50) * 50
        strike     = (atm + 50) if signal == "CALL" else (atm - 50) if signal == "PUT" else atm

        # Nearest Thursday expiry label
        today = datetime.now()
        days  = (3 - today.weekday()) % 7 or 7
        expiry_dt = today + timedelta(days=days)
        expiry_lbl = expiry_dt.strftime("%d %b")

        alerts.append({
            "symbol":           sym,
            "sector":           SECTOR_MAP.get(sym, "F&O"),
            "ltp":              round(ltp, 2),
            "price_change_pct": price_chg,
            "oi":               int(oi_lots),
            "oi_baseline":      round(base_oi / 100, 0),
            "oi_spurt_pct":     oi_spurt,
            "volume":           volume,
            "vol_ratio":        vol_ratio,
            "signal":           signal,
            "confidence":       confidence,
            "suggested_strike": strike,
            "expiry":           expiry_lbl,
            "triggered_by":     {"oi": oi_triggered, "price": price_triggered},
            "timestamp":        datetime.now().isoformat(),
        })

    alerts.sort(key=lambda x: abs(x["oi_spurt_pct"]), reverse=True)

    return jsonify({
        "status":     "ok",
        "count":      len(alerts),
        "call_count": sum(1 for a in alerts if a["signal"] == "CALL"),
        "put_count":  sum(1 for a in alerts if a["signal"] == "PUT"),
        "alerts":     alerts,
        "baseline_captured": _oi_baseline_time.isoformat() if _oi_baseline_time else None,
        "scan_time":  datetime.now().isoformat(),
    })


@oi_scanner_bp.route("/scanner/reset", methods=["POST"])
def oi_spurt_baseline_reset():
    """POST /api/oi/scanner/reset — force re-capture of OI baseline."""
    from server import get_kite
    kite = get_kite()
    if not kite:
        return jsonify({"error": "Kite not connected"}), 401
    global _oi_baseline, _oi_baseline_time
    with _oi_baseline_lock:
        _oi_baseline      = {}
        _oi_baseline_time = None
    _capture_oi_baseline(kite, FNO_SYMBOLS)
    with _oi_baseline_lock:
        captured = len(_oi_baseline)
    return jsonify({"status": "ok", "symbols": captured,
                    "baseline_time": _oi_baseline_time.isoformat() if _oi_baseline_time else None})

    with _oi_baseline_lock:
        captured = len(_oi_baseline)
    return jsonify({"status": "ok", "symbols": captured,
                    "baseline_time": _oi_baseline_time.isoformat() if _oi_baseline_time else None})

