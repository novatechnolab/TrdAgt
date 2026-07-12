/**
 * TradeSignal — Index Movers Dashboard
 *
 * Live & previous-day sector-grouped movers for NSE and BSE indices.
 * Supports: Nifty 50, Bank Nifty, Nifty IT, Nifty 100,
 *           BSE Sensex, BSE Bankex, BSE IT, BSE 100.
 */

// Approximate sector weights (% of index by market cap) for each index
// Source: NSE/BSE index methodology — approximate values, updated periodically
const IM_SECTOR_WEIGHTS = {
  'nifty50': {
    'Financial Services': 35.45,
    'Oil, Gas & Consumable Fuels': 10.95,
    'Information Technology': 9.40,
    'Automobile and Auto Components': 6.60,
    'Fast Moving Consumer Goods': 5.96,
    'Telecommunication': 5.34,
    'Healthcare': 4.68,
    'Metals & Mining': 4.28,
    'Construction': 4.02,
    'Power': 3.03,
    'Consumer Durables': 2.55,
    'Consumer Services': 2.33,
    'Construction Materials': 2.19,
    'Services': 1.82,
    'Capital Goods': 1.40
  },
  'banknifty': {
    'Banking':78, 'Finance':22,
  },
  'niftyit': {
    'IT':100,
  },
  'nifty100': {
    'Banking':22, 'Finance':9,  'IT':12,  'Energy':11, 'FMCG':8,
    'Auto':7,     'Pharma':5,   'Metals':4,'Infra':6,   'Consumer':6,
    'Telecom':4,  'Capital Goods':3, 'Internet':2, 'Chemicals':1,
  },
  'sensex': {
    'Banking':24, 'Finance':9,  'IT':12,  'Energy':12, 'FMCG':10,
    'Auto':8,     'Pharma':5,   'Metals':4,'Infra':5,   'Consumer':5,
    'Telecom':4,  'Capital Goods':2,
  },
  'bsebankex': {
    'Banking':78, 'Finance':22,
  },
  'bseit': {
    'IT':100,
  },
  'bse100': {
    'Banking':22, 'Finance':9,  'IT':12,  'Energy':11, 'FMCG':8,
    'Auto':7,     'Pharma':5,   'Metals':4,'Infra':6,   'Consumer':6,
    'Telecom':4,  'Capital Goods':3, 'Internet':2, 'Chemicals':1,
  },
};

const IM_SECTOR_MAP = {
  // Financial Services
  'AXISBANK':'Financial Services',   'HDFCBANK':'Financial Services',   'ICICIBANK':'Financial Services',
  'KOTAKBANK':'Financial Services',  'SBIN':'Financial Services',        'INDUSINDBK':'Financial Services',
  'BANDHANBNK':'Financial Services', 'FEDERALBNK':'Financial Services',  'PNB':'Financial Services',
  'BANKBARODA':'Financial Services', 'IDFCFIRSTB':'Financial Services',  'AUBANK':'Financial Services',
  'CANBK':'Financial Services',
  'BAJFINANCE':'Financial Services',  'BAJAJFINSV':'Financial Services', 'HDFCLIFE':'Financial Services',
  'SBILIFE':'Financial Services',     'ICICIGI':'Financial Services',     'ICICIPRULI':'Financial Services',
  'CHOLAFIN':'Financial Services',    'MFSL':'Financial Services',        'LICHSGFIN':'Financial Services',
  'MUTHOOTFIN':'Financial Services',  'RECLTD':'Financial Services',
  // IT
  'INFY':'Information Technology',   'TCS':'Information Technology',        'HCLTECH':'Information Technology',  'WIPRO':'Information Technology',
  'TECHM':'Information Technology',  'LTIM':'Information Technology',       'MPHASIS':'Information Technology',  'OFSS':'Information Technology',
  'COFORGE':'Information Technology','PERSISTENT':'Information Technology',
  // Auto
  'BAJAJ-AUTO':'Automobile and Auto Components', 'HEROMOTOCO':'Automobile and Auto Components', 'EICHERMOT':'Automobile and Auto Components',
  'MARUTI':'Automobile and Auto Components',     'TATAMOTORS':'Automobile and Auto Components', 'M&M':'Automobile and Auto Components',
  'BOSCHLTD':'Automobile and Auto Components',   'BALKRISIND':'Automobile and Auto Components',
  // Energy & Oil
  'RELIANCE':'Oil, Gas & Consumable Fuels',   'BPCL':'Oil, Gas & Consumable Fuels',       'ONGC':'Oil, Gas & Consumable Fuels',
  'COALINDIA':'Oil, Gas & Consumable Fuels',  'NTPC':'Power',        'POWERGRID':'Power',
  'GAIL':'Oil, Gas & Consumable Fuels',       'TATAPOWER':'Power',   'ADANIGREEN':'Power',
  'ADANITRANS':'Power',
  // FMCG
  'HINDUNILVR':'Fast Moving Consumer Goods',  'ITC':'Fast Moving Consumer Goods',      'NESTLEIND':'Fast Moving Consumer Goods',
  'BRITANNIA':'Fast Moving Consumer Goods',   'TATACONSUM':'Fast Moving Consumer Goods','DABUR':'Fast Moving Consumer Goods',
  'COLPAL':'Fast Moving Consumer Goods',      'GODREJCP':'Fast Moving Consumer Goods',  'MCDOWELL-N':'Fast Moving Consumer Goods',
  // Pharma & Healthcare
  'SUNPHARMA':'Healthcare', 'CIPLA':'Healthcare',    'DRREDDY':'Healthcare',
  'DIVISLAB':'Healthcare',  'AUROPHARMA':'Healthcare','LUPIN':'Healthcare',
  'APOLLOHOSP':'Healthcare','BIOCON':'Healthcare',   'TORNTPHARM':'Healthcare',
  // Metals
  'HINDALCO':'Metals & Mining', 'TATASTEEL':'Metals & Mining', 'JSWSTEEL':'Metals & Mining',
  'VEDL':'Metals & Mining',     'SAIL':'Metals & Mining',      'JINDALSTEL':'Metals & Mining',
  // Infra & Construction
  'LT':'Construction',          'ADANIPORTS':'Services',  'ADANIENT':'Metals & Mining',
  'GRASIM':'Construction Materials',      'ULTRACEMCO':'Construction Materials',  'AMBUJACEM':'Construction Materials',
  'DLF':'Construction',         'GODREJPROP':'Construction',  'CONCOR':'Services',
  'INDUSTOWER':'Telecommunication',
  // Consumer & Retail
  'TITAN':'Consumer Durables',    'ASIANPAINT':'Consumer Durables', 'BERGEPAINT':'Consumer Durables',
  'PIDILITIND':'Consumer Durables','VOLTAS':'Consumer Durables',    'HAVELLS':'Consumer Durables',
  'DMART':'Consumer Services',    'JUBLFOOD':'Consumer Services',   'PAGEIND':'Consumer Durables',
  'TRENT':'Consumer Services',
  // Telecom
  'BHARTIARTL':'Telecommunication',
  // Chemicals & Agri
  'UPL':'Chemicals', 'PIIND':'Chemicals', 'SRF':'Chemicals',
  // Capital Goods
  'SIEMENS':'Capital Goods', 'ABB':'Capital Goods', 'CUMMINSIND':'Capital Goods',
  // Internet / New Age
  'NAUKRI':'Consumer Services', 'ZOMATO':'Consumer Services', 'PAYTM':'Financial Services',
};

const IM_STOCK_WEIGHTS = {
  'nifty50': {
    'HDFCBANK': 10.94, 'RELIANCE': 8.87, 'ICICIBANK': 8.42, 'BHARTIARTL': 5.34,
    'INFY': 4.28, 'LT': 4.02, 'SBIN': 3.97, 'AXISBANK': 3.26, 'ITC': 2.71, 'M&M': 2.58
  },
  'banknifty': {
    'HDFCBANK': 29.2, 'ICICIBANK': 23.3, 'AXISBANK': 9.8, 'KOTAKBANK': 9.7, 'SBIN': 9.5,
    'INDUSINDBK': 5.2, 'BANKBARODA': 2.6, 'PNB': 2.2, 'AUBANK': 2.1, 'FEDERALBNK': 1.7,
    'IDFCFIRSTB': 1.6, 'BANDHANBNK': 1.1
  },
  'sensex': {
    'HDFCBANK': 13.5, 'RELIANCE': 11.5, 'ICICIBANK': 9.2, 'INFY': 6.5, 'ITC': 5.2,
    'TCS': 4.8, 'LT': 4.6, 'BHARTIARTL': 4.0, 'AXISBANK': 3.8, 'SBIN': 3.6,
  },
  'niftyit': { 'INFY': 26.5, 'TCS': 25.2, 'HCLTECH': 10.5, 'WIPRO': 8.8, 'TECHM': 8.2, 'LTIM': 6.0, 'PERSISTENT': 4.8, 'COFORGE': 3.1 },
  'bseit': { 'INFY': 26.5, 'TCS': 25.2, 'HCLTECH': 10.5, 'WIPRO': 8.8, 'TECHM': 8.2, 'LTIM': 6.0, 'PERSISTENT': 4.8, 'COFORGE': 3.1 },
  'bsebankex': {
    'HDFCBANK': 29.2, 'ICICIBANK': 23.3, 'AXISBANK': 9.8, 'KOTAKBANK': 9.7, 'SBIN': 9.5,
    'INDUSINDBK': 5.2, 'BANKBARODA': 2.6, 'PNB': 2.2, 'AUBANK': 2.1, 'FEDERALBNK': 1.7
  }
};

const IM_WEIGHTS_PUBLISHED_DATE = {
  'nifty50': 'Current (Ref)',
  'banknifty': 'Mar 29, 2026',
  'nifty100': 'Mar 29, 2026',
  'niftyit': 'Mar 29, 2026',
  'sensex': 'Mar 31, 2026',
  'bseit': 'Mar 31, 2026',
  'bsebankex': 'Mar 31, 2026',
  'bse100': 'Mar 31, 2026'
};

class IndexMovers {
  constructor() {
    this._data         = null;
    this._loading      = false;
    this._autoRefresh  = false;
    this._refreshTimer = null;
    this._lastUpdated  = null;
    this._day          = 0;   // 0 = today live, 1 = yesterday
    this._day          = 0;   // 0 = today live, 1 = yesterday
    this._expanded     = {};  // tracks which sectors are expanded
    this._prevData     = null;     // previous fetch for live comparison
  }

  // ── Controls ──
  _getIndex()     { return document.getElementById('im-index')?.value || 'nifty50'; }
  _getTopN()      { return parseInt(document.getElementById('im-topn')?.value || '10', 10); }
  _getThreshold() { return parseFloat(document.getElementById('im-threshold')?.value || '2'); }

  setDay(day) {
    this._day = day;
    const todayBtn = document.getElementById('im-day-today');
    const prevBtn  = document.getElementById('im-day-prev');
    if (todayBtn && prevBtn) {
      todayBtn.style.background = day === 0 ? 'var(--primary)' : 'var(--bg-card)';
      todayBtn.style.color      = day === 0 ? '#fff' : 'var(--text-secondary)';
      prevBtn.style.background  = day === 1 ? 'var(--primary)' : 'var(--bg-card)';
      prevBtn.style.color       = day === 1 ? '#fff' : 'var(--text-secondary)';
    }
    if (day === 1) this.stop();
    this.fetch();
  }

  // ── Fetch ──
  async fetch() {
    if (this._loading) return;
    this._loading = true;
    this._setFetchBtn(true);
    try {
      const res = await app.apiFetch(
        `/api/index-movers?index=${this._getIndex()}&top_n=50&day=${this._day}`
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: 'Server error' }));
        this._showError(err.error || 'Failed to fetch movers.');
        return;
      }
      const data = await res.json();
      if (data.error) { this._showError(data.error); return; }
      this._data        = data;
      this._lastUpdated = new Date();
      this.render();
    } catch (e) {
      this._showError('Could not connect to backend.');
    } finally {
      this._loading = false;
      this._setFetchBtn(false);
    }
  }

  // ── Auto-refresh ──
  toggleAutoRefresh() {
    if (this._day === 1) return;
    this._autoRefresh = !this._autoRefresh;
    const btn = document.getElementById('im-auto-refresh');
    if (btn) {
      btn.textContent = this._autoRefresh ? '⏹ Stop Auto' : '▶ Auto Refresh';
      btn.classList.toggle('btn-danger',    this._autoRefresh);
      btn.classList.toggle('btn-secondary', !this._autoRefresh);
    }
    if (this._autoRefresh) this._scheduleRefresh();
    else { clearTimeout(this._refreshTimer); this._refreshTimer = null; }
  }

  stop() {
    this._autoRefresh = false;
    clearTimeout(this._refreshTimer);
    this._refreshTimer = null;
    const btn = document.getElementById('im-auto-refresh');
    if (btn) {
      btn.textContent = '▶ Auto Refresh';
      btn.classList.remove('btn-danger');
      btn.classList.add('btn-secondary');
    }
  }

  _scheduleRefresh() {
    clearTimeout(this._refreshTimer);
    if (!this._autoRefresh || this._day === 1) return;
    this._refreshTimer = setTimeout(async () => {
      await this.fetch();
      this._scheduleRefresh();
    }, 5000);
  }

  _setFetchBtn(loading) {
    const btn = document.getElementById('im-fetch-btn');
    if (!btn) return;
    btn.disabled    = loading;
    btn.textContent = loading ? '⏳ Fetching...' : 'Fetch Movers';
  }

  _showError(msg) {
    const el = document.getElementById('im-sector-view');
    if (el) el.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-muted);">⚠️ ${msg}</div>`;
    this._updateStats(null);
  }

  // ── Render ──
  render() {
    if (!this._data) return;
    const d         = this._data;
    const threshold = this._getThreshold();
    const all       = d.all || [];

    // Backend now provides `open_change_pct` (true 09:15 change) and `change_pct` (current live or 15:30 close).
    // So we no longer need stateful snapshot capturing.

    // Track previous data for live comparison
    if (this._data && this._prevData) {
      // Attach trend arrows to current data
      for (const stock of all) {
        const prev = this._prevData.find(p => p.symbol === stock.symbol);
        stock._trend = prev ? (stock.change_pct > prev.change_pct ? '↑' : stock.change_pct < prev.change_pct ? '↓' : '→') : '';
      }
    }
    this._prevData = all.map(s => ({ symbol: s.symbol, change_pct: s.change_pct }));

    this._updateStats(d, threshold);
    this._renderOpeningSnapshot();
    this._renderSectorView(all);
    this._renderAlerts(all, threshold);
    this._updateTimestamp();
    this._updateExchangeBadge(d);
  }

  _renderOpeningSnapshot() {
    const container = document.getElementById('im-opening-snapshot');
    if (!container) {
      // Create the snapshot container if it doesn't exist (insert before sector view)
      const sectorCard = document.getElementById('im-sector-view')?.closest('.card');
      if (!sectorCard) return;
      const snapCard = document.createElement('div');
      snapCard.className = 'card mb-16';
      snapCard.innerHTML = `
        <div class="card-header" style="flex-wrap: wrap;">
          <div style="display:flex;align-items:center;gap:12px;width:100%;">
            <h3 style="margin:0;">📸 09:15 Opening Snapshot — Top 10 heavyweights</h3>
            <span style="font-size:0.65rem;font-weight:600;color:var(--primary);background:rgba(30,136,229,0.1);padding:3px 8px;border-radius:4px;" id="im-weights-date"></span>
          </div>
          <span style="font-size:0.72rem;color:var(--text-muted);display:block;margin-top:6px;" id="im-snap-time">—</span>
        </div>
        <div id="im-opening-snapshot" style="padding:4px 0;"></div>
      `;
      sectorCard.parentNode.insertBefore(snapCard, sectorCard);
    }

    const el = document.getElementById('im-opening-snapshot');
    const timeEl = document.getElementById('im-snap-time');
    if (!el) return;

    const indexName = this._getIndex();
    const stockWeights = IM_STOCK_WEIGHTS[indexName] || {};
    
    const all = this._data?.all || [];
    if (!all.length) {
      el.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.82rem;">No data available</div>';
      return;
    }

    const sortedByWeight = [...all].sort((a, b) => {
      const wA = stockWeights[a.symbol] || 0;
      const wB = stockWeights[b.symbol] || 0;
      if (wA !== wB) return wB - wA;
      return b.ltp - a.ltp;
    });

    const movers = sortedByWeight.slice(0, 10);

    if (timeEl) {
      if (this._day === 0) {
        timeEl.textContent = `Open price data representing 09:15 AM IST (Static)`;
      } else {
        timeEl.textContent = `Previous session open vs close`;
      }
    }

    const dateEl = document.getElementById('im-weights-date');
    if (dateEl) {
      const pubDate = IM_WEIGHTS_PUBLISHED_DATE[indexName] || 'Apr 2026';
      dateEl.textContent = `Weights Ref: ${pubDate}`;
      dateEl.style.display = pubDate ? 'inline-block' : 'none';
    }

    const weightTable = IM_SECTOR_WEIGHTS[indexName] || {};
    const fmt = (n, d = 2) => n != null ? (+n).toLocaleString('en-IN', { maximumFractionDigits: d }) : '—';

    el.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:0.8rem;">
        <thead>
          <tr style="font-size:0.68rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.05em;">
            <th style="padding:6px 8px;text-align:left;">#</th>
            <th style="padding:6px 8px;text-align:left;">Symbol</th>
            <th style="padding:6px 8px;text-align:left;">Sector</th>
            <th style="padding:6px 8px;text-align:right;">Weightage</th>
            <th style="padding:6px 8px;text-align:right;">Open Chg%</th>
            <th style="padding:6px 8px;text-align:right;">Current Chg%</th>
            <th style="padding:6px 8px;text-align:center;">Trend</th>
          </tr>
        </thead>
        <tbody>
          ${(() => {
            let totalWeight = 0;
            let totalOpenImpact = 0;
            let totalCurrentImpact = 0;

            const rows = movers.map((m, i) => {
              const sector = IM_SECTOR_MAP[m.symbol] || 'Other';
              const stockWeights = IM_STOCK_WEIGHTS[indexName] || {};
              const stockWeight = stockWeights[m.symbol];
              const weightDisplay = stockWeight != null ? `~${stockWeight}%` : (weightTable[sector] ? `Sec ~${weightTable[sector]}%` : '—');
              
              // Backend provides accurate open_change_pct (09:15 change)
              const openChg = m.open_change_pct != null ? m.open_change_pct : m.change_pct; // Fallback for safety
              const openColor = openChg >= 0 ? 'var(--green)' : 'var(--red)';
              const openSign = openChg >= 0 ? '+' : '';
              
              // Backend provides accurate change_pct (Live or 15:30 close change)
              const currentPct = m.change_pct;
              const currentColor = currentPct >= 0 ? 'var(--green)' : 'var(--red)';
              const currentSign = currentPct >= 0 ? '+' : '';
              
              const trend = currentPct > openChg ? '↑' : currentPct < openChg ? '↓' : '→';
              const trendColor = trend === '↑' ? 'var(--green)' : trend === '↓' ? 'var(--red)' : 'var(--text-muted)';
              
              if (stockWeight != null) {
                totalWeight += stockWeight;
                totalOpenImpact += (stockWeight * openChg);
                totalCurrentImpact += (stockWeight * currentPct);
              }

              return `<tr style="border-top:1px solid var(--border);">
                <td style="padding:6px 8px;color:var(--text-muted);font-size:0.72rem;">${i + 1}</td>
                <td style="padding:6px 8px;font-weight:700;">${m.symbol}</td>
                <td style="padding:6px 8px;color:var(--text-secondary);font-size:0.75rem;">${sector}</td>
                <td style="padding:6px 8px;text-align:right;font-size:0.72rem;color:var(--text-muted);">${weightDisplay}</td>
                <td style="padding:6px 8px;text-align:right;font-weight:600;color:${openColor};">${openSign}${openChg.toFixed(2)}%</td>
                <td style="padding:6px 8px;text-align:right;font-weight:800;color:${currentColor};">${currentSign}${currentPct.toFixed(2)}%</td>
                <td style="padding:6px 8px;text-align:center;font-size:1.1rem;color:${trendColor};">${trend}</td>
              </tr>`;
            }).join('');

            if (totalWeight > 0) {
              // Calculate TRUE index contribution: sum(weight * %change) / 100
              const openContribution = totalOpenImpact / 100;
              const currentContribution = totalCurrentImpact / 100;
              
              const openColor = openContribution >= 0 ? 'var(--green)' : 'var(--red)';
              const openSign = openContribution >= 0 ? '+' : '';
              const currentColor = currentContribution >= 0 ? 'var(--green)' : 'var(--red)';
              const currentSign = currentContribution >= 0 ? '+' : '';
              const trd = currentContribution > openContribution ? '↑' : (currentContribution < openContribution ? '↓' : '→');
              const trdCol = trd === '↑' ? 'var(--green)' : (trd === '↓' ? 'var(--red)' : 'var(--text-muted)');

              const totalRow = `<tr style="border-top:2px solid var(--border); background:rgba(0,0,0,0.02);">
                <td colspan="3" style="padding:10px 8px;font-weight:800;text-align:right;">TOTAL CONTRIBUTION</td>
                <td style="padding:10px 8px;text-align:right;font-weight:800;font-size:0.75rem;">${totalWeight.toFixed(2)}%</td>
                <td style="padding:10px 8px;text-align:right;font-weight:800;color:${openColor};">${openSign}${openContribution.toFixed(2)}%</td>
                <td style="padding:10px 8px;text-align:right;font-weight:800;color:${currentColor};">${currentSign}${currentContribution.toFixed(2)}%</td>
                <td style="padding:10px 8px;text-align:center;font-size:1.1rem;color:${trdCol};">${trd}</td>
              </tr>`;
              return rows + totalRow;
            }
            return rows;
          })()}
        </tbody>
      </table>
    `;
  }

  _updateStats(d, threshold) {
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    if (!d) {
      ['im-stat-total','im-stat-gainers','im-stat-losers','im-stat-updated'].forEach(id => set(id,'—'));
      return;
    }
    const all        = d.all || [];
    const alertCount = all.filter(s => Math.abs(s.change_pct) >= threshold).length;
    set('im-stat-total',   d.total || 0);
    set('im-stat-gainers', all.filter(s => s.change_pct > 0).length);
    set('im-stat-losers',  all.filter(s => s.change_pct < 0).length);
    set('im-stat-alerts',  `${alertCount} alert${alertCount !== 1 ? 's' : ''}`);
  }

  _updateExchangeBadge(d) {
    const el = document.getElementById('im-exchange-badge');
    if (!el) return;
    const exchange = d.exchange || 'NSE';
    const dateStr  = d.day === 1 && d.all?.[0]?.date ? ` · ${d.all[0].date}` : '';
    el.textContent       = `${exchange}${dateStr}`;
    el.style.background  = exchange === 'BSE' ? 'rgba(255,152,0,0.15)' : 'rgba(30,136,229,0.15)';
    el.style.color       = exchange === 'BSE' ? '#FF9800' : 'var(--primary)';
  }

  // ── Sector View ──
  _sectorKey(name) { return name.replace(/\s+/g,'_').replace(/[^a-zA-Z0-9_]/g,''); }

  _renderSectorView(stocks) {
    const container = document.getElementById('im-sector-view');
    if (!container) return;
    if (!stocks.length) {
      container.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted);">No data</div>';
      return;
    }

    // Get index-specific sector weights (or empty for dynamic computation)
    const indexName   = this._getIndex();
    const weightTable = IM_SECTOR_WEIGHTS[indexName] || {};

    // Group by sector
    const groups = {};
    for (const s of stocks) {
      const sector = IM_SECTOR_MAP[s.symbol] || 'Others';
      if (!groups[sector]) groups[sector] = [];
      groups[sector].push(s);
    }

    // Compute sector stats + weightage
    const totalStocks = stocks.length;
    const sectors = Object.entries(groups).map(([name, items]) => {
      const avg     = items.reduce((sum, s) => sum + s.change_pct, 0) / items.length;
      const gainers = items.filter(s => s.change_pct > 0).length;
      const losers  = items.filter(s => s.change_pct < 0).length;
      // Use static weight if available, else approximate by stock count
      const weight  = weightTable[name] != null
        ? weightTable[name]
        : +((items.length / totalStocks) * 100).toFixed(0);
      return { name, items, avg: +avg.toFixed(2), gainers, losers, weight };
    });

    // Sort: gaining sectors first (desc by avg)
    sectors.sort((a, b) => b.avg - a.avg);

    // Max abs avg for bar scaling
    const maxAbs = Math.max(...sectors.map(s => Math.abs(s.avg)), 1);

    container.innerHTML = sectors.map(sec => {
      const key      = this._sectorKey(sec.name);
      const isOpen   = !!this._expanded[key];
      const isPos    = sec.avg >= 0;
      const color    = isPos ? 'var(--green)' : 'var(--red)';
      const bgColor  = isPos ? 'rgba(38,166,154,0.08)' : 'rgba(239,83,80,0.08)';
      const sign     = isPos ? '+' : '';
      const barPct   = Math.min((Math.abs(sec.avg) / maxAbs) * 100, 100).toFixed(1);
      const icon     = isOpen ? '▼' : '▶';

      // Individual stock rows (hidden until expanded)
      const stocksSorted = [...sec.items].sort((a, b) => b.change_pct - a.change_pct);
      const stockRows = stocksSorted.map((s, i) => {
        const fmt      = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits:d}) : '—';
        const sc       = s.change_pct >= 0 ? 'var(--green)' : 'var(--red)';
        const ss       = s.change_pct >= 0 ? '+' : '';
        const rw       = (s.day_high || s.ltp) - (s.day_low || s.ltp);
        const pos      = rw > 0 ? (((s.ltp - s.day_low) / rw) * 100).toFixed(1) : 50;
        return `<tr style="cursor:pointer;font-size:0.8rem;" onclick="
          (function(){
            const inp=document.getElementById('analysis-symbol-input');
            if(inp) inp.value='${s.symbol}';
            document.querySelector('[data-page=analysis]')?.click();
            setTimeout(()=>document.getElementById('btn-analyse')?.click(),300);
          })()">
          <td style="color:var(--text-muted);font-size:0.72rem;padding:6px 8px;">${i+1}</td>
          <td style="font-weight:700;padding:6px 8px;">${s.symbol}</td>
          <td style="padding:6px 8px;">₹${fmt(s.ltp)}</td>
          <td style="font-weight:800;color:${sc};padding:6px 8px;">${ss}${(+s.change_pct).toFixed(2)}%</td>
          <td style="padding:6px 8px;">
            <div style="display:flex;align-items:center;gap:4px;font-size:0.68rem;color:var(--text-muted);">
              <span>₹${fmt(s.day_low,0)}</span>
              <div style="flex:1;height:3px;background:var(--border);border-radius:2px;position:relative;min-width:40px;">
                <div style="position:absolute;top:-3px;left:${pos}%;width:8px;height:8px;border-radius:50%;
                  background:${s.change_pct>=0?'var(--green)':'var(--red)'};transform:translateX(-50%);"></div>
              </div>
              <span>₹${fmt(s.day_high,0)}</span>
            </div>
          </td>
        </tr>`;
      }).join('');

      return `
        <div style="border-bottom:1px solid var(--border);">
          <!-- Sector Header Row -->
          <div onclick="indexMovers._toggleSector('${key}')"
            style="display:flex;align-items:center;gap:12px;padding:11px 16px;cursor:pointer;
                   background:${isOpen ? bgColor : 'transparent'};transition:background 0.15s;user-select:none;"
            onmouseenter="this.style.background='${bgColor}'" onmouseleave="this.style.background='${isOpen ? bgColor : 'transparent'}'">

            <!-- Toggle Icon -->
            <span id="im-tog-${key}" style="font-size:0.65rem;color:var(--text-muted);width:12px;transition:transform 0.2s;">${icon}</span>

            <!-- Sector Name + Weight Badge -->
            <span style="font-weight:700;font-size:0.88rem;min-width:110px;">${sec.name}</span>
            <span title="Approx. index weightage" style="font-size:0.65rem;padding:1px 5px;border-radius:4px;
              background:rgba(255,255,255,0.06);color:var(--text-muted);font-weight:600;white-space:nowrap;">
              ~${sec.weight}%
            </span>

            <!-- Change Bar — center line at 50%, bar grows left (negative) or right (positive) -->
            <div style="flex:1;height:6px;background:var(--border);border-radius:3px;position:relative;max-width:180px;">
              <div style="position:absolute;top:0;height:100%;border-radius:3px;background:${color};
                ${isPos
                  ? `left:50%;width:${(barPct/2).toFixed(1)}%;`
                  : `right:50%;width:${(barPct/2).toFixed(1)}%;`}
                "></div>
              <div style="position:absolute;left:50%;top:-1px;width:1px;height:8px;background:var(--text-muted);opacity:0.4;"></div>
            </div>

            <!-- Avg Change % -->
            <span style="font-weight:800;font-size:0.9rem;color:${color};min-width:64px;text-align:right;">
              ${sign}${sec.avg.toFixed(2)}%
            </span>

            <!-- Stock count + gainers/losers -->
            <span style="font-size:0.72rem;color:var(--text-muted);min-width:120px;text-align:right;">
              ${sec.items.length} stocks &nbsp;
              <span style="color:var(--green);font-weight:600;">${sec.gainers}↑</span>
              &nbsp;
              <span style="color:var(--red);font-weight:600;">${sec.losers}↓</span>
            </span>
          </div>

          <!-- Expanded Stocks Table -->
          <div id="im-stocks-${key}" style="display:${isOpen ? 'block' : 'none'};background:rgba(0,0,0,0.15);">
            <table style="width:100%;border-collapse:collapse;">
              <thead>
                <tr style="font-size:0.7rem;color:var(--text-muted);">
                  <th style="padding:5px 8px;font-weight:500;text-align:left;width:30px;">#</th>
                  <th style="padding:5px 8px;font-weight:500;text-align:left;">Symbol</th>
                  <th style="padding:5px 8px;font-weight:500;text-align:left;">LTP</th>
                  <th style="padding:5px 8px;font-weight:500;text-align:left;">Chg%</th>
                  <th style="padding:5px 8px;font-weight:500;text-align:left;">Day Range</th>
                </tr>
              </thead>
              <tbody>${stockRows}</tbody>
            </table>
          </div>
        </div>`;
    }).join('');
  }

  _toggleSector(key) {
    this._expanded[key] = !this._expanded[key];
    const stocks = document.getElementById(`im-stocks-${key}`);
    const icon   = document.getElementById(`im-tog-${key}`);
    if (stocks) stocks.style.display = this._expanded[key] ? 'block' : 'none';
    if (icon)   icon.textContent     = this._expanded[key] ? '▼' : '▶';
  }

  // ── Alerts ──
  _renderAlerts(stocks, threshold) {
    const el = document.getElementById('im-alerts-body');
    if (!el) return;
    const triggered = stocks.filter(s => Math.abs(s.change_pct) >= threshold);
    if (!triggered.length) {
      el.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:16px;color:var(--text-muted);">No alerts yet. Fetch data to check.</td></tr>';
      return;
    }
    const fmt = (n, d=2) => n != null ? (+n).toLocaleString('en-IN', {maximumFractionDigits:d}) : '—';
    el.innerHTML = triggered.map(s => {
      const dir  = s.change_pct >= 0 ? '▲' : '▼';
      const cls  = s.change_pct >= 0 ? 'text-green' : 'text-red';
      const sign = s.change_pct >= 0 ? '+' : '';
      return `<tr>
        <td style="font-weight:700;">${s.symbol}</td>
        <td>₹${fmt(s.ltp)}</td>
        <td class="${cls}" style="font-weight:800;">${dir} ${sign}${(+s.change_pct).toFixed(2)}%</td>
      </tr>`;
    }).join('');
  }

  _updateTimestamp() {
    const el = document.getElementById('im-stat-updated');
    if (!el) return;
    if (this._day === 1 && this._data?.all?.[0]?.date) {
      el.textContent = this._data.all[0].date;
    } else if (this._lastUpdated) {
      el.textContent = this._lastUpdated.toLocaleTimeString('en-IN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
    }
  }
}

window.indexMovers = new IndexMovers();
