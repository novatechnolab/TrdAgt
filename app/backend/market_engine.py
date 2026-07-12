"""
market_engine.py
────────────────
Unified engine for:
  1. CVD  — Cumulative Volume Delta (tick-by-tick)
  2. VWAP — Volume Weighted Average Price (intraday, resets at session start)
  3. Order Book — 20-level bid/ask depth snapshot
  4. OI + Volume — Open Interest vs Volume correlation (F&O, index + stock)

Fed by Kite WebSocket MODE_FULL ticks.
OI data polled separately via kite.quote() since WebSocket doesn't carry OI.
"""

import threading
import math
from collections import deque
from datetime import datetime


MAX_HISTORY = 500   # rolling tick points per symbol


# ══════════════════════════════════════════════════════════════════════════════
# 1. CVD Engine
# ══════════════════════════════════════════════════════════════════════════════

class CVDEngine:
    def __init__(self):
        self._lock       = threading.Lock()
        self._cvd        = 0.0
        self._buy_vol    = 0.0
        self._sell_vol   = 0.0
        self._last_price = None
        self._tick_count = 0
        self._history    : deque[dict] = deque(maxlen=MAX_HISTORY)

    def process_tick(self, price: float, qty: int, ts) -> None:
        with self._lock:
            if self._last_price is None:
                delta = 0
            elif price >= self._last_price:
                delta          = qty
                self._buy_vol += qty
            else:
                delta           = -qty
                self._sell_vol += qty

            self._cvd        += delta
            self._last_price  = price
            self._tick_count += 1

            label = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)
            self._history.append({
                "ts"      : label,
                "price"   : price,
                "cvd"     : round(self._cvd, 2),
                "delta"   : round(delta, 2),
                "buy_vol" : round(self._buy_vol, 2),
                "sell_vol": round(self._sell_vol, 2),
            })

    def snapshot(self) -> dict:
        with self._lock:
            hist  = list(self._history)
            cvd   = self._cvd
            bv    = self._buy_vol
            sv    = self._sell_vol
            price = self._last_price
            ticks = self._tick_count

        bias = "BULLISH" if cvd > 0 else ("BEARISH" if cvd < 0 else "NEUTRAL")
        div  = _detect_divergence(hist)
        return {
            "cvd"       : round(cvd, 2),
            "buy_vol"   : round(bv, 2),
            "sell_vol"  : round(sv, 2),
            "net_delta" : round(bv - sv, 2),
            "tick_count": ticks,
            "last_price": price,
            "bias"      : bias,
            "divergence": div,
            "history"   : hist[-200:],
        }

    def reset(self):
        with self._lock:
            self._cvd = self._buy_vol = self._sell_vol = 0.0
            self._last_price = None
            self._tick_count = 0
            self._history.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 2. VWAP Engine
# ══════════════════════════════════════════════════════════════════════════════

class VWAPEngine:
    """
    Intraday VWAP = Σ(price × volume) / Σ(volume)
    Bands = VWAP ± 1 standard deviation
    Resets each session via reset().
    """

    def __init__(self):
        self._lock       = threading.Lock()
        self._cum_pv     = 0.0   # Σ price × vol
        self._cum_vol    = 0.0   # Σ vol
        self._cum_p2v    = 0.0   # Σ price² × vol  (for std dev)
        self._last_price = None
        self._history    : deque[dict] = deque(maxlen=MAX_HISTORY)

    def process_tick(self, price: float, qty: int, ts) -> None:
        with self._lock:
            self._cum_pv  += price * qty
            self._cum_vol += qty
            self._cum_p2v += (price ** 2) * qty

            vwap = self._cum_pv / self._cum_vol if self._cum_vol else price
            if self._cum_vol > 0:
                variance = max(0, (self._cum_p2v / self._cum_vol) - vwap ** 2)
                std      = math.sqrt(variance)
            else:
                std = 0

            self._last_price = price
            label = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)
            self._history.append({
                "ts"        : label,
                "price"     : price,
                "vwap"      : round(vwap, 2),
                "upper_band": round(vwap + std, 2),
                "lower_band": round(vwap - std, 2),
                "vol"       : qty,
            })

    def snapshot(self) -> dict:
        with self._lock:
            hist    = list(self._history)
            cum_vol = self._cum_vol
            cum_pv  = self._cum_pv
            cum_p2v = self._cum_p2v
            price   = self._last_price

        if not hist or price is None:
            return {"vwap": None, "signal": "WAITING", "history": []}

        vwap = cum_pv / cum_vol if cum_vol else price
        if cum_vol > 0:
            variance = max(0, (cum_p2v / cum_vol) - vwap ** 2)
            std      = math.sqrt(variance)
        else:
            std = 0

        # Volume trend: recent 10 ticks vs prior 10
        vol_trend = "FLAT"
        if len(hist) >= 20:
            recent_vol = sum(h["vol"] for h in hist[-10:])
            prior_vol  = sum(h["vol"] for h in hist[-20:-10])
            if prior_vol > 0:
                chg = (recent_vol - prior_vol) / prior_vol
                vol_trend = "RISING" if chg > 0.1 else ("FALLING" if chg < -0.1 else "FLAT")

        if price > vwap and vol_trend == "RISING":
            signal = "BULLISH"
        elif price < vwap and vol_trend == "RISING":
            signal = "BEARISH"
        elif price > vwap:
            signal = "ABOVE_VWAP"
        elif price < vwap:
            signal = "BELOW_VWAP"
        else:
            signal = "AT_VWAP"

        return {
            "vwap"      : round(vwap, 2),
            "upper_band": round(vwap + std, 2),
            "lower_band": round(vwap - std, 2),
            "std"       : round(std, 2),
            "vol_trend" : vol_trend,
            "signal"    : signal,
            "cum_volume": round(cum_vol, 0),
            "history"   : hist[-200:],
        }

    def reset(self):
        with self._lock:
            self._cum_pv = self._cum_vol = self._cum_p2v = 0.0
            self._last_price = None
            self._history.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Order Book Engine  (20-level bid/ask)
# ══════════════════════════════════════════════════════════════════════════════

class OrderBookEngine:
    """
    Stores 20-level bid/ask depth from Kite MODE_FULL ticks.
    Detects:
      - Clusters : level qty > 3× average  (institutional interest)
      - Walls    : level qty > 5× average  (strong support/resistance)
      - Absorbed walls: wall price near last trade AND disappeared from book
    """

    CLUSTER_MULTIPLIER = 3.0
    WALL_MULTIPLIER    = 5.0
    DEPTH              = 20
    # wall is "absorbed" only if disappeared within this many ticks of price
    ABSORPTION_PRICE_PROXIMITY = 5.0   # points; tune per instrument

    def __init__(self):
        self._lock        = threading.Lock()
        self._bids        : list[dict] = []
        self._asks        : list[dict] = []
        self._last_price  : float | None = None
        self._updated     = None
        self._bid_history : deque[list] = deque(maxlen=50)
        self._ask_history : deque[list] = deque(maxlen=50)

    def process_tick(self, tick: dict) -> None:
        depth = tick.get("depth", {})
        bids  = depth.get("buy",  [])
        asks  = depth.get("sell", [])
        if not bids and not asks:
            return
        with self._lock:
            self._bids       = bids[:self.DEPTH]
            self._asks       = asks[:self.DEPTH]
            self._last_price = tick.get("last_price") or self._last_price
            self._updated    = datetime.now()
            self._bid_history.append(
                [(b.get("price", 0), b.get("quantity", 0)) for b in self._bids])
            self._ask_history.append(
                [(a.get("price", 0), a.get("quantity", 0)) for a in self._asks])

    def snapshot(self) -> dict:
        with self._lock:
            bids      = list(self._bids)
            asks      = list(self._asks)
            updated   = self._updated
            bid_hist  = list(self._bid_history)
            ask_hist  = list(self._ask_history)
            ltp       = self._last_price

        if not bids and not asks:
            return {
                "bids": [], "asks": [], "top5_bids": [], "top5_asks": [],
                "signal": "WAITING", "bid_clusters": [], "ask_clusters": [],
                "bid_walls": [], "ask_walls": [],
                "absorbed_bid_walls": [], "absorbed_ask_walls": [],
                "imbalance": None, "top5_imbalance": None, "depth_levels": 0,
                "total_bid_qty": 0, "total_ask_qty": 0,
                "top5_bid_qty": 0, "top5_ask_qty": 0,
            }

        total_bid_qty = sum(b.get("quantity", 0) for b in bids)
        total_ask_qty = sum(a.get("quantity", 0) for a in asks)
        imbalance     = round(total_bid_qty / total_ask_qty, 2) if total_ask_qty else None

        top5_bid      = sum(b.get("quantity", 0) for b in bids[:5])
        top5_ask      = sum(a.get("quantity", 0) for a in asks[:5])
        top5_imbalance = round(top5_bid / top5_ask, 2) if top5_ask else None

        avg_bid = (total_bid_qty / len(bids)) if bids else 0
        avg_ask = (total_ask_qty / len(asks)) if asks else 0

        bid_clusters = [b for b in bids if b.get("quantity", 0) > avg_bid * self.CLUSTER_MULTIPLIER]
        ask_clusters = [a for a in asks if a.get("quantity", 0) > avg_ask * self.CLUSTER_MULTIPLIER]
        bid_walls    = [b for b in bids if b.get("quantity", 0) > avg_bid * self.WALL_MULTIPLIER]
        ask_walls    = [a for a in asks if a.get("quantity", 0) > avg_ask * self.WALL_MULTIPLIER]

        # FIX: Wall absorption — only flag if disappeared wall price is near LTP
        absorbed_bid_walls = []
        absorbed_ask_walls = []
        prox = self.ABSORPTION_PRICE_PROXIMITY
        if len(bid_hist) >= 2 and len(ask_hist) >= 2:
            curr_bid_prices = {b.get("price") for b in bids}
            curr_ask_prices = {a.get("price") for a in asks}

            prev_bid_walls = {p for p, q in bid_hist[-2]
                              if q > avg_bid * self.WALL_MULTIPLIER}
            absorbed_bid_walls = [
                p for p in prev_bid_walls
                if p not in curr_bid_prices and ltp and abs(p - ltp) <= prox
            ]

            prev_ask_walls = {p for p, q in ask_hist[-2]
                              if q > avg_ask * self.WALL_MULTIPLIER}
            absorbed_ask_walls = [
                p for p in prev_ask_walls
                if p not in curr_ask_prices and ltp and abs(p - ltp) <= prox
            ]

        # Signal — top5 imbalance weighted higher
        if top5_imbalance and top5_imbalance > 2.0:
            signal = "STRONG_BUY_PRESSURE"
        elif top5_imbalance and top5_imbalance < 0.5:
            signal = "STRONG_SELL_PRESSURE"
        elif imbalance and imbalance > 1.5:
            signal = "BUY_PRESSURE"
        elif imbalance and imbalance < 0.67:
            signal = "SELL_PRESSURE"
        elif absorbed_ask_walls:
            signal = "ASK_WALL_ABSORBED"
        elif absorbed_bid_walls:
            signal = "BID_WALL_ABSORBED"
        else:
            signal = "BALANCED"

        return {
            "bids"               : bids,
            "asks"               : asks,
            "top5_bids"          : bids[:5],
            "top5_asks"          : asks[:5],
            "depth_levels"       : len(bids),
            "total_bid_qty"      : total_bid_qty,
            "total_ask_qty"      : total_ask_qty,
            "top5_bid_qty"       : top5_bid,
            "top5_ask_qty"       : top5_ask,
            "imbalance"          : imbalance,
            "top5_imbalance"     : top5_imbalance,
            "bid_clusters"       : bid_clusters,
            "ask_clusters"       : ask_clusters,
            "bid_walls"          : bid_walls,
            "ask_walls"          : ask_walls,
            "absorbed_bid_walls" : absorbed_bid_walls,
            "absorbed_ask_walls" : absorbed_ask_walls,
            "signal"             : signal,
            "updated"            : updated.strftime("%H:%M:%S") if updated else None,
        }

    def reset(self):
        with self._lock:
            self._bids = []
            self._asks = []
            self._last_price = None
            self._updated = None
            self._bid_history.clear()
            self._ask_history.clear()


# ══════════════════════════════════════════════════════════════════════════════
# 4. OI + Volume Engine  (polled via kite.quote())
# ══════════════════════════════════════════════════════════════════════════════

class OIVolumeEngine:
    """
    Tracks Open Interest + Volume for F&O instruments.
    Polled every N seconds — WebSocket does not carry OI.

    FIX: Signal based on rolling 5-poll window (25s) instead of
    tick-to-tick to avoid false INDECISION on quiet windows.

    Signals:
      Rising OI  + Rising Volume  → FRESH_POSITIONS  (strong trend)
      Rising OI  + Falling Volume → WEAK_TREND
      Falling OI + Rising Volume  → UNWINDING
      Falling OI + Falling Volume → INDECISION
    """

    WINDOW = 5   # polls to compare (5 × 5s = 25s window)

    def __init__(self):
        self._lock    = threading.Lock()
        self._history : deque[dict] = deque(maxlen=200)

    def update(self, oi: float, volume: float, ltp: float) -> None:
        with self._lock:
            ts = datetime.now()
            self._history.append({
                "ts"    : ts.strftime("%H:%M:%S"),
                "oi"    : oi,
                "volume": volume,
                "ltp"   : ltp,
            })

    def snapshot(self) -> dict:
        with self._lock:
            hist = list(self._history)

        if len(hist) < 2:
            return {
                "signal": "WAITING", "history": hist,
                "oi": None, "volume": None,
                "oi_chg": None, "vol_chg": None, "description": "",
            }

        last = hist[-1]

        # FIX: if all OI values in history are 0, this is an equity-only symbol
        # with no F&O data (no fo_token supplied or OI genuinely 0).
        # Returning UNWINDING from volume-alone is incorrect — show NO_DATA.
        if all(h["oi"] == 0 for h in hist):
            return {
                "signal"     : "NO_DATA",
                "oi"         : 0,
                "volume"     : last["volume"],
                "ltp"        : last["ltp"],
                "oi_chg"     : 0,
                "vol_chg"    : None,
                "description": "No OI data — add F&O token to enable this panel",
                "history"    : hist[-100:],
            }

        # FIX: compare against WINDOW polls ago, not just prev tick
        ref_idx = max(0, len(hist) - 1 - self.WINDOW)
        ref     = hist[ref_idx]

        oi_chg  = last["oi"]     - ref["oi"]
        vol_chg = last["volume"] - ref["volume"]
        oi_up   = oi_chg  > 0
        vol_up  = vol_chg > 0

        # FIX: if current OI is 0, we can't trust the trend direction
        if last["oi"] == 0:
            return {
                "signal"     : "NO_DATA",
                "oi"         : 0,
                "volume"     : last["volume"],
                "ltp"        : last["ltp"],
                "oi_chg"     : 0,
                "vol_chg"    : round(vol_chg, 0),
                "description": "No OI data — add F&O token to enable this panel",
                "history"    : hist[-100:],
            }

        if oi_up and vol_up:
            signal      = "FRESH_POSITIONS"
            description = "Rising OI + Rising Volume → strong trend"
        elif oi_up and not vol_up:
            signal      = "WEAK_TREND"
            description = "Rising OI + Falling Volume → weakening"
        elif not oi_up and vol_up:
            signal      = "UNWINDING"
            description = "Falling OI + Rising Volume → short cover / unwinding"
        else:
            signal      = "INDECISION"
            description = "Falling OI + Falling Volume → low conviction"

        return {
            "oi"         : last["oi"],
            "volume"     : last["volume"],
            "ltp"        : last["ltp"],
            "oi_chg"     : round(oi_chg, 0),
            "vol_chg"    : round(vol_chg, 0),
            "signal"     : signal,
            "description": description,
            "history"    : hist[-100:],
        }

    def reset(self):
        with self._lock:
            self._history.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Unified per-symbol profiler
# ══════════════════════════════════════════════════════════════════════════════

class SymbolProfiler:
    """One instance per tracked symbol. Combines all 4 engines."""

    def __init__(self, symbol: str, token: int, exchange: str = "NSE", fo_token: int | None = None):
        self.symbol   = symbol
        self.token    = token
        self.exchange = exchange
        self.fo_token = fo_token

        self.cvd    = CVDEngine()
        self.vwap   = VWAPEngine()
        self.book   = OrderBookEngine()
        self.oi_vol = OIVolumeEngine()

    def process_tick(self, tick: dict) -> None:
        price = tick.get("last_price", 0)
        qty   = tick.get("last_quantity") or tick.get("last_traded_quantity") or 0
        ts    = tick.get("timestamp") or datetime.now()

        if price and qty:
            self.cvd.process_tick(price, qty, ts)
            self.vwap.process_tick(price, qty, ts)

        self.book.process_tick(tick)

    def update_oi(self, quote: dict) -> None:
        oi  = quote.get("oi", 0) or 0
        vol = quote.get("volume", 0) or 0
        ltp = quote.get("last_price", 0) or 0
        # FIX: only record when OI > 0; recording vol-only ticks (oi=0) causes
        # false UNWINDING because oi_chg stays at 0 while vol_chg grows.
        # Volume-only is stored regardless to show it in the UI.
        self.oi_vol.update(oi, vol, ltp)

    def full_snapshot(self) -> dict:
        # FIX: take snapshots once, pass to _overall_signal to avoid double lock
        cvd_snap  = self.cvd.snapshot()
        vwap_snap = self.vwap.snapshot()
        book_snap = self.book.snapshot()
        oi_snap   = self.oi_vol.snapshot()
        overall   = self._overall_signal(cvd_snap, vwap_snap, book_snap, oi_snap)

        return {
            "symbol"    : self.symbol,
            "token"     : self.token,
            "exchange"  : self.exchange,
            "fo_token"  : self.fo_token,
            "last_price": cvd_snap.get("last_price"),
            "cvd"       : cvd_snap,
            "vwap"      : vwap_snap,
            "order_book": book_snap,
            "oi_volume" : oi_snap,
            "overall"   : overall,
        }

    def _overall_signal(self, cvd_s, vwap_s, book_s, oi_s) -> str:
        """
        Composite majority-vote across all 4 panels.
        FIX: all new book signals now scored correctly.
        """
        bull = 0
        bear = 0

        # CVD
        if cvd_s["bias"] == "BULLISH":  bull += 1
        elif cvd_s["bias"] == "BEARISH": bear += 1

        # VWAP
        if vwap_s["signal"] in ("BULLISH", "ABOVE_VWAP"):    bull += 1
        elif vwap_s["signal"] in ("BEARISH", "BELOW_VWAP"):  bear += 1

        # Order Book — FIX: all signals mapped
        book_sig = book_s["signal"]
        if book_sig in ("STRONG_BUY_PRESSURE", "BUY_PRESSURE", "ASK_WALL_ABSORBED"):
            bull += 1
        elif book_sig in ("STRONG_SELL_PRESSURE", "SELL_PRESSURE", "BID_WALL_ABSORBED"):
            bear += 1

        # OI + Volume — direction depends on CVD bias
        oi_sig = oi_s["signal"]
        if oi_sig in ("FRESH_POSITIONS", "UNWINDING"):
            if cvd_s["bias"] == "BULLISH":  bull += 1
            else:                            bear += 1

        if bull >= 3:   return "STRONG_BULL"
        if bear >= 3:   return "STRONG_BEAR"
        if bull > bear: return "MILD_BULL"
        if bear > bull: return "MILD_BEAR"
        return "NEUTRAL"

    def reset(self):
        # FIX: OrderBookEngine now also reset
        self.cvd.reset()
        self.vwap.reset()
        self.book.reset()
        self.oi_vol.reset()


# ══════════════════════════════════════════════════════════════════════════════
# Global Registry
# ══════════════════════════════════════════════════════════════════════════════

class MarketRegistry:
    def __init__(self):
        self._profilers : dict[int, SymbolProfiler] = {}
        self._by_name   : dict[str, SymbolProfiler] = {}
        self._lock       = threading.Lock()

    def add(self, symbol: str, token: int, exchange: str = "NSE", fo_token: int | None = None) -> SymbolProfiler:
        with self._lock:
            if token not in self._profilers:
                p = SymbolProfiler(symbol, token, exchange, fo_token)
                self._profilers[token]        = p
                self._by_name[symbol.upper()] = p
            return self._profilers[token]

    def remove(self, token: int):
        with self._lock:
            p = self._profilers.pop(token, None)
            if p:
                self._by_name.pop(p.symbol.upper(), None)

    def process_tick(self, tick: dict):
        token = tick.get("instrument_token")
        with self._lock:
            p = self._profilers.get(token)
        if p:
            p.process_tick(tick)

    def update_oi(self, token: int, quote: dict):
        with self._lock:
            p = self._profilers.get(token)
        if p:
            p.update_oi(quote)

    def get(self, symbol: str) -> SymbolProfiler | None:
        return self._by_name.get(symbol.upper())

    def all_snapshots(self) -> list[dict]:
        with self._lock:
            profilers = list(self._profilers.values())
        return [p.full_snapshot() for p in profilers]

    def list_symbols(self) -> list[dict]:
        with self._lock:
            return [{"symbol": p.symbol, "token": p.token, "exchange": p.exchange, "fo_token": p.fo_token}
                    for p in self._profilers.values()]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_divergence(history: list) -> str:
    if len(history) < 20:
        return "NONE"
    mid       = len(history) // 2
    price_chg = history[-1]["price"] - history[mid]["price"]
    cvd_chg   = history[-1]["cvd"]   - history[mid]["cvd"]
    if price_chg > 0 and cvd_chg < 0:
        return "BEARISH_DIVERGENCE"
    if price_chg < 0 and cvd_chg > 0:
        return "BULLISH_DIVERGENCE"
    return "NONE"
