/**
 * TradeSignal — SMC Options Signal Dashboard
 *
 * Smart Money Concepts (SMC) confluence engine for NSE F&O options trading.
 * Computes market structure, BOS/CHoCH, premium/discount zones, FVG, order blocks,
 * momentum indicators, and auto-fires signal tags when bias or structure changes.
 *
 * Depends on: TI (technical-indicators.js), scoringEngine, chartSignalEngine,
 *             equityScreener, kiteAPI
 */
class SmcDashboard {
  constructor() {
    this._symbol   = 'NIFTY 50';
    this._interval = '5minute';
    this._tags     = [];
    this._pollTimer = null;
    this._prevState = null;
    this._initialized = false;
    this.POLL_MS   = 30000;
  }

  // ── Market session helpers ──────────────────────────────────────────────────

  /** Returns true Mon-Fri 9:15 AM – 3:30 PM IST */
  isLiveSession() {
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);
    const day = ist.getDay();
    if (day === 0 || day === 6) return false;
    const t = ist.getHours() * 60 + ist.getMinutes();
    return t >= 555 && t < 940; // 9:15 = 555, 15:40 = 940
  }

  /** Returns IST date as YYYY-MM-DD */
  _istToday() {
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);
    const y = ist.getFullYear();
    const m = String(ist.getMonth() + 1).padStart(2, '0');
    const d = String(ist.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
  }

  /** Walks back n trading days (Mon-Fri) from today */
  _tradingSessionsAgo(n) {
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);
    let count = 0;
    while (count < n) {
      ist.setDate(ist.getDate() - 1);
      const day = ist.getDay();
      if (day !== 0 && day !== 6) count++;
    }
    const y = ist.getFullYear();
    const m = String(ist.getMonth() + 1).padStart(2, '0');
    const d = String(ist.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
  }

  // ── Init (lazy, once) ────────────────────────────────────────────────────────

  init() {
    if (this._initialized) return;
    this._initialized = true;
    // Populate the symbol select with F&O stocks
    this._populateSymbolSelect();
    // Load with default symbol
    this.load(this._symbol, this._interval);
  }

  _populateSymbolSelect() {
    const sel = document.getElementById('smc-symbol-select');
    if (!sel) return;
    try {
      const fnoList = equityScreener.getFNOUniverseSync();
      const sorted = [...fnoList].sort((a, b) => a.symbol.localeCompare(b.symbol));
      // Keep existing header options (NIFTY, BANKNIFTY, SENSEX) — append F&O stocks
      const existing = Array.from(sel.options).map(o => o.value);
      sorted.forEach(s => {
        if (!existing.includes(s.symbol)) {
          const opt = document.createElement('option');
          opt.value = s.symbol;
          opt.textContent = `${s.symbol} — ${s.name}`;
          sel.appendChild(opt);
        }
      });
    } catch (e) {
      console.warn('SmcDashboard: could not populate symbol select', e);
    }
  }

  // ── Public: load ─────────────────────────────────────────────────────────────

  async load(symbol, interval) {
    this._stopPolling();
    
    // Clear previous state on symbol change to prevent false auto-signals
    if (symbol && this._symbol && this._symbol !== symbol) {
      this._prevState = null;
      this._tags = [];
      this._renderTags();
    }
    
    this._symbol   = symbol   || this._symbol;
    this._interval = interval || this._interval;

    // Sync UI controls
    const sel = document.getElementById('smc-symbol-select');
    if (sel) sel.value = this._symbol;

    document.querySelectorAll('#smc-interval-tabs .tab-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.interval === this._interval);
    });

    // Show loading state in stat cards
    ['smc-ltp', 'smc-bias', 'smc-pcr', 'smc-iv-rank'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.textContent = '…';
    });
    document.getElementById('smc-updated') && (document.getElementById('smc-updated').textContent = 'Loading…');

    try {
      // Fetch OHLCV — 2 trading sessions back
      const from = this._tradingSessionsAgo(2);
      const to   = this._istToday();
      const token = await this._getToken(this._symbol);
      if (!token) throw new Error(`Instrument token not found for ${this._symbol}`);

      const raw = await kiteAPI.getHistoricalData(token, from, to, this._interval);
      const rawCandles = raw.candles || raw.data?.candles || raw;
      if (!Array.isArray(rawCandles) || rawCandles.length < 2) throw new Error('No OHLCV data returned');
      // Normalize candles to { date (ISO string), open, high, low, close, volume }
      const ohlcv = rawCandles.map(c => {
        let date, open, high, low, close, volume;
        if (Array.isArray(c)) { [date, open, high, low, close, volume] = c; }
        else { ({ date, open, high, low, close, volume } = c); }
        return { date: typeof date === 'string' ? date : new Date(date).toISOString(),
                 open, high, low, close, volume };
      });

      const sessionOhlcv = this._filterLastSession(ohlcv);
      const state = this._computeState(ohlcv, sessionOhlcv);
      const quote = await this._fetchQuote(this._symbol);

      // Merge live price
      if (quote && quote.ltp) state.ltp = quote.ltp;

      this._checkSignalChange(state);
      this._prevState = state;
      this._render(state, quote);

    } catch (err) {
      console.error('SmcDashboard.load error:', err);
      this._renderError(err.message);
    }

    if (this.isLiveSession()) this._startPolling();
  }

  // ── Token lookup ─────────────────────────────────────────────────────────────

  async _getToken(symbol) {
    try {
      // BSE SENSEX lives on BSE exchange with tradingsymbol 'SENSEX'
      if (symbol === 'BSE SENSEX') {
        return kiteAPI.getInstrumentToken('SENSEX', 'BSE')
            || kiteAPI.getInstrumentToken('BSE SENSEX', 'BSE');
      }
      // All other indices and F&O stocks are on NSE
      return kiteAPI.getInstrumentToken(symbol, 'NSE');
    } catch (e) {
      return null;
    }
  }

  // ── Session filter ────────────────────────────────────────────────────────────

  /** Returns bars from today's session (IST), falling back to last available date.
   *  Each bar has a `date` ISO string like "2026-04-07T09:15:00+0530". */
  _filterLastSession(ohlcv) {
    if (!ohlcv || ohlcv.length === 0) return [];
    const today = this._istToday();
    const grouped = ohlcv.reduce((acc, bar) => {
      const dateKey = String(bar.date).substring(0, 10);
      if (!acc[dateKey]) acc[dateKey] = [];
      acc[dateKey].push(bar);
      return acc;
    }, {});

    if (grouped[today] && grouped[today].length > 0) {
      return grouped[today];
    }

    const dates = Object.keys(grouped).sort();
    const latestDate = dates[dates.length - 1];
    return latestDate ? grouped[latestDate] : [];
  }

  // ── Core computation ──────────────────────────────────────────────────────────

  _computeState(fullOhlcv, sessionOhlcv) {
    const defaults = {
      marketStructure: '—', bos: '—', premiumDiscount: '—', fvgLabel: 'No active FVG',
      sweep: '—', bullOBs: [], bearOBs: [],
      rsi: 50, rsiLabel: 'Neutral', macdLabel: 'Neutral', emaStack: 'Mixed',
      volRatio: 1, adx: 0,
      bias: 'NEUTRAL', confidence: 50, ltp: 0, atr: 0,
      patternName: '—', support: null, resistance: null, candlePattern: '—',
    };

    if (!fullOhlcv || fullOhlcv.length < 10) return defaults;

    const src = fullOhlcv;
    const closes  = src.map(d => d.close);
    const highs   = src.map(d => d.high);
    const lows    = src.map(d => d.low);
    const opens   = src.map(d => d.open);
    const volumes = src.map(d => d.volume);
    const len = closes.length;

    // ── EMAs ──
    const ema9arr  = TI.computeEMA(closes, 9)  || [];
    const ema21arr = TI.computeEMA(closes, 21) || [];
    const ema50arr = TI.computeEMA(closes, 50) || [];
    const ema9  = ema9arr[ema9arr.length - 1]   || closes[len - 1];
    const ema21 = ema21arr[ema21arr.length - 1] || closes[len - 1];
    const ema50 = ema50arr[ema50arr.length - 1] || closes[len - 1];

    // ── Momentum ──
    const rsi = scoringEngine.computeRSI ? scoringEngine.computeRSI(closes) : (TI.computeRSI ? TI.computeRSI(closes) : 50);
    const macdObj = TI.computeMACD ? TI.computeMACD(closes) : { histogram: 0 };
    const adx  = scoringEngine.computeADX ? scoringEngine.computeADX(highs, lows, closes) : 20;
    const atr  = scoringEngine.computeATR ? scoringEngine.computeATR(highs, lows, closes) : 0;

    // ── EMA Stack ──
    let emaStack = 'Mixed';
    if (ema9 > ema21 && ema21 > ema50)       emaStack = '9 > 21 > 50 ✓';
    else if (ema9 < ema21 && ema21 < ema50)  emaStack = '9 < 21 < 50 ✓';

    // ── Market Structure (simple EMA9/21 + higher-lows check) ──
    let marketStructure = 'Ranging';
    const bullTrend = ema9 > ema21;
    const bearTrend = ema9 < ema21;

    // Higher-lows detection: compare last 3 swing lows in session
    if (sessionOhlcv && sessionOhlcv.length >= 6) {
      const sessLows = sessionOhlcv.map(d => d.low);
      const hhls = this._checkHigherLows(sessLows);
      const lhls = this._checkLowerHighs(sessionOhlcv.map(d => d.high));
      if (bullTrend && hhls) marketStructure = 'HH-HL (Uptrend)';
      else if (bearTrend && lhls) marketStructure = 'LH-LL (Downtrend)';
      else if (bullTrend) marketStructure = 'HH-HL (Uptrend)';
      else if (bearTrend) marketStructure = 'LH-LL (Downtrend)';
    } else {
      if (bullTrend) marketStructure = 'HH-HL (Uptrend)';
      else if (bearTrend) marketStructure = 'LH-LL (Downtrend)';
    }

    // ── BOS / CHoCH from ChartSignalEngine ──
    let bos = '—';
    try {
      if (window.chartSignalEngine && fullOhlcv.length >= 30) {
        const { signals } = chartSignalEngine.computeSignals(fullOhlcv);
        if (signals && signals.length > 0) {
          const last = signals[signals.length - 1];
          if (last.tags && last.tags.includes('CHoCH')) bos = 'CHoCH';
          else if (last.type === 'BUY' || last.type === 'SELL') bos = 'BOS Confirmed';
        }
      }
    } catch (e) { /* ignore */ }

    // ── Pivots for Premium/Discount & Support/Resistance ──
    let lastMajorHigh = null, lastMajorLow = null;
    let support = null, resistance = null;
    try {
      if (TI.computePivots) {
        const pivots = TI.computePivots(highs, lows, 5);
        for (let i = pivots.length - 1; i >= 0; i--) {
          if (pivots[i]) {
            if (pivots[i].type === 'high' && lastMajorHigh === null) lastMajorHigh = pivots[i].price;
            if (pivots[i].type === 'low'  && lastMajorLow  === null) lastMajorLow  = pivots[i].price;
            if (lastMajorHigh !== null && lastMajorLow !== null) break;
          }
        }
        if (lastMajorHigh !== null) resistance = { low: lastMajorHigh * 0.999, high: lastMajorHigh * 1.001 };
        if (lastMajorLow  !== null) support    = { low: lastMajorLow  * 0.999, high: lastMajorLow  * 1.001 };
      }
    } catch (e) { /* ignore */ }

    // Fallback pivot: rolling 20-bar
    if (!lastMajorHigh) lastMajorHigh = Math.max(...highs.slice(-20));
    if (!lastMajorLow)  lastMajorLow  = Math.min(...lows.slice(-20));
    if (!resistance) resistance = { low: lastMajorHigh * 0.999, high: lastMajorHigh * 1.001 };
    if (!support)    support    = { low: lastMajorLow  * 0.999, high: lastMajorLow  * 1.001 };

    const currentClose = closes[len - 1];
    const midpoint = (lastMajorHigh + lastMajorLow) / 2;
    let premiumDiscount = 'Neutral';
    if (currentClose < midpoint * 0.998) premiumDiscount = 'Discount Zone';
    else if (currentClose > midpoint * 1.002) premiumDiscount = 'Premium Zone';

    // ── FVG ──
    let fvgLabel = 'No active FVG';
    try {
      if (TI.computeFVG) {
        const fvgs = TI.computeFVG(highs, lows);
        for (let i = fvgs.length - 1; i >= 0; i--) {
          if (fvgs[i] && !fvgs[i].mitigated) {
            const mid = ((fvgs[i].low || 0) + (fvgs[i].high || 0)) / 2;
            fvgLabel = `FVG at ₹${mid.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
            break;
          }
        }
      }
    } catch (e) { /* ignore */ }

    // ── Liquidity Sweep (last 5 bars) ──
    let sweep = '—';
    const recentBars = src.slice(-5);
    for (let i = recentBars.length - 1; i >= 0; i--) {
      const bar = recentBars[i];
      // SSL sweep: low dipped below major low but closed above
      if (bar.low < lastMajorLow && bar.close > lastMajorLow) { sweep = 'SSL swept ✓'; break; }
      // BSL sweep: high pierced above major high but closed below
      if (bar.high > lastMajorHigh && bar.close < lastMajorHigh) { sweep = 'BSL swept ✓'; break; }
    }

    // ── Order Blocks (last 30 bars, vol >= 1.3x avg) ──
    const avgVol20 = volumes.slice(-20).reduce((a, b) => a + b, 0) / 20 || 1;
    const scan = src.slice(-30);
    const bullOBs = [], bearOBs = [];
    for (let i = 0; i < scan.length - 1; i++) {
      const bar = scan[i];
      const next = scan[i + 1];
      const volR = bar.volume / avgVol20;
      if (volR >= 1.3) {
        if (next.close > bar.high) {
          bullOBs.push({ low: bar.low, high: bar.high });
        } else if (next.close < bar.low) {
          bearOBs.push({ low: bar.low, high: bar.high });
        }
      }
    }

    // ── MACD label ──
    const hist = macdObj.histogram || 0;
    let macdLabel = 'Neutral';
    if (hist > 0)  macdLabel = 'Bullish crossover';
    if (hist < 0)  macdLabel = 'Bearish crossover';

    // ── RSI label ──
    let rsiLabel = 'Neutral';
    if (rsi >= 55) rsiLabel = 'Bullish';
    if (rsi <= 45) rsiLabel = 'Bearish';

    // ── Volume ratio ──
    const volRatio = volumes.length > 0 ? (volumes[volumes.length - 1] / avgVol20) : 1;

    // ── Candle pattern ──
    let candlePattern = '—';
    try {
      const cp = this._detectCandlePattern(opens, highs, lows, closes);
      if (cp) candlePattern = `${cp.icon} ${cp.name}`;
    } catch (e) { /* ignore */ }

    // ── Pattern name (from OBs / structure) ──
    let patternName = '—';
    if (bullOBs.length > 0 && marketStructure.includes('Uptrend')) patternName = 'Bull OB + Uptrend';
    else if (bearOBs.length > 0 && marketStructure.includes('Downtrend')) patternName = 'Bear OB + Downtrend';
    else if (bos === 'BOS Confirmed') patternName = 'Structure Break';
    else if (bos === 'CHoCH') patternName = 'CHoCH Reversal';
    else if (premiumDiscount === 'Discount Zone') patternName = 'Discount Retest';
    else if (premiumDiscount === 'Premium Zone') patternName = 'Premium Retest';

    // ── Bias scoring ──
    const signals = [];
    if (marketStructure.includes('Uptrend'))      signals.push(1);
    else if (marketStructure.includes('Downtrend')) signals.push(-1);
    else signals.push(0);

    if (ema9 > ema21 && ema21 > ema50)          signals.push(1);
    else if (ema9 < ema21 && ema21 < ema50)     signals.push(-1);
    else signals.push(0);

    if (rsi > 55)       signals.push(1);
    else if (rsi < 45)  signals.push(-1);
    else signals.push(0);

    if (hist > 0)       signals.push(1);
    else if (hist < 0)  signals.push(-1);
    else signals.push(0);

    if (premiumDiscount === 'Discount Zone') signals.push(1);
    else if (premiumDiscount === 'Premium Zone') signals.push(-1);
    else signals.push(0);

    if (bullOBs.length > 0) signals.push(1);
    if (bearOBs.length > 0) signals.push(-1);

    if (volRatio >= 1.3 && hist > 0)  signals.push(1);
    if (volRatio >= 1.3 && hist < 0)  signals.push(-1);

    const bullCount = signals.filter(s => s === 1).length;
    const bearCount = signals.filter(s => s === -1).length;
    const total = signals.length;
    const bullPct = (bullCount / total) * 100;
    const bearPct = (bearCount / total) * 100;

    let bias = 'NEUTRAL';
    let confidence = 50;
    // Lower threshold to 40% but strictly enforce directional dominance
    if (bullPct >= 40 && bullCount > bearCount) { 
        bias = 'BULLISH'; 
        confidence = Math.min(95, 50 + (bullCount / (bullCount + bearCount)) * 45); 
    }
    else if (bearPct >= 40 && bearCount > bullCount) { 
        bias = 'BEARISH'; 
        confidence = Math.min(95, 50 + (bearCount / (bullCount + bearCount)) * 45); 
    }
    else {
        confidence = 50;
    }

    return {
      marketStructure, bos, premiumDiscount, fvgLabel, sweep,
      bullOBs: bullOBs.slice(-2), bearOBs: bearOBs.slice(-2),
      rsi: +rsi.toFixed(1), rsiLabel, macdLabel, emaStack,
      volRatio: +volRatio.toFixed(2), adx: +(adx || 0).toFixed(1),
      bias, confidence: +confidence.toFixed(0),
      ltp: currentClose, atr: +(atr || 0).toFixed(2),
      patternName, support, resistance, candlePattern,
    };
  }

  // ── Candle pattern detection (matches analysis.js) ────────────────────────────

  _detectCandlePattern(opens, highs, lows, closes) {
    const len = closes.length;
    if (len < 3) return null;
    const o = opens[len-1], h = highs[len-1], l = lows[len-1], c = closes[len-1];
    const po = opens[len-2], pc = closes[len-2];
    const body = Math.abs(c - o);
    const range = h - l;
    const upperWick = h - Math.max(o, c);
    const lowerWick = Math.min(o, c) - l;

    if (range > 0 && body / range < 0.1)                              return { name: 'Doji',             bias: 'neutral',  icon: '⟺'  };
    if (lowerWick > body * 2 && upperWick < body * 0.5 && c > o)     return { name: 'Hammer',           bias: 'bullish',  icon: '🔨'  };
    if (upperWick > body * 2 && lowerWick < body * 0.5 && c < o)     return { name: 'Shooting Star',    bias: 'bearish',  icon: '⭐'  };
    if (c > o && pc < po && c > po && o < pc)                         return { name: 'Bullish Engulfing',bias: 'bullish',  icon: '🟢'  };
    if (c < o && pc > po && c < po && o > pc)                         return { name: 'Bearish Engulfing',bias: 'bearish',  icon: '🔴'  };
    if (c > o && Math.abs(c - pc) < range * 0.03)                     return { name: 'Marubozu (Bull)',  bias: 'bullish',  icon: '📈'  };
    if (c < o && Math.abs(c - pc) < range * 0.03)                     return { name: 'Marubozu (Bear)',  bias: 'bearish',  icon: '📉'  };
    return null;
  }

  // ── HH-HL / LH-LL helpers ─────────────────────────────────────────────────────

  _checkHigherLows(lows) {
    if (lows.length < 4) return false;
    // Find last 3 local swing lows
    const swings = [];
    for (let i = 1; i < lows.length - 1; i++) {
      if (lows[i] < lows[i-1] && lows[i] < lows[i+1]) swings.push(lows[i]);
    }
    if (swings.length < 2) return false;
    return swings[swings.length - 1] > swings[swings.length - 2];
  }

  _checkLowerHighs(highs) {
    if (highs.length < 4) return false;
    const swings = [];
    for (let i = 1; i < highs.length - 1; i++) {
      if (highs[i] > highs[i-1] && highs[i] > highs[i+1]) swings.push(highs[i]);
    }
    if (swings.length < 2) return false;
    return swings[swings.length - 1] < swings[swings.length - 2];
  }

  // ── Recommendation ──────────────────────────────────────────────────────────

  _computeRecommendation(state, quote) {
    if (!state || state.bias === 'NEUTRAL') return null;
    const ltp   = (quote && quote.ltp) || state.ltp || 0;
    if (!ltp) return null;

    const sym = this._symbol.toUpperCase();
    const isBankNifty = sym.includes('BANK') || sym.includes('BANKNIFTY');
    const isNifty     = !isBankNifty && (sym === 'NIFTY 50' || sym === 'NIFTY');
    const roundTo     = isBankNifty ? 100 : isNifty ? 50 : 10;
    const lots        = isBankNifty ? 30 : isNifty ? 75 : 50;

    const strike = Math.round(ltp / roundTo) * roundTo;
    const expiry = this._nextThursday();
    const direction = state.bias === 'BULLISH' ? 'CALL' : 'PUT';

    const entry  = +(ltp * 0.004).toFixed(0);   // ~0.4% of spot as rough ATM premium
    const sl     = +(entry * 0.35).toFixed(0);
    const target = +(entry * 1.8).toFixed(0);
    const rr     = '1:' + (target / sl).toFixed(1);

    return {
      direction,
      action: state.bias === 'BULLISH' ? 'BUY CALL' : 'BUY PUT',
      strike,
      expiry,
      entry,
      sl,
      target,
      rr,
      lots,
      ltp,
    };
  }

  /** Returns next Thursday as "DD Mon" (e.g. "10 Apr") */
  _nextThursday() {
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);
    const day = ist.getDay(); // 0=Sun … 6=Sat
    const daysToThurs = (4 - day + 7) % 7 || 7; // 4=Thursday
    const thu = new Date(ist);
    thu.setDate(ist.getDate() + daysToThurs);
    return `${String(thu.getDate()).padStart(2, '0')} ${months[thu.getMonth()]}`;
  }

  // ── Quote fetch ───────────────────────────────────────────────────────────────

  async _fetchQuote(symbol) {
    if (!kiteAPI.connected) return null;
    try {
      const resp = await fetch(`/api/stock-snapshot?symbol=${encodeURIComponent(symbol)}`, {
        headers: {
          'X-Kite-Api-Key':      kiteAPI.apiKey    || '',
          'X-Kite-Access-Token': kiteAPI.accessToken || '',
        },
        credentials: 'include',
      });
      if (!resp.ok) throw new Error('snapshot failed');
      const data = await resp.json();
      const snap = data.equity || data.snapshot || data;
      return {
        ltp:       snap.last_price || snap.ltp || 0,
        change:    snap.change || 0,
        changePct: snap.change_percent || snap.changePct || 0,
        oi:        snap.oi || snap.open_interest || 0,
        pcr:       snap.pcr || 0,
        iv:        snap.iv || 0,
      };
    } catch (e) {
      // Fallback: basic quote
      try {
        const resp2 = await fetch(`/api/quote?symbols=${encodeURIComponent(symbol)}`, {
          credentials: 'include',
        });
        if (!resp2.ok) return null;
        const data2 = await resp2.json();
        const q = data2[symbol] || Object.values(data2)[0] || {};
        return {
          ltp:       q.last_price || 0,
          change:    q.change || 0,
          changePct: q.change_percent || 0,
          oi: 0, pcr: 0, iv: 0,
        };
      } catch (e2) { return null; }
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────────

  _render(state, quote) {
    const bullColor    = '#26A69A';
    const bearColor    = '#EF5350';
    const neutralColor = '#78909C';

    const fmt = n => typeof n === 'number' ? n.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—';

    // ── Header stats ──
    const ltp = (quote && quote.ltp) || state.ltp;
    this._setText('smc-ltp', ltp ? `₹${ltp.toLocaleString('en-IN', { maximumFractionDigits: 2 })}` : '—');

    if (quote && quote.changePct !== undefined) {
      const chg = quote.changePct || 0;
      const chgEl = document.getElementById('smc-ltp-change');
      if (chgEl) {
        chgEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;
        chgEl.style.color = chg >= 0 ? bullColor : bearColor;
      }
    }

    const biasEl = document.getElementById('smc-bias');
    if (biasEl) {
      biasEl.textContent = state.bias;
      biasEl.style.color = state.bias === 'BULLISH' ? bullColor : state.bias === 'BEARISH' ? bearColor : neutralColor;
    }
    this._setText('smc-confidence', `Confidence: ${state.confidence}%`);

    const pcr = quote && quote.pcr ? quote.pcr.toFixed(2) : '—';
    this._setText('smc-pcr', pcr);
    if (quote && quote.pcr) {
      const pcrLabel = quote.pcr > 1.2 ? 'Bullish (Put heavy)' : quote.pcr < 0.8 ? 'Bearish (Call heavy)' : 'Neutral';
      this._setText('smc-pcr-label', pcrLabel);
    } else {
      this._setText('smc-pcr-label', 'N/A');
    }

    const iv = quote && quote.iv ? quote.iv.toFixed(1) : '—';
    this._setText('smc-iv-rank', iv !== '—' ? `${iv}%` : '—');
    if (quote && quote.iv) {
      const ivLabel = quote.iv > 25 ? 'Elevated — sell premium' : quote.iv < 15 ? 'Low — buy premium' : 'Normal';
      this._setText('smc-iv-label', ivLabel);
    } else {
      this._setText('smc-iv-label', 'N/A');
    }

    // ── SMC Structure panel ──
    this._setColorText('smc-market-structure', state.marketStructure,
      state.marketStructure.includes('Uptrend') ? bullColor :
      state.marketStructure.includes('Downtrend') ? bearColor : neutralColor);

    this._setColorText('smc-bos', state.bos,
      state.bos === 'BOS Confirmed' ? bullColor :
      state.bos === 'CHoCH' ? '#FFA726' : neutralColor);

    this._setColorText('smc-premium-discount', state.premiumDiscount,
      state.premiumDiscount === 'Discount Zone' ? bullColor :
      state.premiumDiscount === 'Premium Zone' ? bearColor : neutralColor);

    this._setText('smc-fvg', state.fvgLabel);

    this._setColorText('smc-sweep', state.sweep,
      state.sweep !== '—' ? '#FFA726' : neutralColor);

    // Order blocks
    const obList = document.getElementById('smc-ob-list');
    if (obList) {
      const bulls = state.bullOBs.map(ob =>
        `<span class="tag" style="background:${bullColor}22;color:${bullColor};border:1px solid ${bullColor}44;font-size:0.68rem;">Bull OB ${ob.low.toLocaleString('en-IN',{maximumFractionDigits:0})}–${ob.high.toLocaleString('en-IN',{maximumFractionDigits:0})}</span>`
      ).join('');
      const bears = state.bearOBs.map(ob =>
        `<span class="tag" style="background:${bearColor}22;color:${bearColor};border:1px solid ${bearColor}44;font-size:0.68rem;">Bear OB ${ob.low.toLocaleString('en-IN',{maximumFractionDigits:0})}–${ob.high.toLocaleString('en-IN',{maximumFractionDigits:0})}</span>`
      ).join('');
      obList.innerHTML = (bulls + bears) || `<span style="font-size:0.75rem;color:var(--text-muted);">No OBs detected</span>`;
    }

    // ── Momentum panel ──
    this._setColorText('smc-rsi',
      `${state.rsi} — ${state.rsiLabel}`,
      state.rsiLabel === 'Bullish' ? bullColor : state.rsiLabel === 'Bearish' ? bearColor : neutralColor);

    this._setColorText('smc-macd', state.macdLabel,
      state.macdLabel.includes('Bullish') ? bullColor :
      state.macdLabel.includes('Bearish') ? bearColor : neutralColor);

    this._setColorText('smc-ema-stack', state.emaStack,
      state.emaStack.includes('>') ? bullColor :
      state.emaStack.includes('<') ? bearColor : neutralColor);

    const volColor = state.volRatio >= 1.3 ? bullColor : neutralColor;
    this._setColorText('smc-vol-delta',
      `${state.volRatio.toFixed(2)}x avg${state.volRatio >= 1.5 ? ' 🔥' : ''}`,
      volColor);

    this._setText('smc-oi-change', quote && quote.oi ? fmt(quote.oi) : '—');

    // Strength bar
    const strengthLabel = document.getElementById('smc-strength-label');
    const strengthBar   = document.getElementById('smc-strength-bar');
    if (strengthLabel) {
      strengthLabel.textContent = `${state.confidence}%`;
      strengthLabel.style.color = state.bias === 'BULLISH' ? bullColor : state.bias === 'BEARISH' ? bearColor : neutralColor;
    }
    if (strengthBar) {
      strengthBar.style.width = `${state.confidence}%`;
      strengthBar.style.background = state.bias === 'BULLISH' ? bullColor : state.bias === 'BEARISH' ? bearColor : neutralColor;
    }

    // ── Chart Patterns panel ──
    this._setText('smc-pattern-name', state.patternName);
    this._setText('smc-support',
      state.support ? `₹${state.support.low.toLocaleString('en-IN',{maximumFractionDigits:0})} – ₹${state.support.high.toLocaleString('en-IN',{maximumFractionDigits:0})}` : '—');
    this._setText('smc-resistance',
      state.resistance ? `₹${state.resistance.low.toLocaleString('en-IN',{maximumFractionDigits:0})} – ₹${state.resistance.high.toLocaleString('en-IN',{maximumFractionDigits:0})}` : '—');
    this._setText('smc-candle-pattern', state.candlePattern);

    // ── Recommendation ──
    const reco = this._computeRecommendation(state, quote);
    this._renderReco(reco, state);

    // ── Live dot ──
    const liveDot = document.getElementById('smc-live-dot');
    if (liveDot) {
      liveDot.style.background = this.isLiveSession() ? '#26A69A' : '#78909C';
      liveDot.title = this.isLiveSession() ? 'Live session' : 'Market closed';
    }

    // ── Last updated ──
    const updEl = document.getElementById('smc-updated');
    if (updEl) {
      const ts = new Date().toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
      updEl.textContent = `Updated ${ts} IST`;
    }
  }

  _renderReco(reco, state) {
    const body = document.getElementById('smc-reco-body');
    if (!body) return;
    if (!reco) {
      body.innerHTML = `<div style="color:var(--text-muted);font-size:0.8rem;padding:16px 0;">${state.bias === 'NEUTRAL' ? 'No clear directional bias — wait for setup.' : 'Run analysis to see recommendation.'}</div>`;
      return;
    }
    const dirColor = reco.direction === 'CALL' ? '#26A69A' : '#EF5350';
    body.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
        <span style="font-size:1.5rem;font-weight:700;color:${dirColor};">${reco.action}</span>
        <span class="tag" style="background:${dirColor}22;color:${dirColor};border:1px solid ${dirColor}44;font-size:0.75rem;">${reco.direction}</span>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:0.82rem;">
        <tr><td style="padding:5px 0;color:var(--text-secondary);">Strike</td><td style="text-align:right;font-weight:600;">₹${reco.strike.toLocaleString('en-IN')}</td></tr>
        <tr><td style="padding:5px 0;color:var(--text-secondary);">Expiry</td><td style="text-align:right;font-weight:600;">${reco.expiry} (weekly)</td></tr>
        <tr><td style="padding:5px 0;color:var(--text-secondary);">Entry (est.)</td><td style="text-align:right;font-weight:600;">₹${reco.entry.toLocaleString('en-IN')}</td></tr>
        <tr><td style="padding:5px 0;color:var(--text-secondary);">Stop Loss</td><td style="text-align:right;font-weight:600;color:#EF5350;">₹${reco.sl.toLocaleString('en-IN')}</td></tr>
        <tr><td style="padding:5px 0;color:var(--text-secondary);">Target</td><td style="text-align:right;font-weight:600;color:#26A69A;">₹${reco.target.toLocaleString('en-IN')}</td></tr>
        <tr><td style="padding:5px 0;color:var(--text-secondary);">R:R</td><td style="text-align:right;font-weight:600;">${reco.rr}</td></tr>
        <tr><td style="padding:5px 0;color:var(--text-secondary);">Lot size</td><td style="text-align:right;font-weight:600;">${reco.lots}</td></tr>
      </table>
      <div style="font-size:0.68rem;color:var(--text-muted);margin-top:10px;">⚠️ Premium is estimated at ~0.4% of spot. Verify live option chain for actual prices.</div>
      <button id="btn-smc-tag-entry" data-direction="${reco.direction}" data-entry="${reco.entry}" data-sl="${reco.sl}" data-target="${reco.target}"
        style="margin-top:14px;width:100%;padding:10px;border:none;border-radius:8px;background:${dirColor};color:#fff;font-weight:700;font-size:0.9rem;cursor:pointer;letter-spacing:0.04em;">
        🏷 TAG ENTRY
      </button>
    `;
  }

  _renderError(msg) {
    ['smc-ltp', 'smc-bias', 'smc-pcr', 'smc-iv-rank'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.textContent = '—'; el.style.color = ''; }
    });
    const body = document.getElementById('smc-reco-body');
    if (body) body.innerHTML = `<div style="color:#EF5350;font-size:0.8rem;padding:8px 0;">⚠️ ${msg}</div>`;
    const updEl = document.getElementById('smc-updated');
    if (updEl) updEl.textContent = `Error: ${msg}`;
  }

  // ── Signal change detection ────────────────────────────────────────────────────

  _checkSignalChange(newState) {
    if (!this._prevState) return;
    
    // Live signal tags should only fire during the live session (09:15 onwards)
    if (!this.isLiveSession()) return;
    
    const prev = this._prevState;

    // Bias flip to BULLISH or BEARISH → auto ENTRY tag
    if (prev.bias === 'NEUTRAL' && newState.bias === 'BULLISH') {
      const reco = this._computeRecommendation(newState, null);
      this._addTag({
        time: Date.now(),
        type: 'ENTRY',
        title: `BUY CALL — ${this._symbol}`,
        description: `Structure shifted to uptrend. Consider BUY CALL.`,
        entryPrice: reco ? reco.entry : 0,
        sl:         reco ? reco.sl    : 0,
        target:     reco ? reco.target: 0,
      });
    } else if (prev.bias === 'NEUTRAL' && newState.bias === 'BEARISH') {
      const reco = this._computeRecommendation(newState, null);
      this._addTag({
        time: Date.now(),
        type: 'ENTRY',
        title: `BUY PUT — ${this._symbol}`,
        description: `Structure shifted to downtrend. Consider BUY PUT.`,
        entryPrice: reco ? reco.entry : 0,
        sl:         reco ? reco.sl    : 0,
        target:     reco ? reco.target: 0,
      });
    }

    // Sweep detected → TRAIL tag
    if (prev.sweep === '—' && newState.sweep !== '—') {
      this._addTag({
        time: Date.now(),
        type: 'TRAIL',
        title: `Liquidity Swept — ${this._symbol}`,
        description: `${newState.sweep}. Trail stop or watch for reversal.`,
        entryPrice: 0, sl: 0, target: 0,
      });
    }
  }

  // ── Tag management ─────────────────────────────────────────────────────────────

  _addTag(tag) {
    this._tags.unshift(tag);
    if (this._tags.length > 100) this._tags.pop();
    this._renderTags();
  }

  _renderTags() {
    const feed = document.getElementById('smc-tag-feed');
    if (!feed) return;

    if (this._tags.length === 0) {
      feed.innerHTML = `<div class="empty-state" style="padding:24px;"><div style="font-size:0.82rem;color:var(--text-muted);">No signal tags yet. Signals fire automatically when bias or structure changes.</div></div>`;
      return;
    }

    const colorMap = {
      ENTRY: { border: '#26A69A', bg: '#26A69A22', badge: '#26A69A' },
      EXIT:  { border: '#5C6BC0', bg: '#5C6BC022', badge: '#5C6BC0' },
      TRAIL: { border: '#FFA726', bg: '#FFA72622', badge: '#FFA726' },
      WAIT:  { border: '#78909C', bg: '#78909C22', badge: '#78909C' },
    };

    feed.innerHTML = this._tags.map(tag => {
      const c = colorMap[tag.type] || colorMap.WAIT;
      const ts = new Date(tag.time).toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit'
      });
      const extras = tag.entryPrice ? `
        <div style="font-size:0.7rem;color:var(--text-muted);margin-top:4px;">
          Entry ₹${tag.entryPrice.toLocaleString('en-IN')} · SL ₹${tag.sl.toLocaleString('en-IN')} · Target ₹${tag.target.toLocaleString('en-IN')}
        </div>` : '';
      return `
        <div style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-left:3px solid ${c.border};background:${c.bg};margin-bottom:4px;border-radius:0 6px 6px 0;">
          <span style="font-size:0.7rem;color:var(--text-muted);min-width:44px;padding-top:1px;">${ts}</span>
          <div style="flex:1;">
            <span style="font-size:0.82rem;font-weight:600;color:var(--text-primary);">${tag.title}</span>
            <span style="font-size:0.75rem;color:var(--text-secondary);"> — ${tag.description}</span>
            ${extras}
          </div>
          <span style="font-size:0.65rem;font-weight:700;padding:2px 7px;border-radius:8px;background:${c.badge};color:#fff;white-space:nowrap;">${tag.type}</span>
        </div>`;
    }).join('');
  }

  // ── Simulate helpers ───────────────────────────────────────────────────────────

  simulateBull() {
    const ltp = this._prevState ? this._prevState.ltp : 0;
    const entry  = +(ltp * 0.004).toFixed(0) || 200;
    const sl     = +(entry * 0.35).toFixed(0);
    const target = +(entry * 1.8).toFixed(0);
    this._addTag({
      time: Date.now(),
      type: 'ENTRY',
      title: `BUY CALL — ${this._symbol}`,
      description: `Simulated bullish SMC setup (OB + BOS + EMA stack)`,
      entryPrice: entry, sl, target,
    });
  }

  simulateBear() {
    const ltp = this._prevState ? this._prevState.ltp : 0;
    const entry  = +(ltp * 0.004).toFixed(0) || 200;
    const sl     = +(entry * 0.35).toFixed(0);
    const target = +(entry * 1.8).toFixed(0);
    this._addTag({
      time: Date.now(),
      type: 'ENTRY',
      title: `BUY PUT — ${this._symbol}`,
      description: `Simulated bearish SMC setup (Bear OB + CHoCH + EMA flip)`,
      entryPrice: entry, sl, target,
    });
  }

  simulateExit() {
    this._addTag({
      time: Date.now(),
      type: 'EXIT',
      title: `Exit signal — ${this._symbol}`,
      description: 'Simulated exit: structure broke, trail stop hit',
      entryPrice: 0, sl: 0, target: 0,
    });
  }

  // ── Polling ────────────────────────────────────────────────────────────────────

  _startPolling() {
    this._stopPolling();
    this._pollTimer = setInterval(async () => {
      if (!this.isLiveSession()) {
        this._stopPolling();
        const updEl = document.getElementById('smc-updated');
        if (updEl) updEl.textContent = 'Market closed — polling stopped';
        return;
      }
      try {
        const from = this._tradingSessionsAgo(2);
        const to   = this._istToday();
        const token = await this._getToken(this._symbol);
        if (!token) return;
        const raw = await kiteAPI.getHistoricalData(token, from, to, this._interval);
        const rawCandles = raw.candles || raw.data?.candles || raw;
        if (!Array.isArray(rawCandles) || rawCandles.length < 2) return;
        const ohlcv = rawCandles.map(c => {
          let date, open, high, low, close, volume;
          if (Array.isArray(c)) { [date, open, high, low, close, volume] = c; }
          else { ({ date, open, high, low, close, volume } = c); }
          return { date: typeof date === 'string' ? date : new Date(date).toISOString(),
                   open, high, low, close, volume };
        });
        const sessionOhlcv = this._filterLastSession(ohlcv);
        const state = this._computeState(ohlcv, sessionOhlcv);
        const quote = await this._fetchQuote(this._symbol);
        if (quote && quote.ltp) state.ltp = quote.ltp;
        this._checkSignalChange(state);
        this._prevState = state;
        this._render(state, quote);
      } catch (e) {
        console.warn('SmcDashboard poll error:', e);
      }
    }, this.POLL_MS);
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  stop() {
    this._stopPolling();
  }

  // ── DOM helpers ────────────────────────────────────────────────────────────────

  _setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  _setColorText(id, text, color) {
    const el = document.getElementById(id);
    if (el) { el.textContent = text; el.style.color = color; }
  }
}

// ── Singleton ──
window.smcDashboard = new SmcDashboard();
