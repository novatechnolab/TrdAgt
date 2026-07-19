"""
indicators.py — Canonical Technical Indicators (Python port of technical-indicators.js TI.*)
Algorithms are identical to the JS source to guarantee parity.
All functions operate on plain Python lists, not Pandas Series.
"""
import math
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# ── EMA ───────────────────────────────────────────────────────────────────────

def compute_ema(data, period):
    """Port of TI.computeEMA — same short-series first-value seed fallback."""
    if not data or period <= 0:
        return []
    k = 2 / (period + 1)
    if len(data) < period:
        # JS fallback: seed from first value
        ema = data[0]
        result = [ema]
        for i in range(1, len(data)):
            ema = data[i] * k + ema * (1 - k)
            result.append(ema)
        return result
    result = [None] * (period - 1)
    ema = sum(data[:period]) / period
    result.append(ema)
    for i in range(period, len(data)):
        ema = data[i] * k + ema * (1 - k)
        result.append(ema)
    return result

def ema_last(data, period, fallback=0):
    """Port of TI.emaLast."""
    arr = compute_ema(data, period)
    return arr[-1] if arr else fallback

# ── RSI ───────────────────────────────────────────────────────────────────────

def compute_rsi_array(closes, period=14):
    """Port of TI.computeRSIArray — Wilder smoothing, fallback 50."""
    if not closes or len(closes) < period + 1:
        return [50.0] * (len(closes) if closes else 0)
    result = [None] * period
    gain_sum = loss_sum = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gain_sum += d
        else:
            loss_sum -= d
    avg_gain = gain_sum / period
    avg_loss = loss_sum / period

    def calc(g, l):
        if l == 0:
            return 100.0
        return 100 - 100 / (1 + g / l)

    result.append(calc(avg_gain, avg_loss))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = d if d > 0 else 0
        loss = -d if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result.append(calc(avg_gain, avg_loss))
    return result

def compute_rsi(closes, period=14):
    """Port of TI.computeRSI — returns last value or 50."""
    arr = compute_rsi_array(closes, period)
    return arr[-1] if arr else 50.0

# ── MACD ──────────────────────────────────────────────────────────────────────

def compute_macd_array(closes):
    """Port of TI.computeMACDArray."""
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    if not ema12 or not ema26:
        return {'macdLine': [], 'signalLine': [], 'histogram': []}
    macd_line = []
    valid_macd = []
    for i in range(len(ema26)):
        if ema12[i] is None or ema26[i] is None:
            macd_line.append(None)
        else:
            val = ema12[i] - ema26[i]
            macd_line.append(val)
            valid_macd.append(val)
    valid_signal = compute_ema(valid_macd, 9)
    pad = len(ema26) - len(valid_macd)
    signal_line = [None] * pad + valid_signal
    histogram = [
        (macd_line[i] - signal_line[i])
        if macd_line[i] is not None and signal_line[i] is not None else None
        for i in range(len(macd_line))
    ]
    return {'macdLine': macd_line, 'signalLine': signal_line, 'histogram': histogram}

def compute_macd(closes):
    """Port of TI.computeMACD — returns {macd, signal, histogram} scalars."""
    arr = compute_macd_array(closes)
    return {
        'macd':      arr['macdLine'][-1] or 0 if arr['macdLine'] else 0,
        'signal':    arr['signalLine'][-1] or 0 if arr['signalLine'] else 0,
        'histogram': arr['histogram'][-1] or 0 if arr['histogram'] else 0,
    }

# ── ADX ───────────────────────────────────────────────────────────────────────

def compute_adx(highs, lows, closes, period=14):
    """Port of TI.computeADX — Wilder, fallback 20."""
    n = min(len(highs), len(lows), len(closes))
    if n < period * 2 + 1:
        return 20.0
    highs, lows, closes = highs[:n], lows[:n], closes[:n]
    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(highs)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i]  - closes[i - 1])))
    if len(tr) < period:
        return 20.0
    tr_n     = sum(tr[:period])
    plus_n   = sum(plus_dm[:period])
    minus_n  = sum(minus_dm[:period])
    dx_series = []
    p_di = (plus_n / tr_n * 100) if tr_n > 0 else 0
    m_di = (minus_n / tr_n * 100) if tr_n > 0 else 0
    den  = p_di + m_di
    dx_series.append((abs(p_di - m_di) / den * 100) if den > 0 else 0)
    for i in range(period, len(tr)):
        tr_n    = tr_n    - tr_n    / period + tr[i]
        plus_n  = plus_n  - plus_n  / period + plus_dm[i]
        minus_n = minus_n - minus_n / period + minus_dm[i]
        p_di = (plus_n / tr_n * 100) if tr_n > 0 else 0
        m_di = (minus_n / tr_n * 100) if tr_n > 0 else 0
        den  = p_di + m_di
        dx_series.append((abs(p_di - m_di) / den * 100) if den > 0 else 0)
    if len(dx_series) < period:
        return dx_series[-1] if dx_series else 20.0
    adx = sum(dx_series[:period]) / period
    for i in range(period, len(dx_series)):
        adx = (adx * (period - 1) + dx_series[i]) / period
    return adx

# ── ATR ───────────────────────────────────────────────────────────────────────

def compute_atr(highs, lows, closes, period=14):
    """Port of TI.computeATR — Wilder, fallback 0."""
    if not highs or len(highs) < period + 1:
        return 0.0
    tr = [max(highs[i] - lows[i],
              abs(highs[i] - closes[i - 1]),
              abs(lows[i]  - closes[i - 1]))
          for i in range(1, len(highs))]
    if len(tr) < period:
        return sum(tr) / max(1, len(tr))
    atr = sum(tr[:period]) / period
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr

# ── Bollinger ─────────────────────────────────────────────────────────────────

def compute_bollinger_width(closes, period=20):
    if not closes or len(closes) < period:
        return 0.0
    sl = closes[-period:]
    mean = sum(sl) / period
    std  = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
    return std / mean * 100 if mean else 0.0

def compute_bollinger_bands(closes, period=20, multiplier=2):
    result = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
            continue
        sl   = closes[i - period + 1: i + 1]
        mean = sum(sl) / period
        std  = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
        result.append({'mid': mean, 'upper': mean + multiplier * std,
                       'lower': mean - multiplier * std})
    return result

# ── VWAP ──────────────────────────────────────────────────────────────────────

def compute_vwap(ohlcv):
    """Port of TI.computeVWAP — cumulative, no reset."""
    cum_tpv = cum_vol = 0.0
    result = []
    for d in ohlcv:
        tp = (d['high'] + d['low'] + d['close']) / 3
        cum_tpv += tp * d['volume']
        cum_vol += d['volume']
        result.append(cum_tpv / cum_vol if cum_vol > 0 else tp)
    return result

def compute_intraday_vwap(ohlcv):
    """Port of TI.computeIntradayVWAP — resets at each new trading day."""
    cum_tpv = cum_vol = 0.0
    prev_date = ''
    result = []
    for d in ohlcv:
        date_str = extract_date(d['date'])
        if date_str and date_str != prev_date:
            cum_tpv = cum_vol = 0.0
            prev_date = date_str
        tp = (d['high'] + d['low'] + d['close']) / 3
        cum_tpv += tp * d['volume']
        cum_vol += d['volume']
        result.append(cum_tpv / cum_vol if cum_vol > 0 else tp)
    return result

# ── Volume Ratio ──────────────────────────────────────────────────────────────

def compute_volume_ratio(volumes, period=20):
    """Port of TI.computeVolumeRatio."""
    if not volumes or len(volumes) < 2:
        return 1.0
    eff = min(period, len(volumes) - 1)
    avg = sum(volumes[-(eff + 1):-1]) / eff
    return volumes[-1] / avg if avg > 0 else 1.0

# ── Date Helpers ──────────────────────────────────────────────────────────────

def extract_date(date):
    """Port of TI._extractDate — returns YYYY-MM-DD string."""
    if not date:
        return ''
    if isinstance(date, str):
        return date[:10]
    if isinstance(date, datetime):
        return date.strftime('%Y-%m-%d')
    return str(date)[:10]

def parse_iso_to_ist(date_str):
    """
    Port of TI._parseIsoTimestampToIst.
    Returns {'date': 'YYYY-MM-DD', 'time': 'HH:MM'} in IST, or None.
    """
    if not date_str:
        return None
    if isinstance(date_str, str):
        # Try offset-aware ISO: 2026-04-15T09:15:00+05:30
        import re
        m = re.match(
            r'^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?'
            r'([Zz]|[+-]\d{2}:\d{2})?$',
            date_str
        )
        if m:
            date_part = m.group(1)
            hh, mm   = m.group(2), m.group(3)
            tz_part  = m.group(4)
            if not tz_part:
                # Naive → assume IST (matches JS behaviour)
                return {'date': date_part, 'time': f'{hh}:{mm}'}
            # Convert to IST
            try:
                dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                dt_ist = dt.astimezone(IST)
                return {
                    'date': dt_ist.strftime('%Y-%m-%d'),
                    'time': dt_ist.strftime('%H:%M'),
                }
            except Exception:
                return {'date': date_part, 'time': f'{hh}:{mm}'}
    if isinstance(date_str, datetime):
        dt_ist = date_str.astimezone(IST) if date_str.tzinfo else date_str
        return {'date': dt_ist.strftime('%Y-%m-%d'), 'time': dt_ist.strftime('%H:%M')}
    return None

# ── Session Filters ───────────────────────────────────────────────────────────

def filter_today_session(ohlcv, min_candles=20):
    """Port of TI.filterTodaySession."""
    if not ohlcv:
        return ohlcv
    last_date = extract_date(ohlcv[-1]['date'])
    if not last_date:
        return ohlcv
    today = [d for d in ohlcv if extract_date(d['date']) == last_date]
    return today if len(today) >= min_candles else ohlcv

def filter_session_by_date(ohlcv, date_str, min_candles=20):
    """Port of TI.filterSessionByDate."""
    if not ohlcv or not date_str:
        return ohlcv
    target = date_str[:10]
    session = [d for d in ohlcv if extract_date(d['date']) == target]
    return session if len(session) >= min_candles else ohlcv

def find_prev_day_close(ohlcv):
    """Port of TI.findPrevDayClose."""
    if not ohlcv or len(ohlcv) < 2:
        return None
    last_date = extract_date(ohlcv[-1]['date'])
    for d in reversed(ohlcv):
        dt = extract_date(d['date'])
        if dt and dt != last_date:
            return d['close']
    return None

# ── Truncation ────────────────────────────────────────────────────────────────

def truncate_at_entry_price(ohlcv, entry_price, direction, target_date=None, entry_time=None):
    """Port of TI.truncateAtEntryPrice — same 3-priority logic."""
    if not ohlcv:
        return ohlcv
    if entry_time and not target_date:
        target_date = extract_date(ohlcv[-1]['date'])

    search_from, search_to = 0, len(ohlcv)
    if target_date:
        target = target_date[:10]
        date_start = date_end = -1
        for i, d in enumerate(ohlcv):
            if extract_date(d['date']) == target:
                if date_start == -1:
                    date_start = i
                date_end = i
        if date_start >= 0:
            search_from = date_start
            search_to   = date_end + 1

    # Priority 1: time-based
    if entry_time and target_date:
        target = target_date[:10]
        parts = entry_time.split(':')
        if len(parts) == 2:
            try:
                hh, mm = int(parts[0]), int(parts[1])
                entry_minutes = hh * 60 + mm
                best_idx = latest_le_idx = -1
                best_diff = float('inf')
                for i in range(search_from, search_to):
                    parsed = parse_iso_to_ist(ohlcv[i]['date'])
                    if not parsed or parsed['date'] != target:
                        continue
                    t = parsed['time']
                    c_min = int(t[:2]) * 60 + int(t[3:])
                    if c_min <= entry_minutes:
                        latest_le_idx = i
                    diff = abs(c_min - entry_minutes)
                    if diff < best_diff or (diff == best_diff and c_min <= entry_minutes):
                        best_diff = diff
                        best_idx  = i
                chosen = best_idx if (best_idx >= 0 and best_diff <= 5) else latest_le_idx
                if chosen >= 0:
                    return ohlcv[search_from: chosen + 1]
            except (ValueError, IndexError):
                pass

    # Priority 2: price-based
    if not entry_price or entry_price <= 0:
        return ohlcv
    for i in range(search_from, search_to):
        if ohlcv[i]['low'] <= entry_price <= ohlcv[i]['high']:
            return ohlcv[search_from: i + 1]
    for i in range(search_from, search_to):
        if direction == 'CALL' and ohlcv[i]['close'] >= entry_price:
            return ohlcv[search_from: i + 1]
        if direction == 'PUT'  and ohlcv[i]['close'] <= entry_price:
            return ohlcv[search_from: i + 1]
    closest_idx  = search_to - 1
    closest_dist = float('inf')
    for i in range(search_from, search_to):
        dist = abs(ohlcv[i]['close'] - entry_price)
        if dist < closest_dist:
            closest_dist = dist
            closest_idx  = i
    return ohlcv[search_from: closest_idx + 1]

# ── Supertrend ────────────────────────────────────────────────────────────────

def compute_supertrend(ohlcv, period=10, multiplier=3):
    """Port of TI.computeSupertrend."""
    closes = [d['close'] for d in ohlcv]
    highs  = [d['high']  for d in ohlcv]
    lows   = [d['low']   for d in ohlcv]
    atr = []
    for i in range(len(ohlcv)):
        if i == 0:
            atr.append(highs[i] - lows[i])
        else:
            tr = max(highs[i] - lows[i],
                     abs(highs[i] - closes[i-1]),
                     abs(lows[i]  - closes[i-1]))
            atr.append(tr if i < period else (atr[-1] * (period - 1) + tr) / period)
    result  = []
    prev_dir = 1
    for i in range(len(ohlcv)):
        mid = (highs[i] + lows[i]) / 2
        up  = mid + multiplier * atr[i]
        dn  = mid - multiplier * atr[i]
        if i == 0:
            result.append({'value': up, 'dir': 1, 'up': up, 'dn': dn})
            prev_dir = 1
            continue
        prev_up = result[i-1]['up']
        prev_dn = result[i-1]['dn']
        final_up = up if (up < prev_up or closes[i-1] > prev_up) else prev_up
        final_dn = dn if (dn > prev_dn or closes[i-1] < prev_dn) else prev_dn
        if prev_dir == 1:
            cur_dir = -1 if closes[i] < final_dn else 1
        else:
            cur_dir =  1 if closes[i] > final_up else -1
        value = final_dn if cur_dir == 1 else final_up
        result.append({'value': value, 'dir': cur_dir, 'up': final_up, 'dn': final_dn})
        prev_dir = cur_dir
    return result

# ── SMC Primitives ────────────────────────────────────────────────────────────

def compute_pivots(highs, lows, length=5):
    """Port of TI.computePivots."""
    n = len(highs)
    pivots = [None] * n
    for i in range(length, n - length):
        is_ph = all(highs[i-j] <= highs[i] and highs[i+j] < highs[i] for j in range(1, length+1))
        is_pl = all(lows[i-j]  >= lows[i]  and lows[i+j]  > lows[i]  for j in range(1, length+1))
        if is_ph:
            pivots[i] = {'type': 'PH', 'index': i, 'price': highs[i]}
        elif is_pl:
            pivots[i] = {'type': 'PL', 'index': i, 'price': lows[i]}
    return pivots

def compute_fvg(highs, lows):
    """Port of TI.computeFVG."""
    n = len(highs)
    fvgs = [None] * n
    for i in range(2, n):
        if lows[i] > highs[i-2]:
            fvgs[i] = {'type': 1,  'top': lows[i],    'bottom': highs[i-2], 'isMitigated': False, 'bar': i}
        elif highs[i] < lows[i-2]:
            fvgs[i] = {'type': -1, 'top': lows[i-2], 'bottom': highs[i],   'isMitigated': False, 'bar': i}
    return fvgs

def compute_smc(ohlcv, pivot_len=5):
    """Port of TI.computeSMC."""
    if not ohlcv or len(ohlcv) < pivot_len * 2:
        return {'markers': [], 'fvgs': [], 'srLines': []}
    highs  = [d['high']  for d in ohlcv]
    lows   = [d['low']   for d in ohlcv]
    closes = [d['close'] for d in ohlcv]
    pivots  = compute_pivots(highs, lows, pivot_len)
    raw_fvg = compute_fvg(highs, lows)
    markers, sr_lines, fvgs = [], [], []
    for i, gap in enumerate(raw_fvg):
        if not gap:
            continue
        mitigated = any(
            (gap['type'] == 1  and lows[j]  < gap['bottom']) or
            (gap['type'] == -1 and highs[j] > gap['top'])
            for j in range(i+1, len(ohlcv))
        )
        if not mitigated:
            fvgs.append({'type': gap['type'], 'top': gap['top'],
                         'bottom': gap['bottom'], 'startIndex': i,
                         'startTime': ohlcv[i]['date']})
    last_ph = last_pl = None
    for i, p in enumerate(pivots):
        if not p:
            continue
        t = ohlcv[i]['date']
        if p['type'] == 'PH':
            if last_ph is not None:
                markers.append({'time': t, 'position': 'aboveBar', 'shape': 'text',
                                'color': '#1E88E5' if p['price'] > last_ph['price'] else '#E53935',
                                'text': 'HH' if p['price'] > last_ph['price'] else 'LH'})
            last_ph = p
            sr_lines.append({'price': p['price'], 'type': 'RESISTANCE'})
        elif p['type'] == 'PL':
            if last_pl is not None:
                markers.append({'time': t, 'position': 'belowBar', 'shape': 'text',
                                'color': '#1E88E5' if p['price'] > last_pl['price'] else '#E53935',
                                'text': 'HL' if p['price'] > last_pl['price'] else 'LL'})
            last_pl = p
            sr_lines.append({'price': p['price'], 'type': 'SUPPORT'})
    cur_trend = 0
    for i in range(pivot_len * 2, len(ohlcv)):
        if pivots[i]:
            continue
        r_ph = r_pl = None
        for j in range(i-1, -1, -1):
            if pivots[j]:
                if pivots[j]['type'] == 'PH' and r_ph is None: r_ph = pivots[j]['price']
                if pivots[j]['type'] == 'PL' and r_pl is None: r_pl = pivots[j]['price']
            if r_ph is not None and r_pl is not None:
                break
        if r_ph is None or r_pl is None:
            continue
        c, t = closes[i], ohlcv[i]['date']
        if cur_trend <= 0 and c > r_ph:
            markers.append({'time': t, 'position': 'aboveBar', 'shape': 'arrowUp',
                            'color': '#26A69A', 'text': 'CHOCH' if cur_trend == -1 else 'BOS'})
            cur_trend = 1
            pivots[i] = {'type': 'BREAK'}
        elif cur_trend >= 0 and c < r_pl:
            markers.append({'time': t, 'position': 'belowBar', 'shape': 'arrowDown',
                            'color': '#EF5350', 'text': 'CHOCH' if cur_trend == 1 else 'BOS'})
            cur_trend = -1
            pivots[i] = {'type': 'BREAK'}
    return {'markers': markers, 'fvgs': fvgs, 'srLines': sr_lines}


# ─────────────────────────────────────────────────────────────────────────────
# HTF SMC Bias — daily-timeframe structure overlay
# Called ONCE on daily candles to set session bias context.
# NOT added to signal score totals — surfaced as informational overlay only.
# Rationale: intraday SMC (5-min) is too noisy and lags entries for options.
#            Daily SMC gives the "higher timeframe story" without polluting score.
# ─────────────────────────────────────────────────────────────────────────────

def compute_smc_bias(daily_ohlcv: list, current_ltp: float = 0.0,
                     current_atr: float = 0.0) -> dict:
    """Compute HTF (daily) SMC session bias from daily OHLCV candles.

    Designed to be called on DAILY candles only.  Uses last 60 bars
    (≈ 3 months) so structure is well-formed before scanning.

    Args:
        daily_ohlcv:  List of daily OHLCV dicts with 'date','open','high',
                      'low','close','volume' keys.
        current_ltp:  Current LTP (for FVG proximity check).
        current_atr:  Current ATR (for FVG proximity threshold).

    Returns:
        {
          'bias':               'bullish'|'bearish'|'neutral',
          'bosDirection':       'bullish'|'bearish'|None,
          'chochDirection':     'bullish'|'bearish'|None,
          'structure':          'HH_HL'|'LH_LL'|'HH_LL'|'LH_HL'|'forming',
          'nearestFvgSupport':  float|None,   # top of nearest bullish FVG below LTP
          'nearestFvgResistance': float|None, # bottom of nearest bearish FVG above LTP
          'activeFvgs':         list,         # all unmitigated FVGs [{type,top,bottom}]
          'confidence':         'high'|'medium'|'low',
          'reasons':            list[str],
        }
    """
    _empty = {
        'bias': 'neutral', 'bosDirection': None, 'chochDirection': None,
        'structure': 'forming', 'nearestFvgSupport': None,
        'nearestFvgResistance': None, 'activeFvgs': [],
        'confidence': 'low', 'reasons': ['Insufficient daily data for HTF SMC'],
    }
    # Need at least 20 daily bars for meaningful structure (pivot_len=5 × 4 swings)
    if not daily_ohlcv or len(daily_ohlcv) < 20:
        return _empty

    candles = daily_ohlcv[-60:]          # cap at 60 bars (~3 months)
    smc     = compute_smc(candles)       # full SMC computation on daily candles
    markers = smc.get('markers', [])
    fvgs    = smc.get('fvgs', [])

    reasons = []

    # ── Extract last BOS and CHoCH direction ──────────────────────────────────
    bos_dir   = None
    choch_dir = None
    for m in reversed(markers):
        txt = m.get('text', '')
        if txt == 'BOS' and bos_dir is None:
            # arrow color: #26A69A = bullish BOS, #EF5350 = bearish BOS
            bos_dir = 'bullish' if m.get('color') == '#26A69A' else 'bearish'
        if txt == 'CHOCH' and choch_dir is None:
            choch_dir = 'bullish' if m.get('color') == '#26A69A' else 'bearish'
        if bos_dir and choch_dir:
            break

    # ── Extract recent market structure (last 4 pivot labels) ─────────────────
    struct_labels = [m['text'] for m in markers if m.get('text') in ('HH','HL','LH','LL')]
    recent = struct_labels[-4:] if len(struct_labels) >= 4 else struct_labels
    hh_count = recent.count('HH')
    hl_count = recent.count('HL')
    lh_count = recent.count('LH')
    ll_count = recent.count('LL')

    if hh_count >= 1 and hl_count >= 1 and lh_count == 0 and ll_count == 0:
        structure = 'HH_HL'
    elif lh_count >= 1 and ll_count >= 1 and hh_count == 0 and hl_count == 0:
        structure = 'LH_LL'
    elif hh_count >= 1 and ll_count >= 1:
        structure = 'HH_LL'   # mixed / transitioning
    elif lh_count >= 1 and hl_count >= 1:
        structure = 'LH_HL'   # mixed / transitioning
    else:
        structure = 'forming'

    # ── Overall bias from CHoCH > BOS > structure ─────────────────────────────
    # CHoCH is the strongest reversal signal on daily; BOS = continuation
    if choch_dir:
        bias = choch_dir
        reasons.append(f'Daily CHoCH → {choch_dir} reversal on HTF')
    elif bos_dir:
        bias = bos_dir
        reasons.append(f'Daily BOS → {bos_dir} continuation on HTF')
    elif structure == 'HH_HL':
        bias = 'bullish'
        reasons.append('Daily structure: HH + HL (bullish market structure)')
    elif structure == 'LH_LL':
        bias = 'bearish'
        reasons.append('Daily structure: LH + LL (bearish market structure)')
    else:
        bias = 'neutral'
        reasons.append(f'Daily structure mixed or forming ({structure})')

    if structure not in ('forming',):
        reasons.append(f'Recent pivot labels: {recent}')

    # ── FVG zones — find nearest support/resistance relative to LTP ───────────
    nearest_support    = None   # top of highest bullish FVG below LTP
    nearest_resistance = None   # bottom of lowest bearish FVG above LTP
    prox_thresh        = current_atr * 2.0 if current_atr > 0 else float('inf')

    for fvg in fvgs:
        fvg_type   = fvg.get('type', 0)
        fvg_top    = fvg.get('top', 0)
        fvg_bottom = fvg.get('bottom', 0)
        if fvg_type == 1:   # bullish FVG (gap up) — acts as support
            if current_ltp > 0 and fvg_top < current_ltp:
                if nearest_support is None or fvg_top > nearest_support:
                    nearest_support = round(fvg_top, 2)
        elif fvg_type == -1:  # bearish FVG (gap down) — acts as resistance
            if current_ltp > 0 and fvg_bottom > current_ltp:
                if nearest_resistance is None or fvg_bottom < nearest_resistance:
                    nearest_resistance = round(fvg_bottom, 2)

    if nearest_support:
        reasons.append(f'Nearest daily FVG support: {nearest_support}')
    if nearest_resistance:
        reasons.append(f'Nearest daily FVG resistance: {nearest_resistance}')

    # ── Confidence ─────────────────────────────────────────────────────────────
    has_choch     = choch_dir is not None
    has_bos       = bos_dir is not None
    clean_struct  = structure in ('HH_HL', 'LH_LL')
    confidence = (
        'high'   if (has_choch and clean_struct) or (has_bos and clean_struct and has_choch)
        else 'medium' if has_bos or clean_struct
        else 'low'
    )

    return {
        'bias':               bias,
        'bosDirection':       bos_dir,
        'chochDirection':     choch_dir,
        'structure':          structure,
        'nearestFvgSupport':  nearest_support,
        'nearestFvgResistance': nearest_resistance,
        'activeFvgs':         fvgs,
        'confidence':         confidence,
        'reasons':            reasons,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Risk management helper — centralises ATR-based SL/target/R:R calculation
# Used by entry_validator.py AND scoring_engine.py
# ─────────────────────────────────────────────────────────────────────────────

def compute_risk_levels(entry: float, atr: float, is_long: bool,
                        sl_mult: float = 1.5,
                        t1_mult: float = 2.0,
                        t2_mult: float = 3.0) -> dict:
    """Compute stop-loss, targets and R:R from ATR multipliers.

    Args:
        entry:    Entry price.
        atr:      ATR value.
        is_long:  True for CALL/BULLISH, False for PUT/BEARISH.
        sl_mult:  ATR multiple for stop-loss  (default 1.5).
        t1_mult:  ATR multiple for target 1   (default 2.0).
        t2_mult:  ATR multiple for target 2   (default 3.0).

    Returns:
        dict with keys: entry, stopLoss, target1, target2,
                        riskPerShare, riskReward, atrUsed
    """
    if is_long:
        sl = round(entry - sl_mult * atr, 2)
        t1 = round(entry + t1_mult * atr, 2)
        t2 = round(entry + t2_mult * atr, 2)
    else:
        sl = round(entry + sl_mult * atr, 2)
        t1 = round(entry - t1_mult * atr, 2)
        t2 = round(entry - t2_mult * atr, 2)

    risk   = abs(entry - sl)
    reward = abs(t1 - entry)
    return {
        'entry':       entry,
        'stopLoss':    sl,
        'target1':     t1,
        'target2':     t2,
        'riskPerShare': round(risk, 2),
        'riskReward':  round(reward / risk, 2) if risk > 0 else 0,
        'atrUsed':     round(atr, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Indicator Bundle — computes all standard indicators in ONE pass
# Pass the result to entry_validator AND scoring_engine to avoid double computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_indicator_bundle(ohlcv: list) -> dict:
    """Compute all standard indicators from OHLCV candle list once.

    Usage:
        bundle = compute_indicator_bundle(candles)
        ev_result = entry_validator.validate({..., **bundle})
        sc_result = scoring_engine.score_options({..., **bundle})

    Returns:
        dict with closes, highs, lows, volumes, ema9/21/50 arrays,
        plus scalar rsi, macd, adx, atr, vwap, volRatio.
    """
    closes  = [c['close']  for c in ohlcv]
    highs   = [c['high']   for c in ohlcv]
    lows    = [c['low']    for c in ohlcv]
    volumes = [c['volume'] for c in ohlcv]

    ema9  = compute_ema(closes, 9)
    ema21 = compute_ema(closes, 21)
    ema50 = compute_ema(closes, 50)

    return {
        # Raw series (needed by validators)
        'closes':  closes,
        'highs':   highs,
        'lows':    lows,
        'volumes': volumes,
        # EMA arrays
        'ema9arr':  ema9,
        'ema21arr': ema21,
        'ema50arr': ema50,
        # Scalar snapshots (last value, ready to use)
        'ema9':    ema9[-1]  if ema9  else 0.0,
        'ema21':   ema21[-1] if ema21 else 0.0,
        'ema50':   ema50[-1] if ema50 else 0.0,
        'rsi':     compute_rsi(closes),
        'macd':    compute_macd(closes),
        'adx':     compute_adx(highs, lows, closes),
        'atr':     compute_atr(highs, lows, closes),
        'vwapArr': (_va := compute_intraday_vwap(ohlcv) if ohlcv and 'date' in ohlcv[0] else compute_vwap(ohlcv)),
        'vwap':    _va[-1] if _va else 0.0,
        'volRatio': compute_volume_ratio(volumes),
        'technical_consensus': compute_technical_consensus(ohlcv),
    }


# ── Technical Consensus Indicators ───────────────────────────────────────────

def compute_stochastic(highs, lows, closes, k_period=9, d_period=6):
    """Compute Stochastic Oscillator %K and %D."""
    n = min(len(highs), len(lows), len(closes))
    if n < k_period:
        return {'k': [50.0] * n, 'd': [50.0] * n}
    k_values = [50.0] * (k_period - 1)
    for i in range(k_period - 1, n):
        highest_high = max(highs[i - k_period + 1 : i + 1])
        lowest_low = min(lows[i - k_period + 1 : i + 1])
        diff = highest_high - lowest_low
        k = 50.0 if diff == 0 else (closes[i] - lowest_low) / diff * 100
        k_values.append(k)
    d_values = [50.0] * (k_period - 1)
    for i in range(k_period - 1, n):
        if i < k_period - 1 + d_period - 1:
            d_values.append(50.0)
        else:
            d = sum(k_values[i - d_period + 1 : i + 1]) / d_period
            d_values.append(d)
    return {'k': k_values, 'd': d_values}

def compute_stochastic_rsi(closes, period=14):
    """Compute Stochastic RSI."""
    n = len(closes)
    if n < period * 2:
        return {'k': [50.0] * n, 'd': [50.0] * n}
    rsi_vals = compute_rsi_array(closes, period)
    stoch_rsi_k = [50.0] * (period * 2 - 1)
    for i in range(period * 2 - 1, n):
        window = rsi_vals[i - period + 1 : i + 1]
        valid_window = [x for x in window if x is not None]
        if not valid_window:
            k = 50.0
        else:
            highest_rsi = max(valid_window)
            lowest_rsi = min(valid_window)
            diff = highest_rsi - lowest_rsi
            curr_rsi = rsi_vals[i] if rsi_vals[i] is not None else 50.0
            k = 50.0 if diff == 0 else (curr_rsi - lowest_rsi) / diff * 100
        stoch_rsi_k.append(k)
    stoch_rsi_d = [50.0] * (period * 2 - 1)
    for i in range(period * 2 - 1, n):
        d = sum(stoch_rsi_k[i - 2 : i + 1]) / 3.0
        stoch_rsi_d.append(d)
    return {'k': stoch_rsi_k, 'd': stoch_rsi_d}

def compute_williams_r(highs, lows, closes, period=14):
    """Compute Williams %R."""
    n = min(len(highs), len(lows), len(closes))
    if n < period:
        return [-50.0] * n
    w_r = [-50.0] * (period - 1)
    for i in range(period - 1, n):
        highest_high = max(highs[i - period + 1 : i + 1])
        lowest_low = min(lows[i - period + 1 : i + 1])
        diff = highest_high - lowest_low
        val = -50.0 if diff == 0 else (highest_high - closes[i]) / diff * -100
        w_r.append(val)
    return w_r

def compute_cci(highs, lows, closes, period=14):
    """Compute Commodity Channel Index (CCI)."""
    n = min(len(highs), len(lows), len(closes))
    if n < period:
        return [0.0] * n
    typical_prices = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    tp_sma = [0.0] * (period - 1)
    for i in range(period - 1, n):
        tp_sma.append(sum(typical_prices[i - period + 1 : i + 1]) / period)
    cci = [0.0] * (period - 1)
    for i in range(period - 1, n):
        sma_val = tp_sma[i]
        sum_abs_diff = sum(abs(typical_prices[j] - sma_val) for j in range(i - period + 1, i + 1))
        mean_dev = sum_abs_diff / period
        val = 0.0 if mean_dev == 0 else (typical_prices[i] - sma_val) / (0.015 * mean_dev)
        cci.append(val)
    return cci

def compute_high_low_channels(highs, lows, period=14):
    """Compute Highs and Lows channels (highest high and lowest low of last 14)."""
    n = min(len(highs), len(lows))
    if n < period:
        return {'high': [0.0] * n, 'low': [0.0] * n}
    ch_high = [0.0] * (period - 1)
    ch_low = [0.0] * (period - 1)
    for i in range(period - 1, n):
        ch_high.append(max(highs[i - period + 1 : i + 1]))
        ch_low.append(min(lows[i - period + 1 : i + 1]))
    return {'high': ch_high, 'low': ch_low}

def compute_ultimate_oscillator(highs, lows, closes, p1=7, p2=14, p3=28):
    """Compute Ultimate Oscillator."""
    n = min(len(highs), len(lows), len(closes))
    if n < p3 + 1:
        return [50.0] * n
    bp = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        prev_close = closes[i - 1]
        bp[i] = closes[i] - min(lows[i], prev_close)
        tr[i] = max(highs[i], prev_close) - min(lows[i], prev_close)
    uo = [50.0] * p3
    for i in range(p3, n):
        sum_bp_7 = sum(bp[i - p1 + 1 : i + 1])
        sum_tr_7 = sum(tr[i - p1 + 1 : i + 1])
        avg7 = sum_bp_7 / sum_tr_7 if sum_tr_7 > 0 else 0.5
        sum_bp_14 = sum(bp[i - p2 + 1 : i + 1])
        sum_tr_14 = sum(tr[i - p2 + 1 : i + 1])
        avg14 = sum_bp_14 / sum_tr_14 if sum_tr_14 > 0 else 0.5
        sum_bp_28 = sum(bp[i - p3 + 1 : i + 1])
        sum_tr_28 = sum(tr[i - p3 + 1 : i + 1])
        avg28 = sum_bp_28 / sum_tr_28 if sum_tr_28 > 0 else 0.5
        val = 100.0 * (4.0 * avg7 + 2.0 * avg14 + avg28) / (4.0 + 2.0 + 1.0)
        uo.append(val)
    return uo

def compute_roc(closes, period=12):
    """Compute Rate of Change (ROC)."""
    n = len(closes)
    if n < period + 1:
        return [0.0] * n
    roc = [0.0] * period
    for i in range(period, n):
        prev = closes[i - period]
        val = 0.0 if prev == 0 else (closes[i] - prev) / prev * 100
        roc.append(val)
    return roc

def compute_elder_ray(highs, lows, ema_13_arr):
    """Compute Elder-Ray Bull Power and Bear Power."""
    n = min(len(highs), len(lows), len(ema_13_arr))
    bull_power = []
    bear_power = []
    for i in range(n):
        ema = ema_13_arr[i]
        if ema is None or ema == 0:
            bull_power.append(0.0)
            bear_power.append(0.0)
        else:
            bull_power.append(highs[i] - ema)
            bear_power.append(lows[i] - ema)
    return {'bull': bull_power, 'bear': bear_power}

def compute_sma(data, period):
    """Compute Simple Moving Average (SMA)."""
    if not data or period <= 0:
        return []
    if len(data) < period:
        return [sum(data) / len(data)] * len(data)
    result = [None] * (period - 1)
    for i in range(period - 1, len(data)):
        result.append(sum(data[i - period + 1 : i + 1]) / period)
    return result

def compute_technical_consensus(ohlcv: list) -> dict:
    """Compute consensus of standard oscillators and moving averages."""
    if not ohlcv or len(ohlcv) < 28:
        return {'consensus': 'NEUTRAL', 'bullish': 0, 'bearish': 0, 'neutral': 0}
        
    closes  = [c['close']  for c in ohlcv]
    highs   = [c['high']   for c in ohlcv]
    lows    = [c['low']    for c in ohlcv]
    last_close = closes[-1]
    
    # ── 1. Calculate All Required Oscillators & Indicators ──
    rsi = compute_rsi(closes)
    
    stoch = compute_stochastic(highs, lows, closes)
    stoch_k, stoch_d = stoch['k'][-1], stoch['d'][-1]
    
    stoch_rsi = compute_stochastic_rsi(closes)
    srsi_k = stoch_rsi['k'][-1]
    
    macd_arr = compute_macd_array(closes)
    macd_hist = macd_arr['histogram']
    
    adx_val = compute_adx(highs, lows, closes)
    plus_di = 0.0
    minus_di = 0.0
    if len(highs) >= 15:
        tr, plus_dm, minus_dm = [], [], []
        for i in range(1, len(highs)):
            up   = highs[i] - highs[i - 1]
            down = lows[i - 1] - lows[i]
            plus_dm.append(up if up > down and up > 0 else 0)
            minus_dm.append(down if down > up and down > 0 else 0)
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        if len(tr) >= 14:
            tr_n = sum(tr[-14:])
            plus_n = sum(plus_dm[-14:])
            minus_n = sum(minus_dm[-14:])
            plus_di = (plus_n / tr_n * 100) if tr_n > 0 else 0
            minus_di = (minus_n / tr_n * 100) if tr_n > 0 else 0
            
    w_r = compute_williams_r(highs, lows, closes)[-1]
    cci = compute_cci(highs, lows, closes)[-1]
    
    hl_ch = compute_high_low_channels(highs, lows)
    hl_high, hl_low = hl_ch['high'][-1], hl_ch['low'][-1]
    hl_mid = (hl_high + hl_low) / 2.0
    
    uo = compute_ultimate_oscillator(highs, lows, closes)[-1]
    roc_arr = compute_roc(closes)
    
    ema13_arr = compute_ema(closes, 13)
    elder = compute_elder_ray(highs, lows, ema13_arr)
    bull_arr = elder['bull']
    bear_arr = elder['bear']
    
    # ── 2. Voting logic for Oscillators ──
    bull_votes = 0
    bear_votes = 0
    neutral_votes = 0
    
    # RSI(14)
    if rsi < 30: bull_votes += 1
    elif rsi > 70: bear_votes += 1
    else: neutral_votes += 1
    
    # STOCH(9,6)
    if stoch_k < 20 and stoch_k > stoch_d: bull_votes += 1
    elif stoch_k > 80 and stoch_k < stoch_d: bear_votes += 1
    else: neutral_votes += 1
    
    # STOCHRSI(14)
    if srsi_k < 20: bull_votes += 1
    elif srsi_k > 80: bear_votes += 1
    else: neutral_votes += 1
    
    # MACD(12,26)
    if len(macd_hist) >= 2 and macd_hist[-1] is not None and macd_hist[-2] is not None:
        prev_hist = macd_hist[-2]
        curr_hist = macd_hist[-1]
        if prev_hist <= 0 < curr_hist: bull_votes += 1
        elif prev_hist >= 0 > curr_hist: bear_votes += 1
        else: neutral_votes += 1
    else:
        neutral_votes += 1
    
    # ADX(14)
    if adx_val > 20:
        if plus_di > minus_di: bull_votes += 1
        elif minus_di > plus_di: bear_votes += 1
        else: neutral_votes += 1
    else:
        neutral_votes += 1
        
    # Williams %R
    if w_r < -80: bull_votes += 1
    elif w_r > -20: bear_votes += 1
    else: neutral_votes += 1
    
    # CCI(14)
    if cci < -100: bull_votes += 1
    elif cci > 100: bear_votes += 1
    else: neutral_votes += 1
    
    # Highs/Lows(14)
    if last_close > hl_mid: bull_votes += 1
    elif last_close < hl_mid: bear_votes += 1
    else: neutral_votes += 1
    
    # Ultimate Oscillator
    if uo < 30: bull_votes += 1
    elif uo > 70: bear_votes += 1
    else: neutral_votes += 1
    
    # ROC
    if len(roc_arr) >= 2 and roc_arr[-1] is not None and roc_arr[-2] is not None:
        prev_roc = roc_arr[-2]
        curr_roc = roc_arr[-1]
        if prev_roc <= 0 < curr_roc: bull_votes += 1
        elif prev_roc >= 0 > curr_roc: bear_votes += 1
        else: neutral_votes += 1
    else:
        neutral_votes += 1
    
    # Elder Bull/Bear Power (13)
    if len(bull_arr) >= 2 and len(ema13_arr) >= 2 and ema13_arr[-1] is not None:
        curr_bull = bull_arr[-1]
        prev_bull = bull_arr[-2]
        curr_bear = bear_arr[-1]
        prev_bear = bear_arr[-2]
        curr_ema = ema13_arr[-1]
        if curr_bull > prev_bull and last_close > curr_ema:
            bull_votes += 1
        elif curr_bear < prev_bear and last_close < curr_ema:
            bear_votes += 1
        else:
            neutral_votes += 1
    else:
        neutral_votes += 1
    
    # ── 3. Calculate Moving Averages and Vote ──
    periods = [5, 10, 20, 50, 100, 200]
    for p in periods:
        # SMA
        sma_arr = compute_sma(closes, p)
        sma = sma_arr[-1] if sma_arr and sma_arr[-1] is not None else last_close
        if last_close > sma: bull_votes += 1
        elif last_close < sma: bear_votes += 1
        else: neutral_votes += 1
        
        # EMA
        ema_arr = compute_ema(closes, p)
        ema = ema_arr[-1] if ema_arr and ema_arr[-1] is not None else last_close
        if last_close > ema: bull_votes += 1
        elif last_close < ema: bear_votes += 1
        else: neutral_votes += 1
        
    if bull_votes > bear_votes:
        consensus = 'BULLISH'
    elif bear_votes > bull_votes:
        consensus = 'BEARISH'
    else:
        consensus = 'NEUTRAL'
        
    return {
        'consensus': consensus,
        'bullish': bull_votes,
        'bearish': bear_votes,
        'neutral': neutral_votes
    }



# ── SMC Indicators (for APEX Dashboard) ────────────────────────────────────────

def compute_swings(candles, lb=3):
    """Compute swing highs and lows for BOS/CHoCH detection."""
    if not candles or len(candles) < lb * 2:
        return []
    
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    
    swings = []
    for i in range(lb, len(candles) - lb):
        # Check for swing high
        is_high = all(highs[i] >= highs[j] for j in range(i - lb, i + lb + 1) if j != i)
        # Check for swing low
        is_low = all(lows[i] <= lows[j] for j in range(i - lb, i + lb + 1) if j != i)
        
        if is_high:
            swings.append({'type': 'high', 'price': highs[i], 'index': i})
        elif is_low:
            swings.append({'type': 'low', 'price': lows[i], 'index': i})
    
    return swings

def detect_bos(candles):
    """Detect Break of Structure (BOS)."""
    swings = compute_swings(candles)
    if len(swings) < 2:
        return 'NEUTRAL'
    
    # Get last two swings
    last_two = swings[-2:]
    if len(last_two) < 2:
        return 'NEUTRAL'
    
    prev, curr = last_two
    if prev['type'] == 'high' and curr['type'] == 'high':
        # Bullish BOS: higher high breaks previous high
        if curr['price'] > prev['price']:
            return 'BULLISH'
    elif prev['type'] == 'low' and curr['type'] == 'low':
        # Bearish BOS: lower low breaks previous low
        if curr['price'] < prev['price']:
            return 'BEARISH'
    
    return 'NEUTRAL'

def detect_choch(candles):
    """Detect Change of Character (CHoCH)."""
    swings = compute_swings(candles)
    if len(swings) < 3:
        return 'NEUTRAL'
    
    # Get last three swings
    last_three = swings[-3:]
    if len(last_three) < 3:
        return 'NEUTRAL'
    
    a, b, c = last_three
    if a['type'] == 'low' and b['type'] == 'high' and c['type'] == 'low':
        # Potential bullish CHoCH: lower low after higher high
        if c['price'] < a['price']:
            return 'BULLISH'
    elif a['type'] == 'high' and b['type'] == 'low' and c['type'] == 'high':
        # Potential bearish CHoCH: higher high after lower low
        if c['price'] > a['price']:
            return 'BEARISH'
    
    return 'NEUTRAL'

def htf_bias(close, ema21, ema50):
    """Determine HTF bias from EMA alignment."""
    if close > ema21 > ema50:
        return 'BULLISH'
    elif close < ema21 < ema50:
        return 'BEARISH'
    else:
        return 'NEUTRAL'

def vol_spike(candles, period=20):
    """Check for volume spike."""
    if not candles or len(candles) < period + 1:
        return False
    
    volumes = [c['volume'] for c in candles]
    current_vol = volumes[-1]
    avg_vol = sum(volumes[-period-1:-1]) / period
    
    return current_vol > avg_vol * 1.5

def compute_score(close, ema21, ema50, vwap, rsi, macd_hist, bos, choch, htf, vol_spike):
    """Compute signal score out of 12 points."""
    score = 0
    
    # EMA alignment (2 pts)
    if (close > ema21 > ema50) or (close < ema21 < ema50):
        score += 2
    
    # VWAP position (1 pt)
    if (close > vwap and close > ema21 > ema50) or (close < vwap and close < ema21 < ema50):
        score += 1
    
    # RSI zone (1 pt)
    if (rsi >= 55 and rsi <= 75 and close > ema21) or (rsi >= 25 and rsi <= 45 and close < ema21):
        score += 1
    
    # MACD (1-2 pts)
    if macd_hist:
        if macd_hist > 0:
            score += 1
        if macd_hist > 0 and close > ema21:
            score += 1
        elif macd_hist < 0 and close < ema21:
            score += 1
    
    # CHoCH (2 pts)
    if choch == 'BULLISH' and close > ema21:
        score += 2
    elif choch == 'BEARISH' and close < ema21:
        score += 2
    
    # BOS (1 pt)
    if bos == 'BULLISH' and close > ema21:
        score += 1
    elif bos == 'BEARISH' and close < ema21:
        score += 1
    
    # HTF bias (2 pts)
    if htf == 'BULLISH' and close > ema21:
        score += 2
    elif htf == 'BEARISH' and close < ema21:
        score += 2
    
    # Volume spike (1 pt)
    if vol_spike:
        score += 1
    
    return score


def check_ema9_respect(candles_5min, ema9_series, direction="bullish", 
                        max_body_penetration_pct=20, min_consecutive=2):
    """
    Checks if candles are respecting EMA9 as support/resistance.
    
    direction: "bullish" = price should hold above EMA9
               "bearish" = price should hold below EMA9
    max_body_penetration_pct: max % of candle body allowed to cross EMA9
    min_consecutive: how many recent candles must satisfy this
    """
    results = []

    for i in range(-min_consecutive, 0):
        candle = candles_5min[i]
        ema9 = ema9_series[i]
        o, c = candle["open"], candle["close"]
        body_top = max(o, c)
        body_bottom = min(o, c)
        body_size = body_top - body_bottom

        if body_size == 0:
            results.append(True)  # doji — neutral, don't penalize
            continue

        if direction == "bullish":
            # how much of the body sits below EMA9
            penetration = max(0, ema9 - body_bottom)
            penetration_pct = (penetration / body_size) * 100
            holds = penetration_pct <= max_body_penetration_pct
        else:
            # bearish: how much of the body sits above EMA9
            penetration = max(0, body_top - ema9)
            penetration_pct = (penetration / body_size) * 100
            holds = penetration_pct <= max_body_penetration_pct

        results.append(holds)

    confirmed = all(results)
    return {
        "state": "CONFIRMED" if confirmed else "NOT_CONFIRMED",
        "direction": direction,
        "bars_checked": min_consecutive,
        "detail": results
    }
