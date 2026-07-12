/**
 * TradeSignal — Multi-Factor Scoring Engine
 * 
 * Equity Scoring (5-factor, F&O stocks only):
 *   Technical Momentum (30) + Price Action (25) + Volume (15) + Fundamentals (20) + Sector Momentum (10) = 100
 *   → When Fundamentals/Sector data is missing, scores are normalized to 100-point scale.
 *
 * Options Scoring (7-factor):
 *   Intraday Momentum (20) + Volume & Block Deals (20) + OI Build-up (20) + Options Structure (15)
 *   + Technical Trend (10) + Futures Signal (10) + Catalyst Bonus (5) = 100
 *
 * Delegates all indicator math to the shared TI module (technical-indicators.js).
 */
class ScoringEngine {
  constructor() {
    this.equityWeights = {
      technical: 30,
      priceAction: 25,
      volume: 15,
      fundamentals: 20,
      sectorMomentum: 10
    };

    this.optionsWeights = {
      technical: 25,
      priceAction: 20,
      optionsStructure: 20,
      oiAnalysis: 15,
      volatility: 10,
      catalyst: 10
    };

    // Sector momentum populated by scoreBatch() after full scan
    this._sectorScores = {};
  }

  // ── Technical Indicators — delegates to shared TI module ──
  // Public API preserved for backward-compatibility (app.js, charts, etc.)
  computeEMA(data, period) { return TI.computeEMA(data, period); }
  computeRSI(closes, period = 14) { return TI.computeRSI(closes, period); }
  computeMACD(closes) { return TI.computeMACD(closes); }
  computeADX(highs, lows, closes, period = 14) { return TI.computeADX(highs, lows, closes, period); }
  computeATR(highs, lows, closes, period = 14) { return TI.computeATR(highs, lows, closes, period); }
  computeBollingerWidth(closes, period = 20) { return TI.computeBollingerWidth(closes, period); }
  computeBollingerBands(closes, period = 20, multiplier = 2) { return TI.computeBollingerBands(closes, period, multiplier); }
  computeVWAP(ohlcv) { return TI.computeVWAP(ohlcv); }
  computeIntradayVWAP(ohlcv) { return TI.computeIntradayVWAP(ohlcv); }
  computeSupertrend(ohlcv, period = 10, multiplier = 3) { return TI.computeSupertrend(ohlcv, period, multiplier); }
  computeVolumeRatio(volumes, period = 20) { return TI.computeVolumeRatio(volumes, period); }

  // ── EQUITY SCORING (5-Factor Refined Model for BULLISH/BEARISH) ──
  // Model: Technical Momentum (30pt) | Price Action (25pt) | Volume & Distribution (15pt) |
  //        Market Context (20pt) | Sector Momentum (10pt) = 100pt
  //
  // Each factor has BULLISH-specific and BEARISH-specific rules. Direction is determined
  // by structural trend (Price > EMA21 = BULLISH, else BEARISH). Scoring applies the
  // relevant rules for the detected direction.
  //
  // Mandatory PUT Filters (Very Important):
  //   • Avoid PUT if index strong uptrend (trap risk)
  //   • Avoid PUT if stock already >3–4% down (late entry)
  //   • Prefer: Breakdown + pullback + rejection (best R:R)
  //
  // Removed (Intentionally):
  //   • Fundamentals (no intraday edge, removed from equity scoring)
  //   • Static 20-day proximity (replaced with dynamic breakdown behavior)
  //   • Neutral volume (replaced with directional volume logic)
  scoreEquity(data) {
    const {
      closes, highs, lows, volumes, sectorData = {}, optionsData = {},
      // Intraday enrichments (from equity-screener pass)
      closes15m, rsi15m: rsi15mRaw, sessionVwap,
      pdh, pdl, pivot, r1, s1,
      isExpiryWeek = false, niftyBias = 'NEUTRAL'
    } = data;
    const factors = {};

    // ── Core technical indicators ──
    const rsi  = this.computeRSI(closes);
    const macd = this.computeMACD(closes);
    const adx  = this.computeADX(highs, lows, closes);
    const ema9  = this.computeEMA(closes, 9);
    const ema21 = this.computeEMA(closes, 21);
    const ema50 = this.computeEMA(closes, 50);
    const atr      = this.computeATR(highs, lows, closes);
    const bbWidth  = this.computeBollingerWidth(closes);
    // Use 5-day vol period for intraday responsiveness
    const volRatio = this.computeVolumeRatio(volumes, 5);

    // Blend daily RSI with 15-min RSI for better intraday timing
    const effectiveRsi = (closes15m && rsi15mRaw)
      ? Math.round(rsi * 0.4 + rsi15mRaw * 0.6)
      : rsi;

    const lastClose  = closes[closes.length - 1];
    const prevClose  = closes[closes.length - 2] || lastClose;
    const ema9Last   = ema9[ema9.length - 1]   || lastClose;
    const ema21Last  = ema21[ema21.length - 1]  || lastClose;
    const ema50Last  = ema50[ema50.length - 1]  || lastClose;
    const changePercent = ((lastClose - prevClose) / prevClose) * 100;

    // ── Determine structural trend ──
    // BULLISH: Price > EMA21, else BEARISH
    const isBullishTrend = lastClose > ema21Last;

    // ── Session VWAP alignment (intraday context) ──
    const vwapAvail = sessionVwap && sessionVwap > 0;
    const priceAboveVwap = vwapAvail && lastClose > sessionVwap;

    // ── 1. TECHNICAL MOMENTUM (30pts) ──
    // BULLISH: Price > EMA9 > EMA21 (5pt), EMA21 > EMA50 (5pt), MACD+ (8pt), RSI 45–65 (7pt), ADX >25 (5pt)
    // BEARISH: Price < EMA9 < EMA21 (5pt), EMA21 < EMA50 (5pt), MACD- (8pt), RSI 35–50 OR falling from >60 (7pt), ADX >25 (5pt)
    let techScore = 0;

    if (isBullishTrend) {
      // BULLISH Rules
      // Price > EMA9 > EMA21 (5pt)
      if (lastClose > ema9Last && ema9Last > ema21Last) techScore += 5;
      else if (lastClose > ema21Last && ema9Last > ema21Last) techScore += 3;
      else if (lastClose > ema21Last) techScore += 1;

      // EMA21 > EMA50 (5pt)
      if (ema21Last > ema50Last) techScore += 5;
      else if (Math.abs(ema21Last - ema50Last) < (ema50Last * 0.01)) techScore += 2;

      // MACD+ (8pt)
      if (macd.histogram > 0) techScore += 8;
      else if (macd.histogram > -0.0001) techScore += 2;

      // RSI 45–65 (7pt) — use blended effectiveRsi for better intraday timing
      if (effectiveRsi >= 45 && effectiveRsi <= 65) techScore += 7;
      else if (effectiveRsi >= 40 && effectiveRsi <= 70) techScore += 4;
      else if (effectiveRsi >= 30 && effectiveRsi <= 75) techScore += 1;

      // ADX >25 (5pt)
      if (adx > 25) techScore += 5;
      else if (adx > 20) techScore += 3;

      // Session VWAP alignment (3pt bonus — price above VWAP confirms bullish bias)
      if (vwapAvail) {
        if (priceAboveVwap) techScore += 3;
        else techScore -= 1; // counter-VWAP = weaker setup
      }
    } else {
      // BEARISH Rules
      // Price < EMA9 < EMA21 (5pt)
      if (lastClose < ema9Last && ema9Last < ema21Last) techScore += 5;
      else if (lastClose < ema21Last && ema9Last < ema21Last) techScore += 3;
      else if (lastClose < ema21Last) techScore += 1;

      // EMA21 < EMA50 (5pt)
      if (ema21Last < ema50Last) techScore += 5;
      else if (Math.abs(ema21Last - ema50Last) < (ema50Last * 0.01)) techScore += 2;

      // MACD- (8pt)
      if (macd.histogram < 0) techScore += 8;
      else if (macd.histogram < 0.0001) techScore += 2;

      // RSI 35–50 OR falling from >60 (7pt) — blended effectiveRsi
      if (effectiveRsi >= 35 && effectiveRsi <= 50) techScore += 7;
      else if (effectiveRsi > 60 && effectiveRsi <= 75) techScore += 3;  // falling from overbought
      else if (effectiveRsi >= 30 && effectiveRsi <= 60) techScore += 4;

      // ADX >25 (5pt)
      if (adx > 25) techScore += 5;
      else if (adx > 20) techScore += 3;

      // Session VWAP alignment (3pt bonus — price below VWAP confirms bearish bias)
      if (vwapAvail) {
        if (!priceAboveVwap) techScore += 3;
        else techScore -= 1; // price above VWAP = weaker short setup
      }
    }

    factors.technical = { score: Math.min(techScore, 25), max: 25, label: 'Technical Momentum', color: '#1E88E5' };

    // ── 2. PRICE ACTION (25pts) ──
    // BULLISH: Break above recent resistance (10pt), vol-supported breakout (8pt), price in upper 70% range (7pt)
    // BEARISH: Breakdown below support / 20D low (10pt), continuation after breakdown (5pt), rejection on pullback (5pt), price in lower 30% range (5pt)
    let paScore = 0;
    const high20 = Math.max(...highs.slice(-20));
    const low20 = Math.min(...lows.slice(-20));
    const range = high20 - low20;
    const posInRange = range > 0 ? (lastClose - low20) / range : 0.5;

    if (isBullishTrend) {
      // BULLISH: Break above recent resistance (10pt)
      if (lastClose >= high20 * 0.98) paScore += 10;
      else if (lastClose >= high20 * 0.95) paScore += 7;
      else if (lastClose >= high20 * 0.90) paScore += 4;

      // Vol-supported breakout (8pt)
      if (volRatio > 2) paScore += 8;
      else if (volRatio > 1.5) paScore += 5;
      else if (volRatio > 1.2) paScore += 2;

      // Price in upper 70% range (7pt)
      if (posInRange > 0.7) paScore += 7;
      else if (posInRange > 0.6) paScore += 4;
      else if (posInRange > 0.5) paScore += 2;

      // Breakout quality: close in top 70% of today's candle = strong bar (3pt bonus, capped in factor max)
      const todayH = highs[highs.length - 1], todayL = lows[lows.length - 1];
      const cRange = todayH - todayL;
      if (cRange > 0 && (lastClose - todayL) / cRange > 0.7) paScore += 3;
    } else {
      // BEARISH: Breakdown below support / 20D low (10pt)
      if (lastClose <= low20 * 1.02) paScore += 10;
      else if (lastClose <= low20 * 1.05) paScore += 7;
      else if (lastClose <= low20 * 1.10) paScore += 4;

      // Multi-day continuation after breakdown (5pt) — 3-day trend, not single-day noise
      const avg3 = closes.length >= 3 ? (closes[closes.length-1] + closes[closes.length-2] + closes[closes.length-3]) / 3 : lastClose;
      const avg5 = closes.length >= 5 ? closes.slice(-5).reduce((a,b) => a+b,0) / 5 : lastClose;
      if (avg3 < avg5 * 0.99) paScore += 5;
      else if (avg3 < avg5) paScore += 2;

      // Rejection on pullback to EMA/VWAP (5pt) — bounce rejected at key level
      // Detect if price attempted pullback but rejected
      if (volumes.length >= 2) {
        const prevClose2 = closes[closes.length - 3] || prevClose;
        if (prevClose2 > ema21Last && lastClose < prevClose2) paScore += 5;  // pullback rejection
        else if (lastClose < ema21Last) paScore += 2;  // back below EMA
      }

      // Price in lower 30% range (5pt)
      if (posInRange < 0.3) paScore += 5;
      else if (posInRange < 0.4) paScore += 3;
      else if (posInRange < 0.5) paScore += 1;

      // Breakdown quality: close in bottom 30% of today's candle = strong bearish bar (3pt bonus)
      const todayH2 = highs[highs.length - 1], todayL2 = lows[lows.length - 1];
      const cRange2 = todayH2 - todayL2;
      if (cRange2 > 0 && (lastClose - todayL2) / cRange2 < 0.3) paScore += 3;
    }

    // ── PDH / PDL / Pivot S&R awareness ──
    // Reward setups with room to run; penalise stocks trapped at key S/R
    if (pivot !== null && pdh !== null && pdl !== null) {
      if (isBullishTrend) {
        const distToPdh = (pdh - lastClose) / lastClose;
        if (distToPdh < 0.003) paScore -= 3;          // stuck right at PDH resistance
        else if (lastClose > pivot && r1 && lastClose < r1) paScore += 2; // between pivot and R1 = room to run
        if (r1 && lastClose > r1) paScore += 2;       // above R1 = confirmed breakout level
      } else {
        const distToPdl = (lastClose - pdl) / lastClose;
        if (distToPdl < 0.003) paScore -= 3;          // sitting right at PDL support
        else if (lastClose < pivot && s1 && lastClose > s1) paScore += 2; // between S1 and pivot = room to fall
        if (s1 && lastClose < s1) paScore += 2;       // below S1 = confirmed breakdown level
      }
    }

    factors.priceAction = { score: Math.min(paScore, 25), max: 25, label: 'Price Action', color: '#00BCD4' };

    // ── 3. VOLUME & DISTRIBUTION (15pts) ──
    // BULLISH: Volume ≥2x avg (6pt), bullish candles with high volume (5pt), accumulation pattern (4pt)
    // BEARISH: Volume ≥2x avg (6pt), bearish candles with high volume (5pt), distribution (price ↓ + vol ↑) (4pt)
    let volScore = 0;

    // Volume ≥2x avg (6pt) — applies to both
    if (volRatio > 2) volScore += 6;
    else if (volRatio > 1.5) volScore += 4;
    else if (volRatio > 1.0) volScore += 2;

    // Directional candles with high volume (5pt)
    // BULLISH: higher close + high vol, BEARISH: lower close + high vol
    if (volumes.length >= 2) {
      const todayVol = volumes[volumes.length - 1];
      const prevDayVol = volumes[volumes.length - 2];
      if (isBullishTrend) {
        // Bullish candle (close higher) with high volume
        if (lastClose > prevClose && todayVol > prevDayVol * 1.2) volScore += 5;
        else if (lastClose > prevClose && todayVol > prevDayVol) volScore += 3;
      } else {
        // Bearish candle (close lower) with high volume
        if (lastClose < prevClose && todayVol > prevDayVol * 1.2) volScore += 5;
        else if (lastClose < prevClose && todayVol > prevDayVol) volScore += 3;
      }
    }

    // Accumulation (BULLISH) / Distribution (BEARISH) pattern (4pt)
    // BULLISH: price flat/rising + volume increasing = accumulation
    // BEARISH: price falling + volume increasing = distribution
    if (volumes.length >= 5) {
      const recentVol = volumes.slice(-3).reduce((a, b) => a + b, 0) / 3;
      const priorVol = volumes.slice(-5, -3).reduce((a, b) => a + b, 0) / 2;
      if (isBullishTrend) {
        // Accumulation: vol increasing AND price flat-to-positive (not falling)
        if (recentVol > priorVol * 1.3 && changePercent >= 0 && changePercent < 1.5) volScore += 4;
        else if (recentVol > priorVol * 1.1 && changePercent >= -0.3) volScore += 2;
      } else {
        // Distribution: vol increasing + price falling
        if (recentVol > priorVol * 1.3 && changePercent < -0.5) volScore += 4;
        else if (recentVol > priorVol * 1.1 && changePercent < 0) volScore += 2;
      }
    }

    factors.volume = { score: Math.min(volScore, 15), max: 15, label: 'Volume & Distribution', color: '#AB47BC' };

    // ── OBV Slope (inline, contributes to volume factor via bonus) ──
    // OBV: rising = buyers accumulating, falling = sellers distributing
    // Applied as post-hoc bonus to volume score (capped by factor max above)
    if (closes.length >= 10 && volumes.length >= 10) {
      let obv = 0;
      const obvArr = [];
      for (let i = 0; i < closes.length; i++) {
        if (i === 0) { obvArr.push(0); continue; }
        obv += closes[i] > closes[i-1] ? volumes[i] : closes[i] < closes[i-1] ? -volumes[i] : 0;
        obvArr.push(obv);
      }
      const obvRecent = obvArr.slice(-5).reduce((a,b) => a+b,0) / 5;
      const obvPrior  = obvArr.slice(-10,-5).reduce((a,b) => a+b,0) / 5;
      let obvBonus = 0;
      if (isBullishTrend && obvRecent > obvPrior) obvBonus = 2;       // OBV confirms uptrend
      else if (!isBullishTrend && obvRecent < obvPrior) obvBonus = 2; // OBV confirms downtrend
      else if (isBullishTrend && obvRecent < obvPrior) obvBonus = -1; // OBV divergence warning
      factors.volume.score = Math.min(factors.volume.score + obvBonus, 15);
    }

    // ── OI Build-up Confirmation (inline, contributes to volume factor via bonus) ──
    // Only applied outside of expiry week (rollover noise) and when aligned with trend
    if (!isExpiryWeek && optionsData.buildUp) {
      let oiBonus = 0;
      if (isBullishTrend) {
        if (optionsData.buildUp === 'long_buildup') oiBonus = 3;     // fresh longs confirm bull trend
        else if (optionsData.buildUp === 'short_covering') oiBonus = 1; // squeeze helps, but less conviction
      } else {
        if (optionsData.buildUp === 'short_buildup') oiBonus = 3;    // fresh shorts confirm bear trend
        else if (optionsData.buildUp === 'long_unwinding') oiBonus = 1; // bulls exiting helps, but less conviction
      }
      // Apply bonus, keeping within the 15-point factor max
      factors.volume.score = Math.min(factors.volume.score + oiBonus, 15);
    }

    // ── 4. MARKET CONTEXT (20pts) ──
    // BULLISH: Index > VWAP (8pt), index trend strong ADX >20 (6pt), sector strength >70% (6pt)
    // BEARISH: Index < VWAP (8pt), index trend strong ADX >20 (6pt), sector weakness <30% (6pt)
    // Using ADX as proxy for index trend and fundamentals data for sector
    let marketScore = 0;

    // Market alignment (8pt) — EMA50 structure as proxy for broader market direction
    // Avoids double-counting ADX which is already used in trend strength below
    if (isBullishTrend) {
      if (lastClose > ema50Last && ema21Last > ema50Last) marketScore += 8;
      else if (lastClose > ema50Last) marketScore += 4;
    } else {
      if (lastClose < ema50Last && ema21Last < ema50Last) marketScore += 8;
      else if (lastClose < ema50Last) marketScore += 4;
    }

    // Index trend strength (ADX >20) (6pt) — applies to both
    if (adx > 20) marketScore += 6;
    else if (adx > 15) marketScore += 3;

    // Sector alignment (6pt) — check sector data if available
    const sectorName = data.sector || '';
    const autoSectorData = sectorName ? this._sectorScores[sectorName] : null;
    const mergedSectorData = autoSectorData || sectorData;
    if (mergedSectorData?.relativeStrength) {
      const sectorStrength = mergedSectorData.relativeStrength || 50;
      if (isBullishTrend) {
        if (sectorStrength > 70) marketScore += 6;
        else if (sectorStrength > 60) marketScore += 3;
      } else {
        if (sectorStrength < 30) marketScore += 6;
        else if (sectorStrength < 40) marketScore += 3;
      }
    }

    // ── Nifty market bias alignment (4pt) ── Large-cap only
    // Only meaningful for index constituents (large-cap); mid-caps are stock-specific
    if (niftyBias !== 'NEUTRAL' && data.cap === 'large') {
      const aligned = (isBullishTrend && niftyBias === 'BULLISH') ||
                      (!isBullishTrend && niftyBias === 'BEARISH');
      if (aligned) marketScore += 4;
      else marketScore -= 2; // counter-market large-cap trade = elevated risk
    }

    // ── Expiry week context (-2pt caution — institutional hedging distorts signals) ──
    if (isExpiryWeek) marketScore = Math.max(0, marketScore - 2);

    factors.marketContext = { score: Math.min(marketScore, 15), max: 15, label: 'Market Context', color: '#26A69A' };

    // ── 5. SECTOR MOMENTUM (10pts) ──
    // BULLISH: Sector outperforming (7pt), positive rotation (3pt)
    // BEARISH: Sector underperforming (7pt), negative rotation (3pt)
    let sectorScore = 0;
    let hasRealSector = false;

    const sectorName2 = data.sector || '';
    const autoSectorData2 = sectorName2 ? this._sectorScores[sectorName2] : null;
    const mergedSector = autoSectorData2 || sectorData;

    if (mergedSector?.relativeStrength != null || mergedSector?.rotating != null) {
      hasRealSector = true;
      const sectorStrength = mergedSector.relativeStrength || 50;

      if (isBullishTrend) {
        // BULLISH: Sector outperforming (7pt)
        if (sectorStrength > 70) sectorScore += 7;
        else if (sectorStrength > 60) sectorScore += 4;
        else if (sectorStrength > 50) sectorScore += 2;
      } else {
        // BEARISH: Sector underperforming (7pt)
        if (sectorStrength < 30) sectorScore += 7;
        else if (sectorStrength < 40) sectorScore += 4;
        else if (sectorStrength < 50) sectorScore += 2;
      }

      // Rotation (3pt)
      const isRotating = mergedSector.rotating || false;
      if (isRotating) sectorScore += 3;
    }

    factors.sectorMomentum = { score: Math.min(sectorScore, 10), max: 10, label: 'Sector Momentum', color: '#FFA726', noData: !hasRealSector };

    // ── 6. RISK & PENALTY LAYER (10pts) ──
    // Starts at 10 — deductions for conditions indicating weaker/riskier setup.
    // Clean setups (good volume, aligned sector, trending ADX) lose nothing.
    let penaltyScore = 10;
    const penaltyReasons = [];

    // Breakout/breakdown without volume confirmation (most common false-signal source)
    if (factors.priceAction.score >= 7 && volRatio < 1.2) {
      penaltyScore -= 4;
      penaltyReasons.push('No volume on breakout');
    }
    // Sector RS working against direction
    const _sRS = mergedSector?.relativeStrength;
    if (_sRS != null) {
      if (isBullishTrend && _sRS < 40)  { penaltyScore -= 3; penaltyReasons.push('Weak sector RS'); }
      if (!isBullishTrend && _sRS > 60) { penaltyScore -= 3; penaltyReasons.push('Strong sector (contradicts bear)'); }
    }
    // Low ADX environment — drifting, not trending
    // Pre-breakout coil detection: low ADX + tight Bollinger = accumulation, NO penalty
    if (adx < 13) {
      penaltyScore -= 2;
      penaltyReasons.push(`ADX ${adx.toFixed(0)} < 13 (no trend)`);
    } else if (adx < 18) {
      if (bbWidth < 0.04) { /* tight coil — pre-breakout, no penalty */ }
      else { penaltyScore -= 1; penaltyReasons.push(`ADX ${adx.toFixed(0)} < 18 (weak trend)`); }
    }
    // MACD divergence from trend direction
    if (isBullishTrend && macd.histogram < -0.5)  { penaltyScore -= 1; penaltyReasons.push('MACD divergence'); }
    if (!isBullishTrend && macd.histogram > 0.5)  { penaltyScore -= 1; penaltyReasons.push('MACD divergence'); }

    penaltyScore = Math.max(0, penaltyScore);
    const penaltyLabel = penaltyReasons.length > 0 ? `Risk: ${penaltyReasons.join(', ')}` : 'Risk: Clean Setup';
    const penaltyColor = penaltyScore >= 8 ? '#4CAF50' : penaltyScore >= 5 ? '#FFA726' : '#EF5350';
    factors.riskPenalty = { score: penaltyScore, max: 10, label: penaltyLabel, color: penaltyColor };

    // ── Compute total score ──
    let total = Object.values(factors).reduce((sum, f) => sum + f.score, 0);

    // ── Dynamic normalization ──
    const availableMax = Object.values(factors).reduce((sum, f) => f.noData ? sum : sum + f.max, 0);
    total = availableMax > 0 && availableMax !== 100
      ? Math.round((total / availableMax) * 100)
      : Math.round(total);

    // ── MANDATORY FILTERS (softened — graduated penalty not hard veto) ──
    let putFilterVeto = false;
    let putFilterReasons = [];

    if (!isBullishTrend) {
      // 1. Strong uptrend trap risk: only hard-veto at extreme ADX (>30) with clear EMA stack
      if (adx > 25 && ema21Last > ema50Last) {
        if (adx > 30 && ema21Last > ema50Last * 1.02) {
          putFilterVeto = true;
          putFilterReasons.push(`Strong uptrend trap (ADX ${adx.toFixed(0)} + EMA21>>EMA50)`);
        } else {
          total = Math.max(0, total - 8); // graduated penalty, not hard veto
          putFilterReasons.push(`Uptrend context (-8pts, ADX ${adx.toFixed(0)})`);
        }
      }

      // 2. Avoid PUT if stock already >3.5% down intraday (late entry risk)
      if (changePercent < -3.5) {
        putFilterVeto = true;
        putFilterReasons.push(`Already ${changePercent.toFixed(1)}% down — late entry risk`);
      }

      // 3. Price action must show breakdown structure
      if (factors.priceAction.score < 5) {
        putFilterVeto = true;
        putFilterReasons.push('No breakdown + pullback + rejection pattern');
      }
    }

    // ── Gap Analysis Rule Engine Integration (Dynamic Weighting) ──
    if (globalThis.gapAnalysisEngine) {
      const gapRes = globalThis.gapAnalysisEngine.computeGapScore(data);

      if (gapRes.gapTier >= 1) {
        // Dynamic weighting based on gap tier and confirmation strength
        let w_gap = 0.4;
        let w_core = 0.6;

        // Tier 3+ with strong confirmation → increase gap weight
        if (gapRes.gapTier >= 3 && gapRes.confirmationStrong) {
          w_gap = 0.5;
          w_core = 0.5;
        }

        // Fade scenario detected → reduce gap weight to core
        if (gapRes.isFadeScenario) {
          w_gap = 0.3;
          w_core = 0.7;
        }

        total = (total * w_core) + (gapRes.score * w_gap);

        factors.gapOverlay = {
          score: Math.min(Math.max(gapRes.score, -100), 100),
          max: 100,
          label: gapRes.override ? `Gap [${gapRes.override}]` : `Gap Tier ${gapRes.gapTier} (${gapRes.gapType})`,
          color: gapRes.override ? '#D32F2F' : '#FF9800',
          noData: false
        };
      }
    }

    // ── Determine direction with thresholds & strict multi-timeframe indicator alignment ──
    let direction = 'NEUTRAL';

    // Multi-Timeframe (15m & 1h) indicator check
    let isMtfAlignedBullish = true;
    let isMtfAlignedBearish = true;

    if (data.mtfSignals) {
      const sig15m = data.mtfSignals['15minute'];
      const sig1h  = data.mtfSignals['60minute'];

      if (sig15m && sig1h) {
        const m15Bull = (sig15m.overall === 'BULLISH' && sig15m.macd === 'BULLISH' && sig15m.rsi !== 'BEARISH');
        const m1hBull = (sig1h.overall === 'BULLISH' && sig1h.macd === 'BULLISH' && sig1h.rsi !== 'BEARISH');
        isMtfAlignedBullish = m15Bull && m1hBull;

        const m15Bear = (sig15m.overall === 'BEARISH' && sig15m.macd === 'BEARISH' && sig15m.rsi !== 'BULLISH');
        const m1hBear = (sig1h.overall === 'BEARISH' && sig1h.macd === 'BEARISH' && sig1h.rsi !== 'BULLISH');
        isMtfAlignedBearish = m15Bear && m1hBear;
      }
    }

    if (!isBullishTrend && putFilterVeto) {
      // PUT veto triggered
      direction = 'NO TRADE';
      total = Math.min(total, 39);
      factors.putFilter = {
        score: 0,
        max: 100,
        label: `⚠️ PUT Filter Triggered: ${putFilterReasons.join(' | ')}`,
        color: '#D32F2F',
        noData: false
      };
    } else {
      // Normal direction assignment
      if (isBullishTrend && total >= 55) {
        direction = isMtfAlignedBullish ? 'BULLISH' : 'NEUTRAL';
      } else if (!isBullishTrend && total >= 55) {
        direction = isMtfAlignedBearish ? 'BEARISH' : 'NEUTRAL';
      } else {
        direction = 'NEUTRAL';
      }
    }

    // Controlled override from gap engine: only force direction if confirmation is strong
    if (globalThis.gapAnalysisEngine) {
      const gapRes = globalThis.gapAnalysisEngine.computeGapScore(data);
      if (gapRes.gapTier >= 1 && gapRes.override && gapRes.confirmationStrong) {
        direction = gapRes.override;
      }
    }

    // ── Risk Management: SL / Target / R:R ──
    const entry = data.ltp || lastClose;
    let stopLoss, target1, target2;
    if (isBullishTrend || direction === 'BULLISH') {
      stopLoss = +(entry - 1.5 * atr).toFixed(2);
      target1  = +(entry + 2 * atr).toFixed(2);
      target2  = +(entry + 3 * atr).toFixed(2);
    } else {
      stopLoss = +(entry + 1.5 * atr).toFixed(2);
      target1  = +(entry - 2 * atr).toFixed(2);
      target2  = +(entry - 3 * atr).toFixed(2);
    }
    const riskPerShare = Math.abs(entry - stopLoss);
    const rewardPerShare = Math.abs(target1 - entry);
    const riskReward = riskPerShare > 0 ? +(rewardPerShare / riskPerShare).toFixed(2) : 0;

    return {
      total, factors, direction, rsi, macd, adx, atr, volRatio, changePercent,
      risk: { entry, stopLoss, target1, target2, riskReward, riskPerShare, atrUsed: +atr.toFixed(2) },
      putFilterVeto, putFilterReasons, isBullishTrend
    };
  }

  // ── OPTIONS SCORING (6-Factor Refined Model for CALL/PUT with Global Risk Filters) ──
  // Model: Momentum+Trend (25pt) | Volume+OrderFlow (20pt) | Derivatives (20pt) | 
  //        Options Structure (15pt) | Market Context (15pt) | Catalyst Bonus (5pt) = 100pt
  // 
  // Each factor has CALL-specific and PUT-specific rules. Direction (CALL/PUT) is determined
  // by structural trend (EMA21 > EMA50 = CALL, else PUT). Scoring applies the relevant rules
  // for the detected direction.
  //
  // SIGNAL THRESHOLDS:
  //   ≥75: CALL/PUT with strong signal strength
  //   60–74: CALL/PUT
  //   40–59: NO TRADE
  //   ≤40: Opposite bias
  //
  // GLOBAL RISK FILTERS (OVERRIDE scoring, veto signals):
  //   • Price deviation from VWAP > ±3%
  //   • IV Percentile > 70%
  //   • Circuit proximity (price within 2% of upper/lower circuit)
  //   • ADX < 20 (insufficient trend strength)
  //   • Time after 2:45 PM (late-day volatility risk)
  //   • Bid-ask spread > 1% (illiquidity risk)
  scoreOptions(data) {
    const { closes, highs, lows, volumes, optionsData = {}, fundamentals = {}, snapshot = {} } = data;
    const factors = {};

    const lastClose = closes[closes.length - 1];
    const prevClose = closes[closes.length - 2] || lastClose;
    const rsi = this.computeRSI(closes);
    const macd = this.computeMACD(closes);
    const adx = this.computeADX(highs, lows, closes);
    const atr = this.computeATR(highs, lows, closes);
    const volRatio = this.computeVolumeRatio(volumes);
    const ema9 = this.computeEMA(closes, 9);
    const ema21 = this.computeEMA(closes, 21);
    // Note: EMA50 removed from options — EMA9/21 crossover is sufficient for short-term directional bias

    // ── Extract live snapshot data ──
    const liveChangePct = snapshot.change_pct || ((lastClose - prevClose) / prevClose * 100);
    const liveBuyQty = snapshot.buy_qty || 0;
    const liveSellQty = snapshot.sell_qty || 0;
    const liveVolume = snapshot.volume || volumes[volumes.length - 1] || 0;
    const vwap = snapshot.avg_price || lastClose;  // VWAP for intraday comparison
    const liveLTP = snapshot.ltp || lastClose;
    const circuit = snapshot.circuit || {};
    const depth = snapshot.depth || {};
    const futures = snapshot.futures || {};
    const atmOpt = snapshot.atm_option || {};

    // ── Compute EMAs ──
    const ema9Last = ema9[ema9.length - 1] || lastClose;
    const ema21Last = ema21[ema21.length - 1] || lastClose;

    // ── Determine CALL/PUT bias (short-term trend) ──
    // EMA9 > EMA21 = near-term uptrend = CALL bias (faster & more relevant for options than EMA50)
    const isCallBias = ema9Last > ema21Last;

    // ── GLOBAL RISK FILTERS (Veto signals if ANY are triggered) ──
    let riskFilterVeto = false;
    let riskFilterReasons = [];

    // 1. Price deviation from VWAP > ±3% → NO TRADE
    const priceVwapPctDiff = vwap > 0 ? ((liveLTP - vwap) / vwap) * 100 : 0;
    if (Math.abs(priceVwapPctDiff) > 3) {
      riskFilterVeto = true;
      riskFilterReasons.push(`Price deviation from VWAP ${priceVwapPctDiff.toFixed(1)}% > ±3%`);
    }

    // 2. IV Percentile > 70% → avoid option buying
    const iv = atmOpt.avg_iv || optionsData.ivPercentile || 0;
    if (iv > 70) {
      riskFilterVeto = true;
      riskFilterReasons.push(`IV ${iv.toFixed(1)}% > 70%`);
    }

    // 3. Circuit proximity (price within 2% of upper/lower circuit) → NO TRADE
    if (circuit.upper && circuit.lower && lastClose > 0) {
      const upperDist = ((circuit.upper - lastClose) / lastClose) * 100;
      const lowerDist = ((lastClose - circuit.lower) / lastClose) * 100;
      if (upperDist < 2 || lowerDist < 2) {
        riskFilterVeto = true;
        riskFilterReasons.push(`Circuit proximity: upper ${upperDist.toFixed(1)}%, lower ${lowerDist.toFixed(1)}%`);
      }
    }

    // 4. ADX < 15 → very low trend strength → score reduction (not veto)
    // Daily ADX 15–20 is common during consolidation before breakout; hard veto misses these
    // Only veto at extreme low ADX < 15 where there is truly no trend
    if (adx < 15) {
      riskFilterVeto = true;
      riskFilterReasons.push(`ADX ${adx.toFixed(1)} < 15 (no trend)`);
    }

    // Note: Time-of-day veto removed — scoring uses 90-day daily candles,
    // not intraday data, so scores should be consistent at any time of day.

    // 6. Bid-ask spread > 1% → illiquidity risk → NO TRADE
    if (depth.bid && depth.ask && depth.bid > 0) {
      const bidAskSpread = ((depth.ask - depth.bid) / depth.bid) * 100;
      if (bidAskSpread > 1) {
        riskFilterVeto = true;
        riskFilterReasons.push(`Bid-ask spread ${bidAskSpread.toFixed(2)}% > 1%`);
      }
    }

    // ── 1. MOMENTUM + TREND (25pts) ──
    // CALL Rules: Price > VWAP (8pt), Price within +1.5% VWAP (5pt), EMA9 > EMA21 & Price > EMA9 (6pt), MACD+ (3pt), RSI 45–65 (3pt)
    // PUT Rules:  Price < VWAP (8pt), Price within -1.5% VWAP (5pt), EMA9 < EMA21 & Price < EMA9 (6pt), MACD- (3pt), RSI 35–55 (3pt)
    let momentumTrendScore = 0;

    if (isCallBias) {
      // CALL Rules
      // Price > VWAP (8pt)
      if (liveLTP > vwap) momentumTrendScore += 8;
      else if (liveLTP > (vwap * 0.99)) momentumTrendScore += 4;
      else momentumTrendScore += 0;

      // Price within +1.5% of VWAP (5pt)
      const priceAboveVwapPct = vwap > 0 ? ((liveLTP - vwap) / vwap) * 100 : 0;
      if (priceAboveVwapPct > 0 && priceAboveVwapPct <= 1.5) momentumTrendScore += 5;
      else if (priceAboveVwapPct > 0 && priceAboveVwapPct <= 2.5) momentumTrendScore += 3;
      else if (priceAboveVwapPct > 0) momentumTrendScore += 1;

      // EMA9 > EMA21 & Price > EMA9 (6pt)
      if (ema9Last > ema21Last && liveLTP > ema9Last) momentumTrendScore += 6;
      else if (ema9Last > ema21Last && liveLTP > ema21Last) momentumTrendScore += 4;
      else if (ema9Last > ema21Last) momentumTrendScore += 2;

      // MACD+ (3pt)
      if (macd.histogram > 0) momentumTrendScore += 3;
      else if (Math.abs(macd.histogram) < 0.00001) momentumTrendScore += 1;

      // RSI 45–65 (3pt)
      if (rsi >= 45 && rsi <= 65) momentumTrendScore += 3;
      else if (rsi >= 40 && rsi <= 70) momentumTrendScore += 2;
      else if (rsi >= 30 && rsi <= 75) momentumTrendScore += 1;
    } else {
      // PUT Rules
      // Price < VWAP (8pt)
      if (liveLTP < vwap) momentumTrendScore += 8;
      else if (liveLTP < (vwap * 1.01)) momentumTrendScore += 4;
      else momentumTrendScore += 0;

      // Price within -1.5% of VWAP (5pt)
      const priceBelowVwapPct = vwap > 0 ? ((vwap - liveLTP) / vwap) * 100 : 0;
      if (priceBelowVwapPct > 0 && priceBelowVwapPct <= 1.5) momentumTrendScore += 5;
      else if (priceBelowVwapPct > 0 && priceBelowVwapPct <= 2.5) momentumTrendScore += 3;
      else if (priceBelowVwapPct > 0) momentumTrendScore += 1;

      // EMA9 < EMA21 & Price < EMA9 (6pt)
      if (ema9Last < ema21Last && liveLTP < ema9Last) momentumTrendScore += 6;
      else if (ema9Last < ema21Last && liveLTP < ema21Last) momentumTrendScore += 4;
      else if (ema9Last < ema21Last) momentumTrendScore += 2;

      // MACD- (3pt)
      if (macd.histogram < 0) momentumTrendScore += 3;
      else if (Math.abs(macd.histogram) < 0.00001) momentumTrendScore += 1;

      // RSI 35–55 (3pt)
      if (rsi >= 35 && rsi <= 55) momentumTrendScore += 3;
      else if (rsi >= 30 && rsi <= 60) momentumTrendScore += 2;
      else if (rsi >= 25 && rsi <= 70) momentumTrendScore += 1;
    }

    factors.momentumTrend = { score: Math.min(momentumTrendScore, 20), max: 20, label: 'Momentum + Trend', color: '#FF5722' };

    // ── 2. VOLUME & ORDER FLOW (20pts) ──
    // CALL: Volume ≥2.5x avg (8pt), sustained volume 2 candles (5pt), Buy ratio >60% (4pt), block deal detect (3pt)
    // PUT:  Volume ≥2.5x avg (8pt), sustained volume 2 candles (5pt), Sell ratio >60% (4pt), block deal detect (3pt)
    let volumeOrderFlowScore = 0;

    // Volume ≥2.5x avg (8pt) — applies to both CALL and PUT
    if (volRatio >= 2.5) volumeOrderFlowScore += 8;
    else if (volRatio >= 2.0) volumeOrderFlowScore += 5;
    else if (volRatio >= 1.5) volumeOrderFlowScore += 3;
    else if (volRatio >= 1.0) volumeOrderFlowScore += 1;

    // Sustained volume 2 candles (5pt) — volume consistency
    if (volumes.length >= 3) {
      const avgPrev2 = volumes.slice(-3, -1).reduce((a, b) => a + b, 0) / 2;
      const currentVol = volumes[volumes.length - 1];
      if (currentVol > avgPrev2 * 1.5) volumeOrderFlowScore += 5;
      else if (currentVol > avgPrev2 * 1.2) volumeOrderFlowScore += 3;
      else if (currentVol > avgPrev2) volumeOrderFlowScore += 1;
    }

    // Buy ratio / Sell ratio based on direction (4pt)
    const totalQty = liveBuyQty + liveSellQty;
    if (totalQty > 0) {
      const buyRatio = liveBuyQty / totalQty;
      if (isCallBias) {
        // CALL: Buy ratio >60% (4pt)
        if (buyRatio > 0.60) volumeOrderFlowScore += 4;
        else if (buyRatio > 0.55) volumeOrderFlowScore += 2;
        else if (buyRatio > 0.50) volumeOrderFlowScore += 1;
      } else {
        // PUT: Sell ratio >60% (4pt) → buyRatio < 0.40
        if (buyRatio < 0.40) volumeOrderFlowScore += 4;
        else if (buyRatio < 0.45) volumeOrderFlowScore += 2;
        else if (buyRatio < 0.50) volumeOrderFlowScore += 1;
      }
    }

    // Block deal detection (3pt) — applies to both
    const maxBidQty = depth.max_bid_qty || 0;
    const maxAskQty = depth.max_ask_qty || 0;
    const avgVolPerCandle = volumes.length > 5 ? volumes.slice(-5).reduce((a, b) => a + b, 0) / 5 : liveVolume;
    const blockThreshold = avgVolPerCandle * 0.01;
    if (maxBidQty > blockThreshold || maxAskQty > blockThreshold) {
      volumeOrderFlowScore += 3;
    } else if (maxBidQty > blockThreshold * 0.5 || maxAskQty > blockThreshold * 0.5) {
      volumeOrderFlowScore += 1;
    }

    factors.volumeOrderFlow = { score: Math.min(volumeOrderFlowScore, 20), max: 20, label: 'Volume & Order Flow', color: '#9C27B0' };

    // ── 3. DERIVATIVES (OI + Futures) (20pts) ──
    // CALL: OI ↑ + Price ↑ (8pt), OI buildup BEFORE breakout (5pt), Futures premium >0.3% (4pt), stable OI trend (3pt)
    // PUT:  OI ↑ + Price ↓ (8pt), OI buildup BEFORE breakdown (5pt), Futures discount <-0.3% (4pt), stable OI trend (3pt)
    let derivativesScore = 0;
    const oiChange = optionsData.oiChangePercent || 0;
    const oiBuildUp = optionsData.buildUp || 'none';
    const futPremium = futures.premium_pct || 0;

    if (isCallBias) {
      // CALL Rules
      // OI ↑ + Price ↑ (8pt)
      if (oiChange > 5 && liveChangePct > 0.3) derivativesScore += 8;
      else if (oiChange > 2 && liveChangePct > 0) derivativesScore += 5;
      else if (oiChange > 0 && liveChangePct > 0) derivativesScore += 2;

      // OI buildup BEFORE breakout (5pt)
      if (oiBuildUp === 'long_buildup') derivativesScore += 5;
      else if (oiBuildUp === 'short_covering') derivativesScore += 2;

      // Futures premium >0.3% (4pt)
      if (futPremium > 0.3) derivativesScore += 4;
      else if (futPremium > 0.1) derivativesScore += 2;
      else if (futPremium > 0) derivativesScore += 1;

      // Stable OI trend (3pt) — low volatility in OI
      if (Math.abs(oiChange) < 2) derivativesScore += 3;
      else if (Math.abs(oiChange) < 5) derivativesScore += 1;
    } else {
      // PUT Rules
      // OI ↑ + Price ↓ (8pt)
      if (oiChange > 5 && liveChangePct < -0.3) derivativesScore += 8;
      else if (oiChange > 2 && liveChangePct < 0) derivativesScore += 5;
      else if (oiChange > 0 && liveChangePct < 0) derivativesScore += 2;

      // OI buildup BEFORE breakdown (5pt)
      if (oiBuildUp === 'short_buildup') derivativesScore += 5;
      else if (oiBuildUp === 'long_unwinding') derivativesScore += 2;

      // Futures discount <-0.3% (4pt)
      if (futPremium < -0.3) derivativesScore += 4;
      else if (futPremium < -0.1) derivativesScore += 2;
      else if (futPremium < 0) derivativesScore += 1;

      // Stable OI trend (3pt)
      if (Math.abs(oiChange) < 2) derivativesScore += 3;
      else if (Math.abs(oiChange) < 5) derivativesScore += 1;
    }

    factors.derivatives = { score: Math.min(derivativesScore, 25), max: 25, label: 'Derivatives (OI + Futures)', color: '#EF5350' };

    // ── 4. OPTIONS STRUCTURE (15pts) ──
    // CALL: IV Percentile <40 (6pt), PCR rising with price (5pt), PE IV > CE IV by >3 (4pt)
    // PUT:  IV Percentile <40 (6pt), PCR falling with price (5pt), CE IV > PE IV by >3 (4pt)
    let optionsStructScore = 0;
    const pcr = atmOpt.pcr || optionsData.pcr || 0;
    const ceIV = atmOpt.ce_iv || 0;
    const peIV = atmOpt.pe_iv || 0;

    // IV Percentile <40 (6pt) — applies to both
    if (iv > 0 && iv < 40) optionsStructScore += 6;
    else if (iv > 0 && iv < 50) optionsStructScore += 3;

    if (isCallBias) {
      // CALL: PCR rising with price (5pt)
      if (pcr > 1.3) optionsStructScore += 5;
      else if (pcr > 1.1) optionsStructScore += 3;
      else if (pcr > 0.9) optionsStructScore += 1;

      // PE IV > CE IV by >3 (4pt)
      if (peIV > 0 && ceIV > 0) {
        const skew = peIV - ceIV;
        if (skew > 3) optionsStructScore += 4;
        else if (skew > 1) optionsStructScore += 2;
      }
    } else {
      // PUT: PCR falling with price (5pt)
      if (pcr < 0.7) optionsStructScore += 5;
      else if (pcr < 0.9) optionsStructScore += 3;
      else if (pcr < 1.1) optionsStructScore += 1;

      // CE IV > PE IV by >3 (4pt)
      if (ceIV > 0 && peIV > 0) {
        const skew = ceIV - peIV;
        if (skew > 3) optionsStructScore += 4;
        else if (skew > 1) optionsStructScore += 2;
      }
    }

    factors.optionsStructure = { score: Math.min(optionsStructScore, 15), max: 15, label: 'Options Structure', color: '#AB47BC' };

    // ── 5. MARKET CONTEXT (15pts) ──
    // CALL: Index > VWAP (6pt), Index ADX >20 (5pt), sector aligned (4pt)
    // PUT:  Index < VWAP (6pt), Index ADX >20 (5pt), sector aligned (4pt)
    let marketContextScore = 0;

    if (isCallBias) {
      // CALL: Index > VWAP (6pt) — use trend as proxy for index strength
      marketContextScore += 3;  // Bullish trend implies index strength
    } else {
      // PUT: Index < VWAP (6pt) — use trend as proxy for index weakness
      marketContextScore += 3;  // Bearish trend implies index weakness
    }

    // Index ADX >20 (5pt) — applies to both (use stock ADX as proxy)
    if (adx > 20) marketContextScore += 5;
    else if (adx > 15) marketContextScore += 2;

    // Sector proxy via volume confirmation (4pt) — volume ratio as conviction indicator
    // Delivery % is not available in real-time and irrelevant for short-term options
    if (volRatio > 2.0) marketContextScore += 4;
    else if (volRatio > 1.5) marketContextScore += 2;
    else if (volRatio > 1.0) marketContextScore += 1;

    factors.marketContext = { score: Math.min(marketContextScore, 10), max: 10, label: 'Market Context', color: '#26A69A' };

    // ── 6. CATALYST BONUS (5pts) ──
    // CALL: ADX >25 (2pt), delivery >60% (1pt), extreme PCR >1.5 (2pt)
    // PUT:  ADX >25 (2pt), delivery >60% (1pt), extreme PCR <0.5 (2pt)
    let catalystScore = 0;

    // ADX >25 (2pt) — applies to both
    if (adx > 25) catalystScore += 2;
    else if (adx > 20) catalystScore += 1;

    // Delivery (1pt) — replaced with volume surge as more reliable real-time indicator
    if (volRatio > 2.5) catalystScore += 1;

    // Extreme PCR (2pt) — direction-dependent
    if (isCallBias) {
      // CALL: extreme PCR >1.5 (2pt)
      if (pcr > 1.5) catalystScore += 2;
      else if (pcr > 1.3) catalystScore += 1;
    } else {
      // PUT: extreme PCR <0.5 (2pt)
      if (pcr < 0.5) catalystScore += 2;
      else if (pcr < 0.7) catalystScore += 1;
    }

    factors.catalyst = { score: Math.min(catalystScore, 5), max: 5, label: 'Catalyst Bonus', color: '#1E88E5' };

    // ── RISK & PENALTY LAYER (10pts) ──
    // Starts at 10 — deductions for options-specific risk conditions.
    let optPenalty = 10;
    const optPenaltyReasons = [];

    // High IV makes option buying expensive — reduce conviction
    // Uses iv already declared in global risk filters above
    if (iv > 50 && iv <= 70) { optPenalty -= 3; optPenaltyReasons.push(`IV ${iv.toFixed(0)}% high`); }

    // OI contradiction: CALL bias but OI rising + price falling (smart money is short)
    // Uses oiChange and liveChangePct already declared in Derivatives factor above
    if (isCallBias && oiChange > 3 && liveChangePct < -0.5) {
      optPenalty -= 4; optPenaltyReasons.push('OI up + price down (smart money bearish)');
    }
    if (!isCallBias && oiChange > 3 && liveChangePct > 0.5) {
      optPenalty -= 4; optPenaltyReasons.push('OI up + price up (short covering, not trend)');
    }

    // Falling OI + rising price = weak bull (short covering only, not real demand)
    if (isCallBias && oiChange < -3 && liveChangePct > 0.5) {
      optPenalty -= 2; optPenaltyReasons.push('Falling OI + rising price (weak signal)');
    }

    optPenalty = Math.max(0, optPenalty);
    const optPenaltyLabel = optPenaltyReasons.length > 0 ? `Risk: ${optPenaltyReasons.join(', ')}` : 'Risk: Clean Setup';
    const optPenaltyColor = optPenalty >= 8 ? '#4CAF50' : optPenalty >= 5 ? '#FFA726' : '#EF5350';
    factors.riskPenalty = { score: optPenalty, max: 10, label: optPenaltyLabel, color: optPenaltyColor };

    // ── Compute total score ──
    let total = Object.values(factors).reduce((sum, f) => sum + f.score, 0);

    // ── Dynamic normalization ──
    const availableMax = Object.values(factors).reduce((sum, f) => f.noData ? sum : sum + f.max, 0);
    total = availableMax > 0 && availableMax !== 100
      ? Math.round((total / availableMax) * 100)
      : Math.round(total);

    // ── APPLY GLOBAL RISK FILTERS (override/veto signals) ──
    if (riskFilterVeto) {
      factors.riskFilter = {
        score: 0,
        max: 100,
        label: `⚠️ Risk Filter Triggered: ${riskFilterReasons.join(', ')}`,
        color: '#D32F2F',
        noData: false
      };
    }

    // ── Gap Analysis Rule Engine Integration (Dynamic Weighting) ──
    if (globalThis.gapAnalysisEngine) {
      const gapRes = globalThis.gapAnalysisEngine.computeGapScore(data);

      if (gapRes.gapTier >= 1) {
        // Dynamic weighting based on gap tier and confirmation strength
        let w_gap = 0.4;
        let w_core = 0.6;

        // Tier 3+ with strong confirmation → increase gap weight
        if (gapRes.gapTier >= 3 && gapRes.confirmationStrong) {
          w_gap = 0.5;
          w_core = 0.5;
        }

        // Fade scenario detected → reduce gap weight to core
        if (gapRes.isFadeScenario) {
          w_gap = 0.3;
          w_core = 0.7;
        }

        total = (total * w_core) + (gapRes.score * w_gap);

        factors.gapOverlay = { 
          score: Math.min(Math.max(gapRes.score, -100), 100), 
          max: 100, 
          label: gapRes.override ? `Gap [${gapRes.override}]` : `Gap Tier ${gapRes.gapTier} (${gapRes.gapType})`, 
          color: gapRes.override ? '#D32F2F' : '#FF9800',
          noData: false
        };
      }
    }

    // ── Determine explicit CALL/PUT direction with 4-tier thresholds & strict multi-timeframe alignment ──
    let direction = 'NEUTRAL';
    let signalStrength = 'NEUTRAL';

    // Multi-Timeframe (15m & 1h) indicator check for Options
    let isMtfAlignedBullish = true;
    let isMtfAlignedBearish = true;

    if (data.mtfSignals) {
      const sig15m = data.mtfSignals['15minute'];
      const sig1h  = data.mtfSignals['60minute'];

      if (sig15m && sig1h) {
        const m15Bull = (sig15m.overall === 'BULLISH' && sig15m.macd === 'BULLISH' && sig15m.rsi !== 'BEARISH');
        const m1hBull = (sig1h.overall === 'BULLISH' && sig1h.macd === 'BULLISH' && sig1h.rsi !== 'BEARISH');
        isMtfAlignedBullish = m15Bull && m1hBull;

        const m15Bear = (sig15m.overall === 'BEARISH' && sig15m.macd === 'BEARISH' && sig15m.rsi !== 'BULLISH');
        const m1hBear = (sig1h.overall === 'BEARISH' && sig1h.macd === 'BEARISH' && sig1h.rsi !== 'BULLISH');
        isMtfAlignedBearish = m15Bear && m1hBear;
      }
    }

    if (riskFilterVeto) {
      direction = 'NO TRADE';
      signalStrength = 'VETO';
      total = Math.min(total, 39);  // Force below NO TRADE threshold
    } else {
      if (total >= 75) {
        const targetDir = isCallBias ? 'CALL' : 'PUT';
        if (targetDir === 'CALL' && isMtfAlignedBullish) {
          direction = 'CALL';
          signalStrength = 'STRONG';
        } else if (targetDir === 'PUT' && isMtfAlignedBearish) {
          direction = 'PUT';
          signalStrength = 'STRONG';
        } else {
          direction = 'NO TRADE';
          signalStrength = 'WEAK';
        }
      } else if (total >= 60) {
        const targetDir = isCallBias ? 'CALL' : 'PUT';
        if (targetDir === 'CALL' && isMtfAlignedBullish) {
          direction = 'CALL';
          signalStrength = 'NORMAL';
        } else if (targetDir === 'PUT' && isMtfAlignedBearish) {
          direction = 'PUT';
          signalStrength = 'NORMAL';
        } else {
          direction = 'NO TRADE';
          signalStrength = 'WEAK';
        }
      } else {
        direction = 'NO TRADE';
        signalStrength = 'WEAK';
      }
    }

    // Controlled override from gap engine: only force direction if confirmation is strong and no veto
    if (globalThis.gapAnalysisEngine) {
      const gapRes = globalThis.gapAnalysisEngine.computeGapScore(data);
      if (gapRes.gapTier >= 1 && gapRes.override && gapRes.confirmationStrong && !riskFilterVeto) {
        direction = gapRes.override;
      }
    }

    // ── Risk Management: SL / Target / R:R ──
    const entry = data.ltp || lastClose;
    let stopLoss, target1, target2;
    if (isCallBias || direction.includes('CALL')) {
      stopLoss = +(entry - 1.5 * atr).toFixed(2);
      target1  = +(entry + 2 * atr).toFixed(2);
      target2  = +(entry + 3 * atr).toFixed(2);
    } else {
      stopLoss = +(entry + 1.5 * atr).toFixed(2);
      target1  = +(entry - 2 * atr).toFixed(2);
      target2  = +(entry - 3 * atr).toFixed(2);
    }
    const riskPerShare = Math.abs(entry - stopLoss);
    const rewardPerShare = Math.abs(target1 - entry);
    const riskReward = riskPerShare > 0 ? +(rewardPerShare / riskPerShare).toFixed(2) : 0;

    return {
      total, factors, direction, signalStrength, rsi, macd, adx, atr, volRatio,
      iv, pcr, futPremium,
      oiBuildUp, blockDeal: false,
      liveChangePct, liveVolRatio: volRatio,
      risk: { entry, stopLoss, target1, target2, riskReward, riskPerShare, atrUsed: +atr.toFixed(2) },
      riskFilterVeto, riskFilterReasons,
      isCallBias  // Expose bias for debugging
    };
  }

  // ── Generate scores for a list of stocks with OHLCV ──
  scoreBatch(stockDataList, mode = 'equity') {
    const results = stockDataList.map(stock => {
      const result = mode === 'equity' ? this.scoreEquity(stock) : this.scoreOptions(stock);
      return {
        symbol: stock.symbol,
        sector: stock.sector || '—',
        ltp: stock.closes[stock.closes.length - 1],
        ...result
      };
    });

    if (mode === 'equity') this.computeSectorScores(results);

    return results.sort((a, b) => b.total - a.total);
  }

  // ── Compute sector relative strength from a list of scored stocks ──
  // Called after pass-1 scoring so sector data is available for pass-2.
  // Groups stocks by sector, averages scores, stores in _sectorScores.
  computeSectorScores(scoredStocks) {
    const sectorGroups = {};
    for (const r of scoredStocks) {
      const s = r.sector;
      if (!s || s === '—') continue;
      if (!sectorGroups[s]) sectorGroups[s] = [];
      sectorGroups[s].push(r.total ?? r.score ?? 0);
    }
    const allScores = scoredStocks.map(r => r.total ?? r.score ?? 0);
    const overallAvg = allScores.length > 0
      ? allScores.reduce((a, b) => a + b, 0) / allScores.length
      : 50;
    for (const [sector, scores] of Object.entries(sectorGroups)) {
      const avg = scores.reduce((a, b) => a + b, 0) / scores.length;
      const rs = Math.min(100, Math.max(0, 50 + (avg - overallAvg) * 2));
      this._sectorScores[sector] = { relativeStrength: rs, rotating: avg > overallAvg + 5 };
    }
  }

  // ── INTRADAY SCORING (15-min candles) ──
  // Factors: VWAP Position (30pt) | Opening Range Breakout (25pt) |
  //          Volume Spike (20pt) | 15-min EMA Alignment (15pt) | RSI (10pt) = 100pt
  // Direction: EMA9 > EMA21 on 15-min = BUY bias, else SELL bias
  scoreIntraday(data) {
    const { closes, highs, lows, volumes, ltp } = data;
    if (!closes || closes.length < 5) return { total: 0, direction: 'NEUTRAL' };

    const lastClose = ltp || closes[closes.length - 1];

    // Compute VWAP from all 15-min candles today
    let cumTPV = 0, cumVol = 0;
    for (let i = 0; i < closes.length; i++) {
      const tp = (highs[i] + lows[i] + closes[i]) / 3;
      cumTPV += tp * volumes[i];
      cumVol += volumes[i];
    }
    const vwap = cumVol > 0 ? cumTPV / cumVol : lastClose;
    const vwapPct = ((lastClose - vwap) / vwap) * 100;

    // Opening Range: first 15-min candle defines the day's initial range
    const orbHigh = highs[0];
    const orbLow = lows[0];

    // EMA9 / EMA21 on 15-min closes (short-term intraday trend)
    const ema9 = this.computeEMA(closes, Math.min(9, closes.length));
    const ema21 = this.computeEMA(closes, Math.min(21, closes.length));
    const ema9Last  = ema9[ema9.length - 1]   || lastClose;
    const ema21Last = ema21[ema21.length - 1]  || lastClose;
    const isBuyBias = ema9Last > ema21Last;

    // RSI on 15-min closes
    const rsi = this.computeRSI(closes);

    // Volume spike vs recent 5-candle average
    const avgVol5 = volumes.slice(-5).reduce((a, b) => a + b, 0) / Math.min(5, volumes.length);
    const lastVol  = volumes[volumes.length - 1] || 0;
    const volRatio = avgVol5 > 0 ? lastVol / avgVol5 : 1;

    // ── FACTOR 1: VWAP Position (30pts) ──
    let vwapScore = 0;
    if (isBuyBias) {
      if (lastClose > vwap && vwapPct < 1.5)  vwapScore = 30; // Just above VWAP = ideal entry
      else if (lastClose > vwap && vwapPct < 2.5) vwapScore = 20;
      else if (lastClose > vwap)               vwapScore = 10;
      else if (vwapPct > -0.5)                 vwapScore = 8;  // Just under VWAP = risky
    } else {
      if (lastClose < vwap && vwapPct > -1.5)  vwapScore = 30;
      else if (lastClose < vwap && vwapPct > -2.5) vwapScore = 20;
      else if (lastClose < vwap)               vwapScore = 10;
      else if (vwapPct < 0.5)                  vwapScore = 8;
    }

    // ── FACTOR 2: Opening Range Breakout (25pts) ──
    let orbScore = 0;
    if (isBuyBias) {
      if (lastClose > orbHigh)                        orbScore = 25; // Above ORB = confirmed breakout
      else if (lastClose > (orbHigh + orbLow) / 2)   orbScore = 12;
      else if (lastClose > orbLow)                    orbScore = 5;
    } else {
      if (lastClose < orbLow)                         orbScore = 25;
      else if (lastClose < (orbHigh + orbLow) / 2)   orbScore = 12;
      else if (lastClose < orbHigh)                   orbScore = 5;
    }

    // ── FACTOR 3: Volume Spike (20pts) ──
    let volScore = 0;
    if      (volRatio >= 2.5) volScore = 20;
    else if (volRatio >= 2.0) volScore = 15;
    else if (volRatio >= 1.5) volScore = 10;
    else if (volRatio >= 1.2) volScore = 5;
    else if (volRatio >= 1.0) volScore = 2;

    // ── FACTOR 4: 15-min EMA Alignment (15pts) ──
    let emaScore = 0;
    if (isBuyBias) {
      if (ema9Last > ema21Last && lastClose > ema9Last) emaScore = 15; // Price above both = bullish
      else if (ema9Last > ema21Last)                    emaScore = 8;
    } else {
      if (ema9Last < ema21Last && lastClose < ema9Last) emaScore = 15;
      else if (ema9Last < ema21Last)                    emaScore = 8;
    }

    // ── FACTOR 5: RSI Intraday (10pts) ──
    let rsiScore = 0;
    if (isBuyBias) {
      if (rsi >= 50 && rsi <= 65)       rsiScore = 10;
      else if (rsi >= 45 && rsi <= 70)  rsiScore = 6;
      else if (rsi >= 40)               rsiScore = 2;
    } else {
      if (rsi >= 35 && rsi <= 50)       rsiScore = 10;
      else if (rsi >= 30 && rsi <= 55)  rsiScore = 6;
      else if (rsi <= 60)               rsiScore = 2;
    }

    const total = vwapScore + orbScore + volScore + emaScore + rsiScore;

    let direction = 'NEUTRAL';
    if (total >= 60 && isBuyBias)  direction = 'BUY';
    if (total >= 60 && !isBuyBias) direction = 'SELL';

    // ATR proxy from 15-min candle ranges for SL/Target
    const avgRange = highs.reduce((a, h, i) => a + (h - lows[i]), 0) / highs.length;
    const atr15m = avgRange * 2;
    const entry = lastClose;
    const stopLoss = isBuyBias ? +(entry - atr15m).toFixed(2)        : +(entry + atr15m).toFixed(2);
    const target1  = isBuyBias ? +(entry + atr15m * 1.5).toFixed(2)  : +(entry - atr15m * 1.5).toFixed(2);
    const target2  = isBuyBias ? +(entry + atr15m * 2.5).toFixed(2)  : +(entry - atr15m * 2.5).toFixed(2);

    return {
      total, direction, isBuyBias,
      vwap: +vwap.toFixed(2), vwapPct: +vwapPct.toFixed(2),
      orbHigh: +orbHigh.toFixed(2), orbLow: +orbLow.toFixed(2),
      volRatio: +volRatio.toFixed(2), rsi: +rsi.toFixed(1),
      ema9: +ema9Last.toFixed(2), ema21: +ema21Last.toFixed(2),
      risk: { entry, stopLoss, target1, target2, atrUsed: +atr15m.toFixed(2) }
    };
  }

  // ── Select optimal strike ──
  selectOptimalStrike(strikeData, underlyingPrice, atr, direction = 'CALL') {
    const targetMove = atr * 1.5;
    // Target price calculation (for future reference, not currently used in signal logic)

    const candidates = strikeData.filter(s => {
      const delta = Math.abs(s.delta || 0);
      return delta >= 0.35 && delta <= 0.55 &&
             s.oi >= 5000 &&
             s.volume >= 500 &&
             (s.bidAskSpread || 0) / (s.ltp || 1) < 0.05;
    });

    if (candidates.length === 0) return null;

    return candidates.reduce((best, s) => {
      const expectedReturn = (targetMove * (s.delta || 0.45)) / (s.ltp || 1);
      if (expectedReturn > (best._expectedReturn || 0)) {
        return { ...s, _expectedReturn: expectedReturn };
      }
      return best;
    }, { _expectedReturn: 0 });
  }

  // ── Render Score Breakdown HTML ──
  renderBreakdown(factors) {
    return Object.values(factors).map(f => {
      const noDataTag = f.noData ? '<span style="font-size:0.6rem;color:#78909C;margin-left:4px;">No Data</span>' : '';
      return `
      <div class="score-factor">
        <span class="factor-label">${f.label}${noDataTag}</span>
        <div class="factor-bar">
          <div class="factor-fill" style="width:${(f.score/f.max)*100}%;background:${f.color};"></div>
        </div>
        <span class="factor-value" style="color:${f.color};">${f.score}/${f.max}</span>
      </div>
    `;
    }).join('');
  }

  // ── Position Sizing Calculator ──
  computePositionSize(capital, riskPct, entryPrice, stopLoss) {
    const riskAmount = capital * (riskPct / 100);
    const riskPerShare = Math.abs(entryPrice - stopLoss);
    if (riskPerShare <= 0) return { shares: 0, lotValue: 0, riskAmount: 0 };
    const shares = Math.floor(riskAmount / riskPerShare);
    return {
      shares,
      lotValue: +(shares * entryPrice).toFixed(0),
      riskAmount: +(shares * riskPerShare).toFixed(0),
      riskPerShare: +riskPerShare.toFixed(2)
    };
  }

  // ── Render Risk Management Row HTML ──
  renderRiskRow(risk, direction) {
    if (!risk) return '';
    const fmt = (n) => n == null ? '—' : (+n).toLocaleString('en-IN', { maximumFractionDigits: 2 });
    const rrColor = risk.riskReward >= 2 ? '#26A69A' : risk.riskReward >= 1.3 ? '#FFA726' : '#EF5350';
    const rrLabel = risk.riskReward >= 2 ? 'Favorable' : risk.riskReward >= 1.3 ? 'Fair' : 'Poor';

    return `
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:12px;padding:10px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);">
        <div style="text-align:center;">
          <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;">Entry</div>
          <div style="font-size:0.85rem;font-weight:600;color:var(--text-primary);">₹${fmt(risk.entry)}</div>
        </div>
        <div style="text-align:center;">
          <div style="font-size:0.65rem;color:#EF5350;text-transform:uppercase;letter-spacing:0.5px;">Stop Loss</div>
          <div style="font-size:0.85rem;font-weight:600;color:#EF5350;">₹${fmt(risk.stopLoss)}</div>
        </div>
        <div style="text-align:center;">
          <div style="font-size:0.65rem;color:#26A69A;text-transform:uppercase;letter-spacing:0.5px;">Target 1</div>
          <div style="font-size:0.85rem;font-weight:600;color:#26A69A;">₹${fmt(risk.target1)}</div>
        </div>
        <div style="text-align:center;">
          <div style="font-size:0.65rem;color:#26A69A;text-transform:uppercase;letter-spacing:0.5px;">Target 2</div>
          <div style="font-size:0.85rem;font-weight:600;color:#26A69A;">₹${fmt(risk.target2)}</div>
        </div>
        <div style="text-align:center;">
          <div style="font-size:0.65rem;color:${rrColor};text-transform:uppercase;letter-spacing:0.5px;">R:R Ratio</div>
          <div style="font-size:0.85rem;font-weight:600;color:${rrColor};">${risk.riskReward}:1 <span style="font-size:0.6rem;">${rrLabel}</span></div>
        </div>
        <div style="text-align:center;">
          <div style="font-size:0.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;">ATR</div>
          <div style="font-size:0.85rem;font-weight:600;color:var(--text-secondary);">₹${fmt(risk.atrUsed)}</div>
        </div>
      </div>`;
  }
}

globalThis.scoringEngine = new ScoringEngine();
