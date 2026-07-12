/**
 * TradeSignal — Paper Trading Module
 * Virtual portfolio management with live price execution
 */

class PaperTrader {
  constructor() {
    this._bound = false;
    this._positions = [];
    this._summary = null;
  }

  async init() {
    if (this._bound) return;
    this._bound = true;
    this._bindEvents();
    await this.refresh();
  }

  _bindEvents() {
    document.getElementById('paper-buy-btn')?.addEventListener('click', () => this._executeTrade('BUY'));
    document.getElementById('paper-sell-btn')?.addEventListener('click', () => this._executeTrade('SELL'));
    document.getElementById('paper-reset-btn')?.addEventListener('click', () => this._resetPortfolio());

    const filterBtns = document.querySelectorAll('#paper-filters .tab-btn');
    filterBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        filterBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._renderPositions(btn.dataset.filter);
      });
    });
  }

  async refresh() {
    await Promise.all([this._loadSummary(), this._loadPositions()]);
    this._renderSummary();
    this._renderPositions('OPEN');
  }

  async _loadSummary() {
    try {
      const resp = await app.apiFetch('/api/paper/summary');
      if (resp.ok) this._summary = await resp.json();
    } catch (e) { console.error('Paper summary error:', e); }
  }

  async _loadPositions() {
    try {
      const resp = await app.apiFetch('/api/paper/positions');
      if (resp.ok) {
        const data = await resp.json();
        this._positions = data.positions || [];
      }
    } catch (e) { console.error('Paper positions error:', e); }
  }

  async _executeTrade(direction) {
    const symbol = document.getElementById('paper-symbol')?.value?.trim().toUpperCase();
    const qty = parseInt(document.getElementById('paper-qty')?.value) || 0;
    const price = parseFloat(document.getElementById('paper-price')?.value) || 0;

    if (!symbol || qty <= 0 || price <= 0) {
      this._toast('Enter valid symbol, quantity, and price', 'error');
      return;
    }

    try {
      const endpoint = direction === 'BUY' ? '/api/paper/buy' : '/api/paper/sell';
      const resp = await app.apiFetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, qty, price })
      });

      if (resp.ok) {
        const data = await resp.json();
        this._toast(`${direction} ${qty}× ${symbol} @ ₹${price} ✓`, 'success');
        document.getElementById('paper-symbol').value = '';
        document.getElementById('paper-qty').value = '1';
        document.getElementById('paper-price').value = '';
        await this.refresh();
      } else {
        const err = await resp.json();
        this._toast(err.error || 'Trade failed', 'error');
      }
    } catch (e) { this._toast(e.message, 'error'); }
  }

  async _closeTrade(id) {
    const price = prompt('Enter exit price:');
    if (!price || isNaN(parseFloat(price))) return;

    try {
      const resp = await app.apiFetch(`/api/paper/close/${id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ price: parseFloat(price) })
      });

      if (resp.ok) {
        const data = await resp.json();
        const pnlFmt = data.pnl >= 0 ? `+₹${data.pnl}` : `-₹${Math.abs(data.pnl)}`;
        this._toast(`Position closed. P&L: ${pnlFmt}`, data.pnl >= 0 ? 'success' : 'error');
        await this.refresh();
      } else {
        const err = await resp.json();
        this._toast(err.error || 'Close failed', 'error');
      }
    } catch (e) { this._toast(e.message, 'error'); }
  }

  async _resetPortfolio() {
    const capital = prompt('Reset portfolio with capital (₹):', '1000000');
    if (!capital || isNaN(parseFloat(capital))) return;
    if (!confirm(`Reset portfolio to ₹${parseFloat(capital).toLocaleString()}? All open positions will be cancelled.`)) return;

    try {
      const resp = await app.apiFetch('/api/paper/portfolio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ capital: parseFloat(capital) })
      });
      if (resp.ok) {
        this._toast('Portfolio reset ✓', 'success');
        await this.refresh();
      }
    } catch (e) { this._toast(e.message, 'error'); }
  }

  _renderSummary() {
    const container = document.getElementById('paper-summary');
    if (!container || !this._summary) return;
    const s = this._summary;

    const pnlColor = s.realizedPnl >= 0 ? '#26A69A' : '#EF5350';
    const retColor = s.totalReturn >= 0 ? '#26A69A' : '#EF5350';
    const wrColor = s.winRate >= 50 ? '#26A69A' : s.winRate >= 40 ? '#FFA726' : '#EF5350';

    container.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:12px;">
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Available Capital</span>
          <span class="stat-value" style="font-size:1rem;">₹${s.capital?.toLocaleString()}</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Invested</span>
          <span class="stat-value" style="font-size:1rem;">₹${s.invested?.toLocaleString()}</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Total Equity</span>
          <span class="stat-value" style="font-size:1rem;">₹${s.currentEquity?.toLocaleString()}</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">
        <div class="stat-card" style="padding:8px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Realized P&L</span>
          <span class="stat-value" style="font-size:0.9rem;color:${pnlColor};">₹${s.realizedPnl?.toLocaleString()}</span>
        </div>
        <div class="stat-card" style="padding:8px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Return</span>
          <span class="stat-value" style="font-size:0.9rem;color:${retColor};">${s.totalReturn}%</span>
        </div>
        <div class="stat-card" style="padding:8px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Win Rate</span>
          <span class="stat-value" style="font-size:0.9rem;color:${wrColor};">${s.winRate}%</span>
        </div>
        <div class="stat-card" style="padding:8px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Open</span>
          <span class="stat-value" style="font-size:0.9rem;">${s.openPositions}</span>
        </div>
      </div>`;
  }

  _renderPositions(filter = 'OPEN') {
    const container = document.getElementById('paper-positions');
    if (!container) return;

    const filtered = filter === 'ALL' ? this._positions
      : this._positions.filter(p => p.status === filter);

    if (filtered.length === 0) {
      container.innerHTML = `<div style="text-align:center;padding:32px;color:var(--text-muted);">
        <div style="font-size:2rem;margin-bottom:8px;">📝</div>
        <p>No ${filter.toLowerCase()} positions. Place a trade above.</p>
      </div>`;
      return;
    }

    container.innerHTML = `
      <table style="width:100%;font-size:0.8rem;">
        <thead>
          <tr style="color:var(--text-muted);font-size:0.7rem;text-transform:uppercase;">
            <th style="padding:8px 12px;text-align:left;">Symbol</th>
            <th style="padding:8px 4px;text-align:center;">Dir</th>
            <th style="padding:8px 4px;text-align:right;">Qty</th>
            <th style="padding:8px 4px;text-align:right;">Entry</th>
            <th style="padding:8px 4px;text-align:right;">Exit</th>
            <th style="padding:8px 4px;text-align:right;">P&L</th>
            <th style="padding:8px 4px;text-align:center;">Action</th>
          </tr>
        </thead>
        <tbody>
          ${filtered.map(p => {
            const dirColor = p.direction === 'BUY' ? '#26A69A' : '#EF5350';
            const pnlColor = !p.pnl ? 'var(--text-muted)' : p.pnl >= 0 ? '#26A69A' : '#EF5350';
            return `
              <tr style="border-bottom:1px solid rgba(255,255,255,0.04);">
                <td style="padding:8px 12px;font-weight:700;">${p.symbol}</td>
                <td style="padding:8px 4px;text-align:center;color:${dirColor};font-weight:600;">${p.direction === 'BUY' ? '▲' : '▼'}</td>
                <td style="padding:8px 4px;text-align:right;">${p.qty}</td>
                <td style="padding:8px 4px;text-align:right;">₹${p.entry_price?.toLocaleString()}</td>
                <td style="padding:8px 4px;text-align:right;">${p.exit_price ? `₹${p.exit_price.toLocaleString()}` : '—'}</td>
                <td style="padding:8px 4px;text-align:right;color:${pnlColor};font-weight:600;">${p.pnl != null ? `₹${p.pnl.toLocaleString()}` : '—'}</td>
                <td style="padding:8px 4px;text-align:center;">
                  ${p.status === 'OPEN' ? `<button class="btn btn-sm btn-primary" onclick="paperTrader._closeTrade(${p.id})" style="font-size:0.68rem;padding:3px 8px;">Close</button>` : `<span style="font-size:0.68rem;color:var(--text-muted);">${p.status}</span>`}
                </td>
              </tr>`;
          }).join('')}
        </tbody>
      </table>`;
  }

  _toast(msg, type = 'info') {
    if (typeof app !== 'undefined' && app.showToast) app.showToast(msg, type);
    else console.log(`[Paper] ${type}: ${msg}`);
  }
}

window.paperTrader = new PaperTrader();
