import threading
import time
import datetime
import logging
from collections import deque, Counter
from session_utils import now_ist

# Global pointer to the background thread
_cash_scanner_thread = None

def _get_kite():
    from server import get_kite
    return get_kite()

def _send_telegram(msg):
    pass

def _discover_active_cash_universe(kite):
    """
    Discovers liquid equities dynamically, including F&O names.
    Filters by price (>= 5) and real-time morning turnover (>= 10 Lakhs INR).
    """
    logging.info("[Cash Scanner] Initiating dynamic active universe discovery...")
    try:
        # 1. Fetch NSE equities
        from db_instruments import get_cached_instruments, get_fno_symbols
        nse_instruments = get_cached_instruments("NSE")
        eq_instruments = [
            i for i in nse_instruments 
            if i.get("instrument_type") == "EQ" and i.get("segment") == "NSE"
        ]
        
        # 2. Fetch F&O underlying names to exclude them
        fno_names = set(get_fno_symbols())
        
        # 3. Filter standard series (exclude oddlots, BE, trade-for-trade, etc.) and exclude F&O names
        candidates = []
        for i in eq_instruments:
            sym = i["tradingsymbol"].upper()
            if sym in fno_names:
                continue
            # Skip indexes & illiquid series
            if any(sym.endswith(suffix) for suffix in ["-BE", "-BZ", "-ST", "-TF", "-DE", "-SG"]):
                continue
            candidates.append((i, False))
            
        logging.info(f"[Cash Scanner] Found {len(candidates)} candidates. Batching morning turnover filters...")
        
        # 4. Batch query quotes to check price range & today's morning volume
        active_universe = {}
        nse_keys = [f"NSE:{i['tradingsymbol']}" for i, _ in candidates]
        
        # To avoid rate limits, query quotes in batches of 500
        quotes = {}
        for idx in range(0, len(nse_keys), 500):
            try:
                quotes.update(kite.quote(nse_keys[idx:idx+500]))
                time.sleep(0.1)
            except Exception as e:
                logging.error(f"[Cash Scanner] Batch quote fetch error: {e}")
                
        # 5. Filter for price >= 5 and morning turnover >= 10 Lakhs (1,000,000 INR)
        # Turnover >= 10 Lakhs in first few mins guarantees massive liquidity >= 2-3 Crores by EOD.
        for i, is_fno in candidates:
            sym = i["tradingsymbol"]
            token = i["instrument_token"]
            q_key = f"NSE:{sym}"
            
            if q_key not in quotes:
                continue
                
            q = quotes[q_key]
            ltp = q.get("last_price", 0)
            vol = q.get("volume", 0)
            turnover = vol * ltp
            
            # Apply price/turnover filters for cash-only symbols
            if ltp >= 5 and turnover >= 1_000_000:
                active_universe[token] = {
                    "symbol": sym,
                    "token": token,
                    "is_fno": False,  # Segment tag is always cash
                    "prev_close": q.get("ohlc", {}).get("close", ltp) or ltp,
                    "upper_circuit": q.get("upper_circuit_limit", ltp * 1.2) or (ltp * 1.2)
                }
                
        logging.info(f"[Cash Scanner] Active Universe configured. Tracking {len(active_universe)} highly active liquid stocks.")
        return active_universe
        
    except Exception as e:
        logging.error(f"[Cash Scanner] Failed to configure universe: {e}")
        return {}

def _is_active_window(now):
    """Returns True between 09:15 and 15:15 IST (Trading Session)"""
    if now.hour == 9 and now.minute >= 15: return True
    if 10 <= now.hour <= 14: return True
    if now.hour == 15 and now.minute <= 15: return True
    return False

def _generate_cash_eod_report(kite, alerted_stocks):
    """Generates a premium, formatted Telegram EOD report for Cash Momentum alerts."""
    if not alerted_stocks:
        return None

    today_str = now_ist().strftime('%Y-%m-%d')
    try:
        queries = [f"NSE:{sym}" for sym in alerted_stocks]
        
        # Batch fetch spot quotes for high efficiency
        quotes = {}
        for i in range(0, len(queries), 500):
            quotes.update(kite.quote(queries[i:i+500]))

        report_lines = []
        for sym, is_fno in alerted_stocks.items():
            q_key = f"NSE:{sym}"
            if q_key not in quotes:
                continue
                
            q = quotes[q_key]
            ltp = q.get("last_price", 0)
            net_chg_abs = q.get("net_change", 0) or 0
            prev_close = q.get("ohlc", {}).get("close", 0) or 0

            if prev_close > 0:
                price_change_pct = (net_chg_abs / prev_close) * 100
            else:
                price_change_pct = 0.0

            sign = "+" if price_change_pct >= 0 else ""
            emoji = "🟢" if price_change_pct >= 0 else "🔴"
            segment_label = "FNO" if is_fno else "CASH"
            
            report_lines.append(
                f"• *{sym}* ({segment_label})\n"
                f"  LTP: ₹{ltp:,.2f} | Change: {sign}{price_change_pct:.2f}% {emoji}"
            )

        if report_lines:
            return (
                f"📊 *[Cash Scanner] End-of-Day Momentum Report*\n"
                f"Session: {today_str}\n"
                f"────────────────────────\n"
                f"The following assets generated Cash/F&O Momentum velocity alerts today:\n\n"
                + "\n\n".join(report_lines) +
                f"\n────────────────────────\n"
                f"Total Alerted Assets: {len(report_lines)}\n"
                f"Session closed. Momentum tracking disabled."
            )
    except Exception as e:
        logging.error(f"[Cash Scanner] Failed to compile EOD quotes: {e}")

    # Fail-safe simple list fallback
    simple_lines = []
    for sym, is_fno in alerted_stocks.items():
        segment_label = "FNO" if is_fno else "CASH"
        simple_lines.append(f"• *{sym}* ({segment_label})")
        
    return (
        f"📊 *[Cash Scanner] End-of-Day Momentum Report*\n"
        f"Session: {today_str}\n"
        f"────────────────────────\n"
        f"The following assets generated Cash/F&O Momentum velocity alerts today:\n\n"
        + "\n".join(simple_lines) +
        f"\n────────────────────────\n"
        f"⚠️ Detailed spot metrics unavailable due to Kite/network timeout.\n"
        f"Total Alerted Assets: {len(alerted_stocks)}\n"
        f"Session closed. Momentum tracking disabled."
    )


def _cash_scanner_loop():
    logging.info("[Cash Scanner] Background monitoring thread started. Standing by for 09:15 AM IST.")
    
    # Store price/volume history: token -> deque of (timestamp, ltp, volume) maxlen=20 (5 mins @ 15s interval)
    history_cache = {}
    cooldowns = {}
    active_universe = {}
    alerted_stocks = {}  # Maps sym -> is_fno
    eod_report_sent = False  # Tracks if EOD report has been sent today
    universe_discovered_today = False
    last_reset_date = now_ist().date()
    last_universe_refresh = 0
    
    while True:
        try:
            now = now_ist()
            
            # Reset states daily at midnight (stateful daily date-gate)
            today_date = now.date()
            if last_reset_date != today_date:
                cooldowns.clear()
                active_universe.clear()
                history_cache.clear()
                alerted_stocks.clear()
                eod_report_sent = False
                universe_discovered_today = False
                last_universe_refresh = 0
                last_reset_date = today_date
                logging.info(f"[Cash Scanner] Daily state reset completed successfully for {today_date}.")
                
            # Initialize Cash Universe at market open (09:15 AM)
            if _is_active_window(now) and not active_universe and not universe_discovered_today:
                kite = _get_kite()
                if kite:
                    active_universe = _discover_active_cash_universe(kite)
                    if active_universe:
                        _send_telegram(f"⚡ [Cash Scanner] Initialized.\nTracking {len(active_universe)} high-activity cash stocks for today.\nScanning for rapid velocity bursts...")
                        universe_discovered_today = True
                        last_universe_refresh = time.time()
                    else:
                        logging.info("[Cash Scanner] No liquid cash stocks matching criteria found yet. Retrying...")
                        time.sleep(30)
                        
            # The Scanner Active Execution Window (09:15 AM - 03:15 PM)
            elif _is_active_window(now) and active_universe:
                kite = _get_kite()
                if not kite:
                    logging.warning("[Cash Scanner] Kite session unavailable. Retrying in 5s.")
                    time.sleep(5)
                    continue
                    
                # Periodic Re-Discovery: Every 5 minutes (300 seconds)
                current_time = time.time()
                if current_time - last_universe_refresh >= 300:
                    logging.info("[Cash Scanner] Performing periodic universe refresh (5-min interval) to catch afternoon runners...")
                    new_universe = _discover_active_cash_universe(kite)
                    if new_universe:
                        added_count = 0
                        for token, info in new_universe.items():
                            if token not in active_universe:
                                active_universe[token] = info
                                added_count += 1
                        if added_count > 0:
                            logging.info(f"[Cash Scanner] Universe refreshed. Added {added_count} newly active stocks to tracking!")
                    last_universe_refresh = current_time
                tokens_list = [f"NSE:{v['symbol']}" for v in active_universe.values()]
                quotes = {}
                
                # Fetch live quotes in batches of 500
                for i in range(0, len(tokens_list), 500):
                    try:
                        quotes.update(kite.quote(tokens_list[i:i+500]))
                    except Exception as e:
                        logging.error(f"[Cash Scanner] Live quote query failed: {e}")
                        
                # Process each quote snapshot
                for exch_sym, data in quotes.items():
                    raw_token = data.get("instrument_token")
                    if not raw_token:
                        continue
                    token = int(raw_token)
                    if token not in active_universe:
                        continue
                        
                    u_info = active_universe[token]
                    sym = u_info["symbol"]
                    ltp = data.get("last_price", 0)
                    vol = data.get("volume", 0)
                    ohlc = data.get("ohlc", {})
                    
                    # Circuit locking check (Avoid entries if locked or near live upper circuit limit)
                    upper_circuit = data.get("upper_circuit_limit", ltp * 1.2) or (ltp * 1.2)
                    if ltp >= upper_circuit * 0.992:  # within 0.8% of live upper circuit
                        continue
                        
                    # Ignore highly illiquid / flat penny activity
                    if ltp < 10:
                        continue
                        
                    if token not in history_cache:
                        history_cache[token] = deque(maxlen=40) # 10 minutes of cache @ 15s interval
                        
                    history = history_cache[token]
                    history.append((time.time(), ltp, vol))
                    
                    # Velocity & Volume derivation over 10-minute cache (at least 40 ticks)
                    if len(history) >= 40:
                        # Stale Data Check: Loop-purge all ticks older than 11 minutes (660 seconds) in a single cycle
                        now_ts = time.time()
                        while history and now_ts - history[0][0] > 660:
                            history.popleft()
                        if len(history) < 40:
                            continue
                            
                        # Extract midpoint (5 minutes ago) and oldest ticks of the pristine, cleaned history
                        mid_tick = history[20]
                        oldest_tick = history[0]
                        
                        # Sanity Check: Verify midpoint tick (5 minutes ago) is not stale due to network drops
                        if now_ts - mid_tick[0] > 330:
                            continue
                            
                        mid_ltp = mid_tick[1]
                        mid_vol = mid_tick[2]
                        oldest_vol = oldest_tick[2]
                        
                        if mid_ltp > 0:
                            # 1. Calculate Price Velocity (ROC over the last 5 minutes)
                            price_roc = ((ltp - mid_ltp) / mid_ltp) * 100
                            
                            # 2. Calculate Volume Velocity (Compare current 5-min volume vs preceding 5-min volume)
                            current_vol_delta = vol - mid_vol
                            preceding_vol_delta = mid_vol - oldest_vol
                            
                            # Dynamic Rupees-Value Floor (₹1 Lakh) to prevent division-by-zero & penny volume noise
                            vol_floor = max(preceding_vol_delta, 100000.0 / max(1.0, ltp))
                            rvol = current_vol_delta / vol_floor
                            
                            # Check price velocity trigger (Price rise >= 0.5% within last 5 minutes)
                            if price_roc >= 0.5:
                                # 3. Calculate Conviction Score (1 to 10 scale)
                                score = 0
                                
                                # Volume multiplier points
                                if rvol >= 10: score += 4
                                elif rvol >= 5: score += 2
                                
                                # Breakout level check
                                high_of_day = ohlc.get("high", ltp)
                                prev_close = u_info["prev_close"]
                                
                                if ltp >= high_of_day * 0.998:  # Breaking High of Day
                                    score += 2
                                if ltp >= prev_close * 1.02:    # Significant daily gain
                                    score += 1
                                    
                                # VWAP support check
                                vwap = data.get("average_price", 0)
                                if vwap > 0 and ltp > vwap:
                                    score += 1
                                    
                                # Coil release check (tight range before breakout)
                                pre_breakout_prices = [t[1] for t in list(history)[:20]]
                                range_pct = ((max(pre_breakout_prices) - min(pre_breakout_prices)) / min(pre_breakout_prices)) * 100
                                if range_pct <= 0.4:
                                    score += 2
                                    
                                # We trigger alerts only if Score >= 5
                                if score >= 5:
                                    stage = 1
                                    category = "⚡ SPARK (Early Spike)"
                                    if score >= 9:
                                        stage = 3
                                        category = "🚀 SKYROCKET (Extreme Burst)"
                                    elif score >= 7:
                                        stage = 2
                                        category = "🔥 INTRADAY BURST (Strong Build)"
                                        
                                    last_alert = cooldowns.get(token, {"time": 0, "stage": 0, "stage3_price": 0})
                                    time_since_last = time.time() - last_alert["time"]
                                    
                                    # Stage 3 Continuation Alert: bypass 15-min cooldown if price has advanced by >= 1.5% since last Stage 3
                                    is_continuation = False
                                    if stage == 3 and last_alert.get("stage") == 3:
                                        last_stage3_price = last_alert.get("stage3_price", 0)
                                        if last_stage3_price > 0 and ((ltp - last_stage3_price) / last_stage3_price) * 100 >= 1.5:
                                            is_continuation = True
                                            
                                    # Trigger if 15 minutes passed, OR if alert upgrades, OR is a confirmed Stage 3 continuation
                                    if time_since_last > 900 or stage > last_alert["stage"] or is_continuation:
                                        cooldowns[token] = {
                                            "time": time.time(), 
                                            "stage": stage,
                                            "stage3_price": ltp if stage == 3 else last_alert.get("stage3_price", 0)
                                        }
                                        
                                        if is_continuation:
                                            category = "🚀 SKYROCKET (Continuation Spike)"
                                            
                                        daily_gain = ((ltp - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
                                        turnover_cr = (vol * ltp) / 10_000_000
                                        
                                        alert_tag = f"FNO: {sym}" if u_info.get("is_fno") else f"CASH: {sym}"
                                        msg = (
                                            f"{category} | {alert_tag}\n"
                                            f"Speed: surged {price_roc:.2f}% (₹{mid_tick[1]:.2f} → ₹{ltp:.2f}) in <5 mins!\n"
                                            f"Turnover: ₹{turnover_cr:.2f}Cr | RVol: {rvol:.1f}x\n"
                                            f"Daily Change: {daily_gain:+.2f}% | Conviction: {score}/10"
                                        )
                                        _send_telegram(msg)
                                        logging.info(f"[Cash Scanner] {category} alert fired for {sym} (Score: {score})")
                                        
                                        # Track for EOD stats report
                                        alerted_stocks[sym] = u_info.get("is_fno", False)
                                            
            # Active Window shutdown at 03:16 PM (Stops active polling)
            if now.hour == 15 and now.minute >= 16 and active_universe:
                _send_telegram("🛑 [Cash Scanner] Session closed. Intraday momentum tracking suspended until tomorrow.")
                active_universe.clear()
                history_cache.clear()
                
            # EOD Report Dispatch at 15:35 PM
            if now.hour == 15 and now.minute >= 35 and not eod_report_sent:
                if alerted_stocks:
                    report_msg = _generate_cash_eod_report(kite, alerted_stocks)
                    if report_msg:
                        _send_telegram(report_msg)
                    alerted_stocks.clear()
                else:
                    _send_telegram("🛑 [Cash Scanner] Session closed. No momentum alerts were generated today.")
                eod_report_sent = True
                    
            # Smart delay polling
            if _is_active_window(now) and active_universe:
                time.sleep(15)  # 15s interval checks
            else:
                time.sleep(30)  # low-activity sleep
                
        except Exception as e:
            logging.error(f"[Cash Scanner] Fatal exception in scanning loop: {e}")
            time.sleep(15)

def start_cash_scanner():
    global _cash_scanner_thread
    # Upgraded thread checking with stateful self-healing is_alive restart logic
    if _cash_scanner_thread is None or not _cash_scanner_thread.is_alive():
        logging.info("[Cash Scanner] Spawning background Cash Momentum monitoring daemon thread...")
        _cash_scanner_thread = threading.Thread(target=_cash_scanner_loop, daemon=True)
        _cash_scanner_thread.start()
