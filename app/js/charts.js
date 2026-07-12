/**
 * TradeSignal — Chart Module
 * Lightweight Charts (TradingView) integration for OHLCV candlestick charts.
 * Uses LIVE Kite API historical data only — no mock/demo data.
 */
class ChartManager {
  constructor() {
    this.chart = null;
    this.candleSeries = null;
    this.volumeSeries = null;
    this.emaLines = {};
    this.currentContainer = null;
  }

  // ── Initialize chart in container ──
  init(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    this.currentContainer = container;

    // Clear previous
    container.innerHTML = '';

    this.chart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight || 450,
      layout: {
        background: { color: '#FFFFFF' },
        textColor: '#546E7A',
        fontFamily: "'Inter', sans-serif",
        fontSize: 12
      },
      grid: {
        vertLines: { color: 'rgba(21,101,192,0.04)' },
        horzLines: { color: 'rgba(21,101,192,0.04)' }
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { color: 'rgba(30,136,229,0.3)', width: 1, style: 2, labelBackgroundColor: '#1E88E5' },
        horzLine: { color: 'rgba(30,136,229,0.3)', width: 1, style: 2, labelBackgroundColor: '#1E88E5' }
      },
      rightPriceScale: {
        borderColor: 'rgba(21,101,192,0.1)',
        scaleMargins: { top: 0.1, bottom: 0.25 }
      },
      timeScale: {
        borderColor: 'rgba(21,101,192,0.1)',
        timeVisible: true,
        secondsVisible: false
      },
      handleScroll: { mouseWheel: true, pressedMouseMove: true },
      handleScale: { mouseWheel: true, pinch: true }
    });

    // Candlestick series
    this.candleSeries = this.chart.addCandlestickSeries({
      upColor: '#26A69A',
      downColor: '#EF5350',
      borderUpColor: '#26A69A',
      borderDownColor: '#EF5350',
      wickUpColor: '#26A69A',
      wickDownColor: '#EF5350'
    });

    // Volume series
    this.volumeSeries = this.chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'vol',
      scaleMargins: { top: 0.8, bottom: 0 }
    });

    // Resize observer
    this._resizeObserver = new ResizeObserver(() => {
      if (this.chart && container.clientWidth > 0) {
        this.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      }
    });
    this._resizeObserver.observe(container);
  }

  // ── Load OHLCV data ──
  setData(ohlcv) {
    if (!this.candleSeries) return;

    const candles = ohlcv.map(d => ({
      time: typeof d.date === 'string' ? d.date.split('T')[0] : d.time || d.date,
      open: d.open,
      high: d.high,
      low: d.low,
      close: d.close
    }));

    const volumes = ohlcv.map(d => ({
      time: typeof d.date === 'string' ? d.date.split('T')[0] : d.time || d.date,
      value: d.volume,
      color: d.close >= d.open ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)'
    }));

    this.candleSeries.setData(candles);
    this.volumeSeries.setData(volumes);
    this.chart.timeScale().fitContent();
  }

  // ── Add EMA overlay ──
  addEMA(closes, times, period, color, label) {
    if (!this.chart || closes.length < period) return;

    const k = 2 / (period + 1);
    const emaValues = [];
    let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
    
    for (let i = period - 1; i < closes.length; i++) {
      if (i >= period) {
        ema = closes[i] * k + ema * (1 - k);
      }
      emaValues.push({
        time: typeof times[i] === 'string' ? times[i].split('T')[0] : times[i],
        value: ema
      });
    }

    const lineSeries = this.chart.addLineSeries({
      color: color,
      lineWidth: 1,
      title: label || `EMA ${period}`,
      crosshairMarkerVisible: false,
      priceLineVisible: false
    });
    lineSeries.setData(emaValues);
    this.emaLines[period] = lineSeries;
  }

  // ── Load from Kite API (LIVE data only) ──
  async loadFromAPI(symbol, interval, rangeDays) {
    if (!kiteAPI.connected) {
      this.showError('Kite API not connected. Go to Settings → Connect first.');
      return null;
    }

    const token = kiteAPI.getInstrumentToken(symbol, 'NSE');
    if (!token) {
      this.showError(`Instrument token not found for ${symbol}. Ensure instruments are loaded.`);
      return null;
    }

    const to = new Date().toISOString().split('T')[0];
    const fromDate = new Date();
    fromDate.setDate(fromDate.getDate() - rangeDays);
    const from = fromDate.toISOString().split('T')[0];

    try {
      const data = await kiteAPI.getHistoricalData(token, from, to, interval);
      const candles = data.candles || data.data?.candles || data;

      if (Array.isArray(candles) && candles.length > 0) {
        const ohlcv = candles.map(c => {
          // kiteconnect returns dicts: {date, open, high, low, close, volume}
          // or arrays: [date, open, high, low, close, volume]
          let date, open, high, low, close, volume;
          if (Array.isArray(c)) {
            [date, open, high, low, close, volume] = c;
          } else {
            date = c.date;
            open = c.open;
            high = c.high;
            low = c.low;
            close = c.close;
            volume = c.volume;
          }
          // Extract yyyy-mm-dd from any date format
          const dateStr = typeof date === 'string' ? date.split('T')[0] : date;
          return { date: dateStr, open, high, low, close, volume };
        });
        this.setData(ohlcv);
        
        // Add EMA overlays
        const closes = ohlcv.map(o => o.close);
        const times = ohlcv.map(o => o.date);
        this.addEMA(closes, times, 9, '#42A5F5', 'EMA 9');
        this.addEMA(closes, times, 21, '#FFA726', 'EMA 21');
        this.addEMA(closes, times, 50, '#AB47BC', 'EMA 50');

        return ohlcv;
      } else {
        this.showError(`No historical data returned for ${symbol}.`);
        return null;
      }
    } catch (e) {
      this.showError(`Failed to load chart data: ${e.message}`);
      return null;
    }
  }

  // ── Show error in chart area ──
  showError(message) {
    if (this.currentContainer) {
      this.currentContainer.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:center;height:100%;flex-direction:column;gap:12px;color:#546E7A;font-family:'Inter',sans-serif;">
          <div style="font-size:2.5rem;">⚠️</div>
          <div style="font-size:0.9rem;font-weight:600;">Chart Unavailable</div>
          <div style="font-size:0.8rem;max-width:400px;text-align:center;color:#90A4AE;">${message}</div>
        </div>`;
    }
    console.warn('ChartManager:', message);
  }

  // ── Destroy ──
  destroy() {
    if (this._resizeObserver) this._resizeObserver.disconnect();
    if (this.chart) {
      this.chart.remove();
      this.chart = null;
    }
  }
}

window.chartManager = new ChartManager();
