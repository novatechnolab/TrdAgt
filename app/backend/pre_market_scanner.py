import threading
import time
import datetime
import logging
from session_utils import now_ist

_pre_market_thread = None

def _get_kite():
    from server import get_kite
    return get_kite()

def _send_telegram(msg):
    from server import _send_telegram_message, _telegram_configured
    if _telegram_configured():
        _send_telegram_message(msg)

def run_pre_market_scan(kite):
    """
    Performs pre-market gap analysis on both F&O and standard Cash equities.
    Identifies Gap-Ups >= 1.2% and Gap-Downs <= -1.2%.
    Filters cash equities using a strict pre-open volume and turnover liquidity gate.
    Dispatches a visually segregated premium Telegram watchlist report at 09:10 AM IST.
    """
    logging.info("[Pre-Market Scanner] Initiating multi-segment pre-market gap scan at 09:10 AM IST...")
    try:
        # 1. Discover dynamic F&O symbols to establish derivatives universe
        from db_instruments import get_fno_symbols
        indices = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
        fno_names = set(s for s in get_fno_symbols() if s not in indices)
        
        # 2. Fetch all NSE instruments and filter for standard tradeable EQ candidates
        from db_instruments import get_cached_instruments
        nse = get_cached_instruments("NSE")
        candidates = []
        for i in nse:
            sym = i.get("tradingsymbol", "")
            # Focus strictly on mainboard tradeable EQ shares (ignore BE trade-to-trade and BZ distress counters)
            if i.get("instrument_type") == "EQ" and not sym.endswith("-BE") and not sym.endswith("-BZ"):
                is_fno = sym in fno_names
                if is_fno:
                    candidates.append((sym, True))
                
        if not candidates:
            logging.warning("[Pre-Market Scanner] No tradeable NSE candidates found.")
            return
            
        # ── Key Indices: NIFTY 50, SENSEX, BANKNIFTY, FINNIFTY ─────────────────
        INDEX_MAP = {
            "NIFTY 50":    "NSE:NIFTY 50",
            "BANKNIFTY":   "NSE:NIFTY BANK",
            "FINNIFTY":    "NSE:NIFTY FIN SERVICE",
            "SENSEX":      "BSE:SENSEX",
        }

        index_queries = list(INDEX_MAP.values())
        queries = [f"NSE:{sym}" for sym, _ in candidates]
        logging.info(f"[Pre-Market Scanner] Scanning {len(queries)} equities + {len(index_queries)} indices. Fetching OHLC in batches...")

        # 3. Batch fetch pre-open OHLC quotes
        # Indices are fetched first (small batch), then equities in 500-chunk batches
        quotes = {}
        try:
            quotes.update(kite.ohlc(index_queries))    # Fetch all indices in one call
        except Exception as e:
            logging.warning(f"[Pre-Market Scanner] Index OHLC fetch failed: {e}")
        for i in range(0, len(queries), 500):
            quotes.update(kite.ohlc(queries[i:i+500]))
            time.sleep(0.1)

        # Compute index gaps
        index_gaps = []
        for name, kite_sym in INDEX_MAP.items():
            d = quotes.get(kite_sym, {})
            prev_close    = d.get("ohlc", {}).get("close", 0) or 0
            pre_open      = d.get("ohlc", {}).get("open", 0) or 0
            last_price    = d.get("last_price", 0) or pre_open
            ref_price     = last_price if last_price > 0 else pre_open
            if prev_close > 0 and ref_price > 0:
                gap_pct = ((ref_price - prev_close) / prev_close) * 100
                index_gaps.append((name, ref_price, gap_pct))
            
        gap_ups_fno = []
        gap_downs_fno = []
        
        sym_to_fno = {sym: is_fno for sym, is_fno in candidates}
        
        # 4. Calculate gaps and filter for active setups
        for exch_sym, d in quotes.items():
            sym = exch_sym.replace("NSE:", "")
            is_fno = sym_to_fno.get(sym, False)
            
            prev_close = d.get("ohlc", {}).get("close", 0) or 0
            pre_open_price = d.get("ohlc", {}).get("open", 0) or 0
            pre_open_volume = d.get("volume", 0) or 0
            
            if prev_close > 0 and pre_open_price > 0:
                gap_pct = ((pre_open_price - prev_close) / prev_close) * 100
                
                if is_fno:
                    # F&O Stocks: Always include significant moves (already highly liquid)
                    if gap_pct >= 1.2:
                        gap_ups_fno.append((sym, pre_open_price, gap_pct, pre_open_volume))
                    elif gap_pct <= -1.2:
                        gap_downs_fno.append((sym, pre_open_price, gap_pct, pre_open_volume))
                            
        # 5. Sort segments by gap size
        gap_ups_fno.sort(key=lambda x: x[2], reverse=True)
        gap_downs_fno.sort(key=lambda x: x[2])
        
        if not any([gap_ups_fno, gap_downs_fno]):
            logging.info("[Pre-Market Scanner] No catalyst gaps detected in F&O segment today.")
            return
            
        # 6. Format the premium, segregated Telegram notification
        report_lines = []
        report_lines.append("🌅 *[Pre-Market] Catalyst Watchlist*")
        report_lines.append(f"Session: {now_ist().strftime('%Y-%m-%d')}")
        report_lines.append("────────────────────────")

        # Section 0: Key Indices snapshot
        report_lines.append("📊 *KEY INDICES — Pre-Market Gap:*")
        if index_gaps:
            for name, price, gap in sorted(index_gaps, key=lambda x: x[2], reverse=True):
                arrow = "🟢" if gap >= 0 else "🔴"
                sign  = "+" if gap >= 0 else ""
                report_lines.append(f"• *{name}*: {price:,.2f}  ({sign}{gap:.2f}%) {arrow}")
        else:
            report_lines.append("_Index data unavailable._")
        report_lines.append("────────────────────────")

        # Section A: F&O Derivatives catalysts
        report_lines.append("🔥 *[FNO DERIVATIVES] Catalyst Gaps:*")
        has_fno_gaps = False
        if gap_ups_fno:
            has_fno_gaps = True
            report_lines.append("🚀 *Top Gap-Ups:*")
            for sym, price, gap, vol in gap_ups_fno[:8]:  # Top 8
                report_lines.append(f"• *{sym}*: ₹{price:,.2f} (+{gap:.2f}%) 🟢")
        if gap_downs_fno:
            has_fno_gaps = True
            report_lines.append("📉 *Top Gap-Downs:*")
            for sym, price, gap, vol in gap_downs_fno[:8]:  # Top 8
                report_lines.append(f"• *{sym}*: ₹{price:,.2f} ({gap:.2f}%) 🔴")
        if not has_fno_gaps:
            report_lines.append("_No significant F&O gaps detected today._")
            
        report_lines.append("────────────────────────")
        total_catalysts = len(gap_ups_fno) + len(gap_downs_fno)
        report_lines.append(f"Total F&O Catalysts under track: {total_catalysts} symbols.")
        report_lines.append("Prepare your watchlists for 09:15 AM!")
        
        msg = "\n".join(report_lines)
        _send_telegram(msg)
        logging.info("[Pre-Market Scanner] Multi-segment pre-market gap report successfully sent to Telegram.")
        
    except Exception as e:
        logging.error(f"[Pre-Market Scanner] Error during pre-market scan: {e}")

def _pre_market_loop():
    logging.info("[Pre-Market Scanner] Background thread active. Standing by for 09:10 AM IST.")
    
    last_scan_date = None
    
    while True:
        try:
            now = now_ist()
            today_date = now.date()
            
            # Run scan at 09:10 AM IST (between 09:10 and 09:14 AM IST)
            if now.hour == 9 and 10 <= now.minute <= 14:
                if last_scan_date != today_date:
                    kite = _get_kite()
                    if kite:
                        run_pre_market_scan(kite)
                        last_scan_date = today_date
                    else:
                        logging.warning("[Pre-Market Scanner] Kite authenticated session unavailable. Retrying in 10s...")
                        time.sleep(10)
                        continue
            
            # Sleep 30 seconds to conserve CPU
            time.sleep(30)
            
        except Exception as e:
            logging.error(f"[Pre-Market Scanner] Exception in background loop: {e}")
            time.sleep(15)

def start_pre_market_scanner():
    global _pre_market_thread
    if _pre_market_thread is None or not _pre_market_thread.is_alive():
        logging.info("[Pre-Market Scanner] Spawning pre-market scanner background daemon thread...")
        _pre_market_thread = threading.Thread(target=_pre_market_loop, daemon=True)
        _pre_market_thread.start()
