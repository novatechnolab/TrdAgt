/**
 * TradeSignal — Recommendation P&L Tracker
 * Automatically captures signals from Recommendations & FNO Session features,
 * tracks hypothetical P&L if action taken on recommendations.
 */
class RecoTracker {
  constructor() {
    this._bound = false;
    this._recos = [];
    this._stats = null;
    this._filter = { source: '', status: '' };
    this._date = new Date().toISOString().slice(0, 10);
    // Configurable: which FNO session phases to capture
    // Options: 'all' | 'premarket' | 'opening' | 'live'
    this.fnoSessionCapture = 'all';
  }

  init() {
    if (this._bound) return;
    this._bound = true;
    this._bindEvents();
    this.load();
  }

  _bindEvents() {
    // Date picker
    const datePicker = document.getElementById('reco-tracker-date');
    if (datePicker) {
      datePicker.value = this._date;
      datePicker.addEventListener('change', (e) => {
        this._date = e.target.value;
        this.load();
      });
    }

    // Source filter tabs
    document.querySelectorAll('#reco-tracker-source-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#reco-tracker-source-tabs .tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._filter.source = btn.dataset.source || '';
        this.load();
      });
    });

    // Status filter tabs
    document.querySelectorAll('#reco-tracker-status-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#reco-tracker-status-tabs .tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._filter.status = btn.dataset.status || '';
        this.load();
      });
    });

    // Auto-close button
    document.getElementById('btn-reco-auto-close')?.addEventListener('click', () => this.autoClose());

    // Export CSV
    document.getElementById('btn-reco-export-csv')?.addEventListener('click', () => this.exportCSV());

    // FNO session capture config
    const fnoCaptureSel = document.getElementById('reco-fno-capture-mode');
    if (fnoCaptureSel) {
      fnoCaptureSel.value = this.fnoSessionCapture;
      fnoCaptureSel.addEventListener('change', (e) => {
        this.fnoSessionCapture = e.target.value;
        localStorage.setItem('reco_fno_capture', e.target.value);
      });
    }
    // Restore from localStorage
    const saved = localStorage.getItem('reco_fno_capture');
    if (saved) this.fnoSessionCapture = saved;
  }

  async load() {
    await Promise.all([this._loadRecos(), this._loadStats()]);
    this._renderTable();
    this._renderStats();
    this._renderSummaryCards();
  }

  async _loadRecos() {
    try {
      let url = `/api/reco-tracker?date=${this._date}`;
      if (this._filter.source) url += `&source=${this._filter.source}`;
      if (this._filter.status) url += `&status=${this._filter.status}`;
      const resp = await app.apiFetch(url);
      if (resp.ok) {
        const data = await resp.json();
        this._recos = data.recommendations || [];
      }
    } catch (e) {
      console.error('Reco tracker load error:', e);
    }
  }

  async _loadStats() {
    try {
      const resp = await app.apiFetch(`/api/reco-tracker/stats?date=${this._date}`);
      if (resp.ok) this._stats = await resp.json();
    } catch (e) {
      console.error('Reco stats error:', e);
    }
  }

  /**
   * Auto-capture recommendations from scoring engine output.
   * Called after generateRecommendations() and FNO session analysis.
   * @param {Array} recos - array of recommendation objects
   * @param {string} source - 'equity_picks' | 'options_picks' | 'fno_session'
   * @param {string} sessionPhase - FNO session phase (optional)
   */
  async autoCapture(recos, source = 'equity_picks', sessionPhase = '') {
    if (!recos || recos.length === 0) return;

    // For FNO session: check if this phase should be captured
    if (source === 'fno_session' && this.fnoSessionCapture !== 'all') {
      if (sessionPhase && sessionPhase !== this.fnoSessionCapture) {
        console.log(`[RecoTracker] Skipping FNO phase '${sessionPhase}' (configured: '${this.fnoSessionCapture}')`);
        return;
      }
    }

    const payload = recos.map(r => {
      const isOptions = source === 'options_picks';
      const isFNO = source === 'fno_session';
      const score = isFNO ? r.score : (isOptions ? (r.optScore || r.score) : r.score);
      const signal = isFNO ? (r.direction || r.signal) : (isOptions ? (r.optSignal || r.signal) : r.signal);

      // Determine direction
      const isCall = signal === 'BULLISH' || signal === 'CALL';
      const isBearish = signal === 'BEARISH' || signal === 'PUT';
      const direction = isCall ? 'BUY' : isBearish ? 'SELL' : 'WATCH';

      // Confidence
      let confidence = r.confidence || '';
      if (!confidence) {
        if (score >= 70) confidence = 'HIGH';
        else if (score >= 55) confidence = 'MEDIUM';
        else confidence = 'MODERATE';
      }

      // Risk data
      const risk = r.risk || {};
      const atr = r.atr || (r.ltp * 0.02);
      const entryPrice = r.ltp || risk.entry || 0;
      const target1 = risk.target1 || (isCall ? entryPrice + 2 * atr : isBearish ? entryPrice - 2 * atr : entryPrice);
      const target2 = risk.target2 || (isCall ? entryPrice + 3 * atr : isBearish ? entryPrice - 3 * atr : entryPrice);
      const sl = risk.stopLoss || (isCall ? entryPrice - 1.5 * atr : isBearish ? entryPrice + 1.5 * atr : entryPrice);
      const rr = risk.riskReward || 0;

      // Strategy for FNO
      const strategy = isFNO ? (r.strategy?.trade || r.recommendation || '') :
                       isOptions ? (r.optStrategy || '') : '';

      // Rationale
      const rationale = `Score: ${score}/100 | RSI: ${r.rsi?.toFixed?.(0) || '?'} | Vol: ${r.volRatio?.toFixed?.(1) || '?'}x`;

      return {
        symbol: r.symbol,
        source,
        session_phase: sessionPhase || '',
        signal,
        direction,
        score: Math.round(score || 0),
        confidence,
        entry_price: entryPrice,
        target1: +target1?.toFixed?.(2) || target1,
        target2: +target2?.toFixed?.(2) || target2,
        stop_loss: +sl?.toFixed?.(2) || sl,
        risk_reward: +rr?.toFixed?.(2) || rr,
        strategy,
        rationale
      };
    }).filter(r => r.entry_price > 0 && r.direction !== 'WATCH');

    if (payload.length === 0) return;

    // ── Validate against previously saved recommendations → fire alerts ──
    await this._validateRecommendations(payload, source, sessionPhase);

    try {
      const resp = await app.apiFetch('/api/reco-tracker', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ recommendations: payload })
      });
      if (resp.ok) {
        const result = await resp.json();
        console.log(`[RecoTracker] Captured ${result.upserted} recommendations (${source})`);
        // Update badge
        this._updateBadge(result.upserted);
      }
    } catch (e) {
      console.warn('[RecoTracker] Auto-capture failed:', e.message);
    }
  }

  /**
   * Compare new recommendations against existing saved ones.
   * Fires alerts for: signal flips, SL breaches, target hits, score drops, dropped recos.
   */
  async _validateRecommendations(newRecos, source, sessionPhase) {
    if (typeof alertEngine === 'undefined') return;

    try {
      // Fetch today's saved recommendations for this source
      const today = new Date().toISOString().slice(0, 10);
      let url = `/api/reco-tracker?date=${today}&source=${source}&status=OPEN`;
      const resp = await app.apiFetch(url);
      if (!resp.ok) return;
      const data = await resp.json();
      const savedRecos = data.recommendations || [];
      if (savedRecos.length === 0) return; // first time — nothing to compare

      const newMap = {};
      newRecos.forEach(r => { newMap[r.symbol] = r; });
      const savedMap = {};
      savedRecos.forEach(r => { savedMap[r.symbol] = r; });

      let alertCount = 0;

      // ── 1. Signal Flip: saved BULLISH → new BEARISH (or vice versa) ──
      for (const saved of savedRecos) {
        const fresh = newMap[saved.symbol];
        if (!fresh) continue;

        const savedBull = saved.direction === 'BUY' || saved.signal === 'BULLISH' || saved.signal === 'CALL';
        const freshBull = fresh.direction === 'BUY' || fresh.signal === 'BULLISH' || fresh.signal === 'CALL';

        if (savedBull !== freshBull) {
          alertEngine.trigger({
            stock: saved.symbol,
            type: 'reco_signal_flip',
            title: `🔀 Signal Flip: ${saved.symbol}`,
            description: `${saved.signal} → ${fresh.signal} (Score: ${saved.score} → ${fresh.score}) [${source}]`,
            price: fresh.entry_price
          });
          alertCount++;
        }
      }

      // ── 2. Score Drop: significant confidence downgrade ──
      for (const saved of savedRecos) {
        const fresh = newMap[saved.symbol];
        if (!fresh) continue;

        const scoreDrop = saved.score - fresh.score;
        // Alert if score dropped by ≥15 points, or HIGH→MODERATE/LOW
        const confDrop = (saved.confidence === 'HIGH' && (fresh.confidence === 'MODERATE' || fresh.confidence === 'LOW'));
        if (scoreDrop >= 15 || confDrop) {
          alertEngine.trigger({
            stock: saved.symbol,
            type: 'reco_score_drop',
            title: `📉 Score Dropped: ${saved.symbol}`,
            description: `Score: ${saved.score} → ${fresh.score} | Confidence: ${saved.confidence} → ${fresh.confidence} [${source}]`,
            price: fresh.entry_price
          });
          alertCount++;
        }
      }

      // ── 3. SL Breach: current LTP ≤ SL (for BUY) or ≥ SL (for SELL) ──
      for (const saved of savedRecos) {
        const fresh = newMap[saved.symbol];
        const currentLTP = fresh ? fresh.entry_price : 0;
        if (!currentLTP || !saved.stop_loss) continue;

        const isBuy = saved.direction === 'BUY' || saved.signal === 'BULLISH' || saved.signal === 'CALL';
        const slBreached = isBuy ? (currentLTP <= saved.stop_loss) : (currentLTP >= saved.stop_loss);

        if (slBreached) {
          alertEngine.trigger({
            stock: saved.symbol,
            type: 'reco_sl_breach',
            title: `🛑 SL Breached: ${saved.symbol}`,
            description: `LTP ₹${currentLTP.toFixed(2)} hit SL ₹${saved.stop_loss.toFixed(2)} (Entry: ₹${saved.entry_price.toFixed(2)}) [${source}]`,
            price: currentLTP
          });
          alertCount++;
        }
      }

      // ── 4. Target Hit: current LTP ≥ Target1 (for BUY) or ≤ Target1 (for SELL) ──
      for (const saved of savedRecos) {
        const fresh = newMap[saved.symbol];
        const currentLTP = fresh ? fresh.entry_price : 0;
        if (!currentLTP || !saved.target1) continue;

        const isBuy = saved.direction === 'BUY' || saved.signal === 'BULLISH' || saved.signal === 'CALL';
        const t1Hit = isBuy ? (currentLTP >= saved.target1) : (currentLTP <= saved.target1);
        const t2Hit = saved.target2 && (isBuy ? (currentLTP >= saved.target2) : (currentLTP <= saved.target2));

        if (t2Hit) {
          alertEngine.trigger({
            stock: saved.symbol,
            type: 'reco_target_hit',
            title: `🎯 Target 2 Hit: ${saved.symbol}`,
            description: `LTP ₹${currentLTP.toFixed(2)} reached T2 ₹${saved.target2.toFixed(2)} 🎉 [${source}]`,
            price: currentLTP
          });
          alertCount++;
        } else if (t1Hit) {
          alertEngine.trigger({
            stock: saved.symbol,
            type: 'reco_target_hit',
            title: `🎯 Target 1 Hit: ${saved.symbol}`,
            description: `LTP ₹${currentLTP.toFixed(2)} reached T1 ₹${saved.target1.toFixed(2)} [${source}]`,
            price: currentLTP
          });
          alertCount++;
        }
      }

      // ── 5. Reco Dropped: stock was in saved list but not in new list ──
      for (const saved of savedRecos) {
        if (!newMap[saved.symbol]) {
          alertEngine.trigger({
            stock: saved.symbol,
            type: 'reco_invalidated',
            title: `⚠️ Reco Dropped: ${saved.symbol}`,
            description: `${saved.signal} reco (Score: ${saved.score}) no longer in ${source} list. Conditions may have changed.`,
            price: saved.entry_price
          });
          alertCount++;
        }
      }

      if (alertCount > 0) {
        console.log(`[RecoTracker] Generated ${alertCount} recommendation alerts`);
      }

    } catch (e) {
      console.warn('[RecoTracker] Validation check failed:', e.message);
    }
  }

  async autoClose() {
    const btn = document.getElementById('btn-reco-auto-close');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Fetching Prices...'; }

    try {
      const resp = await app.apiFetch('/api/reco-tracker/auto-close', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: this._date })
      });
      if (resp.ok) {
        const result = await resp.json();
        this._toast(`Closed ${result.closed} recommendations with live prices`, 'success');
        await this.load();
      } else {
        const err = await resp.json();
        this._toast(err.error || 'Auto-close failed', 'error');
      }
    } catch (e) {
      this._toast(e.message, 'error');
    }

    if (btn) { btn.disabled = false; btn.textContent = '📡 Auto-fetch Close Prices'; }
  }

  async updatePnL(id) {
    const price = prompt('Enter exit price:');
    if (!price || isNaN(parseFloat(price))) return;

    try {
      const resp = await app.apiFetch(`/api/reco-tracker/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exit_price: parseFloat(price) })
      });
      if (resp.ok) {
        const result = await resp.json();
        this._toast(`P&L: ₹${result.pnl} (${result.pnl_pct}%) — ${result.outcome}`, 'success');
        await this.load();
      }
    } catch (e) {
      this._toast(e.message, 'error');
    }
  }

  async deleteReco(id) {
    if (!confirm('Delete this recommendation record?')) return;
    try {
      const resp = await app.apiFetch(`/api/reco-tracker/${id}`, { method: 'DELETE' });
      if (resp.ok) {
        this._toast('Deleted', 'success');
        await this.load();
      }
    } catch (e) {
      this._toast(e.message, 'error');
    }
  }

  _renderSummaryCards() {
    const container = document.getElementById('reco-tracker-summary');
    if (!container) return;

    const total = this._recos.length;
    const open = this._recos.filter(r => r.status === 'OPEN').length;
    const closed = total - open;
    const wins = this._recos.filter(r => (r.pnl || 0) > 0 && r.status !== 'OPEN').length;
    const netPnl = this._recos.reduce((sum, r) => sum + (r.pnl || 0), 0);

    const pnlColor = netPnl >= 0 ? '#26A69A' : '#EF5350';
    const wrColor = closed > 0 ? (wins / closed * 100 >= 50 ? '#26A69A' : '#EF5350') : 'var(--text-muted)';

    container.innerHTML = `
      <div class="stat-card" style="padding:12px;text-align:center;">
        <span class="stat-label" style="font-size:0.65rem;">Total Signals</span>
        <span class="stat-value" style="font-size:1.2rem;">${total}</span>
      </div>
      <div class="stat-card" style="padding:12px;text-align:center;">
        <span class="stat-label" style="font-size:0.65rem;">Open</span>
        <span class="stat-value" style="font-size:1.2rem;color:#2196F3;">${open}</span>
      </div>
      <div class="stat-card" style="padding:12px;text-align:center;">
        <span class="stat-label" style="font-size:0.65rem;">Closed</span>
        <span class="stat-value" style="font-size:1.2rem;">${closed}</span>
      </div>
      <div class="stat-card" style="padding:12px;text-align:center;">
        <span class="stat-label" style="font-size:0.65rem;">Win Rate</span>
        <span class="stat-value" style="font-size:1.2rem;color:${wrColor};">${closed > 0 ? (wins / closed * 100).toFixed(0) + '%' : '—'}</span>
      </div>
      <div class="stat-card" style="padding:12px;text-align:center;">
        <span class="stat-label" style="font-size:0.65rem;">Net P&L</span>
        <span class="stat-value" style="font-size:1.2rem;color:${pnlColor};">₹${netPnl.toFixed(0)}</span>
      </div>
    `;
  }

  _renderTable() {
    const container = document.getElementById('reco-tracker-table-body');
    if (!container) return;

    if (this._recos.length === 0) {
      container.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:40px;color:var(--text-muted);">
        <div style="font-size:2rem;margin-bottom:8px;">📊</div>
        <div>No recommendations for ${this._date}.</div>
        <div style="font-size:0.75rem;margin-top:8px;">Run "Generate Report" on the Recommendations page or analyze stocks in FNO Sessions to auto-capture signals.</div>
      </td></tr>`;
      return;
    }

    container.innerHTML = this._recos.map(r => {
      const isOpen = r.status === 'OPEN';
      const pnlColor = !r.pnl ? 'var(--text-muted)' : r.pnl > 0 ? '#26A69A' : '#EF5350';
      const pnlText = r.pnl != null ? `₹${r.pnl.toFixed(2)}` : '—';
      const pnlPctText = r.pnl_pct != null ? `${r.pnl_pct > 0 ? '+' : ''}${r.pnl_pct.toFixed(2)}%` : '—';

      // Signal styling
      const isCall = r.signal === 'BULLISH' || r.signal === 'CALL';
      const isBearish = r.signal === 'BEARISH' || r.signal === 'PUT';
      const signalClass = isCall ? 'tag-bullish' : isBearish ? 'tag-bearish' : 'tag-neutral';

      // Source badge
      const sourceLabels = {
        'equity_picks': '📈 Equity',
        'options_picks': '⛓️ Options',
        'fno_session': '⏰ FNO'
      };
      const sourceLabel = sourceLabels[r.source] || r.source;
      const phaseLabel = r.session_phase ? ` · ${r.session_phase}` : '';

      // Outcome badge
      const outcomeBadges = {
        'WIN': '<span style="padding:2px 6px;border-radius:4px;background:rgba(38,166,154,0.15);color:#26A69A;font-size:0.68rem;font-weight:700;">✅ WIN</span>',
        'TARGET2': '<span style="padding:2px 6px;border-radius:4px;background:rgba(38,166,154,0.2);color:#26A69A;font-size:0.68rem;font-weight:700;">🎯 T2 HIT</span>',
        'SL_HIT': '<span style="padding:2px 6px;border-radius:4px;background:rgba(239,83,80,0.15);color:#EF5350;font-size:0.68rem;font-weight:700;">🛑 SL HIT</span>',
        'LOSS': '<span style="padding:2px 6px;border-radius:4px;background:rgba(239,83,80,0.1);color:#EF5350;font-size:0.68rem;font-weight:700;">❌ LOSS</span>',
        'NEUTRAL': '<span style="padding:2px 6px;border-radius:4px;background:rgba(255,167,38,0.1);color:#FFA726;font-size:0.68rem;font-weight:700;">↔ PARTIAL</span>'
      };
      const outcomeBadge = r.outcome ? (outcomeBadges[r.outcome] || r.outcome) : '';

      // Score styling
      const scoreClass = r.score >= 70 ? 'score-high' : r.score >= 55 ? 'score-medium' : 'score-low';

      // Status badge
      const statusBadge = isOpen
        ? '<span style="padding:2px 6px;border-radius:4px;background:rgba(33,150,243,0.15);color:#2196F3;font-size:0.68rem;font-weight:700;">OPEN</span>'
        : '<span style="padding:2px 6px;border-radius:4px;background:rgba(38,166,154,0.1);color:#26A69A;font-size:0.68rem;font-weight:700;">CLOSED</span>';

      const fmt = (n) => n != null ? `₹${(+n).toFixed(2)}` : '—';

      return `<tr style="font-size:0.78rem;">
        <td style="font-weight:700;color:var(--primary);cursor:pointer;" onclick="app.scoreStock('${r.symbol}')">${r.symbol}</td>
        <td><span style="font-size:0.68rem;color:var(--text-secondary);">${sourceLabel}${phaseLabel}</span></td>
        <td><span class="tag ${signalClass}" style="font-size:0.7rem;">${r.signal}</span></td>
        <td><span class="score-badge ${scoreClass}" style="width:28px;height:28px;font-size:0.68rem;">${r.score}</span></td>
        <td>${fmt(r.entry_price)}</td>
        <td style="color:#26A69A;">${fmt(r.target1)}</td>
        <td style="color:#EF5350;">${fmt(r.stop_loss)}</td>
        <td>${r.exit_price ? fmt(r.exit_price) : '—'}</td>
        <td style="color:${pnlColor};font-weight:600;">${pnlText}<br><span style="font-size:0.65rem;">${pnlPctText}</span></td>
        <td>${statusBadge} ${outcomeBadge}</td>
        <td style="font-size:0.68rem;color:var(--text-muted);">${r.reco_time || ''}</td>
        <td>
          <div style="display:flex;gap:3px;">
            ${isOpen ? `<button class="btn btn-sm btn-primary" onclick="recoTracker.updatePnL(${r.id})" style="font-size:0.65rem;padding:3px 6px;" title="Set Exit Price">💰</button>` : ''}
            <button class="btn btn-sm btn-secondary" onclick="recoTracker.deleteReco(${r.id})" style="font-size:0.65rem;padding:3px 6px;" title="Delete">🗑</button>
          </div>
        </td>
      </tr>`;
    }).join('');
  }

  _renderStats() {
    const container = document.getElementById('reco-tracker-stats');
    if (!container) return;

    const s = this._stats;
    if (!s || !s.total) {
      container.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:0.8rem;">Close recommendations to see accuracy analytics</div>';
      return;
    }

    // Source breakdown
    const sourceRows = Object.entries(s.bySource || {}).map(([src, d]) => {
      const labels = { 'equity_picks': '📈 Equity', 'options_picks': '⛓️ Options', 'fno_session': '⏰ FNO' };
      return `<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:0.78rem;">
        <span>${labels[src] || src}</span>
        <span style="color:${d.win_rate >= 50 ? '#26A69A' : '#EF5350'};font-weight:600;">${d.win_rate}% WR · ${d.count} signals · ₹${d.pnl}</span>
      </div>`;
    }).join('');

    // Confidence breakdown
    const confRows = Object.entries(s.byConfidence || {}).map(([conf, d]) => {
      return `<div style="display:flex;justify-content:space-between;padding:4px 0;font-size:0.78rem;">
        <span>${conf}</span>
        <span style="color:${d.win_rate >= 50 ? '#26A69A' : '#EF5350'};font-weight:600;">${d.win_rate}% WR · ${d.count} signals</span>
      </div>`;
    }).join('');

    const pnlColor = s.totalPnl >= 0 ? '#26A69A' : '#EF5350';

    container.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:16px;">
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Win Rate</span>
          <span class="stat-value" style="font-size:1rem;color:${s.winRate >= 50 ? '#26A69A' : '#EF5350'};">${s.winRate}%</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Avg P&L %</span>
          <span class="stat-value" style="font-size:1rem;color:${s.avgPnlPct >= 0 ? '#26A69A' : '#EF5350'};">${s.avgPnlPct > 0 ? '+' : ''}${s.avgPnlPct}%</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Target 1 Hit</span>
          <span class="stat-value" style="font-size:1rem;">${s.target1HitRate}%</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Target 2 Hit</span>
          <span class="stat-value" style="font-size:1rem;">${s.target2HitRate}%</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">SL Hit Rate</span>
          <span class="stat-value" style="font-size:1rem;color:#EF5350;">${s.slHitRate}%</span>
        </div>
        <div class="stat-card" style="padding:10px;text-align:center;">
          <span class="stat-label" style="font-size:0.6rem;">Total P&L</span>
          <span class="stat-value" style="font-size:1rem;color:${pnlColor};">₹${s.totalPnl}</span>
        </div>
      </div>

      ${s.best ? `<div style="display:flex;gap:16px;font-size:0.75rem;margin-bottom:8px;">
        <span style="color:#26A69A;">🏆 Best: ${s.best.symbol} (${s.best.pnl_pct > 0 ? '+' : ''}${s.best.pnl_pct}%)</span>
        <span style="color:#EF5350;">💀 Worst: ${s.worst.symbol} (${s.worst.pnl_pct > 0 ? '+' : ''}${s.worst.pnl_pct}%)</span>
      </div>` : ''}

      ${sourceRows ? `<div style="margin-top:12px;">
        <strong style="font-size:0.78rem;display:block;margin-bottom:6px;">📊 By Source</strong>
        ${sourceRows}
      </div>` : ''}

      ${confRows ? `<div style="margin-top:12px;">
        <strong style="font-size:0.78rem;display:block;margin-bottom:6px;">🎯 By Confidence</strong>
        ${confRows}
      </div>` : ''}
    `;
  }

  exportCSV() {
    if (this._recos.length === 0) {
      this._toast('No data to export', 'error');
      return;
    }

    const headers = ['Symbol', 'Source', 'Phase', 'Signal', 'Direction', 'Score', 'Confidence',
                     'Entry', 'Target1', 'Target2', 'SL', 'R:R', 'Exit', 'P&L', 'P&L%', 'Status', 'Outcome', 'Date', 'Time'];
    const rows = this._recos.map(r => [
      r.symbol, r.source, r.session_phase || '', r.signal, r.direction, r.score, r.confidence,
      r.entry_price, r.target1, r.target2, r.stop_loss, r.risk_reward,
      r.exit_price || '', r.pnl || '', r.pnl_pct || '', r.status, r.outcome || '',
      r.reco_date, r.reco_time
    ]);

    const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `reco_tracker_${this._date}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    this._toast('CSV exported ✓', 'success');
  }

  _updateBadge(count) {
    const badge = document.getElementById('reco-tracker-badge');
    if (badge) {
      const current = parseInt(badge.textContent) || 0;
      badge.textContent = current + count;
      badge.style.display = (current + count) > 0 ? 'inline-flex' : 'none';
    }
  }

  _toast(msg, type = 'info') {
    if (typeof app !== 'undefined' && app.showToast) app.showToast(msg, type);
    else console.log(`[RecoTracker] ${type}: ${msg}`);
  }
}

window.recoTracker = new RecoTracker();
