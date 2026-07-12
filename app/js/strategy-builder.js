/**
 * TradeSignal — Strategy Builder
 * Multi-leg options strategy builder with live payoff diagram,
 * breakeven visualization, Greeks summary, and pre-built templates.
 */
class StrategyBuilder {
  constructor() {
    this._legs = [];
    this._spot = 0;
    this._symbol = '';
    this._chart = null;
  }

  // ── Templates ──
  get templates() {
    return {
      'Long Call':        [{ type:'CE', action:'BUY',  qty:1, strikeOffset:0,    premium:0 }],
      'Long Put':         [{ type:'PE', action:'BUY',  qty:1, strikeOffset:0,    premium:0 }],
      'Bull Call Spread': [{ type:'CE', action:'BUY',  qty:1, strikeOffset:0,    premium:0 },
                          { type:'CE', action:'SELL', qty:1, strikeOffset:100,  premium:0 }],
      'Bear Put Spread':  [{ type:'PE', action:'BUY',  qty:1, strikeOffset:0,    premium:0 },
                          { type:'PE', action:'SELL', qty:1, strikeOffset:-100, premium:0 }],
      'Iron Condor':      [{ type:'PE', action:'SELL', qty:1, strikeOffset:-200, premium:0 },
                          { type:'PE', action:'BUY',  qty:1, strikeOffset:-300, premium:0 },
                          { type:'CE', action:'SELL', qty:1, strikeOffset:200,  premium:0 },
                          { type:'CE', action:'BUY',  qty:1, strikeOffset:300,  premium:0 }],
      'Straddle':         [{ type:'CE', action:'BUY',  qty:1, strikeOffset:0,    premium:0 },
                          { type:'PE', action:'BUY',  qty:1, strikeOffset:0,    premium:0 }],
      'Strangle':         [{ type:'CE', action:'BUY',  qty:1, strikeOffset:200,  premium:0 },
                          { type:'PE', action:'BUY',  qty:1, strikeOffset:-200, premium:0 }],
      'Covered Call':     [{ type:'STOCK', action:'BUY', qty:1, strikeOffset:0,  premium:0 },
                          { type:'CE', action:'SELL', qty:1, strikeOffset:200,  premium:0 }],
    };
  }

  // ── Render main builder UI ──
  render() {
    const container = document.getElementById('strategy-content');
    if (!container) return;
    container.innerHTML = `
      <!-- Controls row -->
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:16px;">
        <div style="display:flex;align-items:center;gap:6px;">
          <label style="font-size:0.78rem;font-weight:600;color:var(--text-secondary);">Symbol</label>
          <input type="text" id="sb-symbol" list="strategy-symbol-list" placeholder="e.g. NIFTY"
            style="width:140px;padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
          <datalist id="strategy-symbol-list"></datalist>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <label style="font-size:0.78rem;font-weight:600;color:var(--text-secondary);">Spot</label>
          <input type="number" id="sb-spot" placeholder="Spot price" step="1"
            style="width:120px;padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <label style="font-size:0.78rem;font-weight:600;color:var(--text-secondary);">Template</label>
          <select id="sb-template" style="padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--bg-card);font-size:0.82rem;color:var(--text-primary);">
            <option value="">— Custom —</option>
            ${Object.keys(this.templates).map(t => `<option value="${t}">${t}</option>`).join('')}
          </select>
        </div>
        <button class="btn btn-secondary btn-sm" onclick="strategyBuilder.addLeg()">＋ Add Leg</button>
        <button class="btn btn-primary btn-sm" onclick="strategyBuilder.calculate()">📊 Calculate Payoff</button>
      </div>

      <!-- Legs table -->
      <div class="card mb-16" style="padding:12px;">
        <div style="font-weight:600;font-size:0.82rem;margin-bottom:8px;">Strategy Legs</div>
        <div id="sb-legs-container">
          <div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.82rem;">
            No legs added. Select a template or click ＋ Add Leg.
          </div>
        </div>
      </div>

      <!-- Payoff chart -->
      <div class="card mb-16" style="padding:16px;">
        <div style="font-weight:600;font-size:0.85rem;margin-bottom:12px;">📈 Payoff Diagram</div>
        <canvas id="sb-payoff-canvas" width="800" height="250"
          style="width:100%;border-radius:8px;background:#FAFBFC;border:1px solid var(--border);"></canvas>
      </div>

      <!-- Strategy summary -->
      <div id="sb-summary" style="display:none;">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:16px;" id="sb-stats-grid"></div>
        <div class="card" style="padding:14px;">
          <div style="font-weight:600;font-size:0.82rem;margin-bottom:8px;">Greeks Estimate</div>
          <div id="sb-greeks" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:0.78rem;"></div>
        </div>
      </div>`;

    // Bind template selector
    document.getElementById('sb-template')?.addEventListener('change', (e) => {
      if (e.target.value) this.applyTemplate(e.target.value);
    });

    // Populate datalist
    const dl = document.getElementById('strategy-symbol-list');
    if (dl && window.equityScreener) {
      ['NIFTY','BANKNIFTY','FINNIFTY'].forEach(s => { const o = document.createElement('option'); o.value = s; dl.appendChild(o); });
      equityScreener.getFNOUniverseSync().forEach(s => { const o = document.createElement('option'); o.value = s.symbol; dl.appendChild(o); });
    }

    // Auto-fetch spot when symbol changes
    document.getElementById('sb-symbol')?.addEventListener('blur', () => this._fetchSpot());
  }

  async _fetchSpot() {
    const symbol = document.getElementById('sb-symbol')?.value?.trim().toUpperCase();
    if (!symbol || !kiteAPI.connected) return;
    try {
      const res = await app.apiFetch(`/api/quote?symbol=${encodeURIComponent(symbol)}`);
      if (res.ok) {
        const data = await res.json();
        const ltp = data.last_price || data[`NSE:${symbol}`]?.last_price;
        if (ltp) {
          document.getElementById('sb-spot').value = Math.round(ltp);
          this._spot = ltp;
        }
      }
    } catch (e) {}
  }

  applyTemplate(name) {
    const template = this.templates[name];
    if (!template) return;
    this._legs = template.map((l, i) => ({ ...l, id: i, label: `${l.action} ${l.type}` }));
    this._renderLegs();
  }

  addLeg() {
    const id = Date.now();
    this._legs.push({ id, type: 'CE', action: 'BUY', qty: 1, strike: 0, premium: 0, strikeOffset: 0 });
    this._renderLegs();
  }

  removeLeg(id) {
    this._legs = this._legs.filter(l => l.id !== id);
    this._renderLegs();
  }

  _renderLegs() {
    const container = document.getElementById('sb-legs-container');
    if (!container) return;
    if (this._legs.length === 0) {
      container.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.82rem;">No legs added. Select a template or click ＋ Add Leg.</div>';
      return;
    }
    container.innerHTML = `
      <table style="width:100%;font-size:0.8rem;border-collapse:collapse;">
        <thead>
          <tr style="color:var(--text-secondary);font-size:0.72rem;text-transform:uppercase;border-bottom:1px solid var(--border);">
            <th style="padding:4px 8px;text-align:left;">Action</th>
            <th style="padding:4px 8px;text-align:left;">Type</th>
            <th style="padding:4px 8px;text-align:left;">Strike Offset</th>
            <th style="padding:4px 8px;text-align:left;">Premium (₹)</th>
            <th style="padding:4px 8px;text-align:left;">Qty (lots)</th>
            <th style="padding:4px 8px;text-align:left;"></th>
          </tr>
        </thead>
        <tbody>
          ${this._legs.map(l => `
            <tr style="border-bottom:1px solid var(--border);" data-lid="${l.id}">
              <td style="padding:6px 8px;">
                <select onchange="strategyBuilder._updateLeg(${l.id},'action',this.value)"
                  style="padding:4px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);font-size:0.78rem;">
                  <option ${l.action==='BUY'?'selected':''}>BUY</option>
                  <option ${l.action==='SELL'?'selected':''}>SELL</option>
                </select>
              </td>
              <td style="padding:6px 8px;">
                <select onchange="strategyBuilder._updateLeg(${l.id},'type',this.value)"
                  style="padding:4px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);font-size:0.78rem;">
                  <option ${l.type==='CE'?'selected':''}>CE</option>
                  <option ${l.type==='PE'?'selected':''}>PE</option>
                  <option ${l.type==='STOCK'?'selected':''}>STOCK</option>
                  <option ${l.type==='FUTURE'?'selected':''}>FUTURE</option>
                </select>
              </td>
              <td style="padding:6px 8px;">
                <input type="number" value="${l.strikeOffset}" step="50"
                  onchange="strategyBuilder._updateLeg(${l.id},'strikeOffset',+this.value)"
                  style="width:80px;padding:4px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);font-size:0.78rem;">
              </td>
              <td style="padding:6px 8px;">
                <input type="number" value="${l.premium||0}" step="0.5" min="0"
                  onchange="strategyBuilder._updateLeg(${l.id},'premium',+this.value)"
                  style="width:80px;padding:4px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);font-size:0.78rem;">
              </td>
              <td style="padding:6px 8px;">
                <input type="number" value="${l.qty||1}" min="1"
                  onchange="strategyBuilder._updateLeg(${l.id},'qty',+this.value)"
                  style="width:60px;padding:4px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);font-size:0.78rem;">
              </td>
              <td style="padding:6px 8px;">
                <button onclick="strategyBuilder.removeLeg(${l.id})"
                  style="background:none;border:none;cursor:pointer;color:#EF5350;font-size:1rem;">🗑</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  }

  _updateLeg(id, field, value) {
    const leg = this._legs.find(l => l.id === id);
    if (leg) leg[field] = value;
  }

  // ── Calculate and render payoff ──
  calculate() {
    const spot = parseFloat(document.getElementById('sb-spot')?.value) || this._spot;
    if (!spot || spot <= 0) { alert('Enter a valid spot price first.'); return; }
    this._spot = spot;

    const legs = this._legs;
    if (legs.length === 0) { alert('Add at least one leg.'); return; }

    // Net premium (debit/credit)
    const netPremium = legs.reduce((sum, l) => {
      const p = (l.premium || 0) * l.qty;
      return sum + (l.action === 'BUY' ? -p : p);
    }, 0);

    // Generate price range ±20% from spot
    const range = [];
    const lo = spot * 0.75, hi = spot * 1.25, step = (hi - lo) / 200;
    for (let p = lo; p <= hi; p += step) range.push(Math.round(p));

    // Calculate payoff at expiry for each price point
    const payoffs = range.map(price => {
      return legs.reduce((sum, l) => {
        const strike = spot + (l.strikeOffset || 0);
        let pnl = 0;
        if (l.type === 'CE') {
          pnl = Math.max(0, price - strike) - (l.premium || 0);
          if (l.action === 'SELL') pnl = -pnl;
        } else if (l.type === 'PE') {
          pnl = Math.max(0, strike - price) - (l.premium || 0);
          if (l.action === 'SELL') pnl = -pnl;
        } else if (l.type === 'STOCK') {
          pnl = (price - spot) - (l.premium || 0);
          if (l.action === 'SELL') pnl = -pnl;
        } else if (l.type === 'FUTURE') {
          pnl = (price - spot) - (l.premium || 0);
          if (l.action === 'SELL') pnl = -pnl;
        }
        return sum + pnl * l.qty;
      }, 0);
    });

    // Breakevenpoints
    const breakevens = [];
    for (let i = 1; i < payoffs.length; i++) {
      if ((payoffs[i-1] < 0 && payoffs[i] >= 0) || (payoffs[i-1] >= 0 && payoffs[i] < 0)) {
        breakevens.push(Math.round((range[i-1] + range[i]) / 2));
      }
    }

    const maxProfit = Math.max(...payoffs);
    const maxLoss   = Math.min(...payoffs);
    const rr = maxProfit > 0 && maxLoss < 0 ? (maxProfit / Math.abs(maxLoss)).toFixed(2) : '∞';

    this._drawPayoff(range, payoffs, spot, breakevens);
    this._renderStats(netPremium, maxProfit, maxLoss, rr, breakevens, legs);
  }

  _drawPayoff(prices, payoffs, spot, breakevens) {
    const canvas = document.getElementById('sb-payoff-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const pad = { top: 20, right: 30, bottom: 40, left: 70 };
    const cW = W - pad.left - pad.right, cH = H - pad.top - pad.bottom;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#FAFBFC';
    ctx.fillRect(0, 0, W, H);

    const minP = Math.min(...prices), maxP = Math.max(...prices);
    const minY = Math.min(...payoffs), maxY = Math.max(...payoffs);
    const rangeX = maxP - minP || 1;
    const rangeY = (maxY - minY) || 1;
    const yPad = rangeY * 0.1;

    const toX = p => pad.left + ((p - minP) / rangeX) * cW;
    const toY = v => pad.top + ((maxY + yPad - v) / (rangeY + 2*yPad)) * cH;
    const zeroY = toY(0);

    // Grid lines
    ctx.strokeStyle = 'rgba(21,101,192,0.06)'; ctx.lineWidth = 1;
    for (let i = 0; i <= 5; i++) {
      const y = pad.top + (i / 5) * cH;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(pad.left + cW, y); ctx.stroke();
    }

    // Zero line
    ctx.strokeStyle = '#B0BEC5'; ctx.lineWidth = 1.5; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(pad.left, zeroY); ctx.lineTo(pad.left + cW, zeroY); ctx.stroke();
    ctx.setLineDash([]);

    // Spot line
    const spotX = toX(spot);
    ctx.strokeStyle = '#1E88E5'; ctx.lineWidth = 1.5; ctx.setLineDash([6,3]);
    ctx.beginPath(); ctx.moveTo(spotX, pad.top); ctx.lineTo(spotX, pad.top + cH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#1E88E5'; ctx.font = '11px Inter,sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('Spot', spotX, pad.top - 6);

    // Draw payoff curve with fill
    ctx.beginPath();
    prices.forEach((p, i) => { i === 0 ? ctx.moveTo(toX(p), toY(payoffs[i])) : ctx.lineTo(toX(p), toY(payoffs[i])); });
    const gradient = ctx.createLinearGradient(0, pad.top, 0, pad.top + cH);
    gradient.addColorStop(0, 'rgba(38,166,154,0.15)');
    gradient.addColorStop(1, 'rgba(239,83,80,0.15)');
    ctx.strokeStyle = '#1E88E5'; ctx.lineWidth = 2.5;
    ctx.stroke();

    // Profit fill (above zero)
    ctx.save();
    ctx.beginPath();
    prices.forEach((p, i) => { i === 0 ? ctx.moveTo(toX(p), toY(payoffs[i])) : ctx.lineTo(toX(p), toY(payoffs[i])); });
    ctx.lineTo(toX(prices[prices.length-1]), zeroY);
    ctx.lineTo(toX(prices[0]), zeroY);
    ctx.closePath();
    ctx.clip();
    ctx.fillStyle = 'rgba(38,166,154,0.12)';
    ctx.fillRect(0, 0, W, zeroY);
    ctx.fillStyle = 'rgba(239,83,80,0.12)';
    ctx.fillRect(0, zeroY, W, H);
    ctx.restore();

    // Y-axis labels
    ctx.fillStyle = '#78909C'; ctx.font = '10px Inter,sans-serif'; ctx.textAlign = 'right';
    for (let i = 0; i <= 5; i++) {
      const val = maxY + yPad - (i / 5) * (rangeY + 2*yPad);
      ctx.fillText(`₹${Math.round(val)}`, pad.left - 6, pad.top + (i / 5) * cH + 4);
    }

    // X-axis labels
    ctx.textAlign = 'center';
    [0, 0.25, 0.5, 0.75, 1].forEach(f => {
      const p = minP + f * rangeX;
      ctx.fillText(Math.round(p), toX(p), pad.top + cH + 16);
    });

    // Breakeven markers
    breakevens.forEach(be => {
      const bx = toX(be);
      ctx.strokeStyle = '#FFA726'; ctx.lineWidth = 1.5; ctx.setLineDash([4,3]);
      ctx.beginPath(); ctx.moveTo(bx, pad.top); ctx.lineTo(bx, pad.top + cH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#FFA726'; ctx.font = 'bold 10px Inter,sans-serif'; ctx.textAlign = 'center';
      ctx.fillText(`BE\n${be}`, bx, pad.top + cH + 30);
    });
  }

  _renderStats(netPremium, maxProfit, maxLoss, rr, breakevens, legs) {
    const summary = document.getElementById('sb-summary');
    if (summary) summary.style.display = 'block';

    const grid = document.getElementById('sb-stats-grid');
    const fmt = v => v === Infinity ? '∞' : v === -Infinity ? '-∞' : `₹${Math.abs(v).toFixed(0)}`;
    const cards = [
      { label: 'Net Premium', value: netPremium >= 0 ? `+₹${netPremium.toFixed(0)} (Credit)` : `-₹${Math.abs(netPremium).toFixed(0)} (Debit)`, color: netPremium >= 0 ? '#26A69A' : '#EF5350' },
      { label: 'Max Profit',  value: maxProfit === Infinity ? 'Unlimited' : fmt(maxProfit),  color: '#26A69A' },
      { label: 'Max Loss',    value: maxLoss === -Infinity ? 'Unlimited' : fmt(maxLoss),     color: '#EF5350' },
      { label: 'Risk/Reward', value: `1 : ${rr}`,                                            color: '#1E88E5' },
      { label: 'Breakeven',   value: breakevens.length ? breakevens.map(b => `₹${b}`).join(', ') : 'N/A', color: '#FFA726' },
      { label: 'Legs',        value: legs.length,                                             color: '#78909C' },
    ];

    if (grid) grid.innerHTML = cards.map(c => `
      <div class="card" style="padding:12px;">
        <div style="font-size:0.68rem;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-muted);">${c.label}</div>
        <div style="font-size:1rem;font-weight:700;margin-top:4px;color:${c.color};">${c.value}</div>
      </div>`).join('');

    // Simplified Greeks estimate (approximate for educational display)
    const greeks = document.getElementById('sb-greeks');
    if (greeks) {
      const totalDelta = legs.reduce((s, l) => {
        let d = l.type === 'CE' ? 0.5 : l.type === 'PE' ? -0.5 : 1;
        return s + (l.action === 'BUY' ? d : -d) * l.qty;
      }, 0);
      greeks.innerHTML = [
        { name: 'Delta', value: totalDelta.toFixed(3), hint: 'Directional exposure' },
        { name: 'Gamma', value: '~', hint: 'Rate of delta change' },
        { name: 'Theta', value: netPremium > 0 ? '+' : '-', hint: 'Time decay (per day)' },
        { name: 'Vega',  value: legs.some(l => l.action==='BUY') ? '+' : '-', hint: 'IV sensitivity' },
      ].map(g => `
        <div class="card" style="padding:8px 10px;background:rgba(0,0,0,0.02);">
          <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;">${g.name}</div>
          <div style="font-size:0.9rem;font-weight:700;margin-top:2px;">${g.value}</div>
          <div style="font-size:0.6rem;color:var(--text-muted);margin-top:1px;">${g.hint}</div>
        </div>`).join('');
    }
  }
}

window.strategyBuilder = new StrategyBuilder();
