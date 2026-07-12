/**
 * TradeSignal — Trade Journal Module
 * CRUD interface for tracking personal trades with analytics
 */

class TradeJournal {
  constructor() {
    this._bound = false;
    this._trades = [];
    this._stats = null;
    this._filter = 'ALL'; // ALL | OPEN | CLOSED
    this._editing = null;
  }

  async init() {
    if (this._bound) return;
    this._bound = true;
    this._bindEvents();
    await this.refresh();
  }

  _bindEvents() {
    const form = document.getElementById('journal-form');
    if (form) form.addEventListener('submit', e => { e.preventDefault(); this._submit(); });

    const filterBtns = document.querySelectorAll('#journal-filters .tab-btn');
    filterBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        filterBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._filter = btn.dataset.filter;
        this._renderList();
      });
    });
  }

  async refresh() {
    await Promise.all([this._loadTrades(), this._loadStats()]);
    this._renderList();
    this._renderStats();
  }

  async _loadTrades() {
    try {
      const resp = await app.apiFetch('/api/journal');
      if (resp.ok) {
        const data = await resp.json();
        this._trades = data.trades || [];
      }
    } catch (e) { console.error('Journal load error:', e); }
  }

  async _loadStats() {
    try {
      const resp = await app.apiFetch('/api/journal/stats');
      if (resp.ok) this._stats = await resp.json();
    } catch (e) { console.error('Journal stats error:', e); }
  }

  async _submit() {
    const symbol = document.getElementById('j-symbol')?.value?.trim().toUpperCase();
    const direction = document.getElementById('j-direction')?.value || 'LONG';
    const entryPrice = parseFloat(document.getElementById('j-entry-price')?.value);
    const exitPrice = parseFloat(document.getElementById('j-exit-price')?.value) || null;
    const qty = parseInt(document.getElementById('j-qty')?.value) || 1;
    const entryDate = document.getElementById('j-entry-date')?.value;
    const exitDate = document.getElementById('j-exit-date')?.value || null;
    const rationale = document.getElementById('j-rationale')?.value || '';
    const tags = document.getElementById('j-tags')?.value || '';

    if (!symbol || !entryPrice || !entryDate) {
      this._toast('Symbol, entry price, and entry date are required', 'error');
      return;
    }

    const body = { symbol, direction, entry_price: entryPrice, exit_price: exitPrice,
                   qty, entry_date: entryDate, exit_date: exitDate, rationale, tags };

    try {
      const url = this._editing ? `/api/journal/${this._editing}` : '/api/journal';
      const method = this._editing ? 'PUT' : 'POST';
      const resp = await app.apiFetch(url, {
        method, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (resp.ok) {
        this._editing = null;
        document.getElementById('journal-form')?.reset();
        document.getElementById('j-entry-date').value = new Date().toISOString().slice(0, 10);
        this._toast(method === 'PUT' ? 'Trade updated' : 'Trade logged ✓', 'success');
        await this.refresh();
      } else {
        const err = await resp.json();
        this._toast(err.error || 'Failed to save', 'error');
      }
    } catch (e) { this._toast(e.message, 'error'); }
  }

  async _delete(id) {
    if (!confirm('Delete this trade entry?')) return;
    try {
      const resp = await app.apiFetch(`/api/journal/${id}`, { method: 'DELETE' });
      if (resp.ok) {
        this._toast('Deleted', 'success');
        await this.refresh();
      }
    } catch (e) { this._toast(e.message, 'error'); }
  }

  async _close(id) {
    const price = prompt('Enter exit price:');
    if (!price || isNaN(parseFloat(price))) return;
    try {
      const resp = await app.apiFetch(`/api/journal/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exit_price: parseFloat(price), exit_date: new Date().toISOString().slice(0, 10), status: 'CLOSED' })
      });
      if (resp.ok) {
        this._toast('Trade closed ✓', 'success');
        await this.refresh();
      }
    } catch (e) { this._toast(e.message, 'error'); }
  }

  _renderList() {
    const container = document.getElementById('journal-list');
    if (!container) return;

    let filtered = this._trades;
    if (this._filter !== 'ALL') {
      filtered = this._trades.filter(t => t.status === this._filter);
    }

    if (filtered.length === 0) {
      container.innerHTML = `<div style="text-align:center;padding:32px;color:var(--text-muted);">
        <div style="font-size:2rem;margin-bottom:8px;">📓</div>
        <p>No ${this._filter.toLowerCase()} trades yet. Use the form above to log your first trade.</p>
      </div>`;
      return;
    }

    container.innerHTML = filtered.map(t => {
      const pnlColor = !t.pnl ? 'var(--text-muted)' : t.pnl > 0 ? '#26A69A' : '#EF5350';
      const pnlText = t.pnl != null ? `₹${t.pnl.toLocaleString()}` : '—';
      const statusBadge = t.status === 'OPEN'
        ? '<span style="padding:2px 8px;border-radius:4px;background:rgba(33,150,243,0.15);color:#2196F3;font-size:0.7rem;font-weight:600;">OPEN</span>'
        : '<span style="padding:2px 8px;border-radius:4px;background:rgba(38,166,154,0.15);color:#26A69A;font-size:0.7rem;font-weight:600;">CLOSED</span>';
      const dirBadge = t.direction === 'LONG'
        ? '<span style="color:#26A69A;font-weight:700;">▲ LONG</span>'
        : '<span style="color:#EF5350;font-weight:700;">▼ SHORT</span>';

      return `
        <div style="padding:12px 16px;border-bottom:1px solid rgba(255,255,255,0.04);display:grid;grid-template-columns:1fr auto;gap:8px;align-items:start;">
          <div>
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
              <span style="font-weight:700;font-size:0.95rem;">${t.symbol}</span>
              ${dirBadge}
              ${statusBadge}
              <span style="font-size:0.72rem;color:var(--text-muted);">×${t.qty}</span>
            </div>
            <div style="display:flex;gap:16px;font-size:0.78rem;color:var(--text-secondary);">
              <span>Entry: ₹${t.entry_price?.toLocaleString()}</span>
              ${t.exit_price ? `<span>Exit: ₹${t.exit_price.toLocaleString()}</span>` : ''}
              <span style="color:${pnlColor};font-weight:600;">P&L: ${pnlText}</span>
            </div>
            <div style="font-size:0.7rem;color:var(--text-muted);margin-top:4px;">
              ${t.entry_date}${t.exit_date ? ` → ${t.exit_date}` : ''}
              ${t.tags ? ` · ${t.tags.split(',').map(tag => `<span style="padding:1px 6px;border-radius:3px;background:rgba(255,255,255,0.06);margin-left:4px;">${tag.trim()}</span>`).join('')}` : ''}
            </div>
            ${t.rationale ? `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px;font-style:italic;">💬 ${t.rationale}</div>` : ''}
          </div>
          <div style="display:flex;gap:4px;">
            ${t.status === 'OPEN' ? `<button class="btn btn-sm btn-primary" onclick="tradeJournal._close(${t.id})" style="font-size:0.7rem;padding:4px 8px;">Close</button>` : ''}
            <button class="btn btn-sm btn-secondary" onclick="tradeJournal._delete(${t.id})" style="font-size:0.7rem;padding:4px 8px;">🗑</button>
          </div>
        </div>`;
    }).join('');
  }

  _renderStats() {
    const container = document.getElementById('journal-stats');
    if (!container || !this._stats) return;

    const s = this._stats;
    if (!s.totalTrades) {
      container.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">Complete and close trades to see analytics</div>';
      return;
    }

    const pnlColor = s.totalPnl >= 0 ? '#26A69A' : '#EF5350';
    const wrColor = s.winRate >= 50 ? '#26A69A' : s.winRate >= 40 ? '#FFA726' : '#EF5350';

    container.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px;">
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Total P&L</span>
          <span class="stat-value" style="font-size:1rem;color:${pnlColor};">₹${s.totalPnl?.toLocaleString()}</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Win Rate</span>
          <span class="stat-value" style="font-size:1rem;color:${wrColor};">${s.winRate}%</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Trades</span>
          <span class="stat-value" style="font-size:1rem;">${s.totalTrades}</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.62rem;">Profit Factor</span>
          <span class="stat-value" style="font-size:1rem;">${s.profitFactor}</span>
        </div>
      </div>
      <div style="display:flex;gap:16px;font-size:0.75rem;color:var(--text-secondary);">
        <span>🟢 Avg Win: ₹${s.avgWin?.toLocaleString()}</span>
        <span>🔴 Avg Loss: ₹${s.avgLoss?.toLocaleString()}</span>
        <span>W: ${s.wins} | L: ${s.losses}</span>
      </div>`;
  }

  _toast(msg, type = 'info') {
    if (typeof app !== 'undefined' && app.showToast) app.showToast(msg, type);
    else console.log(`[Journal] ${type}: ${msg}`);
  }
}

window.tradeJournal = new TradeJournal();
