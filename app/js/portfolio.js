/**
 * TradeSignal — Portfolio Tracker & P&L Module
 *
 * Fetches holdings/positions from Kite, displays real-time P&L,
 * sector allocation, and concentration risk alerts.
 */
class Portfolio {
  constructor() {
    this._data = null;
    this._loading = false;
  }

  async load() {
    if (this._loading) return;
    this._loading = true;

    const container = document.getElementById('portfolio-content');
    if (container) {
      container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);">Loading portfolio data...</div>';
    }

    try {
      const [summaryRes, holdingsRes] = await Promise.all([
        app.apiFetch('/api/portfolio/summary'),
        app.apiFetch('/api/portfolio/holdings'),
      ]);

      if (!summaryRes.ok || !holdingsRes.ok) {
        this._showError(container, 'Failed to load portfolio. Ensure Kite is connected.');
        return;
      }

      const summary = await summaryRes.json();
      const holdingsData = await holdingsRes.json();

      if (summary.error) {
        this._showError(container, summary.error);
        return;
      }

      this._data = { summary, holdings: holdingsData.holdings || [] };
      this.render();
    } catch (e) {
      this._showError(container, 'Could not connect to backend.');
    } finally {
      this._loading = false;
    }
  }

  _showError(container, msg) {
    if (container) {
      container.innerHTML = `
        <div style="text-align:center;padding:60px 20px;color:var(--text-muted);">
          <div style="font-size:3rem;margin-bottom:12px;">💼</div>
          <h3 style="font-family:var(--font-display);margin-bottom:8px;">Portfolio Unavailable</h3>
          <p style="font-size:0.85rem;">${msg}</p>
          <button class="btn btn-primary" onclick="portfolio.load()" style="margin-top:16px;">Retry</button>
        </div>`;
    }
  }

  render() {
    const container = document.getElementById('portfolio-content');
    if (!container || !this._data) return;

    const s = this._data.summary;
    const holdings = this._data.holdings;
    const fmt = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits: d}) : '—';
    const pnlColor = (v) => v >= 0 ? '#26A69A' : '#EF5350';
    const pnlSign = (v) => v >= 0 ? '+' : '';

    // Summary cards
    let html = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px;">
        <div class="card" style="padding:16px;">
          <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;">Total Invested</div>
          <div style="font-size:1.3rem;font-weight:700;font-family:var(--font-display);margin-top:4px;">₹${fmt(s.total_invested, 0)}</div>
        </div>
        <div class="card" style="padding:16px;">
          <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;">Current Value</div>
          <div style="font-size:1.3rem;font-weight:700;font-family:var(--font-display);margin-top:4px;">₹${fmt(s.total_current, 0)}</div>
        </div>
        <div class="card" style="padding:16px;">
          <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;">Overall P&L</div>
          <div style="font-size:1.3rem;font-weight:700;margin-top:4px;color:${pnlColor(s.overall_pnl)};">
            ${pnlSign(s.overall_pnl)}₹${fmt(Math.abs(s.overall_pnl), 0)}
            <span style="font-size:0.75rem;font-weight:500;">(${pnlSign(s.overall_pnl_pct)}${s.overall_pnl_pct}%)</span>
          </div>
        </div>
        <div class="card" style="padding:16px;">
          <div style="font-size:0.72rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;">Day P&L</div>
          <div style="font-size:1.3rem;font-weight:700;margin-top:4px;color:${pnlColor(s.day_pnl)};">
            ${pnlSign(s.day_pnl)}₹${fmt(Math.abs(s.day_pnl), 0)}
          </div>
        </div>
      </div>`;

    // Concentration alerts
    if (s.concentration_alerts && s.concentration_alerts.length > 0) {
      html += `<div style="background:rgba(239,83,80,0.08);border:1px solid rgba(239,83,80,0.3);border-radius:8px;padding:12px 16px;margin-bottom:16px;">
        <div style="font-weight:600;color:#EF5350;font-size:0.82rem;margin-bottom:4px;">⚠ Concentration Risk</div>
        ${s.concentration_alerts.map(a => `<div style="font-size:0.78rem;color:var(--text-secondary);margin-top:2px;">${a.message}</div>`).join('')}
      </div>`;
    }

    // Holdings heat map
    if (holdings.length > 0) {
      html += `<div class="card" style="padding:16px;margin-bottom:16px;">
        <h4 style="font-family:var(--font-display);margin-bottom:12px;">Holdings (${s.holdings_count})</h4>
        <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:16px;">`;

      // Heat map tiles
      const maxVal = Math.max(...holdings.map(h => Math.abs(h.pnl || 0)), 1);
      holdings.forEach(h => {
        const pnl = h.pnl || 0;
        const qty = h.quantity || 0;
        const ltp = h.last_price || 0;
        const value = qty * ltp;
        const pnlPct = h.average_price ? ((ltp - h.average_price) / h.average_price * 100) : 0;
        const intensity = Math.min(Math.abs(pnl) / maxVal, 1);
        const bg = pnl >= 0
          ? `rgba(38,166,154,${0.1 + intensity * 0.5})`
          : `rgba(239,83,80,${0.1 + intensity * 0.5})`;
        const textColor = pnl >= 0 ? '#26A69A' : '#EF5350';

        html += `
          <div style="background:${bg};border-radius:6px;padding:8px 10px;min-width:110px;flex:1 1 110px;max-width:180px;cursor:pointer;"
            onclick="document.getElementById('analysis-symbol-input') && (document.getElementById('analysis-symbol-input').value='${h.tradingsymbol}'); document.querySelector('[data-page=analysis]')?.click();"
            title="${h.tradingsymbol}: ₹${fmt(ltp)} | P&L: ₹${fmt(pnl)}">
            <div style="font-weight:600;font-size:0.78rem;">${h.tradingsymbol}</div>
            <div style="font-size:0.68rem;color:${textColor};font-weight:600;margin-top:2px;">
              ${pnlSign(pnlPct)}${pnlPct.toFixed(1)}%
            </div>
          </div>`;
      });

      html += `</div>`;

      // Holdings table
      html += `<div style="overflow-x:auto;">
        <table class="data-table">
          <thead>
            <tr><th>Stock</th><th>Qty</th><th>Avg Price</th><th>LTP</th><th>Invested</th><th>Current</th><th>P&L</th><th>P&L %</th></tr>
          </thead>
          <tbody>`;

      holdings.forEach(h => {
        const qty = h.quantity || 0;
        const avg = h.average_price || 0;
        const ltp = h.last_price || 0;
        const invested = qty * avg;
        const current = qty * ltp;
        const pnl = h.pnl || (current - invested);
        const pnlPct = avg ? ((ltp - avg) / avg * 100) : 0;
        const color = pnl >= 0 ? 'text-green' : 'text-red';

        html += `<tr>
          <td style="font-weight:600;">${h.tradingsymbol}</td>
          <td>${qty}</td>
          <td>₹${fmt(avg)}</td>
          <td>₹${fmt(ltp)}</td>
          <td>₹${fmt(invested, 0)}</td>
          <td>₹${fmt(current, 0)}</td>
          <td class="${color}" style="font-weight:600;">${pnlSign(pnl)}₹${fmt(Math.abs(pnl), 0)}</td>
          <td class="${color}" style="font-weight:600;">${pnlSign(pnlPct)}${pnlPct.toFixed(2)}%</td>
        </tr>`;
      });

      html += `</tbody></table></div></div>`;
    }

    // Open positions
    if (s.open_positions && s.open_positions.length > 0) {
      html += `<div class="card" style="padding:16px;">
        <h4 style="font-family:var(--font-display);margin-bottom:12px;">Open Positions (${s.positions_count})</h4>
        <div style="overflow-x:auto;">
        <table class="data-table">
          <thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>P&L</th><th>Product</th></tr></thead>
          <tbody>`;

      s.open_positions.forEach(p => {
        const color = p.pnl >= 0 ? 'text-green' : 'text-red';
        html += `<tr>
          <td style="font-weight:600;">${p.symbol}</td>
          <td>${p.qty}</td>
          <td>₹${fmt(p.avg)}</td>
          <td>₹${fmt(p.ltp)}</td>
          <td class="${color}" style="font-weight:600;">${pnlSign(p.pnl)}₹${fmt(Math.abs(p.pnl), 0)}</td>
          <td><span class="tag" style="font-size:0.68rem;">${p.product}</span></td>
        </tr>`;
      });

      html += `</tbody></table></div></div>`;
    }

    // Empty state if no holdings and no positions
    if (holdings.length === 0 && (!s.open_positions || s.open_positions.length === 0)) {
      html += `<div style="text-align:center;padding:40px;color:var(--text-muted);">
        <div style="font-size:2rem;margin-bottom:8px;">📭</div>
        <p>No holdings or open positions found.</p>
      </div>`;
    }

    container.innerHTML = html;
  }
}

window.portfolio = new Portfolio();
