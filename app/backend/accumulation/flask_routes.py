# flask_routes.py — FnO Accumulation Scanner Routes
# Add to existing app.py:
#   from flask_routes import register_accumulation_routes
#   register_accumulation_routes(app, kite, get_vwap, get_ltp, india_vix, get_gift_nifty)

import threading
import logging
import time
import json as _json
from datetime import datetime
from flask import Blueprint, jsonify, render_template, request, current_app, Response

from accumulation_scanner import run_scan, _last_scan_results, _last_scan_time, _scan_progress
from sector_map import load_fno_symbols, refresh_fno_symbols, check_symbol_list_freshness
from event_calendar import fetch_events, get_calendar_status
from error_handler import get_recent_errors
from config import SCAN_INTERVAL_SEC, MARKET_OPEN, MARKET_CLOSE

log = logging.getLogger("flask_routes")


def _safe_json(obj):
    """Recursively convert numpy/pandas scalar types to native Python types."""
    try:
        import numpy as np
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except ImportError:
        pass
    if isinstance(obj, dict):
        return {k: _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_json(i) for i in obj]
    return obj


def _json_response(data, status=200):
    """Return a JSON Response using _safe_json to handle numpy types."""
    body = _json.dumps(_safe_json(data), ensure_ascii=False)
    return Response(body, status=status, mimetype="application/json")

# ── Globals ───────────────────────────────────────────────────────────────────
_scan_thread = None
_gift_nifty_manual = {"value": None, "entered_at": None}
_last_payload = {}
_alert_cache = {}  # { symbol: { "state": "PRE_BREAKOUT", "time": datetime } }

# ── Active-client heartbeat ───────────────────────────────────────────────────
# The background scanner skips run_scan() when no page has polled for >90 s.
_accum_last_client_time = time.time()  # Start scanning immediately without waiting for first client poll
_accum_client_lock = threading.Lock()

def _accum_notify_client():
    """Call from /api/accumulation each time the frontend polls."""
    global _accum_last_client_time
    with _accum_client_lock:
        _accum_last_client_time = time.time()

def _accum_has_clients(timeout_sec=90):
    """Returns True if a page has polled within the last timeout_sec seconds."""
    with _accum_client_lock:
        return (time.time() - _accum_last_client_time) < timeout_sec

import os
import urllib.request
import urllib.parse

def _send_tg_alert(symbol, direction, state, ltp, target, coil_str):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        return
        
    icon = "🟢" if direction == "UP" else "🔴"
    if state == "PRE_BREAKOUT":
        title = f"{icon} PRE-BREAKOUT DETECTED: {symbol}"
        body = f"Price: ₹{ltp}\nStatus: Pinned / Coiling ({coil_str})\nGet ready! Expected breakout {direction} in 15-20 mins."
    else:
        title = f"{icon} BREAKING NOW: {symbol}"
        body = f"Price: ₹{ltp}\nStatus: Breaking out {direction}\nTarget 1: ₹{target}\nCoil Time: {coil_str}"
        
    msg = f"{title}\n\n{body}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({'chat_id': chat_id, 'text': msg}).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=payload, method='POST')
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.warning(f"Telegram alert failed for {symbol}: {e}")


def register_accumulation_routes(app, kite, get_vwap_fn, get_ltp_fn,
                                   india_vix_fn, get_gift_nifty_fn=None):
    """
    Call this from your existing app.py to register all scanner routes.

    Example in app.py:
        from flask_routes import register_accumulation_routes
        register_accumulation_routes(app, kite, get_vwap, get_ltp, india_vix, get_gift_nifty)
    """

    bp = Blueprint("accumulation", __name__, template_folder="templates")

    # ── Background scan thread ─────────────────────────────────────────────

    def _background_scanner():
        global _last_payload
        log.info("Background scanner thread started")
        while True:
            try:
                # ── Pause when no webpage is open ─────────────────────────────
                if not _accum_has_clients():
                    time.sleep(10)
                    continue
                # ──────────────────────────────────────────────────────────────
                now = datetime.now()
                def t(s): return datetime.strptime(s, "%H:%M").time()
                # Always scan — coiling/squeeze detection is valid on EOD 15min data
                # at any time. market_state in the payload signals live vs EOD to frontend.
                if True:
                    symbols = load_fno_symbols()

                    # Gift Nifty: prefer auto-fetch, fall back to manual
                    def gift_nifty_resolved():
                        if get_gift_nifty_fn:
                            try:
                                v = get_gift_nifty_fn()
                                if v:
                                    return v
                            except Exception:
                                pass
                        # Manual fallback
                        mn = _gift_nifty_manual
                        if mn["value"] and mn["entered_at"]:
                            age_min = (datetime.now() - mn["entered_at"]).seconds // 60
                            from config import GIFT_NIFTY_STALE_MIN
                            if age_min <= GIFT_NIFTY_STALE_MIN:
                                return mn["value"]
                        return None

                    payload = run_scan(
                        kite, symbols,
                        get_vwap_fn, get_ltp_fn,
                        india_vix_fn, gift_nifty_resolved
                    )
                    _last_payload = payload
                    
                    # Process Telegram Alerts
                    for r in payload.get('results', []):
                        state = r.get("state")
                        sym = r.get("symbol")
                        if state in ("PRE_BREAKOUT", "BREAKING"):
                            last_alert = _alert_cache.get(sym)
                            if not last_alert or last_alert["state"] != state or (datetime.now() - last_alert["time"]).total_seconds() > 3600:
                                _send_tg_alert(
                                    sym, r.get("direction", "UP"), state, 
                                    r.get("ltp"), r.get("target1"), r.get("time_in_coil", "")
                                )
                                _alert_cache[sym] = {"state": state, "time": datetime.now()}
                    
                    log.info(f"Scan complete: {len(payload.get('results', []))} stocks found")

            except Exception as e:
                log.error(f"Background scanner error: {e}")
                import traceback
                traceback.print_exc()

            time.sleep(SCAN_INTERVAL_SEC)

    # Start background thread
    global _scan_thread
    if _scan_thread is None or not _scan_thread.is_alive():
        _scan_thread = threading.Thread(target=_background_scanner, daemon=True)
        _scan_thread.start()

    # ── Routes ─────────────────────────────────────────────────────────────

    @bp.route("/accumulation")
    def accumulation_dashboard():
        """Render the scanner dashboard page."""
        return render_template("accumulation.html")

    @bp.route("/api/accumulation")
    def api_accumulation():
        """
        Main API endpoint — returns full scan payload.
        Clients poll this every 30 seconds.
        """
        _accum_notify_client()  # heartbeat: page is open
        if not _last_payload:
            return _json_response({
                "error": "Scan not yet complete",
                "market_state": "LOADING",
                "progress": _scan_progress
            }, 202)
        return _json_response(_last_payload)

    @bp.route("/api/accumulation/stock/<symbol>")
    def api_stock_detail(symbol):
        """Get expanded details for a specific stock."""
        results = _last_payload.get("results", [])
        stock = next((r for r in results if r["symbol"] == symbol.upper()), None)
        if not stock:
            return jsonify({"error": "Symbol not found in current scan"}), 404
        return _json_response(stock)

    @bp.route("/api/accumulation/sector")
    def api_sector():
        """Sector signals summary."""
        return _json_response(_last_payload.get("sector_signals", {}))

    @bp.route("/api/symbols/refresh", methods=["POST"])
    def api_symbols_refresh():
        """Manually trigger FnO symbol list refresh."""
        result = refresh_fno_symbols(kite)
        return jsonify(result)

    @bp.route("/api/symbols/status")
    def api_symbols_status():
        """Check freshness of symbol list."""
        return jsonify(check_symbol_list_freshness())

    @bp.route("/api/gift-nifty", methods=["POST"])
    def api_gift_nifty_manual():
        """
        Manual Gift Nifty input.
        POST {"value": 22500.5}
        """
        data = request.get_json()
        val = data.get("value")
        if val is None:
            return jsonify({"error": "value required"}), 400
        _gift_nifty_manual["value"] = float(val)
        _gift_nifty_manual["entered_at"] = datetime.now()
        return jsonify({"ok": True, "value": float(val),
                        "entered_at": datetime.now().isoformat()})

    @bp.route("/api/errors")
    def api_errors():
        """Last 5 scanner errors — shown in dashboard footer."""
        return jsonify(get_recent_errors())

    @bp.route("/api/scan/trigger", methods=["POST"])
    def api_scan_trigger():
        """
        Force an immediate scan (for testing / manual refresh).
        Runs in current thread — may take up to 3 minutes.
        """
        try:
            symbols = load_fno_symbols()
            payload = run_scan(kite, symbols, get_vwap_fn, get_ltp_fn,
                               india_vix_fn, get_gift_nifty_fn)
            global _last_payload
            _last_payload = payload
            return jsonify({"ok": True, "stocks_found": len(payload.get("results", []))})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    app.register_blueprint(bp)
    log.info("Accumulation scanner routes registered")
    return bp
