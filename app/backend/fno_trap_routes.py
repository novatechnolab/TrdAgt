"""
FNO Trap Dashboard — Blueprint v2.0  (Phase 2 — Full backend integration)
==========================================================================
Routes: /fno-trap/*  consumed by app/fno-trap-dashboard.html
Uses existing TradeSignal Kite session (session_utils.get_kite()).
Engine: fno_trap package (db, kite_fetcher, signal_engine, trap_engine, output_reducer)
"""
import os
import threading
import time
import logging

from flask import Blueprint, jsonify, request, send_from_directory

log = logging.getLogger(__name__)

# ── Bootstrap DB on import ────────────────────────────────────────────────
try:
    from fno_trap.db import init_db
    init_db()
    _DB_OK = True
except Exception as _e:
    log.error("FNO Trap: DB init failed: %s", _e)
    _DB_OK = False

# ── Import engine ─────────────────────────────────────────────────────────
try:
    from fno_trap.trap_engine import run_cycle, get_cached_card, set_cached_card
    from fno_trap.db import get_connection
    from fno_trap.time_phase import now_ist, get_time_phase
    _ENGINE_OK = True
except Exception as _e:
    log.error("FNO Trap: engine import failed: %s", _e)
    _ENGINE_OK = False

# ── Blueprint ─────────────────────────────────────────────────────────────
fno_trap_bp = Blueprint('fno_trap', __name__, url_prefix='/fno-trap')

# ── Global tick history cache ─────────────────────────────────────────────
_fno_history = {}
_fno_lock = threading.Lock()

# ── Background cycle scheduler ────────────────────────────────────────────
_scheduler_started = False
_scheduler_lock    = threading.Lock()


def _bg_scheduler():
    """Refresh all watchlist symbols every 5 min during market hours."""
    while True:
        try:
            phase = get_time_phase() if _ENGINE_OK else "MARKET_CLOSED"
            if phase not in ("MARKET_CLOSED", "SETTLEMENT_EARLY") and _ENGINE_OK:
                syms = _get_watchlist_symbols()
                acct = _get_account_size()
                for sym in syms:
                    try:
                        run_cycle(sym, acct)
                    except Exception as e:
                        log.warning("FNO Trap BG cycle error for %s: %s", sym, e)
        except Exception as e:
            log.error("FNO Trap scheduler error: %s", e)
        time.sleep(300)  # 5-minute cycle


def _ensure_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if not _scheduler_started and _ENGINE_OK:
            t = threading.Thread(target=_bg_scheduler, daemon=True, name="fno-trap-scheduler")
            t.start()
            _scheduler_started = True
            log.info("FNO Trap background scheduler started")


def _get_watchlist_symbols():
    if not _DB_OK:
        return ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    try:
        conn = get_connection()
        rows = conn.execute("SELECT symbol FROM watchlist WHERE 1 ORDER BY symbol").fetchall()
        conn.close()
        return [r["symbol"] for r in rows] or ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    except Exception:
        return ["NIFTY", "BANKNIFTY", "FINNIFTY"]


def _get_account_size():
    if not _DB_OK:
        return 200000
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT account_size_inr FROM session_context ORDER BY session_date DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row["account_size_inr"] if row else 200000
    except Exception:
        return 200000


# ── Static: serve dashboard HTML ─────────────────────────────────────────
@fno_trap_bp.route('/', strict_slashes=False)
def serve_dashboard():
    app_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    return send_from_directory(app_dir, 'fno-trap-dashboard.html')


# ── Health ────────────────────────────────────────────────────────────────
@fno_trap_bp.route('/api/health')
def fno_trap_health():
    _ensure_scheduler()
    kite_ok = False
    if _ENGINE_OK:
        try:
            from fno_trap.kite_fetcher import get_kite
            kite_ok = get_kite() is not None
        except Exception:
            pass

    now = now_ist() if _ENGINE_OK else __import__('datetime').datetime.now()
    phase = get_time_phase() if _ENGINE_OK else "UNKNOWN"
    return jsonify({
        "ok":            True,
        "kite_connected": kite_ok,
        "engine_ok":     _ENGINE_OK,
        "db_ok":         _DB_OK,
        "phase":         phase,
        "server_time":   now.strftime("%H:%M:%S IST"),
        "backend":       "v2.0-full",
    })


# ── Watchlist ─────────────────────────────────────────────────────────────
@fno_trap_bp.route('/api/watchlist', methods=['GET'])
def get_watchlist():
    _ensure_scheduler()
    if not _DB_OK:
        return jsonify({"symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY"]})
    try:
        conn = get_connection()
        rows = conn.execute("SELECT symbol, lot_size, is_index FROM watchlist ORDER BY symbol").fetchall()
        conn.close()
        symbols = [r["symbol"] for r in rows] or ["NIFTY", "BANKNIFTY", "FINNIFTY"]
        return jsonify({"symbols": symbols})
    except Exception as e:
        log.error("FNO Trap: get_watchlist error: %s", e)
        return jsonify({"symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY"]})


@fno_trap_bp.route('/api/watchlist', methods=['POST'])
def add_watchlist():
    data = request.get_json(silent=True) or {}
    sym      = (data.get('symbol') or '').upper().strip()
    lot_size = int(data.get('lot_size', 50))
    is_index = int(data.get('is_index', 0))
    if not sym:
        return jsonify({'error': 'symbol required'}), 400
    if not _DB_OK:
        return jsonify({'ok': True, 'symbols': [sym]})
    try:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, lot_size, is_index) VALUES(?,?,?)",
            (sym, lot_size, is_index)
        )
        conn.commit()
        rows = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
        conn.close()
        return jsonify({'ok': True, 'symbols': [r["symbol"] for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@fno_trap_bp.route('/api/watchlist/<symbol>', methods=['DELETE'])
def remove_watchlist(symbol):
    sym = symbol.upper().strip()
    if not _DB_OK:
        return jsonify({'ok': True})
    try:
        conn = get_connection()
        conn.execute("DELETE FROM watchlist WHERE symbol=?", (sym,))
        conn.commit()
        rows = conn.execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
        conn.close()
        return jsonify({'ok': True, 'symbols': [r["symbol"] for r in rows]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Symbol search ───────────────────────────────────────────────────
@fno_trap_bp.route('/api/search')
def fno_search():
    """
    Search F&O universe: NSE stocks, NSE indices, BSE SENSEX/BANKEX.
    GET /fno-trap/api/search?q=RELI&limit=15
    Returns [{symbol, exchange, lot_size, type}]
    """
    q     = (request.args.get('q') or '').strip()
    limit = min(int(request.args.get('limit', 20)), 50)
    if len(q) < 2:
        return jsonify({'results': [], 'query': q})
    if not _ENGINE_OK:
        return jsonify({'results': [], 'query': q})
    try:
        from fno_trap.kite_fetcher import search_fno_universe
        results = search_fno_universe(q, limit)
        return jsonify({'results': results, 'query': q, 'count': len(results)})
    except Exception as e:
        log.error("FNO Trap: search error: %s", e)
        return jsonify({'results': [], 'error': str(e)})


# ── OI Chain Enhanced — feeds the rich OI panels in the dashboard ─────────
@fno_trap_bp.route('/api/oi-chain')
def fno_trap_oi_chain():
    """
    GET /fno-trap/api/oi-chain?symbol=NIFTY
    Returns full OI chain with summary, pivots, CE/PE walls, top writing zones.
    Delegates to the OI scanner options-chain for live data.
    """
    sym = request.args.get('symbol', 'NIFTY').upper().strip()

    # ── Try to get kite ────────────────────────────────────────────────────
    kite = None
    try:
        from fno_trap.kite_fetcher import get_kite
        kite = get_kite()
    except Exception:
        pass

    if not kite:
        return jsonify({'error': 'Kite not connected'}), 401

    try:
        from oi_scanner_routes import (
            get_all_instruments, inst_prefix, SPOT_MAP,
            kite_quote_chunked, trading_time_to_expiry, RISK_FREE_RATE,
            implied_vol
        )
        from datetime import date, datetime, timedelta

        instruments = get_all_instruments(kite)
        pfx = inst_prefix(sym)
        filtered = [i for i in instruments
                    if i['name'] == sym and i['instrument_type'] in ('CE', 'PE')]
        if not filtered:
            return jsonify({'error': f'No contracts found for {sym}'}), 404

        today = date.today()
        expiries = sorted({i['expiry'] for i in filtered if i['expiry'] and i['expiry'] >= today})
        if not expiries:
            return jsonify({'error': 'No upcoming expiries'}), 404
        target = expiries[0]

        # Build chain dict
        chain = {}
        for inst in filtered:
            if inst['expiry'] != target:
                continue
            k = float(inst['strike'])
            chain.setdefault(k, {'strike': k, 'CE': None, 'PE': None})
            chain[k][inst['instrument_type']] = inst

        strikes_sorted = sorted(chain.values(), key=lambda x: x['strike'])

        # Spot
        spot_sym = SPOT_MAP.get(sym, f'NSE:{sym}')
        spot = 0.0
        try:
            spot = kite.ltp([spot_sym])[spot_sym]['last_price']
        except Exception:
            pass

        # ATM
        atm_idx = (min(range(len(strikes_sorted)),
                       key=lambda i: abs(strikes_sorted[i]['strike'] - spot))
                   if spot else len(strikes_sorted) // 2)
        nearby = strikes_sorted[max(0, atm_idx - 15): atm_idx + 16]

        tokens = [f"{pfx}:{s[t]['tradingsymbol']}"
                  for s in nearby for t in ('CE', 'PE') if s[t]]
        # Also fetch futures for futures OI
        fut_tokens = []
        for inst in instruments:
            if inst['name'] == sym and inst['instrument_type'] == 'FUT' and inst.get('expiry') and inst['expiry'] >= today:
                fut_tokens.append(f"{pfx}:{inst['tradingsymbol']}")
                break

        quotes = kite_quote_chunked(kite, tokens + fut_tokens + [spot_sym]) if tokens else {}
        spot_q = quotes.get(spot_sym, {})
        spot_prev = float(spot_q.get('ohlc', {}).get('close', spot_q.get('close_price', spot)))
        if not spot_prev: spot_prev = spot

        T = trading_time_to_expiry(target)
        T_days = (target - today).days
        r = RISK_FREE_RATE

        rows = []
        total_ce_oi = 0
        total_pe_oi = 0
        ce_oi_by_strike = {}
        pe_oi_by_strike = {}
        atm_strike = strikes_sorted[atm_idx]['strike'] if strikes_sorted else 0

        for s in nearby:
            row = {'strike': s['strike'], 'T_days': T_days, 'is_atm': s['strike'] == atm_strike}
            for opt_type in ('CE', 'PE'):
                inst = s[opt_type]
                if not inst:
                    row[opt_type] = None
                    continue
                key = f"{pfx}:{inst['tradingsymbol']}"
                q = quotes.get(key, {})
                ltp  = float(q.get('last_price', 0) or 0)
                oi   = int(q.get('oi', 0) or 0)
                oi_prev = int(q.get('oi_day_low', 0) or 0)
                vol  = int(q.get('volume', 0) or 0)
                oi_chg = oi - oi_prev
                
                prev_px = float(q.get('ohlc', {}).get('close', q.get('close_price', ltp)))
                iv_curr = implied_vol(ltp, spot, s['strike'], T, r, opt_type) if ltp > 0 else 0
                iv_prev = implied_vol(prev_px, spot_prev, s['strike'], T, r, opt_type) if prev_px > 0 else 0

                oi_trend = "Increasing" if oi_chg > (oi_prev * 0.02) else "Reducing" if oi_chg < -(oi_prev * 0.02) else "Flat"
                if oi_prev == 0: oi_trend = "Increasing" if oi_chg > 0 else "Flat"
                px_trend = "Rising" if ltp > (prev_px * 1.01) else "Falling" if ltp < (prev_px * 0.99) else "Flat"
                if prev_px == 0: px_trend = "Flat"
                iv_trend = "Rising" if iv_curr > (iv_prev * 1.01) else "Falling" if iv_curr < (iv_prev * 0.99) else "Flat"
                if iv_prev == 0: iv_trend = "Flat"

                row[opt_type] = {
                    'ltp': round(ltp, 2),
                    'oi': oi,
                    'oi_chg': oi_chg,
                    'volume': vol,
                    'oi_trend': oi_trend,
                    'px_trend': px_trend,
                    'iv_trend': iv_trend,
                }
                if opt_type == 'CE':
                    total_ce_oi += oi
                    ce_oi_by_strike[s['strike']] = oi
                else:
                    total_pe_oi += oi
                    pe_oi_by_strike[s['strike']] = oi
            rows.append(row)

        # ── Max Pain ──────────────────────────────────────────────────────
        all_strikes = sorted(ce_oi_by_strike.keys() | pe_oi_by_strike.keys())
        max_pain_strike = atm_strike
        if all_strikes:
            pain_losses = {}
            for exp_s in all_strikes:
                loss = 0
                for s, oi in ce_oi_by_strike.items():
                    if exp_s > s:
                        loss += oi * (exp_s - s)
                for s, oi in pe_oi_by_strike.items():
                    if exp_s < s:
                        loss += oi * (s - exp_s)
                pain_losses[exp_s] = loss
            max_pain_strike = min(pain_losses, key=pain_losses.get)

        # ── CE Wall (highest CE OI = resistance) ───────────────────────
        ce_wall = max(ce_oi_by_strike, key=ce_oi_by_strike.get) if ce_oi_by_strike else 0
        pe_wall = max(pe_oi_by_strike, key=pe_oi_by_strike.get) if pe_oi_by_strike else 0

        # ── Overall PCR ───────────────────────────────────────────────────
        overall_pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 0
        pcr_label = ('Extreme Bullish' if overall_pcr > 1.5 else
                     'Bullish' if overall_pcr > 1.2 else
                     'Neutral' if overall_pcr > 0.8 else
                     'Bearish' if overall_pcr > 0.5 else 'Extreme Bearish')

        # ── Futures OI ────────────────────────────────────────────────────
        futures_oi_curr = 0
        futures_oi_prev = 0
        futures_ltp = spot
        if fut_tokens:
            fq = quotes.get(fut_tokens[0], {})
            futures_oi_curr = int(fq.get('oi', 0) or 0)
            futures_oi_prev = int(fq.get('oi_day_low', 0) or 0)
            futures_ltp = float(fq.get('last_price', spot) or spot)

        # ── Update rolling history cache ──
        with _fno_lock:
            if sym not in _fno_history:
                _fno_history[sym] = []
            
            # Fetch ATM strike options quotes for delta calculations
            atm_ce_oi = 0
            atm_pe_oi = 0
            atm_ce_ltp = 0
            atm_pe_ltp = 0
            atm_row = next((r for r in rows if r['strike'] == atm_strike), None)
            if atm_row:
                atm_ce = atm_row.get('CE') or {}
                atm_pe = atm_row.get('PE') or {}
                atm_ce_oi = atm_ce.get('oi', 0)
                atm_pe_oi = atm_pe.get('oi', 0)
                atm_ce_ltp = atm_ce.get('ltp', 0)
                atm_pe_ltp = atm_pe.get('ltp', 0)

            # strikes data for strike-specific derivatives
            strikes_data = {}
            for r_data in rows:
                strike_k = r_data["strike"]
                ce_d = r_data.get('CE') or {}
                pe_d = r_data.get('PE') or {}
                strikes_data[strike_k] = {
                    "ce_oi": ce_d.get("oi", 0),
                    "pe_oi": pe_d.get("oi", 0)
                }

            tick = {
                "timestamp": time.time(),
                "futures_oi": futures_oi_curr,
                "futures_ltp": futures_ltp,
                "spot_ltp": spot,
                "atm_ce_oi": atm_ce_oi,
                "atm_pe_oi": atm_pe_oi,
                "atm_ce_ltp": atm_ce_ltp,
                "atm_pe_ltp": atm_pe_ltp,
                "strikes": strikes_data
            }
            _fno_history[sym].append(tick)
            if len(_fno_history[sym]) > 4:
                _fno_history[sym] = _fno_history[sym][-4:]
            history = list(_fno_history[sym])

        # Step 1: Flat Futures OI Gate
        is_flat_futures = False
        futures_oi_change = 0
        if len(history) >= 3:
            fut_oi_ticks = [t["futures_oi"] for t in history[-3:]]
            if len(set(fut_oi_ticks)) == 1:
                is_flat_futures = True
            futures_oi_change = history[-1]["futures_oi"] - history[-2]["futures_oi"]
        else:
            futures_oi_change = futures_oi_curr - (futures_oi_prev or futures_oi_curr)
            if futures_oi_prev and futures_oi_curr == futures_oi_prev:
                is_flat_futures = True

        def _fmt_k(v):
            if v >= 10_000_000: return f'{v/10_000_000:.2f}Cr'
            if v >= 100_000:    return f'{v/100_000:.2f}L'
            if v >= 1_000:      return f'{v/1_000:.1f}K'
            return str(v)

        # ── Straddle ATM ─────────────────────────────────────────────────
        atm_row = next((r for r in rows if r['strike'] == atm_strike), None)
        straddle = 0
        if atm_row:
            ce_ltp = (atm_row.get('CE') or {}).get('ltp', 0) or 0
            pe_ltp = (atm_row.get('PE') or {}).get('ltp', 0) or 0
            straddle = round(ce_ltp + pe_ltp, 1)

        # ── Max Pain vs LTP ───────────────────────────────────────────────
        mp_vs_ltp = ('Above LTP' if max_pain_strike > spot else
                     'Below LTP' if max_pain_strike < spot else 'At LTP')

        # ── Pivot / S&R (Camarilla approximation using spot as proxy) ────
        # Use spot as HLC proxy (we don't have full day OHLC — use available data)
        # Better: try to get today's OHLC from Kite
        ohlc_data = {}
        try:
            ohlc_raw = kite.ltp([spot_sym])
            ohlc_data = ohlc_raw.get(spot_sym, {}).get('ohlc', {})
        except Exception:
            pass
        H = float(ohlc_data.get('high', spot) or spot)
        L = float(ohlc_data.get('low', spot) or spot)
        C = float(ohlc_data.get('close', spot) or spot)
        if H == L == C == spot:
            # fallback — estimate range from ATM straddle
            H = spot + straddle * 0.6
            L = spot - straddle * 0.6
        pivot = round((H + L + C) / 3, 2)
        r1 = round(2 * pivot - L, 2)
        r2 = round(pivot + (H - L), 2)
        r3 = round(H + 2 * (pivot - L), 2)
        s1 = round(2 * pivot - H, 2)
        s2 = round(pivot - (H - L), 2)
        s3 = round(L - 2 * (H - pivot), 2)

        # ── Day change vs prev close ──────────────────────────────────────
        prev_close = C  # C from OHLC is prev day close
        spot_chg     = round(spot - prev_close, 2) if prev_close and prev_close != spot else 0
        spot_chg_pct = round(spot_chg / prev_close * 100, 2) if prev_close else 0

        # ── Advanced Checklist & Proximity Calculations ──
        # Step 2: Basis Expansion/Contraction
        basis = futures_ltp - spot
        prev_basis = history[-2]["futures_ltp"] - history[-2]["spot_ltp"] if len(history) >= 2 else basis
        basis_trend = "Expanding"
        if basis > prev_basis:
            basis_trend = "Expanding"
        elif basis < prev_basis:
            basis_trend = "Contracting"
        elif basis == prev_basis:
            basis_trend = "Flat"

        # Step 3: ATM Delta Bias & Step 4: ATM LTP Divergence Checklist Gates
        is_atm_consolidation = False
        is_atm_ce_writers_dominating = False
        is_atm_pe_writers_dominating = False
        if len(history) >= 2:
            prev_tick = history[-2]
            curr_tick = history[-1]
            atm_ce_oi_change = curr_tick["atm_ce_oi"] - prev_tick["atm_ce_oi"]
            atm_pe_oi_change = curr_tick["atm_pe_oi"] - prev_tick["atm_pe_oi"]
            atm_ce_ltp_change = curr_tick["atm_ce_ltp"] - prev_tick["atm_ce_ltp"]
            atm_pe_ltp_change = curr_tick["atm_pe_ltp"] - prev_tick["atm_pe_ltp"]

            # Step 3: Consolidation check (Threshold: difference less than noise limit)
            noise_limit = 25000 if sym.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYIT") else 2000
            if abs(atm_ce_oi_change - atm_pe_oi_change) < (noise_limit * 0.5):
                is_atm_consolidation = True

            # Step 4: ATM LTP Divergence (Writers dominating CE or PE)
            if atm_ce_oi_change > 0 and atm_ce_ltp_change < 0:
                is_atm_ce_writers_dominating = True
            if atm_pe_oi_change > 0 and atm_pe_ltp_change < 0:
                is_atm_pe_writers_dominating = True

        # Gap Day Detection
        is_index_sym = sym.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "NIFTYIT")
        gap_threshold = 0.5 if is_index_sym else 0.75
        spot_open = float(spot_q.get('ohlc', {}).get('open', spot)) if spot_q else spot
        open_gap_pct = round((spot_open - spot_prev) / spot_prev * 100, 2) if spot_prev else 0.0
        gap_detected = abs(open_gap_pct) >= gap_threshold

        # Min sig OI threshold
        min_sig_oi = 25_000 if is_index_sym else 2_000


        # ── Top CE Writing zones (resistance — sorted by OI descending) ──
        ce_top = sorted(
            [{'strike': k, 'oi': v,
              'oi_chg': (rows[next((i for i, r in enumerate(rows) if r['strike'] == k), -1)].get('CE') or {}).get('oi_chg', 0) if any(r['strike'] == k for r in rows) else 0,
              'ltp': (next((r for r in rows if r['strike'] == k), {}).get('CE') or {}).get('ltp', 0),
              'volume': (next((r for r in rows if r['strike'] == k), {}).get('CE') or {}).get('volume', 0),
             } for k, v in ce_oi_by_strike.items()],
            key=lambda x: x['oi'], reverse=True
        )[:6]

        pe_top = sorted(
            [{'strike': k, 'oi': v,
              'oi_chg': (next((r for r in rows if r['strike'] == k), {}).get('PE') or {}).get('oi_chg', 0),
              'ltp': (next((r for r in rows if r['strike'] == k), {}).get('PE') or {}).get('ltp', 0),
              'volume': (next((r for r in rows if r['strike'] == k), {}).get('PE') or {}).get('volume', 0),
             } for k, v in pe_oi_by_strike.items()],
            key=lambda x: x['oi'], reverse=True
        )[:6]

        def _buildup_tag(oi, oi_chg, vol):
            if oi_chg > 0 and vol > 5000: return 'Long Buildup'
            if oi_chg < 0: return 'Unwinding'
            return None

        def _trap_tag(oi, strike_pcr, opt_type):
            if opt_type == 'CE' and oi > 500000 and strike_pcr < 0.5:
                return 'Call Trap'
            if opt_type == 'PE' and oi > 500000 and strike_pcr > 1.5:
                return 'Put Trap'
            return None

        for item in ce_top:
            item['oi_fmt'] = _fmt_k(item['oi'])
            pe_at = pe_oi_by_strike.get(item['strike'], 0)
            item['strike_pcr'] = round(pe_at / item['oi'], 2) if item['oi'] > 0 else 0
            item['buildup'] = _buildup_tag(item['oi'], item['oi_chg'], item['volume'])
            item['trap'] = _trap_tag(item['oi'], item['strike_pcr'], 'CE')

        for item in pe_top:
            item['oi_fmt'] = _fmt_k(item['oi'])
            ce_at = ce_oi_by_strike.get(item['strike'], 0)
            item['strike_pcr'] = round(item['oi'] / ce_at, 2) if ce_at > 0 else 0
            item['buildup'] = _buildup_tag(item['oi'], item['oi_chg'], item['volume'])
            item['trap'] = _trap_tag(item['oi'], item['strike_pcr'], 'PE')

        # ── Calculate Live Retail Action & HAction ──────────────────────
        final_retail_action = ""
        final_h_action = ""
        highest_conviction = 0
        signal_source = None
        h_action_source = None

        strike_step = (all_strikes[1] - all_strikes[0]) if len(all_strikes) >= 2 else (spot * 0.01)

        sorted_rows = sorted(rows, key=lambda x: abs(x['strike'] - spot))
        for i, r_data in enumerate(sorted_rows):
            is_above = r_data['strike'] >= spot
            is_below = r_data['strike'] < spot
            
            for opt_type in ('CE', 'PE'):
                inst = r_data.get(opt_type)
                if not inst: continue
                
                opt_oi = inst.get('oi', 0)
                if opt_oi < min_sig_oi: continue
                
                oi_d = inst.get('oi_trend', 'Flat')
                px_d = inst.get('px_trend', 'Flat')
                iv_d = inst.get('iv_trend', 'Flat')
                
                action = ""
                h_action = ""
                
                # Calculate OI Velocity from tick history
                prefix = opt_type.lower()
                oi_velocity = "Flat"
                strike = r_data["strike"]
                if len(history) >= 3:
                    h_st = [h["strikes"].get(strike, {}).get(f"{prefix}_oi", opt_oi) for h in history[-3:]]
                    chg_t0 = h_st[2] - h_st[1]
                    chg_t1 = h_st[1] - h_st[0]
                    if chg_t0 > chg_t1 and chg_t0 > 0:
                        oi_velocity = "Accelerating"
                    elif chg_t0 < chg_t1 and chg_t0 > 0:
                        oi_velocity = "Decelerating"
                    elif chg_t1 > 0 and chg_t0 < 0:
                        oi_velocity = "Reversing"
                    elif chg_t0 < 0 and chg_t1 < 0:
                        oi_velocity = "Reducing"

                # Calculate strike proximity dynamically based on dynamic strike step size
                dist = abs(strike - spot)
                if dist <= 0.5 * strike_step:
                    loc_str = "At Wall Zone"
                elif dist <= 2.5 * strike_step:
                    loc_str = "Testing"
                else:
                    loc_str = "Approaching"

                # ── Evaluate Checklist Gates & Scenario Matrix ──
                if is_flat_futures:
                    action = "IGNORE"
                    h_action = "❌ IGNORE — No institutional flow"
                elif loc_str == "At Wall Zone":
                    action = "IGNORE"
                    h_action = "❌ NO ENTRY — At Wall/Floor Zone"
                else:
                    # High Conviction Institutional H-Action Logic
                    # 1. ★★★★★ CE BUY (Breakout)
                    if opt_type == 'CE' and oi_d == "Reducing" and px_d == "Rising" and futures_oi_change > 0 and spot >= strike:
                        h_action = "★★★★★ CE BUY"
                    # 2. ★★★★★ PE BUY (Breakdown)
                    elif opt_type == 'PE' and oi_d == "Reducing" and px_d == "Rising" and futures_oi_change > 0 and spot <= strike:
                        h_action = "★★★★★ PE BUY"
                    # 3. ★★★★★ CE BUY Preemptive
                    elif opt_type == 'CE' and oi_d == "Reducing" and px_d in ("Rising", "Flat") and loc_str == "Testing" and is_above and futures_oi_change > 0:
                        h_action = "★★★★★ CE BUY Preemptive"
                    # 4. ★★★★★ PE BUY Preemptive
                    elif opt_type == 'PE' and oi_d == "Reducing" and px_d in ("Rising", "Flat") and loc_str == "Testing" and is_below and futures_oi_change > 0:
                        h_action = "★★★★★ PE BUY Preemptive"
                    # 5. ★★★★ CE BUY
                    elif opt_type == 'CE' and oi_d == "Increasing" and px_d == "Rising" and futures_oi_change > 0 and loc_str == "Testing" and is_above:
                        h_action = "★★★★ CE BUY"
                    # 6. ★★★★ PE BUY
                    elif opt_type == 'PE' and oi_d == "Increasing" and px_d == "Rising" and futures_oi_change > 0 and loc_str == "Testing" and is_below:
                        h_action = "★★★★ PE BUY"
                    # 7. ⭐⭐⭐ PREPARE for CE BUY
                    elif opt_type == 'CE' and oi_d == "Increasing" and px_d == "Falling" and oi_velocity == "Decelerating" and loc_str == "Testing" and is_above:
                        h_action = "⭐⭐⭐ PREPARE for CE BUY"
                    # 8. ⭐⭐⭐ PREPARE for PE BUY
                    elif opt_type == 'PE' and oi_d == "Increasing" and px_d == "Falling" and oi_velocity == "Decelerating" and loc_str == "Testing" and is_below:
                        h_action = "⭐⭐⭐ PREPARE for PE BUY"
                    # 9. ⭐⭐⭐ WAIT — Floor/Wall forming
                    elif opt_type == 'CE' and oi_d == "Increasing" and oi_velocity == "Accelerating" and px_d == "Falling" and loc_str == "Approaching":
                        h_action = "⭐⭐⭐ WAIT — Floor/Wall forming"
                    elif opt_type == 'PE' and oi_d == "Increasing" and oi_velocity == "Accelerating" and px_d == "Falling" and loc_str == "Approaching":
                        h_action = "⭐⭐⭐ WAIT — Floor/Wall forming"

                    # ── Apply Advanced Checklist Step 3 & Step 4 Gates ──
                    if "BUY" in h_action:
                        # Step 3: ATM consolidation check
                        if is_atm_consolidation:
                            h_action = "⚠️ CONSOLIDATION — Strike OI Bias Neutral"
                        # Step 4: ATM LTP divergence check (Writers dominating)
                        elif opt_type == 'CE' and is_atm_ce_writers_dominating:
                            h_action = "⚠️ WAIT — Call Writers Dominating ATM"
                        elif opt_type == 'PE' and is_atm_pe_writers_dominating:
                            h_action = "⚠️ WAIT — Put Writers Dominating ATM"
                        else:
                            # Step 2 (Gap 3): Apply position sizing scaling based on basis expansion
                            size_tag = " (Full Size)"
                            if basis_trend == "Flat":
                                size_tag = " (Half Size — Flat Basis)"
                            elif basis_trend == "Contracting":
                                size_tag = " (Quarter Size — Contracting Basis)"
                            h_action += size_tag

                    # Retail Action Logic
                    if opt_type == 'CE':
                        if oi_d == "Increasing" and px_d == "Falling" and iv_d == "Falling":
                            action = "WAIT (Building wall)" if is_above else "PE BUY"
                        elif oi_d == "Increasing" and px_d == "Rising" and iv_d == "Rising":
                            action = "CE BUY"
                        elif oi_d == "Reducing" and px_d == "Rising" and iv_d == "Rising":
                            action = "CE BUY" if is_above else "IGNORE"
                        elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Rising":
                            action = "WAIT"
                    elif opt_type == 'PE':
                        if oi_d == "Increasing" and px_d == "Falling" and iv_d == "Falling":
                            action = "WAIT (Building floor)" if is_below else "CE BUY"
                        elif oi_d == "Increasing" and px_d == "Rising" and iv_d == "Rising":
                            action = "PE BUY"
                        elif oi_d == "Reducing" and px_d == "Rising" and iv_d == "Rising":
                            action = "PE BUY" if is_below else "IGNORE"
                        elif oi_d == "Flat" and px_d == "Rising" and iv_d == "Rising":
                            action = "WAIT"

                # Resolve Conviction Precedence
                conviction_score = 0
                if "★★★★★" in h_action: conviction_score = 5
                elif "★★★★" in h_action: conviction_score = 4
                elif "⭐⭐⭐" in h_action: conviction_score = 3
                elif "WAIT — " in h_action or "CONSOLIDATION" in h_action: conviction_score = 2
                elif "NO ENTRY" in h_action: conviction_score = 2
                elif "IGNORE" in h_action: conviction_score = 1

                if action and "IGNORE" not in action and not final_retail_action:
                    final_retail_action = action
                    signal_source = {"strike": strike, "oi": opt_oi, "side": opt_type}
                    
                if h_action and conviction_score > highest_conviction:
                    highest_conviction = conviction_score
                    final_h_action = h_action
                    h_action_source = {"strike": strike, "oi": opt_oi, "side": opt_type}

        # ── Format chain rows for heatmap ────────────────────────────────
        chain_rows = []
        for row in rows:
            ce = row.get('CE') or {}
            pe = row.get('PE') or {}
            chain_rows.append({
                'strike': row['strike'],
                'is_atm': row.get('is_atm', False),
                'is_max_pain': row['strike'] == max_pain_strike,
                'ce_ltp': ce.get('ltp', 0),
                'ce_oi': ce.get('oi', 0),
                'ce_oi_fmt': _fmt_k(ce.get('oi', 0)),
                'ce_oi_chg': ce.get('oi_chg', 0),
                'pe_ltp': pe.get('ltp', 0),
                'pe_oi': pe.get('oi', 0),
                'pe_oi_fmt': _fmt_k(pe.get('oi', 0)),
                'pe_oi_chg': pe.get('oi_chg', 0),
            })

        return jsonify({
            'symbol': sym,
            'expiry': str(target),
            'spot': round(spot, 2),
            'straddle': straddle,
            'overall_pcr': overall_pcr,
            'pcr_label': pcr_label,
            'max_pain': max_pain_strike,
            'max_pain_vs_ltp': mp_vs_ltp,
            'ce_wall': ce_wall,
            'pe_wall': pe_wall,
            'futures_oi_curr': _fmt_k(futures_oi_curr),
            'futures_oi_prev': _fmt_k(futures_oi_prev),
            'total_ce_oi': _fmt_k(total_ce_oi),
            'total_pe_oi': _fmt_k(total_pe_oi),
            'pivots': {
                'R3': r3, 'R2': r2, 'R1': r1,
                'P': pivot,
                'S1': s1, 'S2': s2, 'S3': s3,
            },
            'retail_action': final_retail_action or "WAIT (No clear setup)",
            'h_action': final_h_action,
            'signal_source': signal_source,
            'h_action_source': h_action_source,
            'gap_detected': gap_detected,
            'ce_writing': ce_top,
            'pe_writing': pe_top,
            'chain': chain_rows,
            'spot_chg': spot_chg,
            'spot_chg_pct': spot_chg_pct,
        })

    except Exception as e:
        log.exception('fno_trap_oi_chain error for %s', sym)
        return jsonify({'error': str(e)}), 500


# ── Card ─────────────────────────────────────────────────────────────────
@fno_trap_bp.route('/api/card')
def fno_trap_card():
    sym = request.args.get('symbol', 'NIFTY').upper().strip()
    _ensure_scheduler()

    if not _ENGINE_OK:
        return _stub_card(sym)

    # Try in-memory cache first
    cached = get_cached_card(sym)
    if cached and cached.get("pipeline") not in ("no_data", None):
        return jsonify(cached)

    # Run cycle — will use prev-session DB data if market closed,
    # or do a live Kite fetch if market open
    try:
        acct = _get_account_size()
        payload = run_cycle(sym, acct)

        # If DB is still empty after run_cycle (no prev session data),
        # AND Kite is connected → force a live data pull regardless of market hours
        if payload.get("pipeline") == "no_data" and _DB_OK:
            live_payload = _force_live_fetch(sym, acct)
            if live_payload:
                return jsonify(live_payload)

        return jsonify(payload)
    except Exception as e:
        log.error("FNO Trap: run_cycle error for %s: %s", sym, e)
        return _stub_card(sym)


def _force_live_fetch(sym: str, acct: float):
    """
    Pull live data from Kite outside market hours.
    Kite returns last-traded LTP and OI values 24/7.
    Populates DB so subsequent calls show prev-session data.
    """
    try:
        from fno_trap.kite_fetcher import get_kite, fetch_spot, get_near_expiry, fetch_oi_snapshot
        from fno_trap.trap_engine import run_cycle as _run

        kite = get_kite()
        if not kite:
            log.debug("FNO Trap: forced fetch skipped — no Kite session")
            return None

        log.info("FNO Trap: forcing live Kite fetch for %s (DB empty)", sym)

        # Fetch spot (always works, even after close)
        spot = fetch_spot(sym)
        if not spot:
            log.warning("FNO Trap: forced fetch — spot unavailable for %s", sym)
            return None

        # Get near expiry from instruments list
        expiry = get_near_expiry(sym)
        if not expiry:
            log.warning("FNO Trap: forced fetch — no expiry found for %s", sym)
            return None

        # Fetch OI snapshot — Kite returns last-session values
        oi_rows = fetch_oi_snapshot(sym, expiry)
        log.info("FNO Trap: forced fetch got %d OI rows for %s", len(oi_rows), sym)

        if not oi_rows:
            return None

        # Now re-run cycle — DB is populated, will use prev-session mode
        payload = _run(sym, acct)
        return payload

    except Exception as e:
        log.error("FNO Trap: _force_live_fetch error for %s: %s", sym, e)
        return None


def _stub_card(sym):
    """Fallback when engine is unavailable."""
    from fno_trap.time_phase import get_time_phase, now_ist
    now = now_ist()
    phase = get_time_phase()
    return jsonify({
        "card_state": "MARKET_CLOSED" if phase == "MARKET_CLOSED" else "WAIT",
        "trap_dir": None, "action": None, "strike": None, "expiry": None,
        "lots": 1, "lot_cost": None, "premium": None, "stop": None,
        "spot_inval": None, "exit_time": "15:00",
        "why": "Backend engine initialising — please wait.",
        "spot_t1": None, "spot_t2": None, "spot": 0, "spot_dir": "up",
        "phase": phase, "dte": "—", "data_conf": "amber",
        "snapshot_time": now.strftime("%H:%M"), "confidence": 0,
        "wait_reason": "Engine starting up.", "wait_mins": 0, "wait_total_mins": 0,
        "avoid_reason": None, "block_reason": None, "warnings": [],
        "has_position": False, "position_state": None,
        "pos_strike": None, "pos_wap": None, "pos_now": 0, "pos_lots": None,
        "survivability_snaps": 99, "trail": None,
        "trap_score": 0, "pcr_oi": 0, "pcr_vol": 0, "max_pain": 0, "vwap": 0,
        "pivot_r1": 0, "pivot_s1": 0, "oi_data": [], "regime": None,
        "vix": None, "exec_gate": None, "psi": None, "crowding": 0, "conditions": [],
        "ws_bandwidth": None, "oi_age": None, "pipeline": "starting",
        "cooldown_active": False, "cooldown_expiry": None,
        "has_event": False, "event_text": None, "nifty_spot": 0, "nifty_dir": "up",
    })


# ── Position log / exit ───────────────────────────────────────────────────
@fno_trap_bp.route('/api/position/log', methods=['POST'])
def fno_trap_position_log():
    data = request.get_json(silent=True) or {}
    sym = (data.get('symbol') or '').upper().strip()
    if not sym:
        return jsonify({'error': 'symbol required'}), 400
    if not _DB_OK:
        return jsonify({'ok': True, 'position_id': f'pos-{sym}-stub'})
    try:
        conn = get_connection()
        # Upsert: close any existing open position first
        conn.execute(
            "UPDATE position_entries SET is_open=0, closed_at=datetime('now') WHERE symbol=? AND is_open=1",
            (sym,)
        )
        cur = conn.execute("""
            INSERT INTO position_entries
            (symbol, strike, option_type, expiry, entry_price, lots_total)
            VALUES(?,?,?,?,?,?)
        """, (
            sym,
            data.get('strike_raw') or 0,
            data.get('option_type') or 'CE',
            data.get('expiry') or now_ist().date().isoformat(),
            data.get('fill_price') or 0,
            data.get('lots') or 1,
        ))
        pos_id = cur.lastrowid
        fill_time = data.get('fill_time') or now_ist().strftime("%H:%M")
        conn.execute("""
            INSERT INTO position_fills(position_id, fill_sequence, lots, fill_price, fill_time, fill_source)
            VALUES(?,1,?,?,?,'ENTRY')
        """, (pos_id, data.get('lots') or 1, data.get('fill_price') or 0, fill_time))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'position_id': pos_id})
    except Exception as e:
        log.error("FNO Trap: position_log error: %s", e)
        return jsonify({'error': str(e)}), 500


@fno_trap_bp.route('/api/position/exit', methods=['POST'])
def fno_trap_position_exit():
    data = request.get_json(silent=True) or {}
    sym = (data.get('symbol') or '').upper().strip()
    if not _DB_OK:
        return jsonify({'ok': True})
    try:
        conn = get_connection()
        pos = conn.execute(
            "SELECT id FROM position_entries WHERE symbol=? AND is_open=1 ORDER BY opened_at DESC LIMIT 1",
            (sym,)
        ).fetchone()
        if pos:
            fill_time = data.get('exit_time') or now_ist().strftime("%H:%M")
            conn.execute("""
                INSERT INTO position_fills(position_id, fill_sequence, lots, fill_price, fill_time, fill_source)
                SELECT id, COALESCE(MAX(fill_sequence),0)+1, lots_total, ?, ?, 'FULL_EXIT'
                FROM position_entries WHERE id=?
            """, (data.get('exit_price') or 0, fill_time, pos["id"]))
            conn.execute(
                "UPDATE position_entries SET is_open=0, closed_at=datetime('now') WHERE id=?",
                (pos["id"],)
            )
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        log.error("FNO Trap: position_exit error: %s", e)
        return jsonify({'error': str(e)}), 500


# ── Session / Discipline ──────────────────────────────────────────────────
@fno_trap_bp.route('/api/session', methods=['GET'])
def fno_trap_session_get():
    now = now_ist() if _ENGINE_OK else __import__('datetime').datetime.now()
    if not _DB_OK:
        return jsonify({"account_size_inr": 200000, "max_daily_loss_inr": 5000,
                        "max_consecutive_losses": 2, "phase": "UNKNOWN",
                        "server_time": now.strftime("%H:%M:%S IST")})
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM session_context ORDER BY session_date DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return jsonify({**dict(row),
                            "phase": get_time_phase() if _ENGINE_OK else "UNKNOWN",
                            "server_time": now.strftime("%H:%M:%S IST")})
        return jsonify({"account_size_inr": 200000, "max_daily_loss_inr": 5000,
                        "max_consecutive_losses": 2})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@fno_trap_bp.route('/api/session', methods=['PATCH'])
def fno_trap_session_patch():
    data = request.get_json(silent=True) or {}
    if not _DB_OK:
        return jsonify({'ok': True})
    try:
        today = now_ist().date().isoformat() if _ENGINE_OK else __import__('datetime').date.today().isoformat()
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO session_context(session_date) VALUES(?)", (today,))
        for key in ('account_size_inr', 'max_daily_loss_inr', 'max_consecutive_losses'):
            if key in data:
                conn.execute(
                    f"UPDATE session_context SET {key}=? WHERE session_date=?",
                    (data[key], today)
                )
        conn.commit()
        row = conn.execute("SELECT * FROM session_context WHERE session_date=?", (today,)).fetchone()
        conn.close()
        return jsonify({'ok': True, **(dict(row) if row else {})})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
