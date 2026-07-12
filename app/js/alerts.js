/**
 * TradeSignal — Alert Engine
 * Manages alert rules, evaluates conditions against live ticks, and triggers notifications
 */
class AlertEngine {
  constructor() {
    this.rules = [];
    this.alerts = [];
    this.maxAlerts = 100;
    this._loadState();
  }

  // ── Rule Management ──
  addRule(rule) {
    const newRule = {
      id: Date.now().toString(36) + Math.random().toString(36).substr(2, 4),
      stock: rule.stock,
      type: rule.type, // price_above, price_below, oi_spike, score_change
      value: parseFloat(rule.value),
      active: true,
      created: new Date().toISOString(),
      triggered: false
    };
    this.rules.push(newRule);
    this._saveState();
    this.renderRules();
    return newRule;
  }

  removeRule(id) {
    this.rules = this.rules.filter(r => r.id !== id);
    this._saveState();
    this.renderRules();
  }

  toggleRule(id) {
    const rule = this.rules.find(r => r.id === id);
    if (rule) {
      rule.active = !rule.active;
      this._saveState();
      this.renderRules();
    }
  }

  // ── Alert Triggers ──
  trigger(alert) {
    const entry = {
      id: Date.now().toString(36),
      stock: alert.stock,
      type: alert.type,
      title: alert.title,
      description: alert.description,
      price: alert.price,
      timestamp: new Date().toISOString(),
      read: false
    };
    this.alerts.unshift(entry);
    if (this.alerts.length > this.maxAlerts) this.alerts = this.alerts.slice(0, this.maxAlerts);
    this._saveState();
    this.renderAlerts();
    this.updateBadge();
    this._pushNotification(entry);
    return entry;
  }

  // ── Evaluate ticks against rules ──
  evaluate(ticks, symbolMap = {}) {
    if (!Array.isArray(ticks)) return;
    
    ticks.forEach(tick => {
      const symbol = symbolMap[tick.instrument_token] || tick.tradingsymbol || '';
      const ltp = tick.last_price || tick.ltp || 0;

      this.rules.filter(r => r.active && !r.triggered && r.stock === symbol).forEach(rule => {
        let triggered = false;
        let title = '';
        let desc = '';

        switch (rule.type) {
          case 'price_above':
            if (ltp >= rule.value) {
              triggered = true;
              title = `${symbol} crossed above ₹${rule.value}`;
              desc = `LTP: ₹${ltp.toFixed(2)}`;
            }
            break;
          case 'price_below':
            if (ltp <= rule.value) {
              triggered = true;
              title = `${symbol} dropped below ₹${rule.value}`;
              desc = `LTP: ₹${ltp.toFixed(2)}`;
            }
            break;
          case 'oi_spike':
            // Support both server-provided `oi_change_percent` and
            // fallback computation from raw `oi` + `oi_day_change`.
            let oiChange = tick.oi_change_percent;
            if (oiChange == null) {
              const oi = tick.oi || 0;
              const oiDayChange = tick.oi_day_change || 0;
              oiChange = (oiDayChange / (oi - oiDayChange) * 100) || 0;
            }
            if (Math.abs(oiChange) >= rule.value) {
              triggered = true;
              title = `OI spike on ${symbol}: ${oiChange > 0 ? '+' : ''}${oiChange.toFixed(1)}%`;
              desc = `Threshold: ${rule.value}%`;
            }
            break;
        }

        if (triggered) {
          rule.triggered = true;
          this.trigger({ stock: symbol, type: rule.type, title, description: desc, price: ltp });
        }
      });
    });
  }

  // ── Browser Notification ──
  _pushNotification(alert) {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(`TradeSignal Alert: ${alert.stock}`, {
        body: alert.title,
        icon: '📈',
        tag: alert.id
      });
    }
  }

  requestPermission() {
    if ('Notification' in window) {
      Notification.requestPermission();
    }
  }

  // ── Clear ──
  clearAlerts() {
    this.alerts = [];
    this._saveState();
    this.renderAlerts();
    this.updateBadge();
  }

  // ── Persistence ──
  _saveState() {
    try {
      localStorage.setItem('ts_alert_rules', JSON.stringify(this.rules));
      localStorage.setItem('ts_alerts', JSON.stringify(this.alerts));
    } catch (e) { /* quota exceeded */ }
  }

  _loadState() {
    try {
      const rules = localStorage.getItem('ts_alert_rules');
      const alerts = localStorage.getItem('ts_alerts');
      if (rules) this.rules = JSON.parse(rules);
      if (alerts) this.alerts = JSON.parse(alerts);
    } catch (e) { /* corrupt data */ }
  }

  // ── Render Rules ──
  renderRules() {
    const container = document.getElementById('alert-rules');
    if (!container) return;

    if (this.rules.length === 0) {
      container.innerHTML = '<div class="empty-state" style="padding:20px;"><p class="text-muted">No alert rules configured. Click "+ Add Rule" to create one.</p></div>';
      return;
    }

    container.innerHTML = this.rules.map(r => {
      const typeLabels = {
        price_above: '📈 Price Above',
        price_below: '📉 Price Below',
        oi_spike: '🔄 OI Spike',
        score_change: '🧠 Score Change',
        reco_signal_flip: '🔀 Signal Flip',
        reco_sl_breach: '🛑 SL Breach',
        reco_target_hit: '🎯 Target Hit',
        reco_score_drop: '📉 Score Drop',
        reco_invalidated: '⚠️ Reco Dropped'
      };
      const statusClass = r.triggered ? 'tag-neutral' : r.active ? 'tag-bullish' : 'tag-bearish';
      const statusLabel = r.triggered ? 'Triggered' : r.active ? 'Active' : 'Paused';

      return `<div class="alert-item">
        <div class="alert-icon ${r.type === 'price_above' ? 'target' : r.type === 'price_below' ? 'stoploss' : 'oi'}">
          ${typeLabels[r.type]?.charAt(0) || '⚡'}
        </div>
        <div class="alert-body">
          <div class="alert-title">${r.stock} — ${typeLabels[r.type] || r.type}</div>
          <div class="alert-desc">Value: ₹${r.value.toLocaleString()} <span class="tag ${statusClass}" style="margin-left:8px;">${statusLabel}</span></div>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn btn-sm btn-secondary" onclick="alertEngine.toggleRule('${r.id}')">${r.active ? '⏸' : '▶'}</button>
          <button class="btn btn-sm btn-secondary" onclick="alertEngine.removeRule('${r.id}')" style="color:var(--red);">✕</button>
        </div>
      </div>`;
    }).join('');
  }

  // ── Render Alert Log ──
  renderAlerts() {
    const container = document.getElementById('alert-log');
    if (!container) return;

    if (this.alerts.length === 0) {
      container.innerHTML = '<div class="empty-state" style="padding:30px;"><div class="empty-icon">🔕</div><h4>No Alerts</h4><p>Alerts will appear here when triggered</p></div>';
      return;
    }

    container.innerHTML = this.alerts.map(a => {
      const timeAgo = this._timeAgo(a.timestamp);
      const icons = {
        price_above: '🎯', price_below: '🛑', oi_spike: '🔄', signal_flip: '🔀', score: '🧠',
        reco_signal_flip: '🔀', reco_sl_breach: '🛑', reco_target_hit: '🎯',
        reco_score_drop: '📉', reco_invalidated: '⚠️'
      };
      const icon = icons[a.type] || '⚡';

      return `<div class="alert-item${a.read ? '' : ' flash-up'}">
        <div class="alert-icon">${icon}</div>
        <div class="alert-body">
          <div class="alert-title">${a.title}</div>
          <div class="alert-desc">${a.description}</div>
        </div>
        <span class="alert-time">${timeAgo}</span>
      </div>`;
    }).join('');
  }

  // ── Badge ──
  updateBadge() {
    const unread = this.alerts.filter(a => !a.read).length;
    const badge = document.getElementById('alerts-badge');
    const navBadge = document.getElementById('alert-count');
    if (badge) {
      badge.textContent = unread;
      badge.style.display = unread > 0 ? 'flex' : 'none';
    }
    if (navBadge) navBadge.textContent = unread;
  }

  _timeAgo(timestamp) {
    const diff = Date.now() - new Date(timestamp).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'Just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }
}

window.alertEngine = new AlertEngine();
