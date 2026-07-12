import re

with open("app/js/multi-chart.js", "r") as f:
    text = f.read()

# 1. Update the box innerHTML to a dropdown
dropdown_html = """
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
            <div style="position:absolute; top:calc(100% + 4px); right:0; background:#1e242c; border:1px solid #444; z-index:100; display:flex; flex-direction:column; padding:8px 12px; border-radius:6px; min-width:180px; box-shadow:0 8px 24px rgba(0,0,0,0.8); font-size:0.75rem; white-space:nowrap;">
              <label><input type="checkbox" class="mc-chk-vol" checked> Volume</label>
              <label><input type="checkbox" class="mc-chk-sma" checked> EMA 9/21</label>
              <label><input type="checkbox" class="mc-chk-ema50"> EMA 50</label>
              <label><input type="checkbox" class="mc-chk-vwap" ${symbol.includes('NIFTY') ? '' : 'checked'}> VWAP</label>
              <label><input type="checkbox" class="mc-chk-bb" checked> Bollinger Bands</label>
              <hr style="border-color:#444; margin:4px 0;">
              <label><input type="checkbox" class="mc-chk-smc" checked> SMC Structure (BOS, CHOCH)</label>
              <label><input type="checkbox" class="mc-chk-fvg" checked> SMC FVG</label>
              <hr style="border-color:#444; margin:4px 0;">
              <label><input type="checkbox" class="mc-chk-rsi" checked> RSI (14)</label>
              <label><input type="checkbox" class="mc-chk-adx"> ADX</label>
              <label><input type="checkbox" class="mc-chk-atr"> ATR</label>
            </div>
          </details>
        </div>
        <button class="btn btn-close text-red" style="padding:2px 8px; border:1px solid #333;" onclick="multiChartManager.removeChart('${id}')">✖</button>
      </div>
      <div class="mc-chart-container" id="container-${id}"></div>
"""
text = re.sub(r'<div class="mc-chart-header">.*?<div class="mc-chart-container" id="container-\$\{id\}"></div>', dropdown_html.strip(), text, flags=re.DOTALL)

# 2. Add sub-series tracking in instanceObj
instanceObj_code = """
    const instanceObj = {
      id, symbol, targetDate: date, chart, candleSeries,
      lines: [], priceLines: [],
      volSeries: chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: '', scaleMargins: { top: 0.8, bottom: 0 } }),
      rsiSeries: chart.addLineSeries({ priceScaleId: 'left', color: '#B388FF', lineWidth: 1, title: 'RSI' }),
      adxSeries: chart.addLineSeries({ priceScaleId: 'left', color: '#FFB74D', lineWidth: 1, title: 'ADX' }),
      atrSeries: chart.addLineSeries({ priceScaleId: 'left', color: '#4DD0E1', lineWidth: 1, title: 'ATR' }),
    };
"""
text = re.sub(r'const instanceObj = {\s*id, symbol, targetDate: date, chart, candleSeries,\s*lines: \[\], priceLines: \[\]\s*};', instanceObj_code.strip(), text)

# 3. Add leftPriceScale config
chart_config = """
    const chart = LightweightCharts.createChart(container, {
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#B0BEC5' },
      grid: { vertLines: { color: '#2d333b' }, horzLines: { color: '#2d333b' } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#444c56' },
      leftPriceScale: { visible: true, borderColor: '#444c56', scaleMargins: { top: 0.75, bottom: 0 } },
      timeScale: { borderColor: '#444c56', timeVisible: true, borderVisible: true },
    });
"""
text = re.sub(r'const chart = LightweightCharts.createChart\(container, \{.*?(?=const candleSeries)/?\}?\)?;', chart_config.strip() + '\n', text, flags=re.DOTALL)


# 4. Extract rendering logic for all new toggles
# Replace Configuration block
render_block = """
    const closes = ohlcv.map(d => d.close);
    const highs = ohlcv.map(d => d.high);
    const lows = ohlcv.map(d => d.low);

    // Configuration
    const showVol = box.querySelector('.mc-chk-vol').checked;
    const showEMAConfig = box.querySelector('.mc-chk-sma').checked;
    const showEMA50 = box.querySelector('.mc-chk-ema50').checked;
    const showVWAP = box.querySelector('.mc-chk-vwap').checked;
    const showBB = box.querySelector('.mc-chk-bb').checked;
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
    assignOscillator(cObj.rsiSeries, showRSI, TI.computeRSI.bind(TI), [closes]);
    assignOscillator(cObj.adxSeries, showADX, TI.computeADX.bind(TI), [highs, lows, closes]);
    assignOscillator(cObj.atrSeries, showATR, TI.computeATR.bind(TI), [highs, lows, closes]);

    const addLine = (dataArr, color, title) => {
"""

text = re.sub(r'const closes = ohlcv.map\(d => d.close\);.*?const addLine = \(dataArr, color, title\) => \{', render_block.strip() + ' {', text, flags=re.DOTALL)


# 5. Fix SMC integration block to respect FVG toggle
smc_block_replace = """
        const visibleSR = showSMC ? smc.srLines.slice(-6) : [];
        visibleSR.forEach(sr => {
          const isRes = sr.type === 'RESISTANCE';
          cObj.priceLines.push(cObj.candleSeries.createPriceLine({
              price: sr.price, color: isRes ? 'rgba(239,83,80,0.5)' : 'rgba(38,166,154,0.5)', lineWidth: 1, lineStyle: 2, axisLabelVisible: false
          }));
        });

        const visibleFVG = showFVG ? smc.fvgs.slice(-5) : [];
        visibleFVG.forEach(fvg => {
"""
text = re.sub(r'const visibleSR = smc.srLines.slice\(-6\);.*?const visibleFVG = smc.fvgs.slice\(-5\);\s*visibleFVG.forEach\(fvg => \{', smc_block_replace.strip() + '\n', text, flags=re.DOTALL)

smc_condition = r'if \(showSMC && typeof TI.computeSMC === \'function\'\) \{'
new_smc_condition = r'if ((showSMC || showFVG) && typeof TI.computeSMC === \'function\') {'
text = re.sub(smc_condition, new_smc_condition, text)

new_smc_markers = """
        if (showSMC) {
            smc.markers.forEach(m => {
              const t = this._toUnixTimestamp(m.time);
              if (t) markers.push({ time: t, position: m.position, color: m.color, shape: m.shape, text: m.text });
            });
            markers.sort((a,b) => a.time - b.time);
            if (markers.length > 0) cObj.candleSeries.setMarkers(markers);
            else cObj.candleSeries.setMarkers([]);
        } else {
            cObj.candleSeries.setMarkers([]);
        }
"""
old_smc_markers = r'smc.markers.forEach\(m => \{.*?else cObj.candleSeries.setMarkers\(\[\]\);'
text = re.sub(old_smc_markers, new_smc_markers.strip(), text, flags=re.DOTALL)

with open("app/js/multi-chart.js", "w") as f:
    f.write(text)

print("Applied multi-chart advanced indicators!")
