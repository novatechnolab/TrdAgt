/**
 * TradeSignal — Live Movers (Intraday Screener) Module
 *
 * Real-time movers during market hours: gainers, losers,
 * volume buzzers, OI spikes. Auto-refreshes every 15s.
 */
class LiveMovers {
  constructor() {
    this._data = null;
    this._loading = false;
    this._activeTab = 'gainers';
    this._refreshTimer = null;
  }

  async load() {
    if (this._loading) return;
    this._loading = true;

    const container = document.getElementById('movers-content');
    if (!this._data && container) {
      container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);">Scanning F&O stocks for live movers...</div>';
    }

    try {
      const res = await app.apiFetch('/api/live-movers');
      if (!res.ok) {
        if (!this._data) this._showError(container, 'Failed to load movers. Ensure Kite is connected.');
        return;
      }

      const data = await res.json();
      if (data.error) {
        if (!this._data) this._showError(container, data.error);
        return;
      }

      this._data = data;
      this.render();
    } catch (e) {
      if (!this._data) this._showError(container, 'Could not connect to backend.');
    } finally {
      this._loading = false;
      this._scheduleRefresh();
    }
  }

  stop() {
    clearTimeout(this._refreshTimer);
    this._refreshTimer = null;
  }

  _scheduleRefresh() {
    clearTimeout(this._refreshTimer);
    // Only auto-refresh during market hours
    const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const day = now.getDay(), hr = now.getHours(), mn = now.getMinutes();
    const marketOpen = day >= 1 && day <= 5 && (hr > 9 || (hr === 9 && mn >= 0)) && (hr < 15 || (hr === 15 && mn <= 30));
    if (marketOpen) {
      this._refreshTimer = setTimeout(() => this.load(), 15000);
    }
  }

  _showError(container, msg) {
    if (container) {
      container.innerHTML = `
        <div style="text-align:center;padding:60px 20px;color:var(--text-muted);">
          <div style="font-size:3rem;margin-bottom:12px;">🔥</div>
          <h3 style="font-family:var(--font-display);margin-bottom:8px;">Live Movers Unavailable</h3>
          <p style="font-size:0.85rem;">${msg}</p>
          <button class="btn btn-primary" onclick="liveMovers.load()" style="margin-top:16px;">Retry</button>
        </div>`;
    }
  }

  setTab(tab) {
    this._activeTab = tab;
    // Update tab buttons
    document.querySelectorAll('.movers-tab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    this._renderTable();
  }

  render() {
    const container = document.getElementById('movers-content');
    if (!container || !this._data) return;

    const fmt = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits: d}) : '—';

    // Tab bar
    const tabs = [
      { key: 'gainers', label: '📈 Top Gainers', count: this._data.gainers?.length || 0 },
      { key: 'losers', label: '📉 Top Losers', count: this._data.losers?.length || 0 },
      { key: 'volume_buzzers', label: '🔊 Volume Buzzers', count: this._data.volume_buzzers?.length || 0 },
      { key: 'oi_spikes', label: '🔗 OI Leaders', count: this._data.oi_spikes?.length || 0 },
    ];

    let html = `
      <div style="display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap;">
        ${tabs.map(t => `
          <button class="btn ${this._activeTab === t.key ? 'btn-primary' : 'btn-secondary'} movers-tab"
            data-tab="${t.key}" onclick="liveMovers.setTab('${t.key}')"
            style="font-size:0.75rem;padding:6px 12px;">
            ${t.label} <span style="opacity:0.7;font-size:0.65rem;">(${t.count})</span>
          </button>
        `).join('')}
        <div style="margin-left:auto;font-size:0.68rem;color:var(--text-muted);display:flex;align-items:center;gap:4px;">
          📊 ${this._data.total_stocks || 0} stocks scanned
        </div>
      </div>
      <div id="movers-table-container"></div>`;

    container.innerHTML = html;
    this._renderTable();
  }

  _renderTable() {
    const container = document.getElementById('movers-table-container');
    if (!container || !this._data) return;

    const items = this._data[this._activeTab] || [];
    const fmt = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits: d}) : '—';

    if (items.length === 0) {
      container.innerHTML = '<div style="text-align:center;padding:30px;color:var(--text-muted);">No data available.</div>';
      return;
    }

    const isVolume = this._activeTab === 'volume_buzzers';
    const isOI = this._activeTab === 'oi_spikes';

    let html = `<div style="overflow-x:auto;">
      <table class="data-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Stock</th>
            <th>LTP</th>
            <th>Change %</th>
            ${isVolume ? '<th>Volume</th>' : ''}
            ${isOI ? '<th>Open Interest</th>' : ''}
            <th>Day Range</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>`;

    items.forEach((m, i) => {
      const chgClass = m.change_pct >= 0 ? 'text-green' : 'text-red';
      const chgSign = m.change_pct >= 0 ? '+' : '';
      const rangeWidth = m.day_high - m.day_low;
      const ltpPos = rangeWidth > 0 ? ((m.ltp - m.day_low) / rangeWidth * 100) : 50;

      html += `<tr style="cursor:pointer;" onclick="document.getElementById('analysis-symbol-input')&&(document.getElementById('analysis-symbol-input').value='${m.symbol}');document.querySelector('[data-page=analysis]')?.click();setTimeout(()=>document.getElementById('btn-analyse')?.click(),300);">
        <td style="font-weight:700;color:var(--primary);">${i + 1}</td>
        <td style="font-weight:600;">${m.symbol}</td>
        <td>₹${fmt(m.ltp)}</td>
        <td class="${chgClass}" style="font-weight:700;">${chgSign}${m.change_pct.toFixed(2)}%</td>
        ${isVolume ? `<td>${(m.volume / 100000).toFixed(1)}L</td>` : ''}
        ${isOI ? `<td>${(m.oi / 100000).toFixed(1)}L</td>` : ''}
        <td>
          <div style="display:flex;align-items:center;gap:6px;font-size:0.7rem;">
            <span>₹${fmt(m.day_low,0)}</span>
            <div style="flex:1;height:4px;background:var(--border);border-radius:2px;position:relative;min-width:50px;">
              <div style="position:absolute;top:-3px;left:${ltpPos}%;width:10px;height:10px;border-radius:50%;background:var(--primary);transform:translateX(-50%);"></div>
            </div>
            <span>₹${fmt(m.day_high,0)}</span>
          </div>
        </td>
        <td onclick="event.stopPropagation();">
          <button class="btn btn-sm btn-secondary" onclick="window.watchlist?.toggle('${m.symbol}', this);"
            title="${window.watchlist?.has(m.symbol) ? 'Remove from Watchlist' : 'Add to Watchlist'}"
            style="font-size:0.85rem;padding:2px 6px;">${window.watchlist?.has(m.symbol) ? '⭐' : '☆'}</button>
        </td>
      </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
  }
}

window.liveMovers = new LiveMovers();
