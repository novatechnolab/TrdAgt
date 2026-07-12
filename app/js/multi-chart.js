/**
 * TradeSignal — Multi-Chart Tracking Dashboard
 * Manages up to 4 autonomous Lightweight Charts instances.
 */
class MultiChartManager {
  constructor() {
    this._charts = []; // Array of tracking objects
    this._maxCharts = 4;
    this._nextId = 1;
    this._grid = null;
    this._initialized = false;
  }

  init() {
    if (this._initialized) return;
    this._initialized = true;

    this._grid = document.getElementById('mc-grid');
    
    // Set default date to today
    const dateInput = document.getElementById('mc-date-select');
    if (dateInput) {
      dateInput.value = new Date().toISOString().slice(0, 10);
    }

    document.getElementById('mc-add-chart-btn')?.addEventListener('click', () => {
      this.addChart();
    });

    document.getElementById('mc-display-chart-btn')?.addEventListener('click', () => {
      this.clearAll();
      this.addChart();
    });

    console.log('MultiChartManager initialized.');
  }

  clearAll() {
    [...this._charts].forEach(c => this.removeChart(c.id));
  }

  addChart() {
    if (this._charts.length >= this._maxCharts) {
      if (window.alertEngine) alertEngine.triggerToast('Maximum 4 charts allowed', 'warning');
      else alert('Maximum 4 charts allowed');
      return;
    }

    const symbol = document.getElementById('mc-stock-select')?.value;
    if (!symbol) {
      // Find NIFTY or a default
      if (window.alertEngine) alertEngine.triggerToast('Please select a symbol first', 'warning');
      else alert('Please select a symbol first');
      return;
    }

    const date = document.getElementById('mc-date-select')?.value;
    if (!date) return;

    const id = `mc-instance-${this._nextId++}`;
    
    // Create DOM
    const box = document.createElement('div');
    box.className = 'mc-chart-box';
    box.id = id;

    // Checkbox Config UI
    box.innerHTML = `
      <div class="mc-chart-header">
        <strong style="color:var(--primary); font-size: 0.9rem;">${symbol}</strong>
        <div class="controls" style="display:flex; align-items:center; gap:8px;">
          <select class="mc-interval" style="font-size:0.75rem; padding:2px;">
            <option value="5minute" selected>5m</option>
            <option value="15minute">15m</option>
            <option value="30minute">30m</option>
            <option value="day">1D</option>
            <option value="week">1W</option>
          </select>
          <details style="position:relative; display:inline-block;">
            <summary class="btn btn-secondary" style="padding:2px 8px; font-size:0.7rem; cursor:pointer;">Indicators ⚙️</summary>
            <div style="position:absolute; top:calc(100% + 4px); right:0; background:#1e242c; border:1px solid #444; z-index:100; display:flex; flex-direction:column; padding:8px 12px; border-radius:6px; min-width:180px; box-shadow:0 8px 24px rgba(0,0,0,0.8); font-size:0.75rem; white-space:nowrap; color:#fff; text-align:left;">
              <label><input type="checkbox" class="mc-chk-vol" checked> Volume</label>
              <label><input type="checkbox" class="mc-chk-sma" checked> EMA 9/21</label>
              <label><input type="checkbox" class="mc-chk-ema50"> EMA 50</label>
              <label><input type="checkbox" class="mc-chk-vwap" ${symbol.includes('NIFTY') ? '' : 'checked'}> VWAP</label>
              <label><input type="checkbox" class="mc-chk-bb" checked> Bollinger Bands</label>
              <label><input type="checkbox" class="mc-chk-cpr" checked> CPR & PDH/PDL Levels</label>
              <hr style="border-color:#444; margin:4px 0;">
              <label><input type="checkbox" class="mc-chk-smc" checked> SMC Structure (BOS, CHOCH)</label>
              <label><input type="checkbox" class="mc-chk-fvg" checked> SMC FVG Zones</label>
              <label><input type="checkbox" class="mc-chk-ob" checked> 🟩 Order Blocks</label>
              <hr style="border-color:#444; margin:4px 0;">
              <label><input type="checkbox" class="mc-chk-rsi" checked> RSI (14)</label>
              <label><input type="checkbox" class="mc-chk-adx"> ADX</label>
              <label><input type="checkbox" class="mc-chk-atr"> ATR</label>
              <hr style="border-color:#444; margin:4px 0;">
              <label><input type="checkbox" class="mc-chk-signals" checked> 📈 CALL/PUT Signals</label>
              <label><input type="checkbox" class="mc-chk-beep" checked> 🔔 Alert Beep</label>
            </div>
          </details>
        </div>
        <button class="btn btn-close text-red" style="padding:2px 8px; border:1px solid #333;" onclick="multiChartManager.removeChart('${id}')">✖</button>
      </div>
      <div class="mc-chart-container" id="container-${id}" style="position:relative; flex:1; width:100%; min-height:0; overflow:hidden;"></div>
    `;

    this._grid.appendChild(box);
    this._updateGridLayout();

    // Chart Instance
    const container = document.getElementById(`container-${id}`);
    const chart = LightweightCharts.createChart(container, {
      autoSize: true,
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#B0BEC5' },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderVisible: false },
      leftPriceScale: { visible: false },
      // timeScale: timestamps are stored as "fake-UTC" IST wall-clock seconds
      // (see _toUnixTimestamp). LWC renders them as UTC labels → shows correct IST times.
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
        visible: true,
        tickMarkMaxCharacterLength: 5,
      },
      localization: {
        // Force display as HH:MM (UTC display = IST wall-clock with fake-UTC trick)
        timeFormatter: (ts) => {
          const d = new Date(ts * 1000); // ts is fake-UTC IST seconds
          const hh = String(d.getUTCHours()).padStart(2, '0');
          const mm = String(d.getUTCMinutes()).padStart(2, '0');
          return `${hh}:${mm}`;
        },
      },
    });

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#26A69A', downColor: '#EF5350', borderVisible: false,
      wickUpColor: '#26A69A', wickDownColor: '#EF5350',
    });

    const instanceObj = {
      id, symbol, targetDate: date, chart, candleSeries,
      lines: [], priceLines: [],
      volSeries: chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: '', scaleMargins: { top: 0.8, bottom: 0 } }),
      rsiSeries: chart.addLineSeries({ priceScaleId: 'oscillators', color: '#B388FF', lineWidth: 1, title: 'RSI', lastValueVisible: false, priceLineVisible: false }),
      adxSeries: chart.addLineSeries({ priceScaleId: 'oscillators', color: '#FFB74D', lineWidth: 1, title: 'ADX', lastValueVisible: false, priceLineVisible: false }),
      atrSeries: chart.addLineSeries({ priceScaleId: 'oscillators', color: '#4DD0E1', lineWidth: 1, title: 'ATR', lastValueVisible: false, priceLineVisible: false }),
      _liveInterval: null,  // live tracking poll timer
      // ── Per-chart signal state (persisted across live polls) ──────
      _signalState: {
        inCall: false, inPut: false,
        alertedTs: new Set(),
        lastCallPrice: 0, lastPutPrice: 0,
      },
      _zoneData: { fvgs: [], obs: [] },        // latest OB + FVG data for redraws
      _zoneUnsubscribe: null,                   // cleanup handle for scale listener
    };

    this._charts.push(instanceObj);

    // Event Listeners for controls
    box.querySelector('.mc-interval').addEventListener('change', () => {
      this._stopLiveTracking(instanceObj);
      this.loadDataForChart(instanceObj);
    });
    box.querySelectorAll('input[type="checkbox"]').forEach(chk => {
      chk.addEventListener('change', () => this.loadDataForChart(instanceObj, true));
    });

    // Subscribe to scale changes to redraw zone canvas on pan/zoom
    const zoneRedraw = () => this._drawZones(instanceObj);
    instanceObj.chart.timeScale().subscribeVisibleLogicalRangeChange(zoneRedraw);
    instanceObj._zoneUnsubscribe = () =>
      instanceObj.chart.timeScale().unsubscribeVisibleLogicalRangeChange(zoneRedraw);

    // Initial Load
    this.loadDataForChart(instanceObj);
  }

  removeChart(id) {
    const idx = this._charts.findIndex(c => c.id === id);
    if (idx >= 0) {
      const c = this._charts[idx];
      this._stopLiveTracking(c);
      if (c._zoneUnsubscribe) c._zoneUnsubscribe();
      this._clearZoneCanvas(c);
      c.chart.remove();
      this._charts.splice(idx, 1);
    }
    const box = document.getElementById(id);
    if (box) box.remove();
    this._updateGridLayout();
  }

  _updateGridLayout() {
    const count = this._charts.length;
    this._grid.className = 'multi-chart-grid';
    if (count === 1) this._grid.classList.add('layout-1x1');
    else if (count === 2) this._grid.classList.add('layout-1x2'); // side by side
    else if (count === 3 || count === 4) this._grid.classList.add('layout-2x2');
  }

  async loadDataForChart(cObj, isRefreshOnly = false, fromLive = false) {
    const box = document.getElementById(cObj.id);
    if (!box) return;

    const interval = box.querySelector('.mc-interval').value;

    let ohlcv = cObj.ohlcv; // cached

    if (!isRefreshOnly) {
      if (app.apiFetch) {
         try {
           const mappedSymbol = {'NIFTY':'NIFTY 50','BANKNIFTY':'NIFTY BANK','FINNIFTY':'NIFTY FIN SERVICE','SENSEX':'SENSEX'}[cObj.symbol.toUpperCase()] || cObj.symbol;
           const body = { symbol: mappedSymbol, interval, date: cObj.targetDate, price: 0 };
           const res = await app.apiFetch('/api/validate-entry', {
             method: 'POST', headers: { 'Content-Type': 'application/json' },
             body: JSON.stringify(body)
           });
           
           if (!res.ok) throw new Error('API fetching failed');
           const data = await res.json();
           ohlcv = data.candles || [];
           cObj.ohlcv = ohlcv;

           // Start live tracking if session is active
           this._startLiveTracking(cObj);
         } catch(e) {
           console.warn('Multichart fetch error:', e);
         }
      }
    }

    if (!ohlcv || ohlcv.length === 0) return;

    const intervalSecs = { '5minute': 300, '15minute': 900, '30minute': 1800, 'day': 86400, 'week': 604800 }[interval] || 300;
    const chartData = ohlcv.map(d => ({
      // Floor timestamp to the nearest interval boundary so candle labels
      // always show clean times (09:15, 09:20 … not 09:13, 14:13 etc.)
      time: this._floorToInterval(this._toUnixTimestamp(d.date), intervalSecs),
      open: d.open, high: d.high, low: d.low, close: d.close,
    })).filter(d => d.time);
    // Remove any duplicate timestamps that arise after flooring
    const seen = new Set();
    const dedupedChartData = chartData.filter(d => seen.has(d.time) ? false : (seen.add(d.time), true));

    cObj.candleSeries.setData(dedupedChartData);

    // Scroll the chart to the target session window (09:15–15:30 IST).
    // _toUnixTimestamp stores fake-UTC IST seconds, so session bounds must
    // also be fake-UTC: treat IST hours as if they were UTC hours in Date.UTC.
    if (chartData.length > 0 && cObj.targetDate) {
      try {
        const [yr, mo, dy] = cObj.targetDate.split('-').map(Number);
        // fake-UTC: 09:15 IST → Date.UTC(yr,mo-1,dy, 9,15,0)
        const sessionStart = Date.UTC(yr, mo - 1, dy,  9, 15, 0);
        const sessionEnd   = Date.UTC(yr, mo - 1, dy, 15, 31, 0);  // last 5m bar closes 15:30
        cObj.chart.timeScale().setVisibleRange({
          from: Math.floor(sessionStart / 1000),
          to:   Math.floor(sessionEnd   / 1000),
        });
      } catch(_) {
        cObj.chart.timeScale().fitContent();
      }
    }

    // Clean up previous series and lines
    cObj.lines.forEach(l => { try { cObj.chart.removeSeries(l); } catch(e){} });
    cObj.priceLines.forEach(pl => { try { cObj.candleSeries.removePriceLine(pl); } catch(e){} });
    cObj.lines = [];
    cObj.priceLines = [];

    const closes = ohlcv.map(d => d.close);
    const highs = ohlcv.map(d => d.high);
    const lows = ohlcv.map(d => d.low);

    // Configuration
    const showVol = box.querySelector('.mc-chk-vol').checked;
    const showEMAConfig = box.querySelector('.mc-chk-sma').checked;
    const showEMA50 = box.querySelector('.mc-chk-ema50').checked;
    const showVWAP = box.querySelector('.mc-chk-vwap').checked;
    const showBB = box.querySelector('.mc-chk-bb').checked;
    const showCPR = box.querySelector('.mc-chk-cpr')?.checked ?? true;
    const showSMC = box.querySelector('.mc-chk-smc').checked;
    const showFVG = box.querySelector('.mc-chk-fvg').checked;
    const showRSI = box.querySelector('.mc-chk-rsi').checked;
    const showADX = box.querySelector('.mc-chk-adx').checked;
    const showATR = box.querySelector('.mc-chk-atr').checked;

    // Volume
    if (showVol && ohlcv[0].volume !== undefined) {
       const volData = ohlcv.map((d, i) => ({ time: chartData[i].time, value: d.volume, color: (d.close >= d.open) ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)' })).filter(d => d.time);
       cObj.volSeries.setData(volData);
    } else { cObj.volSeries.setData([]); }

    // Sub-Oscillators
    const assignOscillator = (series, display, computeFn, args) => {
       if (display) {
          const arr = computeFn(...args);
          series.setData(arr.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
       } else { series.setData([]); }
    };
    if (showRSI && typeof TI.computeRSIArray === 'function') {
        const rsiArr = TI.computeRSIArray(closes, 14);
        cObj.rsiSeries.setData(rsiArr.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
    } else { cObj.rsiSeries.setData([]); }

    // Compute ATR Array
    if (showATR) {
        let atrData = [];
        let lastAtr = null;
        for (let i = 0; i < closes.length; i++) {
           if (i===0) { atrData.push(null); continue; }
           const tr = Math.max(highs[i]-lows[i], Math.abs(highs[i]-closes[i-1]), Math.abs(lows[i]-closes[i-1]));
           if (i < 14) {
             lastAtr = (lastAtr || 0) + tr;
             if (i === 13) { lastAtr = lastAtr/14; atrData.push(lastAtr); }
             else atrData.push(null);
           } else {
             lastAtr = ((lastAtr * 13) + tr) / 14;
             atrData.push(lastAtr);
           }
        }
        cObj.atrSeries.setData(atrData.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
    } else { cObj.atrSeries.setData([]); }

    // Compute ADX Array
    if (showADX) {
        let adxData = [];
        let plusDMArr = [], minusDMArr = [], trArr = [];
        for (let i=0; i<closes.length; i++) {
            if(i===0){ trArr.push(0); plusDMArr.push(0); minusDMArr.push(0); continue; }
            const up = highs[i]-highs[i-1], down = lows[i-1]-lows[i];
            plusDMArr.push((up > down && up > 0) ? up : 0);
            minusDMArr.push((down > up && down > 0) ? down : 0);
            trArr.push(Math.max(highs[i]-lows[i], Math.abs(highs[i]-closes[i-1]), Math.abs(lows[i]-closes[i-1])));
        }
        let smoothedTR = [], smoothedPlusDM = [], smoothedMinusDM = [], dx = [];
        let lastTR=0, lastPDM=0, lastMDM=0, lastADX=null;
        for (let i=0; i<closes.length; i++) {
            if(i===0){ smoothedTR.push(0); smoothedPlusDM.push(0); smoothedMinusDM.push(0); dx.push(0); adxData.push(null); continue; }
            if (i <= 14) {
               lastTR += trArr[i]; lastPDM += plusDMArr[i]; lastMDM += minusDMArr[i];
               smoothedTR.push(lastTR); smoothedPlusDM.push(lastPDM); smoothedMinusDM.push(lastMDM);
            } else {
               lastTR = lastTR - (lastTR/14) + trArr[i];
               lastPDM = lastPDM - (lastPDM/14) + plusDMArr[i];
               lastMDM = lastMDM - (lastMDM/14) + minusDMArr[i];
               smoothedTR.push(lastTR); smoothedPlusDM.push(lastPDM); smoothedMinusDM.push(lastMDM);
            }
            if (i >= 14 && lastTR > 0) {
               const pdi = 100 * (lastPDM / lastTR);
               const mdi = 100 * (lastMDM / lastTR);
               const curDX = 100 * Math.abs(pdi - mdi) / (pdi + mdi || 1);
               dx.push(curDX);
               if (i === 27) {
                 lastADX = dx.slice(14, 28).reduce((a,b)=>a+b,0)/14;
                 adxData.push(lastADX);
               } else if (i > 27) {
                 lastADX = ((lastADX * 13) + curDX) / 14;
                 adxData.push(lastADX);
               } else { adxData.push(null); }
            } else { dx.push(0); adxData.push(null); }
        }
        cObj.adxSeries.setData(adxData.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time));
    } else { cObj.adxSeries.setData([]); }

    const addLine = (dataArr, color, title) => {
        const line = cObj.chart.addLineSeries({ color, lineWidth: 2, title, crosshairMarkerVisible: false });
        const lineData = dataArr.map((v, i) => v != null ? { time: chartData[i]?.time, value: v } : null).filter(d => d && d.time);
        line.setData(lineData);
        cObj.lines.push(line);
    };

    if (showEMAConfig) {
       addLine(TI.computeEMA(closes, 9), '#FF9800', 'EMA 9');
       addLine(TI.computeEMA(closes, 21), '#AB47BC', 'EMA 21');
    }
    if (showEMA50) {
       addLine(TI.computeEMA(closes, 50), '#78909C', 'EMA 50');
    }
    if (showVWAP) {
       addLine(TI.computeIntradayVWAP(ohlcv), '#FFD54F', 'VWAP');
    }
    if (showBB) {
       const bb = TI.computeBollingerBands(closes);
       addLine(bb.map(d => d ? d.upper : null), 'rgba(38, 166, 154, 0.4)', 'BB Upper');
       addLine(bb.map(d => d ? d.lower : null), 'rgba(38, 166, 154, 0.4)', 'BB Lower');
    }
    if (showCPR) {
       const targetDayStr = cObj.targetDate;
       const sessionDays = [];
       ohlcv.forEach(c => {
         const dStr = c.date ? c.date.slice(0, 10) : '';
         if (dStr && dStr < targetDayStr && !sessionDays.includes(dStr)) {
           sessionDays.push(dStr);
         }
       });
       sessionDays.sort();
       const prevSessionDay = sessionDays[sessionDays.length - 1];
       if (prevSessionDay) {
         const prevCandles = ohlcv.filter(c => c.date && c.date.startsWith(prevSessionDay));
         if (prevCandles.length > 0) {
           const prevHighs = prevCandles.map(c => c.high);
           const prevLows = prevCandles.map(c => c.low);
           const pdh = Math.max(...prevHighs);
           const pdl = Math.min(...prevLows);
           const pdc = prevCandles[prevCandles.length - 1].close;

           const pivot = (pdh + pdl + pdc) / 3.0;
           let bc = (pdh + pdl) / 2.0;
           let tc = (2.0 * pivot) - bc;
           if (tc < bc) {
             const temp = tc;
             tc = bc;
             bc = temp;
           }

           const bcLine = cObj.candleSeries.createPriceLine({
               price: bc,
               color: 'rgba(147, 51, 234, 0.85)',
               lineWidth: 2,
               lineStyle: 0, // Solid
               axisLabelVisible: true,
               title: `BC: ${bc.toFixed(1)}`
           });
           const tcLine = cObj.candleSeries.createPriceLine({
               price: tc,
               color: 'rgba(147, 51, 234, 0.85)',
               lineWidth: 2,
               lineStyle: 0, // Solid
               axisLabelVisible: true,
               title: `TC: ${tc.toFixed(1)}`
           });
           cObj.priceLines.push(bcLine);
           cObj.priceLines.push(tcLine);
           const pdhLine = cObj.candleSeries.createPriceLine({
               price: pdh,
               color: "#2e7d32", // Dark Green
               lineWidth: 1.5,
               lineStyle: 0, // Solid
               axisLabelVisible: true,
               title: `PDH: ${pdh.toFixed(1)}`
           });
           const pdlLine = cObj.candleSeries.createPriceLine({
               price: pdl,
               color: "#c62828", // Dark Red
               lineWidth: 1.5,
               lineStyle: 0, // Solid
               axisLabelVisible: true,
               title: `PDL: ${pdl.toFixed(1)}`
           });
           cObj.priceLines.push(pdhLine);
           cObj.priceLines.push(pdlLine);
         }
       }
    }


    const showSignals = box.querySelector('.mc-chk-signals')?.checked ?? true;

    // ── Collect all markers: SMC + Entry signals ──────────────────
    let allMarkers = [];

    // ── Always compute SMC if SMC visible OR signals enabled ──────
    // smcMarkersRaw is the raw-date array (for CHoCH detection in signals)
    let smcMarkersRaw = [];
    if ((showSMC || showFVG || showSignals) && typeof TI.computeSMC === 'function') {
        // ── SESSION FILTER: restrict OB/FVG detection to target date only ──
        // The API returns multi-day data; OBs from previous sessions must NOT
        // carry forward. Filter to today before all zone computation.
        const targetDate = cObj.targetDate || TI._extractDate(ohlcv[ohlcv.length - 1]?.date);
        const sessionOhlcv = TI.filterSessionByDate(ohlcv, targetDate, 10);

        const smc = TI.computeSMC(sessionOhlcv, 5);
        smcMarkersRaw = smc.markers; // keep raw-date version for signal detection

        if (showSMC) {
            smc.markers.forEach(m => {
              const t = this._toUnixTimestamp(m.time);
              if (t) allMarkers.push({ time: t, position: m.position, color: m.color, shape: m.shape, text: m.text });
            });
        }

        const visibleSR = showSMC ? smc.srLines.slice(-6) : [];
        visibleSR.forEach(sr => {
          const isRes = sr.type === 'RESISTANCE';
          cObj.priceLines.push(cObj.candleSeries.createPriceLine({
              price: sr.price, color: isRes ? 'rgba(239,83,80,0.5)' : 'rgba(38,166,154,0.5)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false
          }));
        });

        const showOB  = box.querySelector('.mc-chk-ob')?.checked ?? true;
        const interval = box.querySelector('.mc-interval')?.value || '5minute';

        // ── Step 1: Compute 5-min OBs + FVGs (session candles only) ──
        const raw5OB  = showOB  ? this._computeOrderBlocks(sessionOhlcv, smc.markers) : [];
        const raw5FVG = showFVG ? this._computeFVGsFiltered(sessionOhlcv, smc.markers) : [];

        // ── Step 2: HTF (15-min) zones — also session-filtered ────────
        let htfObs = [], htfFvgs = [];
        if (interval === '5minute' && sessionOhlcv.length >= 9) {
          const ohlcv15 = this._resampleTo15min(sessionOhlcv);
          const smc15   = typeof TI.computeSMC === 'function' ? TI.computeSMC(ohlcv15, 5) : { markers: [] };
          htfObs  = showOB  ? this._computeOrderBlocks(ohlcv15, smc15.markers) : [];
          htfFvgs = showFVG ? this._computeFVGsFiltered(ohlcv15, smc15.markers) : [];
        }

        // ── Step 3: Filter 5-min zones to inside HTF zones ────────────
        // Spec: "5-Min OB must sit INSIDE 15-Min OB or FVG zone. If outside → IGNORE IT"
        const htfZones = [...htfObs, ...htfFvgs.map(f => ({ high: f.top, low: f.bottom, type: f.type === 1 ? 'bullish' : 'bearish' }))];
        const insideHTF = (zone) => {
          if (htfZones.length === 0) return true; // no HTF zones = no filter
          return htfZones.some(h => zone.high >= h.low && zone.low <= h.high);
        };
        const visibleOB  = raw5OB.filter(ob  => insideHTF(ob));
        const visibleFVG = raw5FVG.filter(fvg => insideHTF({ high: fvg.top, low: fvg.bottom }));

        // Store zone data for redraw on pan/zoom, then draw
        cObj._zoneData = { fvgs: visibleFVG, obs: visibleOB, htfObs, htfFvgs };
        this._drawZones(cObj);
    } else {
      // SMC/zones disabled — clear any existing zone canvas
      this._clearZoneCanvas(cObj);
    }

    // ── CALL / PUT entry-exit signals (session candles only) ───────
    if (showSignals) {
      // sessionOhlcv is defined inside the SMC block; fall back to ohlcv if signals-only mode
      const sigOhlcv = (typeof sessionOhlcv !== 'undefined') ? sessionOhlcv : ohlcv;
      const sigMarkers = this._computeEntrySignals(sigOhlcv, chartData, smcMarkersRaw);
      allMarkers = allMarkers.concat(sigMarkers);
      // Fire beep + toast on new signals during live streaming
      if (fromLive) this._checkLiveSignalAlerts(cObj, sigMarkers);
    }

    // Render all markers sorted by time
    allMarkers.sort((a, b) => a.time - b.time);
    cObj.candleSeries.setMarkers(allMarkers);
  }

  // ── Order Block Detection ─────────────────────────────────────
  // Find last red candle before each bullish BOS/CHoCH → Bullish OB (blue)
  // Find last green candle before each bearish BOS/CHoCH → Bearish OB (orange)
  // Show most recent 2 per direction regardless of later mitigation.
  _computeOrderBlocks(ohlcv, smcMarkers) {
    if (!ohlcv || ohlcv.length < 10 || !smcMarkers || !smcMarkers.length) return [];
    const closes = ohlcv.map(d => d.close);
    const opens  = ohlcv.map(d => d.open);
    const highs  = ohlcv.map(d => d.high);
    const lows   = ohlcv.map(d => d.low);
    const bullObs = [], bearObs = [];

    // Filter only BOS/CHOCH markers
    const bosMarkers = smcMarkers.filter(m => m.text === 'BOS' || m.text === 'CHOCH');

    bosMarkers.forEach(m => {
      const isBull = m.color === '#26A69A';
      const mDate  = (m.time || '').slice(0, 16);
      const mIdx   = ohlcv.findIndex(d => (d.date || '').slice(0, 16) === mDate);
      if (mIdx < 2) return;

      if (isBull) {
        // Look back for last red candle
        for (let k = mIdx - 1; k >= Math.max(0, mIdx - 20); k--) {
          if (closes[k] < opens[k]) {
            bullObs.push({
              type: 'bullish',
              high: highs[k], low: lows[k],
              mid: (highs[k] + lows[k]) / 2,
              startTime: ohlcv[k].date,
            });
            break;
          }
        }
      } else {
        // Look back for last green candle
        for (let k = mIdx - 1; k >= Math.max(0, mIdx - 20); k--) {
          if (closes[k] > opens[k]) {
            bearObs.push({
              type: 'bearish',
              high: highs[k], low: lows[k],
              mid: (highs[k] + lows[k]) / 2,
              startTime: ohlcv[k].date,
            });
            break;
          }
        }
      }
    });

    // Deduplicate by startTime, show most recent 2 per direction
    const dedup = arr => {
      const seen = new Set();
      return arr.filter(o => seen.has(o.startTime) ? false : (seen.add(o.startTime), true));
    };
    const result = [...dedup(bullObs).slice(-2), ...dedup(bearObs).slice(-2)];
    return result;
  }

  // ── FVG Detection ─────────────────────────────────────────────
  // 3-candle pattern: C1 (i-1) | C2 impulse (i) | C3 (i+1)
  // Bullish FVG : C3.low > C1.high  → top=C3.low, bot=C1.high (gap below price)
  // Bearish FVG : C3.high < C1.low  → top=C1.low, bot=C3.high (gap above price)
  // Filters per spec:
  //   ✅ After BOS/CHoCH (within ±5 bars)
  //   ✅ Impulse candle ≥ 2× avg candle size
  //   ✅ Gap ≥ 0.3% of price
  //   ✅ Not 50%+ filled within next 20 bars
  //   ✅ Max 1 FVG per direction shown (most recent unmitigated)
  //   ✅ startTime = C1's timestamp (box starts where gap began)
  _computeFVGsFiltered(ohlcv, smcMarkers) {
    if (!ohlcv || ohlcv.length < 5) return [];
    const highs  = ohlcv.map(d => d.high);
    const lows   = ohlcv.map(d => d.low);
    const closes = ohlcv.map(d => d.close);
    const avgCandle = ohlcv.reduce((s, d) => s + (d.high - d.low), 0) / ohlcv.length;
    const FILL_WINDOW = 20;

    // Map BOS/CHOCH marker positions → direction
    const bosMap = new Map();
    (smcMarkers || []).forEach(m => {
      if (m.text !== 'BOS' && m.text !== 'CHOCH') return;
      const mDate = (m.time || '').slice(0, 16);
      const idx   = ohlcv.findIndex(d => (d.date || '').slice(0, 16) === mDate);
      if (idx >= 0) bosMap.set(idx, m.color === '#26A69A' ? 'bull' : 'bear');
    });

    const bullFvgs = [], bearFvgs = [];

    // i = C2 (impulse candle); C1 = i-1; C3 = i+1
    for (let i = 1; i < ohlcv.length - 1; i++) {
      const impulseSize = highs[i] - lows[i];

      // ── Impulse filter: C2 must be at least 2× average candle ────
      if (impulseSize < 2 * avgCandle) continue;

      // ── BOS proximity: must be within ±5 bars of a BOS/CHoCH ─────
      let nearDir = null;
      for (const [bIdx, dir] of bosMap) {
        if (Math.abs(i - bIdx) <= 5) { nearDir = dir; break; }
      }
      if (!nearDir) continue;

      const midPrice = closes[i]; // reference price for gap %
      const minGap   = midPrice * 0.003; // 0.3% minimum gap size

      // ── Bullish FVG: C3.low > C1.high ─────────────────────────────
      if (nearDir === 'bull') {
        const gap = lows[i + 1] - highs[i - 1]; // gap between C1.high and C3.low
        if (gap >= minGap) {
          const top = lows[i + 1], bot = highs[i - 1], mid = (top + bot) / 2;
          // Skip if 50%+ filled within next 20 bars
          const filled = ohlcv.slice(i + 2, i + 2 + FILL_WINDOW).some(d => d.low <= mid);
          if (!filled) {
            bullFvgs.push({
              type: 1, top, bottom: bot, mid,
              startTime: ohlcv[i - 1].date, // ← C1: where the gap began
            });
          }
        }
      }

      // ── Bearish FVG: C3.high < C1.low ─────────────────────────────
      if (nearDir === 'bear') {
        const gap = lows[i - 1] - highs[i + 1]; // gap between C3.high and C1.low
        if (gap >= minGap) {
          const top = lows[i - 1], bot = highs[i + 1], mid = (top + bot) / 2;
          const filled = ohlcv.slice(i + 2, i + 2 + FILL_WINDOW).some(d => d.high >= mid);
          if (!filled) {
            bearFvgs.push({
              type: -1, top, bottom: bot, mid,
              startTime: ohlcv[i - 1].date, // ← C1: where the gap began
            });
          }
        }
      }
    }

    // Spec: max 1 FVG per direction (most recent unmitigated)
    // Combined max 2 zones total matches "1-2 FVG boxes maximum"
    const lastBull = bullFvgs.slice(-1);
    const lastBear = bearFvgs.slice(-1);
    return [...lastBull, ...lastBear];
  }

  // ── 15-min resampling from 5-min bars (for HTF overlay) ───────
  // Groups bars by actual wall-clock 15-min bucket (floor to :00, :15, :30, :45)
  // so the resulting timestamps are always clean 15-min boundaries.
  _resampleTo15min(ohlcv5) {
    const buckets = new Map(); // key = ISO 15-min bucket string, value = candle array
    for (const c of ohlcv5) {
      const ts = this._toUnixTimestamp(c.date);
      if (!ts) continue;
      // Compute floor to 15-min boundary in seconds
      const bucketTs = Math.floor(ts / 900) * 900;
      if (!buckets.has(bucketTs)) buckets.set(bucketTs, []);
      buckets.get(bucketTs).push(c);
    }
    const result = [];
    for (const [, sl] of [...buckets.entries()].sort((a, b) => a[0] - b[0])) {
      if (!sl.length) continue;
      result.push({
        date:   sl[0].date,  // original date string for compatibility
        open:   sl[0].open,
        high:   Math.max(...sl.map(c => c.high)),
        low:    Math.min(...sl.map(c => c.low)),
        close:  sl[sl.length - 1].close,
        volume: sl.reduce((s, c) => s + (c.volume || 0), 0),
      });
    }
    return result;
  }

  // ── Zone Canvas Drawing ───────────────────────────────────────
  // OBs: Blue (bull) / Orange (bear), 15% fill
  // FVGs: Green (bull) / Red (bear), 12% fill
  // 50% midline drawn dotted inside each zone
  // On 5-min charts: also draws HTF (15-min derived) zones as grey bands
  _drawZones(cObj) {
    const { fvgs = [], obs = [], htfObs = [], htfFvgs = [] } = cObj._zoneData || {};
    if (!fvgs.length && !obs.length && !htfObs.length && !htfFvgs.length) {
      this._clearZoneCanvas(cObj); return;
    }

    const container = document.getElementById(`container-${cObj.id}`);
    if (!container) return;

    let canvas = container.querySelector('.mc-zone-canvas');
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.className = 'mc-zone-canvas';
      canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2;';
      container.insertBefore(canvas, container.firstChild);
    }

    const dpr  = window.devicePixelRatio || 1;
    const rect = container.getBoundingClientRect();
    canvas.width  = rect.width  * dpr;
    canvas.height = rect.height * dpr;
    canvas.style.width  = rect.width  + 'px';
    canvas.style.height = rect.height + 'px';

    const ctx    = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.scale(dpr, dpr);

    const chart  = cObj.chart;
    const series = cObj.candleSeries;
    const ts     = chart.timeScale();
    const ohlcv  = cObj.ohlcv || [];
    const lastTs = ohlcv.length ? ohlcv[ohlcv.length - 1].date : null;
    if (!lastTs) return;

    const W = rect.width;
    const currentPrice = ohlcv.length ? ohlcv[ohlcv.length - 1].close : 0;
    const PROXIMITY = 0.05; // HTF zones further than 5% from price are irrelevant

    // ── Shared pill label helper ──────────────────────────────────
    const drawPill = (txt, px, py) => {
      if (!txt) return;
      ctx.font = 'bold 9px Inter,sans-serif';
      const tw = ctx.measureText(txt).width;
      ctx.fillStyle = 'rgba(0,0,0,0.55)';
      ctx.beginPath();
      ctx.roundRect(px, py - 9, tw + 6, 12, 3);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.fillText(txt, px + 3, py);
    };

    // ── Full-width band helper (for HTF reference zones) ──────────
    // HTF zones span full chart width — they're directional bias levels.
    const drawBand = (topPrice, botPrice, fillColor, borderColor, midPrice, label) => {
      if (currentPrice > 0) {
        const avg = (topPrice + botPrice) / 2;
        if (Math.abs(avg - currentPrice) / currentPrice > PROXIMITY) return;
      }
      const y1 = series.priceToCoordinate(topPrice);
      const y2 = series.priceToCoordinate(botPrice);
      if (y1 == null || y2 == null) return;
      const ry = Math.min(y1, y2), rh = Math.abs(y2 - y1);
      if (rh < 1) return;
      ctx.fillStyle = fillColor;
      ctx.fillRect(0, ry, W, rh);
      ctx.strokeStyle = borderColor;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([]);
      ctx.strokeRect(0.5, ry + 0.5, W - 1, rh - 1);
      if (midPrice != null) {
        const my = series.priceToCoordinate(midPrice);
        if (my != null) {
          ctx.beginPath(); ctx.setLineDash([4, 3]);
          ctx.strokeStyle = borderColor; ctx.lineWidth = 1;
          ctx.moveTo(0, my); ctx.lineTo(W, my);
          ctx.stroke(); ctx.setLineDash([]);
        }
      }
      if (label && rh > 10) {
        drawPill(label[0], 4, ry + 10);
        if (midPrice != null && rh > 22) {
          const my2 = series.priceToCoordinate(midPrice);
          if (my2 != null) drawPill(label[1] || '50%', 4, my2 + 4);
        }
      }
    };

    // ── Core draw helper ─────────────────────────────────────────
    const drawZone = (startTime, topPrice, botPrice, fillColor, borderColor, midPrice, label, dashed) => {
      let x1 = ts.timeToCoordinate(this._toUnixTimestamp(startTime));
      const x2 = ts.timeToCoordinate(this._toUnixTimestamp(lastTs));
      const y1 = series.priceToCoordinate(topPrice);
      const y2 = series.priceToCoordinate(botPrice);
      // x1 null = zone started before visible range → clamp to left canvas edge
      if (x1 == null || x1 < 0) x1 = 0;
      if (x2 == null || y1 == null || y2 == null) return;
      const rx = x1;                            // start from OB candle (or left edge if before view)
      const ry = Math.min(y1, y2);
      const rw = (x2 - rx) + 8;               // extend 8px past last candle
      const rh = Math.abs(y2 - y1);
      if (rw < 2 || rh < 1) return;

      // Fill
      ctx.fillStyle = fillColor;
      ctx.fillRect(rx, ry, rw, rh);

      // Border (dashed for 5-min OBs inside 15-min zone)
      ctx.strokeStyle = borderColor;
      ctx.lineWidth = dashed ? 1 : 1.5;
      if (dashed) ctx.setLineDash([4, 3]); else ctx.setLineDash([]);
      ctx.strokeRect(rx + 0.5, ry + 0.5, rw - 1, rh - 1);
      ctx.setLineDash([]);

      // 50% midline (dotted)
      if (midPrice != null) {
        const my = series.priceToCoordinate(midPrice);
        if (my != null) {
          ctx.beginPath();
          ctx.setLineDash([3, 3]);
          ctx.strokeStyle = borderColor;
          ctx.lineWidth = 1;
          ctx.moveTo(rx, my);
          ctx.lineTo(rx + rw, my);
          ctx.stroke();
          ctx.setLineDash([]);
        }
      }

      // Inside-zone pill labels using shared drawPill helper
      if (label && rh > 10) {
        drawPill(label[0], rx + 4, ry + 10);
        if (midPrice != null && rh > 22) {
          const my2 = series.priceToCoordinate(midPrice);
          if (my2 != null) drawPill(label[1] || '50%', rx + 4, my2 + 4);
        }
        if (rh > 30 && label[2]) drawPill(label[2], rx + 4, ry + rh - 4);
      }
    };

    // ── Layer 1: HTF reference band — ONE closest zone, always grey ─
    // Spec: "15-Min OB/FVG zone shaded as reference (grey band)"
    // Only draw the single HTF zone closest to current price.
    const allHTF = [
      ...htfObs.map(z  => ({ top: z.high, bot: z.low,  mid: z.mid, label: z.type === 'bullish' ? 'HTF OB▲' : 'HTF OB▼' })),
      ...htfFvgs.map(z => ({ top: z.top,  bot: z.bottom, mid: z.mid, label: 'HTF FVG' })),
    ].filter(z => {
      // Proximity: skip zones further than 5% from current price
      if (!currentPrice) return true;
      const mid = (z.top + z.bot) / 2;
      return Math.abs(mid - currentPrice) / currentPrice <= 0.05;
    }).sort((a, b) => {
      // Sort by distance to current price — closest first
      const da = Math.abs((a.top + a.bot) / 2 - currentPrice);
      const db = Math.abs((b.top + b.bot) / 2 - currentPrice);
      return da - db;
    });

    // Draw max 2 closest HTF zones as neutral grey bands
    allHTF.slice(0, 2).forEach(z => {
      drawBand(z.top, z.bot,
        'rgba(150,150,170,0.10)',   // very light grey fill
        'rgba(120,120,150,0.55)',   // grey border
        z.mid, [z.label, '50%']);
    });

    // ── Layer 2: 5-min FVGs (candle-anchored, colored) ───────────
    // Spec: Green=bull 20% fill, Red=bear 20% fill, starts at impulse candle
    fvgs.forEach(fvg => {
      const bull = fvg.type === 1;
      drawZone(fvg.startTime, fvg.top, fvg.bottom,
        bull ? 'rgba(38,166,154,0.20)' : 'rgba(239,83,80,0.20)',
        bull ? 'rgba(38,166,154,0.85)' : 'rgba(239,83,80,0.85)',
        fvg.mid, [bull ? 'FVG▲' : 'FVG▼', 'EQ', ''], false);
    });

    // ── Layer 3: 5-min OBs (candle-anchored, boldest) ────────────
    // Spec: Blue=bull 15% fill, Orange=bear 15% fill, 50% dotted midline
    obs.forEach(ob => {
      const bull = ob.type === 'bullish';
      drawZone(ob.startTime, ob.high, ob.low,
        bull ? 'rgba(41,182,246,0.15)' : 'rgba(255,152,0,0.15)',
        bull ? 'rgba(41,182,246,0.90)' : 'rgba(255,152,0,0.90)',
        ob.mid,
        [bull ? `OB▲ ${ob.high.toFixed(1)}` : `OB▼ ${ob.high.toFixed(1)}`,
         '50%', ob.low.toFixed(1)],
        false);
    });
  }

  _clearZoneCanvas(cObj) {
    const container = document.getElementById(`container-${cObj.id}`);
    if (!container) return;
    const canvas = container.querySelector('.mc-zone-canvas');
    if (canvas) canvas.remove();
    if (cObj._zoneData) cObj._zoneData = { fvgs: [], obs: [] };
  }

  _toUnixTimestamp(date) {
    if (!date) return null;
    if (typeof date === 'number') return date > 9999999999 ? Math.floor(date / 1000) : date;
    // Strategy: "fake-UTC" trick.
    // Kite returns IST ISO strings like "2026-04-30T09:15:00+05:30".
    // We strip the timezone and treat the IST wall-clock digits as UTC,
    // so LWC renders "09:15" on the axis (UTC display = IST wall-clock).
    if (typeof date === 'string') {
      // Match YYYY-MM-DDTHH:MM:SS with any offset or Z
      const m = date.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
      if (m) {
        const [, yr, mo, dy, hh, mm, ss = '00'] = m;
        // Build as UTC so Date.UTC(yr,mo-1,dy,hh,mm,ss) = IST wall-clock fake-UTC
        return Math.floor(Date.UTC(+yr, +mo - 1, +dy, +hh, +mm, +ss) / 1000);
      }
    }
    const dt = new Date(date);
    if (isNaN(dt.getTime())) return null;
    // Fallback: add IST offset to shift UTC to IST wall-clock
    return Math.floor(dt.getTime() / 1000) + 19800;
  }

  // ── Floor a fake-UTC timestamp (seconds) to nearest interval boundary ──
  // e.g. 14:13 with 5-min → 14:10; 14:13 with 15-min → 14:00
  _floorToInterval(ts, intervalSecs) {
    if (!ts) return null;
    return Math.floor(ts / intervalSecs) * intervalSecs;
  }

  // ── Entry / Exit Signal Detection ──────────────────────────────
  // DUAL-TRIGGER model:
  //
  //  CALL ENTRY fires when EITHER:
  //    A) EMA Cross  : EMA9 crossed above EMA21 within last 2 bars
  //                   + close > VWAP + RSI 42–68 + green candle
  //    B) CHoCH/BOS  : Bullish CHoCH or BOS detected by SMC at this bar
  //                   + EMA9 rising (ema9[i] > ema9[i-3])
  //                   + RSI < 70 + not in any trade
  //
  //  PUT ENTRY fires when EITHER:
  //    A) EMA Cross  : EMA9 crossed below EMA21 within last 2 bars
  //                   + close < VWAP + RSI 32–58 + red candle
  //    B) CHoCH/BOS  : Bearish CHoCH or BOS at this bar
  //                   + EMA9 falling (ema9[i] < ema9[i-3])
  //                   + RSI > 30 + not in any trade
  //
  //  EXIT : opposite EMA cross while in a trade
  //
  _computeEntrySignals(ohlcv, chartData, smcMarkersRaw = []) {
    if (!ohlcv || ohlcv.length < 22) return [];
    const closes  = ohlcv.map(d => d.close);
    const opens   = ohlcv.map(d => d.open);
    const volumes = ohlcv.map(d => d.volume || 0);
    const vwap    = (typeof TI.computeIntradayVWAP === 'function') ? TI.computeIntradayVWAP(ohlcv) : [];
    const ema9    = TI.computeEMA(closes, 9);
    const ema21   = TI.computeEMA(closes, 21);
    const rsi     = (typeof TI.computeRSIArray === 'function') ? TI.computeRSIArray(closes, 14) : [];

    // Average volume for relative volume filter
    const avgVol  = volumes.length > 10
      ? volumes.slice(1, -1).reduce((a, b) => a + b, 0) / Math.max(1, volumes.length - 2)
      : 0;

    // ── Map SMC CHoCH / BOS events to candle indices ──────────────
    // smcMarkersRaw use raw date strings; convert to fake-UTC and match chartData
    const smcBullIdx = new Set(); // indices of bullish CHoCH/BOS candles
    const smcBearIdx = new Set(); // indices of bearish CHoCH/BOS candles
    smcMarkersRaw.forEach(m => {
      const isStructure = (m.text === 'CHOCH' || m.text === 'BOS');
      if (!isStructure) return;
      const ts  = this._toUnixTimestamp(m.time);
      const idx = chartData.findIndex(c => c.time === ts);
      if (idx < 0) return;
      if (m.color === '#26A69A') smcBullIdx.add(idx); // green = bullish
      if (m.color === '#EF5350') smcBearIdx.add(idx); // red   = bearish
    });

    const markers = [];
    let inCall = false, inPut = false;
    let entryPrice = 0, entrySL = 0, entryTarget = 0;
    // Track last cross candle index for follow-through window
    let lastBullCrossIdx = -99, lastBearCrossIdx = -99;

    // ── ATR array (for dynamic SL/target) ────────────────────────
    const highs = ohlcv.map(d => d.high);
    const lows  = ohlcv.map(d => d.low);
    const atrArr = [];
    let lastAtr = null;
    for (let k = 0; k < closes.length; k++) {
      if (k === 0) { atrArr.push(null); continue; }
      const tr = Math.max(highs[k]-lows[k], Math.abs(highs[k]-closes[k-1]), Math.abs(lows[k]-closes[k-1]));
      if (k < 14) {
        lastAtr = (lastAtr || 0) + tr;
        if (k === 13) { lastAtr /= 14; atrArr.push(lastAtr); }
        else atrArr.push(null);
      } else {
        lastAtr = ((lastAtr * 13) + tr) / 14;
        atrArr.push(lastAtr);
      }
    }

    for (let i = 22; i < ohlcv.length; i++) {
      const t   = chartData[i]?.time;
      if (!t) continue;
      const e9   = ema9[i],  e9p  = ema9[i-1];
      const e21  = ema21[i], e21p = ema21[i-1];
      const r    = rsi[i];
      const v    = vwap[i];
      const cls  = closes[i];
      const opn  = opens[i];
      const vol  = volumes[i];
      if (e9 == null || e21 == null || e9p == null || e21p == null) continue;

      // ── Cross detection ───────────────────────────────────────────
      const crossUp   = (e9p <= e21p) && (e9 > e21);  // exact cross bar
      const crossDown = (e9p >= e21p) && (e9 < e21);
      if (crossUp)   { lastBullCrossIdx = i; lastBearCrossIdx = -99; } // reset opposite
      if (crossDown) { lastBearCrossIdx = i; lastBullCrossIdx = -99; }

      // Within-2-bar follow-through window (catches cross + next 2 confirmation candles)
      const recentBullCross = (i - lastBullCrossIdx) <= 2 && lastBullCrossIdx >= 0;
      const recentBearCross = (i - lastBearCrossIdx) <= 2 && lastBearCrossIdx >= 0;

      const aboveVwap  = v != null ? cls > v  : true;
      const belowVwap  = v != null ? cls < v  : true;
      const rsiCallOk  = r == null || (r > 42 && r < 70); // bullish RSI zone
      const rsiPutOk   = r == null || (r > 30 && r < 58); // bearish RSI zone
      const bullBody   = cls > opn;  // green candle
      const bearBody   = cls < opn;  // red candle
      const volSurge   = avgVol > 0 ? vol >= avgVol * 0.7 : true; // ≥70% avg vol

      // EMA9 slope — rising/falling over last 3 bars
      const ema9Rising  = i >= 3 && ema9[i]  != null && ema9[i-3]  != null && ema9[i]  > ema9[i-3];
      const ema9Falling = i >= 3 && ema9[i]  != null && ema9[i-3]  != null && ema9[i]  < ema9[i-3];

      // ── SMC CHoCH/BOS trigger flags ──────────────────────────────
      const bullChoch = smcBullIdx.has(i);
      const bearChoch = smcBearIdx.has(i);

      // ── EXIT signals — take priority ──────────────────────────────
      // 1. EMA cross exit
      if (inCall && crossDown) {
        markers.push({ time: t, position: 'aboveBar', color: '#FF5252', shape: 'arrowDown', text: `EXIT CALL(EMA) @${cls.toFixed(0)}` });
        inCall = false; entryPrice = entrySL = entryTarget = 0;
      }
      if (inPut && crossUp) {
        markers.push({ time: t, position: 'belowBar', color: '#00E676', shape: 'arrowUp', text: `EXIT PUT(EMA) @${cls.toFixed(0)}` });
        inPut = false; entryPrice = entrySL = entryTarget = 0;
      }

      // 2. ATR-based SL hit
      if (inCall && entrySL > 0 && cls < entrySL) {
        markers.push({ time: t, position: 'aboveBar', color: '#FF1744', shape: 'arrowDown', text: `SL HIT @${cls.toFixed(0)}` });
        inCall = false; entryPrice = entrySL = entryTarget = 0;
      }
      if (inPut && entrySL > 0 && cls > entrySL) {
        markers.push({ time: t, position: 'belowBar', color: '#FF1744', shape: 'arrowUp', text: `SL HIT @${cls.toFixed(0)}` });
        inPut = false; entryPrice = entrySL = entryTarget = 0;
      }

      // 3. ATR-based Target hit
      if (inCall && entryTarget > 0 && cls >= entryTarget) {
        markers.push({ time: t, position: 'aboveBar', color: '#FFD600', shape: 'arrowDown', text: `TARGET @${cls.toFixed(0)}` });
        inCall = false; entryPrice = entrySL = entryTarget = 0;
      }
      if (inPut && entryTarget > 0 && cls <= entryTarget) {
        markers.push({ time: t, position: 'belowBar', color: '#FFD600', shape: 'arrowUp', text: `TARGET @${cls.toFixed(0)}` });
        inPut = false; entryPrice = entrySL = entryTarget = 0;
      }

      // 4. RSI extreme exit (overbought/oversold bail-out)
      if (inCall && r != null && r > 78) {
        markers.push({ time: t, position: 'aboveBar', color: '#FF9800', shape: 'arrowDown', text: `EXIT CALL(RSI OB) @${cls.toFixed(0)}` });
        inCall = false; entryPrice = entrySL = entryTarget = 0;
      }
      if (inPut && r != null && r < 22) {
        markers.push({ time: t, position: 'belowBar', color: '#FF9800', shape: 'arrowUp', text: `EXIT PUT(RSI OS) @${cls.toFixed(0)}` });
        inPut = false; entryPrice = entrySL = entryTarget = 0;
      }

      // 5. SMC reversal CHoCH exit (structure flips against trade)
      if (inCall && bearChoch) {
        markers.push({ time: t, position: 'aboveBar', color: '#FF5252', shape: 'arrowDown', text: `EXIT CALL(CHoCH) @${cls.toFixed(0)}` });
        inCall = false; entryPrice = entrySL = entryTarget = 0;
      }
      if (inPut && bullChoch) {
        markers.push({ time: t, position: 'belowBar', color: '#00E676', shape: 'arrowUp', text: `EXIT PUT(CHoCH) @${cls.toFixed(0)}` });
        inPut = false; entryPrice = entrySL = entryTarget = 0;
      }

      if (inCall || inPut) continue; // already in a trade — skip new entries

      // ── CALL ENTRY — Type A: EMA Cross (with 2-bar window) ───────
      const callByEMA = recentBullCross && aboveVwap && rsiCallOk && bullBody && volSurge;

      // ── CALL ENTRY — Type B: Bullish CHoCH / BOS ─────────────────
      // Structural break IS the confirmation — no VWAP gate here.
      // EMA9 slope rising + RSI not overbought is sufficient.
      const callByChoch = bullChoch && ema9Rising && (r == null || r < 72);

      if (callByEMA || callByChoch) {
        const label = callByChoch && !callByEMA ? `CALL(CHoCH) @${cls.toFixed(0)}` : `CALL @${cls.toFixed(0)}`;
        markers.push({ time: t, position: 'belowBar', color: '#00C853', shape: 'arrowUp', text: label });
        inCall = true;
        // Set ATR-based SL and target (SL=1.5×ATR, Target=2×ATR)
        const atr = atrArr[i] || (cls * 0.005); // fallback 0.5%
        entryPrice = cls;
        entrySL    = cls - 1.5 * atr;
        entryTarget= cls + 2.0 * atr;
        continue;
      }

      // ── PUT ENTRY — Type A: EMA Cross (with 2-bar window) ────────
      const putByEMA = recentBearCross && belowVwap && rsiPutOk && bearBody && volSurge;

      // ── PUT ENTRY — Type B: Bearish CHoCH / BOS ──────────────────
      // Structural break IS the confirmation — no VWAP gate here.
      const putByChoch = bearChoch && ema9Falling && (r == null || r > 28);

      if (putByEMA || putByChoch) {
        const label = putByChoch && !putByEMA ? `PUT(CHoCH) @${cls.toFixed(0)}` : `PUT @${cls.toFixed(0)}`;
        markers.push({ time: t, position: 'aboveBar', color: '#FF1744', shape: 'arrowDown', text: label });
        inPut = true;
        // Set ATR-based SL and target
        const atr = atrArr[i] || (cls * 0.005);
        entryPrice = cls;
        entrySL    = cls + 1.5 * atr;
        entryTarget= cls - 2.0 * atr;
      }
    }
    return markers;
  }

  // ── Live Tracking ─────────────────────────────────────────────
  _isMarketLive() {
    const now = new Date();
    // Convert to IST
    const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
    const day = ist.getDay(); // 0=Sun, 6=Sat
    if (day === 0 || day === 6) return false;
    const hhmm = ist.getHours() * 100 + ist.getMinutes();
    return hhmm >= 915 && hhmm <= 1530;
  }

  _isToday(dateStr) {
    const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' }); // YYYY-MM-DD
    return dateStr === today;
  }

  _startLiveTracking(cObj) {
    this._stopLiveTracking(cObj); // clear any existing

    // Only track if date is today and market is live
    if (!this._isToday(cObj.targetDate) || !this._isMarketLive()) {
      this._updateLiveBadge(cObj, false);
      return;
    }

    console.log(`[MC] 🔴 LIVE tracking started for ${cObj.symbol}`);
    this._updateLiveBadge(cObj, true);

    const poll = async () => {
      try {
        // Check if market is still open
        if (!this._isMarketLive()) {
          console.log(`[MC] Market closed — stopping live tracking for ${cObj.symbol}`);
          this._stopLiveTracking(cObj);
          return;
        }

        const API = window.API_BASE || '';
        const mappedSym = {'NIFTY':'NIFTY 50','BANKNIFTY':'NIFTY BANK','FINNIFTY':'NIFTY FIN SERVICE','SENSEX':'SENSEX'}[cObj.symbol.toUpperCase()] || cObj.symbol;
        const exchange = (cObj.symbol.toUpperCase() === 'SENSEX' || cObj.symbol.toUpperCase() === 'BANKEX') ? 'BSE' : 'NSE';
        const resp = await fetch(`${API}/api/quote?symbols=${exchange}:${encodeURIComponent(mappedSym)}`);
        if (!resp.ok) return;
        const data = await resp.json();

        // Extract quote — Kite returns { 'NSE:SYMBOL': { ... } }
        const key = Object.keys(data)[0];
        if (!key) return;
        const q = data[key];
        const ltp = q.last_price;
        const ohlcData = q.ohlc || {};
        const vol = q.volume || 0;

        if (!cObj.ohlcv || cObj.ohlcv.length === 0) return;

        // Update last candle
        const last = cObj.ohlcv[cObj.ohlcv.length - 1];
        const box = document.getElementById(cObj.id);
        const interval = box?.querySelector('.mc-interval')?.value || '5minute';

        // Check if we need a new candle (interval boundary crossed)
        const intervalMs = { '5minute': 300000, '15minute': 900000, '30minute': 1800000, 'day': 86400000, 'week': 604800000 };
        const intMs = intervalMs[interval] || 300000;
        const lastTime = new Date(last.date).getTime();
        const nowMs = Date.now();

        if (interval !== 'day' && interval !== 'week' && (nowMs - lastTime) >= intMs) {
          // New candle needed — use IST ISO so _toUnixTimestamp fake-UTC trick works correctly
          const newCandle = { date: this._nowIST(), open: ltp, high: ltp, low: ltp, close: ltp, volume: 0 };
          cObj.ohlcv.push(newCandle);
        } else {
          // Update existing last candle
          last.close = ltp;
          if (ltp > last.high) last.high = ltp;
          if (ltp < last.low) last.low = ltp;
          if (interval === 'day') {
            last.volume = vol; // day volume from Kite is cumulative
            last.open = ohlcData.open || last.open;
            last.high = Math.max(last.high, ohlcData.high || 0);
            last.low = Math.min(last.low, ohlcData.low || Infinity);
          }
        }

        // Efficient in-place candle update
        const updatedLast = cObj.ohlcv[cObj.ohlcv.length - 1];
        const ts = this._toUnixTimestamp(updatedLast.date);
        if (ts) {
          cObj.candleSeries.update({
            time: ts, open: updatedLast.open, high: updatedLast.high,
            low: updatedLast.low, close: updatedLast.close
          });

          // Update volume bar
          if (cObj.volSeries) {
            cObj.volSeries.update({
              time: ts, value: updatedLast.volume || vol,
              color: updatedLast.close >= updatedLast.open ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)'
            });
          }
        }

        // Refresh indicators + detect NEW signals for alerts, then redraw zones
        this.loadDataForChart(cObj, true, /* fromLive */ true);
        this._drawZones(cObj); // repaint zones after candle update (price scale may shift)

      } catch (e) {
        console.warn(`[MC] Live poll error for ${cObj.symbol}:`, e.message);
      }
    };

    // First poll immediately, then every 6s
    poll();
    cObj._liveInterval = setInterval(poll, 5000); // 5s poll
  }

  _stopLiveTracking(cObj) {
    if (cObj._liveInterval) {
      clearInterval(cObj._liveInterval);
      cObj._liveInterval = null;
      console.log(`[MC] Live tracking stopped for ${cObj.symbol}`);
    }
    this._updateLiveBadge(cObj, false);
  }

  _updateLiveBadge(cObj, isLive) {
    const box = document.getElementById(cObj.id);
    if (!box) return;
    let badge = box.querySelector('.mc-live-badge');
    if (isLive) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'mc-live-badge';
        badge.style.cssText = 'background:#EF5350;color:#fff;font-size:0.6rem;padding:2px 6px;border-radius:3px;margin-left:8px;animation:mc-pulse 1.5s infinite;font-weight:700;';
        badge.textContent = '🔴 LIVE';
        const header = box.querySelector('.mc-chart-header strong');
        if (header) header.appendChild(badge);
      }
    } else {
      if (badge) badge.remove();
    }
  }
  // ── IST now as ISO string (for new live candles) ─────────────
  _nowIST() {
    const now = new Date();
    const ist = new Date(now.getTime() + 5.5 * 3600000); // shift to IST
    return ist.toISOString().replace('Z', '+05:30');
  }

  // ── loadDataForChart override: detect new signals in live mode ─
  // Called with fromLive=true after each poll. Computes signals and
  // fires alert if a NEW entry/exit just appeared at the latest candle.
  _checkLiveSignalAlerts(cObj, sigMarkers) {
    if (!sigMarkers || sigMarkers.length === 0) return;
    // Check if beep is enabled in this chart's settings
    const box = document.getElementById(cObj.id);
    const beepEnabled = box?.querySelector('.mc-chk-beep')?.checked ?? true;

    const state  = cObj._signalState;
    const latest = sigMarkers[sigMarkers.length - 1];
    if (!latest) return;
    if (state.alertedTs.has(latest.time)) return;

    const txt = latest.text || '';
    const isEntry = txt.startsWith('CALL') || txt.startsWith('PUT');
    const isExit  = txt.startsWith('EXIT') || txt.includes('SL HIT') || txt.includes('TARGET');
    if (!isEntry && !isExit) return;

    // Only alert if this signal is at/near the latest candle
    const ohlcv = cObj.ohlcv;
    if (!ohlcv || ohlcv.length === 0) return;
    const lastTs = this._toUnixTimestamp(ohlcv[ohlcv.length - 1].date);
    // Dynamic barSecs based on interval
    const interval = box?.querySelector('.mc-interval')?.value || '5minute';
    const barSecs = { '5minute': 300, '15minute': 900, '30minute': 1800, 'day': 86400, 'week': 604800 }[interval] || 300;
    if (Math.abs(latest.time - lastTs) > barSecs * 2) return;

    state.alertedTs.add(latest.time);

    const isCall = txt.includes('CALL');
    const emoji  = isExit ? '🔔' : (isCall ? '🟢' : '🔴');
    const msg    = `${emoji} ${cObj.symbol} — ${txt}`;

    this._triggerAlert(msg, isExit ? 'exit' : 'entry', beepEnabled);
  }

  // ── Alert engine: beep + toast ────────────────────────────────
  _triggerAlert(message, type = 'entry', beepEnabled = true) {
    // 1. Audio beep via Web Audio API (only if enabled)
    if (beepEnabled) {
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);

        if (type === 'entry') {
          // Two rising tones for entry
          osc.frequency.setValueAtTime(660, ctx.currentTime);
          osc.frequency.setValueAtTime(880, ctx.currentTime + 0.15);
          gain.gain.setValueAtTime(0.4, ctx.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
          osc.start(ctx.currentTime);
          osc.stop(ctx.currentTime + 0.5);
        } else {
          // Single descending tone for exit
          osc.frequency.setValueAtTime(550, ctx.currentTime);
          osc.frequency.setValueAtTime(330, ctx.currentTime + 0.2);
          gain.gain.setValueAtTime(0.35, ctx.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.45);
          osc.start(ctx.currentTime);
          osc.stop(ctx.currentTime + 0.45);
        }
      } catch(e) { /* AudioContext not available */ }
    }

    // 2. Visual toast notification
    this._showToast(message, type);

    // 3. Console log
    console.log(`[MC ALERT] ${message}`);
  }

  _showToast(message, type) {
    let container = document.getElementById('mc-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'mc-toast-container';
      container.style.cssText = [
        'position:fixed', 'top:70px', 'right:16px', 'z-index:9999',
        'display:flex', 'flex-direction:column', 'gap:8px', 'pointer-events:none',
      ].join(';');
      document.body.appendChild(container);
    }

    const colors = {
      entry: { bg: '#1b2a1b', border: '#00C853', text: '#00E676' },
      exit:  { bg: '#2a1b1b', border: '#FF5252', text: '#FF7070' },
    };
    const c = colors[type] || colors.entry;

    const toast = document.createElement('div');
    toast.style.cssText = [
      `background:${c.bg}`, `border:1.5px solid ${c.border}`,
      `color:${c.text}`, 'font-size:0.8rem', 'font-weight:600',
      'padding:10px 16px', 'border-radius:8px',
      'box-shadow:0 4px 20px rgba(0,0,0,0.7)',
      'animation:mc-toast-in 0.25s ease',
      'pointer-events:auto', 'min-width:220px',
      'font-family:Inter,monospace',
    ].join(';');
    toast.textContent = message;
    container.appendChild(toast);

    // Auto-dismiss after 6 seconds
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.4s';
      setTimeout(() => toast.remove(), 400);
    }, 6000);
  }
}

// CSS for LIVE badge pulse + toast animation
const mcStyle = document.createElement('style');
mcStyle.textContent = [
  '@keyframes mc-pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }',
  '@keyframes mc-toast-in { from{opacity:0;transform:translateX(30px)} to{opacity:1;transform:none} }',
].join('\n');
document.head.appendChild(mcStyle);

window.multiChartManager = new MultiChartManager();
