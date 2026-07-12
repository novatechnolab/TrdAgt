/**
 * TradeSignal — News & Sentiment Feed Module
 *
 * Fetches aggregated news from RSS feeds via the backend,
 * displays headlines with sentiment badges, and provides
 * a stock-specific sentiment gauge.
 */
class NewsFeed {
  constructor() {
    this._headlines = [];
    this._loading = false;
    this._currentCategory = 'All';
    this._filter = '';
  }

  async load(symbol, refresh = false) {
    if (this._loading) return;
    this._loading = true;
    this._filter = (symbol || '').toUpperCase();

    const container = document.getElementById('news-content');
    if (container) {
      container.innerHTML = `<div style="text-align:center;padding:40px;color:var(--text-muted);">${refresh ? 'Fetching very latest feeds from RSS sources...' : 'Fetching market news & institutional feeds...'}</div>`;
    }

    try {
      let url = this._filter ? `/api/news?symbol=${encodeURIComponent(this._filter)}` : '/api/news';
      if (refresh) {
        url += (url.includes('?') ? '&' : '?') + 'refresh=true';
      }
      const res = await app.apiFetch(url);

      if (!res.ok) {
        this._showError(container, 'Failed to load news feed.');
        return;
      }

      const data = await res.json();
      if (data.error) {
        this._showError(container, data.error);
        return;
      }

      this._headlines = data.headlines || [];
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
          <div style="font-size:3rem;margin-bottom:12px;">📰</div>
          <h3 style="font-family:var(--font-display);margin-bottom:8px;">News Unavailable</h3>
          <p style="font-size:0.85rem;">${msg}</p>
          <button class="btn btn-primary" onclick="newsFeed.load('', true)" style="margin-top:16px;">Retry</button>
        </div>`;
    }
  }

  render() {
    const container = document.getElementById('news-content');
    if (!container) return;

    if (this._headlines.length === 0) {
      container.innerHTML = `
        <div style="text-align:center;padding:60px 20px;color:var(--text-muted);">
          <div style="font-size:3rem;margin-bottom:12px;">📰</div>
          <h3 style="font-family:var(--font-display);margin-bottom:8px;">No Headlines Found</h3>
          <p style="font-size:0.85rem;">${this._filter ? `No recent news for ${this._filter}.` : 'No news from RSS feeds.'}</p>
        </div>`;
      return;
    }

    // Filter headlines dynamically by the active category tab
    const filteredHeadlines = this._currentCategory === 'All' 
      ? this._headlines 
      : this._headlines.filter(h => h.category === this._currentCategory);

    // Compute sentiment statistics
    const total = this._headlines.length;
    const bullish = this._headlines.filter(h => h.sentiment === 'bullish').length;
    const bearish = this._headlines.filter(h => h.sentiment === 'bearish').length;
    const neutral = total - bullish - bearish;
    const bullPct = total > 0 ? Math.round((bullish / total) * 100) : 0;
    const bearPct = total > 0 ? Math.round((bearish / total) * 100) : 0;
    const netScore = bullish - bearish;

    const sentimentLabel = netScore > 3 ? 'Strong Bullish' : netScore > 0 ? 'Bullish' : netScore < -3 ? 'Strong Bearish' : netScore < 0 ? 'Bearish' : 'Neutral';
    const sentimentColor = netScore > 0 ? '#26A69A' : netScore < 0 ? '#EF5350' : '#78909C';

    // 1. Premium Sentiment Meter UI Block
    let html = `
      <div style="background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:20px;margin-bottom:24px;display:flex;gap:24px;align-items:center;flex-wrap:wrap;box-shadow:var(--shadow-sm);">
        <div style="flex:1;min-width:200px;">
          <h4 style="margin:0 0 8px 0;font-size:0.75rem;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary);">Market Buzz Sentiment</h4>
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px;">
            <span style="font-size:1.5rem;font-weight:700;color:${sentimentColor}">${sentimentLabel}</span>
            <span style="font-size:0.7rem;padding:3px 8px;background:rgba(120,144,156,0.1);border-radius:20px;color:var(--text-muted);font-weight:600;">Score: ${netScore > 0 ? '+' : ''}${netScore}</span>
          </div>
          <div style="display:flex;height:8px;border-radius:4px;overflow:hidden;background:#37474F;">
            <div style="width:${bullPct}%;background:#26A69A;" title="Bullish"></div>
            <div style="width:${100 - bullPct - bearPct}%;background:#78909C;" title="Neutral"></div>
            <div style="width:${bearPct}%;background:#EF5350;" title="Bearish"></div>
          </div>
          <div style="display:flex;justify-content:space-between;font-size:0.7rem;color:var(--text-muted);margin-top:6px;">
            <span style="color:#26A69A;font-weight:600;">🟢 Bullish: ${bullPct}%</span>
            <span style="color:#78909C;font-weight:600;">⚪ Neutral: ${100 - bullPct - bearPct}%</span>
            <span style="color:#EF5350;font-weight:600;">🔴 Bearish: ${bearPct}%</span>
          </div>
        </div>
      </div>`;

    // 2. Interactive Premium Category Tab Bar
    const categories = ['All', 'Corporate Earnings', 'Regulatory Updates', 'Corporate Actions', 'Business Catalysts', 'Macro/Sectoral'];
    html += `
      <div style="display:flex;gap:8px;margin-bottom:20px;border-bottom:1px solid var(--border);padding-bottom:12px;overflow-x:auto;scrollbar-width:none;">
        ${categories.map(cat => {
          const isActive = this._currentCategory === cat;
          const label = cat === 'All' ? '📁 All Feeds' 
            : cat === 'Corporate Earnings' ? '📈 Earnings'
            : cat === 'Regulatory Updates' ? '⚖️ Regulatory'
            : cat === 'Corporate Actions' ? '💼 Corp Actions'
            : cat === 'Business Catalysts' ? '⚡ Catalysts'
            : '🌍 Macro';
          return `
            <button onclick="newsFeed.setCategory('${cat}')" 
              style="padding:6px 14px;border:none;border-radius:20px;background:${isActive ? 'rgba(30,136,229,0.15)' : 'none'};color:${isActive ? '#1E88E5' : 'var(--text-secondary)'};font-size:0.78rem;font-weight:600;cursor:pointer;white-space:nowrap;transition:all 0.2s;border:1px solid ${isActive ? 'rgba(30,136,229,0.3)' : 'transparent'};">
              ${label}
            </button>`;
        }).join('')}
      </div>`;

    // 3. Render Headlines
    if (filteredHeadlines.length === 0) {
      html += `<div style="text-align:center;padding:40px;color:var(--text-muted);font-size:0.82rem;">No recent news found in the selected category.</div>`;
      container.innerHTML = html;
      return;
    }

    html += '<div style="display:flex;flex-direction:column;gap:12px;">';

    filteredHeadlines.forEach(h => {
      const badgeColor = h.sentiment === 'bullish' ? '#26A69A' : h.sentiment === 'bearish' ? '#EF5350' : '#78909C';
      const badgeBg = h.sentiment === 'bullish' ? 'rgba(38,166,154,0.08)' : h.sentiment === 'bearish' ? 'rgba(239,83,80,0.08)' : 'rgba(120,144,156,0.08)';

      const timeStr = h.time ? this._formatTime(h.time) : '';

      const severityStyle = h.impact_rating === 'High'
        ? 'background:rgba(239,83,80,0.15);color:#EF5350;border:1px solid rgba(239,83,80,0.3);'
        : 'background:rgba(30,136,229,0.1);color:#1E88E5;border:1px solid rgba(30,136,229,0.2);';

      const stockPills = (h.impacted_stocks || []).map(sym => `
        <span onclick="event.preventDefault(); newsFeed.load('${sym}');" 
          style="padding:2px 8px;border-radius:4px;background:rgba(30,136,229,0.15);color:#1E88E5;font-weight:800;font-size:0.68rem;cursor:pointer;margin-right:6px;border:1px solid rgba(30,136,229,0.3);">
          🏷️ ${sym}
        </span>
      `).join('');

      // Default fallback if takeaway isn't generated by backend yet
      const takeawayStr = h.takeaway || (h.sentiment === 'bullish' ? `📈 POSITIVE IMPACT: ${h.title}` : h.sentiment === 'bearish' ? `📉 NEGATIVE IMPACT: ${h.title}` : `⚡ Catalyst Focus: ${h.title}`);

      html += `
        <a href="${h.url}" target="_blank" rel="noopener" 
          style="text-decoration:none;display:block;padding:16px;border-radius:12px;background:var(--surface);border:1px solid var(--border);border-left:6px solid ${badgeColor};transition:all 0.2s;box-shadow:var(--shadow-sm);"
          onmouseover="this.style.borderColor='#1E88E5';this.style.transform='translateY(-1px)';" onmouseout="this.style.borderColor='var(--border)';this.style.transform='none';">
          
          <!-- 1. The Dynamic Catalyst Takeaway Banner (Top of Card) -->
          <div style="background:${badgeBg};border:1px solid ${badgeColor}33;border-radius:8px;padding:10px 14px;margin-bottom:12px;">
            <div style="font-size:0.85rem;font-weight:700;color:${badgeColor};line-height:1.4;">
              ${takeawayStr}
            </div>
          </div>

          <!-- 2. Source & Metadata Row -->
          <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px;">
            <span style="font-size:0.68rem;padding:2px 8px;border-radius:20px;font-weight:600;${severityStyle}">
              ${h.impact_rating || 'Low'} Impact
            </span>
            <span style="font-size:0.68rem;color:var(--text-muted);">${h.source} ${timeStr ? `• ${timeStr}` : ''}</span>
          </div>

          <!-- 3. Original Headline (Secondary Focus) -->
          <div style="font-size:0.78rem;font-weight:500;color:var(--text-secondary);line-height:1.4;margin-bottom:4px;border-left:2px solid var(--border);padding-left:8px;">
            <span style="color:var(--text-muted);font-weight:600;font-size:0.7rem;text-transform:uppercase;">Source Headline:</span> ${h.title}
          </div>

          <!-- 4. Summary Text -->
          <div style="font-size:0.75rem;color:var(--text-secondary);line-height:1.4;margin-bottom:12px;padding-left:10px;">
            ${h.summary || ''}
          </div>

          <!-- 5. Interactive Stock Tags & Sentiment Pill -->
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;border-top:1px solid var(--border);padding-top:10px;">
            <div style="display:flex;align-items:center;">
              ${stockPills || '<span style="font-size:0.68rem;color:var(--text-muted);font-weight:600;">🏷️ GENERAL MARKET</span>'}
            </div>
            <span style="padding:3px 10px;border-radius:8px;background:${badgeColor}20;color:${badgeColor};font-weight:800;text-transform:uppercase;font-size:0.65rem;letter-spacing:0.5px;">
              ${h.sentiment}
            </span>
          </div>
        </a>`;
    });

    html += '</div>';
    container.innerHTML = html;
  }

  setCategory(cat) {
    this._currentCategory = cat;
    if (cat === 'All' && this._headlines.length === 0) {
      // 'All Feeds' tab clicked with no cached data — trigger a real fetch
      this.load('', true);
    } else {
      this.render();
    }
  }

  async renderWidget(symbol, targetId) {
    const container = document.getElementById(targetId);
    if (!container) return;

    try {
      const res = await app.apiFetch(`/api/news?symbol=${encodeURIComponent(symbol)}`);
      if (!res.ok) return;
      const data = await res.json();
      const headlines = (data.headlines || []).slice(0, 5);

      if (headlines.length === 0) {
        container.innerHTML = '<div style="color:var(--text-muted);font-size:0.78rem;padding:8px;">No recent news for this stock.</div>';
        return;
      }

      container.innerHTML = headlines.map(h => {
        const icon = h.sentiment === 'bullish' ? '🟢' : h.sentiment === 'bearish' ? '🔴' : '⚪';
        return `<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:0.78rem;">
          <a href="${h.url}" target="_blank" rel="noopener" style="color:var(--text-primary);text-decoration:none;">
            ${icon} ${h.title}
          </a>
          <div style="font-size:0.65rem;color:var(--text-muted);margin-top:2px;">${h.source}</div>
        </div>`;
      }).join('');
    } catch (e) {
      // Non-critical widget
    }
  }

  _formatTime(timeStr) {
    try {
      const d = new Date(timeStr);
      if (isNaN(d)) return '';
      const now = new Date();
      const diffMs = now - d;
      const diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffHr = Math.floor(diffMin / 60);
      if (diffHr < 24) return `${diffHr}h ago`;
      return `${Math.floor(diffHr / 24)}d ago`;
    } catch { return ''; }
  }
}

window.newsFeed = new NewsFeed();
