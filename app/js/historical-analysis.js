/**
 * TradeSignal — Historical Predictive Analytics Controller
 * Modular controller for the Historical Analysis Dashboard
 */

class HistoricalAnalysis {
  constructor() {
    this.symbol = '';
    this.days = 90;
    this.interval = 'day';
  }

  init() {
    console.log('Historical Analysis module initialized.');
    this.populateDropdown();
    this.bindDropdownEvents();
    this.bindEvents();
    if (typeof window.haFHFetchHistory === 'function') {
      window.haFHFetchHistory();
    }
  }

  populateDropdown() {
    // ── Static index entries (always pinned at top) ──────────────────
    const indices = [
      { symbol: 'NIFTY 50', label: 'NIFTY 50' },
      { symbol: 'NIFTY BANK', label: 'NIFTY BANK' },
      { symbol: 'NIFTY FIN SERVICE', label: 'NIFTY FIN SERVICE' },
      { symbol: 'INDIA VIX', label: 'INDIA VIX' },
    ];

    // ── F&O stocks — sorted A→Z ──────────────────────────────────────
    let fnoStocks = [];
    if (window.equityScreener) {
      fnoStocks = equityScreener.getFNOUniverseSync()
        .map(s => ({ symbol: s.symbol, label: s.symbol }))
        .sort((a, b) => a.symbol.localeCompare(b.symbol));
    } else {
      const fallback = ['DMART', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN'];
      fnoStocks = fallback.sort().map(s => ({ symbol: s, label: s }));
    }

    this._allGroups = [
      { group: '📊 Indices', items: indices },
      { group: '📈 F&O Stocks', items: fnoStocks },
    ];

    // If dropdown events have already been bound, we can trigger a re-render
    if (typeof this._renderList === 'function') {
      const searchInput = document.getElementById('ha-symbol-search');
      this._renderList(searchInput ? searchInput.value : '');
    }
  }

  bindDropdownEvents() {
    const listEl = document.getElementById('ha-symbol-list');
    const hiddenInput = document.getElementById('ha-symbol');
    const searchInput = document.getElementById('ha-symbol-search');
    const dropdownEl = document.getElementById('ha-symbol-dropdown');
    if (!listEl || !hiddenInput || !searchInput || !dropdownEl) return;

    // ── Render filtered list into the dropdown panel ─────────────────
    const renderList = (query) => {
      const q = query.trim().toUpperCase();
      listEl.innerHTML = '';
      let totalVisible = 0;

      if (!this._allGroups) return;

      this._allGroups.forEach(({ group, items }) => {
        const filtered = q ? items.filter(it => it.symbol.includes(q)) : items;
        if (!filtered.length) return;
        totalVisible += filtered.length;

        // Group header row
        const hdr = document.createElement('div');
        hdr.textContent = group;
        hdr.style.cssText = [
          'padding:6px 12px 4px',
          'font-size:0.68rem',
          'font-weight:700',
          'color:var(--text-muted)',
          'text-transform:uppercase',
          'letter-spacing:0.06em',
          'border-top:1px solid rgba(255,255,255,0.06)',
          'margin-top:2px',
          'user-select:none',
        ].join(';');
        listEl.appendChild(hdr);

        filtered.forEach(it => {
          // Highlight matched portion
          let displayHTML = it.label;
          if (q) {
            const idx = it.label.indexOf(q);
            if (idx !== -1) {
              displayHTML =
                it.label.slice(0, idx) +
                `<span style="color:var(--primary);font-weight:800;">${it.label.slice(idx, idx + q.length)}</span>` +
                it.label.slice(idx + q.length);
            }
          }

          const row = document.createElement('div');
          row.innerHTML = displayHTML;
          row.style.cssText = [
            'padding:8px 14px',
            'cursor:pointer',
            'font-size:0.84rem',
            'font-weight:600',
            'color:var(--text-primary)',
            'transition:background 0.1s',
          ].join(';');
          row.addEventListener('mouseenter', () => { row.style.background = 'rgba(255,255,255,0.08)'; });
          row.addEventListener('mouseleave', () => { row.style.background = ''; });
          row.addEventListener('mousedown', (e) => {
            e.preventDefault(); // keep focus on input briefly so blur doesn't fire first
            hiddenInput.value = it.symbol;
            searchInput.value = it.symbol;
            dropdownEl.style.display = 'none';
          });
          listEl.appendChild(row);
        });
      });

      if (totalVisible === 0) {
        const empty = document.createElement('div');
        empty.textContent = 'No matching stock found';
        empty.style.cssText = 'padding:16px; text-align:center; color:var(--text-muted); font-size:0.8rem;';
        listEl.appendChild(empty);
      }
    };

    // Save reference for dynamically updating list contents
    this._renderList = renderList;

    // ── Event wiring ─────────────────────────────────────────────────
    searchInput.addEventListener('focus', () => {
      renderList(searchInput.value);
      dropdownEl.style.display = 'block';
    });

    searchInput.addEventListener('input', () => {
      hiddenInput.value = ''; // clear confirmed selection when user edits
      renderList(searchInput.value);
      dropdownEl.style.display = 'block';
    });

    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        dropdownEl.style.display = 'none';
        searchInput.blur();
      }
    });

    searchInput.addEventListener('blur', () => {
      // Delay so mousedown on a row fires before we hide
      setTimeout(() => { dropdownEl.style.display = 'none'; }, 160);
    });

    document.addEventListener('click', (e) => {
      if (!searchInput.contains(e.target) && !dropdownEl.contains(e.target)) {
        dropdownEl.style.display = 'none';
      }
    });

    // Pre-render so dropdown is ready when user first focuses
    renderList('');
  }

  bindEvents() {
    // Run Scan Button
    const runBtn = document.getElementById('btn-ha-run-scan');
    if (runBtn) {
      runBtn.addEventListener('click', () => this.runScan());
    }

    // Interval tabs
    const tabBtns = document.querySelectorAll('#ha-chart-interval-tabs .tab-btn');
    tabBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        tabBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        this.interval = btn.dataset.interval;
        console.log('Interval changed to:', this.interval);
        // In the future, this will refresh the chart with the selected interval
      });
    });
  }

  runScan() {
    const symbolSelect = document.getElementById('ha-symbol');
    const daysSelect = document.getElementById('ha-days');
    const emptyState = document.getElementById('ha-empty-state');
    const resultPanel = document.getElementById('ha-result-panel');

    const symbol = symbolSelect?.value;
    const days = parseInt(daysSelect?.value || '90');

    if (!symbol) {
      alert('Please select a valid instrument first.');
      return;
    }

    this.symbol = symbol;
    this.days = days;

    // Show loading state
    if (emptyState) {
      emptyState.innerHTML = `
        <div class="empty-state">
          <div class="loading-spinner"></div>
          <h4 style="font-family:var(--font-display); font-weight:700; margin-top:16px;">Analyzing Historical Volatility...</h4>
          <p>Running ${days}-day retrospective backtest, parsing climax nodes, and computing gap profiles for ${symbol}...</p>
        </div>
      `;
      emptyState.style.display = 'flex';
    }
    if (resultPanel) {
      resultPanel.style.display = 'none';
    }

    // Fetch live backend metrics and multi-timeframe in parallel
    Promise.all([
      fetch(`/api/historical-analytics?symbol=${encodeURIComponent(symbol)}&days=${days}`).then(res => {
        if (!res.ok) throw new Error('API fetch failed');
        return res.json();
      }),
      fetch('/api/multi-timeframe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ symbol, intervals: ['15minute', '60minute', 'day', 'week'] })
      }).then(res => res.ok ? res.json() : null).catch(() => null)
    ])
      .then(([data, mtfData]) => {
        if (emptyState) emptyState.style.display = 'none';
        if (resultPanel) resultPanel.style.display = 'block';

        // Restore emptyState default html for next resets
        if (emptyState) {
          emptyState.innerHTML = `
            <div style="font-size:4rem; margin-bottom:16px;">🔍</div>
            <h4 style="font-family:var(--font-display); font-weight:700; color:var(--text-primary); font-size:1.4rem;">Select an Instrument to Begin</h4>
            <p style="color:var(--text-secondary); max-width:480px; margin:8px auto 0; font-size:0.88rem;">Select an index or F&O stock and select your historical window. We will perform a deep multi-timeframe backtest, analyze opening gap behaviors, and extract volume walls.</p>
          `;
        }

        this.renderResults(data, mtfData);
      })
      .catch(err => {
        console.error('Historical scan failed:', err);
        if (emptyState) {
          emptyState.innerHTML = `
            <div style="font-size:4rem; margin-bottom:16px;">⚠️</div>
            <h4 style="font-family:var(--font-display); font-weight:700; color:var(--red); font-size:1.4rem;">Scan Integration Failed</h4>
            <p style="color:var(--text-secondary); max-width:480px; margin:8px auto 0; font-size:0.88rem;">Failed to fetch live quantitative telemetry. Please check that server.py is running and you are connected to the network.</p>
          `;
          emptyState.style.display = 'flex';
        }
      });
  }

  renderResults(data, mtfData) {
    if (!data) return;

    // 1. Populate summary stats
    document.getElementById('ha-stat-symbol').textContent = data.symbol;

    // Resolve Last Session return
    if (data.rows && data.rows.length > 0) {
      const lastSessionVal = data.rows[0].change;
      const isSessionUp = lastSessionVal >= 0;
      const sessionChangeEl = document.getElementById('ha-stat-change');
      if (sessionChangeEl) {
        sessionChangeEl.textContent = `${isSessionUp ? '+' : ''}${lastSessionVal.toFixed(2)}% (Last Session)`;
        sessionChangeEl.style.color = isSessionUp ? 'var(--green)' : 'var(--red)';
      }
    }

    // Core predictive scoring
    const scoreVal = data.score;
    const dirStr = data.direction;
    const tagClass = dirStr === 'BULLISH' ? 'tag-bullish' : dirStr === 'BEARISH' ? 'tag-bearish' : 'tag-neutral';

    const scoreEl = document.getElementById('ha-stat-score');
    if (scoreEl) {
      scoreEl.textContent = scoreVal;
      scoreEl.className = 'stat-value ' + (scoreVal >= 70 ? 'score-high' : scoreVal >= 50 ? 'score-medium' : 'score-low');
    }

    const dirEl = document.getElementById('ha-stat-direction');
    if (dirEl) {
      dirEl.textContent = dirStr;
      dirEl.className = `tag ${tagClass}`;
    }

    // Bind Upcoming Session Prediction
    const pred = data.prediction_telemetry;
    if (pred) {
      const biasEl = document.getElementById('ha-stat-predict-bias');
      const confEl = document.getElementById('ha-stat-predict-confidence');
      const detailsEl = document.getElementById('ha-stat-predict-details');

      if (biasEl) {
        biasEl.textContent = pred.consensus_bias;
        biasEl.style.color = pred.consensus_bias === 'BULLISH' ? 'var(--green)' : pred.consensus_bias === 'BEARISH' ? 'var(--red)' : 'var(--text-secondary)';
      }

      if (confEl) {
        confEl.textContent = `${pred.confidence_score}% Confidence`;
        confEl.className = `tag ${pred.consensus_bias === 'BULLISH' ? 'tag-bullish' : pred.consensus_bias === 'BEARISH' ? 'tag-bearish' : 'tag-neutral'}`;
      }

      if (detailsEl) {
        const markov = pred.metrics.markov_transition_probabilities;
        const action = pred.metrics.climax_wall_action;
        detailsEl.textContent = `Markov: ${markov.next_up_prob}% Up | Climax: ${action || 'NEUTRAL'}`;
      }
    }

    // 2. Volatility CHOP
    const chopVal = data.chop;
    document.getElementById('ha-stat-chop').textContent = chopVal.toFixed(1);
    const chopLabel = document.getElementById('ha-stat-chop-label');
    if (chopLabel) {
      if (chopVal > 61.8) {
        chopLabel.textContent = 'EXTREME COILING / CONSOLIDATION (CHOP > 61.8)';
        chopLabel.style.background = 'rgba(255, 152, 0, 0.1)';
        chopLabel.style.color = 'var(--orange)';
      } else if (chopVal < 38.2) {
        chopLabel.textContent = 'STRONG TREND EXPANSION (CHOP < 38.2)';
        chopLabel.style.background = 'rgba(156, 39, 176, 0.1)';
        chopLabel.style.color = 'var(--purple)';
      } else {
        chopLabel.textContent = 'NEUTRAL / CHOP STATE (38.2 - 61.8)';
        chopLabel.style.background = 'rgba(255, 255, 255, 0.1)';
        chopLabel.style.color = 'var(--text-secondary)';
      }
    }

    // EMA 50/200 Crossover status & Strength
    const emaCross = data.ema_crossover;
    const emaCrossEl = document.getElementById('ha-stat-ema-cross');
    const emaCrossLabel = document.getElementById('ha-stat-ema-cross-label');

    if (emaCross && emaCrossEl && emaCrossLabel) {
      if (emaCross.status === 'BULLISH_APPROACHING') {
        emaCrossEl.textContent = 'Golden Cross Approaching';
        emaCrossEl.style.color = 'var(--green)';
        emaCrossLabel.textContent = `${emaCross.details} | ${emaCross.strength_label}`;
        emaCrossLabel.style.background = 'rgba(76, 175, 80, 0.1)';
        emaCrossLabel.style.color = 'var(--green)';
      } else if (emaCross.status === 'BEARISH_APPROACHING') {
        emaCrossEl.textContent = 'Death Cross Approaching';
        emaCrossEl.style.color = 'var(--red)';
        emaCrossLabel.textContent = `${emaCross.details} | ${emaCross.strength_label}`;
        emaCrossLabel.style.background = 'rgba(244, 67, 54, 0.1)';
        emaCrossLabel.style.color = 'var(--red)';
      } else if (emaCross.status === 'BULLISH_ACTIVE') {
        emaCrossEl.textContent = 'Golden Cross Active';
        emaCrossEl.style.color = 'var(--green)';
        emaCrossLabel.textContent = `${emaCross.details} | ${emaCross.strength_label}`;
        emaCrossLabel.style.background = 'rgba(76, 175, 80, 0.2)';
        emaCrossLabel.style.color = 'var(--green)';
      } else if (emaCross.status === 'BEARISH_ACTIVE') {
        emaCrossEl.textContent = 'Death Cross Active';
        emaCrossEl.style.color = 'var(--red)';
        emaCrossLabel.textContent = `${emaCross.details} | ${emaCross.strength_label}`;
        emaCrossLabel.style.background = 'rgba(244, 67, 54, 0.2)';
        emaCrossLabel.style.color = 'var(--red)';
      } else {
        emaCrossEl.textContent = 'Neutral Separation';
        emaCrossEl.style.color = 'var(--text-primary)';
        emaCrossLabel.textContent = `${emaCross.details} | ${emaCross.strength_label}`;
        emaCrossLabel.style.background = 'rgba(255, 255, 255, 0.1)';
        emaCrossLabel.style.color = 'var(--text-secondary)';
      }
    }

    // 1h & 15m Structure Alignment calculation
    const structEl = document.getElementById('ha-stat-struct');
    const structLabel = document.getElementById('ha-stat-struct-label');
    if (structEl && structLabel) {
      if (!mtfData) {
        structEl.textContent = 'No MTF Data';
        structEl.style.color = 'var(--text-secondary)';
        structLabel.textContent = 'Failed to load multi-timeframe telemetry';
        structLabel.style.background = 'rgba(255,255,255,0.06)';
        structLabel.style.color = 'var(--text-secondary)';
      } else {
        const getSingleTimeframeStructure = (ohlcv) => {
          if (!ohlcv || ohlcv.length < 25) return { status: 'CHOPPY', reason: 'Insufficient data' };
          const highs = ohlcv.map(d => d.high);
          const lows = ohlcv.map(d => d.low);
          const closes = ohlcv.map(d => d.close);
          const chop = TI.computeCHOP(highs, lows, closes, 14);
          if (chop > 55) return { status: 'CHOPPY', reason: `High CHOP Index (${chop.toFixed(0)})` };

          const ema9 = TI.computeEMA(closes, 9);
          const ema21 = TI.computeEMA(closes, 21);
          let crossCount = 0;
          for (let i = closes.length - 20; i < closes.length; i++) {
            if (i > 0 && ema9[i] !== null && ema21[i] !== null && ema9[i - 1] !== null && ema21[i - 1] !== null) {
              if ((ema9[i - 1] - ema21[i - 1]) * (ema9[i] - ema21[i]) < 0) crossCount++;
            }
          }
          if (crossCount >= 2) return { status: 'CHOPPY', reason: `EMA whipsaw (${crossCount} crosses)` };

          const pivots = TI.computePivots(highs, lows, 3);
          const validPivots = pivots.filter(p => p !== null);
          if (validPivots.length >= 4) {
            const lastPivots = validPivots.slice(-4);
            const phs = lastPivots.filter(p => p.type === 'PH');
            const pls = lastPivots.filter(p => p.type === 'PL');
            if (phs.length >= 2 && pls.length >= 2) {
              const ph1 = phs[phs.length - 2].price;
              const ph2 = phs[phs.length - 1].price;
              const pl1 = pls[pls.length - 2].price;
              const pl2 = pls[pls.length - 1].price;
              const isBull = (ph2 > ph1) && (pl2 > pl1);
              const isBear = (ph2 < ph1) && (pl2 < pl1);
              const lastE9 = ema9[closes.length - 1];
              const lastE21 = ema21[closes.length - 1];
              if (isBull && lastE9 > lastE21) return { status: 'BULLISH', reason: 'HH/HL confirmed' };
              if (isBear && lastE9 < lastE21) return { status: 'BEARISH', reason: 'LH/LL confirmed' };
            }
          }
          return { status: 'CHOPPY', reason: 'No clear HH/HL or LH/LL structure' };
        };

        const ohlcv15 = mtfData['ohlcv_15minute'] || [];
        const ohlcv60 = mtfData['ohlcv_60minute'] || [];
        const s15 = getSingleTimeframeStructure(ohlcv15);
        const s60 = getSingleTimeframeStructure(ohlcv60);

        if (s15.status === 'CHOPPY' || s60.status === 'CHOPPY') {
          structEl.textContent = 'No Trade';
          structEl.style.color = 'var(--orange)';
          structLabel.textContent = `Choppy: [15m: ${s15.reason}] [1h: ${s60.reason}]`;
          structLabel.style.background = 'rgba(255, 152, 0, 0.1)';
          structLabel.style.color = 'var(--orange)';
        } else if (s15.status === s60.status) {
          structEl.textContent = `${s15.status} ALIGNED`;
          structEl.style.color = s15.status === 'BULLISH' ? 'var(--green)' : 'var(--red)';
          structLabel.textContent = `1h & 15m structures fully align ${s15.status}`;
          structLabel.style.background = s15.status === 'BULLISH' ? 'rgba(76, 175, 80, 0.15)' : 'rgba(244, 67, 54, 0.15)';
          structLabel.style.color = s15.status === 'BULLISH' ? 'var(--green)' : 'var(--red)';
        } else {
          structEl.textContent = 'No Trade';
          structEl.style.color = 'var(--text-secondary)';
          structLabel.textContent = `Divergent Structure: 15m is ${s15.status} but 1h is ${s60.status}`;
          structLabel.style.background = 'rgba(255,255,255,0.08)';
          structLabel.style.color = 'var(--text-secondary)';
        }
      }
    }

    // 3. Multi-TF status
    const dtTrend = data.daily_trend;
    const htTrend = data.hourly_trend;
    const mtTrend = data.m15_trend;

    document.getElementById('ha-tf-daily').textContent = dtTrend;
    document.getElementById('ha-tf-daily').style.color = dtTrend === 'BULLISH' ? 'var(--green)' : 'var(--red)';
    document.getElementById('ha-tf-1h').textContent = htTrend;
    document.getElementById('ha-tf-1h').style.color = htTrend === 'BULLISH' ? 'var(--green)' : htTrend === 'BEARISH' ? 'var(--red)' : 'var(--orange)';
    document.getElementById('ha-tf-15m').textContent = mtTrend;
    document.getElementById('ha-tf-15m').style.color = mtTrend === 'BULLISH' ? 'var(--green)' : mtTrend === 'BEARISH' ? 'var(--red)' : 'var(--orange)';

    // 4. Backtest Win-Rate Table
    const backtestRows = document.getElementById('ha-backtest-rows');
    if (backtestRows) {
      const bBull = data.backtest_bullish;
      const bBear = data.backtest_bearish;

      backtestRows.innerHTML = `
        <tr>
          <td style="padding:10px 4px; font-weight:700; color:var(--green);">🟢 Bullish (Score &ge; 55)</td>
          <td style="text-align:center; padding:10px 4px; font-weight:600;">${bBull.count} Triggers</td>
          <td style="text-align:center; padding:10px 4px; color:var(--green); font-weight:600;">${bBull.win_1d.toFixed(1)}%</td>
          <td style="text-align:center; padding:10px 4px; color:var(--green); font-weight:600;">${bBull.win_2d.toFixed(1)}%</td>
          <td style="text-align:center; padding:10px 4px; color:var(--green); font-weight:600;">${bBull.win_5d.toFixed(1)}%</td>
          <td style="text-align:right; padding:10px 4px; font-weight:700; color:var(--green);">${bBull.avg_change >= 0 ? '+' : ''}${bBull.avg_change.toFixed(2)}%</td>
        </tr>
        <tr style="border-top:1px solid var(--border-light);">
          <td style="padding:10px 4px; font-weight:700; color:var(--red);">🔴 Bearish (Score &lt; 45)</td>
          <td style="text-align:center; padding:10px 4px; font-weight:600;">${bBear.count} Triggers</td>
          <td style="text-align:center; padding:10px 4px; color:var(--red); font-weight:600;">${bBear.win_1d.toFixed(1)}%</td>
          <td style="text-align:center; padding:10px 4px; color:var(--red); font-weight:600;">${bBear.win_2d.toFixed(1)}%</td>
          <td style="text-align:center; padding:10px 4px; color:var(--red); font-weight:600;">${bBear.win_5d.toFixed(1)}%</td>
          <td style="text-align:right; padding:10px 4px; font-weight:700; color:var(--red);">${bBear.avg_change >= 0 ? '+' : ''}${bBear.avg_change.toFixed(2)}%</td>
        </tr>
      `;
    }

    // 4.5 Populate Streak Reversal & Exhaustion
    const climbStreakEl = document.getElementById('ha-climb-streak');
    const climbProbEl = document.getElementById('ha-climb-prob');
    const climbTargetEl = document.getElementById('ha-climb-target');
    const dropStreakEl = document.getElementById('ha-drop-streak');
    const dropProbEl = document.getElementById('ha-drop-prob');
    const dropTargetEl = document.getElementById('ha-drop-target');

    if (climbStreakEl && dropStreakEl) {
      const cs = data.climb_streak;
      const ds = data.drop_streak;

      climbStreakEl.textContent = cs.days > 0 ? `${cs.days} Days (+${cs.change.toFixed(2)}%)` : '0 Consecutive Days';
      climbProbEl.textContent = cs.days > 0 ? `${cs.prob.toFixed(1)}% (${cs.prob >= 75 ? 'HIGH' : 'MODERATE'})` : '—';
      climbTargetEl.textContent = cs.days > 0 ? `₹${cs.target.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';

      dropStreakEl.textContent = ds.days > 0 ? `${ds.days} Days (${ds.change.toFixed(2)}%)` : '0 Consecutive Days';
      dropProbEl.textContent = ds.days > 0 ? `${ds.prob.toFixed(1)}% (${ds.prob >= 80 ? 'EXTREME' : 'MODERATE'})` : '—';
      dropTargetEl.textContent = ds.days > 0 ? `₹${ds.target.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';
    }

    // 5. Opening Gap Stats
    const g = data.gaps;
    document.getElementById('ha-gapup-count').textContent = `${g.gapup_count} Days`;
    document.getElementById('ha-gapup-follow').textContent = g.gapup_follow;
    document.getElementById('ha-gapup-fade').textContent = g.gapup_fade;

    document.getElementById('ha-gapdown-count').textContent = `${g.gapdown_count} Days`;
    document.getElementById('ha-gapdown-follow').textContent = g.gapdown_follow;
    document.getElementById('ha-gapdown-fade').textContent = g.gapdown_fade;

    // 6. Institutional Climax Support/Resistance
    const climaxList = document.getElementById('ha-climax-list');
    if (climaxList) {
      if (data.climax_walls && data.climax_walls.length > 0) {
        climaxList.innerHTML = data.climax_walls.map(node => `
          <div style="display:flex; justify-content:space-between; align-items:center; background:rgba(255,255,255,0.4); border-radius:var(--radius-sm); padding:8px; font-size:0.75rem; border:1px solid var(--border-light); margin-bottom:4px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span class="badge" style="background:var(--primary); color:white; font-weight:700; font-size:0.65rem; padding:2px 4px; border-radius:4px;">VOLUME SPIKE</span>
              <span style="font-weight:600;">${node.date}</span>
            </div>
            <div style="display:flex; gap:12px;">
              <span>Multiplier: <strong style="color:var(--orange);">${node.multiplier}x</strong></span>
              <span>Close: <strong style="color:var(--primary-dark);">₹${node.close.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong></span>
            </div>
          </div>
        `).join('');
      } else {
        climaxList.innerHTML = `<div style="text-align:center; padding:15px; color:var(--text-muted); font-size:0.75rem;">No institutional volume climax nodes found.</div>`;
      }
    }

    // 7. Render 30 Sessions in the Table
    const tableBody = document.getElementById('ha-ohlcv-rows');
    if (tableBody && data.rows) {
      tableBody.innerHTML = data.rows.map(row => {
        const isUp = row.change >= 0;
        const colorClass = isUp ? 'var(--green)' : 'var(--red)';
        const sign = isUp ? '+' : '';

        return `
          <tr style="border-bottom:1px solid var(--border-light); height:44px; transition:background 0.2s;" onmouseover="this.style.background='rgba(255,255,255,0.2)'" onmouseout="this.style.background='transparent'">
            <td style="padding:10px 12px; font-weight:700; color:var(--text-primary); text-align:left;">${row.date}</td>
            <td style="padding:10px 12px; font-weight:700; color:${colorClass}; text-align:right;">${row.price.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td style="padding:10px 12px; color:var(--text-secondary); text-align:right;">${row.open.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td style="padding:10px 12px; color:var(--text-secondary); text-align:right;">${row.high.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td style="padding:10px 12px; color:var(--text-secondary); text-align:right;">${row.low.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td style="padding:10px 12px; color:var(--text-secondary); text-align:right; font-weight:500;">${row.vol}</td>
            <td style="padding:10px 12px; font-weight:700; color:${colorClass}; text-align:right;">${sign}${row.change.toFixed(2)}%</td>
          </tr>
        `;
      }).join('');
    }

    const countEl = document.getElementById('ha-table-count');
    if (countEl) countEl.textContent = `Showing Last ${data.rows ? data.rows.length : 0} Sessions`;
  }
}

// Instantiate globally
window.historicalAnalysis = new HistoricalAnalysis();

// ─────────────────────────────────────────────
//  Bulk Historical Scan Controller
// ─────────────────────────────────────────────

let _haBulkResults = [];   // persisted for CSV download

/** Toggle the bulk panel open/closed */
window.haBulkToggle = function () {
  const panel = document.getElementById('ha-bulk-panel');
  const icon = document.getElementById('ha-bulk-toggle-icon');
  if (!panel) return;
  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  icon.textContent = isOpen ? '▼ Expand' : '▲ Collapse';
};

/**
 * Quick-fill the bulk symbol textarea with all F&O stocks for a given cap tier.
 * Uses equityScreener universe (same source as the rest of the app).
 * @param {'large'|'mid'|'small'} cap
 */
window.haBulkFillCap = function (cap) {
  const ta = document.getElementById('ha-bulk-symbols');
  if (!ta) return;

  let stocks = [];
  if (window.equityScreener) {
    try {
      stocks = equityScreener.getFNOUniverseSync()
        .filter(s => s.cap === cap)
        .map(s => s.symbol);
    } catch (e) { /* fallback below */ }
  }

  // Hardcoded fallback if equityScreener isn't loaded yet
  if (!stocks.length) {
    const CAPS = {
      large: ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'BAJFINANCE', 'LT',
        'HINDUNILVR', 'ITC', 'AXISBANK', 'KOTAKBANK', 'MARUTI', 'TATAMOTORS', 'SUNPHARMA',
        'WIPRO', 'BHARTIARTL', 'ASIANPAINT', 'TATASTEEL', 'HINDALCO', 'JSWSTEEL',
        'ADANIENT', 'ADANIPORTS', 'POWERGRID', 'NTPC', 'COALINDIA', 'ONGC', 'BPCL',
        'DRREDDY', 'CIPLA'],
      mid: ['TRENT', 'COFORGE', 'KAYNES', 'PERSISTENT', 'MPHASIS', 'ZYDUSLIFE', 'JUBLFOOD',
        'PIIND', 'PAGEIND', 'DIXON', 'POLYCAB', 'LALPATHLAB', 'METROPOLIS', 'IRCTC',
        'GLAND', 'DEEPAKNTR', 'AAVAS', 'HOMEFIRST', 'CAMS', 'ANGELONE'],
      small: ['IDFCFIRSTB', 'RBLBANK', 'BANDHANBNK', 'FEDERALBNK', 'KARURVYSYA',
        'CENTURYTEX', 'GNFC', 'GHCL', 'ATUL', 'NAVINFLUOR', 'FINEORG',
        'ROUTE', 'LATENTVIEW', 'TARSONS', 'HAPPYMIND']
    };
    stocks = CAPS[cap] || [];
  }

  if (!stocks.length) {
    alert(`No ${cap} cap stocks found in the universe.`);
    return;
  }

  ta.value = stocks.join(', ');

  // Visual feedback — briefly highlight the textarea
  ta.style.borderColor = cap === 'large' ? '#1E88E5' : cap === 'mid' ? '#FFA726' : '#26A69A';
  setTimeout(() => { ta.style.borderColor = ''; }, 800);
};

/** Parse and deduplicate raw symbol input */
function _parseBulkSymbols(raw) {
  return [...new Set(
    raw.split(/[\s,;\n]+/)
      .map(s => s.trim().toUpperCase())
      .filter(s => s.length > 0)
  )];
}

/** Run bulk scan — POST to backend, render table, enable CSV */
window.haBulkRun = async function () {
  const rawInput = (document.getElementById('ha-bulk-symbols')?.value || '').trim();
  const days = parseInt(document.getElementById('ha-bulk-days')?.value || '90', 10);

  if (!rawInput) {
    alert('Please enter at least one stock symbol.');
    return;
  }

  const symbols = _parseBulkSymbols(rawInput);
  if (symbols.length === 0) { alert('No valid symbols found.'); return; }
  if (symbols.length > 100) { alert('Maximum 100 symbols allowed.'); return; }

  // UI — start state
  const runBtn = document.getElementById('btn-ha-bulk-run');
  const csvBtn = document.getElementById('btn-ha-bulk-csv');
  const progWrap = document.getElementById('ha-bulk-progress-wrap');
  const progBar = document.getElementById('ha-bulk-progress-bar');
  const progLbl = document.getElementById('ha-bulk-progress-label');
  const progCnt = document.getElementById('ha-bulk-progress-count');
  const resultEl = document.getElementById('ha-bulk-results');

  if (runBtn) { runBtn.disabled = true; runBtn.textContent = '⏳ Scanning…'; }
  if (csvBtn) { csvBtn.disabled = true; csvBtn.style.opacity = '0.5'; csvBtn.style.cursor = 'not-allowed'; }
  if (progWrap) progWrap.style.display = 'block';
  if (progBar) progBar.style.width = '5%';
  if (progLbl) progLbl.textContent = `Scanning ${symbols.length} stocks…`;
  if (progCnt) progCnt.textContent = `0 / ${symbols.length}`;
  if (resultEl) resultEl.style.display = 'none';

  try {
    const resp = await fetch('/api/historical-analytics-bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ symbols, days })
    });

    if (progBar) progBar.style.width = '90%';

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Bulk scan failed: ${err.error || resp.statusText}`);
      return;
    }

    const data = await resp.json();
    _haBulkResults = data.results || [];

    if (progBar) progBar.style.width = '100%';
    if (progCnt) progCnt.textContent = `${_haBulkResults.length} / ${symbols.length}`;
    if (progLbl) progLbl.textContent = `Scan complete — ${_haBulkResults.length} stocks processed`;

    _renderBulkTable(_haBulkResults, days);

    // Enable CSV
    if (csvBtn) {
      csvBtn.disabled = false;
      csvBtn.style.opacity = '1';
      csvBtn.style.cursor = 'pointer';
    }

  } catch (e) {
    alert(`Network error: ${e.message}`);
  } finally {
    if (runBtn) { runBtn.disabled = false; runBtn.textContent = '⚡ Run Bulk Scan'; }
  }
};

/** Render the results table */
function _renderBulkTable(results, days) {
  const tbody = document.getElementById('ha-bulk-rows');
  const wrapper = document.getElementById('ha-bulk-results');
  const countEl = document.getElementById('ha-bulk-results-count');
  const tsEl = document.getElementById('ha-bulk-results-ts');

  if (!tbody || !wrapper) return;

  const sc = (s) => s === 'BULLISH' || s.includes('BULLISH') ? '#26A69A' : s === 'BEARISH' || s.includes('BEARISH') ? '#EF5350' : '#78909C';
  const bg = (s) => s === 'BULLISH' || s.includes('BULLISH') ? 'rgba(38,166,154,0.12)' : s === 'BEARISH' || s.includes('BEARISH') ? 'rgba(239,83,80,0.12)' : 'rgba(120,144,156,0.08)';
  const tag = (label, status) => `<span style="padding:2px 8px;border-radius:6px;font-weight:700;font-size:0.72rem;background:${bg(status)};color:${sc(status)};">${label}</span>`;

  tbody.innerHTML = results.map(r => {
    if (r.status && r.status.startsWith('ERROR')) {
      return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);">
        <td style="padding:8px 12px;font-weight:700;">${r.symbol}</td>
        <td colspan="18" style="padding:8px;text-align:center;color:#EF5350;font-size:0.72rem;">${r.status}</td>
      </tr>`;
    }

    const streakLabel = r.current_streak_dir === 'climb'
      ? `🟢 +${r.current_streak_days}d`
      : r.current_streak_dir === 'drop'
        ? `🔴 -${r.current_streak_days}d`
        : '—';

    const emaCrossShort = {
      'BULLISH_ACTIVE': '✅ GX Active',
      'BULLISH_APPROACHING': '⚠️ GX Near',
      'BEARISH_ACTIVE': '🔴 DX Active',
      'BEARISH_APPROACHING': '⚠️ DX Near',
    }[r.ema_cross_status] || '—';

    const scoreColor = r.score >= 70 ? '#26A69A' : r.score >= 55 ? '#FFA726' : r.score < 45 ? '#EF5350' : '#78909C';

    const upcomingPredictionTag = r.upcoming_prediction
      ? tag(`${r.upcoming_prediction.consensus_bias} (${r.upcoming_prediction.confidence_score}%)`, r.upcoming_prediction.consensus_bias)
      : '—';

    return `<tr style="border-bottom:1px solid rgba(255,255,255,0.05);" id="ha-bulk-row-${r.symbol}">
      <td style="padding:8px 12px;font-weight:800;color:var(--text-primary);">${r.symbol}</td>
      <td style="padding:8px 10px;text-align:center;"><span style="font-weight:800;color:${scoreColor};">${r.score}</span></td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.direction, r.direction)}</td>
      <td style="padding:8px 10px;text-align:center;">${upcomingPredictionTag}</td>
      <td style="padding:8px 10px;text-align:right;font-weight:600;">₹${(r.ltp || 0).toLocaleString('en-IN')}</td>
      <td style="padding:8px 10px;text-align:center;color:${r.rsi > 60 ? '#26A69A' : r.rsi < 40 ? '#EF5350' : '#FFA726'};">${r.rsi}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.macd_signal, r.macd_signal)}</td>
      <td style="padding:8px 10px;text-align:center;color:${r.chop_state === 'TRENDING' ? '#AB47BC' : '#78909C'};">${r.chop} <span style="font-size:0.65rem;">${r.chop_state}</span></td>
      <td style="padding:8px 10px;text-align:center;font-size:0.72rem;">${emaCrossShort}</td>
      <td style="padding:8px 10px;text-align:center;color:${r.ema_strength?.startsWith('+') ? '#26A69A' : '#EF5350'};font-weight:700;">${r.ema_strength}</td>
      <td style="padding:8px 10px;text-align:center;">${r.gap_up_fade_pct}</td>
      <td style="padding:8px 10px;text-align:center;">${r.gap_down_fade_pct}</td>
      <td style="padding:8px 10px;text-align:center;color:#26A69A;font-weight:600;">${r.bull_backtest_win_rate}</td>
      <td style="padding:8px 10px;text-align:center;color:#EF5350;font-weight:600;">${r.bear_backtest_win_rate}</td>
      <td style="padding:8px 10px;text-align:center;">${streakLabel}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.daily_trend || 'NEUTRAL', r.daily_trend || 'NEUTRAL')}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.hourly_trend || 'NEUTRAL', r.hourly_trend || 'NEUTRAL')}</td>
      <td style="padding:8px 10px;text-align:center;">${tag(r.m15_trend || 'NEUTRAL', r.m15_trend || 'NEUTRAL')}</td>
      <td style="padding:8px 10px;text-align:center;"><span style="padding:2px 6px;border-radius:4px;font-size:0.65rem;font-weight:700;background:rgba(38,166,154,0.1);color:#26A69A;">${r.status}</span></td>
    </tr>`;
  }).join('');

  const ok = results.filter(r => !r.status?.startsWith('ERROR')).length;
  const errs = results.length - ok;

  if (countEl) countEl.textContent = `${results.length} stocks scanned — ${ok} OK${errs > 0 ? `, ${errs} errors` : ''}`;
  if (tsEl) tsEl.textContent = `Generated at ${new Date().toLocaleTimeString()} · ${days}-day window`;

  wrapper.style.display = 'block';
}

/** Build and trigger CSV download */
window.haBulkDownloadCSV = function () {
  if (!_haBulkResults || _haBulkResults.length === 0) return;

  const headers = [
    'Date', 'Symbol', 'Open', 'High', 'Low', 'Close', 'Volume', 'Change %',
    'Score', 'Direction', 'Upcoming Bias', 'Upcoming Bias Conf %', 'RSI', 'MACD Signal', 'CHOP', 'CHOP State',
    'EMA 50', 'EMA 200', 'EMA Cross Status', 'EMA Cross Gap %', 'EMA Strength',
    'Gap Up Count', 'Gap Up Fade %', 'Gap Down Count', 'Gap Down Fade %',
    'Bull Backtest Triggers', 'Bull Win Rate %', 'Bear Backtest Triggers', 'Bear Win Rate %',
    'Current Streak Dir', 'Current Streak Days', 'Daily Trend', 'Hourly Trend', '15m Trend',
    'Status'
  ];

  const csvRows = [];
  _haBulkResults.forEach(r => {
    if (r.status && r.status.startsWith('ERROR')) {
      csvRows.push([
        '', r.symbol, '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', r.status
      ]);
      return;
    }

    const daily = r.daily_rows || [];
    if (daily.length > 0) {
      daily.forEach(d => {
        csvRows.push([
          d.date,
          r.symbol,
          d.open,
          d.high,
          d.low,
          d.close,
          d.volume,
          d.change_pct,
          d.score,
          d.direction,
          d.upcoming_predict_bias || '',
          d.upcoming_predict_conf || '',
          d.rsi,
          d.macd_signal,
          d.chop,
          d.chop_state,
          d.ema_50,
          d.ema_200,
          d.ema_cross_status,
          d.ema_cross_gap_pct,
          d.ema_strength,
          d.gap_up_count,
          d.gap_up_fade_pct,
          d.gap_down_count,
          d.gap_down_fade_pct,
          d.bull_backtest_triggers,
          d.bull_backtest_win_rate,
          d.bear_backtest_triggers,
          d.bear_backtest_win_rate,
          d.current_streak_dir,
          d.current_streak_days,
          d.daily_trend,
          d.hourly_trend,
          d.m15_trend,
          r.status
        ]);
      });
    } else {
      csvRows.push([
        '',
        r.symbol,
        '',
        '',
        '',
        r.ltp || '',
        '',
        '',
        r.score || '',
        r.direction || '',
        r.upcoming_prediction ? r.upcoming_prediction.consensus_bias : '',
        r.upcoming_prediction ? r.upcoming_prediction.confidence_score : '',
        r.rsi || '',
        r.macd_signal || '',
        r.chop || '',
        r.chop_state || '',
        r.ema_50 || '',
        r.ema_200 || '',
        r.ema_cross_status || '',
        r.ema_cross_gap_pct || '',
        r.ema_strength || '',
        r.gap_up_count || 0,
        r.gap_up_fade_pct || '',
        r.gap_down_count || 0,
        r.gap_down_fade_pct || '',
        r.bull_backtest_triggers || 0,
        r.bull_backtest_win_rate || '',
        r.bear_backtest_triggers || 0,
        r.bear_backtest_win_rate || '',
        r.current_streak_dir || '',
        r.current_streak_days || 0,
        r.daily_trend || '',
        r.hourly_trend || '',
        r.m15_trend || '',
        r.status
      ]);
    }
  });

  const rows = csvRows.map(r => r.map(v => `"${String(v ?? '').replace(/"/g, '""')}"`));
  const csvContent = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);

  const a = document.createElement('a');
  a.href = url;
  const ts = new Date().toISOString().slice(0, 10);
  a.download = `TradeSignal_BulkHistoricalScan_${ts}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

// ─────────────────────────────────────────────
//  First-Hour Pattern Continuation/Reversal Analyzer Controller
// ─────────────────────────────────────────────

window.haFHToggle = function () {
  const panel = document.getElementById('ha-fh-panel');
  const icon = document.getElementById('ha-fh-toggle-icon');
  if (!panel) return;
  const isOpen = panel.style.display !== 'none';
  panel.style.display = isOpen ? 'none' : 'block';
  icon.textContent = isOpen ? '▼ Expand' : '▲ Collapse';
};

window.haFHFillCap = function (cap) {
  const ta = document.getElementById('ha-fh-symbols');
  if (!ta) return;

  let stocks = [];
  if (window.equityScreener) {
    try {
      stocks = equityScreener.getFNOUniverseSync()
        .filter(s => s.cap === cap)
        .map(s => s.symbol);
    } catch (e) { /* fallback below */ }
  }

  // Hardcoded fallback if equityScreener isn't loaded yet
  if (!stocks.length) {
    const CAPS = {
      large: ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'BAJFINANCE', 'LT',
        'HINDUNILVR', 'ITC', 'AXISBANK', 'KOTAKBANK', 'MARUTI', 'TATAMOTORS', 'SUNPHARMA',
        'WIPRO', 'BHARTIARTL', 'ASIANPAINT', 'TATASTEEL', 'HINDALCO', 'JSWSTEEL',
        'ADANIENT', 'ADANIPORTS', 'POWERGRID', 'NTPC', 'COALINDIA', 'ONGC', 'BPCL',
        'DRREDDY', 'CIPLA'],
      mid: ['TRENT', 'COFORGE', 'KAYNES', 'PERSISTENT', 'MPHASIS', 'ZYDUSLIFE', 'JUBLFOOD',
        'PIIND', 'PAGEIND', 'DIXON', 'POLYCAB', 'LALPATHLAB', 'METROPOLIS', 'IRCTC',
        'GLAND', 'DEEPAKNTR', 'AAVAS', 'HOMEFIRST', 'CAMS', 'ANGELONE'],
      small: ['IDFCFIRSTB', 'RBLBANK', 'BANDHANBNK', 'FEDERALBNK', 'KARURVYSYA',
        'CENTURYTEX', 'GNFC', 'GHCL', 'ATUL', 'NAVINFLUOR', 'FINEORG',
        'ROUTE', 'LATENTVIEW', 'TARSONS', 'HAPPYMIND']
    };
    stocks = CAPS[cap] || [];
  }

  ta.value = stocks.join(', ');

  // Visual feedback — briefly highlight the textarea
  ta.style.borderColor = cap === 'large' ? '#1E88E5' : cap === 'mid' ? '#FFA726' : '#26A69A';
  setTimeout(() => { ta.style.borderColor = ''; }, 800);
};

window.haFHRun = async function () {
  const rawInput = (document.getElementById('ha-fh-symbols')?.value || '').trim();
  const orWindow = parseInt(document.getElementById('ha-fh-window')?.value || '45', 10);
  const gapThreshold = parseFloat(document.getElementById('ha-fh-gap-threshold')?.value || '0.3');

  if (!rawInput) {
    alert('Please enter at least one stock symbol.');
    return;
  }
  const symbols = _parseBulkSymbols(rawInput);
  if (symbols.length === 0) {
    alert('No valid symbols found.');
    return;
  }

  const runBtn = document.getElementById('btn-ha-fh-run');
  const progWrap = document.getElementById('ha-fh-progress-wrap');
  const progBar = document.getElementById('ha-fh-progress-bar');
  const progLbl = document.getElementById('ha-fh-progress-label');
  const progCnt = document.getElementById('ha-fh-progress-count');
  const resultEl = document.getElementById('ha-fh-results');

  if (runBtn) {
    runBtn.disabled = true;
    runBtn.textContent = '⏳ Analyzing…';
  }
  if (progWrap) progWrap.style.display = 'block';
  if (progBar) progBar.style.width = '5%';
  if (progLbl) progLbl.textContent = `Running First-Hour Analyzer for ${symbols.length} stocks…`;
  if (progCnt) progCnt.textContent = `0 / ${symbols.length}`;
  if (resultEl) resultEl.style.display = 'none';

  try {
    const resp = await fetch('/api/first-hour-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({
        symbols: symbols,
        or_window: orWindow,
        gap_threshold: gapThreshold
      })
    });

    if (progBar) progBar.style.width = '90%';

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`First-Hour analysis failed: ${err.error || resp.statusText}`);
      return;
    }

    const data = await resp.json();
    const results = data.results || [];

    if (progBar) progBar.style.width = '100%';
    if (progCnt) progCnt.textContent = `${results.length} / ${symbols.length}`;
    if (progLbl) progLbl.textContent = 'Analysis complete';

    haFHLastResults = results;
    haFHResultsSort = { column: null, desc: false };

    _renderFHResultsTable(haFHLastResults);
    await window.haFHFetchHistory();

  } catch (e) {
    alert(`Network error: ${e.message}`);
  } finally {
    if (runBtn) {
      runBtn.disabled = false;
      runBtn.textContent = '⚡ Run Analyzer';
    }
  }
};

// Global states for First-Hour Analyzer sorting and caching
let haFHLastResults = [];
let haFHLastHistory = [];
let haFHResultsSort = { column: null, desc: false };
let haFHHistorySort = { column: null, desc: false };

function _renderFHResultsTable(results) {
  const tbody = document.getElementById('ha-fh-rows');
  const thead = document.getElementById('ha-fh-results-thead');
  const wrapper = document.getElementById('ha-fh-results');
  const tsEl = document.getElementById('ha-fh-results-ts');
  if (!tbody || !wrapper) return;

  const cols = [
    { key: 'symbol', label: 'Symbol', align: 'left' },
    { key: 'date', label: 'Date', align: 'center' },
    { key: 'pattern_key', label: 'Pattern Key', align: 'center' },
    { key: 'predicted_outcome', label: 'Predicted Outcome', align: 'center' },
    { key: 'prediction_confidence', label: 'Confidence', align: 'center' },
    { key: 'actual_outcome', label: 'Actual Outcome', align: 'center' },
    { key: 'validation_result', label: 'Validation', align: 'center' },
    { key: 'stats', label: 'Details (Cont / Rev / Chop)', align: 'center' }
  ];

  const sortIcon = (colKey) => {
    if (haFHResultsSort.column === colKey) {
      return haFHResultsSort.desc ? ' ▼' : ' ▲';
    }
    return ' ↕';
  };

  if (thead) {
    thead.innerHTML = `<tr style="background:rgba(255,255,255,0.06); border-bottom:1.5px solid var(--border);">
      ${cols.map(c => `
        <th onclick="window.haFHSortResults('${c.key}')" 
            style="padding:8px ${c.key === 'symbol' ? '12px' : '10px'}; text-align:${c.align}; color:var(--text-secondary); cursor:pointer; user-select:none;">
          ${c.label}${sortIcon(c.key)}
        </th>
      `).join('')}
    </tr>`;
  }

  tbody.innerHTML = results.map(r => {
    if (r.error) {
      return `<tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
        <td style="padding: 8px 12px; font-weight: 700; color: var(--text-primary);">${r.symbol}</td>
        <td colspan="7" style="padding: 8px; text-align: center; color: #EF5350; font-size: 0.72rem;">${r.error}</td>
      </tr>`;
    }

    const valBadge = r.validation_result === 'CORRECT'
      ? `<span style="padding: 2px 6px; border-radius: 4px; font-size: 0.65rem; font-weight: 700; background: rgba(76,175,80,0.2); color: var(--green);">🟢 CORRECT</span>`
      : r.validation_result === 'INCORRECT'
        ? `<span style="padding: 2px 6px; border-radius: 4px; font-size: 0.65rem; font-weight: 700; background: rgba(239,83,80,0.2); color: var(--red);">🔴 INCORRECT</span>`
        : `<span style="padding: 2px 6px; border-radius: 4px; font-size: 0.65rem; font-weight: 700; background: rgba(255,255,255,0.1); color: var(--text-secondary);">⏳ PENDING</span>`;

    const predColor = r.predicted_outcome === 'continuation' ? 'var(--green)' : r.predicted_outcome === 'reversal' ? 'var(--red)' : 'var(--orange)';
    const actualColor = r.actual_outcome === 'continuation' ? 'var(--green)' : r.actual_outcome === 'reversal' ? 'var(--red)' : r.actual_outcome === 'chop' ? 'var(--orange)' : 'var(--text-secondary)';

    return `<tr style="border-bottom: 1px solid rgba(255,255,255,0.05);">
      <td style="padding: 8px 12px; font-weight: 800; color: var(--text-primary);">${r.symbol}</td>
      <td style="padding: 8px 10px; text-align: center;">${r.date}</td>
      <td style="padding: 8px 10px; text-align: center; font-weight: 600; font-size: 0.7rem; color: var(--text-secondary);">${r.pattern_key}</td>
      <td style="padding: 8px 10px; text-align: center; font-weight: 700; color: ${predColor}; text-transform: uppercase;">${r.predicted_outcome}</td>
      <td style="padding: 8px 10px; text-align: center; font-weight: 700;">${r.prediction_confidence}%</td>
      <td style="padding: 8px 10px; text-align: center; font-weight: 700; color: ${actualColor}; text-transform: uppercase;">${r.actual_outcome || '—'}</td>
      <td style="padding: 8px 10px; text-align: center;">${valBadge}</td>
      <td style="padding: 8px 10px; text-align: center; font-size: 0.7rem; color: var(--text-muted);">
        Cont: ${r.stats.continuation_pct}% | Rev: ${r.stats.reversal_pct}% | Chop: ${r.stats.chop_pct}% (n=${r.stats.sample_size})
      </td>
    </tr>`;
  }).join('');

  if (tsEl && !tsEl.textContent) {
    tsEl.textContent = `Scanned at ${new Date().toLocaleTimeString()}`;
  }
  wrapper.style.display = 'block';
}

function _renderFHHistoryTable(history) {
  const tbody = document.getElementById('ha-fh-history-rows');
  const thead = document.getElementById('ha-fh-history-thead');
  if (!tbody) return;

  if (history.length === 0) {
    tbody.innerHTML = `<tr>
      <td colspan="8" style="text-align: center; padding: 15px; color: var(--text-muted);">No predictions logged yet. Run predictions above to start tracking performance.</td>
    </tr>`;
    return;
  }

  const historyCols = [
    { key: 'date', label: 'Date', align: 'left' },
    { key: 'symbol', label: 'Symbol', align: 'left' },
    { key: 'pattern_key', label: 'Pattern', align: 'center' },
    { key: 'predicted_outcome', label: 'Prediction', align: 'center' },
    { key: 'prediction_confidence', label: 'Confidence', align: 'center' },
    { key: 'actual_outcome', label: 'Actual', align: 'center' },
    { key: 'validation_result', label: 'Result', align: 'center' },
    { key: 'created_at', label: 'Logged At', align: 'center' }
  ];

  const sortIconHistory = (colKey) => {
    if (haFHHistorySort.column === colKey) {
      return haFHHistorySort.desc ? ' ▼' : ' ▲';
    }
    return ' ↕';
  };

  if (thead) {
    thead.innerHTML = `<tr style="background:rgba(255,255,255,0.04); border-bottom:1px solid var(--border);">
      ${historyCols.map(c => `
        <th onclick="window.haFHSortHistory('${c.key}')" 
            style="padding:6px 10px; text-align:${c.align}; color:var(--text-secondary); cursor:pointer; user-select:none;">
          ${c.label}${sortIconHistory(c.key)}
        </th>
      `).join('')}
    </tr>`;
  }

  tbody.innerHTML = history.map(h => {
    const predColor = h.predicted_outcome === 'continuation' ? 'var(--green)' : h.predicted_outcome === 'reversal' ? 'var(--red)' : h.predicted_outcome === 'chop' ? 'var(--orange)' : 'var(--text-secondary)';
    const actualColor = h.actual_outcome === 'continuation' ? 'var(--green)' : h.actual_outcome === 'reversal' ? 'var(--red)' : h.actual_outcome === 'chop' ? 'var(--orange)' : 'var(--text-secondary)';
    const valBadge = h.validation_result === 'CORRECT'
      ? `<span style="color: var(--green); font-weight: 700;">🟢 CORRECT</span>`
      : h.validation_result === 'INCORRECT'
        ? `<span style="color: var(--red); font-weight: 700;">🔴 INCORRECT</span>`
        : `<span style="color: var(--text-secondary); font-weight: 600;">⏳ PENDING</span>`;

    let loggedAt = '—';
    if (h.created_at) {
      try {
        loggedAt = new Date(h.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      } catch (e) {
        loggedAt = h.created_at.split('T')[1]?.slice(0, 5) || '—';
      }
    }

    return `<tr style="border-bottom: 1px solid rgba(255,255,255,0.03);">
      <td style="padding: 6px 10px; text-align: left; font-weight: 600;">${h.date}</td>
      <td style="padding: 6px 10px; text-align: left; font-weight: 800; color: var(--text-primary);">${h.symbol}</td>
      <td style="padding: 6px 10px; text-align: center; color: var(--text-muted); font-size: 0.68rem;">${h.pattern_key}</td>
      <td style="padding: 6px 10px; text-align: center; font-weight: 700; color: ${predColor}; text-transform: uppercase;">${h.predicted_outcome}</td>
      <td style="padding: 6px 10px; text-align: center; font-weight: 600;">${h.prediction_confidence}%</td>
      <td style="padding: 6px 10px; text-align: center; font-weight: 700; color: ${actualColor}; text-transform: uppercase;">${h.actual_outcome || '—'}</td>
      <td style="padding: 6px 10px; text-align: center;">${valBadge}</td>
      <td style="padding: 6px 10px; text-align: center; color: var(--text-muted);">${loggedAt}</td>
    </tr>`;
  }).join('');
}

window.haFHSortResults = function (column) {
  if (haFHResultsSort.column === column) {
    haFHResultsSort.desc = !haFHResultsSort.desc;
  } else {
    haFHResultsSort.column = column;
    haFHResultsSort.desc = false;
  }

  haFHLastResults.sort((a, b) => {
    let valA, valB;
    if (column === 'stats') {
      valA = a.stats?.sample_size || 0;
      valB = b.stats?.sample_size || 0;
    } else {
      valA = a[column];
      valB = b[column];
    }

    if (valA === undefined || valA === null) valA = '';
    if (valB === undefined || valB === null) valB = '';

    if (typeof valA === 'string' && typeof valB === 'string') {
      return haFHResultsSort.desc
        ? valB.localeCompare(valA)
        : valA.localeCompare(valB);
    } else {
      return haFHResultsSort.desc
        ? Number(valB) - Number(valA)
        : Number(valA) - Number(valB);
    }
  });

  _renderFHResultsTable(haFHLastResults);
};

window.haFHSortHistory = function (column) {
  if (haFHHistorySort.column === column) {
    haFHHistorySort.desc = !haFHHistorySort.desc;
  } else {
    haFHHistorySort.column = column;
    haFHHistorySort.desc = false;
  }

  haFHLastHistory.sort((a, b) => {
    let valA = a[column];
    let valB = b[column];

    if (valA === undefined || valA === null) valA = '';
    if (valB === undefined || valB === null) valB = '';

    if (typeof valA === 'string' && typeof valB === 'string') {
      return haFHHistorySort.desc
        ? valB.localeCompare(valA)
        : valA.localeCompare(valB);
    } else {
      return haFHHistorySort.desc
        ? Number(valB) - Number(valA)
        : Number(valA) - Number(valB);
    }
  });

  _renderFHHistoryTable(haFHLastHistory);
};

window.haFHExportResultsCSV = function () {
  if (!haFHLastResults || haFHLastResults.length === 0) {
    alert("No data available to export.");
    return;
  }

  const headers = ["Symbol", "Date", "Pattern Key", "Predicted Outcome", "Confidence", "Actual Outcome", "Validation Result", "Cont Pct", "Rev Pct", "Chop Pct", "Sample Size"];
  const rows = haFHLastResults.map(r => {
    if (r.error) {
      return [r.symbol, "", "", "", "", "", r.error, "", "", "", ""];
    }
    return [
      r.symbol,
      r.date,
      r.pattern_key,
      r.predicted_outcome,
      `${r.prediction_confidence}%`,
      r.actual_outcome || "",
      r.validation_result || "",
      `${r.stats?.continuation_pct || 0}%`,
      `${r.stats?.reversal_pct || 0}%`,
      `${r.stats?.chop_pct || 0}%`,
      r.stats?.sample_size || 0
    ];
  });

  _downloadCSV("first_hour_today_predictions.csv", headers, rows);
};

window.haFHExportHistoryCSV = function () {
  if (!haFHLastHistory || haFHLastHistory.length === 0) {
    alert("No data available to export.");
    return;
  }

  const headers = ["Date", "Symbol", "Pattern Key", "Prediction", "Confidence", "Actual Outcome", "Validation Result", "Logged At"];
  const rows = haFHLastHistory.map(h => [
    h.date,
    h.symbol,
    h.pattern_key,
    h.predicted_outcome,
    `${h.prediction_confidence}%`,
    h.actual_outcome || "",
    h.validation_result || "",
    h.created_at || ""
  ]);

  _downloadCSV("first_hour_prediction_history.csv", headers, rows);
};

function _downloadCSV(filename, headers, rows) {
  const escapeCSV = (val) => {
    if (val === null || val === undefined) return '';
    const str = String(val);
    if (str.includes(',') || str.includes('"') || str.includes('\n')) {
      return `"${str.replace(/"/g, '""')}"`;
    }
    return str;
  };

  const csvContent = [
    headers.map(escapeCSV).join(','),
    ...rows.map(row => row.map(escapeCSV).join(','))
  ].join('\n');

  const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.setAttribute("href", url);
  link.setAttribute("download", filename);
  link.style.visibility = 'hidden';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

window.haFHFetchHistory = async function () {
  try {
    const resp = await fetch('/api/first-hour-predictions/history');
    if (!resp.ok) return;
    const data = await resp.json();

    document.getElementById('fh-card-total').textContent = data.summary.total_predictions;
    document.getElementById('fh-card-validated').textContent = data.summary.validated_count;
    document.getElementById('fh-card-correct').textContent = data.summary.correct_count;
    document.getElementById('fh-card-accuracy').textContent = `${data.summary.accuracy_pct}%`;

    haFHLastHistory = data.history || [];
    haFHHistorySort = { column: null, desc: false };

    _renderFHHistoryTable(haFHLastHistory);

  } catch (e) {
    console.error('Failed to fetch prediction history:', e);
  }
};

