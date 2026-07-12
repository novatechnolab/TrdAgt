/**
 * TradeSignal — Backtesting Engine
 * Signal-based backtest on historical OHLCV data.
 * Entry rules: EMA crossover, RSI breakout, ATR breakout.
 * Outputs: Equity curve, trade log, Sharpe ratio, max drawdown.
 */
class Backtester {
  constructor() {
    this._results = null;
    this._chart = null;
  }

  // ── Render backtest UI ──
  render() {
    const container = document.getElementById('backtest-content');
    if (!container) return;
    container.innerHTML = `
      <!-- Config panel -->
      <div class="card mb-16" style="padding:16px;">
        <div style="font-weight:600;font-size:0.9rem;margin-bottom:14px;">⚙️ Backtest Configuration</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;">
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Symbol</label>
            <input type="text" id="bt-symbol" list="bt-symbol-list" placeholder="e.g. RELIANCE"
              style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
            <datalist id="bt-symbol-list"></datalist>
          </div>
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Lookback (days)</label>
            <select id="bt-range" style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
              <option value="90">3 Months</option>
              <option value="180" selected>6 Months</option>
              <option value="365">1 Year</option>
              <option value="730">2 Years</option>
            </select>
          </div>
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Entry Signal</label>
            <select id="bt-signal" style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
              <option value="ema_cross">EMA 9/21 Crossover</option>
              <option value="rsi_breakout">RSI Breakout (&lt;30 buy / &gt;70 sell)</option>
              <option value="atr_breakout">ATR Breakout (1.5x)</option>
              <option value="macd_cross">MACD Signal Cross</option>
            </select>
          </div>
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Exit After (days)</label>
            <input type="number" id="bt-hold" value="5" min="1" max="30"
              style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
          </div>
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Stop Loss (%)</label>
            <input type="number" id="bt-sl" value="2" min="0.5" max="10" step="0.5"
              style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
          </div>
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Take Profit (%)</label>
            <input type="number" id="bt-tp" value="4" min="0.5" max="20" step="0.5"
              style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
          </div>
          <div>
            <label style="display:block;font-size:0.72rem;font-weight:600;color:var(--text-secondary);margin-bottom:4px;">Capital (₹)</label>
            <input type="number" id="bt-capital" value="100000" step="10000"
              style="width:100%;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
          </div>
        </div>
        <div style="margin-top:14px;display:flex;gap:10px;">
          <button class="btn btn-primary" id="bt-run-btn" onclick="backtester.run()">▶ Run Backtest</button>
          <button class="btn btn-secondary" onclick="backtester.exportCSV()">⬇ Export CSV</button>
        </div>
      </div>

      <!-- Results area -->
      <div id="bt-results" style="display:none;">
        <!-- Stats grid -->
        <div id="bt-stats-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px;"></div>

        <!-- Equity curve canvas -->
        <div class="card mb-16" style="padding:16px;">
          <div style="font-weight:600;font-size:0.85rem;margin-bottom:10px;">📈 Equity Curve</div>
          <canvas id="bt-equity-canvas" width="900" height="250"
            style="width:100%;border-radius:8px;background:#FAFBFC;border:1px solid var(--border);"></canvas>
        </div>

        <!-- Trade log -->
        <div class="card" style="padding:16px;">
          <div style="font-weight:600;font-size:0.85rem;margin-bottom:10px;">📋 Trade Log</div>
          <div style="overflow-x:auto;">
            <table class="data-table" id="bt-trade-table">
              <thead><tr><th>#</th><th>Entry Date</th><th>Entry Price</th><th>Exit Date</th><th>Exit Price</th><th>P&L</th><th>Return %</th><th>Exit Reason</th></tr></thead>
              <tbody id="bt-trade-body"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div id="bt-empty" class="empty-state">
        <div class="empty-icon">🧪</div>
        <h4>Run a Backtest</h4>
        <p>Configure your signal, symbol, and risk parameters above, then click Run Backtest to simulate historical performance.</p>
      </div>`;

    // Populate datalist
    const dl = document.getElementById('bt-symbol-list');
    if (dl && window.equityScreener) {
      equityScreener.getFNOUniverseSync().forEach(s => {
        const o = document.createElement('option'); o.value = s.symbol; dl.appendChild(o);
      });
    }
  }

  async run() {
    const symbol   = document.getElementById('bt-symbol')?.value?.trim().toUpperCase();
    const rangeDays = parseInt(document.getElementById('bt-range')?.value) || 180;
    const signal   = document.getElementById('bt-signal')?.value || 'ema_cross';
    const holdDays = parseInt(document.getElementById('bt-hold')?.value)  || 5;
    const slPct    = parseFloat(document.getElementById('bt-sl')?.value)  || 2;
    const tpPct    = parseFloat(document.getElementById('bt-tp')?.value)  || 4;
    const capital  = parseFloat(document.getElementById('bt-capital')?.value) || 100000;

    if (!symbol) { alert('Enter a symbol.'); return; }
    if (!kiteAPI.connected) { alert('Connect to Kite API first.'); return; }

    const btn = document.getElementById('bt-run-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Loading data...'; }

    try {
      // Fetch historical data via chartManager
      const token = kiteAPI.getInstrumentToken(symbol, 'NSE');
      if (!token) throw new Error(`Instrument token not found for ${symbol}`);

      const to = new Date().toISOString().split('T')[0];
      const fromDate = new Date(); fromDate.setDate(fromDate.getDate() - rangeDays);
      const from = fromDate.toISOString().split('T')[0];

      const rawData = await kiteAPI.getHistoricalData(token, from, to, 'day');
      const candles  = rawData.candles || rawData.data?.candles || rawData;
      if (!Array.isArray(candles) || candles.length < 30) throw new Error('Insufficient historical data (need 30+ days)');

      const ohlcv = candles.map(c => {
        const [date, open, high, low, close, volume] = Array.isArray(c) ? c : [c.date, c.open, c.high, c.low, c.close, c.volume];
        return { date: typeof date === 'string' ? date.split('T')[0] : String(date), open, high, low, close, volume };
      });

      if (btn) btn.textContent = '⏳ Running simulation...';

      const results = this._simulate(ohlcv, signal, holdDays, slPct, tpPct, capital);
      this._results = results;
      this._render(results, symbol, signal);
    } catch (e) {
      alert(`Backtest failed: ${e.message}`);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '▶ Run Backtest'; }
    }
  }

  // ── Core simulation engine ──
  _simulate(ohlcv, signal, holdDays, slPct, tpPct, capital) {
    const closes = ohlcv.map(d => d.close);
    const highs  = ohlcv.map(d => d.high);
    const lows   = ohlcv.map(d => d.low);

    // Compute indicators
    const ema9  = this._ema(closes, 9);
    const ema21 = this._ema(closes, 21);
    const rsi   = this._rsi(closes, 14);
    const macd  = this._macd(closes);
    const atr   = this._atr(highs, lows, closes, 14);

    const trades = [];
    let equity = capital;
    const equityCurve = [{ date: ohlcv[0].date, value: capital }];
    let inTrade = false;
    let entryIdx, entryPrice, direction;

    for (let i = 30; i < ohlcv.length; i++) {
      if (inTrade) {
        const holding = i - entryIdx;
        const price = ohlcv[i].close;
        const change = direction === 'Long'
          ? (price - entryPrice) / entryPrice * 100
          : (entryPrice - price) / entryPrice * 100;

        let exitReason = null;
        if (change <= -slPct)  exitReason = 'Stop Loss';
        else if (change >= tpPct) exitReason = 'Take Profit';
        else if (holding >= holdDays) exitReason = 'Time Exit';

        if (exitReason) {
          const pnlPct = direction === 'Long'
            ? (price - entryPrice) / entryPrice
            : (entryPrice - price) / entryPrice;
          const posSize = equity * 0.95;
          const pnl = posSize * pnlPct;
          equity += pnl;

          trades.push({
            entryDate:  ohlcv[entryIdx].date, entryPrice,
            exitDate:   ohlcv[i].date,        exitPrice: price,
            pnl: +pnl.toFixed(2), returnPct: +(pnlPct * 100).toFixed(2),
            direction, exitReason
          });

          equityCurve.push({ date: ohlcv[i].date, value: +equity.toFixed(2) });
          inTrade = false;
        }
        continue;
      }

      // Entry signals
      let sig = null;
      if (signal === 'ema_cross') {
        const prev9 = ema9[i-1], cur9 = ema9[i], prev21 = ema21[i-1], cur21 = ema21[i];
        if (prev9 && prev21 && cur9 && cur21) {
          if (prev9 < prev21 && cur9 > cur21) sig = 'Long';
          if (prev9 > prev21 && cur9 < cur21) sig = 'Short';
        }
      } else if (signal === 'rsi_breakout') {
        if (rsi[i-1] < 30 && rsi[i] >= 30) sig = 'Long';
        if (rsi[i-1] > 70 && rsi[i] <= 70) sig = 'Short';
      } else if (signal === 'atr_breakout') {
        const move = Math.abs(ohlcv[i].close - ohlcv[i-1].close);
        if (atr[i] && move > 1.5 * atr[i]) {
          sig = ohlcv[i].close > ohlcv[i-1].close ? 'Long' : 'Short';
        }
      } else if (signal === 'macd_cross') {
        const pm = macd[i-1], cm = macd[i];
        if (pm && cm) {
          if (pm.hist < 0 && cm.hist >= 0) sig = 'Long';
          if (pm.hist > 0 && cm.hist <= 0) sig = 'Short';
        }
      }

      if (sig) {
        inTrade = true; entryIdx = i; entryPrice = ohlcv[i].close; direction = sig;
      }
    }

    // Compute stats
    const wins = trades.filter(t => t.pnl > 0);
    const losses = trades.filter(t => t.pnl <= 0);
    const totalReturn = (equity - capital) / capital * 100;
    const annualReturn = totalReturn / (ohlcv.length / 252);
    const winRate = trades.length ? wins.length / trades.length * 100 : 0;
    const avgWin  = wins.length  ? wins.reduce((s, t) => s + t.returnPct, 0)  / wins.length  : 0;
    const avgLoss = losses.length? losses.reduce((s, t) => s + t.returnPct, 0)/ losses.length: 0;
    const profitFactor = losses.length && losses.reduce((s,t) => s + Math.abs(t.pnl), 0) > 0
      ? wins.reduce((s,t) => s + t.pnl, 0) / Math.abs(losses.reduce((s,t) => s + t.pnl, 0)) : Infinity;

    // Max drawdown
    let peak = capital, maxDD = 0, runVal = capital;
    equityCurve.forEach(p => {
      runVal = p.value;
      if (runVal > peak) peak = runVal;
      const dd = (peak - runVal) / peak * 100;
      if (dd > maxDD) maxDD = dd;
    });

    // Sharpe ratio (simplified)
    const returns = trades.map(t => t.returnPct / 100);
    const avgReturn = returns.length ? returns.reduce((s,r) => s+r, 0) / returns.length : 0;
    const stdDev = returns.length > 1 ? Math.sqrt(returns.reduce((s,r) => s + (r-avgReturn)**2, 0) / returns.length) : 1;
    const sharpe = stdDev > 0 ? (avgReturn / stdDev * Math.sqrt(252)).toFixed(2) : '—';

    return {
      trades, equityCurve, capital, finalEquity: equity,
      totalReturn: +totalReturn.toFixed(2), annualReturn: +annualReturn.toFixed(2),
      winRate: +winRate.toFixed(1), avgWin: +avgWin.toFixed(2), avgLoss: +avgLoss.toFixed(2),
      profitFactor: typeof profitFactor === 'number' ? +profitFactor.toFixed(2) : '∞',
      maxDD: +maxDD.toFixed(2), sharpe, totalTrades: trades.length
    };
  }

  // ── Indicator helpers ──
  _ema(closes, period) {
    const k = 2/(period+1), result = new Array(period-1).fill(null);
    let ema = closes.slice(0,period).reduce((a,b)=>a+b,0)/period;
    result.push(ema);
    for (let i=period; i<closes.length; i++) { ema = closes[i]*k + ema*(1-k); result.push(ema); }
    return result;
  }

  _rsi(closes, period = 14) {
    const result = new Array(period).fill(null);
    let gains = 0, losses = 0;
    for (let i=1; i<=period; i++) {
      const d = closes[i] - closes[i-1];
      d > 0 ? gains += d : losses -= d;
    }
    let ag = gains/period, al = losses/period;
    result.push(al === 0 ? 100 : 100 - 100/(1 + ag/al));
    for (let i=period+1; i<closes.length; i++) {
      const d = closes[i] - closes[i-1];
      ag = (ag*(period-1) + Math.max(d,0)) / period;
      al = (al*(period-1) + Math.max(-d,0)) / period;
      result.push(al === 0 ? 100 : 100 - 100/(1 + ag/al));
    }
    return result;
  }

  _macd(closes, fast=12, slow=26, signal=9) {
    const emaF = this._ema(closes, fast);
    const emaS = this._ema(closes, slow);
    const macdLine = closes.map((_, i) => (emaF[i] != null && emaS[i] != null) ? emaF[i] - emaS[i] : null);
    const validMacd = macdLine.filter(v => v != null);
    const sigLine = this._ema(validMacd, signal);
    const result = new Array(macdLine.length).fill(null);
    let si = 0;
    for (let i=0; i<macdLine.length; i++) {
      if (macdLine[i] != null && si < sigLine.length && sigLine[si] != null) {
        result[i] = { macd: macdLine[i], signal: sigLine[si], hist: macdLine[i] - sigLine[si] };
        si++;
      }
    }
    return result;
  }

  _atr(highs, lows, closes, period=14) {
    const result = [highs[0]-lows[0]];
    for (let i=1; i<closes.length; i++) {
      const tr = Math.max(highs[i]-lows[i], Math.abs(highs[i]-closes[i-1]), Math.abs(lows[i]-closes[i-1]));
      result.push(result[result.length-1] * (period-1)/period + tr/period);
    }
    return result;
  }

  // ── Render results ──
  _render(res, symbol, signal) {
    document.getElementById('bt-empty').style.display = 'none';
    const results = document.getElementById('bt-results');
    results.style.display = 'block';

    // Stats
    const pnl = res.finalEquity - res.capital;
    const grid = document.getElementById('bt-stats-grid');
    const stats = [
      { label: 'Total Return',  value: `${res.totalReturn >= 0 ? '+' : ''}${res.totalReturn}%`,  color: res.totalReturn >= 0 ? '#26A69A' : '#EF5350' },
      { label: 'Ann. Return',   value: `${res.annualReturn >= 0 ? '+' : ''}${res.annualReturn}%`, color: res.annualReturn >= 0 ? '#26A69A' : '#EF5350' },
      { label: 'Net P&L',       value: `${pnl >= 0 ? '+' : ''}₹${Math.abs(pnl).toFixed(0)}`,    color: pnl >= 0 ? '#26A69A' : '#EF5350' },
      { label: 'Win Rate',      value: `${res.winRate}%`,      color: res.winRate >= 50 ? '#26A69A' : '#EF5350' },
      { label: 'Total Trades',  value: res.totalTrades,        color: '#1E88E5' },
      { label: 'Avg Win',       value: `+${res.avgWin}%`,      color: '#26A69A' },
      { label: 'Avg Loss',      value: `${res.avgLoss}%`,      color: '#EF5350' },
      { label: 'Profit Factor', value: res.profitFactor,       color: res.profitFactor > 1 ? '#26A69A' : '#EF5350' },
      { label: 'Max Drawdown',  value: `${res.maxDD}%`,        color: '#EF5350' },
      { label: 'Sharpe Ratio',  value: res.sharpe,             color: +res.sharpe >= 1 ? '#26A69A' : '#78909C' },
    ];
    if (grid) grid.innerHTML = stats.map(s => `
      <div class="card" style="padding:10px;">
        <div style="font-size:0.65rem;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted);">${s.label}</div>
        <div style="font-size:1rem;font-weight:700;margin-top:4px;color:${s.color};">${s.value}</div>
      </div>`).join('');

    this._drawEquityCurve(res.equityCurve, res.capital);
    this._renderTradeLog(res.trades);
  }

  _drawEquityCurve(curve, startCapital) {
    const canvas = document.getElementById('bt-equity-canvas');
    if (!canvas || curve.length < 2) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const pad = { top: 20, right: 30, bottom: 40, left: 80 };
    const cW = W - pad.left - pad.right, cH = H - pad.top - pad.bottom;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#FAFBFC'; ctx.fillRect(0, 0, W, H);

    const values = curve.map(c => c.value);
    const minV = Math.min(...values, startCapital * 0.9);
    const maxV = Math.max(...values, startCapital * 1.05);
    const rangeV = maxV - minV || 1;
    const toX = i => pad.left + (i / (curve.length - 1)) * cW;
    const toY = v => pad.top + ((maxV - v) / rangeV) * cH;
    const baseY = toY(startCapital);

    // Grid
    ctx.strokeStyle = 'rgba(21,101,192,0.06)'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (i / 4) * cH;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + cW, y); ctx.stroke();
      const val = maxV - (i / 4) * rangeV;
      ctx.fillStyle = '#90A4AE'; ctx.font = '10px Inter,sans-serif'; ctx.textAlign = 'right';
      ctx.fillText(`₹${(val/1000).toFixed(0)}K`, pad.left - 6, y + 4);
    }

    // Baseline
    ctx.strokeStyle = '#CFD8DC'; ctx.lineWidth = 1; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pad.left, baseY); ctx.lineTo(pad.left + cW, baseY); ctx.stroke();
    ctx.setLineDash([]);

    // Fill under curve
    ctx.beginPath();
    curve.forEach((c, i) => i === 0 ? ctx.moveTo(toX(i), toY(c.value)) : ctx.lineTo(toX(i), toY(c.value)));
    ctx.lineTo(toX(curve.length - 1), baseY);
    ctx.lineTo(toX(0), baseY);
    ctx.closePath();
    ctx.fillStyle = values[values.length-1] >= startCapital ? 'rgba(38,166,154,0.1)' : 'rgba(239,83,80,0.1)';
    ctx.fill();

    // Equity curve line
    ctx.beginPath();
    curve.forEach((c, i) => i === 0 ? ctx.moveTo(toX(i), toY(c.value)) : ctx.lineTo(toX(i), toY(c.value)));
    ctx.strokeStyle = values[values.length-1] >= startCapital ? '#26A69A' : '#EF5350';
    ctx.lineWidth = 2; ctx.stroke();

    // X-axis date labels (sample 5)
    ctx.fillStyle = '#90A4AE'; ctx.font = '9px Inter,sans-serif'; ctx.textAlign = 'center';
    [0, 0.25, 0.5, 0.75, 1].forEach(f => {
      const idx = Math.round(f * (curve.length - 1));
      ctx.fillText(curve[idx]?.date?.slice(5) || '', toX(idx), pad.top + cH + 16);
    });
  }

  _renderTradeLog(trades) {
    const tbody = document.getElementById('bt-trade-body');
    if (!tbody) return;
    if (trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--text-muted);">No trades executed</td></tr>';
      return;
    }
    tbody.innerHTML = trades.map((t, i) => {
      const pnlClass = t.pnl >= 0 ? 'text-green' : 'text-red';
      const reasonTag = t.exitReason === 'Stop Loss' ? 'tag-bearish' : t.exitReason === 'Take Profit' ? 'tag-bullish' : 'tag-neutral';
      return `<tr>
        <td style="font-weight:700;color:var(--primary);">${i+1}</td>
        <td>${t.entryDate}</td>
        <td>₹${t.entryPrice?.toFixed(2)}</td>
        <td>${t.exitDate}</td>
        <td>₹${t.exitPrice?.toFixed(2)}</td>
        <td class="${pnlClass}" style="font-weight:600;">${t.pnl >= 0 ? '+' : ''}₹${t.pnl}</td>
        <td class="${pnlClass}" style="font-weight:600;">${t.returnPct >= 0 ? '+' : ''}${t.returnPct}%</td>
        <td><span class="tag ${reasonTag}" style="font-size:0.68rem;">${t.exitReason}</span></td>
      </tr>`;
    }).join('');
  }

  exportCSV() {
    if (!this._results) { alert('Run a backtest first.'); return; }
    const rows = [['#','Entry Date','Entry Price','Exit Date','Exit Price','P&L','Return %','Exit Reason']];
    this._results.trades.forEach((t, i) => {
      rows.push([i+1, t.entryDate, t.entryPrice, t.exitDate, t.exitPrice, t.pnl, t.returnPct, t.exitReason]);
    });
    const csv = rows.map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'tradesignal_backtest.csv'; a.click();
    URL.revokeObjectURL(url);
  }
}

window.backtester = new Backtester();
