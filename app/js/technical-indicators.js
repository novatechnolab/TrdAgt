/**
 * TradeSignal — Shared Technical Indicators Module
 * Single source of truth for all technical indicator computations.
 * Used by: ScoringEngine, StockAnalysis, FNOSessionAnalyzer, ChartManager
 */
const TI = {
  /**
   * Exponential Moving Average — returns array of EMA values.
   * Leading values (before `period` data points) are null.
   */
  computeEMA(data, period) {
    if (!data || data.length === 0 || !period || period <= 0) return [];
    const k = 2 / (period + 1);

    // If we have fewer than `period` points (common in early-session replay),
    // still compute a valid EMA series instead of returning [].
    // Seed with the first close (industry-standard fallback).
    if (data.length < period) {
      let ema = data[0];
      const result = [ema];
      for (let i = 1; i < data.length; i++) {
        ema = data[i] * k + ema * (1 - k);
        result.push(ema);
      }
      return result;
    }

    const result = new Array(period - 1).fill(null);
    let ema = data.slice(0, period).reduce((a, b) => a + b, 0) / period;
    result.push(ema);
    for (let i = period; i < data.length; i++) {
      ema = data[i] * k + ema * (1 - k);
      result.push(ema);
    }
    return result;
  },

  /** Returns the last (most recent) EMA value, or fallback if insufficient data. */
  emaLast(data, period, fallback) {
    const arr = TI.computeEMA(data, period);
    return arr.length > 0 ? arr[arr.length - 1] : (fallback ?? 0);
  },

  /**
   * Relative Strength Index (Wilder's smoothing).
   * Returns most recent RSI value (single number).
   */
  computeRSIArray(closes, period = 14) {
    if (!closes || closes.length < period + 1) return new Array(closes ? closes.length : 0).fill(50);

    const rsiArr = new Array(period).fill(null); // First few empty
    
    let gainSum = 0;
    let lossSum = 0;
    for (let i = 1; i <= period; i++) {
      const diff = closes[i] - closes[i - 1];
      if (diff >= 0) gainSum += diff;
      else lossSum += -diff;
    }

    let avgGain = gainSum / period;
    let avgLoss = lossSum / period;

    const calcRSI = (g, l) => {
      if (l === 0) return 100;
      const rs = g / l;
      return 100 - (100 / (1 + rs));
    };

    rsiArr.push(calcRSI(avgGain, avgLoss));

    for (let i = period + 1; i < closes.length; i++) {
      const diff = closes[i] - closes[i - 1];
      const gain = diff > 0 ? diff : 0;
      const loss = diff < 0 ? -diff : 0;
      avgGain = ((avgGain * (period - 1)) + gain) / period;
      avgLoss = ((avgLoss * (period - 1)) + loss) / period;
      rsiArr.push(calcRSI(avgGain, avgLoss));
    }

    return rsiArr;
  },

  computeRSI(closes, period = 14) {
    const arr = TI.computeRSIArray(closes, period);
    return arr[arr.length - 1] || 50;
  },

  /**
   * MACD (12, 26, 9) — returns { macd, signal, histogram }.
   * Uses proper 9-period EMA signal line.
   */
  computeMACDArray(closes) {
    const ema12 = TI.computeEMA(closes, 12);
    const ema26 = TI.computeEMA(closes, 26);
    if (ema12.length === 0 || ema26.length === 0) return { macdLine: [], signalLine: [], histogram: [] };
    
    const macdLine = [];
    const validMacdLine = [];
    for (let i = 0; i < ema26.length; i++) {
      if (ema12[i] === null || ema26[i] === null) {
        macdLine.push(null);
      } else {
        const val = ema12[i] - ema26[i];
        macdLine.push(val);
        validMacdLine.push(val);
      }
    }
    
    const validSignalLine = TI.computeEMA(validMacdLine, 9);
    const signalLine = new Array(ema26.length - validMacdLine.length).fill(null).concat(validSignalLine);
    
    const histogram = macdLine.map((m, i) => (m !== null && signalLine[i] !== null) ? (m - signalLine[i]) : null);
    return { macdLine, signalLine, histogram };
  },

  computeMACD(closes) {
    const arr = TI.computeMACDArray(closes);
    const macd = arr.macdLine[arr.macdLine.length - 1] || 0;
    const signal = arr.signalLine[arr.signalLine.length - 1] || 0;
    const histogram = arr.histogram[arr.histogram.length - 1] || 0;
    return { macd, signal, histogram };
  },

  /**
   * Average Directional Index (Wilder).
   */
  computeADX(highs, lows, closes, period = 14) {
    if (!highs || !lows || !closes || highs.length < period * 2 + 1) return 20;

    const tr = [];
    const plusDM = [];
    const minusDM = [];

    for (let i = 1; i < highs.length; i++) {
      const upMove = highs[i] - highs[i - 1];
      const downMove = lows[i - 1] - lows[i];
      plusDM.push((upMove > downMove && upMove > 0) ? upMove : 0);
      minusDM.push((downMove > upMove && downMove > 0) ? downMove : 0);
      tr.push(Math.max(
        highs[i] - lows[i],
        Math.abs(highs[i] - closes[i - 1]),
        Math.abs(lows[i] - closes[i - 1])
      ));
    }

    if (tr.length < period) return 20;

    let trN = tr.slice(0, period).reduce((a, b) => a + b, 0);
    let plusDMN = plusDM.slice(0, period).reduce((a, b) => a + b, 0);
    let minusDMN = minusDM.slice(0, period).reduce((a, b) => a + b, 0);

    const dxSeries = [];
    
    // Initial DX based on simple sum of first `period` elements
    const plusDI = trN > 0 ? (plusDMN / trN) * 100 : 0;
    const minusDI = trN > 0 ? (minusDMN / trN) * 100 : 0;
    const denom = plusDI + minusDI;
    dxSeries.push(denom > 0 ? (Math.abs(plusDI - minusDI) / denom) * 100 : 0);

    for (let i = period; i < tr.length; i++) {
      trN = trN - (trN / period) + tr[i];
      plusDMN = plusDMN - (plusDMN / period) + plusDM[i];
      minusDMN = minusDMN - (minusDMN / period) + minusDM[i];

      const pDI = trN > 0 ? (plusDMN / trN) * 100 : 0;
      const mDI = trN > 0 ? (minusDMN / trN) * 100 : 0;
      const den = pDI + mDI;
      dxSeries.push(den > 0 ? (Math.abs(pDI - mDI) / den) * 100 : 0);
    }

    if (dxSeries.length < period) {
      return dxSeries.length ? dxSeries[dxSeries.length - 1] : 20;
    }

    let adx = dxSeries.slice(0, period).reduce((a, b) => a + b, 0) / period;
    for (let i = period; i < dxSeries.length; i++) {
      adx = ((adx * (period - 1)) + dxSeries[i]) / period;
    }
    return adx;
  },

  /**
   * Average True Range.
   */
  computeATR(highs, lows, closes, period = 14) {
    if (!highs || !lows || !closes || highs.length < period + 1) return 0;
    const tr = [];
    for (let i = 1; i < highs.length; i++) {
      tr.push(Math.max(
        highs[i] - lows[i],
        Math.abs(highs[i] - closes[i - 1]),
        Math.abs(lows[i] - closes[i - 1])
      ));
    }
    if (tr.length < period) return tr.reduce((a, b) => a + b, 0) / Math.max(1, tr.length);

    let atr = tr.slice(0, period).reduce((a, b) => a + b, 0) / period;
    for (let i = period; i < tr.length; i++) {
      atr = ((atr * (period - 1)) + tr[i]) / period;
    }
    return atr;
  },

  /**
   * Dreiss Choppiness Index (CHOP) 14-period.
   * Returns a value between 0 and 100.
   */
  computeCHOP(highs, lows, closes, period = 14) {
    if (!highs || !lows || !closes || closes.length < period + 1) return 50.0;
    const len = closes.length;
    const tr = [];
    for (let i = 1; i < len; i++) {
      tr.push(Math.max(
        highs[i] - lows[i],
        Math.abs(highs[i] - closes[i - 1]),
        Math.abs(lows[i] - closes[i - 1])
      ));
    }
    if (tr.length < period) return 50.0;
    
    // Sum of TR over the last period
    const recentTr = tr.slice(-period);
    const atrSum = recentTr.reduce((a, b) => a + b, 0);
    
    // Max high and min low over the last period
    const recentHighs = highs.slice(-period);
    const recentLows = lows.slice(-period);
    const maxHigh = Math.max(...recentHighs);
    const minLow = Math.min(...recentLows);
    
    const diff = maxHigh - minLow;
    if (diff === 0) return 50.0;
    
    const chop = 100 * Math.log10(atrSum / diff) / Math.log10(period);
    return isNaN(chop) ? 50.0 : chop;
  },

  /** Bollinger Band Width as percentage of mean. */
  computeBollingerWidth(closes, period = 20) {
    if (!closes || closes.length < period) return 0;
    const slice = closes.slice(-period);
    const mean = slice.reduce((a, b) => a + b, 0) / period;
    const std = Math.sqrt(slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period);
    return std / mean * 100;
  },

  /** Bollinger Bands — returns array of { mid, upper, lower } or null. */
  computeBollingerBands(closes, period = 20, multiplier = 2) {
    const result = [];
    for (let i = 0; i < closes.length; i++) {
      if (i < period - 1) { result.push(null); continue; }
      const slice = closes.slice(i - period + 1, i + 1);
      const mean = slice.reduce((a, b) => a + b, 0) / period;
      const std = Math.sqrt(slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period);
      result.push({ mid: mean, upper: mean + multiplier * std, lower: mean - multiplier * std });
    }
    return result;
  },

  /** VWAP from OHLCV array (cumulative, no session reset — use for daily data). */
  computeVWAP(ohlcv) {
    let cumTPV = 0, cumVol = 0;
    return ohlcv.map(d => {
      const tp = (d.high + d.low + d.close) / 3;
      cumTPV += tp * d.volume;
      cumVol += d.volume;
      return cumVol > 0 ? cumTPV / cumVol : tp;
    });
  },

  /**
   * Intraday VWAP — resets cumulative counters at each trading-day boundary.
   * Detects day changes by comparing the date portion of each candle's timestamp.
   * For daily/weekly data this behaves identically to computeVWAP().
   */
  computeIntradayVWAP(ohlcv) {
    let cumTPV = 0, cumVol = 0;
    let prevDateStr = '';
    return ohlcv.map(d => {
      const dateStr = TI._extractDate(d.date);
      // Reset at new trading day
      if (dateStr && dateStr !== prevDateStr) {
        cumTPV = 0;
        cumVol = 0;
        prevDateStr = dateStr;
      }
      const tp = (d.high + d.low + d.close) / 3;
      cumTPV += tp * d.volume;
      cumVol += d.volume;
      return cumVol > 0 ? cumTPV / cumVol : tp;
    });
  },

  /**
   * Filter OHLCV to today's trading session only.
   * Extracts candles whose date portion matches the most recent candle's date.
   * If the filtered set has fewer than `minCandles` entries, returns the full array
   * so that indicator convergence is not broken.
   *
   * @param {Array} ohlcv — full multi-day OHLCV array (most recent last)
   * @param {number} minCandles — minimum candles required (default 20)
   * @returns {Array} today's session candles, or full array if insufficient
   */
  filterTodaySession(ohlcv, minCandles = 20) {
    if (!ohlcv || ohlcv.length === 0) return ohlcv;
    const lastDate = TI._extractDate(ohlcv[ohlcv.length - 1].date);
    if (!lastDate) return ohlcv; // no parseable date, return as-is
    const todayCandles = ohlcv.filter(d => TI._extractDate(d.date) === lastDate);
    return todayCandles.length >= minCandles ? todayCandles : ohlcv;
  },

  /**
   * Find the previous day's closing price from a multi-day OHLCV array.
   * Looks for the last candle whose date differs from the most recent candle's date.
   * Returns null if there is no previous day data.
   */
  findPrevDayClose(ohlcv) {
    if (!ohlcv || ohlcv.length < 2) return null;
    const lastDate = TI._extractDate(ohlcv[ohlcv.length - 1].date);
    if (!lastDate) return null;
    for (let i = ohlcv.length - 1; i >= 0; i--) {
      const d = TI._extractDate(ohlcv[i].date);
      if (d && d !== lastDate) return ohlcv[i].close;
    }
    return null;
  },

  /** Extract YYYY-MM-DD from a date value (string, Date, or numeric timestamp). */
  _extractDate(date) {
    if (!date) return '';
    if (typeof date === 'string') return date.slice(0, 10);
    if (date instanceof Date) return date.toISOString().slice(0, 10);
    if (typeof date === 'number') return new Date(date).toISOString().slice(0, 10);
    return String(date).slice(0, 10);
  },

  /** Supertrend (period, multiplier). */
  computeSupertrend(ohlcv, period = 10, multiplier = 3) {
    const closes = ohlcv.map(d => d.close);
    const highs  = ohlcv.map(d => d.high);
    const lows   = ohlcv.map(d => d.low);
    const atr = [];
    for (let i = 0; i < ohlcv.length; i++) {
      if (i === 0) { atr.push(highs[i] - lows[i]); continue; }
      const tr = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i-1]), Math.abs(lows[i] - closes[i-1]));
      atr.push(i < period ? tr : (atr[atr.length - 1] * (period - 1) + tr) / period);
    }
    const result = [];
    let prevDir = 1;
    for (let i = 0; i < ohlcv.length; i++) {
      const mid = (highs[i] + lows[i]) / 2;
      const up = mid + multiplier * atr[i];
      const dn = mid - multiplier * atr[i];
      if (i === 0) { result.push({ value: up, dir: 1, up, dn }); prevDir = 1; continue; }
      const prevUp = result[i-1].up;
      const prevDn = result[i-1].dn;
      const finalUp = up < prevUp || closes[i-1] > prevUp ? up : prevUp;
      const finalDn = dn > prevDn || closes[i-1] < prevDn ? dn : prevDn;
      const dir = prevDir === 1 ? (closes[i] < finalDn ? -1 : 1) : (closes[i] > finalUp ? 1 : -1);
      const value = dir === 1 ? finalDn : finalUp;
      result.push({ value, dir, up: finalUp, dn: finalDn });
      prevDir = dir;
    }
    return result;
  },

  /** Volume ratio: latest volume vs average of prior `period` candles. */
  computeVolumeRatio(volumes, period = 20) {
    if (!volumes || volumes.length < 2) return 1;
    const effectivePeriod = Math.min(period, volumes.length - 1);
    const avg = volumes.slice(-(effectivePeriod + 1), -1).reduce((a, b) => a + b, 0) / effectivePeriod;
    return avg > 0 ? volumes[volumes.length - 1] / avg : 1;
  },

  /**
   * Filter OHLCV to a specific trading session date.
   * Like filterTodaySession but for any given date string (YYYY-MM-DD).
   * Falls back to full array if insufficient candles for the target date.
   *
   * @param {Array} ohlcv — multi-day OHLCV array
   * @param {string} dateStr — target date in YYYY-MM-DD format
   * @param {number} minCandles — minimum candles required (default 20)
   * @returns {Array} filtered session candles
   */
  filterSessionByDate(ohlcv, dateStr, minCandles = 20) {
    if (!ohlcv || ohlcv.length === 0 || !dateStr) return ohlcv;
    const target = dateStr.slice(0, 10);
    const sessionCandles = ohlcv.filter(d => TI._extractDate(d.date) === target);
    return sessionCandles.length >= minCandles ? sessionCandles : ohlcv;
  },

  /**
   * Truncate OHLCV data at a specific entry point in time.
   * This ensures all indicators (EMA, VWAP, RSI, etc.) reflect the market
   * state AT THE TIME of entry — not end-of-day values.
   *
   * Priority:
   *   1. If entryTime (HH:MM) is provided → truncate at that exact 5-min candle
   *   2. Otherwise, find by price: first candle whose range includes entryPrice
   *   3. Fallback: closest candle to entry price
   *
   * @param {Array} ohlcv — OHLCV array (most recent last)
   * @param {number} entryPrice — the user's proposed entry price
   * @param {string} direction — 'CALL' or 'PUT'
   * @param {string} [targetDate] — optional YYYY-MM-DD to restrict search
   * @param {string} [entryTime] — optional HH:MM to truncate at exact time
   * @returns {Array} truncated OHLCV up to and including the entry candle
   */
  _parseIsoTimestampToIst(date) {
    if (!date) return null;
    if (date instanceof Date) {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: 'Asia/Kolkata',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).formatToParts(date);
      const year  = parts.find(p => p.type === 'year')?.value;
      const month = parts.find(p => p.type === 'month')?.value;
      const day   = parts.find(p => p.type === 'day')?.value;
      const hour  = parts.find(p => p.type === 'hour')?.value;
      const minute= parts.find(p => p.type === 'minute')?.value;
      if (year && month && day && hour && minute) {
        return { date: `${year}-${month}-${day}`, time: `${hour}:${minute}` };
      }
      return null;
    }

    if (typeof date === 'string') {
      const m = date.match(/^([0-9]{4}-[0-9]{2}-[0-9]{2})[T ]([0-9]{2}):([0-9]{2})(?::[0-9]{2})?(?:([Zz]|[\+\-][0-9]{2}:[0-9]{2}))?$/);
      if (m) {
        const datePart = m[1];
        const timePart = `${m[2]}:${m[3]}`;
        const tzPart = m[4];
        if (!tzPart) {
          // Naive ISO timestamp from backend is assumed to be IST.
          return { date: datePart, time: timePart };
        }
      }
      const dt = new Date(date);
      if (!Number.isNaN(dt.getTime())) {
        return TI._parseIsoTimestampToIst(dt);
      }
    }

    return null;
  },

  truncateAtEntryPrice(ohlcv, entryPrice, direction, targetDate, entryTime) {
    if (!ohlcv || ohlcv.length === 0) return ohlcv;

    if (entryTime && !targetDate) {
      targetDate = TI._extractDate(ohlcv[ohlcv.length - 1].date);
    }

    // Determine search bounds (restrict to target date if specified)
    let searchFrom = 0;
    let searchTo = ohlcv.length;
    if (targetDate) {
      const target = targetDate.slice(0, 10);
      let dateStart = -1, dateEnd = -1;
      for (let i = 0; i < ohlcv.length; i++) {
        const d = TI._extractDate(ohlcv[i].date);
        if (d === target) {
          if (dateStart === -1) dateStart = i;
          dateEnd = i;
        }
      }
      if (dateStart >= 0) {
        searchFrom = dateStart;
        searchTo = dateEnd + 1;
      }
    }

    // ── Priority 1: Time-based truncation (most precise) ──
    if (entryTime && targetDate) {
      const target = targetDate.slice(0, 10);
      const [hh, mm] = entryTime.split(':').map(Number);
      if (!isNaN(hh) && !isNaN(mm)) {
        const entryMinutes = hh * 60 + mm;
        let bestIdx = -1;
        let bestAbsDiff = Infinity;
        let latestLeIdx = -1;

        for (let i = searchFrom; i < searchTo; i++) {
          const d = ohlcv[i].date;
          const parsed = TI._parseIsoTimestampToIst(d);
          if (!parsed || parsed.date !== target) continue;
          const candleMinutes = parseInt(parsed.time.slice(0, 2), 10) * 60 + parseInt(parsed.time.slice(3), 10);

          // Track latest candle at or before requested time (deterministic fallback)
          if (candleMinutes <= entryMinutes) {
            latestLeIdx = i;
          }

          // Track nearest candle by absolute time difference.
          // This guards against timezone/string quirks that can miss exact matches.
          const absDiff = Math.abs(candleMinutes - entryMinutes);
          if (absDiff < bestAbsDiff || (absDiff === bestAbsDiff && candleMinutes <= entryMinutes)) {
            bestAbsDiff = absDiff;
            bestIdx = i;
          }
        }

        // Prefer the nearest candle if within one interval (5 min).
        // Otherwise use latest candle <= entry time, which is safer for replay.
        const chosenIdx = (bestIdx >= 0 && bestAbsDiff <= 5) ? bestIdx : latestLeIdx;
        if (chosenIdx >= 0) {
          console.log(
            `[Truncate] Time-based: chosenIdx=${chosenIdx}, nearestDiff=${bestAbsDiff}min, ` +
            `latestLeIdx=${latestLeIdx}, returning=${chosenIdx - searchFrom + 1} candles`
          );
          return ohlcv.slice(searchFrom, chosenIdx + 1);
        }
      }
    }

    // ── Priority 2: Price-based truncation ──
    if (!entryPrice || entryPrice <= 0) return ohlcv;

    // Strategy 2a: Exact match — candle range includes entry price
    for (let i = searchFrom; i < searchTo; i++) {
      if (ohlcv[i].low <= entryPrice && ohlcv[i].high >= entryPrice) {
        return ohlcv.slice(searchFrom, i + 1);
      }
    }

    // Strategy 2b: Close crossed through entry price
    for (let i = searchFrom; i < searchTo; i++) {
      if (direction === 'CALL' && ohlcv[i].close >= entryPrice) {
        return ohlcv.slice(searchFrom, i + 1);
      }
      if (direction === 'PUT' && ohlcv[i].close <= entryPrice) {
        return ohlcv.slice(searchFrom, i + 1);
      }
    }

    // Strategy 2c: Closest candle to entry price
    let closestIdx = searchTo - 1;
    let closestDist = Infinity;
    for (let i = searchFrom; i < searchTo; i++) {
      const dist = Math.abs(ohlcv[i].close - entryPrice);
      if (dist < closestDist) {
        closestDist = dist;
        closestIdx = i;
      }
    }
    return ohlcv.slice(searchFrom, closestIdx + 1);
  },

  // ═════════════════════════════════════════════════════════════════════
  // ── Smart Money Concepts (SMC) Primitives
  // ═════════════════════════════════════════════════════════════════════

  /**
   * Pivot Highs & Lows (Fractals)
   * Highs/Lows that are the highest/lowest within a left and right window.
   */
  computePivots(highs, lows, length = 5) {
    const len = highs.length;
    const pivots = new Array(len).fill(null);
    for (let i = length; i < len - length; i++) {
      let isPH = true;
      let isPL = true;
      for (let j = 1; j <= length; j++) {
        if (highs[i - j] > highs[i] || highs[i + j] >= highs[i]) isPH = false;
        if (lows[i - j] < lows[i] || lows[i + j] <= lows[i]) isPL = false;
      }
      if (isPH) pivots[i] = { type: 'PH', index: i, price: highs[i] };
      if (isPL) pivots[i] = { type: 'PL', index: i, price: lows[i] };
    }
    return pivots;
  },

  /**
   * Fair Value Gaps (FVG) / Imbalances
   * Bull FVG (1) = Low of current candle > High of candle 2 bars ago.
   * Bear FVG (-1) = High of current candle < Low of candle 2 bars ago.
   */
  computeFVG(highs, lows) {
    const len = highs.length;
    const fvgs = new Array(len).fill(null);
    for (let i = 2; i < len; i++) {
      if (lows[i] > highs[i - 2]) {
        // Bullish gap: distance between Low[i] and High[i-2]
        fvgs[i] = { type: 1, top: lows[i], bottom: highs[i - 2], isMitigated: false, bar: i };
      } else if (highs[i] < lows[i - 2]) {
        // Bearish gap: distance between High[i] and Low[i-2]
        fvgs[i] = { type: -1, top: lows[i - 2], bottom: highs[i], isMitigated: false, bar: i };
      }
    }
    return fvgs;
  },

  /**
   * Main Smart Money Concepts Engine
   * Derives HH, HL, LH, LL, BOS, CHOCH, FVGs, and S&R zones from OHLCV array.
   */
  computeSMC(ohlcv, pivotLen = 5) {
    if (!ohlcv || ohlcv.length < pivotLen * 2) return { markers: [], fvgs: [], srLines: [] };

    const highs = ohlcv.map(d => d.high);
    const lows = ohlcv.map(d => d.low);
    const closes = ohlcv.map(d => d.close);
    
    const pivots = TI.computePivots(highs, lows, pivotLen);
    const rawFvgs = TI.computeFVG(highs, lows);
    
    const markers = []; 
    const srLines = []; 
    const fvgs = [];

    // 1. Identify Unmitigated FVGs
    for (let i = 0; i < rawFvgs.length; i++) {
       const gap = rawFvgs[i];
       if (!gap) continue;
       
       let mitigated = false;
       for (let j = i + 1; j < ohlcv.length; j++) {
           if (gap.type === 1 && lows[j] < gap.bottom) mitigated = true;
           else if (gap.type === -1 && highs[j] > gap.top) mitigated = true;
           if (mitigated) break;
       }
       if (!mitigated) {
           fvgs.push({
               type: gap.type,
               top: gap.top,
               bottom: gap.bottom,
               startIndex: i,
               startTime: ohlcv[i].date
           });
       }
    }

    // 2. Swing Structure Logging (HH, HL, LH, LL)
    let lastPH = null;
    let lastPL = null;
    let currentTrend = 0; // 1 = Bullish, -1 = Bearish

    for (let i = 0; i < pivots.length; i++) {
        const p = pivots[i];
        if (!p) continue;
        
        const time = ohlcv[i].date;
        
        if (p.type === 'PH') {
            if (lastPH !== null) {
                if (p.price > lastPH.price) markers.push({ time, position: 'aboveBar', shape: 'text', color: '#1E88E5', text: 'HH' });
                else markers.push({ time, position: 'aboveBar', shape: 'text', color: '#E53935', text: 'LH' });
            }
            lastPH = p;
            srLines.push({ price: p.price, type: 'RESISTANCE' });
        } else if (p.type === 'PL') {
            if (lastPL !== null) {
                if (p.price > lastPL.price) markers.push({ time, position: 'belowBar', shape: 'text', color: '#1E88E5', text: 'HL' });
                else markers.push({ time, position: 'belowBar', shape: 'text', color: '#E53935', text: 'LL' });
            }
            lastPL = p;
            srLines.push({ price: p.price, type: 'SUPPORT' });
        }
    }

    // 3. BOS and CHOCH (Breakouts)
    let recentPH = -1;
    let recentPL = Infinity;

    for (let i = pivotLen * 2; i < ohlcv.length; i++) {
        if (pivots[i]) continue; // Skip pivot bars themselves to avoid premature breaks

        // Recalculate most recent valid swing for breakout check
        recentPH = -1; 
        recentPL = Infinity;
        for (let j = i - 1; j >= 0; j--) {
            if (pivots[j]) {
                if (pivots[j].type === 'PH' && recentPH === -1) recentPH = pivots[j].price;
                if (pivots[j].type === 'PL' && recentPL === Infinity) recentPL = pivots[j].price;
            }
            if (recentPH !== -1 && recentPL !== Infinity) break;
        }
        
        if (recentPH === -1 || recentPL === Infinity) continue;

        const c = closes[i];
        const time = ohlcv[i].date;

        if (currentTrend <= 0 && c > recentPH) {
            // Bullish Breakout
            markers.push({ 
                time, position: 'aboveBar', shape: 'arrowUp', 
                color: '#26A69A', text: currentTrend === -1 ? 'CHOCH' : 'BOS' 
            });
            currentTrend = 1;
            // Mutate logic so we don't trigger again until a new pivot forms
            pivots[i] = { type: 'BREAK' }; 
        } else if (currentTrend >= 0 && c < recentPL) {
            // Bearish Breakout
            markers.push({ 
                time, position: 'belowBar', shape: 'arrowDown', 
                color: '#EF5350', text: currentTrend === 1 ? 'CHOCH' : 'BOS' 
            });
            currentTrend = -1;
            pivots[i] = { type: 'BREAK' };
        }
    }

    return { markers, fvgs, srLines };
  }
};

window.TI = TI;
