import threading
import time
import datetime
import logging
from collections import deque, Counter
from session_utils import now_ist

_expiry_thread = None
_token_to_underlying = {}  # Thread-safe mapping of option token -> underlying symbol


def _get_kite():
    from server import get_kite
    return get_kite()

def _send_telegram(msg):
    pass

def _discover_expiry_tokens(kite):
    """Finds today's expiring instruments and targets ATM ± 2 OTM."""
    global _token_to_underlying
    _token_to_underlying.clear()
    today_str = now_ist().strftime('%Y-%m-%d')
    nfo, bfo = [], []
    try:
        from db_instruments import get_cached_instruments
        nfo = get_cached_instruments("NFO")
    except Exception as e:
        logging.error(f"[Expiry Engine] Failed to fetch NFO instruments: {e}")
    try:
        from db_instruments import get_cached_instruments
        bfo = get_cached_instruments("BFO")
    except Exception as e:
        logging.error(f"[Expiry Engine] Failed to fetch BFO instruments: {e}")
        
    instruments = nfo + bfo
    if not instruments:
        return None
    
    expiring_instruments = []
    underlyings = set()
    for i in instruments:
        # For BSE (BFO) derivatives, only include indices SENSEX and BANKEX
        if i.get('exchange') == 'BFO' and i.get('name') not in ['SENSEX', 'BANKEX']:
            continue
            
        if i.get('instrument_type') in ['CE', 'PE']:
            exp = i.get('expiry')
            if exp:
                exp_str = exp.strftime('%Y-%m-%d') if hasattr(exp, 'strftime') else str(exp)[:10]
                if exp_str == today_str:
                    expiring_instruments.append(i)
                    underlyings.add(i['name'])
                    
    if not underlyings:
        return {}
        
    from oi_spurt_routes import EXCHANGE_MAP
    
    # Get live spot prices for underlyings
    spot_queries = []
    sym_map = {}
    for u in underlyings:
        exch_sym = EXCHANGE_MAP.get(u, f"NSE:{u}")
        spot_queries.append(exch_sym)
        sym_map[exch_sym] = u
        
    quotes = {}
    try:
        for i in range(0, len(spot_queries), 500):
            quotes.update(kite.quote(spot_queries[i:i+500]))
    except Exception as e:
        logging.error(f"[Expiry Engine] Failed to fetch spot prices: {e}")
        return None
        
    spot_prices = {}
    for exch_sym, data in quotes.items():
        if 'last_price' in data and data['last_price'] > 0:
            spot_prices[sym_map[exch_sym]] = data['last_price']
            
    if underlyings and not spot_prices:
        return None
            
    # Find ATM and ±2 OTM strikes
    active_tokens = {}
    by_underlying = {}
    for i in expiring_instruments:
        u = i['name']
        if u not in by_underlying:
            by_underlying[u] = []
        by_underlying[u].append(i)
        
    for u, insts in by_underlying.items():
        if u not in spot_prices:
            continue
            
        spot = spot_prices[u]
        
        # Estimate strike step dynamically using the most common difference
        strikes = sorted(list(set(i['strike'] for i in insts)))
        if len(strikes) < 2:
            continue
            
        diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
        step = Counter(diffs).most_common(1)[0][0]
        
        atm_strike = round(spot / step) * step
        
        # Target strikes: ATM, +1, +2, -1, -2
        target_strikes = [
            atm_strike - (2 * step),
            atm_strike - step,
            atm_strike,
            atm_strike + step,
            atm_strike + (2 * step)
        ]
        
        for i in insts:
            if i['strike'] in target_strikes:
                # Store full exchange prefix dynamically with integer token normalization
                token = int(i['instrument_token'])
                active_tokens[token] = f"{i['exchange']}:{i['tradingsymbol']}"
                _token_to_underlying[token] = i['name']  # Map option token to underlying asset
                
    return active_tokens

def _is_active_window(now):
    """Returns True between 13:25 and 15:15 IST"""
    if now.hour == 13 and now.minute >= 25: return True
    if now.hour == 14: return True
    if now.hour == 15 and now.minute <= 15: return True
    return False

def _is_trigger_window(now):
    """Returns True between 13:30 and 15:15 IST"""
    if now.hour == 13 and now.minute >= 30: return True
    if now.hour == 14: return True
    if now.hour == 15 and now.minute <= 15: return True
    return False

def _generate_eod_report(kite, alerted_underlyings):
    """Generates a premium, formatted Telegram EOD report for alerted underlyings."""
    if not alerted_underlyings:
        return None

    today_str = now_ist().strftime('%Y-%m-%d')
    try:
        from oi_spurt_routes import EXCHANGE_MAP
        
        queries = []
        sym_to_underlying = {}
        for u in alerted_underlyings:
            exch_sym = EXCHANGE_MAP.get(u.upper(), f"NSE:{u}")
            queries.append(exch_sym)
            sym_to_underlying[exch_sym] = u

        # Batch fetch spot quotes for high efficiency
        quotes = {}
        for i in range(0, len(queries), 500):
            quotes.update(kite.quote(queries[i:i+500]))

        report_lines = []
        for exch_sym, d in quotes.items():
            underlying = sym_to_underlying.get(exch_sym, exch_sym)
            ltp = d.get("last_price", 0)
            net_chg_abs = d.get("net_change", 0) or 0
            prev_close = d.get("ohlc", {}).get("close", 0) or 0

            if prev_close > 0:
                price_change_pct = (net_chg_abs / prev_close) * 100
            else:
                price_change_pct = 0.0

            sign = "+" if price_change_pct >= 0 else ""
            emoji = "🟢" if price_change_pct >= 0 else "🔴"
            report_lines.append(
                f"• *{underlying}*\n"
                f"  LTP: ₹{ltp:,.2f} | Change: {sign}{price_change_pct:.2f}% {emoji}"
            )

        if report_lines:
            return (
                f"📊 *[Expiry Engine] End-of-Day Gamma Burst Report*\n"
                f"Session: {today_str}\n"
                f"────────────────────────\n"
                f"The following assets generated explosive Gamma Burst alerts during today's session:\n\n"
                + "\n\n".join(report_lines) +
                f"\n────────────────────────\n"
                f"Total Alerted Assets: {len(report_lines)}\n"
                f"Session closed. Premium tracking disabled."
            )
    except Exception as e:
        logging.error(f"[Expiry Engine] Failed to compile EOD quotes: {e}")

    # Fail-safe simple list fallback
    simple_lines = [f"• *{u}*" for u in alerted_underlyings]
    return (
        f"📊 *[Expiry Engine] End-of-Day Gamma Burst Report*\n"
        f"Session: {today_str}\n"
        f"────────────────────────\n"
        f"The following assets generated explosive Gamma Burst alerts today:\n\n"
        + "\n".join(simple_lines) +
        f"\n────────────────────────\n"
        f"⚠️ Detailed spot metrics unavailable due to Kite/network timeout.\n"
        f"Total Alerted Assets: {len(alerted_underlyings)}\n"
        f"Session closed. Premium tracking disabled."
    )


def _expiry_loop():
    logging.info("[Expiry Engine] Background thread started. Standing by for Tuesday/Thursday 13:25 PM IST.")
    
    # Store history: token -> deque of (timestamp, price) maxlen=24 (120 secs @ 5s interval)
    price_history = {}
    cooldowns = {}
    active_tokens = {}
    alerted_underlyings = set()  # Set to track unique underlyings during the session
    eod_report_sent = False  # Tracks if EOD report has been dispatched today
    no_expiry_today = False  # Prevents repeated instruments API calls on non-expiry days
    last_reset_date = now_ist().date()
    
    while True:
        try:
            now = now_ist()
            
            # Reset cooldowns and tokens daily at midnight (stateful daily date-gate)
            today_date = now.date()
            if last_reset_date != today_date:
                cooldowns.clear()
                active_tokens.clear()
                _token_to_underlying.clear()
                alerted_underlyings.clear()
                eod_report_sent = False
                price_history.clear()  # Gap 1 fix: prevents stale data polluting next expiry's velocity calc
                no_expiry_today = False  # Gap 3 fix: allow fresh discovery for the new day
                last_reset_date = today_date
                logging.info(f"[Expiry Engine] Daily midnight state reset completed successfully for {today_date}.")
                
            # Weekday Filter: Only run on Tuesday (1) and Thursday (3)
            if now.weekday() not in (1, 3):
                time.sleep(60)
                continue
                
            # Self-Healing Late Boot & 1:25 PM Initializer
            if _is_active_window(now) and not active_tokens and not no_expiry_today:
                kite = _get_kite()
                if kite:
                    logging.info(f"[Expiry Engine] {now.strftime('%H:%M')} IST. Identifying today's expirations...")
                    discovered = _discover_expiry_tokens(kite)
                    if discovered is None:
                        logging.warning("[Expiry Engine] Expiry discovery failed due to network/DB error. Will retry.")
                    elif discovered:
                        active_tokens = discovered
                        _send_telegram(f"🔔 [Expiry Engine] Initialized.\nTracking {len(active_tokens)} ATM/OTM strikes for today's expiry.\nScanning for Gamma bursts...")
                    else:
                        logging.info("[Expiry Engine] No expirations found today.")
                        no_expiry_today = True  # Verified non-expiry day: stop retrying
                    time.sleep(60) # Pause so it doesn't spam initialization if it fails
            
            # The Trigger Window (1:30 PM - 3:15 PM)
            elif _is_trigger_window(now) and active_tokens:
                kite = _get_kite()
                if not kite:
                    logging.warning("[Expiry Engine] Kite session unavailable in trigger window. Retrying in 5s.")
                    time.sleep(5)
                    continue
                    
                # 1. Fetch live quotes safely in batches
                tokens = list(active_tokens.values())
                quotes = {}
                try:
                    for i in range(0, len(tokens), 500):
                        quotes.update(kite.quote(tokens[i:i+500]))
                except Exception as e:
                    logging.error(f"[Expiry Engine] Quote fetch error: {e}")
                    
                # 2. Check velocity
                for exch_sym, data in quotes.items():
                    # Gap 1 Fix: Immediately normalize token to integer
                    token = int(data.get('instrument_token'))
                    if not token or 'last_price' not in data:
                        continue
                        
                    ltp = data['last_price']
                    oi = data.get('oi', 0)
                    
                    # Gap 4 Fix: Loosen entry guard to ltp < 2 to catch early stage Gamma breakouts
                    if ltp < 2:
                        continue 
                        
                    # Extract clean tradingsymbol by discarding exchange prefixes (dropped str() lookup fallback)
                    raw_sym = active_tokens.get(token) or exch_sym
                    sym = raw_sym.replace("NFO:", "").replace("BFO:", "")
                    
                    if token not in price_history:
                        price_history[token] = deque(maxlen=24)
                        
                    history = price_history[token]
                    history.append((time.time(), ltp, oi))
                    
                    # Gap 2 Fix: Instantly loop-purge all stale baseline ticks in a single cycle
                    now_ts = time.time()
                    while history and now_ts - history[0][0] > 135:
                        history.popleft()
                        
                    # Gap 3 Fix: Constant 60-second velocity lookback offset
                    LOOKBACK = 12
                    if len(history) >= LOOKBACK:
                        old_ts, old_price, old_oi = history[-LOOKBACK]
                        
                        if old_price > 0:
                            # Gap 2: Rupee gain floor to filter out penny fluctuation noise (e.g. ₹2 -> ₹4)
                            rupee_gain = ltp - old_price
                            if rupee_gain < 5:
                                continue
                                
                            spike_pct = ((ltp - old_price) / old_price) * 100
                            oi_change_pct = ((oi - old_oi) / old_oi) * 100 if old_oi > 0 else 0
                            
                            if spike_pct >= 10:
                                stage = 0
                                category = ""
                                if spike_pct >= 40:
                                    stage = 3
                                    category = "🚀 EXTREME RISE"
                                elif spike_pct >= 20:
                                    stage = 2
                                    category = "🔥 BLAZING"
                                elif spike_pct >= 10:
                                    stage = 1
                                    category = "📈 RISING"
                                    
                                last_alert = cooldowns.get(token, {'time': 0, 'stage': 0})
                                # Handle backward compatibility if cooldowns has a float (time) from an older run
                                if isinstance(last_alert, float):
                                    last_alert = {'time': last_alert, 'stage': 0}
                                    
                                time_since_last = time.time() - last_alert['time']
                                
                                # Trigger if 15 mins passed, OR if it's an UPGRADE to a higher stage
                                if time_since_last > 900 or stage > last_alert['stage']:
                                    cooldowns[token] = {'time': time.time(), 'stage': stage}
                                    
                                    oi_str = f"+{oi_change_pct:.1f}%" if oi_change_pct > 0 else f"{oi_change_pct:.1f}%"
                                    opt_type = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else "OPT"
                                    
                                    msg = (f"{category} | {sym}\n"
                                           f"Speed: Premium surged {spike_pct:.0f}% (₹{old_price:.2f} → ₹{ltp:.2f}) in <2 mins!\n"
                                           f"{opt_type} OI Change: {oi_str}")
                                    
                                    _send_telegram(msg)
                                    logging.info(f"[Expiry Engine] Burst Alert ({category}) fired for {sym}")
                                    
                                    # Track that an alert was fired for this underlying stock
                                    underlying = _token_to_underlying.get(token)
                                    if underlying:
                                        alerted_underlyings.add(underlying)
                                            
            # Shutdown at 3:16 PM (Stops option tracking immediately)
            if now.hour == 15 and now.minute >= 16 and active_tokens:
                _send_telegram("🛑 [Expiry Engine] Session closed. Premium tracking disabled until next expiry.")
                active_tokens.clear()
                price_history.clear()
                
            # EOD Report Dispatch at 15:35 PM
            if now.hour == 15 and now.minute >= 35 and not eod_report_sent:
                if alerted_underlyings:
                    # Generate and dispatch premium EOD report
                    report_msg = _generate_eod_report(kite, alerted_underlyings)
                    if report_msg:
                        _send_telegram(report_msg)
                    alerted_underlyings.clear()
                else:
                    _send_telegram("🛑 [Expiry Engine] Session closed. No Gamma burst alerts were generated today.")
                _token_to_underlying.clear()
                eod_report_sent = True
            
            # Smart Polling Delay
            if _is_trigger_window(now) and active_tokens:
                time.sleep(5)
            else:
                time.sleep(30)
                
        except Exception as e:
            logging.error(f"[Expiry Engine] Fatal error: {e}")
            time.sleep(10)

def start_expiry_engine():
    global _expiry_thread
    if _expiry_thread is None or not _expiry_thread.is_alive():
        logging.info("[Expiry Engine] Spawning/Restarting background Gamma Burst monitoring daemon thread...")
        _expiry_thread = threading.Thread(target=_expiry_loop, daemon=True)
        _expiry_thread.start()
