/**
 * TradeSignal — Trade Tracker Engine
 *
 * Implements Trade Management rules from executable_call_put_rules.pdf:
 *   HOLD → REDUCE → SCALE-OUT → FULL EXIT
 *
 * Evaluates each new candle against the active trade position and fires
 * appropriate alerts (HOLD/REDUCE/SCALE_OUT/EXIT).
 *
 * Audio alerts are toggleable via tradeTracker.audioEnabled.
 *
 * Depends on: TI (technical-indicators.js), entryValidator, alertEngine
 */
class TradeTracker {
  constructor() {
    this._activeTrade = null;
    this._signalLog = [];
    this._listeners = [];
    this.audioEnabled = true;
    this._audioCtx = null;
  }

  // ── Audio ──

  _beep(freq = 880, duration = 200) {
    if (!this.audioEnabled) return;
    try {
      if (!this._audioCtx) this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = this._audioCtx.createOscillator();
      const gain = this._audioCtx.createGain();
      osc.connect(gain);
      gain.connect(this._audioCtx.destination);
      osc.frequency.value = freq;
      gain.gain.value = 0.3;
      osc.start();
      osc.stop(this._audioCtx.currentTime + duration / 1000);
    } catch (e) { /* silent fail */ }
  }

  _alertBeep() { this._beep(880, 150); setTimeout(() => this._beep(1100, 150), 200); }
  _exitBeep() { this._beep(440, 300); setTimeout(() => this._beep(330, 300), 350); setTimeout(() => this._beep(220, 500), 700); }

  // ── Event system ──

  on(event, fn) { this._listeners.push({ event, fn }); }
  _emit(event, data) { this._listeners.filter(l => l.event === event).forEach(l => l.fn(data)); }

  // ── Trade Management ──

  /**
   * Enter a new trade.
   * @param {Object} params
   *   - direction: 'CALL' | 'PUT'
   *   - entryPrice: number
   *   - stopLoss: number
   *   - setupType: 'TYPE_1' | 'TYPE_2' | 'TYPE_3'
   *   - symbol: string
   *   - entryTime: string (ISO timestamp)
   */
  enterTrade(params) {
    const { direction, entryPrice, stopLoss, setupType, symbol, entryTime } = params;

    this._activeTrade = {
      direction,
      entryPrice,
      stopLoss,
      originalSL: stopLoss,
      setupType,
      symbol,
      entryTime: entryTime || new Date().toISOString(),
      candleCount: 0,
      status: 'ACTIVE',      // ACTIVE | REDUCED | SCALED | CLOSED
      scaledOutPct: 0,       // % already scaled out
      recentHL: null,        // Most recent higher-low (CALL) / lower-high (PUT)
      ema9BreakCount: 0,     // Consecutive EMA 9 breaks
      vwapFailCount: 0,      // Consecutive VWAP failures
      signalHistory: [],     // [{candle, action, reason}]
      lastAction: 'ENTRY',
    };

    this._signalLog = [];
    this._addSignal('ENTRY', `${direction} Entry @ ₹${entryPrice.toFixed(0)} | SL: ₹${stopLoss.toFixed(0)} | Setup: ${setupType}`, entryTime);
    this._emit('trade-entered', this._activeTrade);
    return this._activeTrade;
  }

  /**
   * Process a new candle against the active trade.
   * Called on each 5-minute candle close.
   *
   * @param {Object} candle - { date, open, high, low, close, volume }
   * @param {Array} allCandles - full session OHLCV array up to this candle
   * @returns {Object} action result
   */
  processCandle(candle, allCandles) {
    if (!this._activeTrade || this._activeTrade.status === 'CLOSED') {
      return { action: 'NONE', reason: 'No active trade' };
    }

    const trade = this._activeTrade;
    trade.candleCount++;

    const len = allCandles.length;

    // ── Session-aware: filter to today's candles for indicator computation ──
    const sessionCandles = TI.filterTodaySession(allCandles);
    const closes = sessionCandles.map(d => d.close);
    const highs = sessionCandles.map(d => d.high);
    const lows = sessionCandles.map(d => d.low);
    const opens = sessionCandles.map(d => d.open);
    const volumes = sessionCandles.map(d => d.volume);

    const c = candle.close;
    const o = candle.open;
    const h = candle.high;
    const l = candle.low;
    const range = h - l;

    // Compute indicators on today's session
    const ema9arr = TI.computeEMA(closes, 9);
    const ema21arr = TI.computeEMA(closes, 21);
    const vwapArr = TI.computeIntradayVWAP(sessionCandles);
    const atr = TI.computeATR(highs, lows, closes);

    const ema9 = ema9arr[ema9arr.length - 1] || c;
    const ema21 = ema21arr[ema21arr.length - 1] || c;
    const vwap = vwapArr[vwapArr.length - 1] || c;

    // Average candle range for context
    const avgRange = allCandles.slice(-10).reduce((s, d) => s + (d.high - d.low), 0) / Math.min(10, allCandles.length);

    // Track higher-lows (CALL) / lower-highs (PUT) within current leg
    this._updateStructure(trade, candle, allCandles);

    const isCall = trade.direction === 'CALL';
    const time = candle.date;

    // ══════════════════════════════════════════════════════════
    // EVALUATION ORDER (most severe first — Override Rule)
    // ══════════════════════════════════════════════════════════

    // ── 1. FULL EXIT checks ──

    // 1a. STOP LOSS broken on closing basis
    if (isCall && c <= trade.stopLoss) {
      return this._fullExit(trade, c, time, 'Stop Loss broken on close');
    }
    if (!isCall && c >= trade.stopLoss) {
      return this._fullExit(trade, c, time, 'Stop Loss broken on close');
    }

    // 1b. Gap Open Exception — if price gaps through stop at open
    if (isCall && o < trade.stopLoss && l < trade.stopLoss) {
      return this._fullExit(trade, o, time, 'Gap open below SL — exit immediately at open');
    }
    if (!isCall && o > trade.stopLoss && h > trade.stopLoss) {
      return this._fullExit(trade, o, time, 'Gap open above SL — exit immediately at open');
    }

    // 1c. STRUCTURE BREAK — HL (call) / LH (put) broken with close
    if (trade.recentHL != null) {
      if (isCall && c < trade.recentHL) {
        return this._fullExit(trade, c, time, `Structure break: HL ₹${trade.recentHL.toFixed(0)} violated`);
      }
      if (!isCall && c > trade.recentHL) {
        return this._fullExit(trade, c, time, `Structure break: LH ₹${trade.recentHL.toFixed(0)} violated`);
      }
    }

    // 1d. VWAP FAILURE — 2 consecutive closes on wrong side
    if (isCall && c < vwap) {
      trade.vwapFailCount++;
    } else if (!isCall && c > vwap) {
      trade.vwapFailCount++;
    } else {
      trade.vwapFailCount = 0;
    }

    // Skip VWAP rule in first 15 minutes (no anchor) and reduce weight in last 30 min
    const timeObj = new Date(time);
    const minsInSession = timeObj.getHours() * 60 + timeObj.getMinutes() - 9 * 60 - 15;
    const isFirst15 = minsInSession < 15;
    const isLast30 = minsInSession >= 345;

    if (!isFirst15 && trade.vwapFailCount >= 2) {
      if (!isLast30) {
        return this._fullExit(trade, c, time, `VWAP Failure: ${trade.vwapFailCount} consecutive closes on wrong side`);
      }
      // Last 30 min: VWAP carries reduced weight, only flag, don't auto-exit
    }

    // ── 2. REDUCE checks (early warning) ──

    // 2a. Two consecutive closes below EMA 9 (call) / above EMA 9 (put)
    if (isCall && c < ema9) {
      trade.ema9BreakCount++;
    } else if (!isCall && c > ema9) {
      trade.ema9BreakCount++;
    } else {
      trade.ema9BreakCount = 0;
    }

    if (trade.ema9BreakCount >= 2 && trade.status !== 'REDUCED') {
      // Only trigger if price is near VWAP (within 1 avg candle range)
      const vwapDistance = Math.abs(c - vwap);
      if (vwapDistance < avgRange) {
        return this._reduce(trade, c, time, `2 consecutive closes ${isCall ? 'below' : 'above'} EMA 9 near VWAP`);
      }
      // If price is well above/below VWAP, it's a normal fast-trend pullback
    }

    // ── 3. PARTIAL SCALE-OUT checks ──

    // 3a. 3+ full-body exhaustion candles
    if (trade.candleCount >= 3 && trade.scaledOutPct < 40) {
      const lastN = allCandles.slice(-3);
      const allFullBody = lastN.every(bar => {
        const body = Math.abs(bar.close - bar.open);
        const rng = bar.high - bar.low;
        if (rng <= 0) return false;
        const isFullBody = body / rng > 0.7;
        const atExtreme = isCall ? (bar.close >= bar.high - rng * 0.1) : (bar.close <= bar.low + rng * 0.1);
        return isFullBody && atExtreme;
      });
      if (allFullBody) {
        return this._scaleOut(trade, c, time, '3+ full-body exhaustion candles without pause');
      }
    }

    // ── 4. HOLD — IGNORE NOISE ──

    // Single counter-trend candle
    if (trade.candleCount === 1) {
      return this._hold(trade, c, time, 'First candle after entry — monitoring');
    }

    // Single wick through a level with no close beyond
    // Single EMA 9 break with no close confirmation
    if (trade.ema9BreakCount === 1) {
      return this._hold(trade, c, time, 'Single EMA 9 touch — no close confirmation, ignoring noise');
    }

    // Check for large engulfing candle (early warning, but no action yet)
    const prevCandle = allCandles[allCandles.length - 2];
    if (prevCandle) {
      const currentBody = Math.abs(c - o);
      const prevBody = Math.abs(prevCandle.close - prevCandle.open);
      const isCounterTrend = isCall ? (c < o) : (c > o);
      if (isCounterTrend && currentBody > prevBody * 2 && currentBody > avgRange * 0.8) {
        return this._hold(trade, c, time, '⚠ Large counter-trend candle detected — watching next candle closely');
      }
    }

    // Default: HOLD
    const pnl = isCall ? ((c - trade.entryPrice) / trade.entryPrice * 100) : ((trade.entryPrice - c) / trade.entryPrice * 100);
    return this._hold(trade, c, time, `Trend intact. P&L: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`);
  }

  // ── Actions ──

  _fullExit(trade, price, time, reason) {
    trade.status = 'CLOSED';
    const pnl = trade.direction === 'CALL'
      ? ((price - trade.entryPrice) / trade.entryPrice * 100)
      : ((trade.entryPrice - price) / trade.entryPrice * 100);
    const result = { action: 'EXIT', reason, price, pnl: +pnl.toFixed(2), time };
    this._addSignal('EXIT', `🔴 FULL EXIT @ ₹${price.toFixed(0)} — ${reason} | P&L: ${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`, time);
    this._exitBeep();
    this._emit('trade-exit', { trade, result });
    this._fireBrowserAlert('EXIT', `${trade.symbol} ${trade.direction}: EXIT — ${reason}`);
    return result;
  }

  _reduce(trade, price, time, reason) {
    trade.status = 'REDUCED';
    trade.lastAction = 'REDUCE';
    const result = { action: 'REDUCE', reason, price, time };
    this._addSignal('REDUCE', `⚠ REDUCE — ${reason}`, time);
    this._alertBeep();
    this._emit('trade-reduce', { trade, result });
    this._fireBrowserAlert('REDUCE', `${trade.symbol} ${trade.direction}: REDUCE — ${reason}`);
    return result;
  }

  _scaleOut(trade, price, time, reason) {
    trade.scaledOutPct += 40; // Scale out 40%
    trade.status = 'SCALED';
    trade.lastAction = 'SCALE_OUT';
    const result = { action: 'SCALE_OUT', reason, price, scaledPct: trade.scaledOutPct, time };
    this._addSignal('SCALE_OUT', `📤 SCALE OUT 40% @ ₹${price.toFixed(0)} — ${reason}`, time);
    this._alertBeep();
    this._emit('trade-scale', { trade, result });
    return result;
  }

  _hold(trade, price, time, reason) {
    trade.lastAction = 'HOLD';
    const result = { action: 'HOLD', reason, price, time };
    this._addSignal('HOLD', reason, time);
    return result;
  }

  // ── Structure tracking ──

  _updateStructure(trade, candle, allCandles) {
    const isCall = trade.direction === 'CALL';
    const len = allCandles.length;
    if (len < 3) return;

    // Find most recent higher-low (CALL) or lower-high (PUT)
    const recentLows = [];
    const recentHighs = [];
    for (let i = Math.max(0, len - 20); i < len; i++) {
      if (i >= 1 && i < len - 1) {
        if (allCandles[i].low < allCandles[i - 1].low && allCandles[i].low < allCandles[i + 1].low) {
          recentLows.push(allCandles[i].low);
        }
        if (allCandles[i].high > allCandles[i - 1].high && allCandles[i].high > allCandles[i + 1].high) {
          recentHighs.push(allCandles[i].high);
        }
      }
    }

    if (isCall && recentLows.length >= 2) {
      // Most recent higher-low — only valid if it's actually higher than the one before it
      const lastTwo = recentLows.slice(-2);
      if (lastTwo[1] >= lastTwo[0]) {
        trade.recentHL = lastTwo[1]; // Confirmed higher-low
      }
      // If latest low is LOWER than previous, structure is already broken —
      // keep the old HL so the exit check fires on the next candle
    }
    if (!isCall && recentHighs.length >= 2) {
      // Most recent lower-high — only valid if it's actually lower than the one before it
      const lastTwo = recentHighs.slice(-2);
      if (lastTwo[1] <= lastTwo[0]) {
        trade.recentHL = lastTwo[1]; // Confirmed lower-high
      }
      // If latest high is HIGHER than previous, structure is already broken —
      // keep the old LH so the exit check fires on the next candle
    }
  }

  // ── Signal Log ──

  _addSignal(type, message, time) {
    const entry = {
      type,
      message,
      time: time || new Date().toISOString(),
      candleNum: this._activeTrade?.candleCount || 0,
    };
    this._signalLog.push(entry);
    this._emit('signal', entry);
  }

  getSignalLog() { return [...this._signalLog]; }

  getActiveTrade() { return this._activeTrade ? { ...this._activeTrade } : null; }

  closeTrade() {
    if (this._activeTrade) {
      this._activeTrade.status = 'CLOSED';
      this._addSignal('CLOSE', 'Trade manually closed');
      this._emit('trade-closed', this._activeTrade);
    }
    this._activeTrade = null;
  }

  // ── Re-entry after reduce ──

  handleReEntry(price, time) {
    if (!this._activeTrade || this._activeTrade.status !== 'REDUCED') return null;
    const trade = this._activeTrade;
    trade.status = 'ACTIVE';
    trade.ema9BreakCount = 0;
    // Set fresh stop from most recent HL/LH
    if (trade.recentHL != null) {
      trade.stopLoss = trade.recentHL;
    }
    this._addSignal('RE_ENTRY', `↩ Re-entry @ ₹${price.toFixed(0)} | Fresh SL: ₹${trade.stopLoss.toFixed(0)}`, time);
    return { action: 'RE_ENTRY', price, newSL: trade.stopLoss, time };
  }

  // ── Browser Alerts ──

  _fireBrowserAlert(type, message) {
    try {
      if (typeof alertEngine !== 'undefined' && alertEngine.notify) {
        alertEngine.notify(message);
      }
      if (Notification.permission === 'granted') {
        new Notification(`TradeSignal — ${type}`, { body: message, icon: type === 'EXIT' ? '🔴' : '⚠️' });
      }
    } catch (e) { /* ignore */ }
  }

  // ── HTML Rendering ──

  renderSignalLog() {
    if (this._signalLog.length === 0) {
      return '<div style="padding:20px;text-align:center;color:var(--text-muted);font-size:0.8rem;">No signals yet. Enter a trade to start tracking.</div>';
    }

    return this._signalLog.map(s => {
      const colors = {
        ENTRY: '#1E88E5', HOLD: '#78909C', REDUCE: '#FF9800',
        SCALE_OUT: '#AB47BC', EXIT: '#EF5350', CLOSE: '#EF5350', RE_ENTRY: '#26A69A',
      };
      const color = colors[s.type] || '#78909C';
      const bg = `${color}11`;
      const icon = s.type === 'ENTRY' ? '▶' : s.type === 'EXIT' || s.type === 'CLOSE' ? '🔴' :
                   s.type === 'REDUCE' ? '⚠' : s.type === 'SCALE_OUT' ? '📤' :
                   s.type === 'RE_ENTRY' ? '↩' : '·';

      const ts = new Date(s.time);
      const timeStr = `${String(ts.getHours()).padStart(2, '0')}:${String(ts.getMinutes()).padStart(2, '0')}`;

      return `<div style="padding:6px 10px;border-left:3px solid ${color};background:${bg};border-radius:0 6px 6px 0;margin-bottom:4px;font-size:0.75rem;display:flex;gap:8px;align-items:baseline;">
        <span style="color:var(--text-muted);min-width:36px;font-size:0.68rem;">${timeStr}</span>
        <span style="font-weight:600;color:${color};min-width:14px;">${icon}</span>
        <span style="color:var(--text-primary);">${s.message}</span>
      </div>`;
    }).join('');
  }

  renderActiveTradeStatus() {
    const trade = this._activeTrade;
    if (!trade) return '';

    const isCall = trade.direction === 'CALL';
    const dirColor = isCall ? '#26A69A' : '#EF5350';
    const statusColors = { ACTIVE: '#26A69A', REDUCED: '#FF9800', SCALED: '#AB47BC', CLOSED: '#EF5350' };
    const statusColor = statusColors[trade.status] || '#78909C';

    return `
      <div style="display:flex;align-items:center;gap:12px;padding:10px 16px;background:${dirColor}0A;border-radius:8px;border:1px solid ${dirColor}33;">
        <div style="width:10px;height:10px;border-radius:50%;background:${statusColor};${trade.status === 'ACTIVE' ? 'animation:pulse 1.5s infinite;' : ''}"></div>
        <div>
          <span style="font-weight:700;font-size:0.9rem;color:${dirColor};">${trade.symbol} ${trade.direction}</span>
          <span style="color:var(--text-muted);font-size:0.75rem;margin-left:8px;">@ ₹${trade.entryPrice.toFixed(0)}</span>
        </div>
        <div style="margin-left:auto;text-align:right;">
          <span class="tag" style="background:${statusColor}22;color:${statusColor};border:1px solid ${statusColor}44;font-size:0.68rem;">${trade.status}</span>
          <div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px;">Candle ${trade.candleCount} | SL: ₹${trade.stopLoss.toFixed(0)}</div>
        </div>
      </div>`;
  }
}

window.tradeTracker = new TradeTracker();
