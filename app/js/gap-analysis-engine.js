/**
 * TradeSignal — Gap Analysis Rule Engine (Indian Equity Market)
 * Implements refined 6-layer, 24-rule gap analysis institutional overlay.
 * Safer execution with conditional overrides and confirmation requirements.
 * Uses shared TI module for indicator computations.
 */

class GapAnalysisEngine {
  constructor() {
    // Gap fill history — populated from historical OHLCV via buildGapFillHistory()
    // Format: { symbol: { fillRate: 0-1, totalGaps: N, filledGaps: N } }
    this.gapFillDatabase = {};
    
    // Track immediate reversal detection (first 10 min)
    this.reversalDetector = {};
    
    // Auto-initialize gap fill history on startup
    this.initializationFlag = false;
  }

  /**
   * Initialize gap fill history from market snapshot data.
   * Call this once after initial snapshot/OHLCV load.
   */
  initializeGapFillHistory(symbol, ohlcv) {
    if (!symbol || this.gapFillDatabase[symbol]) return;  // Already initialized or invalid
    this.buildGapFillHistory(symbol, ohlcv);
    this.initializationFlag = true;
  }

  /**
   * Build gap fill history from historical OHLCV data.
   * A "gap" is an open price deviating > 0.5% from previous close.
   * A filled gap means intraday price returned to the previous close level.
   * Call this once per stock after loading OHLCV data.
   */
  buildGapFillHistory(symbol, ohlcv) {
    if (!ohlcv || ohlcv.length < 10) return;
    let totalGaps = 0, filledGaps = 0;
    for (let i = 1; i < ohlcv.length; i++) {
      const prevClose = ohlcv[i - 1].close;
      const open = ohlcv[i].open;
      const gapPct = Math.abs((open - prevClose) / prevClose * 100);
      if (gapPct < 0.5) continue;   // skip insignificant gaps
      totalGaps++;
      const isGapUp = open > prevClose;
      // Check if gap was filled intraday
      if (isGapUp && ohlcv[i].low <= prevClose) filledGaps++;
      else if (!isGapUp && ohlcv[i].high >= prevClose) filledGaps++;
    }
    if (totalGaps > 0) {
      this.gapFillDatabase[symbol] = {
        fillRate: filledGaps / totalGaps,
        totalGaps,
        filledGaps
      };
    }
  }

  computeGapScore(data) {
    const { closes, snapshot = {}, highs, lows, volumes, fundamentals = {}, optionsData = {} } = data;
    
    // Validate inputs
    if (!closes || closes.length < 2) {
      return { gapTier: 0, score: 0, override: null, log: [], confirmationStrong: false, isFadeScenario: false };
    }

    const lastClose = closes[closes.length - 1];
    const prevClose = closes[closes.length - 2];
    const todayOpen = snapshot.open || lastClose;
    
    if (!todayOpen || !prevClose) {
      return { gapTier: 0, score: 0, override: null, log: [], confirmationStrong: false, isFadeScenario: false };
    }

    const gapPercent = ((todayOpen - prevClose) / prevClose) * 100;
    const absGap = Math.abs(gapPercent);
    const isGapUp = gapPercent > 0;
    
    let gapTier = 0;
    let multiplier = 1.0;
    let confirmationStrong = false;
    let isFadeScenario = false;
    
    // ── LAYER 1 (R-01): Gap Size Tiering (Refined Multipliers) ──
    if (absGap < 0.25) {
      return { gapTier: 0, score: 0, override: null, log: ['Tier 0: No actionable gap'], confirmationStrong: false, isFadeScenario: false };
    } else if (absGap <= 1) {
      gapTier = 1; 
      multiplier = 0.6;  // REFINED: was 0.5
    } else if (absGap <= 3) {
      gapTier = 2; 
      multiplier = 1.0;
    } else if (absGap <= 6) {
      gapTier = 3; 
      multiplier = 0.85;  // REFINED: was 0.7
    } else {
      gapTier = 4; 
      multiplier = 0.6;
    }

    let score = 0;
    let log = [`Gap Tier: ${gapTier} (${gapPercent.toFixed(2)}%)`];
    let overrideTrigger = null;

    // ── Compute technical indicators early ──
    const adx = TI.computeADX(highs, lows, closes);
    const rsi = TI.computeRSI(closes);
    const catalystScore = snapshot.catalyst_score || 0;
    const vix = snapshot.india_vix || 15;

    // ── LAYER 6 (Early Checks): Hard Overrides (Conditional) ──
    
    // OV-01: India VIX > 30 → No fresh trades
    if (vix > 30) {
      return { gapTier, score: 0, override: 'WATCH', log: [...log, 'OV-01: India VIX > 30 (Extreme Risk)'], confirmationStrong: false, isFadeScenario: false };
    }
    
    // OV-03: Stock under ASM/ESM → Mandatory avoid
    if (snapshot.surveillance_tags && snapshot.surveillance_tags.length > 0) {
      return { gapTier, score: 0, override: 'AVOID', log: [...log, 'OV-03: Stock under ASM/ESM'], confirmationStrong: false, isFadeScenario: false };
    }

    // ── LAYER 1 (cont): Gap Type Classification (Refined) ──
    let gapType = 'Common';
    let hasLongWick = false;
    
    // Check for long wick (high/low significantly away from close)
    if (highs.length > 0 && lows.length > 0) {
      const recent_wicks = highs.slice(-5).map((h, i) => {
        const l = lows[lows.length - 5 + i];
        const c = closes[closes.length - 5 + i];
        return Math.max(Math.abs(h - c), Math.abs(c - l)) / c;
      });
      hasLongWick = recent_wicks.some(w => w > 0.03);  // >3% wick
    }
    
    // Refined Gap Type Classification
    if (catalystScore >= 6 && adx > 25 && lastClose === (snapshot.vwap || lastClose)) {
      gapType = 'Breakaway';   // Catalyst + Trend + VWAP held
      confirmationStrong = true;
    } else if (adx > 30 && lastClose !== (snapshot.vwap || lastClose)) {
      gapType = 'Runaway';      // Strong trend without VWAP retest
    } else if ((isGapUp && rsi > 78) || (!isGapUp && rsi < 22)) {
      if (hasLongWick) {
        gapType = 'Exhaustion';  // Overbought/sold with long wick
        isFadeScenario = true;
      }
    } else if (adx < 20) {
      gapType = 'Common';
      isFadeScenario = true;    // Low ADX = high gap fill probability
    }
    log.push(`Gap Type: ${gapType}`);

    // ── LAYER 1 (cont): Gap Fill Rate (R-02) ──
    const symbol = data.symbol || '';
    const gapHistory = this.gapFillDatabase[symbol];
    if (gapHistory && gapHistory.totalGaps >= 5) {
      const fr = gapHistory.fillRate;
      if (gapType === 'Common' && fr > 0.7) {
        // High fill rate on common gaps → fade bias
        score += isGapUp ? -8 : 8;
        isFadeScenario = true;
        log.push(`R-02: High gap fill rate (${(fr * 100).toFixed(0)}%) — fade bias`);
      } else if (gapType === 'Breakaway' && fr < 0.3) {
        // Low fill rate on breakaway → continuation bias
        score += isGapUp ? 10 : -10;
        confirmationStrong = true;
        log.push(`R-02: Low fill rate (${(fr * 100).toFixed(0)}%) — continuation bias`);
      }
    }

    // ── LAYER 1 (cont): Catalyst Persistence (R-04) ──
    if (catalystScore >= 6) {
      score += isGapUp ? 10 : -10;
      confirmationStrong = true;
      log.push(`R-04: Strong Catalyst (${catalystScore})`);
    }

    // ── LAYER 2: Pre-Open Signals (R-05 to R-09) ──
    const preOpenBuy = snapshot.pre_open_buy_qty ?? snapshot.buy_qty;
    const preOpenSell = snapshot.pre_open_sell_qty ?? snapshot.sell_qty;
    if (preOpenBuy != null && preOpenSell != null && (preOpenBuy + preOpenSell) > 0) {
      const bQty = preOpenBuy;
      const sQty = preOpenSell;
      const imbalance = (bQty - sQty) / (bQty + sQty || 1);
      
      // Capped to ±10 (refined: was ±12)
      if (isGapUp) {
        if (imbalance > 0.5) score += 10;
        else if (imbalance < -0.3) score -= 10;
      } else {
        if (imbalance < -0.5) score -= 10;
        else if (imbalance > 0.3) score += 10;
      }
      log.push(`R-05: Pre-Open Imbalance (${imbalance.toFixed(2)})`);
    }

    // ── R-06: Gift Nifty Premium/Discount (aligned check) ──
    if (snapshot.gift_nifty_premium !== undefined) {
      const giftPrem = snapshot.gift_nifty_premium;
      if (isGapUp && giftPrem > 0.5) {
        score += 8;  // Gift aligned with gap up
        log.push(`R-06: Gift Nifty +${giftPrem.toFixed(2)}% supports gap up`);
      } else if (isGapUp && giftPrem <= 0) {
        score -= 8;  // Divergence: gap up but Gift down
        isFadeScenario = true;
      } else if (!isGapUp && giftPrem < -0.5) {
        score += 8;  // Gift aligned with gap down
        log.push(`R-06: Gift Nifty ${giftPrem.toFixed(2)}% supports gap down`);
      } else if (!isGapUp && giftPrem > 0) {
        score -= 8;  // Divergence: gap down but Gift up
        isFadeScenario = true;
      }
    }

    // ── R-08: VIX Level (Refined multiplier: 0.7 instead of 0.5) ──
    if (vix >= 18 && vix <= 25) {
      if (gapTier < 2 || catalystScore < 7) {
        multiplier *= 0.7;  // REFINED: was 0.5
        log.push('R-08: Elevated VIX dampens score');
      }
    } else if (vix < 13) {
      multiplier *= 0.9;
    }

    // ── LAYER 3: Opening Range & VWAP Validation (Apply after 5–10 min) ──
    const orHigh = snapshot.or_high || (snapshot.ohlc ? snapshot.ohlc.high : 0);
    const orLow = snapshot.or_low || (snapshot.ohlc ? snapshot.ohlc.low : 0);
    
    if (orHigh && orLow && lastClose) {
      if (isGapUp) {
        // R-11 (Gap Up): Bullish continuation
        if (lastClose > orHigh) {
          score += 15;  // Price breaks above OR_High
          log.push('R-11: Price > OR_High (bullish continuation)');
          confirmationStrong = true;
        } else if (lastClose < orLow) {
          score -= 15;  // Gap failure → bearish
          isFadeScenario = true;
          log.push('R-11: Price < OR_Low (gap failure)');
        }
      } else {
        // R-12 (Gap Down): Refined logic
        if (lastClose < orLow) {
          score += 15;  // REFINED: was -15 (bearish continuation now POSITIVE for gap down)
          log.push('R-12: Price < OR_Low (bearish continuation)');
          confirmationStrong = true;
        } else if (lastClose > orHigh) {
          score -= 15;  // REFINED: was +20 (V-shape recovery now NEGATIVE)
          isFadeScenario = true;
          log.push('R-12: Price > OR_High (V-shape recovery, gap fills)');
        }
      }
    }

    // ── R-13: VWAP Position (Refined with rejection logic) ──
    const vwap = snapshot.vwap || snapshot.avg_price;
    if (vwap && vwap > 0) {
      const vwapDiff = ((lastClose - vwap) / vwap) * 100;
      
      if (isGapUp) {
        if (lastClose > vwap) {
          score += 8;    // Above VWAP: strength
          log.push(`R-13: Price > VWAP (${vwapDiff.toFixed(2)}% above)`);
        } else if (vwapDiff > -1) {
          score -= 0;    // Near VWAP: neutral
        } else {
          score -= 8;    // VWAP rejection (below)
          isFadeScenario = true;
          log.push(`R-13: Price rejected at VWAP (${vwapDiff.toFixed(2)}% below)`);
        }
      } else {
        if (lastClose < vwap) {
          score += 8;    // Below VWAP: weakness confirmation
          log.push(`R-13: Price < VWAP (${vwapDiff.toFixed(2)}% below)`);
        } else if (vwapDiff < 1) {
          score -= 0;    // Near VWAP: neutral
        } else {
          score -= 8;    // VWAP rejection (above)
          isFadeScenario = true;
          log.push(`R-13: Price rejected at VWAP (${vwapDiff.toFixed(2)}% above)`);
        }
      }
    }

    // ── LAYER 4: Intraday Continuation (R-15 to R-18) ──
    
    // R-15: Sector Breadth
    if (snapshot.sector_breadth_pct !== undefined) {
      if (isGapUp) {
        if (snapshot.sector_breadth_pct > 70) {
          score += 10;
          log.push(`R-15: Sector breadth ${snapshot.sector_breadth_pct}% > 70% (aligned)`);
        } else if (snapshot.sector_breadth_pct < 40) {
          score -= 10;
          isFadeScenario = true;
          log.push(`R-15: Sector breadth ${snapshot.sector_breadth_pct}% < 40% (divergence)`);
        }
      } else {
        if (snapshot.sector_breadth_pct < 30) {
          score += 10;
          log.push(`R-15: Sector breadth ${snapshot.sector_breadth_pct}% < 30% (aligned)`);
        } else if (snapshot.sector_breadth_pct > 60) {
          score -= 10;
          isFadeScenario = true;
          log.push(`R-15: Sector breadth ${snapshot.sector_breadth_pct}% > 60% (divergence)`);
        }
      }
    }

    // R-17: Volume Confirmation (Refined: low volume now -8, was -5)
    if (volumes && volumes.length >= 20) {
      const volRatio = TI.computeVolumeRatio(volumes);
      if (volRatio > 2) {
        score += 8;
        log.push(`R-17: High volume (${volRatio.toFixed(1)}x) confirms gap`);
      } else if (volRatio < 0.5) {
        score -= 8;   // REFINED: was -5
        isFadeScenario = true;
        log.push('R-17: Low volume (weak gap, fade risk)');
      }
    }

    // R-18: Price Holding OR Level
    if (orHigh && orLow) {
      if (isGapUp) {
        if (lastClose > orHigh) {
          score += 5;
          log.push('R-18: Holding above OR_High');
        } else if (lastClose < orLow) {
          score -= 5;
          log.push('R-18: Failed to hold OR level');
        }
      } else {
        if (lastClose < orLow) {
          score += 5;
          log.push('R-18: Holding below OR_Low');
        } else if (lastClose > orHigh) {
          score -= 5;
          log.push('R-18: Failed to hold OR level');
        }
      }
    }

    // ── LAYER 5: India Specifics (R-20 to R-21) ──
    
    // R-20: Weekly Expiry + Banking Sector (Refined: 0.7 instead of 0.65)
    if (snapshot.is_weekly_expiry && snapshot.sector === 'Banking') {
      multiplier *= 0.7;  // REFINED: was 0.65
      log.push('R-20: Weekly Expiry + Banking (0.7× multiplier)');
    }

    // R-21: Index Divergence (NEW)
    if (snapshot.index_direction && snapshot.index_direction !== (isGapUp ? 'UP' : 'DOWN')) {
      score -= 6;
      isFadeScenario = true;
      log.push(`R-21: Index divergence (stock ${isGapUp ? 'up' : 'down'}, index opposite)`);
    }

    // ── LAYER 6 (cont): Conditional Overrides with Confirmation ──
    
    // OV-05: Promoter Pledge Invocation + Gap Down + Continuation (Refined)
    if (snapshot.promoter_pledge_invoked && !isGapUp && score < -10) {
      overrideTrigger = 'STRONG SELL';
      log.push('OV-05: Promoter pledge + gap down + negative momentum');
    }

    // OV-06: Earnings Miss + Gap Up + VWAP Break (Refined with validation)
    if (snapshot.earnings_surprise_pct !== undefined) {
      if (snapshot.earnings_surprise_pct < -5 && isGapUp && lastClose < vwap) {
        overrideTrigger = 'STRONG FADE';
        isFadeScenario = true;
        log.push('OV-06: Earnings miss + gap up + VWAP break');
      }
    }

    // OV-07: Immediate Reversal (Gap >5% + First 10 min reversal) — NEW
    if (gapTier >= 4 && absGap > 5) {
      // Check if there's immediate reversal signal (high within first bar if gap up, low if gap down)
      const priceMoveWithinGap = isGapUp 
        ? ((todayOpen - lows[lows.length - 1]) / todayOpen * 100)
        : ((highs[highs.length - 1] - todayOpen) / todayOpen * 100);
      
      if (priceMoveWithinGap > absGap * 0.5) {
        overrideTrigger = 'FADE';
        isFadeScenario = true;
        log.push(`OV-07: Extreme gap (${absGap.toFixed(2)}%) + immediate reversal detected`);
      }
    }

    // ── Final Score Calculation ──
    const finalScore = score * multiplier;

    return { 
      gapTier, 
      score: finalScore, 
      override: overrideTrigger, 
      log, 
      gapType, 
      gapFillRate: gapHistory?.fillRate,
      confirmationStrong,     // NEW: high confidence signal
      isFadeScenario          // NEW: indicates fade/reversal probability
    };
  }
}

globalThis.gapAnalysisEngine = new GapAnalysisEngine();
