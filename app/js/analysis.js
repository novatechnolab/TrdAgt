/**
 * TradeSignal — On-Demand Stock Analysis
 *
 * Supports pre-market (OHLCV-based) and live (real-time Kite data) modes.
 * Polls every 15s in live mode and triggers alerts on signal flips.
 */
class StockAnalysis {
  constructor() {
    this._pollTimer = null;
    this._lastSignal = {};   // { equity: 'BULLISH', options: 'CALL' }
    this._lastScore  = {};   // { equity: 72, options: 68 }
    this._isLive     = false;
    this.POLL_MS     = 15000; // 15-second polling interval
    this._currentSymbol = null;
    this._loadSignalState();
  }

  _loadSignalState() {
    try {
      const saved = localStorage.getItem('ts_analysis_signals');
      if (saved) {
        const data = JSON.parse(saved);
        this._lastSignal = data.signals || {};
        this._lastScore = data.scores || {};
      }
    } catch (e) {
      console.warn('Failed to load signal state:', e);
    }
  }

  _saveSignalState() {
    try {
      const data = {
        signals: this._lastSignal,
        scores: this._lastScore,
        timestamp: new Date().toISOString()
      };
      localStorage.setItem('ts_analysis_signals', JSON.stringify(data));
    } catch (e) {
      console.warn('Failed to save signal state:', e);
    }
  }

  // ── NSE Market Hours: Mon-Fri 9:00 AM – 3:30 PM IST ──
  isMarketOpen() {
    const now = new Date();
    // Convert to IST (UTC+5:30)
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);

    const day = ist.getDay(); // 0=Sun, 6=Sat
    const hours = ist.getHours();
    const mins = ist.getMinutes();
    const timeInMins = hours * 60 + mins;

    // Weekend check
    if (day === 0 || day === 6) return { open: false, reason: 'Weekend' };

    // Market hours: 9:00 (540 mins) to 15:30 (930 mins)
    if (timeInMins < 540) return { open: false, reason: 'Pre-Market — opens at 9:00 AM IST' };
    if (timeInMins >= 930) return { open: false, reason: 'Market closed — closed at 3:30 PM IST' };

    return { open: true };
  }

  // ── Public: run analysis ──
  async run(symbol, mode) {
    this._currentSymbol = symbol;
    this._isLive = (mode === 'live');
    this._stopPolling();

    // ── Block live mode when market is closed ──
    if (this._isLive) {
      const mkt = this.isMarketOpen();
      if (!mkt.open) {
        this._setStatus('warning',
          `🔒 <strong>Market is Closed</strong> — ${mkt.reason}.<br>` +
          `<span style="font-size:0.78rem;color:var(--text-muted);">` +
          `Live analysis requires an active NSE trading session (Mon–Fri, 9:00 AM – 3:30 PM IST).<br>` +
          `Use <strong>Pre-Market</strong> mode to analyse using saved historical data.</span>`
        );
        // Hide stop button since we're not polling
        const stopBtn = document.getElementById('btn-stop-analysis');
        if (stopBtn) stopBtn.style.display = 'none';
        return;
      }
    }

    this._setStatus('loading', `🔍 Analysing ${symbol} (${mode === 'live' ? 'Live' : 'Pre-Market'})...`);
    await this._fetchAndRender(symbol, mode);

    if (this._isLive) {
      this._pollTimer = setInterval(() => {
        // Re-check market hours on each poll — auto-stop if market closes mid-session
        const mkt = this.isMarketOpen();
        if (!mkt.open) {
          this.stop();
          this._setStatus('warning',
            `⏹ <strong>Market has closed</strong> — live polling stopped automatically.<br>` +
            `<span style="font-size:0.78rem;color:var(--text-muted);">Last analysis was based on live data. Switch to Pre-Market for historical analysis.</span>`
          );
          const stopBtn = document.getElementById('btn-stop-analysis');
          if (stopBtn) stopBtn.style.display = 'none';
          return;
        }
        this._fetchAndRender(symbol, 'live');
      }, this.POLL_MS);
    }
  }

  // ── Public: stop live polling ──
  stop() {
    this._stopPolling();
    this._isLive = false;
  }

  _stopPolling() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  // ── Core fetch + render ──
  async _fetchAndRender(symbol, mode) {
    try {
      // Fetch both stock-analysis and multi-timeframe in parallel
      const [resp, respMtf] = await Promise.all([
        fetch('/api/stock-analysis', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ symbol, mode })
        }),
        fetch('/api/multi-timeframe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ symbol, intervals: ['15minute', '60minute', 'day', 'week'] })
        }).catch(e => {
          console.warn('MTF fetch failed in parallel:', e);
          return null;
        })
      ]);

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        this._setStatus('error', `❌ ${err.error || 'Analysis failed'}`);
        return;
      }

      const data = await resp.json();
      if (!data.ohlcv || data.ohlcv.length < 20) {
        this._setStatus('warning', `⚠️ Not enough OHLCV data for ${symbol}. Connect Kite and try again.`);
        return;
      }

      // If MTF was fetched successfully, compute indicator alignment and pass to scoring engine
      if (respMtf && respMtf.ok) {
        const mtfData = await respMtf.json();
        data.mtfSignals = {};
        
        const intervals = ['15minute', '60minute'];
        for (const key of intervals) {
          const ohlcv = mtfData[`ohlcv_${key}`] || [];
          if (ohlcv.length >= 14) {
            const closes = ohlcv.map(c => c.close);
            const rsi = TI.computeRSI(closes, 14);
            const macd = TI.computeMACD(closes);
            const ema9 = TI.emaLast(closes, 9, closes[closes.length - 1]);
            const ema21 = TI.emaLast(closes, 21, closes[closes.length - 1]);

            const rsiSignal = rsi > 60 ? 'BULLISH' : rsi < 40 ? 'BEARISH' : 'NEUTRAL';
            const macdSignal = macd.histogram > 0 ? 'BULLISH' : macd.histogram < 0 ? 'BEARISH' : 'NEUTRAL';
            const emaSignal = ema9 > ema21 ? 'BULLISH' : ema9 < ema21 ? 'BEARISH' : 'NEUTRAL';

            const signals = [rsiSignal, macdSignal, emaSignal];
            const bullCount = signals.filter(s => s === 'BULLISH').length;
            const bearCount = signals.filter(s => s === 'BEARISH').length;
            const overall = bullCount >= 2 ? 'BULLISH' : bearCount >= 2 ? 'BEARISH' : 'NEUTRAL';

            data.mtfSignals[key] = {
              rsi: rsiSignal,
              macd: macdSignal,
              ema: emaSignal,
              overall
            };
          }
        }
        
        // Cache mtfData so _loadMultiTimeframe can render it immediately without another network request
        this._cachedMtfData = mtfData;
      }

      const result = this._score(data);
      this._checkSignalFlip(symbol, result);
      this._render(data, result, mode);
      this._setStatus('ok', mode === 'live'
        ? `🟢 Live — refreshes every 15s (next: ${new Date(Date.now() + this.POLL_MS).toLocaleTimeString()})`
        : `✅ Pre-Market analysis complete`);
    } catch (e) {
      this._setStatus('error', `❌ ${e.message}`);
    }
  }

  // ── Score using existing ScoringEngine ──
  _score(data) {
    const ohlcv   = data.ohlcv;
    const livSnap = data.snapshot || {};
    const closes  = ohlcv.map(c => c.close);
    const highs   = ohlcv.map(c => c.high);
    const lows    = ohlcv.map(c => c.low);
    const opens   = ohlcv.map(c => c.open);
    const volumes = ohlcv.map(c => c.volume);

    const isLive = !!(livSnap.ltp && livSnap.ltp > 0);

    // ── When market is NOT live, build a snapshot from historical OHLCV ──
    // This uses real saved market data, not mock/placeholder values
    let snap;
    if (isLive) {
      snap = livSnap;
    } else {
      const lastClose  = closes[closes.length - 1] || 0;
      const prevClose  = closes[closes.length - 2] || lastClose;
      const lastHigh   = highs[highs.length - 1] || 0;
      const lastLow    = lows[lows.length - 1] || 0;
      const lastVol    = volumes[volumes.length - 1] || 0;
      const changePct  = prevClose > 0 ? ((lastClose - prevClose) / prevClose) * 100 : 0;
      const avgPrice   = (lastHigh + lastLow + lastClose) / 3;

      snap = {
        ltp: lastClose,
        change_pct: changePct,
        volume: lastVol,
        avg_price: avgPrice,
        buy_qty: 0,     // genuinely unavailable from OHLCV
        sell_qty: 0,    // genuinely unavailable from OHLCV
        upper_circuit: 0,
        lower_circuit: 0,
        oi: 0,          // OI not in OHLCV
        oi_day_change: 0,
        futures: livSnap.futures || {},    // fallback to live snapshot futures from backend if present
        depth: {},
        atm_option: livSnap.atm_option || {},  // fallback to live snapshot option details from backend if present
        _fromHistory: true  // flag to indicate this is derived from saved data
      };
    }

    // ── Confidence ──
    const confidenceFields = ['ltp','volume','oi','buy_qty','futures'];
    const liveFilled = isLive ? confidenceFields.filter(k => snap[k]).length : 0;
    const confidence = isLive
      ? (liveFilled / confidenceFields.length * 100)
      : 0;
    const confidenceLabel = isLive
      ? (confidence >= 80 ? 'High' : confidence >= 40 ? 'Medium' : 'Low')
      : 'Historical';

    // ── Candlestick pattern detection ──
    const pattern = this._detectCandlePattern(opens, highs, lows, closes);

    // ── Support / Resistance from 52-week data ──
    const levels = this._computeSupportResistance(highs, lows, closes);

    // ── Build real data objects from backend response ──
    const analyst = data.analyst || {};

    // Fundamentals: only pass real fields if available from NSE
    const fundamentals = {};
    if (analyst.pe) fundamentals.pe = parseFloat(analyst.pe) || undefined;

    // Sector data: only pass if available
    const sectorData = {};
    if (analyst.sector) sectorData.sector = analyst.sector;

    // Options data: pass real OI from snapshot
    const optionsData = {};
    if (snap.oi) optionsData.oiChangePercent = snap.oi_day_change || 0;

    // ── Equity score (works fully from historical OHLCV) ──
    const equityInput = { closes, highs, lows, volumes, fundamentals, sectorData };
    const equityResult = scoringEngine.scoreEquity(equityInput);

    // ── Options score (uses live snapshot, or historical-derived snapshot) ──
    const optInput = {
      closes, highs, lows, volumes,
      fundamentals,
      snapshot: {
        ltp: snap.ltp,
        change_pct: snap.change_pct,
        volume: snap.volume,
        avg_price: snap.avg_price,
        buy_qty: snap.buy_qty,
        sell_qty: snap.sell_qty,
        circuit: { upper: snap.upper_circuit, lower: snap.lower_circuit },
        depth: snap.depth || {},
        futures: snap.futures || {},
        atm_option: snap.atm_option || {},
        isHistoricalSession: !isLive
      },
      optionsData: { ...optionsData, isHistoricalSession: !isLive }
    };
    const optResult = scoringEngine.scoreOptions(optInput);

    return {
      equity: equityResult,
      options: optResult,
      confidence, confidenceLabel,
      pattern, levels,
      isLive,
      dataSource: isLive ? 'Live Market' : 'Historical (Saved)',
      ltp: snap.ltp || closes[closes.length - 1],
      changePct: snap.change_pct || equityResult.changePercent,
      volume: snap.volume || volumes[volumes.length - 1],
      oi: snap.oi || 0,
      futures: (snap.futures && snap.futures.ltp) ? snap.futures : null,
      week52High: Math.max(...highs),
      week52Low: Math.min(...lows),
    };
  }

  // ── Signal flip detection → alert ──
  _checkSignalFlip(symbol, result) {
    const prevEq = this._lastSignal[symbol + '_eq'];
    const prevOp = this._lastSignal[symbol + '_op'];
    const curEq  = result.equity.direction;
    const curOp  = result.options.direction;

    if (prevEq && prevEq !== curEq) {
      try {
        alertEngine.trigger({
          stock: symbol,
          type: 'signal_flip',
          title: `🔄 Signal Flipped: ${symbol} Equity`,
          description: `${prevEq} → ${curEq} (Score: ${result.equity.total})`,
          price: result.ltp
        });
      } catch (e) {}
    }

    if (prevOp && prevOp !== curOp) {
      try {
        alertEngine.trigger({
          stock: symbol,
          type: 'signal_flip',
          title: `🔄 Options Signal Flipped: ${symbol}`,
          description: `${prevOp} → ${curOp} (Score: ${result.options.total})`,
          price: result.ltp
        });
      } catch (e) {}
    }

    this._lastSignal[symbol + '_eq'] = curEq;
    this._lastSignal[symbol + '_op'] = curOp;
    this._lastScore[symbol + '_eq']  = result.equity.total;
    this._lastScore[symbol + '_op']  = result.options.total;
    this._saveSignalState();
  }

  // ── Candlestick pattern detection ──
  _detectCandlePattern(opens, highs, lows, closes) {
    const len = closes.length;
    if (len < 3) return null;
    const o = opens[len - 1], h = highs[len - 1], l = lows[len - 1], c = closes[len - 1];
    const po = opens[len - 2], pc = closes[len - 2];
    const body = Math.abs(c - o);
    const range = h - l;
    const upperWick = h - Math.max(o, c);
    const lowerWick = Math.min(o, c) - l;

    if (range > 0 && body / range < 0.1) return { name: 'Doji', bias: 'neutral', icon: '⟺' };
    if (lowerWick > body * 2 && upperWick < body * 0.5 && c > o)
      return { name: 'Hammer', bias: 'bullish', icon: '🔨' };
    if (upperWick > body * 2 && lowerWick < body * 0.5 && c < o)
      return { name: 'Shooting Star', bias: 'bearish', icon: '⭐' };
    if (c > o && pc < po && c > po && o < pc)
      return { name: 'Bullish Engulfing', bias: 'bullish', icon: '🟢' };
    if (c < o && pc > po && c < po && o > pc)
      return { name: 'Bearish Engulfing', bias: 'bearish', icon: '🔴' };
    if (c > o && Math.abs(c - pc) < range * 0.03)
      return { name: 'Marubozu (Bull)', bias: 'bullish', icon: '📈' };
    if (c < o && Math.abs(c - pc) < range * 0.03)
      return { name: 'Marubozu (Bear)', bias: 'bearish', icon: '📉' };
    return null;
  }

  // ── Support / Resistance levels ──
  _computeSupportResistance(highs, lows, closes) {
    const recent = closes.slice(-252); // ~1 year of trading days
    if (recent.length < 20) return {};
    const h52 = Math.max(...highs.slice(-252));
    const l52 = Math.min(...lows.slice(-252));
    const lastClose = closes[closes.length - 1];
    const pivot = (highs[highs.length - 1] + lows[lows.length - 1] + lastClose) / 3;
    return {
      week52High: +h52.toFixed(2),
      week52Low:  +l52.toFixed(2),
      pivot:      +pivot.toFixed(2),
      r1:         +(2 * pivot - lows[lows.length - 1]).toFixed(2),
      s1:         +(2 * pivot - highs[highs.length - 1]).toFixed(2),
      distFromHigh: +(((h52 - lastClose) / h52) * 100).toFixed(1),
      distFromLow:  +(((lastClose - l52) / l52) * 100).toFixed(1),
    };
  }

  // ── Render the full analysis card ──
  _render(data, result, _mode) {
    const container = document.getElementById('analysis-result');
    if (!container) return;

    const analyst = data.analyst;
    const eq = result.equity;
    const op = result.options;
    const lvl = result.levels;

    // EMA 50/200 Crossover & Strength Card calculation
    const emaCross = data.ema_crossover;
    let emaCrossHtml = '';
    if (emaCross) {
      let badgeBg = 'rgba(120, 144, 156, 0.1)';
      let badgeColor = '#78909C';
      let borderCol = 'rgba(120, 144, 156, 0.2)';
      let statusText = 'Neutral Range';
      
      if (emaCross.status === 'BULLISH_APPROACHING') {
        badgeBg = 'rgba(38, 166, 154, 0.1)';
        badgeColor = '#26A69A';
        borderCol = 'rgba(38, 166, 154, 0.2)';
        statusText = 'Golden Cross Approaching';
      } else if (emaCross.status === 'BEARISH_APPROACHING') {
        badgeBg = 'rgba(239, 83, 80, 0.1)';
        badgeColor = '#EF5350';
        borderCol = 'rgba(239, 83, 80, 0.2)';
        statusText = 'Death Cross Approaching';
      } else if (emaCross.status === 'BULLISH_ACTIVE') {
        badgeBg = 'rgba(38, 166, 154, 0.2)';
        badgeColor = '#26A69A';
        borderCol = 'rgba(38, 166, 154, 0.3)';
        statusText = 'Golden Cross Active';
      } else if (emaCross.status === 'BEARISH_ACTIVE') {
        badgeBg = 'rgba(239, 83, 80, 0.2)';
        badgeColor = '#EF5350';
        borderCol = 'rgba(239, 83, 80, 0.3)';
        statusText = 'Death Cross Active';
      }
      
      emaCrossHtml = `
      <!-- EMA Crossover Strength Overlay Card -->
      <div style="background:var(--bg-glass); backdrop-filter:blur(8px); padding:12px 16px; border-radius:12px; border:1px solid rgba(255,255,255,0.08); margin-bottom:16px; display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; box-shadow:var(--shadow-sm);">
        <div style="display:flex; align-items:center; gap:10px;">
          <span style="font-size:1.35rem; display:inline-block; vertical-align:middle; filter:drop-shadow(0 2px 4px rgba(0,0,0,0.1));">🛡️</span>
          <div>
            <div style="font-weight:700; font-size:0.88rem; color:var(--text-primary);">Major Trend Strength (Daily EMA 50/200)</div>
            <div style="font-size:0.75rem; color:var(--text-secondary); margin-top:2px;">${emaCross.details}</div>
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <span style="padding:4px 12px; border-radius:20px; font-size:0.72rem; font-weight:700; background:${badgeBg}; color:${badgeColor}; border:1px solid ${borderCol};">${statusText}</span>
          <span style="padding:4px 12px; border-radius:20px; font-size:0.72rem; font-weight:600; background:rgba(255,255,255,0.06); color:var(--text-secondary); border:1px solid var(--border);">${emaCross.strength_label}</span>
        </div>
      </div>`;
    }

    const signalColor = (s) => s === 'BULLISH' || s === 'CALL' ? '#26A69A' : s === 'BEARISH' || s === 'PUT' ? '#EF5350' : '#78909C';
    const signalBg    = (s) => s === 'BULLISH' || s === 'CALL' ? 'rgba(38,166,154,0.1)' : s === 'BEARISH' || s === 'PUT' ? 'rgba(239,83,80,0.1)' : 'rgba(120,144,156,0.1)';
    const fmt = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits: d}) : '—';

    const getIvTier = (ivVal, cap) => {
      if (!ivVal) return { label: 'Low', color: '#26A69A' };
      if (cap === 'Large Cap') {
        if (ivVal < 20) return { label: 'Low', color: '#26A69A' };
        if (ivVal <= 35) return { label: 'Medium', color: '#FFA726' };
        return { label: 'High', color: '#EF5350' };
      } else if (cap === 'Mid Cap') {
        if (ivVal < 35) return { label: 'Low', color: '#26A69A' };
        if (ivVal <= 60) return { label: 'Medium', color: '#FFA726' };
        return { label: 'High', color: '#EF5350' };
      } else {
        // Small Cap / Default
        if (ivVal < 50) return { label: 'Low', color: '#26A69A' };
        if (ivVal <= 100) return { label: 'Medium', color: '#FFA726' };
        return { label: 'High', color: '#EF5350' };
      }
    };

    const getStockCap = (sym) => {
      const list = window.equityScreener?.getFNOUniverseSync() || window.equityScreener?.getStaticFNOUniverse() || [];
      const stock = list.find(s => s.symbol === sym);
      if (stock) {
        if (stock.cap === 'large') return 'Large Cap';
        if (stock.cap === 'mid') return 'Mid Cap';
        if (stock.cap === 'small') return 'Small Cap';
      }
      return 'Small Cap';
    };

    const confBadge = result.confidence >= 80 ? '#26A69A'
      : result.confidence >= 40 ? '#FFA726' : '#78909C';

    const patternHtml = result.pattern
      ? `<span class="tag" style="background:${signalBg(result.pattern.bias === 'bullish' ? 'BULLISH' : result.pattern.bias === 'bearish' ? 'BEARISH' : 'NEUTRAL')};color:${signalColor(result.pattern.bias === 'bullish' ? 'BULLISH' : result.pattern.bias === 'bearish' ? 'BEARISH' : 'NEUTRAL')};font-size:0.75rem;">${result.pattern.icon} ${result.pattern.name}</span>`
      : `<span class="tag" style="font-size:0.75rem;color:var(--text-muted)">—</span>`;

    const futuresHtml = result.futures
      ? `<div class="analysis-stat"><span class="stat-label">Fut LTP</span><span class="stat-value" style="font-size:0.9rem;">₹${fmt(result.futures.ltp)}</span></div>
         <div class="analysis-stat"><span class="stat-label">Fut Premium</span><span class="stat-value" style="font-size:0.9rem;color:${result.futures.premium >= 0 ? '#26A69A' : '#EF5350'}">${result.futures.premium >= 0 ? '+' : ''}${fmt(result.futures.premium_pct, 2)}%</span></div>
         <div class="analysis-stat"><span class="stat-label">Fut OI Chg</span><span class="stat-value" style="font-size:0.9rem;color:${result.futures.oi_change >= 0 ? '#26A69A' : '#EF5350'}">${result.futures.oi_change >= 0 ? '+' : ''}${fmt(result.futures.oi_change, 0)}</span></div>`
      : '';

    const analystHtml = analyst
      ? `<div class="analysis-stat"><span class="stat-label">Sector</span><span class="stat-value" style="font-size:0.78rem;">${analyst.sector || '—'}</span></div>
         <div class="analysis-stat"><span class="stat-label">52W High</span><span class="stat-value" style="font-size:0.85rem;color:#26A69A;">₹${fmt(analyst.week52High || result.week52High)}</span></div>
         <div class="analysis-stat"><span class="stat-label">52W Low</span><span class="stat-value" style="font-size:0.85rem;color:#EF5350;">₹${fmt(analyst.week52Low || result.week52Low)}</span></div>`
      : `<div class="analysis-stat"><span class="stat-label">52W High</span><span class="stat-value" style="color:#26A69A;">₹${fmt(result.week52High)}</span></div>
         <div class="analysis-stat"><span class="stat-label">52W Low</span><span class="stat-value" style="color:#EF5350;">₹${fmt(result.week52Low)}</span></div>`;

    const capLabel = getStockCap(data.symbol);
    const capBadge = capLabel
      ? `<span style="padding:4px 10px;border-radius:20px;font-size:0.7rem;font-weight:600;background:rgba(30,136,229,0.1);color:#1E88E5;border:1px solid rgba(30,136,229,0.2);">${capLabel}</span>`
      : '';

    container.innerHTML = `
      <!-- Header row -->
      <div style="display:flex;align-items:center;gap:16px;margin-bottom:20px;flex-wrap:wrap;">
        <div>
          <h2 style="font-size:1.6rem;font-family:var(--font-display);margin:0;">${data.symbol}</h2>
          <div style="display:flex;gap:8px;align-items:center;margin-top:4px;">
            <span style="font-size:1.1rem;font-weight:600;">₹${fmt(result.ltp)}</span>
            <span style="font-size:0.85rem;color:${result.changePct >= 0 ? '#26A69A' : '#EF5350'};">
              ${result.changePct >= 0 ? '▲' : '▼'} ${Math.abs(result.changePct).toFixed(2)}%
            </span>
            ${patternHtml}
          </div>
        </div>
        <div style="margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          ${capBadge}
          <span style="padding:4px 10px;border-radius:20px;font-size:0.7rem;font-weight:600;background:${confBadge}22;color:${confBadge};border:1px solid ${confBadge}44;">
            ${result.isLive ? '⚡' : '📊'} ${result.dataSource}
          </span>
          ${result.isLive ? `<span class="tag tag-bullish" style="font-size:0.7rem;">🔴 LIVE</span>` : `<span style="padding:4px 10px;border-radius:20px;font-size:0.65rem;font-weight:500;background:rgba(120,144,156,0.1);color:#78909C;border:1px solid rgba(120,144,156,0.2);">Based on ${data.ohlcv_count} days of saved OHLCV</span>`}
          <button id="analysis-watch-btn" onclick="window.watchlist?.toggle('${data.symbol}', this);"
            title="${window.watchlist?.has(data.symbol) ? 'Remove from Watchlist' : 'Add to Watchlist'}"
            style="background:none;border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:1rem;padding:4px 10px;color:var(--text-secondary);">${window.watchlist?.has(data.symbol) ? '⭐ Watching' : '☆ Watch'}</button>
        </div>
      </div>

      <!-- Two recommendation cards: Equity + Options -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
        <!-- Equity Card -->
        <div class="top-pick-card" style="background:${signalBg(eq.direction)};border:1.5px solid ${signalColor(eq.direction)}44;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
            <div>
               <div class="pick-symbol">${data.symbol}</div>
               <div class="pick-signal" style="color:${signalColor(eq.direction)};">
                 📈 Equity — ${eq.direction}
               </div>
            </div>
            <div class="score-badge ${eq.total >= 70 ? 'score-high' : eq.total >= 50 ? 'score-med' : 'score-low'}" style="width:52px;height:52px;font-size:1.1rem;">${Math.round(eq.total)}</div>
          </div>
          <div class="score-breakdown" style="margin-bottom:8px;">${scoringEngine.renderBreakdown(eq.factors)}</div>
          <div style="display:flex;gap:8px;font-size:0.72rem;color:var(--text-secondary);flex-wrap:wrap;">
            <span>RSI: ${eq.rsi?.toFixed(1)}</span>
            <span>ADX: ${eq.adx?.toFixed(1)}</span>
            <span>VolRatio: ${eq.volRatio?.toFixed(2)}x</span>
            <span>IV: <strong style="color:${getIvTier(op.iv, capLabel).color};">${getIvTier(op.iv, capLabel).label} (${op.iv?.toFixed(1) || '—'}%)</strong></span>
          </div>
          ${scoringEngine.renderRiskRow(eq.risk, eq.direction)}
        </div>

        <!-- Options Card -->
        <div class="top-pick-card" style="background:${signalBg(op.direction)};border:1.5px solid ${signalColor(op.direction)}44;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px;">
            <div>
               <div class="pick-symbol">${data.symbol}</div>
               <div class="pick-signal" style="color:${signalColor(op.direction)};">
                 ⚡ Options — ${op.direction}
               </div>
            </div>
            <div class="score-badge ${op.total >= 70 ? 'score-high' : op.total >= 50 ? 'score-med' : 'score-low'}" style="width:52px;height:52px;font-size:1.1rem;">${Math.round(op.total)}</div>
          </div>
          <div class="score-breakdown" style="margin-bottom:8px;">${scoringEngine.renderBreakdown(op.factors)}</div>
          <div style="display:flex;gap:8px;font-size:0.72rem;color:var(--text-secondary);flex-wrap:wrap;">
            <span>PCR: ${op.pcr?.toFixed(2) || '—'}</span>
            <span>IV: <strong style="color:${getIvTier(op.iv, capLabel).color};">${getIvTier(op.iv, capLabel).label} (${op.iv?.toFixed(1) || '—'}%)</strong></span>
            <span>OI: ${op.oiBuildUp || '—'}</span>
          </div>
          ${scoringEngine.renderRiskRow(op.risk, op.direction)}
        </div>
      </div>

      ${emaCrossHtml}

      <!-- Stats grid: OHLCV, live data, S/R, analyst -->
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:16px;">
        <div class="analysis-stat"><span class="stat-label">Volume</span><span class="stat-value" style="font-size:0.9rem;">${fmt(result.volume, 0)}</span></div>
        <div class="analysis-stat"><span class="stat-label">OI</span><span class="stat-value" style="font-size:0.9rem;">${result.oi ? fmt(result.oi, 0) : '—'}</span></div>
        <div class="analysis-stat"><span class="stat-label">Pivot</span><span class="stat-value" style="font-size:0.9rem;">₹${fmt(lvl?.pivot)}</span></div>
        <div class="analysis-stat"><span class="stat-label">R1</span><span class="stat-value" style="font-size:0.9rem;color:#26A69A;">₹${fmt(lvl?.r1)}</span></div>
        <div class="analysis-stat"><span class="stat-label">S1</span><span class="stat-value" style="font-size:0.9rem;color:#EF5350;">₹${fmt(lvl?.s1)}</span></div>
        <div class="analysis-stat"><span class="stat-label">From 52W High</span><span class="stat-value" style="font-size:0.9rem;color:#EF5350;">-${lvl?.distFromHigh || '—'}%</span></div>
        ${futuresHtml}
        ${analystHtml}
      </div>

      <!-- OHLCV count note -->
      <div style="font-size:0.72rem;color:var(--text-muted);text-align:right;padding-top:8px;border-top:1px solid rgba(255,255,255,0.06);">
        Based on ${data.ohlcv_count} trading days of data · Candlestick: ${result.pattern?.name || 'No clear pattern'}
        ${data.ohlcv_error ? ` · ⚠️ ${data.ohlcv_error}` : ''}
      </div>

      <!-- Earnings Badge -->
      <div id="analysis-earnings-badge" style="margin-top:12px;"></div>

      <!-- Multi-Timeframe Confluence Matrix -->
      <div id="analysis-mtf-section" style="margin-top:16px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;">
          <h4 style="font-family:var(--font-display);font-size:0.95rem;margin:0;">📊 Multi-Timeframe Confluence</h4>
          <span style="font-size:0.7rem;color:var(--text-muted);">Loading...</span>
        </div>
        <div id="mtf-matrix" style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.8rem;">
          ⏳ Fetching multi-timeframe data...
        </div>
      </div>`;

    container.style.display = 'block';

    // Load earnings badge asynchronously
    this._loadEarningsBadge(data.symbol);

    // Load multi-timeframe analysis asynchronously
    this._loadMultiTimeframe(data.symbol);
  }

  // ── Earnings Badge ──
  async _loadEarningsBadge(symbol) {
    const badgeEl = document.getElementById('analysis-earnings-badge');
    if (!badgeEl) return;
    try {
      const resp = await fetch('/api/earnings-calendar', { credentials: 'include' });
      if (!resp.ok) { badgeEl.innerHTML = ''; return; }
      const data = await resp.json();
      const earnings = (data.earnings || []).filter(e => e.symbol === symbol);
      if (earnings.length > 0) {
        const e = earnings[0];
        const daysText = e.daysUntil === 0 ? 'TODAY' : e.daysUntil === 1 ? 'Tomorrow' : `in ${e.daysUntil} days`;
        const urgency = e.daysUntil <= 3 ? '#EF5350' : e.daysUntil <= 7 ? '#FFA726' : '#26A69A';
        badgeEl.innerHTML = `
          <div style="display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:10px;background:${urgency}11;border:1px solid ${urgency}33;margin-top:4px;">
            <span style="font-size:1.4rem;">📅</span>
            <div>
              <div style="font-weight:700;font-size:0.85rem;color:${urgency};">Earnings ${daysText} — ${e.date}</div>
              <div style="font-size:0.72rem;color:var(--text-muted);">${e.purpose || 'Board Meeting / Results'}</div>
              <div style="font-size:0.68rem;color:var(--text-muted);margin-top:2px;">⚠️ Options premiums may be elevated pre-earnings</div>
            </div>
          </div>`;
      } else {
        badgeEl.innerHTML = '';
      }
    } catch (e) {
      badgeEl.innerHTML = '';
    }
  }

  // ── Multi-Timeframe Confluence ──
  async _loadMultiTimeframe(symbol) {
    const matrixEl = document.getElementById('mtf-matrix');
    const sectionEl = document.getElementById('analysis-mtf-section');
    if (!matrixEl || !sectionEl) return;
    const statusSpan = sectionEl.querySelector('span');

    try {
      let data;
      if (this._cachedMtfData && this._cachedMtfData.symbol === symbol) {
        data = this._cachedMtfData;
      } else {
        const resp = await fetch('/api/multi-timeframe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ symbol, intervals: ['15minute', '60minute', 'day', 'week'] })
        });

        if (!resp.ok) {
          const errText = await resp.text();
          matrixEl.innerHTML = `<span style="color:var(--text-muted);font-size:0.78rem;">Multi-timeframe load failed: ${errText || resp.statusText}</span>`;
          if (statusSpan) statusSpan.textContent = 'Error';
          return;
        }

        data = await resp.json();
      }
      const intervals = [
        { key: '15minute', label: '15 Min' },
        { key: '60minute', label: '1 Hour' },
        { key: 'day', label: 'Daily' },
        { key: 'week', label: 'Weekly' }
      ];
      const indicators = ['RSI', 'MACD', 'EMA Trend', 'Overall'];

      // Compute indicators for each interval
      const results = {};
      for (const iv of intervals) {
        const ohlcv = data[`ohlcv_${iv.key}`] || [];
        if (ohlcv.length < 14) {
          results[iv.key] = { rsi: null, macd: null, ema: null, overall: null };
          continue;
        }
        const closes = ohlcv.map(c => c.close);
        const rsi = TI.computeRSI(closes, 14);
        const macd = TI.computeMACD(closes);
        const ema9 = TI.emaLast(closes, 9, closes[closes.length - 1]);
        const ema21 = TI.emaLast(closes, 21, closes[closes.length - 1]);
        const rsiSignal = rsi > 60 ? 'BULLISH' : rsi < 40 ? 'BEARISH' : 'NEUTRAL';
        const macdSignal = macd.histogram > 0 ? 'BULLISH' : macd.histogram < 0 ? 'BEARISH' : 'NEUTRAL';
        const emaSignal = ema9 > ema21 ? 'BULLISH' : ema9 < ema21 ? 'BEARISH' : 'NEUTRAL';

        // Overall: majority vote
        const signals = [rsiSignal, macdSignal, emaSignal];
        const bullCount = signals.filter(s => s === 'BULLISH').length;
        const bearCount = signals.filter(s => s === 'BEARISH').length;
        const overall = bullCount >= 2 ? 'BULLISH' : bearCount >= 2 ? 'BEARISH' : 'NEUTRAL';

        results[iv.key] = {
          rsi: { value: rsi, signal: rsiSignal },
          macd: { value: macd.histogram, signal: macdSignal },
          ema: { signal: emaSignal },
          overall
        };
      }

      // Helper for structural trend & chop detection
      const getSingleTimeframeStructure = (ohlcv) => {
        if (!ohlcv || ohlcv.length < 25) return { status: 'CHOPPY', reason: 'Insufficient data' };

        const highs = ohlcv.map(d => d.high);
        const lows = ohlcv.map(d => d.low);
        const closes = ohlcv.map(d => d.close);

        // 1. Dreiss Choppiness Index (CHOP)
        const chop = TI.computeCHOP(highs, lows, closes, 14);
        if (chop > 55) {
          return { status: 'CHOPPY', reason: `CHOP Index is ${chop.toFixed(0)} (High Choppiness)` };
        }

        // 2. Count EMA Crossovers in last 20 candles (indicator of chop/whipsaw)
        const ema9 = TI.computeEMA(closes, 9);
        const ema21 = TI.computeEMA(closes, 21);
        let crossCount = 0;
        for (let i = closes.length - 20; i < closes.length; i++) {
          if (i > 0 && ema9[i] !== null && ema21[i] !== null && ema9[i-1] !== null && ema21[i-1] !== null) {
            const prevDiff = ema9[i-1] - ema21[i-1];
            const currDiff = ema9[i] - ema21[i];
            if (prevDiff * currDiff < 0) {
              crossCount++;
            }
          }
        }
        if (crossCount >= 2) {
          return { status: 'CHOPPY', reason: `Whipsawing: ${crossCount} EMA Crosses in last 20 candles` };
        }

        // 3. Check HH, HL, LH, LL from last 3 pivots
        const pivots = TI.computePivots(highs, lows, 3);
        const validPivots = pivots.filter(p => p !== null);
        if (validPivots.length >= 4) {
          const lastPivots = validPivots.slice(-4);
          const phs = lastPivots.filter(p => p.type === 'PH');
          const pls = lastPivots.filter(p => p.type === 'PL');
          if (phs.length >= 2 && pls.length >= 2) {
            const ph1 = phs[phs.length - 2].price;
            const ph2 = phs[phs.length - 1].price;
            const pl1 = pls[pls.length - 2].price;
            const pl2 = pls[pls.length - 1].price;
            const isBullish = (ph2 > ph1) && (pl2 > pl1);
            const isBearish = (ph2 < ph1) && (pl2 < pl1);
            const latestEma9 = ema9[closes.length - 1];
            const latestEma21 = ema21[closes.length - 1];
            if (isBullish && latestEma9 > latestEma21) {
              return { status: 'BULLISH', reason: 'HH & HL structure confirmed' };
            }
            if (isBearish && latestEma9 < latestEma21) {
              return { status: 'BEARISH', reason: 'LH & LL structure confirmed' };
            }
          }
        }

        return { status: 'CHOPPY', reason: 'No clear HH/HL or LH/LL structure' };
      };

      // Compute 1hr and 15m structural alignment
      const ohlcv15 = data['ohlcv_15minute'] || [];
      const ohlcv60 = data['ohlcv_60minute'] || [];
      let struct15 = { status: 'CHOPPY', reason: 'No data' };
      let struct60 = { status: 'CHOPPY', reason: 'No data' };

      if (ohlcv15.length >= 25) {
        struct15 = getSingleTimeframeStructure(ohlcv15);
      }
      if (ohlcv60.length >= 25) {
        struct60 = getSingleTimeframeStructure(ohlcv60);
      }

      let structAlignMsg = '';
      if (struct15.status === 'CHOPPY' || struct60.status === 'CHOPPY') {
        structAlignMsg = `<div style="text-align:center;padding:10px;background:rgba(255,167,38,0.1);border-radius:8px;color:#FFA726;font-weight:700;font-size:0.8rem;margin-top:10px;border:1.5px solid rgba(255,167,38,0.25);">
          ⚠️ Structure Alignment: <span style="text-transform:uppercase;letter-spacing:0.05em;color:#FFA726;background:rgba(255,167,38,0.15);padding:2px 8px;border-radius:4px;font-size:0.75rem;margin-left:4px;">No Trade</span> (Choppy Market Structure)
          <div style="font-size:0.68rem;font-weight:normal;color:var(--text-secondary);margin-top:6px;font-family:var(--font-mono, monospace);">
            15m: ${struct15.reason} | 1h: ${struct60.reason}
          </div>
        </div>`;
      } else if (struct15.status === struct60.status) {
        const isBull = struct15.status === 'BULLISH';
        structAlignMsg = `<div style="text-align:center;padding:10px;background:${isBull ? 'rgba(38,166,154,0.1)' : 'rgba(239,83,80,0.1)'};border-radius:8px;color:${isBull ? '#26A69A' : '#EF5350'};font-weight:700;font-size:0.8rem;margin-top:10px;border:1.5px solid ${isBull ? 'rgba(38,166,154,0.25)' : 'rgba(239,83,80,0.25)'};">
          ${isBull ? '🟢' : '🔴'} Structure Alignment: ${struct15.status} ALIGNED (1h &amp; 15m)
          <div style="font-size:0.68rem;font-weight:normal;color:var(--text-secondary);margin-top:6px;">
            Both timeframes show aligned structural progression (${isBull ? 'Higher Highs &amp; Higher Lows' : 'Lower Highs &amp; Lower Lows'})
          </div>
        </div>`;
      } else {
        structAlignMsg = `<div style="text-align:center;padding:10px;background:rgba(120,144,156,0.1);border-radius:8px;color:#90A4AE;font-weight:700;font-size:0.8rem;margin-top:10px;border:1.5px solid rgba(120,144,156,0.25);">
          ⚪ Structure Alignment: <span style="text-transform:uppercase;letter-spacing:0.05em;color:#90A4AE;background:rgba(120,144,156,0.15);padding:2px 8px;border-radius:4px;font-size:0.75rem;margin-left:4px;">No Trade</span> (Divergent Structure)
          <div style="font-size:0.68rem;font-weight:normal;color:var(--text-secondary);margin-top:6px;font-family:var(--font-mono, monospace);">
            15m structure is ${struct15.status} but 1h structure is ${struct60.status}
          </div>
        </div>`;
      }

      // Check confluence — all timeframes agree
      const overalls = intervals.map(iv => results[iv.key]?.overall).filter(Boolean);
      const allBullish = overalls.every(s => s === 'BULLISH');
      const allBearish = overalls.every(s => s === 'BEARISH');
      let confluenceMsg = '';
      if (allBullish) confluenceMsg = '<div style="text-align:center;padding:8px;background:rgba(38,166,154,0.1);border-radius:8px;color:#26A69A;font-weight:700;font-size:0.82rem;margin-top:8px;">🟢 STRONG CONFLUENCE — All timeframes align BULLISH</div>';
      else if (allBearish) confluenceMsg = '<div style="text-align:center;padding:8px;background:rgba(239,83,80,0.1);border-radius:8px;color:#EF5350;font-weight:700;font-size:0.82rem;margin-top:8px;">🔴 STRONG CONFLUENCE — All timeframes align BEARISH</div>';

      const signalColor = (s) => s === 'BULLISH' ? '#26A69A' : s === 'BEARISH' ? '#EF5350' : '#78909C';
      const signalBg = (s) => s === 'BULLISH' ? 'rgba(38,166,154,0.12)' : s === 'BEARISH' ? 'rgba(239,83,80,0.12)' : 'rgba(120,144,156,0.08)';
      const signalIcon = (s) => s === 'BULLISH' ? '▲' : s === 'BEARISH' ? '▼' : '—';

      matrixEl.innerHTML = `
        <div style="overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:0.78rem;">
            <thead>
              <tr style="border-bottom:1px solid rgba(255,255,255,0.1);">
                <th style="text-align:left;padding:8px 12px;color:var(--text-secondary);font-weight:600;">Timeframe</th>
                ${indicators.map(ind => `<th style="text-align:center;padding:8px 10px;color:var(--text-secondary);font-weight:600;">${ind}</th>`).join('')}
              </tr>
            </thead>
            <tbody>
              ${intervals.map(iv => {
                const r = results[iv.key];
                if (!r || !r.rsi) {
                  return `<tr><td style="padding:8px 12px;font-weight:600;">${iv.label}</td><td colspan="4" style="text-align:center;color:var(--text-muted);padding:8px;">Insufficient data</td></tr>`;
                }
                return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
                  <td style="padding:8px 12px;font-weight:700;">${iv.label}</td>
                  <td style="text-align:center;padding:6px;"><span style="padding:3px 10px;border-radius:6px;background:${signalBg(r.rsi.signal)};color:${signalColor(r.rsi.signal)};font-weight:600;">${signalIcon(r.rsi.signal)} ${r.rsi.value?.toFixed(0)}</span></td>
                  <td style="text-align:center;padding:6px;"><span style="padding:3px 10px;border-radius:6px;background:${signalBg(r.macd.signal)};color:${signalColor(r.macd.signal)};font-weight:600;">${signalIcon(r.macd.signal)} ${r.macd.signal}</span></td>
                  <td style="text-align:center;padding:6px;"><span style="padding:3px 10px;border-radius:6px;background:${signalBg(r.ema.signal)};color:${signalColor(r.ema.signal)};font-weight:600;">${signalIcon(r.ema.signal)} ${r.ema.signal}</span></td>
                  <td style="text-align:center;padding:6px;"><span style="padding:4px 12px;border-radius:6px;background:${signalBg(r.overall)};color:${signalColor(r.overall)};font-weight:700;font-size:0.82rem;">${signalIcon(r.overall)} ${r.overall}</span></td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
        ${confluenceMsg}
        ${structAlignMsg}`;

      // Update section header
      sectionEl.querySelector('span').textContent = overalls.length > 0 ? `${overalls.length} intervals analysed` : 'No data';

    } catch (e) {
      matrixEl.innerHTML = `<span style="color:var(--text-muted);font-size:0.78rem;">⚠️ ${e.message}</span>`;
      if (statusSpan) statusSpan.textContent = 'Error';
    }
  }

  // ── Technical Indicator Helpers — delegated to shared TI module ──
  _computeRSI(closes, period = 14) { return TI.computeRSI(closes, period); }
  _computeMACD(closes) { return TI.computeMACD(closes); }
  _computeEMA(closes, period) { return TI.emaLast(closes, period, closes[closes.length - 1]); }

  _setStatus(type, msg) {
    const el = document.getElementById('analysis-status');
    if (!el) return;
    const colors = { loading: '#FFA726', ok: '#26A69A', error: '#EF5350', warning: '#FFA726' };
    el.innerHTML = `<span style="color:${colors[type] || '#78909C'};font-size:0.82rem;">${msg}</span>`;
  }
}

window.stockAnalysis = new StockAnalysis();

/**
 * AnalysisSignalChart — Standalone intraday signal chart for the Stock Analysis page.
 * Creates its own LightweightCharts instance; does NOT share chartManager with historical page.
 *
 * Live session (9:15 AM – 3:30 PM IST, Mon–Fri):
 *   - Polls every 30 s; updates the forming bar and appends new bars incrementally.
 *   - Re-computes SMC + confluence signals and refreshes the signal log on every poll.
 *   - Auto-stops at 3:30 PM; shows a pulsing LIVE badge.
 * Outside live hours: shows the previous close session (static).
 */
class AnalysisSignalChart {
  constructor() {
    this.chart         = null;
    this.candleSeries  = null;
    this.volumeSeries  = null;
    this._emaLines     = {};        // { 9: lineSeries, 21: lineSeries }
    this._resizeObs    = null;
    this._pollTimer    = null;
    this._sessionWatcher = null;    // fires every minute to detect session open/close
    this._currentSymbol   = null;
    this._currentInterval = '5minute';
    this._ohlcvFull    = [];        // full multi-day history (for SMC pivot lookback)
    this._sessionDate  = null;      // 'YYYY-MM-DD' of currently displayed session
    this._loading      = false;     // guard against concurrent load() calls
    this.POLL_MS       = 30000;     // 30-second refresh during live session
    
    // EMA preset configurations
    this._emaPresets   = {
      'preset1': { periods: [5, 9, 21],    colors: ['#FFD700', '#42A5F5', '#FFA726'] },
      'preset2': { periods: [9, 21, 50],   colors: ['#42A5F5', '#FFA726', '#00BCD4'] }
    };
    this._selectedEmaPreset = 'preset2';  // default preset: 9, 21, 50
    
    // Wire up EMA preset button listeners
    this._initEmaPresetListeners();
  }

  // ── Initialize EMA preset button listeners ──
  _initEmaPresetListeners() {
    const preset1Btn = document.getElementById('btn-ema-preset1');
    const preset2Btn = document.getElementById('btn-ema-preset2');
    
    if (preset1Btn) {
      preset1Btn.addEventListener('click', () => {
        this._selectEmaPreset('preset1');
      });
    }
    
    if (preset2Btn) {
      preset2Btn.addEventListener('click', () => {
        this._selectEmaPreset('preset2');
      });
    }
  }

  // ── Select an EMA preset and update UI ──
  _selectEmaPreset(presetKey) {
    if (!this._emaPresets[presetKey]) return;
    
    this._selectedEmaPreset = presetKey;
    
    // Update button active states
    document.querySelectorAll('[id^="btn-ema-preset"]').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.preset === presetKey);
    });
    
    // Re-render chart with new EMA lines if we have data loaded
    if (this.candleSeries && this._ohlcvFull.length > 0) {
      this._render(this._ohlcvFull, this._filterLastSession(this._ohlcvFull));
    }
  }

  // ── NSE live session: Mon–Fri 9:15 AM – 3:30 PM IST ──
  isLiveSession() {
    const now = new Date();
    const ist = new Date(now.getTime() + (now.getTimezoneOffset() + 330) * 60000);
    const day = ist.getDay();
    if (day === 0 || day === 6) return false;
    const mins = ist.getHours() * 60 + ist.getMinutes();
    return mins >= 555 && mins < 930;   // 9:15 = 555, 15:30 = 930
  }

  // ── Public: initial load (resets chart, starts polling if live) ──
  async load(symbol, interval) {
    if (this._loading) return;   // prevent concurrent calls (e.g. from session watcher)
    this._loading = true;
    this._stopPolling();

    interval = interval || this._currentInterval;
    this._currentInterval = interval;
    this._currentSymbol   = symbol;

    // Sync interval tab UI
    document.querySelectorAll('#analysis-chart-interval-tabs .tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.interval === interval);
    });

    // Show card with loading placeholder (before chart init — prevents DOM clobber)
    const card = document.getElementById('analysis-chart-card');
    if (card) card.style.display = 'block';
    this._showLoading(`Loading ${interval === '5minute' ? '5m' : '15m'} chart for ${symbol}…`);
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    this._setLiveBadge(false);

    try {
      if (!kiteAPI.connected) {
        this._showError('Kite API not connected. Go to Settings → Connect first.');
        return;
      }

      // Fetch BEFORE init — so chart appears with data immediately (no blank flash)
      const ohlcv = await this._fetchOhlcv(symbol, interval);
      if (!ohlcv) return;

      const sessionOhlcv = this._filterLastSession(ohlcv);
      if (!sessionOhlcv.length) { this._showError('No data for the last trading session.'); return; }

      this._ohlcvFull   = ohlcv;
      this._sessionDate = sessionOhlcv[0].date.substring(0, 10);

      // Now init chart (clears container) and render immediately
      if (!this._initChart()) return;
      this._render(ohlcv, sessionOhlcv);

      // Start live polling if market is open, otherwise watch for session open
      if (this.isLiveSession()) {
        this._setLiveBadge(true);
        this._startPolling();
      }
      this._startSessionWatcher();
    } finally {
      this._loading = false;  // always release lock, even on early returns / errors
    }
  }

  // ── Public: stop live updates ──
  stop() {
    this._stopPolling();
    this._stopSessionWatcher();
    this._setLiveBadge(false, 'Stopped');
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Private
  // ─────────────────────────────────────────────────────────────────────────

  _startPolling() {
    this._pollTimer = setInterval(async () => {
      if (!this.isLiveSession()) {
        this._stopPolling();
        this._setLiveBadge(false, 'Market Closed');
        return;
      }
      await this._pollRefresh();
    }, this.POLL_MS);
  }

  _stopPolling() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    document.getElementById('btn-stop-signal-chart')?.style && (document.getElementById('btn-stop-signal-chart').style.display = 'none');
  }

  // ── Watch for session open/close while chart is visible ──
  // Fires every minute; auto-reloads to live mode when session opens,
  // and stops live polling when session closes.
  _startSessionWatcher() {
    this._stopSessionWatcher();
    this._sessionWatcher = setInterval(() => {
      const card = document.getElementById('analysis-chart-card');
      const visible = card && card.style.display !== 'none';
      if (!visible || !this._currentSymbol) return;

      if (this.isLiveSession() && !this._pollTimer) {
        // Session just opened — reload to pick up today's bars and go live
        this.load(this._currentSymbol, this._currentInterval);
      } else if (!this.isLiveSession() && this._pollTimer) {
        // Session just closed — stop polling, keep chart static
        this._stopPolling();
        this._setLiveBadge(false, `Closed ${new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' })}`);
      }
    }, 60000);
  }

  _stopSessionWatcher() {
    if (this._sessionWatcher) { clearInterval(this._sessionWatcher); this._sessionWatcher = null; }
  }

  // Incremental refresh: fetch latest data, smart-update bars, re-compute signals
  async _pollRefresh() {
    if (!this._currentSymbol || !this.candleSeries) return;

    const fresh = await this._fetchOhlcv(this._currentSymbol, this._currentInterval);
    if (!fresh || !fresh.length) return;

    const sessionFresh = this._filterLastSession(fresh);
    if (!sessionFresh.length) return;

    // ── Smart bar update ──
    const prevFull  = this._ohlcvFull;
    const toTime    = d => Math.floor(new Date(d.date).getTime() / 1000);

    // Map of already-known timestamps in the stored full history
    const knownTimes = new Set(prevFull.map(d => toTime(d)));

    let hasChanges = false;

    for (const bar of sessionFresh) {
      const t = toTime(bar);
      const candle = { time: t, open: bar.open, high: bar.high, low: bar.low, close: bar.close };
      const vol    = { time: t, value: bar.volume,
                       color: bar.close >= bar.open ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)' };

      if (!knownTimes.has(t)) {
        // Brand-new bar — append
        this.candleSeries.update(candle);
        this.volumeSeries.update(vol);
        hasChanges = true;
      } else {
        // Existing bar — may be the forming (last) bar; update if it changed
        const prev = prevFull.find(d => toTime(d) === t);
        if (prev && (prev.close !== bar.close || prev.high !== bar.high ||
                     prev.low  !== bar.low  || prev.volume !== bar.volume)) {
          this.candleSeries.update(candle);
          this.volumeSeries.update(vol);
          hasChanges = true;
        }
      }
    }

    // Update stored history
    this._ohlcvFull = fresh;

    if (!hasChanges) return;  // nothing new — skip expensive signal re-compute

    // ── Update EMA lines ──
    this._updateEmaLines(fresh, sessionFresh);

    // ── Re-compute signals ──
    this._applySignals(fresh, this._sessionDate);

    // Scroll to latest bar (only if user hasn't panned away significantly)
    try { this.chart.timeScale().scrollToRealTime(); } catch (e) {}
  }

  // ── Today's date in IST (YYYY-MM-DD) ──
  _istToday() {
    return new Date(Date.now() + 5.5 * 60 * 60 * 1000).toISOString().substring(0, 10);
  }

  // ── Date of exactly n trading sessions ago, skipping weekends (IST) ──
  // e.g. called on Monday → walks back to Thursday (skips Sun, Sat)
  _tradingSessionsAgo(n) {
    const d = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
    let counted = 0;
    while (counted < n) {
      d.setDate(d.getDate() - 1);
      const day = d.getDay();
      if (day !== 0 && day !== 6) counted++; // skip Sunday (0) and Saturday (6)
    }
    return d.toISOString().substring(0, 10);
  }

  // ── Fetch multi-day intraday OHLCV from Kite API ──
  async _fetchOhlcv(symbol, interval) {
    const token = kiteAPI.getInstrumentToken(symbol, 'NSE');
    if (!token) { this._showError(`Token not found for ${symbol}.`); return null; }

    const to   = this._istToday();
    const from = this._tradingSessionsAgo(2);

    try {
      const data    = await kiteAPI.getHistoricalData(token, from, to, interval);
      const candles = data.candles || data.data?.candles || data;
      if (!Array.isArray(candles) || !candles.length) {
        this._showError(`No intraday data returned for ${symbol}.`);
        return null;
      }
      return candles.map(c => {
        let date, open, high, low, close, volume;
        if (Array.isArray(c)) { [date, open, high, low, close, volume] = c; }
        else { ({ date, open, high, low, close, volume } = c); }
        return { date: typeof date === 'string' ? date : new Date(date).toISOString(),
                 open, high, low, close, volume };
      });
    } catch (e) {
      this._showError(`Failed to fetch data: ${e.message}`);
      return null;
    }
  }

  // ── Full render: set all series data + EMA + signals ──
  _render(ohlcv, sessionOhlcv) {
    const toTime = d => Math.floor(new Date(d.date).getTime() / 1000);

    // Candles + volume
    this.candleSeries.setData(sessionOhlcv.map(d => ({
      time: toTime(d), open: d.open, high: d.high, low: d.low, close: d.close
    })));
    this.volumeSeries.setData(sessionOhlcv.map(d => ({
      time: toTime(d), value: d.volume,
      color: d.close >= d.open ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)'
    })));

    // EMA lines from selected preset
    const preset = this._emaPresets[this._selectedEmaPreset];
    this._emaLines = {};
    if (preset) {
      for (let i = 0; i < preset.periods.length; i++) {
        const period = preset.periods[i];
        const color = preset.colors[i];
        this._buildEmaLine(ohlcv, sessionOhlcv, period, color);
      }
    }

    // Signals + log
    this._applySignals(ohlcv, this._sessionDate);

    this.chart.timeScale().fitContent();

    // Session label (use IST date, not UTC)
    const today = this._istToday();
    let label;
    if (this._sessionDate === today) {
      label = this.isLiveSession() ? 'Today — Live' : 'Today — Session Closed';
    } else {
      label = `Prev Close: ${this._sessionDate}`;
    }
    const labelEl = document.getElementById('analysis-chart-session-label');
    if (labelEl) labelEl.textContent = label;
  }

  // ── Build / replace an EMA line series ──
  _buildEmaLine(fullOhlcv, sessionOhlcv, period, color) {
    if (!this.chart || fullOhlcv.length < period) return;
    const lineData = this._computeEmaLineData(fullOhlcv, sessionOhlcv, period);
    if (!lineData.length) return;
    const line = this.chart.addLineSeries({
      color, lineWidth: 1, crosshairMarkerVisible: false, priceLineVisible: false,
      title: `EMA${period}`
    });
    line.setData(lineData);
    this._emaLines[period] = line;
  }

  // ── Update existing EMA line data after a poll ──
  _updateEmaLines(fullOhlcv, sessionOhlcv) {
    for (const [period, line] of Object.entries(this._emaLines)) {
      const lineData = this._computeEmaLineData(fullOhlcv, sessionOhlcv, Number(period));
      if (lineData.length) {
        try { line.setData(lineData); } catch (e) {}
      }
    }
  }

  // ── Compute EMA values mapped to session timestamps ──
  _computeEmaLineData(fullOhlcv, sessionOhlcv, period) {
    const closes = fullOhlcv.map(d => d.close);
    const k = 2 / (period + 1);
    let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
    const emaMap = {};
    for (let i = period - 1; i < closes.length; i++) {
      if (i > period - 1) ema = closes[i] * k + ema * (1 - k);
      emaMap[fullOhlcv[i].date] = ema;
    }
    const toTime = d => Math.floor(new Date(d.date).getTime() / 1000);
    return sessionOhlcv
      .filter(d => emaMap[d.date] !== undefined)
      .map(d => ({ time: toTime(d), value: emaMap[d.date] }));
  }

  // ── Compute signals for both EMA presets and render split CALL/PUT logs ──
  _applySignals(fullOhlcv, sessionDate) {
    const sessionOhlcv = fullOhlcv.filter(d => String(d.date).substring(0, 10) === sessionDate);
    if (!sessionOhlcv.length) return;

    const presetResults = {
      preset1: this._computePresetSignalSet(sessionOhlcv, 'preset1'),
      preset2: this._computePresetSignalSet(sessionOhlcv, 'preset2')
    };

    const selectedResult = presetResults[this._selectedEmaPreset] || presetResults.preset2;
    const renderSignals = selectedResult.signals || [];
    
    // Show markers from ALL presets with signals, not just the selected one
    const allMarkers = [
      ...(presetResults.preset1?.markers || []),
      ...(presetResults.preset2?.markers || [])
    ];

    try {
      this.candleSeries.setMarkers(
        [...allMarkers].sort((a, b) => (a.time || 0) - (b.time || 0))
      );
    } catch (e) { console.warn('AnalysisSignalChart markers:', e.message); }

    this._renderSignalUI(presetResults, selectedResult);
  }

  _computePresetSignalSet(sessionOhlcv, presetKey) {
    if (!window.chartSignalEngine || !sessionOhlcv.length) {
      return { markers: [], signals: [], summary: { totalSignals: 0, calls: 0, puts: 0, strongSignals: 0, highSignals: 0 } };
    }

    const chartResult = chartSignalEngine.computeSignals(sessionOhlcv, { emaPreset: presetKey });
    
    // Normalize marker times to Unix timestamps (seconds) matching candle times
    const toTime = d => Math.floor(new Date(d.date || d).getTime() / 1000);
    const normalizedMarkers = (chartResult.markers || []).map(m => {
      let markerTime = m.time;
      // If marker time is a date string, convert to Unix timestamp
      if (typeof markerTime === 'string') {
        markerTime = toTime(markerTime);
      } else if (typeof markerTime === 'number' && markerTime > 1e10) {
        // If it's milliseconds (>10 billion), convert to seconds
        markerTime = Math.floor(markerTime / 1000);
      }
      return { ...m, time: markerTime };
    });
    
    const normalizedSignals = (chartResult.signals || [])
      .map(sig => {
        const type = sig.type === 'BUY' ? 'CALL' : sig.type === 'SELL' ? 'PUT' : sig.type;
        const strength = sig.strength === 'HIGH CONVICTION' ? 'HIGH' : sig.strength;
        return {
          ...sig,
          type,
          direction: type,
          strength: strength || 'MOD',
          price: sig.price != null ? sig.price : sig.entry || 0,
          time: typeof sig.time === 'number'
            ? new Date(sig.time * 1000).toLocaleTimeString('en-IN', { hour12: false })
            : sig.time
        };
      })
      .filter(sig => sig.type === 'CALL' || sig.type === 'PUT');

    const calls = normalizedSignals.filter(s => s.type === 'CALL').length;
    const puts = normalizedSignals.filter(s => s.type === 'PUT').length;

    return {
      markers: normalizedMarkers,
      signals: normalizedSignals,
      summary: {
        totalSignals: normalizedSignals.length,
        calls,
        puts,
        callRatio: calls + puts > 0 ? ((calls / (calls + puts)) * 100).toFixed(1) : '—',
        strongSignals: normalizedSignals.filter(s => s.strength === 'HIGH' || s.strength === 'AGGRESSIVE').length,
        highSignals: normalizedSignals.filter(s => s.strength === 'HIGH').length,
      }
    };
  }

  // ── Render signal summary + log into analysis page elements ──
  _renderSignalUI(signalsByPreset, selectedResult) {
    if (!selectedResult) return;

    // For summary: show whichever preset has the most signals (best signal producer)
    const preset1Signals = signalsByPreset.preset1?.signals || [];
    const preset2Signals = signalsByPreset.preset2?.signals || [];
    const summaryPreset = preset1Signals.length >= preset2Signals.length 
      ? signalsByPreset.preset1 
      : signalsByPreset.preset2;

    const summaryEl = document.getElementById('analysis-signal-summary');
    if (summaryEl) {
      summaryEl.style.display = 'grid';
      const calls = summaryPreset.signals.filter(s => s.type === 'CALL').length;
      const puts = summaryPreset.signals.filter(s => s.type === 'PUT').length;
      const totalSignals = calls + puts;
      document.getElementById('as-total').textContent = totalSignals;
      document.getElementById('as-buys').textContent = calls;
      document.getElementById('as-sells').textContent = puts;
      const wr = totalSignals > 0 ? ((calls / totalSignals) * 100).toFixed(1) : '—';
      const wrEl = document.getElementById('as-winrate');
      wrEl.textContent = `${wr}%`;
      wrEl.style.color = wr !== '—' && parseFloat(wr) >= 50 ? '#1E88E5' : '#EF5350';
      document.getElementById('as-strong').textContent = summaryPreset.signals.filter(s => s.strength === 'STRONG' || s.strength === 'HIGH').length;
      const pnlEl = document.getElementById('as-pnl');
      pnlEl.textContent = '—';
      pnlEl.style.color = 'var(--text-muted)';
    }

    const logCard = document.getElementById('analysis-signal-log-card');
    if (!logCard) return;
    logCard.style.display = 'block';

    const logBody = document.getElementById('analysis-signal-log-body');
    const logCount = document.getElementById('analysis-signal-log-count');
    if (logBody) {
      const sections = [
        { key: 'preset1', title: 'EMA 5,9,21' },
        { key: 'preset2', title: 'EMA 9,21,50' }
      ];

      logBody.innerHTML = sections.map(section => {
        const signals = signalsByPreset[section.key]?.signals || [];
        const countText = `${signals.length} signal${signals.length !== 1 ? 's' : ''}`;
        const rows = signals.length > 0 ? signals.map(sig => {
          const priceText = sig.price != null ? `₹${sig.price.toFixed(2)}` : '—';
          const tags = Array.isArray(sig.tags) ? sig.tags.join(' · ') : '';
          return `
            <div class="signal-log-entry" style="display:flex;gap:10px;padding:10px;border-bottom:1px solid var(--border);align-items:center;">
              <div style="font-weight:700;width:44px;text-align:center;color:${sig.type === 'CALL' ? '#1E88E5' : '#EF5350'};">
                ${sig.type === 'CALL' ? '↑ C' : '↓ P'}
              </div>
              <div style="flex:1;font-size:0.8rem;">
                <div style="font-weight:600;color:var(--text-primary);">${priceText} ${sig.direction}</div>
                <div style="color:var(--text-muted);font-size:0.72rem;">Score: ${sig.score?.toFixed ? sig.score.toFixed(1) : sig.score} · ${sig.strength}</div>
                <div style="color:var(--text-muted);font-size:0.7rem;">${tags}</div>
              </div>
              <div style="color:var(--text-muted);font-size:0.72rem;white-space:nowrap;">${sig.time || '—'}</div>
            </div>`;
        }).join('') : `<div style="color:var(--text-muted);font-size:0.82rem;padding:10px 0;">No ${section.title} signals detected.</div>`;

        return `<div style="margin-bottom:18px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <div style="font-weight:700;color:var(--text-primary);">${section.title}</div>
            <div style="font-size:0.8rem;color:var(--text-muted);">${countText}</div>
          </div>
          ${rows}
        </div>`;
      }).join('');
    }
    if (logCount) {
      logCount.textContent = `Selected preset: ${this._selectedEmaPreset === 'preset1' ? 'EMA 5,9,21' : 'EMA 9,21,50'} · EMA 5,9,21: ${signalsByPreset.preset1.signals.length} · EMA 9,21,50: ${signalsByPreset.preset2.signals.length}`;
    }
  }

  // ── Filter OHLCV to the most recent closed/live trading session ──
  // When not in a live session, prefer the previous completed trading day.
  _filterLastSession(ohlcv) {
    if (!ohlcv || ohlcv.length === 0) return [];
    const today = this._istToday();
    const grouped = ohlcv.reduce((acc, bar) => {
      const dateKey = String(bar.date).substring(0, 10);
      if (!acc[dateKey]) acc[dateKey] = [];
      acc[dateKey].push(bar);
      return acc;
    }, {});

    // Use today's bars only when the session is currently live.
    if (this.isLiveSession() && grouped[today] && grouped[today].length > 0) {
      return grouped[today];
    }

    const dates = Object.keys(grouped).sort();
    const priorDates = dates.filter(d => d !== today);
    const fallbackDate = priorDates.length ? priorDates[priorDates.length - 1] : dates[dates.length - 1];
    return fallbackDate ? grouped[fallbackDate] : [];
  }

  // ── Create / recreate chart in container ──
  _initChart() {
    const container = document.getElementById('analysis-chart-container');
    if (!container) return false;

    if (this.chart) {
      try { this.chart.remove(); } catch (e) {}
      this.chart = null; this.candleSeries = null; this.volumeSeries = null; this._emaLines = {};
    }
    if (this._resizeObs) { this._resizeObs.disconnect(); this._resizeObs = null; }
    container.innerHTML = '';

    this.chart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight || 420,
      layout: {
        background: { color: '#FFFFFF' }, textColor: '#546E7A',
        fontFamily: "'Inter', sans-serif", fontSize: 12
      },
      localization: {
        // Timestamps are UTC epoch seconds; shift +5:30 h to display as IST
        timeFormatter: (ts) => {
          const ist = new Date((ts + 19800) * 1000); // 19800 = 5.5 * 3600
          return `${String(ist.getUTCHours()).padStart(2,'0')}:${String(ist.getUTCMinutes()).padStart(2,'0')}`;
        }
      },
      grid: {
        vertLines: { color: 'rgba(21,101,192,0.04)' },
        horzLines: { color: 'rgba(21,101,192,0.04)' }
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: 'rgba(30,136,229,0.3)', width: 1, style: 2, labelBackgroundColor: '#1E88E5' },
        horzLine: { color: 'rgba(30,136,229,0.3)', width: 1, style: 2, labelBackgroundColor: '#1E88E5' }
      },
      rightPriceScale: { borderColor: 'rgba(21,101,192,0.1)', scaleMargins: { top: 0.08, bottom: 0.22 } },
      timeScale: {
        borderColor: 'rgba(21,101,192,0.1)',
        timeVisible: true,
        secondsVisible: false,
        // Format bottom-axis tick labels as IST (UTC+5:30)
        tickMarkFormatter: (ts) => {
          const ist = new Date((ts + 19800) * 1000);
          return `${String(ist.getUTCHours()).padStart(2,'0')}:${String(ist.getUTCMinutes()).padStart(2,'0')}`;
        }
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true }
    });

    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: '#26A69A', downColor: '#EF5350',
      borderUpColor: '#26A69A', borderDownColor: '#EF5350',
      wickUpColor: '#26A69A', wickDownColor: '#EF5350'
    });
    this.volumeSeries = this.chart.addHistogramSeries({
      priceFormat: { type: 'volume' }, priceScaleId: 'vol', scaleMargins: { top: 0.82, bottom: 0 }
    });

    this._resizeObs = new ResizeObserver(() => {
      if (this.chart && container.clientWidth > 0)
        this.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight || 420 });
    });
    this._resizeObs.observe(container);
    return true;
  }

  // ── Live badge + stop button ──
  _setLiveBadge(live, note) {
    const badge  = document.getElementById('analysis-chart-live-badge');
    const stopBtn = document.getElementById('btn-stop-signal-chart');
    if (badge) badge.style.display = live ? 'inline-block' : 'none';
    if (stopBtn) stopBtn.style.display = live ? 'inline-flex' : 'none';

    const labelEl = document.getElementById('analysis-chart-session-label');
    if (labelEl && !live && note) labelEl.textContent = note;

    // Wire Stop button once
    if (live && stopBtn && !stopBtn._wired) {
      stopBtn._wired = true;
      stopBtn.addEventListener('click', () => this.stop());
    }
  }

  _showLoading(msg) {
    const c = document.getElementById('analysis-chart-container');
    if (c) c.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#90A4AE;font-family:'Inter',sans-serif;font-size:0.85rem;">${msg}</div>`;
  }

  _showError(msg) {
    const c = document.getElementById('analysis-chart-container');
    if (c) c.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px;color:#546E7A;font-family:'Inter',sans-serif;">
      <div style="font-size:2rem;">⚠️</div>
      <div style="font-size:0.82rem;max-width:380px;text-align:center;color:#90A4AE;">${msg}</div>
    </div>`;
    console.warn('AnalysisSignalChart:', msg);
  }
}

window.analysisSignalChart = new AnalysisSignalChart();

// ─────────────────────────────────────────────
//  Stock Analysis — Bulk Scan Controller
//  (reuses /api/historical-analytics-bulk backend)
// ─────────────────────────────────────────────

let _saBulkResults = [];

/** Toggle the bulk panel open/closed */
window.saBulkToggle = function () {
  const panel = document.getElementById('sa-bulk-panel');
  const icon  = document.getElementById('sa-bulk-toggle-icon');
  if (!panel) return;
  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  icon.textContent = isOpen ? '▼ Expand' : '▲ Collapse';
};

/** Quick-fill textarea with F&O universe by cap tier */
window.saBulkFillCap = function (cap) {
  const ta = document.getElementById('sa-bulk-symbols');
  if (!ta) return;

  let stocks = [];
  if (window.equityScreener) {
    try {
      stocks = equityScreener.getFNOUniverseSync()
        .filter(s => s.cap === cap)
        .map(s => s.symbol);
    } catch (e) { /* fallback below */ }
  }

  if (!stocks.length) {
    const CAPS = {
      large: ['RELIANCE','TCS','INFY','HDFCBANK','ICICIBANK','SBIN','BAJFINANCE','LT',
               'HINDUNILVR','ITC','AXISBANK','KOTAKBANK','MARUTI','TATAMOTORS','SUNPHARMA',
               'WIPRO','BHARTIARTL','ASIANPAINT','TATASTEEL','HINDALCO','JSWSTEEL',
               'ADANIENT','ADANIPORTS','POWERGRID','NTPC','COALINDIA','ONGC','BPCL',
               'DRREDDY','CIPLA'],
      mid:   ['TRENT','COFORGE','KAYNES','PERSISTENT','MPHASIS','ZYDUSLIFE','JUBLFOOD',
               'PIIND','PAGEIND','DIXON','POLYCAB','LALPATHLAB','METROPOLIS','IRCTC',
               'GLAND','DEEPAKNTR','AAVAS','HOMEFIRST','CAMS','ANGELONE'],
      small: ['IDFCFIRSTB','RBLBANK','BANDHANBNK','FEDERALBNK','KARURVYSYA',
               'CENTURYTEX','GNFC','GHCL','ATUL','NAVINFLUOR','FINEORG',
               'ROUTE','LATENTVIEW','TARSONS','HAPPYMIND']
    };
    stocks = CAPS[cap] || [];
  }

  if (!stocks.length) { alert(`No ${cap} cap stocks found.`); return; }
  ta.value = stocks.join(', ');

  ta.style.borderColor = cap === 'large' ? '#1E88E5' : cap === 'mid' ? '#FFA726' : '#26A69A';
  setTimeout(() => { ta.style.borderColor = ''; }, 800);
};

/** Run bulk scan */
window.saBulkRun = async function () {
  const rawInput = (document.getElementById('sa-bulk-symbols')?.value || '').trim();
  if (!rawInput) { alert('Please enter at least one stock symbol.'); return; }

  const symbols = [...new Set(
    rawInput.split(/[\s,;\n]+/).map(s => s.trim().toUpperCase()).filter(Boolean)
  )];
  if (!symbols.length)  { alert('No valid symbols found.'); return; }
  if (symbols.length > 100) { alert('Maximum 100 symbols allowed.'); return; }

  const runBtn   = document.getElementById('btn-sa-bulk-run');
  const csvBtn   = document.getElementById('btn-sa-bulk-csv');
  const progWrap = document.getElementById('sa-bulk-progress-wrap');
  const progBar  = document.getElementById('sa-bulk-progress-bar');
  const progLbl  = document.getElementById('sa-bulk-progress-label');
  const progCnt  = document.getElementById('sa-bulk-progress-count');
  const resultEl = document.getElementById('sa-bulk-results');

  if (runBtn)  { runBtn.disabled = true; runBtn.textContent = '⏳ Scanning…'; }
  if (csvBtn)  { csvBtn.disabled = true; csvBtn.style.opacity = '0.5'; csvBtn.style.cursor = 'not-allowed'; }
  if (progWrap) progWrap.style.display = 'block';
  if (progBar)  progBar.style.width = '5%';
  if (progLbl)  progLbl.textContent = `Scanning ${symbols.length} stocks…`;
  if (progCnt)  progCnt.textContent = `0 / ${symbols.length}`;
  if (resultEl) resultEl.style.display = 'none';

  try {
    const resp = await fetch('/api/historical-analytics-bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ symbols, days: 90 })   // Pre-Market uses 90-day window
    });

    if (progBar) progBar.style.width = '90%';

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Bulk scan failed: ${err.error || resp.statusText}`);
      return;
    }

    const data = await resp.json();
    _saBulkResults = data.results || [];

    if (progBar) progBar.style.width = '100%';
    if (progCnt) progCnt.textContent = `${_saBulkResults.length} / ${symbols.length}`;
    if (progLbl) progLbl.textContent = `Scan complete — ${_saBulkResults.length} stocks processed`;

    _renderSaBulkTable(_saBulkResults);

    if (csvBtn) { csvBtn.disabled = false; csvBtn.style.opacity = '1'; csvBtn.style.cursor = 'pointer'; }

  } catch (e) {
    alert(`Network error: ${e.message}`);
  } finally {
    if (runBtn) { runBtn.disabled = false; runBtn.textContent = '⚡ Run Bulk Scan'; }
  }
};

/** Render results table (Stock Analysis page uses sa- prefixed IDs) */
function _renderSaBulkTable(results) {
  const tbody   = document.getElementById('sa-bulk-rows');
  const wrapper = document.getElementById('sa-bulk-results');
  const countEl = document.getElementById('sa-bulk-results-count');
  const tsEl    = document.getElementById('sa-bulk-results-ts');
  if (!tbody || !wrapper) return;

  const sc  = (s) => s && s.includes('BULLISH') ? '#26A69A' : s && s.includes('BEARISH') ? '#EF5350' : '#78909C';
  const bg  = (s) => s && s.includes('BULLISH') ? 'rgba(38,166,154,0.12)' : s && s.includes('BEARISH') ? 'rgba(239,83,80,0.12)' : 'rgba(120,144,156,0.08)';
  const tag = (label, status) => `<span style="padding:2px 8px;border-radius:6px;font-weight:700;font-size:0.72rem;background:${bg(status)};color:${sc(status)};">${label}</span>`;

  tbody.innerHTML = results.map(r => {
    if (r.status && r.status.startsWith('ERROR')) {
      return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
        <td style="padding:8px 12px;font-weight:700;">${r.symbol}</td>
        <td colspan="17" style="padding:8px;text-align:center;color:#EF5350;font-size:0.72rem;">${r.status}</td>
      </tr>`;
    }

    const streakLabel = r.current_streak_dir === 'climb'
      ? `🟢 +${r.current_streak_days}d`
      : r.current_streak_dir === 'drop' ? `🔴 -${r.current_streak_days}d` : '—';

    const emaCrossShort = {
      'BULLISH_ACTIVE': '✅ GX Active', 'BULLISH_APPROACHING': '⚠️ GX Near',
      'BEARISH_ACTIVE': '🔴 DX Active', 'BEARISH_APPROACHING': '⚠️ DX Near'
    }[r.ema_cross_status] || '—';

    const scoreColor = r.score >= 70 ? '#26A69A' : r.score >= 55 ? '#FFA726' : r.score < 45 ? '#EF5350' : '#78909C';

    return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
      <td style="padding:8px 12px;font-weight:800;color:var(--text-primary);">${r.symbol}</td>
      <td style="padding:8px 10px;text-align:center;"><span style="font-weight:800;color:${scoreColor};">${r.score}</span></td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.direction, r.direction)}</td>
      <td style="padding:8px 10px;text-align:right;font-weight:600;">₹${(r.ltp || 0).toLocaleString('en-IN')}</td>
      <td style="padding:8px 10px;text-align:center;color:${r.rsi > 60 ? '#26A69A' : r.rsi < 40 ? '#EF5350' : '#FFA726'};">${r.rsi}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.macd_signal, r.macd_signal)}</td>
      <td style="padding:8px 10px;text-align:center;color:${r.chop_state === 'TRENDING' ? '#AB47BC' : '#78909C'};">${r.chop} <span style="font-size:0.65rem;">${r.chop_state}</span></td>
      <td style="padding:8px 10px;text-align:center;font-size:0.72rem;">${emaCrossShort}</td>
      <td style="padding:8px 10px;text-align:center;color:${r.ema_strength?.startsWith('+') ? '#26A69A' : '#EF5350'};font-weight:700;">${r.ema_strength}</td>
      <td style="padding:8px 10px;text-align:center;">${r.gap_up_fade_pct}</td>
      <td style="padding:8px 10px;text-align:center;">${r.gap_down_fade_pct}</td>
      <td style="padding:8px 10px;text-align:center;color:#26A69A;font-weight:600;">${r.bull_backtest_win_rate}</td>
      <td style="padding:8px 10px;text-align:center;color:#EF5350;font-weight:600;">${r.bear_backtest_win_rate}</td>
      <td style="padding:8px 10px;text-align:center;">${streakLabel}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.daily_trend || 'NEUTRAL', r.daily_trend || 'NEUTRAL')}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.hourly_trend || 'NEUTRAL', r.hourly_trend || 'NEUTRAL')}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.m15_trend || 'NEUTRAL', r.m15_trend || 'NEUTRAL')}</td>
      <td style="padding:8px 10px;text-align:center;"><span style="padding:2px 6px;border-radius:4px;font-size:0.65rem;font-weight:700;background:rgba(38,166,154,0.1);color:#26A69A;">${r.status}</span></td>
    </tr>`;
  }).join('');

  const ok   = results.filter(r => !r.status?.startsWith('ERROR')).length;
  const errs = results.length - ok;
  if (countEl) countEl.textContent = `${results.length} stocks scanned — ${ok} OK${errs > 0 ? `, ${errs} errors` : ''}`;
  if (tsEl)    tsEl.textContent = `Generated at ${new Date().toLocaleTimeString()} · 90-day window`;
  wrapper.style.display = 'block';
}

/** CSV download for Stock Analysis bulk results */
window.saBulkDownloadCSV = function () {
  if (!_saBulkResults || !_saBulkResults.length) return;

  const headers = [
    'Symbol','Status','Days Analysed','Score','Direction','LTP',
    'RSI','MACD Signal','CHOP','CHOP State',
    'EMA 50','EMA 200','EMA Cross Status','EMA Cross Gap %','EMA Strength',
    'Gap Up Count','Gap Up Fade %','Gap Down Count','Gap Down Fade %',
    'Bull Backtest Triggers','Bull Win Rate %','Bear Backtest Triggers','Bear Win Rate %',
    'Current Streak Dir','Current Streak Days','Daily Trend','Hourly Trend','15m Trend'
  ];

  const rows = _saBulkResults.map(r => [
    r.symbol, r.status, r.days_analysed||'', r.score||'', r.direction||'', r.ltp||'',
    r.rsi||'', r.macd_signal||'', r.chop||'', r.chop_state||'',
    r.ema_50||'', r.ema_200||'', r.ema_cross_status||'', r.ema_cross_gap_pct||'', r.ema_strength||'',
    r.gap_up_count||0, r.gap_up_fade_pct||'', r.gap_down_count||0, r.gap_down_fade_pct||'',
    r.bull_backtest_triggers||0, r.bull_backtest_win_rate||'',
    r.bear_backtest_triggers||0, r.bear_backtest_win_rate||'',
    r.current_streak_dir||'', r.current_streak_days||0, r.daily_trend||'', r.hourly_trend||'', r.m15_trend||''
  ].map(v => `"${String(v).replace(/"/g, '""')}"`));

  const csv  = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = `TradeSignal_StockAnalysis_Bulk_${new Date().toISOString().slice(0,10)}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};
