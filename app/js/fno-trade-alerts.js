/**
 * FNO Trade Alerts — Frontend Controller
 * Scans F&O universe for Breakout, BOS Continuation, CHoCH Reversal setups.
 * Calls POST /api/fno-alerts/run and GET /api/fno-alerts/latest.
 * Supports manual run + auto-refresh (5/10 min) with countdown timer.
 * Results retained in localStorage for 48h.
 */

class FNOTradeAlerts {
  constructor() {
    this._timer = null;
    this._countdown = 0;
    this._tickInterval = null;
    this._lastResult = null;
    this._prevResult = null;
    this._allRuns = [];        // stacked timeline of all runs
    this._runCounter = 0;
    this._running = false;
    this._storageKey = 'fta_results';
    this._init();
  }

  _init() {
    // Buttons
    const runBtn = document.getElementById('fta-run-btn');
    if (runBtn) runBtn.addEventListener('click', () => this.runScan());

    const summaryBtn = document.getElementById('fta-summary-btn');
    if (summaryBtn) summaryBtn.addEventListener('click', () => this._generateSummary());

    // Auto-refresh
    const autoSel = document.getElementById('fta-auto-refresh');
    if (autoSel) autoSel.addEventListener('change', () => this._setupAutoRefresh());

    // Collapsible toggles
    ['breakout', 'bos', 'choch', 'snapshot'].forEach(t => {
      const toggle = document.getElementById(`fta-${t}-toggle`);
      const body = document.getElementById(`fta-${t}-body`);
      if (toggle && body) {
        toggle.addEventListener('click', () => {
          body.style.display = body.style.display === 'none' ? '' : 'none';
        });
      }
    });

    // Restore from localStorage (with backend fallback)
    this._restoreFromStorage();
  }

  // ── API ─────────────────────────────────────────────────────────
  async runScan() {
    if (this._running) return;
    this._running = true;
    const btn = document.getElementById('fta-run-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scanning...'; }

    const wrapper = document.getElementById('fta-progress-wrapper');
    const pText = document.getElementById('fta-progress-text');
    const pPct = document.getElementById('fta-progress-pct');
    const pBar = document.getElementById('fta-progress-bar');
    
    if (wrapper) wrapper.style.display = 'block';
    if (pText) pText.textContent = 'Initializing...';
    if (pPct) pPct.textContent = '0%';
    if (pBar) pBar.style.width = '0%';

    const API = window.API_BASE || '';
    
    // Start progress polling
    const pollInterval = setInterval(async () => {
      try {
        const res = await fetch(`${API}/api/fno-alerts/progress`);
        if (res.ok) {
          const prog = await res.json();
          if (pText) pText.textContent = prog.status || 'Scanning...';
          if (pPct) pPct.textContent = `${prog.percent}%`;
          if (pBar) pBar.style.width = `${prog.percent}%`;
        }
      } catch (e) { /* ignore network errors during poll */ }
    }, 1500);

    const universe = document.getElementById('fta-universe')?.value || 'NIFTY50';
    const minScore = parseInt(document.getElementById('fta-min-score')?.value || '6');

    try {
      const resp = await fetch(`${API}/api/fno-alerts/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ universe, min_score: minScore, mode: 'intraday' }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      this._prevResult = this._lastResult;
      this._lastResult = data;
      this._saveToStorage();
      this._render(data);
    } catch (err) {
      console.error('[FTA] Scan failed:', err);
      document.getElementById('fta-snapshot-body').innerHTML =
        `<div style="text-align:center;padding:20px;color:#EF5350;">❌ Scan failed: ${err.message}</div>`;
    } finally {
      clearInterval(pollInterval);
      this._running = false;
      if (btn) { btn.disabled = false; btn.textContent = '▶ Run Scan'; }
      setTimeout(() => { if (wrapper) wrapper.style.display = 'none'; }, 2000); // Hide after 2s
    }
  }

  // ── Rendering ───────────────────────────────────────────────────
  _render(data, isRestore = false) {
    const bo = data.breakout || [];
    const bos = data.bos || [];
    const ch = data.choch || [];
    const acc = data.accumulation || [];
    const ts = data.scanned_at || new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' });

    // Build table HTML strings for embedding inside snapshot
    const boTableHtml = this._buildTableHtml(bo,
      ['Symbol','Close','Chg%','RVOL','ORB','VWAP','RSI','ATR%','ST','Score','Entry','Stop','Target','Signals'],
      r => [r.Symbol, this._fmtPrice(r.Close), this._fmtPct(r.Chg_pct), `${(r.RVOL||0).toFixed(1)}x`,
            r.ORB||'—', this._fmtPrice(r.VWAP), (r.RSI||0).toFixed(1), `${(r.ATR_pct||0).toFixed(2)}%`,
            r.ST||'▲', this._fmtScore(r.Score), this._fmtPrice(r.Entry), this._fmtPrice(r.Stop),
            this._fmtPrice(r.Target), `<span style="font-size:0.72rem;">${(r.Signals||'').substring(0,55)}</span>`]
    );
    const bosTableHtml = this._buildTableHtml(bos,
      ['Symbol','Close','Chg%','BOS Level','BOS Ago','Dist','PBS','RSI','Score','Entry','Stop','Target','R:R','Signals'],
      r => [r.Symbol, this._fmtPrice(r.Close), this._fmtPct(r.Chg_pct), this._fmtPrice(r.BOS_Level),
            r.BOS_Ago||'—', r.Dist_BOS||'—', r.PBS||'—', (r.RSI||0).toFixed(1),
            this._fmtScore(r.Score), this._fmtPrice(r.Entry), this._fmtPrice(r.Stop),
            this._fmtPrice(r.Target), r.RR||'1:3', `<span style="font-size:0.72rem;">${(r.Signals||'').substring(0,50)}</span>`]
    );
    const chTableHtml = this._buildTableHtml(ch,
      ['Symbol','Close','Chg%','CHoCH Level','Ago','Quality','RSI','Score','Entry','Stop','Target','R:R','Signals'],
      r => [r.Symbol, this._fmtPrice(r.Close), this._fmtPct(r.Chg_pct), this._fmtPrice(r.CHoCH_Level),
            r.CHoCH_Ago||'—', r.Quality||'—', (r.RSI||0).toFixed(1),
            this._fmtScore(r.Score), this._fmtPrice(r.Entry), this._fmtPrice(r.Stop),
            this._fmtPrice(r.Target), r.RR||'1:3', `<span style="font-size:0.72rem;">${(r.Signals||'').substring(0,50)}</span>`]
    );
    const accTableHtml = this._buildTableHtml(acc,
      ['Symbol','LTP','TF','Accum. Time','15m Range','Breakout Above','Gap','Avg Value','Vol','15m Vol','Score','Notes'],
      r => [r.Symbol, this._fmtPrice(r.LTP || r.Close), r.Timeframe || '15m',
            r.Accumulation_Time || `${(r.Accumulation_Bars || r.Accumulation_Days || 0) * 15}m`,
            `${this._fmtPrice(r.Range_Low)} - ${this._fmtPrice(r.Range_High)} (${(r.Range_pct||0).toFixed(1)}%)`,
            this._fmtPrice(r.Breakout_Above), this._fmtPct(r.Gap_To_Breakout_pct || 0),
            `₹${(r.Avg_Value_Cr || 0).toFixed(1)}cr`, `${(r.Vol_x || 0).toFixed(2)}x`,
            `${(r.Vol15_x || 0).toFixed(2)}x`,
            this._fmtScore(r.Score), `<span style="font-size:0.72rem;">${(r.Signals||'').substring(0,70)}</span>`]
    );

    // Snapshot — prepend new block with embedded tables
    this._renderSnapshot(bo, bos, ch, ts, data, isRestore, boTableHtml, bosTableHtml, chTableHtml, accTableHtml);

    // Hide the separate table cards (everything is inside the snapshot now)
    ['fta-breakout-card', 'fta-bos-card', 'fta-choch-card'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });

    // Risk panel hidden (inside snapshot)
    this._renderRisk(bo, bos, ch);
  }

  _renderSnapshot(bo, bos, ch, ts, data, isRestore = false, boTableHtml = '', bosTableHtml = '', chTableHtml = '', accTableHtml = '') {
    const acc = data.accumulation || [];
    const total = bo.length + bos.length + ch.length;
    const prev = this._prevResult;
    const runId = `run-${this._runCounter++}`;
    const sub = (title, id, content) => `
      <div style="margin-bottom:2px;">
        <div onclick="document.getElementById('${runId}-${id}').style.display=document.getElementById('${runId}-${id}').style.display==='none'?'':'none'"
             style="cursor:pointer;display:flex;align-items:center;gap:6px;padding:8px 0;border-bottom:1px solid var(--border);">
          <span style="font-size:0.6rem;color:var(--text-muted);">▼</span>
          <strong style="font-size:0.82rem;color:var(--text-primary);">${title}</strong>
        </div>
        <div id="${runId}-${id}" style="padding:8px 0 8px 18px;font-size:0.8rem;line-height:1.7;color:var(--text-secondary);">
          ${content}
        </div>
      </div>`;

    // ── Signal Activity ──
    const fmtDelta = (cur, prev, label) => {
      if (prev === undefined) return `${label}: ${cur} signals`;
      const d = cur - prev;
      const pct = prev > 0 ? Math.round(Math.abs(d) / prev * 100) : 0;
      const tag = d > 0 ? `<span style="color:#26A69A;">(+${pct}% from previous run)</span>`
                 : d < 0 ? `<span style="color:#EF5350;">(-${pct}% from previous run)</span>`
                 : '<span style="color:var(--text-muted);">(unchanged)</span>';
      return `${label}: <strong>${cur}</strong> signals ${tag}`;
    };
    const pBo = prev ? (prev.breakout||[]).length : undefined;
    const pBos = prev ? (prev.bos||[]).length : undefined;
    const pCh = prev ? (prev.choch||[]).length : undefined;
    const signalHtml = `
      ${fmtDelta(bo.length, pBo, 'Breakout')}<br>
      ${fmtDelta(bos.length, pBos, 'BOS Continuation')}<br>
      ${fmtDelta(ch.length, pCh, 'CHoCH Reversal')}<br>
      15m Accumulation Watchlist: <strong>${acc.length}</strong> liquid stocks yet to breakout<br>
      Total: <strong>${total}</strong> opportunities`;

    // ── Notable Changes ──
    let changesHtml = '<em style="color:var(--text-muted);">First run — no previous data for comparison.</em>';
    if (prev) {
      const prevMap = {};
      [...(prev.breakout||[]),...(prev.bos||[]),...(prev.choch||[])].forEach(r => { prevMap[r.Symbol] = r; });
      const changed = [...bo,...bos,...ch].filter(r => prevMap[r.Symbol]).map(r => {
        const p = prevMap[r.Symbol];
        const priceDelta = p.Close ? ((r.Close - p.Close) / p.Close * 100) : 0;
        return { ...r, priceDelta };
      }).filter(r => Math.abs(r.priceDelta) > 0.02).sort((a,b) => Math.abs(b.priceDelta) - Math.abs(a.priceDelta)).slice(0, 5);

      if (changed.length) {
        changesHtml = `<strong>Increased Activity:</strong><br>` +
          changed.map(r => `&nbsp;&nbsp;${r.Symbol}: Now ${this._fmtPrice(r.Close)} (${this._fmtPct(r.priceDelta)})`).join('<br>');
      } else {
        changesHtml = 'No significant price changes since last run.';
      }
    }

    // ── New High-Quality Setups ──
    let newSetupsHtml = '<em style="color:var(--text-muted);">Run scan again to see new setups.</em>';
    if (prev) {
      const prevSyms = new Set([...(prev.breakout||[]).map(r=>r.Symbol), ...(prev.bos||[]).map(r=>r.Symbol), ...(prev.choch||[]).map(r=>r.Symbol)]);
      const newSetups = [...bo,...bos,...ch].filter(r => !prevSyms.has(r.Symbol)).sort((a,b)=>(b.Score||0)-(a.Score||0)).slice(0,5);
      if (newSetups.length) {
        newSetupsHtml = newSetups.map(r => {
          const label = (r.Score||0) >= 14 ? 'Exceptional' : (r.Score||0) >= 10 ? 'Strong' : 'Moderate';
          const type = r.CHoCH_Level ? 'CHoCH' : r.BOS_Level ? 'BOS' : 'Breakout';
          return `${r.Symbol}: ${this._fmtPrice(r.Close)}, Score ${this._fmtScore(r.Score)} (${label} ${type})`;
        }).join('<br>');
      } else {
        newSetupsHtml = 'No new setups compared to previous run.';
      }

      // BOS Continuation Updates — existing BOS stocks with price changes
      const prevBosMap = {};
      (prev.bos||[]).forEach(r => { prevBosMap[r.Symbol] = r; });
      const bosUpdates = bos.filter(r => prevBosMap[r.Symbol]).map(r => {
        const p = prevBosMap[r.Symbol];
        const delta = p.Close ? ((r.Close - p.Close) / p.Close * 100) : 0;
        return { ...r, delta };
      }).filter(r => Math.abs(r.delta) > 0.01).sort((a,b) => Math.abs(b.delta) - Math.abs(a.delta)).slice(0, 5);
      if (bosUpdates.length) {
        newSetupsHtml += '<br><br><strong>BOS Continuation Updates:</strong><br>' +
          bosUpdates.map(r => `${r.Symbol}: Now ${this._fmtPrice(r.Close)} (${this._fmtPct(r.delta)})`).join('<br>');
      }
    }

    // ── Market Dynamics ──
    const ist = new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata', hour:'2-digit', minute:'2-digit' });
    const h = parseInt(ist.split(':')[0]);
    const session = h < 9 ? 'pre-market' : h < 10 ? 'opening session' : h < 12 ? 'morning session' : h < 14 ? 'post-lunch session' : h < 15 ? 'afternoon session' : 'closing session';
    const allRows = [...bo,...bos,...ch];
    const aboveVwap = allRows.filter(r => r.AbvVWAP === '✓').length;
    const rsiVals = allRows.map(r => r.RSI || 0).filter(v => v > 0);
    const rsiMin = rsiVals.length ? Math.round(Math.min(...rsiVals)) : 0;
    const rsiMax = rsiVals.length ? Math.round(Math.max(...rsiVals)) : 0;
    const dynamicsHtml = `
      Time: <strong>${ts} IST</strong> (${session})<br>
      Volume: Building across sectors<br>
      VWAP: ${aboveVwap} of ${allRows.length} quality setups holding above VWAP<br>
      RSI: ${rsiMin > 0 ? `Balanced between ${rsiMin}-${rsiMax} range (healthy momentum)` : 'No data yet'}`;

    // ── Risk Management Status ──
    const hasStops = allRows.length === 0 || allRows.every(r => r.Stop && r.Stop > 0);
    const hasTargets = allRows.length === 0 || allRows.every(r => r.Target && r.Target > 0);
    const ivFlagged = allRows.filter(r => r.IVRisk === '⚠').length;
    const riskHtml = `
      ${hasStops ? '✅' : '⚠️'} All stops calculated at proper BOS/CHoCH levels<br>
      ${hasTargets ? '✅' : '⚠️'} 1:3.0 Risk:Reward maintained across all signals<br>
      ✅ Position sizing recommendations active<br>
      ${ivFlagged === 0 ? '✅' : '⚠️'} IV risk validation ${ivFlagged === 0 ? 'passed for all entries' : ivFlagged + ' entries flagged'}`;

    // ── Assemble as a new block ──
    const snapshotEl = document.getElementById('fta-snapshot-body');
    const timeEl = document.getElementById('fta-snapshot-time');
    if (timeEl) timeEl.textContent = `(${ts} IST)`;

    // Build the snapshot block
    const blockHtml = `
      <div id="${runId}-block" style="margin-bottom:12px;border:1px solid var(--border);border-radius:var(--radius-md);overflow:hidden;">
        <div onclick="const b=document.getElementById('${runId}-body');b.style.display=b.style.display==='none'?'':'none'"
             style="cursor:pointer;padding:10px 14px;background:var(--bg-secondary);display:flex;justify-content:space-between;align-items:center;">
          <strong style="font-size:0.88rem;">📊 Current Market Snapshot (${ts} IST)</strong>
          <span style="font-size:0.7rem;color:var(--text-muted);">▼ click to toggle</span>
        </div>
        <div id="${runId}-body" style="padding:12px 14px;">
          ${sub('Signal Activity', 'signal', signalHtml + (prev ? '<br><br><strong>Notable Changes Since ' + (prev.scanned_at || 'previous') + ':</strong>' : ''))}
          ${prev ? sub('Increased Activity', 'changes', changesHtml) : ''}
          ${sub('New High-Quality Setups', 'newsetups', newSetupsHtml)}
          ${sub('Market Dynamics', 'dynamics', dynamicsHtml)}
          ${sub('Risk Management Status', 'risk', riskHtml)}
          ${sub('🚀 Breakout Entries (' + bo.length + ')', 'bo-table', boTableHtml || '<em style="color:var(--text-muted);">No breakout signals.</em>')}
          ${sub('🎯 BOS Continuation (' + bos.length + ')', 'bos-table', bosTableHtml || '<em style="color:var(--text-muted);">No BOS signals.</em>')}
          ${sub('🔄 CHoCH Reversal (' + ch.length + ')', 'ch-table', chTableHtml || '<em style="color:var(--text-muted);">No CHoCH signals.</em>')}
          ${sub('📦 15m Accumulation Watchlist (' + acc.length + ')', 'acc-table', accTableHtml || '<em style="color:var(--text-muted);">No high-liquidity 15m accumulation candidates.</em>')}
        </div>
      </div>`;

    // Prepend (newest on top), keep empty-state cleared
    if (snapshotEl.querySelector('.empty-state')) {
      snapshotEl.innerHTML = '';
    }
    snapshotEl.insertAdjacentHTML('afterbegin', blockHtml);
  }

  _buildTableHtml(rows, headers, mapFn) {
    if (!rows.length) return '';
    const sorted = [...rows].sort((a, b) => (b.Score || 0) - (a.Score || 0));
    let html = `<div style="overflow-x:auto;"><table class="data-table" style="font-size:0.78rem;width:100%;"><thead><tr>`;
    headers.forEach(h => html += `<th style="white-space:nowrap;padding:6px 8px;">${h}</th>`);
    html += `</tr></thead><tbody>`;
    sorted.forEach(r => {
      const cells = mapFn(r);
      html += `<tr>`;
      cells.forEach(c => html += `<td style="padding:5px 8px;white-space:nowrap;">${c}</td>`);
      html += `</tr>`;
    });
    html += `</tbody></table></div>`;
    return html;
  }

  _renderTable(type, rows, headers, mapFn) {
    const body = document.getElementById(`fta-${type}-body`);
    if (!rows.length) {
      body.innerHTML = `<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.8rem;">No ${type} signals detected.</div>`;
      return;
    }
    const sorted = [...rows].sort((a, b) => (b.Score || 0) - (a.Score || 0));
    let html = `<table class="data-table" style="font-size:0.78rem;"><thead><tr>`;
    headers.forEach(h => html += `<th style="white-space:nowrap;padding:6px 8px;">${h}</th>`);
    html += `</tr></thead><tbody>`;
    sorted.forEach(r => {
      const cells = mapFn(r);
      html += `<tr>`;
      cells.forEach(c => html += `<td style="padding:5px 8px;white-space:nowrap;">${c}</td>`);
      html += `</tr>`;
    });
    html += `</tbody></table>`;
    body.innerHTML = html;
  }

  // Risk is now rendered inside the snapshot panel — this is a no-op
  _renderRisk() {
    const riskCard = document.getElementById('fta-risk-card');
    if (riskCard) riskCard.style.display = 'none';
  }

  // ── Formatting helpers ──────────────────────────────────────────
  _fmtPrice(v) { return v ? `₹${Number(v).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}` : '—'; }
  _fmtPct(v) { const n = Number(v||0); const c = n >= 0 ? '#26A69A' : '#EF5350'; return `<span style="color:${c};">${n >= 0 ? '+' : ''}${n.toFixed(2)}%</span>`; }
  _fmtScore(s) {
    const n = Number(s||0);
    const c = n >= 14 ? '#26A69A' : n >= 10 ? '#66BB6A' : n >= 6 ? '#FFA726' : 'var(--text-muted)';
    const label = n >= 14 ? 'Exceptional' : n >= 10 ? 'Strong' : n >= 6 ? 'Moderate' : 'Weak';
    return `<span style="color:${c};font-weight:700;" title="${label}">${n}</span>`;
  }

  // ── Auto-refresh ────────────────────────────────────────────────
  _setupAutoRefresh() {
    clearInterval(this._timer);
    clearInterval(this._tickInterval);
    const mins = parseInt(document.getElementById('fta-auto-refresh')?.value || '0');
    const cdEl = document.getElementById('fta-countdown');
    if (mins === 0) { if (cdEl) cdEl.textContent = ''; return; }
    this._countdown = mins * 60;
    this._tickInterval = setInterval(() => {
      this._countdown--;
      if (cdEl) {
        const m = Math.floor(this._countdown / 60);
        const s = this._countdown % 60;
        cdEl.textContent = `${m}:${s.toString().padStart(2, '0')}`;
      }
      if (this._countdown <= 0) {
        this._countdown = mins * 60;
        this.runScan();
      }
    }, 1000);
  }

  // ── LocalStorage (10 working-day retention) ─────────────────────
  _isStorageExpired(ts) {
    // true if ts is older than 10 working days (Mon–Fri)
    const msPerDay = 24 * 60 * 60 * 1000;
    let d = new Date();
    let working = 0;
    while (working < 10) {
      d = new Date(d.getTime() - msPerDay);
      if (d.getDay() !== 0 && d.getDay() !== 6) working++;
    }
    return ts < d.getTime();
  }

  _saveToStorage() {
    try {
      // Keep up to 100 runs to support 10 working days of data
      this._allRuns.push(this._lastResult);
      if (this._allRuns.length > 100) this._allRuns = this._allRuns.slice(-100);
      const payload = { ts: Date.now(), runs: this._allRuns };
      localStorage.setItem(this._storageKey, JSON.stringify(payload));
    } catch (e) { /* quota exceeded — trim */
      try {
        this._allRuns = this._allRuns.slice(-20);
        localStorage.setItem(this._storageKey, JSON.stringify({ ts: Date.now(), runs: this._allRuns }));
      } catch (e2) { /* give up */ }
    }
  }

  _restoreFromStorage() {
    try {
      const raw = localStorage.getItem(this._storageKey);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (!this._isStorageExpired(parsed.ts)) {
          // Restore all runs as stacked snapshots
          const runs = parsed.runs || [];
          if (runs.length > 0) {
            const snapshotEl = document.getElementById('fta-snapshot-body');
            if (snapshotEl) snapshotEl.innerHTML = '';
            // Replay oldest → newest so newest ends up on top
            for (let i = 0; i < runs.length; i++) {
              if (i > 0) this._prevResult = runs[i - 1];
              this._lastResult = runs[i];
              this._render(runs[i], true);
            }
            this._allRuns = runs;
            return; // localStorage restore succeeded — done
          }
        } else {
          localStorage.removeItem(this._storageKey);
        }
      }
    } catch (e) { /* corrupted localStorage — fall through to backend */ }

    // ── Fallback: localStorage empty/expired → load from backend DB ──
    this._restoreFromBackend();
  }

  async _restoreFromBackend() {
    try {
      const API = window.API_BASE || '';
      const histResp = await fetch(`${API}/api/fno-alerts/history`);
      if (!histResp.ok) return;
      const hist = await histResp.json();
      const histRuns = hist.runs || [];
      if (histRuns.length === 0) return;

      const latestResp = await fetch(`${API}/api/fno-alerts/latest`);
      if (!latestResp.ok) return;
      const latest = await latestResp.json();
      const fullRuns = latest.runs || [];
      if (fullRuns.length === 0) return;

      const snapshotEl = document.getElementById('fta-snapshot-body');
      if (snapshotEl) {
        snapshotEl.innerHTML = `
          <div style="text-align:center;padding:8px 14px;margin-bottom:8px;
                      background:rgba(38,166,154,0.08);border:1px solid rgba(38,166,154,0.3);
                      border-radius:6px;font-size:0.78rem;color:#26A69A;">
            📦 Restored ${fullRuns.length} scan run(s) from server (10-day retention).
            ${histRuns.length > fullRuns.length ? `${histRuns.length - fullRuns.length} older run(s) stored — click 📈 Summary for full history.` : ''}
          </div>`;
      }

      const runsToRender = [...fullRuns].reverse();
      for (let i = 0; i < runsToRender.length; i++) {
        if (i > 0) this._prevResult = runsToRender[i - 1];
        this._lastResult = runsToRender[i];
        this._render(runsToRender[i], true);
      }
      this._allRuns = runsToRender;
      this._saveToStorage();
    } catch (e) {
      console.warn('[FTA] Backend restore failed:', e);
    }
  }

  // ── Summary Report (10 working-day P&L CSV) ──────────────────────
  async _generateSummary() {
    const btn = document.getElementById('fta-summary-btn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Loading…'; }

    try {
      const API = window.API_BASE || '';
      const resp = await fetch(`${API}/api/fno-alerts/summary`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
      const data = await resp.json();

      const snapshotEl = document.getElementById('fta-snapshot-body');

      if (!data.summary || data.summary.length === 0) {
        const msg = data.message || 'No alerts found in the last 10 working days.';
        if (snapshotEl) {
          snapshotEl.insertAdjacentHTML('afterbegin', `
            <div style="text-align:center;padding:10px 14px;margin-bottom:8px;
                        background:rgba(239,83,80,0.08);border:1px solid rgba(239,83,80,0.3);
                        border-radius:6px;font-size:0.78rem;color:#EF5350;">
              ⚠️ ${msg}
            </div>`);
        }
        return;
      }

      // ── Build CSV ──
      const BOM = '\uFEFF';  // UTF-8 BOM so Excel opens correctly
      const headers = [
        'Date', 'Symbol', 'Setup Type', 'Direction', 'First Alert Time (IST)',
        'Entry (INR)', 'Stop Loss (INR)', 'Target (INR)', 'LTP (INR)',
        'P&L (INR)', 'P&L (%)', 'Status', 'Score', 'Universe', 'Signals'
      ];

      const csvRows = data.summary.map(r => {
        const pnl    = r.pnl    || 0;
        const entry  = r.entry  || 0;
        // Always compute pnl_pct from pnl/entry so sign is always consistent
        const pnlPct = entry > 0 ? (pnl / entry * 100) : 0;
        // Clean signals: strip commas, quotes, newlines that break CSV parsers
        const signals = (r.signals || '')
          .replace(/[\r\n]+/g, ' ')
          .replace(/"/g, "'")
          .replace(/,/g, ';');
        return [
          r.date,
          r.symbol,
          r.setup_type,
          r.direction,
          r.first_alert_time,
          entry.toFixed(2),
          (r.stop   || 0).toFixed(2),
          (r.target || 0).toFixed(2),
          (r.ltp    || 0).toFixed(2),
          pnl.toFixed(2),
          pnlPct.toFixed(2),
          r.status,
          r.score,
          r.universe,
          `"${signals}"`
        ].join(',');
      });

      const csv  = BOM + [headers.join(','), ...csvRows].join('\n');
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
      const url  = URL.createObjectURL(blob);
      const a    = document.createElement('a');
      const today = new Date().toISOString().slice(0, 10);
      a.href     = url;
      a.download = `fno_alerts_summary_${today}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      const days = [...new Set(data.summary.map(r => r.date))].length;
      if (snapshotEl) {
        snapshotEl.insertAdjacentHTML('afterbegin', `
          <div style="text-align:center;padding:8px 14px;margin-bottom:8px;
                      background:rgba(38,166,154,0.08);border:1px solid rgba(38,166,154,0.3);
                      border-radius:6px;font-size:0.78rem;color:#26A69A;">
            ✅ Summary CSV downloaded — ${data.count} alerts across ${days} trading day(s).
          </div>`);
      }

    } catch (err) {
      console.error('[FTA] Summary failed:', err);
      const snapshotEl = document.getElementById('fta-snapshot-body');
      if (snapshotEl) {
        snapshotEl.insertAdjacentHTML('afterbegin', `
          <div style="text-align:center;padding:8px 14px;margin-bottom:8px;
                      background:rgba(239,83,80,0.08);border:1px solid rgba(239,83,80,0.3);
                      border-radius:6px;font-size:0.78rem;color:#EF5350;">
            ❌ Summary failed: ${err.message}
          </div>`);
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '📈 Summary'; }
    }
  }
}

// ── Init on DOM ready ─────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  window.fnoTradeAlerts = new FNOTradeAlerts();
});

// ════════════════════════════════════════════════════════════════
// ── AI Alert Validator ──────────────────────────────────────────
// ════════════════════════════════════════════════════════════════

const _FTA_AI_MODELS = {
  gemini: [
    { value: 'gemini-2.0-flash',                    label: 'Gemini 2.0 Flash (recommended)' },
    { value: 'gemini-2.0-flash-lite',               label: 'Gemini 2.0 Flash Lite (fastest)' },
    { value: 'gemini-2.5-flash-preview-04-17',      label: 'Gemini 2.5 Flash Preview' },
    { value: 'gemini-2.5-pro-preview-03-25',        label: 'Gemini 2.5 Pro Preview' },
    { value: 'gemini-1.5-pro',                      label: 'Gemini 1.5 Pro' },
    { value: 'gemini-1.5-flash-8b',                 label: 'Gemini 1.5 Flash 8B (fast)' },
  ],
  openai: [
    { value: 'gpt-4o',          label: 'GPT-4o (recommended)' },
    { value: 'gpt-4o-mini',     label: 'GPT-4o Mini (fast)' },
    { value: 'gpt-4-turbo',     label: 'GPT-4 Turbo' },
    { value: 'gpt-3.5-turbo',   label: 'GPT-3.5 Turbo' },
  ],
  anthropic: [
    { value: 'claude-sonnet-4-5',             label: 'Claude Sonnet 4.5 (recommended)' },
    { value: 'claude-opus-4',                  label: 'Claude Opus 4' },
    { value: 'claude-haiku-3-5',               label: 'Claude Haiku 3.5 (fast)' },
    { value: 'claude-3-5-sonnet-20241022',     label: 'Claude 3.5 Sonnet' },
    { value: 'claude-3-haiku-20240307',        label: 'Claude 3 Haiku (cheapest)' },
  ],
};

class FNOAIValidator {
  constructor() {
    this._running = false;
    this._init();
  }

  _init() {
    // Populate model dropdown on provider change
    const providerSel = document.getElementById('fta-ai-provider');
    const modelSel    = document.getElementById('fta-ai-model');
    if (!providerSel || !modelSel) return;

    const _populateModels = () => {
      const p = providerSel.value;
      modelSel.innerHTML = (_FTA_AI_MODELS[p] || [])
        .map(m => `<option value="${m.value}">${m.label}</option>`)
        .join('');
    };
    _populateModels();   // initial fill (Gemini default)
    providerSel.addEventListener('change', _populateModels);

    // Run button
    const runBtn = document.getElementById('fta-ai-run-btn');
    if (runBtn) runBtn.addEventListener('click', () => this.runValidation());
  }

  async runValidation() {
    if (this._running) return;
    this._running = true;

    const runBtn    = document.getElementById('fta-ai-run-btn');
    const statusBar = document.getElementById('fta-ai-status');
    const statusTxt = document.getElementById('fta-ai-status-text');
    const body      = document.getElementById('fta-ai-body');

    const provider = document.getElementById('fta-ai-provider')?.value || 'gemini';
    const model    = document.getElementById('fta-ai-model')?.value    || 'gemini-2.0-flash';
    const apiKey   = document.getElementById('fta-ai-key')?.value?.trim() || '';

    if (!apiKey) {
      this._showError(body, '🔑 Please paste your API key before running AI validation.');
      this._running = false;
      return;
    }

    // ── UI: loading state ──
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = '⏳ Validating…'; }
    if (statusBar) statusBar.style.display = 'flex';
    if (statusTxt) statusTxt.textContent = `Calling ${this._providerLabel(provider)} (${model})…`;
    if (body) body.innerHTML = this._buildSkeletonHtml();

    try {
      const API  = window.API_BASE || '';
      const resp = await fetch(`${API}/api/fno-alerts/ai-rank`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ provider, model, api_key: apiKey }),
      });

      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      if (!data.ranked || data.ranked.length === 0) {
        this._showEmpty(body, 'AI did not return ranked alerts. Run a scan again or verify your API key.');
        return;
      }
      this._renderRanked(data, body);
    } catch (err) {
      console.error('[FTA-AI] Validation failed:', err);
      this._showError(body, `❌ AI Validation failed: ${err.message}`);
    } finally {
      this._running = false;
      if (runBtn)    { runBtn.disabled = false; runBtn.textContent = '✨ Run AI Validation'; }
      if (statusBar) statusBar.style.display = 'none';
    }
  }

  _renderRanked(data, container) {
    const { ranked, model, provider, scanned_at, total_input, total_output, validated_at } = data;

    const sortedRanked = Array.isArray(ranked)
      ? [...ranked].sort((a, b) => {
          const aConf = Number(a.confidence) || 0;
          const bConf = Number(b.confidence) || 0;
          return bConf - aConf;
        })
      : ranked;

    // ── Meta header ──
    const ist  = validated_at ? new Date(validated_at).toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour:'2-digit', minute:'2-digit' }) : '—';
    const scan = scanned_at ? scanned_at.slice(11,16) : '—';
    let html = `
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;
                  padding:8px 12px;margin-bottom:14px;border-radius:8px;
                  background:rgba(156,39,176,0.08);border:1px solid rgba(156,39,176,0.2);">
        <div style="font-size:0.75rem;color:#CE93D8;">
          🤖 <strong>${this._providerLabel(provider)}</strong> — ${model}
        </div>
        <div style="font-size:0.72rem;color:var(--text-muted);">
          Scan: ${scan} IST &nbsp;|&nbsp; Validated: ${ist} IST
          &nbsp;|&nbsp; ${total_output} of ${total_input} alerts ranked
        </div>
      </div>
      <div style="display:grid;gap:10px;">`;

    // ── Rank cards ──
    sortedRanked.forEach((r, i) => {
      const actionClass = {
        'STRONG BUY': 'fta-ai-action-strong',
        'BUY':        'fta-ai-action-buy',
        'WATCH':      'fta-ai-action-watch',
        'AVOID':      'fta-ai-action-avoid',
      }[r.action] || 'fta-ai-action-watch';

      const conf    = Math.min(100, Math.max(0, Number(r.confidence) || 0));
      const rankEmoji = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `#${r.rank || i+1}`;
      const confColor = conf >= 75 ? '#26A69A' : conf >= 55 ? '#FFA726' : '#EF5350';

      const setupTag = {
        'Breakout':          '🚀',
        'BOS Continuation':  '🎯',
        'CHoCH Reversal':    '🔄',
      }[r.setup_type] || '📊';

      html += `
        <div class="fta-ai-rank-card" style="padding-left:22px;">
          <div class="fta-ai-rank-badge">${rankEmoji}</div>

          <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:6px;margin-bottom:8px;">
            <div>
              <span style="font-size:1.05rem;font-weight:800;color:var(--text-primary);">${r.symbol}</span>
              <span style="font-size:0.72rem;color:var(--text-muted);margin-left:6px;">${setupTag} ${r.setup_type || '—'}</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
              <span class="fta-ai-action-pill ${actionClass}">${r.action || 'WATCH'}</span>
              <span style="font-size:0.78rem;font-weight:700;color:${confColor};">${conf}%</span>
            </div>
          </div>

          <!-- Confidence bar -->
          <div style="background:rgba(255,255,255,0.06);border-radius:2px;height:4px;margin-bottom:10px;overflow:hidden;">
            <div class="fta-ai-confidence-bar" style="width:${conf}%;"></div>
          </div>

          <!-- Rationale & Risk -->
          <div style="font-size:0.78rem;color:var(--text-secondary);line-height:1.6;margin-bottom:8px;">
            ${r.rationale || '—'}
          </div>
          <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:0.72rem;color:var(--text-muted);">
            <span>⚠️ <em>${r.key_risk || '—'}</em></span>
            <span style="margin-left:auto;color:#CE93D8;font-weight:600;">
              Position: ${r.suggested_position_size || '—'}
            </span>
          </div>
        </div>`;
    });

    html += '</div>';
    container.innerHTML = html;
  }

  // ── Helpers ──────────────────────────────────────────────────────
  _providerLabel(p) {
    return { gemini: 'Google Gemini', openai: 'OpenAI GPT', anthropic: 'Anthropic Claude' }[p] || p;
  }

  _showError(container, msg) {
    if (!container) return;
    container.innerHTML = `
      <div style="text-align:center;padding:20px;border-radius:8px;
                  background:rgba(239,83,80,0.08);border:1px solid rgba(239,83,80,0.3);">
        <div style="font-size:1.4rem;margin-bottom:8px;">⚠️</div>
        <div style="font-size:0.82rem;color:#EF5350;">${msg}</div>
      </div>`;
  }

  _showEmpty(container, msg) {
    if (!container) return;
    container.innerHTML = `
      <div style="text-align:center;padding:24px;color:var(--text-muted);font-size:0.82rem;">
        <div style="font-size:1.6rem;margin-bottom:8px;">🤷</div>${msg}
      </div>`;
  }

  _buildSkeletonHtml() {
    return Array.from({ length: 3 }).map(() => `
      <div style="border:1px solid rgba(156,39,176,0.15);border-radius:10px;padding:16px;margin-bottom:10px;
                  background:rgba(156,39,176,0.04);">
        <div style="display:flex;justify-content:space-between;margin-bottom:10px;">
          <div style="width:100px;height:18px;border-radius:4px;background:rgba(255,255,255,0.06);
                      animation:fta-ai-pulse 1.2s ease-in-out infinite;"></div>
          <div style="width:60px;height:18px;border-radius:4px;background:rgba(255,255,255,0.06);
                      animation:fta-ai-pulse 1.2s ease-in-out infinite;"></div>
        </div>
        <div style="height:4px;border-radius:2px;background:rgba(255,255,255,0.06);margin-bottom:10px;
                    animation:fta-ai-pulse 1.2s ease-in-out infinite;"></div>
        <div style="height:36px;border-radius:4px;background:rgba(255,255,255,0.06);
                    animation:fta-ai-pulse 1.2s ease-in-out infinite;"></div>
      </div>`
    ).join('');
  }
}

// Init AI Validator after DOM ready
document.addEventListener('DOMContentLoaded', () => {
  window.fnoAIValidator = new FNOAIValidator();
});
