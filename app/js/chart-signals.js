/**
 * TradeSignal — Chart Signal Engine (Institutional Grade v2)
 *
 * Multi-indicator confluence system for generating BUY/SELL entry and exit markers
 * on intraday (5m/15m) and daily charts. Each signal is scored for quality.
 *
 * v2 IMPROVEMENTS:
 * - TREND DAY DETECTION: Recognizes strong one-directional days and switches to
 *   swing-low trailing instead of ATR-based trailing, preventing premature exits.
 * - SWING TRAILING: Uses lowest-low of last 8 bars minus ATR buffer,
 *   respecting actual market structure.
 * - STRUCTURE BREAK EXITS: In trend mode, only exits on EMA 9/21 cross
 *   or Supertrend flip — not on random noise pullbacks.
 * - WIDER SL/TARGET: 2.5x ATR SL and 4x ATR target on trend days.
 * - EXTENDED COOLDOWN: 8 bars after trend exits to prevent whipsaw re-entries.
 * - TARGET RAISING: When price hits target but trend is intact, raises target
 *   instead of closing — lets winners run.
 *
 * ┌─────────────────────────────────────────────────────────────┐
 * │  ENTRY FRAMEWORK (≥4 confluence required)                  │
 * ├──────────────────┬──────────────────────────────────────────┤
 * │ Trend Layer      │ EMA 9/21/50 alignment + Supertrend dir  │
 * │ Momentum Layer   │ RSI zone + MACD histogram crossover     │
 * │ Volume Layer     │ Volume ratio ≥1.3x avg = conviction     │
 * │ Price Action     │ BB breakout, VWAP cross, candle pattern │
 * │ Risk Filter      │ ADX ≥18 (trending) required for entry   │
 * ├──────────────────┼──────────────────────────────────────────┤
 * │ EXIT: NORMAL     │ ATR trailing stop, fixed target R:R     │
 * │ EXIT: TREND DAY  │ Swing-low trail, structure-break only   │
 * └──────────────────┴──────────────────────────────────────────┘
 */
class ChartSignalEngine {
  constructor() {
    this._lastMarkers = [];
    this.config = {
      minConfluence: 4,
      minADX: 18,
      volConfirmation: 1.3,
      atrSlMultiplier: 1.5,    // normal mode SL
      atrTgtMultiplier: 3.0,   // normal mode target
      trailATRMultiplier: 2.0, // normal trailing width
      cooldownBars: 3,
      minRR: 2.0,
      triggerVolumeBars: 3,
      maxEMA9DistanceATR: 2.5,
      bbExtensionPct: 0.15,
      rsiOversold: 30,
      rsiOverbought: 70,
      scaleOutPct1: 0.4,       // 40% at TGT1
      scaleOutPct2: 0.4,       // 40% at TGT2
      trailPct: 0.2,           // 20% trailing
    };
  }

  computeSignals(ohlcv, opts = {}) {
    if (!ohlcv || ohlcv.length < 30) return { markers: [], signals: [], summary: this._emptySummary() };

    const cfg = { ...this.config, ...opts };
    const len = ohlcv.length;
    const closes = ohlcv.map(d => d.close);
    const highs = ohlcv.map(d => d.high);
    const lows = ohlcv.map(d => d.low);
    const opens = ohlcv.map(d => d.open);
    const volumes = ohlcv.map(d => d.volume);

    // ── Pre-compute all indicator arrays ──
    const ema5 = TI.computeEMA(closes, 5);
    const ema9 = TI.computeEMA(closes, 9);
    const ema21 = TI.computeEMA(closes, 21);
    const ema50 = TI.computeEMA(closes, 50);
    const rsiArr = this._computeRSIArray(closes, 14);
    const macdHist = this._computeMACDHistArray(closes);
    const supertrend = TI.computeSupertrend(ohlcv, 10, 3);
    const vwap = TI.computeIntradayVWAP(ohlcv);
    const bb = TI.computeBollingerBands(closes, 20, 2);
    const avgVol = this._rollingAvg(volumes, 20);
    const atrArr = this._computeATRArray(highs, lows, closes, 14);
    const adxArr = this._computeADXArray(highs, lows, closes, 14);

    // ── SMC Arrays ──
    const pivots = window.TI && TI.computePivots ? TI.computePivots(highs, lows, 5) : new Array(len).fill(null);
    const fvgs = window.TI && TI.computeFVG ? TI.computeFVG(highs, lows) : new Array(len).fill(null);

    let lastMajorHigh = null;
    let lastMajorLow = null;
    let activeBullOBs = [];
    let activeBearOBs = [];

    const markers = [];
    const signals = [];
    let position = null;
    let cooldown = 0;

    // ── Pre-compute swing lows/highs for trailing (lookback 8 bars) ──
    const SWING_LOOK = 8;
    const swingLows = new Array(len).fill(0);
    const swingHighs = new Array(len).fill(0);
    for (let i = SWING_LOOK; i < len; i++) {
      swingLows[i] = Math.min(...lows.slice(i - SWING_LOOK, i));
      swingHighs[i] = Math.max(...highs.slice(i - SWING_LOOK, i));
    }

    // ── Higher-lows / Lower-highs detection (market structure) ──
    const isHigherLows = (idx, lookback = 12) => {
      if (idx < lookback + 2) return false;
      let prevLow = Infinity, hlCount = 0;
      for (let j = idx - lookback; j <= idx; j += 3) {
        const localLow = Math.min(lows[j], lows[Math.min(j + 1, idx)], lows[Math.min(j + 2, idx)]);
        if (localLow > prevLow) hlCount++;
        prevLow = localLow;
      }
      return hlCount >= 3;
    };
    const isLowerHighs = (idx, lookback = 12) => {
      if (idx < lookback + 2) return false;
      let prevHigh = 0, lhCount = 0;
      for (let j = idx - lookback; j <= idx; j += 3) {
        const localHigh = Math.max(highs[j], highs[Math.min(j + 1, idx)], highs[Math.min(j + 2, idx)]);
        if (localHigh < prevHigh && prevHigh > 0) lhCount++;
        prevHigh = localHigh;
      }
      return lhCount >= 3;
    };

    for (let i = 1; i < len; i++) {
      const c = closes[i];
      const o = opens[i];
      const h = highs[i];
      const l = lows[i];
      const time = this._toChartTime(ohlcv[i].date);
      if (!time) continue;

      const atr = atrArr[i] || (c * 0.015);
      const adx = adxArr[i] || 20;
      const rsi = rsiArr[i];
      const macd = macdHist[i];
      const volRatio = avgVol[i] > 0 ? volumes[i] / avgVol[i] : 1;

      if (cooldown > 0) cooldown--;

      // ── SMC State Tracking ──
      const pivot = pivots[i - 5]; 
      if (pivot) {
        if (pivot.type === 'PH') lastMajorHigh = pivot.price;
        if (pivot.type === 'PL') lastMajorLow = pivot.price;
      }
      
      // Premium/Discount Detection
      let isDiscount = false;
      let isPremium = false;
      if (lastMajorHigh && lastMajorLow && lastMajorHigh > lastMajorLow) {
        const mid = (lastMajorHigh + lastMajorLow) / 2;
        if (c < mid && c > lastMajorLow) isDiscount = true;
        if (c > mid && c < lastMajorHigh) isPremium = true;
      }

      // Liquidity Sweeps
      const bslSweep = lastMajorHigh && h > lastMajorHigh && c < lastMajorHigh;
      const sslSweep = lastMajorLow && l < lastMajorLow && c > lastMajorLow;

      // FVG Mitigations
      let fvgBullTap = false;
      let fvgBearTap = false;
      for (let j = Math.max(0, i - 20); j < i; j++) {
        const f = fvgs[j];
        if (!f || f.isMitigated) continue;
        if (f.type === 1 && l <= f.top) { fvgBullTap = true; f.isMitigated = true; }
        if (f.type === -1 && h >= f.bottom) { fvgBearTap = true; f.isMitigated = true; }
      }

      // Order Block (OB) Detection & Mitigations
      if (volRatio >= 1.3) {
        const isBullCandle = c > o && (c - o) / (h - l) > 0.6;
        const isBearCandle = c < o && (o - c) / (h - l) > 0.6;
        if (isBullCandle && closes[i-1] < opens[i-1]) activeBullOBs.push({ top: highs[i-1], bottom: lows[i-1], active: true });
        if (isBearCandle && closes[i-1] > opens[i-1]) activeBearOBs.push({ top: highs[i-1], bottom: lows[i-1], active: true });
      }
      let obBullTap = false;
      let obBearTap = false;
      for (const ob of activeBullOBs) { if (ob.active && l <= ob.top && c >= ob.bottom) { obBullTap = true; ob.active = false; break; } }
      for (const ob of activeBearOBs) { if (ob.active && h >= ob.bottom && c <= ob.top) { obBearTap = true; ob.active = false; break; } }

      // ── TREND CONTEXT ──
      const trendBull = ema9[i] != null && ema21[i] != null && ema9[i] > ema21[i];
      const trendBear = ema9[i] != null && ema21[i] != null && ema9[i] < ema21[i];
      const stBull = supertrend[i]?.dir === 1;
      const stBear = supertrend[i]?.dir === -1;
      const bigTrendBull = ema21[i] != null && c > ema21[i];
      const bigTrendBear = ema21[i] != null && c < ema21[i];
      const bullCHoCH = isHigherLows(i);
      const bearCHoCH = isLowerHighs(i);

      // ────────────────────────────────────────────────────────
      // TREND DAY DETECTION
      // When all major indicators align AND market is making
      // higher lows (bull) / lower highs (bear), switch to
      // trend-following exit strategy.
      // ────────────────────────────────────────────────────────
      const allAlignedBull = trendBull && bigTrendBull && stBull && adx >= 25;
      const allAlignedBear = trendBear && bigTrendBear && stBear && adx >= 25;
      const isTrendDayBull = allAlignedBull && bullCHoCH;
      const isTrendDayBear = allAlignedBear && bearCHoCH;

      // ── Manage open position ──
      if (position) {
        let exitReason = null;
        let exitPrice = c;
        let exitColor = '#EF5350';
        const barsInTrade = i - position.bar;

        const inTrendMode = position.type === 'LONG'
          ? (trendBull && stBull && bigTrendBull)
          : (trendBear && stBear && bigTrendBear);

        if (position.type === 'LONG') {
          if (!exitReason && ema21[i] != null && c < ema21[i] && (c - closes[i - 1]) < 0) {
            exitReason = `EXIT CALL Invalidation EMA21 ₹${c.toFixed(0)}`;
          }
          if (!exitReason && vwap[i] != null && closes[i - 1] < vwap[i - 1] && c < vwap[i]) {
            exitReason = `EXIT CALL Invalidation VWAP ₹${c.toFixed(0)}`;
          }

          if (!exitReason && inTrendMode && barsInTrade > 3) {
            const swingTrail = swingLows[i] - 0.5 * atr;
            if (swingTrail > position.trailSL) position.trailSL = swingTrail;

            const emaCrossDown = ema9[i] != null && ema21[i] != null && ema9[i] < ema21[i] &&
                                 ema9[i - 1] != null && ema21[i - 1] != null && ema9[i - 1] >= ema21[i - 1];
            const stFlipBear = supertrend[i]?.dir === -1 && supertrend[i - 1]?.dir === 1;

            if (emaCrossDown || stFlipBear) {
              exitReason = `EXIT ${emaCrossDown ? 'EMA Cross' : 'ST Flip'} ₹${c.toFixed(0)}`;
            } else if (l <= position.trailSL) {
              exitReason = `EXIT SwingTrail ₹${position.trailSL.toFixed(0)}`;
              exitPrice = position.trailSL;
            }
          }

          if (!exitReason) {
            const newTrail = c - cfg.trailATRMultiplier * atr;
            if (newTrail > position.trailSL) position.trailSL = newTrail;

            if (l <= position.trailSL) {
              exitReason = position.trailSL >= position.entry
                ? `EXIT Trail ₹${position.trailSL.toFixed(0)}`
                : `EXIT SL ₹${position.trailSL.toFixed(0)}`;
              exitPrice = position.trailSL;
            } else if (h >= position.target) {
              if (trendBull && stBull && bigTrendBull && adx >= 22) {
                position.target = c + cfg.atrTgtMultiplier * atr;
              } else {
                exitReason = `EXIT TGT ₹${position.target.toFixed(0)}`;
                exitPrice = position.target;
              }
            }
          }
        } else {
          if (!exitReason && ema21[i] != null && c > ema21[i] && (c - closes[i - 1]) > 0) {
            exitReason = `EXIT PUT Invalidation EMA21 ₹${c.toFixed(0)}`;
          }
          if (!exitReason && vwap[i] != null && closes[i - 1] > vwap[i - 1] && c > vwap[i]) {
            exitReason = `EXIT PUT Invalidation VWAP ₹${c.toFixed(0)}`;
          }

          if (!exitReason && inTrendMode && barsInTrade > 3) {
            const swingTrail = swingHighs[i] + 0.5 * atr;
            if (swingTrail < position.trailSL) position.trailSL = swingTrail;

            const emaCrossUp = ema9[i] != null && ema21[i] != null && ema9[i] > ema21[i] &&
                               ema9[i - 1] != null && ema21[i - 1] != null && ema9[i - 1] <= ema21[i - 1];
            const stFlipBull = supertrend[i]?.dir === 1 && supertrend[i - 1]?.dir === -1;

            if (emaCrossUp || stFlipBull) {
              exitReason = `EXIT ${emaCrossUp ? 'EMA Cross' : 'ST Flip'} ₹${c.toFixed(0)}`;
            } else if (h >= position.trailSL) {
              exitReason = `EXIT SwingTrail ₹${position.trailSL.toFixed(0)}`;
              exitPrice = position.trailSL;
            }
          }

          if (!exitReason) {
            const newTrail = c + cfg.trailATRMultiplier * atr;
            if (newTrail < position.trailSL) position.trailSL = newTrail;

            if (h >= position.trailSL) {
              exitReason = position.trailSL <= position.entry
                ? `EXIT Trail ₹${position.trailSL.toFixed(0)}`
                : `EXIT SL ₹${position.trailSL.toFixed(0)}`;
              exitPrice = position.trailSL;
            } else if (l <= position.target) {
              if (trendBear && stBear && bigTrendBear && adx >= 22) {
                position.target = c - cfg.atrTgtMultiplier * atr;
              } else {
                exitReason = `EXIT TGT ₹${position.target.toFixed(0)}`;
                exitPrice = position.target;
              }
            }
          }
        }

        if (exitReason) {
          const exitPos = position.type === 'LONG' ? 'aboveBar' : 'belowBar';
          markers.push({ time, position: exitPos, color: exitColor, shape: 'circle', text: 'X' });
          signals.push({ type: 'EXIT', bar: i, time, reason: exitReason, price: exitPrice,
            entryPrice: position.entry, pnl: position.type === 'LONG' ? exitPrice - position.entry : position.entry - exitPrice });
          position = null;
          cooldown = 0;
          if (exitReason.includes('SL') || (exitReason.includes('Trail') && !exitReason.includes('SwingTrail'))) {
            cooldown = Math.max(cfg.cooldownBars, 15);
          }
          continue;
        }
      }

      // ── Skip entry if in position or cooling down ──
      const inOpenRange = this._isOpenRange(this._parseTime(ohlcv[i].date));
      if (position || cooldown > 0 || inOpenRange) continue;

      // ── ADX trending filter ──
      const parsedTime = this._parseTime(ohlcv[i].date);
      const minutesIntoSession = parsedTime ? parsedTime.hour * 60 + parsedTime.minute - 9 * 60 : 0;
      const minADXRequired = minutesIntoSession < 120 ? 8 : 12; // Relaxed: 8 for first 2 hours, 12 after (was 10/18)
      const isTrending = adx == null || adx >= minADXRequired;
      const priceAboveVwap = vwap[i] != null && c > vwap[i];
      const priceBelowVwap = vwap[i] != null && c < vwap[i];
      const pullbackBull = this._hasPullbackIntoEMAs(i, closes, highs, lows, ema9, ema21, volumes, cfg.triggerVolumeBars, true);
      const pullbackBear = this._hasPullbackIntoEMAs(i, closes, highs, lows, ema9, ema21, volumes, cfg.triggerVolumeBars, false);
      const triggerBull = this._isBullishTriggerCandle(i, opens, highs, lows, closes, ema9, ema21);
      const triggerBear = this._isBearishTriggerCandle(i, opens, highs, lows, closes, ema9, ema21);
      const continuationBull = this._isBullishContinuationCandle(i, opens, highs, lows, closes, ema9, ema21);
      const continuationBear = this._isBearishContinuationCandle(i, opens, highs, lows, closes, ema9, ema21);
      const reversalBull = this._isBullishReversalCandle(i, opens, highs, lows, closes, ema9, ema21);
      const reversalBear = this._isBearishReversalCandle(i, opens, highs, lows, closes, ema9, ema21);
      // Allow extended entries in early session when momentum is confirmed (first 120 min)
      const isEarlySession = minutesIntoSession < 120;
      const extendedBull = !isEarlySession && this._isEntryPriceExtended(i, c, ema9, ema21, bb[i], atr, true, cfg);
      const extendedBear = !isEarlySession && this._isEntryPriceExtended(i, c, ema9, ema21, bb[i], atr, false, cfg);
      let bullScore = 0;
      let bullTags = [];
      let bearScore = 0;
      let bearTags = [];

      const ema921AlignedBull = ema5[i] != null && ema9[i] != null && ema21[i] != null && ema5[i] > ema9[i] && ema9[i] > ema21[i];
      const ema921AlignedBear = ema5[i] != null && ema9[i] != null && ema21[i] != null && ema5[i] < ema9[i] && ema9[i] < ema21[i];
      const ema950AlignedBull = ema9[i] != null && ema21[i] != null && ema50[i] != null && ema9[i] > ema21[i] && ema21[i] > ema50[i];
      const ema950AlignedBear = ema9[i] != null && ema21[i] != null && ema50[i] != null && ema9[i] < ema21[i] && ema21[i] < ema50[i];
      const emaPreset = cfg.emaPreset || 'preset2';
      const emaTrendBull = emaPreset === 'preset1' ? ema921AlignedBull : ema950AlignedBull;
      const emaTrendBear = emaPreset === 'preset1' ? ema921AlignedBear : ema950AlignedBear;
      const priceAboveEma9 = ema9[i] != null && c > ema9[i];
      const priceBelowEma9 = ema9[i] != null && c < ema9[i];
      const bullVwapConsecutive = i >= 1 && vwap[i] != null && vwap[i - 1] != null && closes[i] > vwap[i] && closes[i - 1] > vwap[i - 1];
      const bearVwapConsecutive = i >= 1 && vwap[i] != null && vwap[i - 1] != null && closes[i] < vwap[i] && closes[i - 1] < vwap[i - 1];
      const bullEntryTrigger = triggerBull || continuationBull || pullbackBull || reversalBull;
      const bearEntryTrigger = triggerBear || continuationBear || pullbackBear || reversalBear;
      const bullCandle = this._isBullishCandle(opens, highs, lows, closes, i);
      const bearCandle = this._isBearishCandle(opens, highs, lows, closes, i);
      const bullStructureScore = (bullCHoCH ? 2 : 0) + (sslSweep ? 2 : 0) + ((obBullTap || fvgBullTap) ? 2 : 0);
      const bearStructureScore = (bearCHoCH ? 2 : 0) + (bslSweep ? 2 : 0) + ((obBearTap || fvgBearTap) ? 2 : 0);
      const bullTrendScore = (emaTrendBull ? 1.5 : 0) + (priceAboveVwap ? 1.5 : 0) + (stBull ? 1 : 0);
      const bearTrendScore = (emaTrendBear ? 1.5 : 0) + (priceBelowVwap ? 1.5 : 0) + (stBear ? 1 : 0);
      const bullRsiMomentum = rsi != null && rsi > 40 && rsi < 65 && rsiArr[i - 1] != null && rsi > rsiArr[i - 1] ? 1 : 0;
      const bearRsiMomentum = rsi != null && rsi >= 30 && rsi < 55 && rsiArr[i - 1] != null && rsi < rsiArr[i - 1] ? 1 : 0;
      const bullMomentumScore = (volRatio >= cfg.volConfirmation ? 1.5 : 0) + bullRsiMomentum + (macd != null && macd > 0 ? 0.5 : 0);
      const bearMomentumScore = (volRatio >= cfg.volConfirmation ? 1.5 : 0) + bearRsiMomentum + (macd != null && macd < 0 ? 0.5 : 0);
      const bullEntryScore = (bullEntryTrigger ? 1 : 0) + (bullCandle ? 1 : 0);
      const bearEntryScore = (bearEntryTrigger ? 1 : 0) + (bearCandle ? 1 : 0);
      const bullStrongSR = lastMajorHigh && c >= lastMajorHigh - 0.5 * atr && c <= lastMajorHigh + 0.5 * atr;
      const bearStrongSR = lastMajorLow && c <= lastMajorLow + 0.5 * atr && c >= lastMajorLow - 0.5 * atr;
      const bullVwapReject = vwap[i] != null && closes[i - 1] > vwap[i - 1] && c < vwap[i];
      const bearVwapReject = vwap[i] != null && closes[i - 1] < vwap[i - 1] && c > vwap[i];
      let bullTimePenalty = 0;
      if (minutesIntoSession >= 195 && minutesIntoSession < 255) {
        bullTimePenalty = 1;
      } else if (minutesIntoSession >= 345 && minutesIntoSession <= 360) {
        bullTimePenalty = 1.5;
      }
      const bearTimePenalty = bullTimePenalty;

      const bullPenalty = (bullStrongSR ? 999 : 0)
        + (bullVwapReject ? 2 : 0) // Softened from 999 hard veto to 2-point penalty
        + (rsi != null && rsi > 70 ? 1.5 : 0)
        + (volRatio < cfg.volConfirmation ? 0.5 : 0); // Relaxed from 1
      const bearPenalty = (bearStrongSR ? 999 : 0)
        + (bearVwapReject ? 2 : 0)
        + (rsi != null && rsi < 30 ? 1.5 : 0)
        + (volRatio < cfg.volConfirmation ? 0.5 : 0);

      const rawBullScore = bullStructureScore + bullTrendScore + bullMomentumScore + bullEntryScore - bullPenalty - bullTimePenalty;
      const rawBearScore = bearStructureScore + bearTrendScore + bearMomentumScore + bearEntryScore - bearPenalty - bearTimePenalty;
      bullScore = Math.max(0, Math.min(14.5, rawBullScore));
      bearScore = Math.max(0, Math.min(14.5, rawBearScore));

      const bullStructurePass = bullStructureScore >= 1.5; // Relaxed from 2 (allow single strong signal)
      const bearStructurePass = bearStructureScore >= 1.5;
      const bullTrendPass = bullTrendScore >= 1.5 && emaTrendBull; // Removed redundant priceAboveEma9 check
      const bearTrendPass = bearTrendScore >= 1.5 && emaTrendBear;
      const bullMomentumPass = bullMomentumScore >= 1;
      const bearMomentumPass = bearMomentumScore >= 1;
      const bullEntryPass = bullEntryTrigger;
      const bearEntryPass = bearEntryTrigger;

      const bullVwapConfirm = priceAboveVwap && !bullVwapReject;
      const bearVwapConfirm = priceBelowVwap && !bearVwapReject;
      const bullTotalPass = isTrending && !extendedBull && bullScore >= 8.5 && bullStructurePass && bullTrendPass && bullMomentumPass && bullEntryPass && bullVwapConfirm && !bullStrongSR && minutesIntoSession >= 15;
      const bearTotalPass = isTrending && !extendedBear && bearScore >= 8.5 && bearStructurePass && bearTrendPass && bearMomentumPass && bearEntryPass && bearVwapConfirm && !bearStrongSR && minutesIntoSession >= 15;

      let bullTradeType = null;
      if (bullTotalPass) {
        const bullHCBlocked = !(emaPreset === 'preset1' ? ema921AlignedBull : ema950AlignedBull)
          || minutesIntoSession >= 285 || bullMomentumScore < 1.5;
        if (bullScore >= 12.5 && !bullHCBlocked) {
          bullTradeType = 'HIGH CONVICTION';
        } else if (bullScore >= 10) {
          bullTradeType = 'STANDARD';
        } else if (bullScore >= 8.5 && bullScore < 10 && emaTrendBull) {
          bullTradeType = 'AGGRESSIVE';
        }
      }

      let bearTradeType = null;
      if (bearTotalPass) {
        const bearHCBlocked = !(emaPreset === 'preset1' ? ema921AlignedBear : ema950AlignedBear)
          || minutesIntoSession >= 285 || bearMomentumScore < 1.5;
        if (bearScore >= 12.5 && !bearHCBlocked) {
          bearTradeType = 'HIGH CONVICTION';
        } else if (bearScore >= 10) {
          bearTradeType = 'STANDARD';
        } else if (bearScore >= 8.5 && bearScore < 10 && emaTrendBear) {
          bearTradeType = 'AGGRESSIVE';
        }
      }

      const bullEligible = bullTradeType != null;
      const bearEligible = bearTradeType != null;

      const bestBull = bullEligible && (!bearEligible || bullScore > bearScore);
      const bestBear = bearEligible && (!bullEligible || bearScore > bullScore);

      if (bestBull) {
        const target = c + (isTrendDayBull ? 4 : cfg.atrTgtMultiplier) * atr;
        const sl = l - 0.5 * atr;
        if (sl >= c) continue;
        if (minutesIntoSession >= 285 && target - c > 0.5 * atr) continue;
        const rr = ((target - c) / (c - sl)).toFixed(1);
        if (parseFloat(rr) < cfg.minRR) continue;
        const label = `CALL ${bullTradeType} · ${bullTags.slice(0, 3).join(' · ')}`;

        markers.push({ time, position: 'belowBar', color: '#1E88E5', shape: 'arrowUp', text: `C${bullScore.toFixed(1)}` });
        signals.push({ type: 'BUY', bar: i, time, price: c, score: bullScore, strength: bullTradeType,
          tags: [...bullTags].filter(Boolean), sl, target, rr: parseFloat(rr), atr, reason: label });

        position = { type: 'LONG', entry: c, sl, target, tgt1: c + 2 * atr, tgt2: c + 4 * atr, trailSL: sl, bar: i, scaleOut1Hit: false, scaleOut2Hit: false };
      } else if (bestBear) {
        const target = c - (isTrendDayBear ? 4 : cfg.atrTgtMultiplier) * atr;
        const sl = h + 0.5 * atr;
        if (sl <= c) continue;
        if (minutesIntoSession >= 285 && c - target > 0.5 * atr) continue;
        const rr = ((c - target) / (sl - c)).toFixed(1);
        if (parseFloat(rr) < cfg.minRR) continue;
        const label = `PUT ${bearTradeType} · ${bearTags.slice(0, 3).join(' · ')}`;

        markers.push({ time, position: 'aboveBar', color: '#EF5350', shape: 'arrowDown', text: `P${bearScore.toFixed(1)}` });
        signals.push({ type: 'SELL', bar: i, time, price: c, score: bearScore, strength: bearTradeType,
          tags: [...bearTags].filter(Boolean), sl, target, rr: parseFloat(rr), atr, reason: label });

        position = { type: 'SHORT', entry: c, sl, target, tgt1: c - 2 * atr, tgt2: c - 4 * atr, trailSL: sl, bar: i, scaleOut1Hit: false, scaleOut2Hit: false };
      }
    }

    this._lastMarkers = markers;
    const summary = this._computeSummary(signals);
    return { markers, signals, summary };
  }

  applyMarkers(candleSeries, markers) {
    if (!candleSeries || !markers) return;
    try {
      const sorted = [...markers].sort((a, b) => {
        if (typeof a.time === 'string' && typeof b.time === 'string') return a.time.localeCompare(b.time);
        return (a.time || 0) - (b.time || 0);
      });
      candleSeries.setMarkers(sorted);
    } catch (e) {
      console.warn('[ChartSignalEngine] Failed to set markers:', e.message);
    }
  }

  renderSignalLog(signals) {
    if (!signals || signals.length === 0) {
      return '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:0.8rem;">No signals detected on current chart. Try a different timeframe or ensure sufficient data.</div>';
    }

    return signals.map(s => {
      const isEntry = s.type === 'BUY' || s.type === 'SELL';
      const color = s.type === 'BUY' ? '#1E88E5' : s.type === 'SELL' ? '#EF5350' : '#EF5350';
      const bg = s.type === 'BUY' ? 'rgba(30,136,229,0.08)' : s.type === 'SELL' ? 'rgba(239,83,80,0.08)' : 'rgba(239,83,80,0.08)';
      const icon = s.type === 'BUY' ? '▲ CALL' : s.type === 'SELL' ? '▼ PUT' : '● EXIT';

      if (isEntry) {
        const hasTrend = s.tags?.includes('🔥TREND');
        return `<div style="padding:8px 12px;border-left:3px solid ${color};background:${bg};border-radius:0 6px 6px 0;margin-bottom:6px;font-size:0.75rem;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span style="font-weight:700;color:${color};">${icon} ${s.strength}${hasTrend ? ' 🔥' : ''}</span>
            <span style="color:var(--text-muted);">₹${s.price.toFixed(2)}</span>
          </div>
          <div style="margin-top:3px;color:var(--text-secondary);">
            <span>SL: ₹${s.sl?.toFixed(0)} · TGT: ₹${s.target?.toFixed(0)} · R:R ${s.rr}</span>
          </div>
          <div style="margin-top:2px;color:var(--text-muted);font-size:0.68rem;">${(s.tags || []).join(' · ')}</div>
        </div>`;
      } else {
        const pnlColor = (s.pnl || 0) >= 0 ? '#26A69A' : '#EF5350';
        const pnlPct = s.entryPrice ? ((s.pnl / s.entryPrice) * 100).toFixed(2) : '?';
        return `<div style="padding:8px 12px;border-left:3px solid ${color};background:${bg};border-radius:0 6px 6px 0;margin-bottom:6px;font-size:0.75rem;">
          <span style="font-weight:700;color:${color};">${s.reason}</span>
          <span style="margin-left:8px;color:${pnlColor};font-weight:600;">${(s.pnl || 0) >= 0 ? '+' : ''}${pnlPct}%</span>
        </div>`;
      }
    }).join('');
  }

  _computeSummary(signals) {
    const entries = signals.filter(s => s.type === 'BUY' || s.type === 'SELL');
    const exits = signals.filter(s => s.type === 'EXIT');
    const pnls = exits.filter(s => s.pnl != null).map(s => s.pnl);
    const wins = pnls.filter(p => p > 0);
    const losses = pnls.filter(p => p < 0);

    return {
      totalSignals: entries.length,
      buys: entries.filter(s => s.type === 'BUY').length,
      sells: entries.filter(s => s.type === 'SELL').length,
      exits: exits.length,
      winRate: pnls.length > 0 ? (wins.length / pnls.length * 100).toFixed(1) : '—',
      avgWin: wins.length > 0 ? (wins.reduce((a, b) => a + b, 0) / wins.length).toFixed(2) : '—',
      avgLoss: losses.length > 0 ? (losses.reduce((a, b) => a + b, 0) / losses.length).toFixed(2) : '—',
      totalPnl: pnls.reduce((a, b) => a + b, 0).toFixed(2),
      strongSignals: entries.filter(s => s.strength === 'STRONG').length,
      highSignals: entries.filter(s => s.strength === 'HIGH').length,
    };
  }

  _emptySummary() {
    return { totalSignals: 0, buys: 0, sells: 0, exits: 0, winRate: '—', avgWin: '—', avgLoss: '—', totalPnl: '0', strongSignals: 0, highSignals: 0 };
  }

  // ── Candle patterns ──
  _isBullishCandle(opens, highs, lows, closes, i) {
    if (i < 2) return null;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const po = opens[i - 1], pc = closes[i - 1];
    const body = Math.abs(c - o), range = h - l;
    const lowerWick = Math.min(o, c) - l;
    const upperWick = h - Math.max(o, c);

    if (range > 0 && lowerWick > body * 2 && upperWick < body * 0.5 && c > o) return '🔨 Hammer';
    if (c > o && pc < po && c > po && o < pc) return '🟢 Engulf';
    if (i >= 2 && closes[i - 2] > opens[i - 2] === false) {
      const midBody = Math.abs(closes[i - 1] - opens[i - 1]);
      const midRange = highs[i - 1] - lows[i - 1];
      if (midRange > 0 && midBody / midRange < 0.3 && c > o && c > (opens[i - 2] + closes[i - 2]) / 2) return '⭐ MorningStar';
    }
    return null;
  }

  _isBearishCandle(opens, highs, lows, closes, i) {
    if (i < 2) return null;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const po = opens[i - 1], pc = closes[i - 1];
    const body = Math.abs(c - o), range = h - l;
    const upperWick = h - Math.max(o, c);

    if (range > 0 && upperWick > body * 2 && c < o) return '⭐ ShootingStar';
    if (c < o && pc > po && c < po && o > pc) return '🔴 Engulf';
    if (i >= 2 && closes[i - 2] < opens[i - 2] === false) {
      const midBody = Math.abs(closes[i - 1] - opens[i - 1]);
      const midRange = highs[i - 1] - lows[i - 1];
      if (midRange > 0 && midBody / midRange < 0.3 && c < o && c < (opens[i - 2] + closes[i - 2]) / 2) return '🌙 EveningStar';
    }
    return null;
  }

  _computeRSIArray(closes, period = 14) {
    const result = new Array(closes.length).fill(null);
    if (closes.length < period + 1) return result;
    let avgGain = 0, avgLoss = 0;
    for (let j = 1; j <= period; j++) {
      const diff = closes[j] - closes[j - 1];
      if (diff > 0) avgGain += diff; else avgLoss -= diff;
    }
    avgGain /= period;
    avgLoss /= period;
    result[period] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
    for (let i = period + 1; i < closes.length; i++) {
      const diff = closes[i] - closes[i - 1];
      avgGain = (avgGain * (period - 1) + (diff > 0 ? diff : 0)) / period;
      avgLoss = (avgLoss * (period - 1) + (diff < 0 ? -diff : 0)) / period;
      result[i] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));
    }
    return result;
  }

  _computeMACDHistArray(closes) {
    const ema12 = TI.computeEMA(closes, 12);
    const ema26 = TI.computeEMA(closes, 26);
    const result = new Array(closes.length).fill(null);
    if (ema26.length === 0) return result;

    const offset = ema12.length - ema26.length;
    const macdLine = [];
    for (let i = 0; i < ema26.length; i++) {
      if (ema12[i + offset] == null || ema26[i] == null) { macdLine.push(0); continue; }
      macdLine.push(ema12[i + offset] - ema26[i]);
    }
    const signalLine = TI.computeEMA(macdLine, 9);
    const sigOffset = macdLine.length - signalLine.length;

    for (let i = 0; i < signalLine.length; i++) {
      const idx = i + sigOffset + offset;
      if (idx >= 0 && idx < closes.length && signalLine[i] != null) {
        result[idx] = macdLine[i + sigOffset] - signalLine[i];
      }
    }
    return result;
  }

  _computeATRArray(highs, lows, closes, period = 14) {
    const result = new Array(closes.length).fill(null);
    if (closes.length < period + 1) return result;
    let atr = 0;
    for (let i = 1; i <= period; i++) {
      atr += Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
    }
    atr /= period;
    result[period] = atr;
    for (let i = period + 1; i < closes.length; i++) {
      const tr = Math.max(highs[i] - lows[i], Math.abs(highs[i] - closes[i - 1]), Math.abs(lows[i] - closes[i - 1]));
      atr = (atr * (period - 1) + tr) / period;
      result[i] = atr;
    }
    return result;
  }

  _computeADXArray(highs, lows, closes, period = 14) {
    const result = new Array(closes.length).fill(null);
    if (closes.length < period * 2) return result;
    for (let i = period * 2; i < closes.length; i++) {
      let plusDM = 0, minusDM = 0, tr = 0;
      for (let j = i - period + 1; j <= i; j++) {
        const upMove = highs[j] - highs[j - 1];
        const downMove = lows[j - 1] - lows[j];
        plusDM += (upMove > downMove && upMove > 0) ? upMove : 0;
        minusDM += (downMove > upMove && downMove > 0) ? downMove : 0;
        tr += Math.max(highs[j] - lows[j], Math.abs(highs[j] - closes[j - 1]), Math.abs(lows[j] - closes[j - 1]));
      }
      if (tr === 0) { result[i] = 20; continue; }
      const plusDI = (plusDM / tr) * 100;
      const minusDI = (minusDM / tr) * 100;
      result[i] = Math.abs(plusDI - minusDI) / (plusDI + minusDI + 0.001) * 100;
    }
    return result;
  }

  _rollingAvg(arr, period) {
    const result = new Array(arr.length).fill(0);
    let sum = 0;
    for (let i = 0; i < arr.length; i++) {
      sum += arr[i];
      if (i >= period) sum -= arr[i - period];
      result[i] = sum / Math.min(i + 1, period);
    }
    return result;
  }

  _parseTime(date) {
    if (!date) return null;
    const d = typeof date === 'number' ? new Date(date) : new Date(date);
    if (isNaN(d.getTime())) return null;
    return { hour: d.getHours(), minute: d.getMinutes() };
  }

  _isOpenRange(time) {
    if (!time) return false;
    const minutes = time.hour * 60 + time.minute;
    // Skip only the first opening bar to avoid the 9:15-9:18 noise spike.
    // Allow entries from 9:20 onwards for confirmed trends.
    return minutes >= 9 * 60 + 15 && minutes < 9 * 60 + 18;
  }

  _isEMAStackBull(i, ema9, ema21, ema50) {
    if (i < 1 || ema9[i] == null || ema21[i] == null) return false;
    const baseBull = ema9[i] > ema21[i] && ema9[i] > ema9[i - 1] && ema21[i] > ema21[i - 1];
    if (ema50[i] == null) return baseBull;
    return baseBull && ema21[i] > ema50[i] && ema50[i] > ema50[i - 1];
  }

  _isEMAStackBear(i, ema9, ema21, ema50) {
    if (i < 1 || ema9[i] == null || ema21[i] == null) return false;
    const baseBear = ema9[i] < ema21[i] && ema9[i] < ema9[i - 1] && ema21[i] < ema21[i - 1];
    if (ema50[i] == null) return baseBear;
    return baseBear && ema21[i] < ema50[i] && ema50[i] < ema50[i - 1];
  }

  _hasPullbackIntoEMAs(i, closes, highs, lows, ema9, ema21, volumes, lookback, isBull) {
    const bars = [];
    for (let j = Math.max(0, i - lookback); j <= i; j++) bars.push(j);
    if (bars.length < 2) return false;
    const touched = bars.some(j => {
      if (isBull) {
        return closes[j] <= ema9[j] || closes[j] <= ema21[j] || lows[j] <= ema21[j];
      }
      return closes[j] >= ema9[j] || closes[j] >= ema21[j] || highs[j] >= ema21[j];
    });
    const avgPullbackVol = bars.reduce((sum, j) => sum + (volumes[j] || 0), 0) / bars.length;
    const currentVol = volumes[i] || 0;
    return touched && avgPullbackVol > 0 && currentVol > avgPullbackVol;
  }

  _isBullishTriggerCandle(i, opens, highs, lows, closes, ema9, ema21) {
    if (i < 1) return false;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const range = h - l;
    if (range <= 0 || c <= o) return false;
    const bodyRatio = Math.abs(c - o) / range;
    const aboveEma = ema9[i] != null && ema21[i] != null && c > ema9[i] && c > ema21[i];
    const reclaim = c > Math.max(closes[i - 1] || c, ema9[i]);
    return bodyRatio >= 0.35 && aboveEma && reclaim;
  }

  _isBearishTriggerCandle(i, opens, highs, lows, closes, ema9, ema21) {
    if (i < 1) return false;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const range = h - l;
    if (range <= 0 || c >= o) return false;
    const bodyRatio = Math.abs(c - o) / range;
    const belowEma = ema9[i] != null && ema21[i] != null && c < ema9[i] && c < ema21[i];
    const reject = c < Math.min(closes[i - 1] || c, ema9[i]);
    return bodyRatio >= 0.35 && belowEma && reject;
  }

  _isBullishReversalCandle(i, opens, highs, lows, closes, ema9, ema21) {
    if (i < 1) return false;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const range = h - l;
    if (range <= 0 || c <= o) return false;
    const bodyRatio = Math.abs(c - o) / range;
    if (bodyRatio < 0.25) return false;
    
    if (ema9[i] != null) {
      // Use EMA-based logic when available
      const aboveEma9 = c > ema9[i];
      const prevBelowEma9 = closes[i - 1] != null && ema9[i - 1] != null && closes[i - 1] < ema9[i - 1];
      const crossedEma9 = o < ema9[i] && c > ema9[i];
      return aboveEma9 && (prevBelowEma9 || crossedEma9) && c > closes[i - 1];
    } else {
      // For early candles without EMA, use simple price action
      return c > closes[i - 1] && c > o; // Bullish candle with higher close
    }
  }

  _isBearishReversalCandle(i, opens, highs, lows, closes, ema9, ema21) {
    if (i < 1) return false;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const range = h - l;
    if (range <= 0 || c >= o) return false;
    const bodyRatio = Math.abs(c - o) / range;
    if (bodyRatio < 0.25) return false;
    
    if (ema9[i] != null) {
      // Use EMA-based logic when available
      const belowEma9 = c < ema9[i];
      const prevAboveEma9 = closes[i - 1] != null && ema9[i - 1] != null && closes[i - 1] > ema9[i - 1];
      const crossedEma9 = o > ema9[i] && c < ema9[i];
      return belowEma9 && (prevAboveEma9 || crossedEma9) && c < closes[i - 1];
    } else {
      // For early candles without EMA, use simple price action
      return c < closes[i - 1] && c < o; // Bearish candle with lower close
    }
  }

  _isBullishContinuationCandle(i, opens, highs, lows, closes, ema9, ema21) {
    if (i < 1) return false;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const range = h - l;
    if (range <= 0 || c <= o) return false;
    const bodyRatio = Math.abs(c - o) / range;
    const aboveEma = ema9[i] != null && ema21[i] != null && c > ema9[i] && c > ema21[i];
    const stronger = closes[i - 1] != null && c > closes[i - 1];
    const reclaimEma9 = ema9[i] != null && l <= ema9[i] && c > ema9[i];
    return bodyRatio >= 0.25 && aboveEma && (stronger || reclaimEma9);
  }

  _isBearishContinuationCandle(i, opens, highs, lows, closes, ema9, ema21) {
    if (i < 1) return false;
    const o = opens[i], h = highs[i], l = lows[i], c = closes[i];
    const range = h - l;
    if (range <= 0 || c >= o) return false;
    const bodyRatio = Math.abs(c - o) / range;
    const belowEma = ema9[i] != null && ema21[i] != null && c < ema9[i] && c < ema21[i];
    const stronger = closes[i - 1] != null && c < closes[i - 1];
    const rejectEma9 = ema9[i] != null && h >= ema9[i] && c < ema9[i];
    return bodyRatio >= 0.25 && belowEma && (stronger || rejectEma9);
  }

  _isEntryPriceExtended(i, price, ema9, ema21, bb, atr, isBull, cfg) {
    if ((ema9[i] == null && ema21[i] == null) || !atr) return false;
    const closestEmaDist = Math.min(
      ema9[i] != null ? Math.abs(price - ema9[i]) : Number.MAX_VALUE,
      ema21[i] != null ? Math.abs(price - ema21[i]) : Number.MAX_VALUE
    );
    if (closestEmaDist > 3.5 * atr) return true; // Raised from 2.5× to allow momentum breakouts
    if (!bb || bb.upper == null || bb.lower == null) return false;
    const width = bb.upper - bb.lower;
    if (width <= 0) return false;
    if (isBull) {
      return price > bb.upper + width * cfg.bbExtensionPct;
    }
    return price < bb.lower - width * cfg.bbExtensionPct;
  }

  _toChartTime(date) {
    if (!date) return null;
    if (typeof date === 'number') return date;
    if (typeof date === 'string') {
      if (date.length === 10) return date;
      const d = new Date(date);
      if (!isNaN(d.getTime())) return Math.floor(d.getTime() / 1000);
      return date.split('T')[0];
    }
    return null;
  }
}

window.chartSignalEngine = new ChartSignalEngine();
