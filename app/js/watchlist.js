/**
 * TradeSignal — Watchlist Module
 * 
 * Personal stock watchlist with localStorage persistence.
 * Displays mini-scores and quick-analyse buttons.
 */
class Watchlist {
  constructor() {
    this.STORAGE_KEY = 'tradesignal_watchlist';
    this._stocks = this._load();
  }

  // ── CRUD ──
  add(symbol) {
    symbol = symbol.toUpperCase().trim();
    if (!symbol || this.has(symbol)) return false;
    this._stocks.push({ symbol, addedAt: Date.now() });
    this._save();
    return true;
  }

  remove(symbol) {
    this._stocks = this._stocks.filter(s => s.symbol !== symbol.toUpperCase());
    this._save();
  }

  has(symbol) {
    return this._stocks.some(s => s.symbol === symbol.toUpperCase());
  }

  toggle(symbol, btn) {
    symbol = symbol.toUpperCase().trim();
    if (this.has(symbol)) {
      this.remove(symbol);
      if (btn) btn.textContent = '☆';
    } else {
      this.add(symbol);
      if (btn) btn.textContent = '⭐';
    }
  }

  getAll() {
    return [...this._stocks];
  }

  count() {
    return this._stocks.length;
  }

  // ── Persistence ──
  _load() {
    try {
      return JSON.parse(localStorage.getItem(this.STORAGE_KEY)) || [];
    } catch { return []; }
  }

  _save() {
    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(this._stocks));
    this._updateBadge();
  }

  _updateBadge() {
    const badge = document.getElementById('watchlist-count');
    if (badge) {
      badge.textContent = this._stocks.length;
      badge.style.display = this._stocks.length > 0 ? '' : 'none';
    }
  }

  // ── Render Watchlist Page ──
  async render() {
    const container = document.getElementById('watchlist-grid');
    if (!container) return;

    if (this._stocks.length === 0) {
      container.innerHTML = `
        <div style="text-align:center;padding:60px 20px;color:var(--text-muted);">
          <div style="font-size:3rem;margin-bottom:12px;">⭐</div>
          <h3 style="font-family:var(--font-display);margin-bottom:8px;">Your Watchlist is Empty</h3>
          <p style="font-size:0.85rem;">Add stocks from the Stock Analysis page or search bar to track them here.</p>
        </div>`;
      return;
    }

    container.innerHTML = `<div style="text-align:center;padding:30px;color:var(--text-muted);">Loading watchlist data...</div>`;

    // Fetch mini-analysis for each stock in parallel
    const results = await Promise.allSettled(
      this._stocks.map(s => this._fetchMiniAnalysis(s.symbol))
    );

    const fmt = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits: d}) : '—';
    const signalColor = (s) => s === 'BULLISH' || s === 'CALL' ? '#26A69A' : s === 'BEARISH' || s === 'PUT' ? '#EF5350' : '#78909C';

    let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;">';

    this._stocks.forEach((stock, i) => {
      const res = results[i];
      if (res.status === 'fulfilled' && res.value) {
        const d = res.value;
        const changePct = d.changePct || 0;
        html += `
          <div class="card" style="padding:16px;position:relative;">
            <button onclick="watchlist.remove('${stock.symbol}'); watchlist.render();" 
              style="position:absolute;top:8px;right:8px;background:none;border:none;cursor:pointer;font-size:1rem;color:var(--text-muted);" title="Remove">✕</button>
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
              <div>
                <div style="font-weight:700;font-size:1.1rem;font-family:var(--font-display);">${stock.symbol}</div>
                <div style="font-size:0.82rem;color:var(--text-secondary);">
                  ₹${fmt(d.ltp)} 
                  <span style="color:${changePct >= 0 ? '#26A69A' : '#EF5350'};">${changePct >= 0 ? '▲' : '▼'} ${Math.abs(changePct).toFixed(2)}%</span>
                </div>
              </div>
              <div style="display:flex;gap:6px;">
                <div class="score-badge ${d.eqScore >= 70 ? 'score-high' : d.eqScore >= 50 ? 'score-med' : 'score-low'}" 
                  style="width:36px;height:36px;font-size:0.8rem;" title="Equity Score">${d.eqScore}</div>
                <div class="score-badge ${d.opScore >= 70 ? 'score-high' : d.opScore >= 50 ? 'score-med' : 'score-low'}" 
                  style="width:36px;height:36px;font-size:0.8rem;" title="Options Score">${d.opScore}</div>
              </div>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:8px;">
              <span style="padding:2px 8px;border-radius:12px;font-size:0.68rem;font-weight:600;background:${signalColor(d.eqDir)}22;color:${signalColor(d.eqDir)};">📈 ${d.eqDir}</span>
              <span style="padding:2px 8px;border-radius:12px;font-size:0.68rem;font-weight:600;background:${signalColor(d.opDir)}22;color:${signalColor(d.opDir)};">⚡ ${d.opDir}</span>
            </div>
            <div style="display:flex;gap:12px;font-size:0.7rem;color:var(--text-muted);margin-bottom:10px;">
              <span>RSI: ${d.rsi?.toFixed(1) || '—'}</span>
              <span>SL: ₹${fmt(d.sl)}</span>
              <span>T1: ₹${fmt(d.t1)}</span>
              <span>R:R ${d.rr}:1</span>
            </div>
            <button onclick="document.getElementById('analysis-symbol').value='${stock.symbol}'; document.querySelector('[data-page=analysis]').click(); setTimeout(() => document.getElementById('btn-analyse').click(), 300);"
              class="btn btn-primary" style="width:100%;font-size:0.75rem;padding:6px;">🔬 Analyse</button>
          </div>`;
      } else {
        html += `
          <div class="card" style="padding:16px;position:relative;">
            <button onclick="watchlist.remove('${stock.symbol}'); watchlist.render();"
              style="position:absolute;top:8px;right:8px;background:none;border:none;cursor:pointer;font-size:1rem;color:var(--text-muted);" title="Remove">✕</button>
            <div style="font-weight:700;font-size:1.1rem;font-family:var(--font-display);">${stock.symbol}</div>
            <div style="color:var(--text-muted);font-size:0.82rem;margin-top:8px;">Unable to load data</div>
            <button onclick="document.getElementById('analysis-symbol').value='${stock.symbol}'; document.querySelector('[data-page=analysis]').click();"
              class="btn btn-secondary" style="width:100%;font-size:0.75rem;padding:6px;margin-top:10px;">🔬 Analyse</button>
          </div>`;
      }
    });

    html += '</div>';
    container.innerHTML = html;
  }

  // ── Fetch mini-analysis from backend ──
  async _fetchMiniAnalysis(symbol) {
    const resp = await fetch('/api/stock-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ symbol, mode: 'premarket' })
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    if (!data.ohlcv || data.ohlcv.length < 20) return null;

    const closes = data.ohlcv.map(c => c.close);
    const highs  = data.ohlcv.map(c => c.high);
    const lows   = data.ohlcv.map(c => c.low);
    const volumes = data.ohlcv.map(c => c.volume);

    const eq = scoringEngine.scoreEquity({ closes, highs, lows, volumes, fundamentals: {}, sectorData: {} });
    const op = scoringEngine.scoreOptions({ closes, highs, lows, volumes, fundamentals: {}, snapshot: {}, optionsData: {} });

    const lastClose = closes[closes.length - 1];
    const prevClose = closes[closes.length - 2] || lastClose;

    return {
      ltp: lastClose,
      changePct: ((lastClose - prevClose) / prevClose) * 100,
      eqScore: Math.round(eq.total),
      opScore: Math.round(op.total),
      eqDir: eq.direction,
      opDir: op.direction,
      rsi: eq.rsi,
      sl: eq.risk?.stopLoss,
      t1: eq.risk?.target1,
      rr: eq.risk?.riskReward
    };
  }

  // ── Toggle (add/remove) and update button icon in-place ──
  toggle(symbol, btn) {
    const hasText = btn && btn.textContent.includes('Watch');
    if (this.has(symbol)) {
      this.remove(symbol);
      if (btn) {
        btn.textContent = hasText ? '☆ Watch' : '☆';
        btn.title = 'Add to Watchlist';
      }
    } else {
      this.add(symbol);
      if (btn) {
        btn.textContent = hasText ? '⭐ Watching' : '⭐';
        btn.title = 'Remove from Watchlist';
      }
    }
  }

  // ── Init: update badge on load ──
  init() {
    this._updateBadge();
  }
}

window.watchlist = new Watchlist();
