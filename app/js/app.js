/**
 * TradeSignal — Main App Controller
 * Navigation, page routing, event binding, data flow orchestration
 */
const app = {
  currentPage: 'dashboard',
  scoringMode: 'equity',
  stockData: [],
  sectorSortField: 'avgScore',
  sectorSortDesc: true,

  // ── Initialize ──
  async init() {
    console.log('🚀 App initializing...');
    this.bindNavigation();
    this.bindSettings();
    
    // 1. Load config from server (for API Key/Secret pre-population)
    await this.loadConfig();

    // 2. IMPORTANT: Check for auto-login from Kite redirect IMMEDIATELY
    // This MUST happen before other UI binds to avoid being blocked by errors
    await this.checkAutoLogin();

    this.bindDashboard();
    
    // Check if we are in redirect mode — if so, STAY ON SETTINGS
    const params = new URLSearchParams(window.location.search);
    const inRedirect = params.has('request_token') || params.has('status');
    if (inRedirect) {
      this.navigateTo('settings');
    } else {
      this.navigateTo('dashboard');
    }

    // Wrap others in a try-catch for safety
    try {
      this.bindScreener();

      this.bindAnalysis();
      this.bindRecommendations();
      this.bindHistorical();
      if (window.historicalAnalysis) historicalAnalysis.init();
      if (window.notionNotes) notionNotes.init();
      this.bindAlerts();
      this.bindSearch();
      this.bindConnectionEvents();
      this.bindWatchlist();
      this.bindNewsFeed();
      this.bindFNOSessions();
      this.bindRecoTracker();
      this.bindSmcDashboard();
      if (window.researchAI) researchAI.init();
    } catch (e) {
      console.warn('Non-critical UI binding failed:', e);
    }

    this.loadSavedSettings();
    await this.autoConnectBackend();          // Auto-test backend (2 retries)
    await this.hydrateKiteSessionFromBackend(); // Auto-connect Kite (2 retries)
    await this.syncSavedKiteSession();
    // Defer market ticker by 3s — avoids blocking first paint on mobile
    setTimeout(() => this.updateMarketTicker(), 3000);
    
    try {
      if (typeof alertEngine !== 'undefined') {
        alertEngine.renderRules();
        alertEngine.renderAlerts();
        alertEngine.updateBadge();
        alertEngine.requestPermission();
      }
    } catch (e) { console.warn('AlertEngine init failed:', e); }

    // Populate stock dropdowns
    this.populateStockDropdowns();
    console.log('✅ App initialized successfully');
  },

  async apiFetch(endpoint, options = {}) {
    const url = endpoint.startsWith('http') ? endpoint : `${window.location.origin}${endpoint}`;
    const headers = {
      ...(options.headers || {}),
      'X-Kite-Api-Key': kiteAPI?.apiKey || '',
      'X-Kite-Access-Token': kiteAPI?.accessToken || ''
    };
    
    const fetchOptions = {
      ...options,
      headers,
      credentials: 'include'
    };

    return fetch(url, fetchOptions);
  },

  async loadConfig() {
    try {
      const resp = await this.apiFetch('/api/config');
      if (resp.ok) {
        const config = await resp.json();
        console.log('API Config loaded from server:', { 
          has_key: !!config.api_key, 
          has_secret: !!config.api_secret_loaded 
        });

        // ALWAYS use server .env values for api_key — they are
        // authoritative. localStorage values can become stale across sessions/devices
        // and cause "Invalid Checksum" if they don't match the server.
        const apiKeyEl = document.getElementById('set-api-key');
        if (config.api_key && apiKeyEl) {
          apiKeyEl.value = config.api_key;
          kiteAPI.apiKey = config.api_key;
          console.log('Set API Key from server config (always authoritative)');
        }
        
        // API Secret is no longer exposed by the server for security.
        // Show status indicator instead.
        const apiSecretEl = document.getElementById('set-api-secret');
        if (apiSecretEl && config.api_secret_loaded) {
          apiSecretEl.placeholder = '✓ Loaded from server .env';
          console.log('API Secret confirmed loaded on server (not exposed to frontend)');
        }
      } else {
        console.warn('Failed to load server config. Status:', resp.status);
      }
    } catch (e) { console.warn('Failed to load server config:', e); }
  },

  async checkAutoLogin() {
    const params = new URLSearchParams(window.location.search);
    const requestToken = params.get('request_token');
    if (requestToken) {
      await this.autoGenerateSession(requestToken);
    }
  },

  async autoGenerateSession(requestToken) {
    this.navigateTo('settings');
    const statusEl = document.getElementById('kite-status');
    if (statusEl) statusEl.innerHTML = '<span class="tag tag-neutral">🔄 Auto-generating session...</span>';
    
    try {
      // Sync API key and Secret from input fields before generating session
      const apiKey = document.getElementById('set-api-key')?.value;
      const apiSecret = document.getElementById('set-api-secret')?.value;
      if (apiKey) kiteAPI.apiKey = apiKey;
      
      // If still no API key, try the one from config (populated by loadConfig)
      if (!kiteAPI.apiKey) {
        await this.loadConfig();
      }

      const resp = await kiteAPI.generateSession(requestToken, apiSecret);
      if (resp.access_token) {
        if (document.getElementById('set-access-token')) {
          document.getElementById('set-access-token').value = resp.access_token;
        }
        kiteAPI.accessToken = resp.access_token;
        this.saveSettings();
        kiteAPI.configure(kiteAPI.apiKey, resp.access_token, document.getElementById('set-backend-url')?.value || window.location.origin);
        if (statusEl) statusEl.innerHTML = '<span class="tag tag-bullish">✅ Auto-login successful! Loading instruments in background...</span>';
        
        // Clean up URL params
        window.history.replaceState({}, document.title, window.location.pathname);
        
        // Load instruments in the BACKGROUND — do NOT await or it hangs the redirect on mobile
        // The instruments will be ready by the time the user navigates to screener/options
        kiteAPI.getInstruments().then(() => {
          equityScreener.getFNOUniverse().then(() => {
            this.populateStockDropdowns();
            this.updateMarketTicker();
            if (statusEl) statusEl.innerHTML = '<span class="tag tag-bullish">✅ Connected & Ready!</span>';
          }).catch(e => console.warn('Background FNO universe discovery failed:', e));
        }).catch(e => console.warn('Background instruments load failed:', e));
      }
    } catch (e) {
      if (statusEl) statusEl.innerHTML = `<span class="tag tag-bearish">❌ Auto-login failed: ${e.message}</span>`;
    }
  },

  // ── Navigation ──
  bindNavigation() {
    document.querySelectorAll('.nav-item[data-page]').forEach(item => {
      item.addEventListener('click', () => {
        const page = item.dataset.page;
        if (page === 'apex-dashboard') {
          window.open('apex-dashboard.html', '_blank');
          return;
        }
        this.navigateTo(page);
      });
    });
  },

  navigateTo(page) {
    // Update sidebar
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const navItem = document.querySelector(`.nav-item[data-page="${page}"]`);
    if (navItem) navItem.classList.add('active');

    // Update pages
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const pageEl = document.getElementById(`page-${page}`);
    if (pageEl) pageEl.classList.add('active');

    // Update title
    const titles = {
      dashboard:       'Dashboard',
      screener:        'F&O Equity Screener',

      analysis:        'Stock Analysis',
      watchlist:       'Watchlist',
      portfolio:       'Portfolio Tracker',
      'live-movers':   'Live Movers',
      'index-movers':  'Index Movers Dashboard',
      news:            'News & Sentiment',
      strategy:        'Strategy Builder',
      backtest:        'Backtesting Engine',
      journal:         'Trade Journal',
      paper:           'Paper Trading',
      recommendations: 'Live Recommendations',
      'reco-tracker': 'Reco Tracker',
      historical:      'Historical Charts',
      'historical-analysis': 'Historical Predictive Analytics Engine',
      notes:           'Notion Notes Workspace',
      alerts:          'Alert Center',
      settings:        'Settings',
      'smc-dashboard': 'SMC Options Signal Dashboard',
      'apex-dashboard': 'APEX Intraday Signal Dashboard',
      'multi-chart': 'Multi-Chart Tracking',
      'fno-session':   'FNO Session Analysis',
    };
    document.getElementById('page-title').textContent = titles[page] || page;
    this.currentPage = page;

    // Lazy init chart if historical
    if (page === 'historical' && !chartManager.chart) {
      chartManager.init('main-chart');
    }

    // Load data when navigating to dynamic pages
    if (page === 'watchlist' && window.watchlist) {
      watchlist.render();
    }
    if (page === 'portfolio' && window.portfolio) {
      portfolio.load();
    }
    if (page === 'live-movers' && window.liveMovers) {
      liveMovers.load();
    }
    if (page === 'news' && window.newsFeed) {
      // Only render cached state when navigating to the News page.
      // Feeds are ONLY fetched when user clicks the '📁 All Feeds' tab.
      newsFeed.render();
    }
    if (page === 'strategy' && window.strategyBuilder) {
      strategyBuilder.render();
    }
    if (page === 'backtest' && window.backtester) {
      backtester.render();
    }
    if (page === 'notes' && window.notionNotes) {
      notionNotes.loadNotes();
    }
    if (page === 'journal' && window.tradeJournal) {
      tradeJournal.init();
    }
    if (page === 'paper' && window.paperTrader) {
      paperTrader.init();
    }
    if (page === 'reco-tracker' && window.recoTracker) {
      recoTracker.init();
    }
    if (page === 'smc-dashboard' && window.smcDashboard) {
      smcDashboard.init();
    }

    if (page === 'multi-chart' && window.multiChartManager) {
      multiChartManager.init();
    }

    if (page === 'index-movers' && window.indexMovers && !indexMovers._data) {
      // page opened for first time — do nothing, user clicks Fetch
    }

    // Stop live movers refresh when navigating away
    if (page !== 'live-movers' && window.liveMovers) {
      liveMovers.stop();
    }
    // Stop index movers auto-refresh when navigating away
    if (page !== 'index-movers' && window.indexMovers) {
      indexMovers.stop();
    }
    // Stop recommendations auto-refresh when navigating away
    if (page !== 'recommendations') {
      this.stopRecoAutoRefresh();
    }
  },

  // ── Stock Dropdowns ──
  populateStockDropdowns() {
    const fnoList = equityScreener.getFNOUniverseSync();
    const sorted = [...fnoList].sort((a, b) => a.symbol.localeCompare(b.symbol));
    const symbolOptions = sorted.map(s => `<option value="${s.symbol}">${s.symbol} — ${s.name}</option>`).join('');
    const defaultOption = '<option value="">Select F&O Stock...</option>';

    // Select-style dropdowns (replace innerHTML)
    ['hist-symbol', 'alert-stock'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = defaultOption + symbolOptions;
    });

    // Options chain symbol dropdown (includes indices)
    const ocSymbol = document.getElementById('oc-symbol');
    if (ocSymbol) {
      ocSymbol.innerHTML = '<option value="">Select Symbol</option>' +
        '<option value="NIFTY">NIFTY 50</option>' +
        '<option value="BANKNIFTY">BANK NIFTY</option>' +
        '<option value="FINNIFTY">FIN NIFTY</option>' +
        sorted.map(s => `<option value="${s.symbol}">${s.symbol}</option>`).join('');
    }

    // FNO Session analyzer dropdown
    const fnoSessionSym = document.getElementById('fno-session-symbol');
    if (fnoSessionSym) {
      fnoSessionSym.innerHTML = '<option value="">Select F&O Stock</option>' +
        sorted.map(s => `<option value="${s.symbol}">${s.symbol}</option>`).join('');
    }

    // Analysis symbol datalist
    const analysisDatalist = document.getElementById('analysis-symbol-list');
    if (analysisDatalist) {
      analysisDatalist.innerHTML = sorted.map(s => `<option value="${s.symbol}">${s.symbol} — ${s.name}</option>`).join('');
    }

    // Datalist-style inputs (clear + append options)
    ['watchlist-stock-list', 'strategy-symbol-list', 'mc-stock-list'].forEach(id => {
      const dl = document.getElementById(id);
      if (dl) {
        dl.innerHTML = '';
        // Add index symbols for strategy builder and multi-chart
        if (id === 'strategy-symbol-list' || id === 'mc-stock-list') {
          ['NIFTY','BANKNIFTY','FINNIFTY','SENSEX'].forEach(s => {
            const o = document.createElement('option'); o.value = s; dl.appendChild(o);
          });
        }
        sorted.forEach(s => {
          const o = document.createElement('option'); o.value = s.symbol; dl.appendChild(o);
        });
      }
    });

    // Refresh historical analysis dashboard dropdown dynamically if initialized
    if (window.historicalAnalysis && typeof window.historicalAnalysis.populateDropdown === 'function') {
      window.historicalAnalysis.populateDropdown();
    }
  },

  // ── Dashboard ──
  bindDashboard() {
    document.getElementById('btn-run-scoring')?.addEventListener('click', () => this.runFullScoring());

    // Market Breadth refresh
    document.getElementById('btn-refresh-breadth')?.addEventListener('click', () => this.loadMarketBreadth());

    // Load earnings on dashboard init
    this.loadEarningsCalendar();

    // Leaderboard tabs
    document.querySelectorAll('#leaderboard-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#leaderboard-tabs .tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.filterLeaderboard(btn.dataset.filter);
      });
    });

    // Sector table sorting
    document.querySelectorAll('#sector-table th.sortable').forEach(th => {
      th.addEventListener('click', () => {
        const field = th.dataset.sort;
        if (this.sectorSortField === field) {
          this.sectorSortDesc = !this.sectorSortDesc;
        } else {
          this.sectorSortField = field;
          this.sectorSortDesc = true;
        }
        if (this.stockData && this.stockData.length > 0) {
          this.renderSectorialView(this.stockData);
        }
      });
    });
  },

  async runFullScoring() {
    const btn = document.getElementById('btn-run-scoring');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scoring...'; }

    try {
      // Run screener scan (live Kite API data only)
      const results = await equityScreener.scan(this.scoringMode);
      this.stockData = results;

      // Update dashboard stats
      const bullish = results.filter(s => s.signal === 'BULLISH' || s.signal === 'CALL').length;
      const bearish = results.filter(s => s.signal === 'BEARISH' || s.signal === 'PUT').length;
      const topStock = results[0];

      document.getElementById('stat-tracked').textContent = results.length;
      document.getElementById('stat-bullish').textContent = bullish;
      document.getElementById('stat-bearish').textContent = bearish;
      document.getElementById('stat-bullish-pct').textContent = results.length > 0 ? `${((bullish/results.length)*100).toFixed(0)}%` : '0%';
      document.getElementById('stat-bearish-pct').textContent = results.length > 0 ? `${((bearish/results.length)*100).toFixed(0)}%` : '0%';
      if (topStock) {
        document.getElementById('stat-top-score').textContent = topStock.score;
        document.getElementById('stat-top-stock').textContent = topStock.symbol;
      }

      // Render top picks (top 6)
      this.renderTopPicks(results.filter(s => s.score >= 70).slice(0, 6));
      
      // Render sectorial view
      this.renderSectorialView(results);
      
      // Render leaderboard
      this.renderLeaderboard(results);

      // Update reco count
      document.getElementById('reco-count').textContent = results.filter(s => s.score >= 70).length;

      // Auto-refresh breadth when scoring finishes
      this.loadMarketBreadth();
    } catch (e) {
      const grid = document.getElementById('top-picks-grid');
      if (grid) grid.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><h4>Connection Required</h4><p>${e.message}</p></div>`;
    }

    if (btn) { btn.disabled = false; btn.textContent = '▶ Run Scoring Engine'; }
  },

  renderTopPicks(picks) {
    const grid = document.getElementById('top-picks-grid');
    if (!grid) return;

    if (picks.length === 0) {
      grid.innerHTML = '<div class="empty-state"><div class="empty-icon">🔍</div><h4>No High-Score Signals</h4><p>No stocks scored above 70. Try lowering the threshold in Settings.</p></div>';
      return;
    }

    grid.innerHTML = picks.map(p => {
      const chg = p.changePercent || 0;
      const chgClass = chg >= 0 ? 'text-green' : 'text-red';
      // Badge colour driven by direction
      const isBull = p.signal === 'BULLISH' || p.signal === 'CALL';
      const isBear = p.signal === 'BEARISH' || p.signal === 'PUT';
      const scoreClass = isBull ? 'score-high' : isBear ? 'score-low' : 'score-medium';
      const signalTag = isBull ? 'tag-bullish' : isBear ? 'tag-bearish' : 'tag-neutral';
      const direction = isBull ? '📈' : isBear ? '📉' : '➡️';

      const isWatched = window.watchlist?.has(p.symbol);
      return `<div class="pick-card" onclick="app.scoreStock('${p.symbol}')">
        <div class="pick-score">
          <div class="score-badge ${scoreClass}">${p.score}</div>
        </div>
        <div class="pick-info">
          <h4>${direction} ${p.symbol}</h4>
          <div style="font-size:0.78rem;color:var(--text-secondary);">${p.name || ''} · ${p.sector || ''}</div>
          <div class="pick-meta">
            <span class="tag ${signalTag}">${p.signal}</span>
            <span class="text-muted" style="font-size:0.72rem;">RSI: ${p.rsi?.toFixed(0)} | Vol: ${p.volRatio?.toFixed(1)}x</span>
          </div>
        </div>
        <div class="pick-right">
          <div class="price">₹${p.ltp?.toFixed(2)}</div>
          <div class="pick-change ${chgClass}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</div>
          <button onclick="event.stopPropagation(); window.watchlist?.toggle('${p.symbol}', this);"
            title="${isWatched ? 'Remove from Watchlist' : 'Add to Watchlist'}"
            style="background:none;border:none;cursor:pointer;font-size:1rem;padding:2px 4px;">${isWatched ? '⭐' : '☆'}</button>
        </div>
      </div>`;
    }).join('');
  },

  renderSectorialView(results) {
    const tbody = document.getElementById('sector-body');
    if (!tbody) return;

    if (!results || results.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:30px;">No data</td></tr>';
      return;
    }

    const sectorWeights = {
      'Banking': 26, 'IT': 13, 'Energy': 10, 'FMCG': 8,
      'Auto': 7, 'Finance': 8, 'Pharma': 4, 'Infra': 7,
      'Metal': 3, 'Retail': 3, 'Telecom': 3
    };

    const sectors = {};
    results.forEach(s => {
      const sec = s.sector || 'Other';
      if (!sectors[sec]) {
        sectors[sec] = { name: sec, count: 0, scoreSum: 0, changeSum: 0, bullish: 0, bearish: 0 };
      }
      sectors[sec].count++;
      sectors[sec].scoreSum += (s.score || 0);
      sectors[sec].changeSum += (s.changePercent || 0);
      if (s.signal === 'BULLISH' || s.signal === 'CALL') sectors[sec].bullish++;
      else if (s.signal === 'BEARISH' || s.signal === 'PUT') sectors[sec].bearish++;
    });

    const sectorStats = Object.values(sectors).map(sec => {
      sec.avgScore = sec.count > 0 ? (sec.scoreSum / sec.count) : 0;
      sec.avgChange = sec.count > 0 ? (sec.changeSum / sec.count) : 0;
      sec.weight = sectorWeights[sec.name] || '-';
      return sec;
    });

    // First assign Momentum Ranks based on avgScore
    const rankArray = [...sectorStats].sort((a, b) => b.avgScore - a.avgScore);
    rankArray.forEach((sec, i) => { sec.rank = i + 1; });

    // Now sort based on user's chosen field
    const field = this.sectorSortField || 'avgScore';
    const desc = this.sectorSortDesc !== false;

    sectorStats.sort((a, b) => {
      let valA = a[field];
      let valB = b[field];

      if (field === 'name') {
        return desc ? valB.localeCompare(valA) : valA.localeCompare(valB);
      }
      if (field === 'weight') {
        valA = valA === '-' ? 0 : valA;
        valB = valB === '-' ? 0 : valB;
      }

      return desc ? valB - valA : valA - valB;
    });

    // Update header icons for sorting
    document.querySelectorAll('#sector-table th.sortable').forEach(th => {
      th.innerHTML = th.innerHTML.replace(' ↑', ' ↕').replace(' ↓', ' ↕');
      if (th.dataset.sort === field) {
        th.innerHTML = th.innerHTML.replace(' ↕', desc ? ' ↓' : ' ↑');
      }
    });

    tbody.innerHTML = sectorStats.map((sec) => {
      const scoreClass = sec.avgScore >= 60 ? 'text-green' : sec.avgScore <= 40 ? 'text-red' : '';
      const strength = sec.avgScore >= 60 ? 'Strong' : sec.avgScore <= 40 ? 'Weak' : 'Neutral';
      const strengthTag = sec.avgScore >= 60 ? 'tag-bullish' : sec.avgScore <= 40 ? 'tag-bearish' : 'tag-neutral';
      const weightDisplay = typeof sec.weight === 'number' ? `~${sec.weight}%` : sec.weight;
      const changeClass = sec.avgChange > 0 ? 'text-green' : sec.avgChange < 0 ? 'text-red' : '';
      const changeSign = sec.avgChange > 0 ? '+' : '';
      
      return `<tr>
        <td style="font-weight:700;">${sec.name}</td>
        <td>${sec.count}</td>
        <td style="color:var(--text-secondary);">${weightDisplay}</td>
        <td class="${scoreClass}" style="font-weight:600;">${sec.avgScore.toFixed(1)}</td>
        <td><span class="text-green" style="font-weight:600">${sec.bullish}</span> / <span class="text-red" style="font-weight:600">${sec.bearish}</span></td>
        <td class="${changeClass}" style="font-weight:600;">${changeSign}${sec.avgChange.toFixed(2)}%</td>
        <td><span class="tag ${strengthTag}">#${sec.rank} - ${strength}</span></td>
      </tr>`;
    }).join('');
  },

  renderLeaderboard(data) {
    this._leaderboardData = data;
    this.filterLeaderboard('all');
  },

  filterLeaderboard(filter) {
    this._leaderboardFilter = filter;          // remember active filter
    const data = this._leaderboardData || [];
    let filtered = data;
    if (filter === 'bullish') filtered = data.filter(s => s.signal === 'BULLISH' || s.signal === 'CALL');
    if (filter === 'bearish') filtered = data.filter(s => s.signal === 'BEARISH' || s.signal === 'PUT');

    const tbody = document.getElementById('leaderboard-body');
    if (!tbody) return;

    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="13" class="text-muted" style="text-align:center;padding:30px;">No data</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.slice(0, 30).map((s, i) => {
      const chgClass = (s.changePercent || 0) >= 0 ? 'text-green' : 'text-red';
      // Score badge colour driven by signal direction, not raw score value
      const isBull = s.signal === 'BULLISH' || s.signal === 'CALL';
      const isBear = s.signal === 'BEARISH' || s.signal === 'PUT';
      const scoreClass = isBull ? 'score-high' : isBear ? 'score-low' : 'score-medium';
      const signalTag = isBull ? 'tag-bullish' : isBear ? 'tag-bearish' : 'tag-neutral';

      const f = s.factors || {};
      const techScore = f.technical?.score || 0;
      const paScore = f.priceAction?.score || 0;
      const oiScore = (f.oiAnalysis?.score || f.volume?.score || 0);
      const volScore = (f.volatility?.score || f.sectorMomentum?.score || 0);
      const catScore = (f.catalyst?.score || f.fundamentals?.score || 0);

      // Daily trend badge
      const trend = s.dailyTrend || 'Neutral';
      const trendStyle = trend === 'Bullish'
        ? 'background:rgba(38,166,154,0.15);color:#26A69A;border:1px solid rgba(38,166,154,0.35);'
        : trend === 'Bearish'
          ? 'background:rgba(239,83,80,0.15);color:#EF5350;border:1px solid rgba(239,83,80,0.35);'
          : 'background:rgba(120,144,156,0.1);color:var(--text-secondary);border:1px solid rgba(120,144,156,0.2);';
      const trendIcon = trend === 'Bullish' ? '▲' : trend === 'Bearish' ? '▼' : '─';
      const trendHtml = `<span style="font-size:0.7rem;font-weight:700;padding:2px 7px;border-radius:4px;white-space:nowrap;${trendStyle}">${trendIcon} ${trend}</span>`;

      const isWatched = window.watchlist?.has(s.symbol);
      return `<tr style="cursor:pointer;" onclick="app.scoreStock('${s.symbol}')">
        <td style="font-weight:700;color:var(--primary);">${i + 1}</td>
        <td><span class="stock-name">${s.symbol}</span></td>
        <td>₹${s.ltp?.toFixed(2)}</td>
        <td class="${chgClass}" style="font-weight:600;">${(s.changePercent || 0) >= 0 ? '+' : ''}${(s.changePercent || 0).toFixed(2)}%</td>
        <td><span class="score-badge ${scoreClass}" style="width:32px;height:32px;font-size:0.75rem;">${s.score}</span></td>
        <td><span class="tag ${signalTag}">${s.signal}</span></td>
        <td>${trendHtml}</td>
        <td>${techScore}</td>
        <td>${paScore}</td>
        <td>${oiScore}</td>
        <td>${volScore}</td>
        <td>${catScore}</td>
        <td onclick="event.stopPropagation();">
          <button onclick="window.watchlist?.toggle('${s.symbol}', this);"
            title="${isWatched ? 'Remove from Watchlist' : 'Add to Watchlist'}"
            style="background:none;border:none;cursor:pointer;font-size:1rem;padding:2px 6px;">${isWatched ? '⭐' : '☆'}</button>
        </td>
      </tr>`;
    }).join('');
  },

  downloadLeaderboardCSV() {
    const data = this._leaderboardData || [];
    if (data.length === 0) {
      alert('No leaderboard data to export. Run the Scoring Engine first.');
      return;
    }

    // Apply the same filter currently active
    const filter = this._leaderboardFilter || 'all';
    let filtered = data;
    if (filter === 'bullish') filtered = data.filter(s => s.signal === 'BULLISH' || s.signal === 'CALL');
    if (filter === 'bearish') filtered = data.filter(s => s.signal === 'BEARISH' || s.signal === 'PUT');
    filtered = filtered.slice(0, 30);

    const headers = ['Rank','Stock','LTP','Chg%','Score','Signal','Daily Trend','Technical','Price Action','OI/Options','Volatility','Catalyst'];

    const rows = filtered.map((s, i) => {
      const f = s.factors || {};
      return [
        i + 1,
        s.symbol,
        s.ltp?.toFixed(2) ?? '',
        ((s.changePercent || 0) >= 0 ? '+' : '') + (s.changePercent || 0).toFixed(2) + '%',
        s.score,
        s.signal,
        s.dailyTrend || 'Neutral',
        f.technical?.score || 0,
        f.priceAction?.score || 0,
        f.oiAnalysis?.score || f.volume?.score || 0,
        f.volatility?.score || f.sectorMomentum?.score || 0,
        f.catalyst?.score || f.fundamentals?.score || 0,
      ];
    });

    const csvContent = [headers, ...rows]
      .map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(','))
      .join('\n');

    const now = new Date();
    const ts = now.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' }).replace(/\//g, '-')
             + '_' + now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit' }).replace(/:/g, '').replace(' ', '');
    const filename = `FnO_Leaderboard_${filter}_${ts}.csv`;

    const blob = new Blob(['\uFEFF' + csvContent], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },



  // ── Screener ──
  bindScreener() {
    document.getElementById('btn-screen-run')?.addEventListener('click', () => {
      equityScreener.scan(this.scoringMode);
    });

    document.getElementById('screen-sector')?.addEventListener('change', (e) => {
      equityScreener.setFilter('sector', e.target.value);
    });

    document.getElementById('screen-signal')?.addEventListener('change', (e) => {
      equityScreener.setFilter('signal', e.target.value);
    });

    document.getElementById('screen-sort')?.addEventListener('change', (e) => {
      equityScreener.setFilter('sort', e.target.value);
    });

    // Filter chips
    document.querySelectorAll('#screener-filters .filter-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        document.querySelectorAll('#screener-filters .filter-chip').forEach(c => c.classList.remove('active'));
        chip.classList.add('active');
        equityScreener.setFilter('cap', chip.dataset.cap);
      });
    });
  },



  scoreStock(symbol) {
    this.navigateTo('analysis');
    const input = document.getElementById('analysis-symbol-input');
    if (input) {
      input.value = symbol;
      document.getElementById('btn-run-analysis')?.click();
    }
  },

  async viewStock(symbol) {
    this.navigateTo('historical');
    document.getElementById('hist-symbol').value = symbol;
    chartManager.init('main-chart');

    if (!kiteAPI.connected) {
      chartManager.showError('Kite API not connected. Go to Settings → Connect first.');
      return;
    }

    const ohlcv = await chartManager.loadFromAPI(symbol, this._histInterval || 'day', 180);
    if (ohlcv) this.updateTechIndicators(ohlcv);
  },

  // ── Recommendations ──
  bindRecommendations() {
    document.getElementById('btn-gen-reco')?.addEventListener('click', () => this.generateRecommendations());
    document.getElementById('btn-reco-auto')?.addEventListener('click', () => this.toggleRecoAutoRefresh());
    document.getElementById('btn-load-intraday')?.addEventListener('click', () => this.generateIntradayPicks());
  },

  _recoAutoRefresh: false,
  _recoRefreshTimer: null,

  toggleRecoAutoRefresh() {
    this._recoAutoRefresh = !this._recoAutoRefresh;
    const btn = document.getElementById('btn-reco-auto');
    if (btn) {
      btn.textContent = this._recoAutoRefresh ? '⏹ Stop' : '🔄 Auto';
      btn.classList.toggle('btn-danger',    this._recoAutoRefresh);
      btn.classList.toggle('btn-secondary', !this._recoAutoRefresh);
    }
    if (this._recoAutoRefresh) {
      this._scheduleRecoRefresh();
    } else {
      clearTimeout(this._recoRefreshTimer);
      this._recoRefreshTimer = null;
    }
  },

  _scheduleRecoRefresh() {
    clearTimeout(this._recoRefreshTimer);
    if (!this._recoAutoRefresh) return;
    // Only auto-refresh during market hours (9:15–15:30 IST)
    const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const mins = now.getHours() * 60 + now.getMinutes();
    const marketOpen = now.getDay() >= 1 && now.getDay() <= 5 && mins >= 555 && mins <= 930;
    if (!marketOpen) {
      this._recoAutoRefresh = false;
      const btn = document.getElementById('btn-reco-auto');
      if (btn) { btn.textContent = '🔄 Auto'; btn.classList.remove('btn-danger'); btn.classList.add('btn-secondary'); }
      return;
    }
    this._recoRefreshTimer = setTimeout(async () => {
      await this.generateRecommendations();
      this._scheduleRecoRefresh();
    }, 60000);
  },

  stopRecoAutoRefresh() {
    this._recoAutoRefresh = false;
    clearTimeout(this._recoRefreshTimer);
    this._recoRefreshTimer = null;
    const btn = document.getElementById('btn-reco-auto');
    if (btn) { btn.textContent = '🔄 Auto'; btn.classList.remove('btn-danger'); btn.classList.add('btn-secondary'); }
  },

  // ── Intraday Picks (15-min candles, VWAP + ORB scoring) ──
  async generateIntradayPicks() {
    const btn = document.getElementById('btn-load-intraday');
    const container = document.getElementById('reco-list-intraday');
    const ts = document.getElementById('intraday-timestamp');
    if (btn) { btn.textContent = '⏳ Loading...'; btn.disabled = true; }
    if (container) container.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-muted);font-size:0.82rem;">Fetching 15-min candles…</div>';

    try {
      // Universe priority:
      // 1. kiteAPI.fnoInstruments — all F&O eligible large/midcap stocks (~180 stocks)
      // 2. stockData (from prior scoring run)
      // 3. Hardcoded Nifty top-50 as emergency fallback (no API dependency)
      const NIFTY_TOP50_FALLBACK = [
        'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','SBIN','BHARTIARTL',
        'KOTAKBANK','ITC','LT','AXISBANK','BAJFINANCE','ASIANPAINT','MARUTI','HCLTECH',
        'SUNPHARMA','TITAN','WIPRO','ULTRACEMCO','NTPC','POWERGRID','ADANIENT','TATAMOTORS',
        'ONGC','JSWSTEEL','TATASTEEL','TECHM','COALINDIA','GRASIM','BAJAJFINSV','ADANIPORTS',
        'NESTLEIND','CIPLA','DIVISLAB','DRREDDY','EICHERMOT','HINDALCO','HEROMOTOCO','BPCL',
        'BRITANNIA','SBILIFE','HDFCLIFE','INDUSINDBK','BAJAJ-AUTO','APOLLOHOSP','TATACONSUM',
        'PIDILITIND','HAVELLS','TRENT'
      ];

      let universe = [];

      // Priority 1: F&O instruments from kiteAPI (best source — already large/midcap)
      if (kiteAPI?.fnoInstruments?.length > 0) {
        universe = kiteAPI.fnoInstruments
          .filter(i => i.segment === 'NSE_FO' || i.exchange === 'NSE')
          .map(i => i.tradingsymbol || i.name)
          .filter(Boolean)
          .filter((v, i, a) => a.indexOf(v) === i); // deduplicate
      }
      // Priority 2: stockData (if scoring was already run)
      if (universe.length === 0 && this.stockData?.length > 0) {
        universe = this.stockData.map(s => s.symbol);
      }
      // Priority 3: hardcoded fallback
      if (universe.length === 0) universe = NIFTY_TOP50_FALLBACK;

      // Cap at 30 (backend limit per batch)
      universe = universe.slice(0, 30);
      if (container) container.innerHTML = `<div style="padding:16px;text-align:center;color:var(--text-muted);font-size:0.82rem;">Scanning ${universe.length} large & midcap stocks…</div>`;

      const resp = await fetch('/api/intraday-candles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbols: universe })
      });

      if (!resp.ok) throw new Error(`API error ${resp.status}`);
      const candleData = await resp.json();

      // Score each symbol
      const picks = [];
      for (const [symbol, cdata] of Object.entries(candleData)) {
        if (cdata.error || !cdata.closes || cdata.closes.length < 5) continue;
        // Find stock metadata from main data
        const meta = (this.stockData || []).find(s => s.symbol === symbol) || {};
        const ltp = meta.ltp || cdata.closes[cdata.closes.length - 1];

        const result = scoringEngine.scoreIntraday({
          closes: cdata.closes, highs: cdata.highs,
          lows: cdata.lows, volumes: cdata.volumes, ltp
        });

        if (result.direction !== 'NEUTRAL' && result.total >= 55) {
          picks.push({
            symbol, ltp,
            name: meta.name || symbol,
            sector: meta.sector || '—',
            ...result
          });
        }
      }

      // Sort by score descending
      picks.sort((a, b) => b.total - a.total);
      this._recoIntraday = picks;
      this.renderIntradayPicks();
      if (ts) ts.textContent = `Updated ${new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;

    } catch (err) {
      if (container) container.innerHTML = `<div class="empty-state"><div class="empty-icon">❌</div><h4>Failed to load intraday data</h4><p>${err.message}</p></div>`;
    } finally {
      if (btn) { btn.textContent = '⚡ Load Intraday'; btn.disabled = false; }
    }
  },

  renderIntradayPicks() {
    const picks = this._recoIntraday || [];
    const container = document.getElementById('reco-list-intraday');
    if (!container) return;

    if (picks.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><h4>No strong intraday setups</h4><p>Market may be in consolidation. Try again after 10:00 AM when trends establish.</p></div>';
      return;
    }

    container.innerHTML = `<div class="top-picks-grid" style="padding:4px 0;">` + picks.map(p => {
      const isBuy = p.direction === 'BUY';
      const dirColor  = isBuy ? '#26A69A' : '#EF5350';
      const dirBg     = isBuy ? 'rgba(38,166,154,0.1)' : 'rgba(239,83,80,0.1)';
      const dirBorder = isBuy ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)';
      const scoreColor = p.total >= 75 ? '#4CAF50' : p.total >= 60 ? '#FFA726' : '#78909C';
      const scoreBg    = p.total >= 75 ? 'rgba(76,175,80,0.12)' : p.total >= 60 ? 'rgba(255,167,38,0.12)' : 'rgba(120,144,156,0.12)';
      const r = p.risk || {};
      const orbStatus = isBuy
        ? (p.ltp > p.orbHigh ? '✅ Above ORB' : p.ltp > (p.orbHigh + p.orbLow) / 2 ? '⚠️ Mid ORB' : '❌ Below ORB')
        : (p.ltp < p.orbLow  ? '✅ Below ORB' : p.ltp < (p.orbHigh + p.orbLow) / 2 ? '⚠️ Mid ORB' : '❌ Above ORB');
      const vwapStatus = isBuy
        ? (p.vwapPct > 0 ? `+${p.vwapPct.toFixed(1)}% above VWAP` : `${p.vwapPct.toFixed(1)}% below VWAP`)
        : (p.vwapPct < 0 ? `${p.vwapPct.toFixed(1)}% below VWAP` : `+${p.vwapPct.toFixed(1)}% above VWAP`);

      // Phase from daily OHLCV (lookup in stockData for daily candle history)
      const dailyMeta = (this.stockData || []).find(d => d.symbol === p.symbol) || p;
      const phase = this.detectStockPhase(dailyMeta);
      const phaseTag = `<span style="font-size:0.68rem;font-weight:600;padding:2px 7px;border-radius:4px;
        background:${phase.bg};color:${phase.color};border:1px solid ${phase.border};">${phase.label}</span>`;

      return `<div class="reco-card ${isBuy ? 'buy' : 'sell'}" style="border-top:3px solid #00BCD4;">
        <div class="reco-header">
          <div>
            <h3 style="font-family:var(--font-display);font-size:1.1rem;font-weight:700;">${p.symbol}</h3>
            <div style="font-size:0.75rem;color:var(--text-secondary);">${p.sector} · 15-min signal</div>
          </div>
          <div style="text-align:right;">
            <span style="font-size:0.8rem;padding:3px 10px;border-radius:6px;font-weight:700;
              background:${dirBg};color:${dirColor};border:1px solid ${dirBorder};">
              ${isBuy ? '▲ BUY' : '▼ SELL'}
            </span>
            <div style="margin-top:6px;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;
              justify-content:center;font-size:0.78rem;font-weight:700;
              background:${scoreBg};color:${scoreColor};border:1px solid ${scoreColor}44;margin-left:auto;">
              ${p.total}
            </div>
          </div>
        </div>

        <div style="margin:8px 0 6px;display:flex;gap:6px;flex-wrap:wrap;">
          <span style="font-size:0.68rem;font-weight:600;padding:2px 7px;border-radius:4px;background:rgba(0,188,212,0.1);color:#00BCD4;border:1px solid rgba(0,188,212,0.25);">${orbStatus}</span>
          <span style="font-size:0.68rem;font-weight:600;padding:2px 7px;border-radius:4px;background:rgba(0,0,0,0.06);color:var(--text-secondary);">VWAP ₹${p.vwap} · ${vwapStatus}</span>
          ${phaseTag}
          ${p.volRatio >= 1.5 ? `<span style="font-size:0.68rem;font-weight:600;padding:2px 7px;border-radius:4px;background:rgba(171,71,188,0.1);color:#AB47BC;">🔥 Vol ${p.volRatio.toFixed(1)}x</span>` : ''}
        </div>

        <div class="reco-details">
          <div class="reco-detail-item">
            <div class="detail-label">Entry</div>
            <div class="detail-value">₹${(r.entry || p.ltp).toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Target 1</div>
            <div class="detail-value text-green">₹${(r.target1 || 0).toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Target 2</div>
            <div class="detail-value text-green">₹${(r.target2 || 0).toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Stop Loss</div>
            <div class="detail-value text-red">₹${(r.stopLoss || 0).toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">ORB Range</div>
            <div class="detail-value">₹${p.orbLow}–${p.orbHigh}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">RSI (15m)</div>
            <div class="detail-value">${p.rsi.toFixed(0)}</div>
          </div>
        </div>

        <div style="margin-top:10px;font-size:0.73rem;color:var(--text-secondary);line-height:1.5;">
          <strong>Intraday Setup:</strong> Score ${p.total}/100 | EMA9: ₹${p.ema9} · EMA21: ₹${p.ema21} | Vol: ${p.volRatio.toFixed(1)}x avg
        </div>
      </div>`;
    }).join('') + '</div>';
  },

  async generateRecommendations() {
    if (this.stockData.length === 0) {
      await this.runFullScoring();
    }

    const now = new Date();
    document.getElementById('reco-timestamp').textContent = 
      `Generated: ${now.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })}`;

    // Fetch batch snapshots for all scored stocks (enriched real-time data)
    try {
      const symbols = this.stockData.map(s => s.symbol);
      // Fetch in batches of 40 to avoid URL length limits
      for (let i = 0; i < symbols.length; i += 40) {
        const batch = symbols.slice(i, i + 40);
        const resp = await kiteAPI.getBatchSnapshots(batch);
        const snapshots = resp.snapshots || {};
        // Merge snapshot data into each stock
        this.stockData.forEach(s => {
          if (snapshots[s.symbol]) {
            s.snapshot = snapshots[s.symbol];
          }
        });
      }
    } catch (e) {
      console.warn('Batch snapshots unavailable, using historical data only:', e.message);
    }

    // Score stocks in both modes and store separately
    // Threshold raised 45→55: filters weak signals lacking multi-factor confirmation
    const equityScored = this.stockData
      .filter(s => s.score >= 55 && (s.signal === 'BULLISH' || s.signal === 'BEARISH'))
      .sort((a, b) => b.score - a.score)
      .slice(0, 20);

    // Re-score in options mode for the options tab (uses snapshot data if available)
    // Threshold raised 25→38: requires meaningful OI + momentum confluence
    const optionsScored = this.stockData.map(s => {
      const result = scoringEngine.scoreOptions(s);
      return {
        ...s,
        optScore: Math.round(result.total),
        optSignal: result.direction,
        optSignalStrength: result.signalStrength,
        optFactors: result.factors,
        optIV: result.iv,
        optPCR: result.pcr,
        optFutPremium: result.futPremium,
        optBuildUp: result.oiBuildUp,
        optBlockDeal: result.blockDeal,
        optLiveChange: result.liveChangePct,
        optRisk: result.risk
      };
    })
      .filter(s => {
        if (!['CALL', 'PUT'].includes(s.optSignal)) return false;
        if (s.optScore < 38) return false;
        // Quality gate: require at least ONE strong confirming factor.
        // Options only make money when there is real momentum, volume, or OI conviction.
        // A moderate score (38-45) on weak factors = noise. Reject it.
        const f = s.optFactors || {};
        const momentum = f.momentumTrend?.score || 0;
        const volume   = f.volumeOrderFlow?.score || 0;
        const oi       = f.derivatives?.score     || 0;
        const hasStrongMomentum = momentum >= 10; // >2% move with VWAP confirmation
        const hasStrongVolume   = volume   >= 8;  // volume surge + order book pressure
        const hasStrongOI       = oi       >= 10; // clear long/short build-up
        // Score >= 55 passes without factor check (strong overall = sufficient)
        return s.optScore >= 55 || hasStrongMomentum || hasStrongVolume || hasStrongOI;
      })
      .sort((a, b) => b.optScore - a.optScore)
      .slice(0, 20);

    this._recoEquity = equityScored;
    this._recoOptions = optionsScored;

    // Render both workspaces
    this.renderRecommendationSection('equity');
    this.renderRecommendationSection('options');

    // ── Auto-capture recommendations to Reco Tracker ──
    if (window.recoTracker) {
      recoTracker.autoCapture(equityScored, 'equity_picks');
      recoTracker.autoCapture(optionsScored, 'options_picks');
    }

    const totalCount = new Set([...equityScored.map(s => s.symbol), ...optionsScored.map(s => s.symbol)]).size;
    document.getElementById('reco-count').textContent = totalCount;

    this.filterRecommendations('equity');
    this.filterRecommendations('options');
  },

  renderRecommendationSection(rtype) {
    const data = rtype === 'options' ? (this._recoOptions || []) : (this._recoEquity || []);
    const isOptionsMode = rtype === 'options';
    const list = document.getElementById(rtype === 'options' ? 'reco-list-options' : 'reco-list-equity');
    if (!list || data.length === 0) {
      if (list) list.innerHTML = '<div class="empty-state"><div class="empty-icon">🎯</div><h4>No strong directional picks</h4><p>Generate the report again during market hours or widen the signal filters to see more options.</p></div>';
      return;
    }

    list.innerHTML = data.map(s => {
      const score = isOptionsMode ? s.optScore : s.score;
      const signal = isOptionsMode ? s.optSignal : s.signal;
      const strength = isOptionsMode ? (s.optSignalStrength || 'NORMAL') : '';
      const signalDisplay = isOptionsMode && signal ? (strength === 'STRONG' ? `STRONG ${signal}` : strength === 'WEAK' ? `WEAK ${signal}` : signal) : signal;
      const isCall = isOptionsMode ? typeof signal === 'string' && signal.includes('CALL') : signal === 'BULLISH';
      const isBearish = isOptionsMode ? typeof signal === 'string' && signal.includes('PUT') : signal === 'BEARISH';
      const direction = isCall ? 'BUY' : isBearish ? 'SELL' : 'WATCH';
      const dirClass = isCall ? 'buy' : isBearish ? 'sell' : 'hold';

      const risk = isOptionsMode ? (s.optRisk || s.risk || {}) : (s.risk || {});
      const atr = isOptionsMode ? (s.optRisk?.atrUsed || s.atr || (s.ltp * 0.02)) : (s.atr || (s.ltp * 0.02));
      const target1 = risk.target1 || (isCall ? s.ltp + 2 * atr : isBearish ? s.ltp - 2 * atr : s.ltp + atr * 0.5);
      const target2 = risk.target2 || (isCall ? s.ltp + 3 * atr : isBearish ? s.ltp - 3 * atr : s.ltp + atr);
      const sl = risk.stopLoss || (isCall ? s.ltp - 1.5 * atr : isBearish ? s.ltp + 1.5 * atr : s.ltp - atr * 0.5);
      const rr = risk.riskReward || (Math.abs(target1 - s.ltp) / Math.abs(s.ltp - sl));
      const expectedReturn = ((Math.abs(target1 - s.ltp) / s.ltp) * 100);

      let confidence, confClass;
      if (score >= 70) { confidence = 'HIGH'; confClass = 'score-high'; }
      else if (score >= 55) { confidence = 'MEDIUM'; confClass = 'score-medium'; }
      else { confidence = 'MODERATE'; confClass = 'score-low'; }

      const tagClass = isCall ? 'tag-bullish' : isBearish ? 'tag-bearish' : 'tag-neutral';
      const signalLabel = isOptionsMode ? signalDisplay : direction;

      const isContrarian = isOptionsMode && s.signal && s.optSignal &&
        ((s.signal === 'BEARISH' && s.optSignal === 'CALL') ||
         (s.signal === 'BULLISH' && s.optSignal === 'PUT'));
      const contradictionLabel = isContrarian
        ? `<span style="font-size:0.68rem;padding:2px 7px;border-radius:4px;background:rgba(255,152,0,0.15);
             color:#FF9800;font-weight:700;border:1px solid rgba(255,152,0,0.3);"
             title="Options direction is against the equity EMA trend — intraday contrarian trade">⚡ Contrarian</span>`
        : '';

      const oiBuildUp = isOptionsMode ? (s.optBuildUp || s.optionsData?.buildUp || 'none') : (s.optionsData?.buildUp || 'none');
      const oiLabel = { long_buildup: '🟢 Long Build-up', short_buildup: '🔴 Short Build-up', short_covering: '🟡 Short Covering', long_unwinding: '🟠 Long Unwinding', none: '' }[oiBuildUp] || '';
      const blockLabel = (isOptionsMode && s.optBlockDeal) ? ' | 🏦 Block Deal' : '';

      const ivDisplay = isOptionsMode && s.optIV ? `IV: ${s.optIV.toFixed(1)}% | ` : '';
      const pcrDisplay = isOptionsMode && s.optPCR ? `PCR: ${s.optPCR.toFixed(2)} | ` : '';
      const futDisplay = isOptionsMode && s.optFutPremium != null ? `Fut: ${s.optFutPremium > 0 ? '+' : ''}${s.optFutPremium.toFixed(2)}% | ` : '';

      let strategy = '';
      if (isOptionsMode) {
        const ivLevel = s.optIV || 0;
        if (isCall) { strategy = ivLevel > 30 ? 'Bull Call Spread' : 'CE Buy'; }
        else if (isBearish) { strategy = ivLevel > 30 ? 'Bear Put Spread' : 'PE Buy'; }
        else { strategy = 'Iron Condor / Straddle'; }
      }

      // ── Accumulation / Breakout Phase (from existing daily OHLCV) ──
      const phase = this.detectStockPhase(s);
      const phaseHtml = `<span style="font-size:0.68rem;font-weight:600;padding:2px 7px;border-radius:4px;
        background:${phase.bg};color:${phase.color};border:1px solid ${phase.border};">${phase.label}</span>`;

      // ── Max Pain slot (options only — lazy loaded after render) ──
      const maxPainHtml = isOptionsMode
        ? `<div class="reco-detail-item" id="mp-slot-${s.symbol}">
            <div class="detail-label">Max Pain</div>
            <div class="detail-value" style="font-size:0.72rem;color:var(--text-muted);">⌛ loading…</div>
           </div>`
        : '';

      return `<div class="reco-card ${dirClass}">
        <div class="reco-header">
          <div>
            <h3 style="font-family:var(--font-display);font-size:1.1rem;font-weight:700;">${s.symbol}</h3>
            <div style="font-size:0.78rem;color:var(--text-secondary);">${s.name || ''} · ${s.sector || ''}</div>
            <div style="font-size:1rem;font-weight:700;margin-top:4px;color:var(--text-primary);">₹${s.ltp?.toFixed(2)} <span style="font-size:0.72rem;color:${(s.change || 0) >= 0 ? 'var(--green)' : 'var(--red)'};">${(s.change || 0) >= 0 ? '+' : ''}${(s.change || 0).toFixed(2)}%</span></div>
          </div>
          <div style="text-align:right;">
            <span class="tag ${tagClass}" style="font-size:0.82rem;">${signalLabel}</span>
            <div class="score-badge ${confClass}" style="width:36px;height:36px;font-size:0.8rem;margin-top:6px;" title="Confidence: ${confidence}">${Math.round(score)}</div>
          </div>
        </div>
        <div style="margin:8px 0 4px;display:flex;gap:6px;flex-wrap:wrap;">
          <span style="font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:4px;background:${score >= 70 ? 'rgba(38,166,154,0.12)' : score >= 55 ? 'rgba(255,167,38,0.12)' : 'rgba(120,144,156,0.12)'};color:${score >= 70 ? 'var(--green)' : score >= 55 ? '#F57C00' : '#78909C'};">${confidence} CONFIDENCE</span>
          ${isOptionsMode && strategy ? `<span style="font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:4px;background:rgba(30,136,229,0.1);color:var(--primary);">📋 ${strategy}</span>` : ''}
          ${phaseHtml}
          ${contradictionLabel}
          ${oiLabel ? `<span style="font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:4px;background:rgba(0,0,0,0.04);color:var(--text-secondary);">${oiLabel}</span>` : ''}
          ${blockLabel ? `<span style="font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:4px;background:rgba(171,71,188,0.1);color:#AB47BC;">🏦 Block Deal</span>` : ''}
        </div>
        <div class="reco-details">
          <div class="reco-detail-item">
            <div class="detail-label">Entry</div>
            <div class="detail-value">₹${s.ltp?.toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Target 1</div>
            <div class="detail-value text-green">₹${target1.toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Target 2</div>
            <div class="detail-value text-green">₹${target2.toFixed(2)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Stop Loss</div>
            <div class="detail-value text-red">₹${sl.toFixed(2)}</div>
          </div>
          ${maxPainHtml}
          <div class="reco-detail-item">
            <div class="detail-label">R:R Ratio</div>
            <div class="detail-value">1:${rr.toFixed(1)}</div>
          </div>
          <div class="reco-detail-item">
            <div class="detail-label">Exp. Return</div>
            <div class="detail-value text-green">${expectedReturn.toFixed(1)}%</div>
          </div>
        </div>
        <div style="margin-top:12px;font-size:0.75rem;color:var(--text-secondary);line-height:1.5;">
          <strong>Rationale:</strong> Score ${Math.round(score)}/100 | ${ivDisplay}${pcrDisplay}${futDisplay}RSI: ${s.rsi?.toFixed(0)} | MACD: ${s.macd?.histogram > 0 ? 'Bullish' : 'Bearish'} | Vol: ${s.volRatio?.toFixed(1)}x
        </div>
      </div>`;
    }).join('');

    // Lazy-load max pain for options picks after DOM is ready
    if (isOptionsMode) {
      setTimeout(() => this.loadMaxPainForOptions(data), 100);
    }
  },

  // ── Phase Detection (from existing daily OHLCV — no extra API call) ──
  detectStockPhase(s) {
    const closes = s.closes || [];
    const highs  = s.highs  || [];
    const volumes = s.volumes || [];
    if (closes.length < 15) return { label: '—', color: '#78909C', bg: 'rgba(120,144,156,0.1)', border: 'rgba(120,144,156,0.25)' };

    const ltp = s.ltp || closes[closes.length - 1];
    const last20High   = Math.max(...highs.slice(-20));
    const last20AvgVol = volumes.slice(-20).reduce((a,b) => a+b, 0) / Math.min(20, volumes.length);
    const lastVol      = volumes[volumes.length - 1] || 0;
    const last10Closes = closes.slice(-10);
    const range10      = Math.max(...last10Closes) - Math.min(...last10Closes);
    const rangeRatio   = ltp > 0 ? range10 / ltp : 1;

    // Breakout: at/near 20-day high with volume confirmation
    if (ltp >= last20High * 0.995 && lastVol > last20AvgVol * 1.3) {
      return { label: '🚀 Breaking Out', color: '#4CAF50', bg: 'rgba(76,175,80,0.12)', border: 'rgba(76,175,80,0.35)' };
    }
    // Near breakout: within 2% of 20-day high, no volume yet
    if (ltp >= last20High * 0.98) {
      return { label: '⚡ Near Breakout', color: '#FF9800', bg: 'rgba(255,152,0,0.12)', border: 'rgba(255,152,0,0.35)' };
    }
    // Accumulation: tight range (<5% in 10 days) below 20-day high
    if (rangeRatio < 0.05) {
      const quiet = lastVol < last20AvgVol * 0.85;
      return {
        label: quiet ? '🔄 Accumulating' : '🌀 Coiling',
        color: '#00BCD4', bg: 'rgba(0,188,212,0.1)', border: 'rgba(0,188,212,0.3)'
      };
    }
    // Trending up
    if (ltp > closes[closes.length - 5]) {
      return { label: '📈 Uptrend', color: '#26A69A', bg: 'rgba(38,166,154,0.1)', border: 'rgba(38,166,154,0.3)' };
    }
    return { label: '↔️ Ranging', color: '#78909C', bg: 'rgba(120,144,156,0.08)', border: 'rgba(120,144,156,0.2)' };
  },

  // ── Lazy Max Pain loader (called after options picks are rendered) ──
  async loadMaxPainForOptions(picks) {
    for (const s of picks) {
      const slot = document.getElementById(`mp-slot-${s.symbol}`);
      if (!slot) continue;
      try {
        const resp = await fetch(`/api/max-pain?symbol=${encodeURIComponent(s.symbol)}`);
        if (!resp.ok) { slot.querySelector('.detail-value').textContent = '—'; continue; }
        const d = await resp.json();
        if (d.error) { slot.querySelector('.detail-value').textContent = '—'; continue; }
        const pct = d.pct_from_spot;
        const dir = pct > 0 ? '▲' : pct < 0 ? '▼' : '=';
        const col = pct > 1.5 ? '#EF5350' : pct < -1.5 ? '#26A69A' : '#FF9800';
        slot.querySelector('.detail-value').innerHTML =
          `<span style="color:${col};font-weight:600;">₹${d.max_pain} <small>${dir}${Math.abs(pct).toFixed(1)}%</small></span>`;
      } catch (_) {
        slot.querySelector('.detail-value').textContent = '—';
      }
    }
  },

  filterRecommendations(rtype) {
    this.renderRecommendationSection(rtype);
  },


  // ── Historical Charts ──
  bindHistorical() {
    // Populate F&O stock dropdown
    if (window.equityScreener) {
      const fnoGroup = document.getElementById('hist-fno-stocks');
      if (fnoGroup) {
        const stocks = equityScreener.getFNOUniverseSync();
        stocks.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.symbol;
          opt.textContent = s.symbol;
          fnoGroup.appendChild(opt);
        });
      }
    }

    document.getElementById('btn-load-chart')?.addEventListener('click', async () => {
      const symbol = document.getElementById('hist-symbol')?.value;
      const range = parseInt(document.getElementById('hist-range')?.value || '90');
      if (!symbol) return;

      if (!kiteAPI.connected) {
        chartManager.init('main-chart');
        chartManager.showError('Kite API not connected. Go to Settings → Connect first.');
        return;
      }

      const interval = this._histInterval || 'day';
      chartManager.init('main-chart');

      const ohlcv = await chartManager.loadFromAPI(symbol, interval, range);
      if (ohlcv) this.updateTechIndicators(ohlcv);
    });

    // Interval tabs
    document.querySelectorAll('#hist-interval-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#hist-interval-tabs .tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this._histInterval = btn.dataset.interval;

        // Auto-adjust range for intraday
        const rangeEl = document.getElementById('hist-range');
        if (rangeEl) {
          const isIntra = ['5minute', '15minute'].includes(btn.dataset.interval);
          if (isIntra && parseInt(rangeEl.value) > 30) rangeEl.value = '10';
        }
      });
    });

  },

  updateTechIndicators(ohlcv) {
    if (!ohlcv || ohlcv.length === 0) return;
    const closes = ohlcv.map(o => o.close);
    const highs = ohlcv.map(o => o.high);
    const lows = ohlcv.map(o => o.low);

    const rsi = scoringEngine.computeRSI(closes);
    const macd = scoringEngine.computeMACD(closes);
    const adx = scoringEngine.computeADX(highs, lows, closes);
    const atr = scoringEngine.computeATR(highs, lows, closes);

    document.getElementById('ind-rsi').textContent = rsi.toFixed(1);
    document.getElementById('ind-rsi').style.color = rsi > 70 ? 'var(--red)' : rsi < 30 ? 'var(--green)' : 'var(--text-primary)';
    
    document.getElementById('ind-macd').textContent = macd.histogram > 0 ? '▲ Bullish' : '▼ Bearish';
    document.getElementById('ind-macd').style.color = macd.histogram > 0 ? 'var(--green)' : 'var(--red)';

    document.getElementById('ind-adx').textContent = adx.toFixed(1);
    document.getElementById('ind-atr').textContent = '₹' + atr.toFixed(2);
  },

  // ── Alerts ──
  bindAlerts() {
    document.getElementById('btn-add-alert')?.addEventListener('click', () => {
      document.getElementById('modal-overlay').classList.add('active');
    });

    document.getElementById('btn-modal-cancel')?.addEventListener('click', () => {
      document.getElementById('modal-overlay').classList.remove('active');
    });

    document.getElementById('btn-modal-save')?.addEventListener('click', () => {
      const stock = document.getElementById('alert-stock')?.value;
      const type = document.getElementById('alert-type')?.value;
      const value = document.getElementById('alert-value')?.value;
      if (stock && type && value) {
        alertEngine.addRule({ stock, type, value });
        document.getElementById('modal-overlay').classList.remove('active');
      }
    });

    document.getElementById('btn-clear-alerts')?.addEventListener('click', () => {
      alertEngine.clearAlerts();
    });
  },

  // ── Search ──
  bindSearch() {
    const searchInput = document.getElementById('global-search');
    const searchDropdown = document.createElement('div');
    searchDropdown.id = 'search-dropdown';
    searchDropdown.style.cssText = 'position:absolute;top:100%;left:0;right:0;background:#fff;border:1px solid rgba(21,101,192,0.15);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,0.12);z-index:9999;max-height:300px;overflow-y:auto;display:none;';
    if (searchInput) searchInput.parentElement.style.position = 'relative';
    searchInput?.parentElement?.appendChild(searchDropdown);

    let timeout;
    searchInput?.addEventListener('input', () => {
      clearTimeout(timeout);
      timeout = setTimeout(() => {
        const query = searchInput.value.trim().toUpperCase();
        if (query.length >= 1) {
          const allStocks = equityScreener.getFNOUniverseSync();
          const results = allStocks.filter(s =>
            s.symbol.includes(query) || s.name.toUpperCase().includes(query)
          ).slice(0, 15);

          if (results.length > 0) {
            searchDropdown.innerHTML = results.map(s => 
              `<div class="search-result-item" data-symbol="${s.symbol}" style="padding:8px 14px;cursor:pointer;border-bottom:1px solid #f0f4f8;font-size:0.82rem;transition:background 0.15s;">
                <strong style="color:var(--primary);">${s.symbol}</strong>
                <span style="color:#78909C;margin-left:6px;">${s.name} · ${s.sector}</span>
              </div>`
            ).join('');
            searchDropdown.style.display = 'block';

            // Click handlers for results
            searchDropdown.querySelectorAll('.search-result-item').forEach(item => {
                            item.addEventListener('click', () => {
                const sym = item.dataset.symbol;
                if (this.currentPage === 'multi-chart') {
                  const mcInput = document.getElementById('mc-stock-select');
                  if (mcInput) {
                    mcInput.value = sym;
                    document.getElementById('mc-display-chart-btn')?.click();
                  }
                } else if (this.currentPage === 'analysis') {
                  const analysisInput = document.getElementById('analysis-symbol-input');
                  if (analysisInput) {
                    analysisInput.value = sym;
                    document.getElementById('btn-run-analysis')?.click();
                  }
                } else {
                  this.viewStock(sym);
                }
                searchInput.value = '';
                searchDropdown.style.display = 'none';
              });
              item.addEventListener('mouseenter', () => item.style.background = 'rgba(30,136,229,0.06)');
              item.addEventListener('mouseleave', () => item.style.background = 'transparent');
            });
          } else {
            searchDropdown.innerHTML = '<div style="padding:12px 14px;color:#90A4AE;font-size:0.82rem;">No F&O stocks found</div>';
            searchDropdown.style.display = 'block';
          }
        } else {
          searchDropdown.style.display = 'none';
        }
      }, 200);
    });

    // Hide dropdown on blur
    searchInput?.addEventListener('blur', () => {
      setTimeout(() => { searchDropdown.style.display = 'none'; }, 250);
    });

    document.getElementById('btn-refresh')?.addEventListener('click', () => {
      this.runFullScoring();
    });
  },

  // ── Stock Analysis ──
  bindAnalysis() {
    let selectedMode = 'premarket';

    // Mode toggle
    document.querySelectorAll('[data-mode]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-mode]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        selectedMode = btn.dataset.mode;
      });
    });

    // Populate datalist from equityScreener's static F&O universe
    const datalist = document.getElementById('analysis-symbol-list');
    if (datalist && window.equityScreener) {
      const stocks = equityScreener.getFNOUniverseSync();
      stocks.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.symbol;
        opt.textContent = `${s.symbol} — ${s.name}`;
        datalist.appendChild(opt);
      });
    }

    const getSymbol = () => {
      const raw = (document.getElementById('analysis-symbol-input')?.value || '').trim().toUpperCase();
      return raw;
    };

    const runAnalysis = async () => {
      const symbol = getSymbol();
      if (!symbol) {
        document.getElementById('analysis-status').innerHTML =
          '<span style="color:#EF5350;font-size:0.82rem;">⚠️ Please type a stock symbol first (e.g. RELIANCE)</span>';
        return;
      }
      const empty = document.getElementById('analysis-empty');
      if (empty) empty.style.display = 'none';
      const stopBtn = document.getElementById('btn-stop-analysis');
      if (stopBtn) stopBtn.style.display = selectedMode === 'live' ? 'inline-flex' : 'none';

      // Show signal chart button
      const sigBtn = document.getElementById('btn-signal-chart');
      if (sigBtn) {
        sigBtn.style.display = 'inline-flex';
        sigBtn.onclick = () => analysisSignalChart.load(symbol);
      }

      await stockAnalysis.run(symbol, selectedMode);
    };

    // Analyse button
    document.getElementById('btn-run-analysis')?.addEventListener('click', runAnalysis);

    // Enter key in the search field
    document.getElementById('analysis-symbol-input')?.addEventListener('keydown', e => {
      if (e.key === 'Enter') runAnalysis();
    });

    // Stop live button
    document.getElementById('btn-stop-analysis')?.addEventListener('click', () => {
      stockAnalysis.stop();
      document.getElementById('btn-stop-analysis').style.display = 'none';
      document.getElementById('analysis-status').innerHTML =
        '<span style="color:#78909C;font-size:0.82rem;">⏹ Live polling stopped</span>';
    });

    // Signal chart button (also bound dynamically in runAnalysis)
    document.getElementById('btn-signal-chart')?.addEventListener('click', () => {
      const symbol = getSymbol();
      if (symbol) analysisSignalChart.load(symbol);
    });

    // Analysis chart interval tabs
    document.querySelectorAll('#analysis-chart-interval-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#analysis-chart-interval-tabs .tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const symbol = getSymbol();
        if (symbol) analysisSignalChart.load(symbol, btn.dataset.interval);
      });
    });
  },

  // ── Settings ──
  bindSettings() {
    document.getElementById('btn-kite-login')?.addEventListener('click', () => {
      const apiKey = document.getElementById('set-api-key')?.value;
      if (apiKey) {
        kiteAPI.apiKey = apiKey;
        this.saveSettings(); // Save secret and key before redirecting
        // Use current window for redirect so we can capture the request_token on return
        window.location.href = kiteAPI.getLoginUrl();
      } else {
        document.getElementById('kite-status').innerHTML = '<span class="tag tag-bearish">Enter API Key first</span>';
      }
    });

    document.getElementById('btn-gen-session')?.addEventListener('click', async () => {
      const requestToken = document.getElementById('set-request-token')?.value;
      if (requestToken) {
        try {
          const apiSecret = document.getElementById('set-api-secret')?.value;
          const session = await kiteAPI.generateSession(requestToken, apiSecret);
          if (session.access_token) {
            document.getElementById('set-access-token').value = session.access_token;
            kiteAPI.accessToken = session.access_token;
            kiteAPI.configure(kiteAPI.apiKey, session.access_token, document.getElementById('set-backend-url')?.value || window.location.origin);
            this.saveSettings();
            document.getElementById('kite-status').innerHTML = '<span class="tag tag-bullish">Session generated ✓</span>';
          }
        } catch (e) {
          document.getElementById('kite-status').innerHTML = `<span class="tag tag-bearish">Error: ${e.message}</span>`;
        }
      }
    });

    document.getElementById('btn-connect')?.addEventListener('click', async () => {
      const apiKey = document.getElementById('set-api-key')?.value;
      const accessToken = document.getElementById('set-access-token')?.value;
      const backendUrl = document.getElementById('set-backend-url')?.value;

      if (!apiKey || !accessToken) {
        document.getElementById('kite-status').innerHTML = '<span class="tag tag-bearish">Enter API Key and Access Token</span>';
        return;
      }

      kiteAPI.configure(apiKey, accessToken, backendUrl);
      this.saveSettings();

      document.getElementById('kite-status').innerHTML = '<span class="tag tag-neutral">Connecting...</span>';

      const result = await kiteAPI.testConnection();
      if (result.success) {
        await this.apiFetch('/kite/auth', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: apiKey, access_token: accessToken })
        }).catch(e => console.warn('Backend Kite session persist failed:', e));
        document.getElementById('kite-status').innerHTML = '<span class="tag tag-bullish">Connected ✓</span>';
        await kiteAPI.getInstruments();
        await equityScreener.getFNOUniverse();
        this.populateStockDropdowns();
        this.updateMarketTicker();
      } else {
        document.getElementById('kite-status').innerHTML = `<span class="tag tag-bearish">Failed: ${result.error}</span>`;
      }
    });

    document.getElementById('btn-test-backend')?.addEventListener('click', async () => {
      const url = document.getElementById('set-backend-url')?.value || window.location.origin;
      try {
        const res = await this.apiFetch(`${url}/api/health`);
        if (res.ok) {
          const data = await res.json();
          document.getElementById('backend-status').innerHTML = '<span class="tag tag-bullish">Backend Online ✓</span>';
          // Auto-load cache stats if available
          if (data.cache) this._updateCacheStats(data.cache);
        } else {
          document.getElementById('backend-status').innerHTML = '<span class="tag tag-bearish">Backend Error</span>';
        }
      } catch (e) {
        document.getElementById('backend-status').innerHTML = '<span class="tag tag-bearish">Backend Offline</span>';
      }
    });

    // ── Cache Management ──
    document.getElementById('btn-refresh-cache')?.addEventListener('click', async () => {
      const url = document.getElementById('set-backend-url')?.value || window.location.origin;
      try {
        const res = await fetch(`${url}/api/cache/stats`, { credentials: 'include' });
        if (res.ok) {
          const stats = await res.json();
          this._updateCacheStats(stats);
          document.getElementById('cache-status').innerHTML = '<span class="tag tag-bullish">Stats refreshed ✓</span>';
        }
      } catch (e) {
        document.getElementById('cache-status').innerHTML = '<span class="tag tag-bearish">Failed to fetch stats</span>';
      }
    });

    document.getElementById('btn-clear-cache')?.addEventListener('click', async () => {
      if (!confirm('Clear all cached OHLCV data? Next scan will re-fetch from Kite API.')) return;
      const url = document.getElementById('set-backend-url')?.value || window.location.origin;
      try {
        const res = await fetch(`${url}/api/cache/clear`, { method: 'POST', credentials: 'include' });
        if (res.ok) {
          document.getElementById('cache-status').innerHTML = '<span class="tag tag-bullish">Cache cleared ✓</span>';
          this._updateCacheStats({ ohlcv_candles: 0, instruments: 0, unique_tokens: 0, db_size_mb: 0 });
        }
      } catch (e) {
        document.getElementById('cache-status').innerHTML = '<span class="tag tag-bearish">Failed to clear cache</span>';
      }
    });

    document.getElementById('btn-save-settings')?.addEventListener('click', () => this.saveSettings());
  },

  _updateCacheStats(stats) {
    const el = id => document.getElementById(id);
    if (el('cache-candles')) el('cache-candles').textContent = (stats.ohlcv_candles || 0).toLocaleString();
    if (el('cache-instruments')) el('cache-instruments').textContent = (stats.instruments || 0).toLocaleString();
    if (el('cache-tokens')) el('cache-tokens').textContent = (stats.unique_tokens || 0).toLocaleString();
    if (el('cache-size')) el('cache-size').textContent = (stats.db_size_mb || 0) + ' MB';
  },

  saveSettings() {
    const settings = {
      apiKey: document.getElementById('set-api-key')?.value || '',
      apiSecret: document.getElementById('set-api-secret')?.value || '',
      accessToken: document.getElementById('set-access-token')?.value || '',
      backendUrl: document.getElementById('set-backend-url')?.value || window.location.origin,
      scoreThreshold: document.getElementById('set-score-threshold')?.value || '70',
      maxIVP: document.getElementById('set-max-ivp')?.value || '50',
      minRR: document.getElementById('set-min-rr')?.value || '2',
      minOI: document.getElementById('set-min-oi')?.value || '5000',
      posSize: document.getElementById('set-pos-size')?.value || '20'
    };
    localStorage.setItem('ts_settings', JSON.stringify(settings));
  },

  loadSavedSettings() {
    try {
      const settings = JSON.parse(localStorage.getItem('ts_settings') || '{}');
      if (settings.apiKey) document.getElementById('set-api-key').value = settings.apiKey;
      if (settings.apiSecret) document.getElementById('set-api-secret').value = settings.apiSecret;
      if (settings.accessToken) document.getElementById('set-access-token').value = settings.accessToken;

      // Sanitize backendUrl: if it's localhost but we are NOT on localhost, ignore it
      let bUrl = settings.backendUrl;
      if (bUrl && bUrl.includes('localhost') && !window.location.hostname.includes('localhost')) {
          bUrl = ''; // Default to auto-detect
      }
      if (bUrl) document.getElementById('set-backend-url').value = bUrl;

      if (settings.scoreThreshold) document.getElementById('set-score-threshold').value = settings.scoreThreshold;
      if (settings.maxIVP) document.getElementById('set-max-ivp').value = settings.maxIVP;
      if (settings.minRR) document.getElementById('set-min-rr').value = settings.minRR;
      if (settings.minOI) document.getElementById('set-min-oi').value = settings.minOI;
      if (settings.posSize) document.getElementById('set-pos-size').value = settings.posSize;

      if (settings.apiKey && settings.accessToken) {
        kiteAPI.configure(settings.apiKey, settings.accessToken, settings.backendUrl);
      }
    } catch (e) { console.error('Error loading settings:', e); }

    // Check initial auth status
    this.updateAppAuthStatus(!!sessionStorage.getItem('ts_app_auth'), 'Checking...');
  },

  async hydrateKiteSessionFromBackend() {
    const MAX_RETRIES = 2;
    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      try {
        const resp = await this.apiFetch('/kite/auth/session');
        if (!resp.ok) {
          if (attempt < MAX_RETRIES) {
            console.warn(`Kite hydrate attempt ${attempt} failed (HTTP ${resp.status}), retrying in 3s...`);
            await new Promise(r => setTimeout(r, 3000));
            continue;
          }
          return;
        }

        const data = await resp.json();
        if (data.status !== 'ok' || !data.access_token) {
          if (attempt < MAX_RETRIES) {
            console.warn(`Kite hydrate attempt ${attempt}: no token, retrying in 3s...`);
            await new Promise(r => setTimeout(r, 3000));
            continue;
          }
          return;
        }

        const apiKeyEl = document.getElementById('set-api-key');
        const accessTokenEl = document.getElementById('set-access-token');
        const backendUrl = document.getElementById('set-backend-url')?.value || window.location.origin;
        const apiKey = data.api_key || kiteAPI.apiKey || apiKeyEl?.value || '';

        if (apiKeyEl && apiKey && !apiKeyEl.value) apiKeyEl.value = apiKey;
        if (accessTokenEl) accessTokenEl.value = data.access_token;

        kiteAPI.configure(apiKey, data.access_token, backendUrl);
        this.saveSettings();
        console.log('Kite access token hydrated from backend session');

        // Validate connection & load instruments so kiteAPI.connected = true
        // Without this, the screener/dashboard guard rejects all scan requests
        const testResult = await kiteAPI.testConnection();
        if (testResult.success) {
          console.log('✅ Kite session validated from backend hydration');
          const statusEl = document.getElementById('kite-status');
          if (statusEl) statusEl.innerHTML = '<span class="tag tag-bullish">Connected ✓</span>';
          await kiteAPI.getInstruments();
          await equityScreener.getFNOUniverse();
          this.populateStockDropdowns();
          return; // Success — stop retrying
        } else {
          console.warn(`Kite hydrate attempt ${attempt}: test failed: ${testResult.error}`);
          if (attempt < MAX_RETRIES) {
            console.warn(`Retrying hydration in 3s...`);
            await new Promise(r => setTimeout(r, 3000));
            continue;
          }
        }
      } catch (e) {
        console.warn(`Backend Kite session hydrate attempt ${attempt} failed:`, e);
        if (attempt < MAX_RETRIES) {
          await new Promise(r => setTimeout(r, 3000));
          continue;
        }
      }
    }
  },

  async syncSavedKiteSession() {
    const apiKey = kiteAPI?.apiKey || document.getElementById('set-api-key')?.value || '';
    const accessToken = kiteAPI?.accessToken || document.getElementById('set-access-token')?.value || '';
    if (!apiKey || !accessToken) return;

    try {
      const resp = await this.apiFetch('/kite/auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey, access_token: accessToken })
      });
      if (!resp.ok) {
        console.warn('Saved Kite session sync failed:', resp.status, await resp.text());
      }
    } catch (e) {
      console.warn('Saved Kite session sync failed:', e);
    }
  },

  // ── Auto Backend Connection (startup, 2 retries) ─────────────────────────
  async autoConnectBackend() {
    const MAX_RETRIES = 2;
    const statusEl = document.getElementById('backend-status');
    const url = document.getElementById('set-backend-url')?.value || window.location.origin;
    if (statusEl) statusEl.innerHTML = '<span class="tag tag-neutral">Connecting...</span>';

    for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
      try {
        const res = await this.apiFetch(`${url}/api/health`);
        if (res.ok) {
          const data = await res.json();
          if (statusEl) statusEl.innerHTML = '<span class="tag tag-bullish">Backend Online ✓</span>';
          if (data.cache) this._updateCacheStats(data.cache);
          return; // Success
        } else {
          if (attempt < MAX_RETRIES) {
            if (statusEl) statusEl.innerHTML = `<span class="tag tag-neutral">Retrying... (${attempt}/${MAX_RETRIES})</span>`;
            await new Promise(r => setTimeout(r, 2000));
          } else {
            if (statusEl) statusEl.innerHTML = '<span class="tag tag-bearish">Backend Error</span>';
          }
        }
      } catch (e) {
        if (attempt < MAX_RETRIES) {
          if (statusEl) statusEl.innerHTML = `<span class="tag tag-neutral">Retrying... (${attempt}/${MAX_RETRIES})</span>`;
          await new Promise(r => setTimeout(r, 2000));
        } else {
          if (statusEl) statusEl.innerHTML = '<span class="tag tag-bearish">Backend Offline</span>';
        }
      }
    }
  },

  // ── Market Breadth ──
  async loadMarketBreadth() {
    const container = document.getElementById('breadth-content');
    if (!container) return;
    container.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">⏳ Loading breadth data...</div>';
    try {
      const resp = await this.apiFetch('/api/market-breadth');
      if (!resp.ok) {
        container.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">Connect to Kite to load market breadth</div>';
        return;
      }
      const data = await resp.json();
      if (data.error) {
        container.innerHTML = `<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">${data.error}</div>`;
        return;
      }

      const total = data.advances + data.declines + data.unchanged;
      const advPct = total > 0 ? (data.advances / total * 100).toFixed(0) : 0;
      const decPct = total > 0 ? (data.declines / total * 100).toFixed(0) : 0;
      const ratio = data.advanceDeclineRatio;
      const indicator = data.breadthIndicator;

      const indColor = indicator.includes('BULLISH') ? '#26A69A' : indicator.includes('BEARISH') ? '#EF5350' : '#FFA726';
      const indBg = indicator.includes('BULLISH') ? 'rgba(38,166,154,0.12)' : indicator.includes('BEARISH') ? 'rgba(239,83,80,0.12)' : 'rgba(255,167,38,0.12)';

      // Top gainers/losers compact
      const gainersHtml = (data.topGainers || []).slice(0, 3).map(g =>
        `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:rgba(38,166,154,0.1);color:#26A69A;font-size:0.7rem;font-weight:600;margin:2px;">${g.symbol} +${g.change}%</span>`
      ).join('');
      const losersHtml = (data.topLosers || []).slice(0, 3).map(l =>
        `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:rgba(239,83,80,0.1);color:#EF5350;font-size:0.7rem;font-weight:600;margin:2px;">${l.symbol} ${l.change}%</span>`
      ).join('');

      container.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;margin-bottom:12px;">
          <div class="stat-card" style="padding:10px;text-align:center;">
            <span class="stat-label" style="font-size:0.65rem;">Advances</span>
            <span class="stat-value" style="font-size:1.1rem;color:#26A69A;">${data.advances}</span>
          </div>
          <div class="stat-card" style="padding:10px;text-align:center;">
            <span class="stat-label" style="font-size:0.65rem;">Declines</span>
            <span class="stat-value" style="font-size:1.1rem;color:#EF5350;">${data.declines}</span>
          </div>
          <div class="stat-card" style="padding:10px;text-align:center;">
            <span class="stat-label" style="font-size:0.65rem;">A/D Ratio</span>
            <span class="stat-value" style="font-size:1.1rem;color:${indColor};">${ratio >= 999 ? '∞' : ratio}</span>
          </div>
          <div class="stat-card" style="padding:10px;text-align:center;">
            <span class="stat-label" style="font-size:0.65rem;">Breadth</span>
            <span style="padding:4px 10px;border-radius:6px;background:${indBg};color:${indColor};font-weight:700;font-size:0.72rem;">${indicator}</span>
          </div>
        </div>
        <!-- A/D Bar -->
        <div style="height:8px;border-radius:4px;background:rgba(255,255,255,0.06);overflow:hidden;margin-bottom:12px;display:flex;">
          <div style="width:${advPct}%;background:#26A69A;transition:width 0.5s;"></div>
          <div style="width:${decPct}%;background:#EF5350;transition:width 0.5s;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:var(--text-muted);margin-bottom:8px;">
          <span>🟢 ${advPct}% advancing</span>
          <span>${data.unchanged} unchanged</span>
          <span>🔴 ${decPct}% declining</span>
        </div>
        ${gainersHtml || losersHtml ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;">${gainersHtml}${losersHtml}</div>` : ''}
        <div style="font-size:0.65rem;color:var(--text-muted);margin-top:8px;text-align:right;">Source: ${data.source || 'live'} · ${data.total} stocks</div>`;
    } catch (e) {
      container.innerHTML = `<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">⚠️ ${e.message}</div>`;
    }
  },

  // ── Earnings Calendar ──
  async loadEarningsCalendar() {
    const listEl = document.getElementById('earnings-list');
    const sourceEl = document.getElementById('earnings-source');
    if (!listEl) return;

    try {
      const resp = await this.apiFetch('/api/earnings-calendar');
      if (!resp.ok) {
        listEl.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">Earnings data unavailable</div>';
        return;
      }
      const data = await resp.json();
      const earnings = data.earnings || [];

      if (sourceEl) sourceEl.textContent = `${earnings.length} upcoming · ${data.source || ''}`;

      if (earnings.length === 0) {
        listEl.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">No upcoming earnings in next 30 days</div>';
        return;
      }

      // Group by date
      const grouped = {};
      earnings.forEach(e => {
        const d = e.date || 'Unknown';
        if (!grouped[d]) grouped[d] = [];
        grouped[d].push(e);
      });

      listEl.innerHTML = Object.entries(grouped).slice(0, 10).map(([date, items]) => {
        const firstItem = items[0];
        const daysText = firstItem.daysUntil === 0 ? '<span style="color:#EF5350;font-weight:700;">TODAY</span>'
          : firstItem.daysUntil === 1 ? '<span style="color:#FFA726;font-weight:700;">Tomorrow</span>'
          : firstItem.daysUntil <= 7 ? `<span style="color:#FFA726;">${firstItem.daysUntil}d</span>`
          : `<span style="color:var(--text-muted);">${firstItem.daysUntil}d</span>`;

        return `
          <div style="padding:8px 16px;border-bottom:1px solid rgba(255,255,255,0.04);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
              <span style="font-weight:700;font-size:0.82rem;color:var(--text-primary);">📅 ${date}</span>
              <span style="font-size:0.72rem;">${daysText}</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:4px;">
              ${items.map(e => `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:rgba(255,255,255,0.06);font-size:0.72rem;font-weight:600;color:var(--text-secondary);cursor:pointer;" onclick="app.navigateTo('analysis');document.getElementById('analysis-symbol-input').value='${e.symbol}'">${e.symbol}${e.isFnO ? '' : ' ⓘ'}</span>`).join('')}
            </div>
          </div>`;
      }).join('');
    } catch (e) {
      listEl.innerHTML = `<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">⚠️ ${e.message}</div>`;
    }
  },

  // ── Market Ticker ──
  async updateMarketTicker() {
    try {
      const res = await this.apiFetch('/api/market-pulse');
      if (!res || !res.ok) return;

      const data = await res.json();
      if (data.error) return;

      const setTicker = (ltpId, chgId, item) => {
        if (!item) return;
        const ltpEl = document.getElementById(ltpId);
        const chgEl = document.getElementById(chgId);
        if (ltpEl) ltpEl.textContent = (+item.ltp).toLocaleString('en-IN', { maximumFractionDigits: 2 });
        if (chgEl) {
          chgEl.textContent = `${item.change_pct >= 0 ? '+' : ''}${item.change_pct.toFixed(2)}%`;
          chgEl.className = `change ${item.change_pct >= 0 ? 'up' : 'down'}`;
        }
      };

      setTicker('nifty-ltp', 'nifty-chg', data.nifty);
      setTicker('bnf-ltp',   'bnf-chg',   data.banknifty);

      // VIX gauge: green < 15, yellow 15-25, red > 25
      const vix = data.vix;
      if (vix) {
        const ltpEl = document.getElementById('vix-ltp');
        const gaugeEl = document.getElementById('vix-gauge');
        if (ltpEl) ltpEl.textContent = (+vix.ltp).toFixed(2);
        if (gaugeEl) {
          const v = +vix.ltp;
          if (v < 15)      { gaugeEl.textContent = 'LOW';  gaugeEl.style.background = 'rgba(38,166,154,0.2)';  gaugeEl.style.color = '#26A69A'; }
          else if (v < 25) { gaugeEl.textContent = 'MED';  gaugeEl.style.background = 'rgba(255,167,38,0.2)';  gaugeEl.style.color = '#FFA726'; }
          else             { gaugeEl.textContent = 'HIGH'; gaugeEl.style.background = 'rgba(239,83,80,0.2)';   gaugeEl.style.color = '#EF5350'; }
        }
      }

      // Dim ticker if stale (after-hours cached data)
      const ticker = document.getElementById('market-ticker');
      if (ticker) ticker.style.opacity = data.stale ? '0.6' : '1';

    } catch (e) {
      // Silently fail — ticker is non-critical
    }

    // Poll every 15s during market hours (Mon-Fri 9:00-15:30 IST), 60s otherwise
    clearTimeout(this._tickerTimer);
    const now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const day = now.getDay(), hr = now.getHours(), mn = now.getMinutes();
    const marketOpen = day >= 1 && day <= 5 && (hr > 9 || (hr === 9 && mn >= 0)) && (hr < 15 || (hr === 15 && mn <= 30));
    this._tickerTimer = setTimeout(() => this.updateMarketTicker(), marketOpen ? 15000 : 60000);
  },

  // ── Watchlist ──
  bindWatchlist() {
    if (!window.watchlist) return;
    watchlist.init();

    // Datalist is populated centrally by populateStockDropdowns()

    // Add button
    const addBtn = document.getElementById('btn-watchlist-add');
    const addInput = document.getElementById('watchlist-add-input');
    if (addBtn && addInput) {
      const doAdd = () => {
        const sym = addInput.value.trim().toUpperCase();
        if (sym) {
          watchlist.add(sym);
          addInput.value = '';
          watchlist.render();
        }
      };
      addBtn.addEventListener('click', doAdd);
      addInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doAdd();
      });
    }
  },

  // ── News Feed ──
  bindNewsFeed() {
    const filterBtn = document.getElementById('btn-news-filter');
    const filterInput = document.getElementById('news-symbol-filter');
    if (filterBtn && filterInput) {
      const doFilter = () => {
        const sym = filterInput.value.trim().toUpperCase();
        newsFeed.load(sym);
      };
      filterBtn.addEventListener('click', doFilter);
      filterInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doFilter();
      });
    }
  },

  // ── Kite Connection Events ──
  bindConnectionEvents() {
    document.addEventListener('kite-connection', (e) => {
      const connected = e.detail.connected;
      const dot = document.getElementById('status-dot');
      const text = document.getElementById('status-text');
      if (connected) {
        dot?.classList.add('connected');
        if (text) text.textContent = 'Connected';
        this.updateMarketTicker();
        // Refresh F&O universe from Kite instruments and re-populate all dropdowns
        equityScreener.refreshUniverse().then(() => {
          this.populateStockDropdowns();
          console.log('🔄 Dropdowns refreshed with dynamic F&O universe');
        }).catch(e => console.warn('Dynamic universe refresh failed:', e.message));
      } else {
        dot?.classList.remove('connected');
        if (text) text.textContent = 'Disconnected';
      }
    });
  },
  // ── Auth status helper ──

  // ── FNO Sessions Analyzer ──
  bindFNOSessions() {
    this.updateSessionStatus();
    setInterval(() => this.updateSessionStatus(), 5000);
    document.getElementById('fno-session-symbol')?.addEventListener('change', () => {
      document.getElementById('fno-analysis-results').innerHTML = '<div class="empty-state"><div class="empty-icon">⏳</div><h4>Ready for Analysis</h4><p>Click "Quick Analysis" to analyze this stock for the current market session</p></div>';
    });
    document.getElementById('btn-fno-quick-analyze')?.addEventListener('click', () => {
      const symbol = document.getElementById('fno-session-symbol')?.value;
      if (!symbol) { alert('Please select a stock first'); return; }
      this.analyzeFNOStock(symbol);
    });
    document.getElementById('btn-session-analyze')?.addEventListener('click', () => {
      this.runFNOSessionAnalysis();
    });
    // Dropdown is populated centrally by populateStockDropdowns()
  },

  updateSessionStatus() {
    const session = fnoSessionAnalyzer.getCurrentSession();
    const sessionLabel = { premarket: '🌅 Premarket (3:30 PM–9:00 AM)', opening: '🔔 Opening Bell (9:00–9:15 AM)', live: '⚡ Live Trading (9:15 AM–3:30 PM)', closed: '🔒 Market Closed' };
    const sessionDesc = { premarket: 'Pre-market setup analysis. Review gaps, FII flows, OI changes, and overnight news.', opening: 'Critical opening 15 minutes. Watch first candle momentum and volume buildup.', live: 'Live trading. Real-time price action, PCR swings, and IV dynamics.', closed: 'Market is currently closed. Next session starts at 3:30 PM.' };
    const el = document.getElementById('session-status-container');
    if (el) {
      const labelEl = document.getElementById('current-session-label');
      const descEl = document.getElementById('session-description');
      if (labelEl) labelEl.textContent = sessionLabel[session] || 'Unknown Session';
      if (descEl) descEl.textContent = sessionDesc[session] || '';
    }
    const timeEl = document.getElementById('current-session-time');
    if (timeEl) {
      const now = new Date();
      const istTime = new Date(now.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }));
      timeEl.textContent = istTime.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
  },

  async analyzeFNOStock(symbol) {
    const resultsContainer = document.getElementById('fno-analysis-results');
    resultsContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">⏳</div><p>Analyzing...</p></div>';
    try {
      let stockData = this.stockData.find(s => s.symbol === symbol);
      if (!stockData) {
        if (!kiteAPI.connected) { resultsContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><h4>API Not Connected</h4><p>Please configure Kite API in Settings first.</p></div>'; return; }
        const fnoList = equityScreener.getFNOUniverseSync();
        const stock = fnoList.find(s => s.symbol === symbol);
        if (stock) { 
          const snapshotResp = await window.kiteAPI.getBatchSnapshots([symbol]);
          const snapshot = snapshotResp.snapshots?.[symbol] || null;
          stockData = await equityScreener.fetchStockData(stock, snapshot);
        }
      }
      if (!stockData) { resultsContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><h4>No Data</h4><p>Could not retrieve data for this stock.</p></div>'; return; }
      const analysis = await fnoSessionAnalyzer.analyzeStockForSession(stockData);
      const html = fnoSessionAnalyzer.renderAnalysisReport(analysis);
      resultsContainer.innerHTML = html;

      // Auto-capture FNO single analysis to Reco Tracker
      if (window.recoTracker && analysis && analysis.session !== 'closed') {
        recoTracker.autoCapture(
          [{ symbol, ...analysis, ltp: stockData.ltp || stockData.closes?.[stockData.closes.length - 1] }],
          'fno_session',
          analysis.session
        );
      }
    } catch (e) {
      resultsContainer.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div><h4>Analysis Failed</h4><p>${e.message}</p></div>`;
    }
  },

  async runFNOSessionAnalysis() {
    const btn = document.getElementById('btn-session-analyze');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Analyzing...'; }
    try {
      // Ensure we have scanned data first
      if (equityScreener.stocks.length === 0) {
        await equityScreener.scan('equity');
      }
      
      // We want to analyze ALL FNO stocks, not just the filtered top 20
      const allStocks = equityScreener.stocks;
      const analysisResults = [];
      
      for (let i = 0; i < allStocks.length; i++) {
        const analysis = await fnoSessionAnalyzer.analyzeStockForSession(allStocks[i]);
        if (analysis && analysis.session !== 'closed') {
          analysisResults.push({ symbol: allStocks[i].symbol, ...analysis });
        }
      }
      
      // Sort by highest score first to show best opportunities
      analysisResults.sort((a, b) => b.score - a.score);

      // Auto-capture all FNO session results to Reco Tracker
      if (window.recoTracker && analysisResults.length > 0) {
        const session = fnoSessionAnalyzer.getCurrentSession();
        const fnoRecos = analysisResults.filter(a => a.score >= 50).map(a => ({
          ...a,
          ltp: equityScreener.stocks.find(s => s.symbol === a.symbol)?.ltp ||
               equityScreener.stocks.find(s => s.symbol === a.symbol)?.closes?.slice(-1)[0] || 0
        }));
        recoTracker.autoCapture(fnoRecos, 'fno_session', session);
      }

      // Relaxed threshold from strict 70 to 50, to align with analyzer's internal LIVE thresholds
      const bullish = analysisResults.filter(a => a.direction === 'BULLISH' && a.score >= 50);
      const bearish = analysisResults.filter(a => a.direction === 'BEARISH' && a.score >= 50);
      const resultsContainer = document.getElementById('fno-analysis-results');
      let html = '<div style="margin-bottom:20px;"><div class="grid-2" style="margin-bottom:16px;"><div class="stat-card"><div class="stat-label">Bullish Signals</div><div class="stat-value" style="color:var(--green);">' + bullish.length + '</div></div><div class="stat-card"><div class="stat-label">Bearish Signals</div><div class="stat-value" style="color:var(--red);">' + bearish.length + '</div></div></div></div>';
      if (bullish.length > 0) {
        html += '<h4 style="margin-bottom:12px;color:var(--green);">📈 Bullish Opportunities</h4>';
        html += bullish.slice(0, 5).map(a => '<div class="reco-card buy" style="margin-bottom:12px;cursor:pointer;" onclick="app.analyzeFNOStock(\'' + a.symbol + '\')"><div style="display:flex;justify-content:space-between;align-items:center;"><div><strong>' + a.symbol + '</strong></div><div class="score-badge score-high">' + Math.round(a.score) + '</div></div><p style="font-size:0.78rem;color:var(--text-secondary);margin:8px 0 0;">Strategy: ' + (a.strategy?.trade || 'N/A') + '</p></div>').join('');
      }
      if (bearish.length > 0) {
        html += '<h4 style="margin-bottom:12px;margin-top:20px;color:var(--red);">📉 Bearish Opportunities</h4>';
        html += bearish.slice(0, 5).map(a => '<div class="reco-card sell" style="margin-bottom:12px;cursor:pointer;" onclick="app.analyzeFNOStock(\'' + a.symbol + '\')"><div style="display:flex;justify-content:space-between;align-items:center;"><div><strong>' + a.symbol + '</strong></div><div class="score-badge score-high">' + Math.round(a.score) + '</div></div><p style="font-size:0.78rem;color:var(--text-secondary);margin:8px 0 0;">Strategy: ' + (a.strategy?.trade || 'N/A') + '</p></div>').join('');
      }
      if (bullish.length === 0 && bearish.length === 0) {
        html += '<div class="empty-state"><p>No strong signals in current session. Market may be neutral.</p></div>';
      }
      resultsContainer.innerHTML = html;
    } catch (e) {
      const resultsContainer = document.getElementById('fno-analysis-results');
      resultsContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠️</div><h4>Analysis Failed</h4><p>' + e.message + '</p></div>';
    }
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Analyze Now'; }
  },

  bindRecoTracker() {
    // RecoTracker is initialized lazily when user navigates to its page.
    // The auto-capture hooks are already wired into generateRecommendations()
    // and analyzeFNOStock() / runFNOSessionAnalysis().
  },

  bindSmcDashboard() {
    // Symbol select
    const symSel = document.getElementById('smc-symbol-select');
    if (symSel) {
      symSel.addEventListener('change', () => {
        if (window.smcDashboard) smcDashboard.load(symSel.value, smcDashboard._interval);
      });
    }

    // Interval tabs
    document.querySelectorAll('#smc-interval-tabs .tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('#smc-interval-tabs .tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        if (window.smcDashboard) smcDashboard.load(smcDashboard._symbol, btn.dataset.interval);
      });
    });

    // Simulate buttons
    document.getElementById('btn-smc-bull')?.addEventListener('click', () => window.smcDashboard?.simulateBull());
    document.getElementById('btn-smc-bear')?.addEventListener('click', () => window.smcDashboard?.simulateBear());
    document.getElementById('btn-smc-exit')?.addEventListener('click', () => window.smcDashboard?.simulateExit());

    // TAG ENTRY button (dynamically rendered inside reco card — use delegation)
    document.getElementById('page-smc-dashboard')?.addEventListener('click', (e) => {
      const btn = e.target.closest('#btn-smc-tag-entry');
      if (!btn || !window.smcDashboard) return;
      smcDashboard._addTag({
        time: Date.now(),
        type: 'ENTRY',
        title: `Manual TAG ENTRY — ${btn.dataset.direction}`,
        description: `${smcDashboard._symbol}: ${btn.dataset.direction} entry tagged manually.`,
        entryPrice: parseFloat(btn.dataset.entry) || 0,
        sl:         parseFloat(btn.dataset.sl)    || 0,
        target:     parseFloat(btn.dataset.target) || 0,
      });
    });

    // Clear tags
    document.getElementById('btn-smc-clear-tags')?.addEventListener('click', () => {
      if (window.smcDashboard) {
        smcDashboard._tags = [];
        smcDashboard._renderTags();
      }
    });

    // Populate date select with last 5 trading sessions
    this._populateSmcDateSelect();
  },

  _populateSmcDateSelect() {
    const sel = document.getElementById('smc-date-select');
    if (!sel) return;
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);
    const sessions = [];
    let cursor = new Date(ist);
    while (sessions.length < 5) {
      const day = cursor.getDay();
      if (day !== 0 && day !== 6) {
        sessions.push(new Date(cursor));
      }
      cursor.setDate(cursor.getDate() - 1);
    }
    sel.innerHTML = sessions.map((d, i) => {
      const dd = String(d.getDate()).padStart(2, '0');
      const mon = months[d.getMonth()];
      const label = i === 0 ? `Today (${dd} ${mon})` : `${dd} ${mon}`;
      const val = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${dd}`;
      return `<option value="${val}">${label}</option>`;
    }).join('');
  },

  updateAppAuthStatus(authed, status) {
    // For future use: update a global auth badge if present
    const el = document.getElementById('kite-status');
    if (el && status) {
      // Only update if not already showing a success state
      if (!el.innerHTML.includes('✅') && !el.innerHTML.includes('Connected')) {
        el.innerHTML = `<span class="tag tag-neutral">${status}</span>`;
      }
    }
  },
};

// ── Boot ──
document.addEventListener('DOMContentLoaded', () => app.init());
