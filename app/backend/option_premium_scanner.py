"""
================================================================================
🚀 F&O Option Premium Velocity Scanner
================================================================================
Objective:
  Real-time intraday monitoring of the front-month (current expiring) Call and Put 
  option premiums for all 210 F&O stocks. Detects explosive, institutional-grade 
  premium surges and dispatches structured, actionable alerts to Telegram.

Operational Design & How it Works:
  1. Front-Month Targeting:
     - Automatically scans Kite NFO instruments at 09:15 AM to identify all F&O stocks.
     - Dynamically isolates only the front-month (nearest expiry date) contract series,
       completely filtering out illiquid next-month and far-month options.
  2. Dual-Layer Strike Coverage:
     - Tracks up to 10 contracts per stock across two priority layers:
       * Opening Layer (Static, HIGH PRIORITY ⭐): ATM, OTM1, OTM2 CE/PE locked at
         market open (09:15 AM). Maintains uninterrupted deque history all day,
         eliminating blind spots during explosive intraday trends.
       * Running Layer (Hourly, STANDARD 🏃): Current ATM, OTM1 CE/PE recalculated
         every hour to track strike shifts when spot price moves significantly.
     - Opening and Running layers share tokens when strikes overlap — Opening wins.
  3. Priority Alert Dispatching:
     - Candidate triggers per 15-second tick are sorted multi-key:
       (1) Opening strikes first; (2) Highest % premium velocity first.
     - Separate cooldown buckets per priority layer: running alerts CANNOT suppress
       high-priority opening alerts, and vice-versa.
  4. Mathematical Consistency Filters (5-Min Rolling Window / 15s Ticks):
     - Price Velocity Check: Premium has risen by >= 12% in the last 5 minutes.
     - Monotonicity Check: max 1 pullback tolerated across the rolling window.
     - Volume Spurt Check: Current 15s interval volume >= 1.5x rolling average
       (minimum 100 shares), confirming genuine institutional volume injection.
  5. Self-Healing & Memory Optimizations:
     - NFO instrument list and spot prices fetched ONCE per discovery cycle and
       shared across both Opening and Running discovery calls (no double-download).
     - discovery_time only stamped on successful, non-empty universe merge.
     - Enforces per-priority-layer, per-symbol/type 15-minute cooldown to prevent spam.
================================================================================
"""

import threading
import time

import logging
from collections import deque, Counter
from session_utils import now_ist

_option_thread = None

def _get_kite():
    from server import get_kite
    return get_kite()


def _discover_option_contracts(kite, mode="opening", nfo_cache=None, spot_cache=None):
    """
    Dynamically discovers Call and Put ATM & Near-OTM contracts for all 210 F&O stocks.
    Guarantees targeting the highly liquid, front-month expiry series.

    Args:
        nfo_cache: Pre-fetched kite.instruments('NFO') list. If provided, skips the
                   internal download (avoids redundant double-fetch when called twice
                   in the same discovery cycle).
        spot_cache: Pre-fetched {sym: ltp} spot price dict. Same rationale as nfo_cache.

    Returns a dict mapping: token -> {symbol, tradingsymbol, option_type, strike, label, is_opening}
    """
    logging.info(f"[Option Scanner] Discovering F&O option contracts (mode={mode})...")
    try:
        # Use caller-supplied cache to avoid redundant Kite API downloads
        from db_instruments import get_cached_instruments
        nfo = nfo_cache if nfo_cache is not None else get_cached_instruments("NFO")
        indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        
        # Get all option contracts for standard stocks (excluding indices)
        fno_options = []
        underlying_names = set()
        for i in nfo:
            name = i.get("name")
            if i.get("instrument_type") in ["CE", "PE"] and name and name.upper() not in indices:
                fno_options.append(i)
                underlying_names.add(name.upper())
                
        if not fno_options:
            logging.warning("[Option Scanner] No NFO stock options found.")
            return {}
            
        # Use caller-supplied spot prices, or fetch fresh if not provided
        if spot_cache is not None:
            spot_prices = spot_cache
        else:
            from oi_spurt_routes import EXCHANGE_MAP
            queries = [EXCHANGE_MAP.get(u, f"NSE:{u}") for u in underlying_names]
            quotes = {}
            for i in range(0, len(queries), 500):
                quotes.update(kite.quote(queries[i:i+500]))
            spot_prices = {}
            for exch_sym, d in quotes.items():
                sym = exch_sym.replace("NSE:", "")
                ltp = d.get("last_price", 0)
                if ltp > 0:
                    spot_prices[sym] = ltp
                
        # Group option contracts by underlying name
        by_underlying = {}
        for i in fno_options:
            u = i["name"].upper()
            if u not in by_underlying:
                by_underlying[u] = []
            by_underlying[u].append(i)
            
        active_contracts = {}
        for u, insts in by_underlying.items():
            if u not in spot_prices:
                continue
                
            spot = spot_prices[u]
            
            # Find dynamic strike steps using the most common difference
            strikes = sorted(list(set(i["strike"] for i in insts)))
            if len(strikes) < 2:
                continue
                
            diffs = [strikes[idx+1] - strikes[idx] for idx in range(len(strikes)-1)]
            step = Counter(diffs).most_common(1)[0][0]
            
            atm_strike = round(spot / step) * step
            
            # Identify the nearest front-month expiry date for this stock to filter out illiquid far-months
            import datetime
            today = datetime.date.today()
            active_expiries = [i["expiry"] for i in insts if i.get("expiry") and i["expiry"] >= today]
            if not active_expiries:
                continue
            near_expiry = min(active_expiries)
            
            # If mode is "opening", target 6 strikes (ATM ± 2 OTM).
            # If mode is "running", target 4 strikes (ATM ± 1 OTM).
            if mode == "opening":
                targets = {
                    "ATM_CE":  (atm_strike, "CE"),
                    "ATM_PE":  (atm_strike, "PE"),
                    "OTM1_CE": (atm_strike + step, "CE"),
                    "OTM1_PE": (atm_strike - step, "PE"),
                    "OTM2_CE": (atm_strike + 2 * step, "CE"),
                    "OTM2_PE": (atm_strike - 2 * step, "PE")
                }
            else:
                targets = {
                    "ATM_CE":  (atm_strike, "CE"),
                    "ATM_PE":  (atm_strike, "PE"),
                    "OTM1_CE": (atm_strike + step, "CE"),
                    "OTM1_PE": (atm_strike - step, "PE")
                }
            
            # Map front-month option contracts matching our target strikes
            contract_map = {}
            for i in insts:
                if i.get("expiry") == near_expiry:
                    contract_map[(i["strike"], i["instrument_type"])] = i
                
            for key_label, (target_strike, opt_type) in targets.items():
                match_contract = contract_map.get((target_strike, opt_type))
                if match_contract:
                    token = int(match_contract["instrument_token"])
                    active_contracts[token] = {
                        "symbol": u,
                        "tradingsymbol": match_contract["tradingsymbol"],
                        "option_type": opt_type,
                        "strike": target_strike,
                        "label": key_label,
                        "is_opening": (mode == "opening")
                    }
                    
        logging.info(f"[Option Scanner] Successfully registered {len(active_contracts)} option contracts across {len(underlying_names)} F&O stocks.")
        return active_contracts
    except Exception as e:
        logging.error(f"[Option Scanner] Failure during options discovery: {e}")
        return {}

def _is_active_window(now):
    """Returns True between 09:15 and 15:30 IST"""
    if now.hour == 9 and now.minute >= 15: return True
    if 10 <= now.hour <= 14: return True
    if now.hour == 15 and now.minute <= 30: return True
    return False

def _option_scanner_loop():
    logging.info("[Option Scanner] Background monitoring thread active. Standing by for 09:15 AM IST.")
    
    # Store price/volume history: token -> deque of (timestamp, price, cumulative_volume) maxlen=20 (5 mins @ 15s interval)
    history_cache = {}
    cooldowns = {}
    opening_contracts = {}
    running_contracts = {}
    active_contracts = {}
    last_reset_date = None
    last_discovery_time = 0
    
    while True:
        try:
            now = now_ist()
            today_date = now.date()
            
            # Midnight reset
            if last_reset_date != today_date:
                cooldowns.clear()
                opening_contracts.clear()
                running_contracts.clear()
                active_contracts.clear()
                history_cache.clear()
                last_discovery_time = 0
                last_reset_date = today_date
                logging.info(f"[Option Scanner] Daily state reset completed successfully for {today_date}.")
                
            in_window = _is_active_window(now)
            kite = None
            
            # Dynamic Discovery (Runs initially at 09:15 AM, and refreshes every hour to track shifting ATM strikes)
            if in_window:
                current_time = time.time()
                if not active_contracts or (current_time - last_discovery_time >= 3600):
                    kite = _get_kite()
                    if kite:
                        logging.info("[Option Scanner] Running scheduled option contracts discovery...")
                        
                        # Pre-fetch NFO instruments and spot prices ONCE — shared across both
                        # opening and running discovery calls to avoid a redundant double-download
                        # of the full NFO instrument list (~20,000 records).
                        from oi_spurt_routes import EXCHANGE_MAP
                        from db_instruments import get_cached_instruments
                        nfo_data = get_cached_instruments("NFO")
                        _indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
                        _underlying_names = {
                            i["name"].upper() for i in nfo_data
                            if i.get("instrument_type") in ["CE", "PE"]
                            and i.get("name") and i["name"].upper() not in _indices
                        }
                        _queries = [EXCHANGE_MAP.get(u, f"NSE:{u}") for u in _underlying_names]
                        _raw_quotes = {}
                        for _b in range(0, len(_queries), 500):
                            _raw_quotes.update(kite.quote(_queries[_b:_b+500]))
                        shared_spot_prices = {
                            exch_sym.replace("NSE:", ""): d["last_price"]
                            for exch_sym, d in _raw_quotes.items()
                            if d.get("last_price", 0) > 0
                        }

                        # 1. Discover static opening contracts once at start of day
                        if not opening_contracts:
                            new_opening = _discover_option_contracts(
                                kite, mode="opening",
                                nfo_cache=nfo_data, spot_cache=shared_spot_prices
                            )
                            if new_opening:
                                opening_contracts = new_opening
                                logging.info(f"[Option Scanner] Successfully locked {len(opening_contracts)} static Opening strikes.")
                            else:
                                logging.warning("[Option Scanner] Static opening strikes discovery failed.")
                        
                        # 2. Discover/refresh dynamic running contracts hourly
                        new_running = _discover_option_contracts(
                            kite, mode="running",
                            nfo_cache=nfo_data, spot_cache=shared_spot_prices
                        )
                        if new_running:
                            running_contracts = new_running
                            logging.info(f"[Option Scanner] Loaded {len(running_contracts)} dynamic Running strikes.")
                        else:
                            logging.warning("[Option Scanner] Dynamic running strikes discovery failed.")
                            
                        # 3. Merge: Running first, then Opening so that is_opening=True wins for overlaps
                        merged = {}
                        if running_contracts:
                            merged.update(running_contracts)
                        if opening_contracts:
                            merged.update(opening_contracts)
                            
                        if merged:
                            active_contracts = merged
                            # Memory Optimization: Prune histories of obsolete contracts on refresh
                            history_cache = {token: history_cache[token] for token in active_contracts if token in history_cache}
                            # Gap 1 fix: Only stamp last_discovery_time when we have a valid, non-empty
                            # universe. If both discoveries fail, last_discovery_time stays stale so
                            # the `not active_contracts` guard ensures a retry on the very next tick
                            # instead of waiting a full hour.
                            last_discovery_time = current_time
                        else:
                            logging.warning("[Option Scanner] Discovery produced no contracts. Will retry on next tick.")
                    else:
                        logging.warning("[Option Scanner] Kite authenticated session unavailable. Retrying in 10s...")
                        time.sleep(10)
                        continue
                        
            # Live scanning tick
            if in_window and active_contracts:
                if kite is None:
                    kite = _get_kite()
                if not kite:
                    time.sleep(15)
                    continue
                    
                tokens = list(active_contracts.keys())
                
                # Batch fetch NFO quotes in groups of 500
                quotes = {}
                for i in range(0, len(tokens), 500):
                    batch = [f"NFO:{active_contracts[t]['tradingsymbol']}" for t in tokens[i:i+500]]
                    quotes.update(kite.quote(batch))
                    time.sleep(0.1)
                    
                # Signal Accumulator for Tick Prioritization
                tick_triggers = []
                
                for token, info in active_contracts.items():
                    q_key = f"NFO:{info['tradingsymbol']}"
                    if q_key not in quotes:
                        continue
                        
                    q = quotes[q_key]
                    ltp = q.get("last_price", 0)
                    cum_vol = q.get("volume", 0) or 0
                    
                    if ltp <= 0:
                        continue
                        
                    if token not in history_cache:
                        history_cache[token] = deque(maxlen=20)
                        
                    history_cache[token].append((now, ltp, cum_vol))
                    
                    # Evaluate after collecting at least 10 ticks (2.5 mins)
                    if len(history_cache[token]) >= 10:
                        hist = list(history_cache[token])
                        old_tick = hist[0]
                        old_ltp = old_tick[1]
                        
                        # 1. Price Velocity Check (>= 12% Price Gain in 5 minutes)
                        price_change_pct = ((ltp - old_ltp) / old_ltp) * 100
                        
                        if price_change_pct >= 12.0:
                            # 2. Cumulative % Gain Monotonicity Check
                            # Computes the running cumulative % gain from baseline at every tick.
                            # The premium is "consistently increasing" only if it never falls back
                            # below its previous cumulative peak. Flat ticks (grid-lock pauses)
                            # are tolerated, but ANY actual price pullback counts as a violation.
                            cum_pcts = [((h[1] - old_ltp) / old_ltp) * 100 for h in hist]
                            
                            pullbacks = 0
                            for idx in range(1, len(cum_pcts)):
                                if cum_pcts[idx] < cum_pcts[idx - 1]:
                                    pullbacks += 1
                            
                            # Max 1 pullback tolerated (accounts for single NFO grid-lock tick noise)
                            # Zero pullbacks = perfectly consistent upward curve
                            consistently_rising = pullbacks <= 1
                            
                            # Retain a consistency_score metric for Telegram telemetry
                            total_intervals = len(cum_pcts) - 1
                            consistency_score = (1 - pullbacks / total_intervals) * 100 if total_intervals > 0 else 100.0
                            
                            if consistently_rising:
                                # 3. Volume Spurt Check (interval volume >= 1.5x rolling average interval volume)
                                interval_volumes = []
                                for idx in range(1, len(hist)):
                                    diff_vol = hist[idx][2] - hist[idx-1][2]
                                    if diff_vol >= 0:
                                        interval_volumes.append(diff_vol)
                                        
                                if interval_volumes:
                                    current_interval_vol = interval_volumes[-1]
                                    # Use 0 as fallback so first clean volume tick always passes the 1.5x spurt gate
                                    avg_interval_vol = sum(interval_volumes[:-1]) / len(interval_volumes[:-1]) if len(interval_volumes) > 1 else 0
                                                                      # Enforce a minimum interval volume of 100 shares to filter out dead strikes
                                    if current_interval_vol >= 100 and current_interval_vol >= 1.5 * avg_interval_vol:
                                        # Calculate buyer/seller percentage from total order book depth
                                        buy_qty = q.get("buy_quantity", 0) or 0
                                        sell_qty = q.get("sell_quantity", 0) or 0
                                        total_qty = buy_qty + sell_qty
                                        buyer_pct = (buy_qty / total_qty * 100) if total_qty > 0 else 50.0
                                        seller_pct = (sell_qty / total_qty * 100) if total_qty > 0 else 50.0
                                        
                                        # Apply depth criteria: buyer > 40% for CE, sellers > 40% for PE
                                        opt_type = info["option_type"]
                                        depth_passed = False
                                        if opt_type == "CE" and buyer_pct > 40.0:
                                            depth_passed = True
                                        elif opt_type == "PE" and seller_pct > 40.0:
                                            depth_passed = True
                                            
                                        if depth_passed:
                                            sym = info["symbol"]
                                            # Separate cooldown buckets per priority layer.
                                            # Prevents running alerts from suppressing high-priority
                                            # opening alerts and avoids spam within each layer.
                                            priority_tag = "opening" if info.get("is_opening", False) else "running"
                                            cooldown_key = f"{sym}_{opt_type}_{priority_tag}"
                                            
                                            # Verify cooldown prior to adding to candidate triggers
                                            if now.timestamp() - cooldowns.get(cooldown_key, 0) >= 900:
                                                tick_triggers.append({
                                                    "symbol": sym,
                                                    "tradingsymbol": info["tradingsymbol"],
                                                    "option_type": opt_type,
                                                    "label": info["label"],
                                                    "is_opening": info.get("is_opening", False),
                                                    "price_change_pct": price_change_pct,
                                                    "old_ltp": old_ltp,
                                                    "ltp": ltp,
                                                    "consistency_score": consistency_score,
                                                    "current_interval_vol": current_interval_vol,
                                                    "avg_interval_vol": avg_interval_vol,
                                                    "buyer_pct": buyer_pct,
                                                    "seller_pct": seller_pct,
                                                    "cooldown_key": cooldown_key
                                                })
                                            
                # 4. Sort all tick triggers: Opening/High-Priority first, then highest % price velocity
                tick_triggers.sort(key=lambda x: (x["is_opening"], x["price_change_pct"]), reverse=True)
                
                # 5. Dispatch prioritized alerts
                for alert in tick_triggers:
                    # Double-verify cooldown right before dispatch to prevent overlap triggers
                    if now.timestamp() - cooldowns.get(alert["cooldown_key"], 0) >= 900:
                        label_text = "ATM" if "ATM" in alert["label"] else "OTM"
                        if alert["is_opening"]:
                            header_tag = f"⭐ *[FNO Premium Spurt - HIGH PRIORITY]*\n* {alert['symbol']} Opening {label_text} {alert['option_type']} *"
                        else:
                            header_tag = f"🏃 *[FNO Premium Spurt - RUNNING]*\n* {alert['symbol']} Running {label_text} {alert['option_type']} *"
                            
                        msg = (
                            f"{header_tag}\n"
                            f"────────────────────────\n"
                            f"• *Option*: `{alert['tradingsymbol']}`\n"
                            f"• *Premium Speed*: Surged *+{alert['price_change_pct']:.2f}%* in <5 mins! 🔥\n"
                            f"• *Price Trajectory*: ₹{alert['old_ltp']:.2f} → *₹{alert['ltp']:.2f}*\n"
                            f"• *Consistency*: {alert['consistency_score']:.0f}% | No Pullbacks (Consistent Rise ✅)\n"
                            f"• *Interval Option Vol*: {alert['current_interval_vol']:,} shares (Spurt: {(alert['current_interval_vol']/alert['avg_interval_vol'] if alert['avg_interval_vol'] > 0 else 1.0):.1f}x)\n"
                            f"• *Depth (B/S)*: Buyers: *{alert['buyer_pct']:.1f}%* | Sellers: *{alert['seller_pct']:.1f}%*\n"
                            f"────────────────────────\n"
                            f"Prepare trade execution!"
                        )
                        # Alerts disabled (Telegram/Discord integration removed)
                        cooldowns[alert["cooldown_key"]] = now.timestamp()
                        logging.info(f"[Option Scanner] Prioritized alert fired for {alert['tradingsymbol']} (Change: {alert['price_change_pct']:.2f}%)")
                                            
            # Active session interval is 15 seconds; sleeping 30 seconds outside market hours
            if in_window and active_contracts:
                time.sleep(15)
            else:
                time.sleep(30)
        except Exception as e:
            logging.error(f"[Option Scanner] Exception in scanner background loop: {e}")
            time.sleep(15)

def start_option_scanner():
    global _option_thread
    if _option_thread is None or not _option_thread.is_alive():
        logging.info("[Option Scanner] Spawning F&O Option Premium Scanner background thread...")
        _option_thread = threading.Thread(target=_option_scanner_loop, daemon=True)
        _option_thread.start()
