/**
 * FNO Session Analyzer
 * Multi-phase market analysis engine
 * Premarket (3:30 PM prev day - 9:00 AM) → Opening (9:00-9:15 IST) → Live Session (9:15 AM - 3:30 PM IST)
 */

class FNOSessionAnalyzer {
  constructor() {
    this.currentSession = 'premarket'; // premarket | opening | live
    this.sessionStartTime = null;
    this.analysisHistory = [];
    this.maxAnalysisItems = 100;
    
    // Thresholds
    this.thresholds = {
      premarket: { minScore: 60, minVolRatio: 1.2, minOI: 10000 },
      opening: { minScore: 55, minVolRatio: 1.0, minOI: 5000 },
      live: { minScore: 50, minVolRatio: 0.8, minOI: 3000 }
    };
  }

  /**
   * Determine current market session based on time
   * IST timezone (UTC+5:30)
   */
  getCurrentSession() {
    const now = new Date();
    const istTime = new Date(now.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }));
    const hours = istTime.getHours();
    const minutes = istTime.getMinutes();
    const totalMinutes = hours * 60 + minutes;

    // Premarket: 3:30 PM (prev day) - 9:00 AM IST  (930 = 15:30, 540 = 9:00)
    if (totalMinutes >= 930 || totalMinutes < 540) return 'premarket';
    
    // Opening: 9:00 AM - 9:15 AM IST
    if (totalMinutes >= 540 && totalMinutes <= 555) return 'opening';
    
    // Live: 9:15 AM - 3:30 PM IST
    if (totalMinutes > 555 && totalMinutes < 930) return 'live';
    
    // Market Closed (shouldn't reach here, but fallback)
    return 'closed';
  }

  /**
   * Main analysis orchestrator
   */
  async analyzeStockForSession(stock, allStocks = []) {
    const session = this.getCurrentSession();
    if (session === 'closed') {
      return { session: 'closed', signal: 'WAIT', message: 'Market is closed' };
    }

    this.currentSession = session;

    // Route to appropriate analyzer based on session
    let analysis;
    if (session === 'premarket') {
      analysis = this.analyzePremarket(stock);
    } else if (session === 'opening') {
      analysis = this.analyzeOpening(stock, allStocks);
    } else if (session === 'live') {
      analysis = this.analyzeLive(stock);
    }

    // Add timestamp and session metadata
    analysis.session = session;
    analysis.timestamp = new Date().toISOString();
    analysis.sessionTime = this._getSessionTime();

    // Store in history
    this.analysisHistory.push({
      symbol: stock.symbol,
      ...analysis,
      time: new Date()
    });

    if (this.analysisHistory.length > this.maxAnalysisItems) {
      this.analysisHistory.shift();
    }

    return analysis;
  }

  /**
   * PREMARKET ANALYSIS (3:30 PM prev day - 9:00 AM IST)
   * Focus: Gap analysis, overnight sentiment, FII flows, circuit breaker probability
   */
  analyzePremarket(stock) {
    let score = 0;
    const factors = {};
    const signals = [];

    // 1. Gap Analysis (30pts)
    let gapScore = 0;
    const gapPct = stock.changePercent || 0; // Pre-open gap % mapped from change_pct
    const absGap = Math.abs(gapPct);

    if (absGap > 2.5) gapScore = 30; // Significant gap
    else if (absGap > 1.5) gapScore = 20;
    else if (absGap > 0.5) gapScore = 10;
    else gapScore = 5;

    if (gapPct > 0) signals.push('Gap Up detected');
    else if (gapPct < 0) signals.push('Gap Down detected');

    factors.gapAnalysis = { score: gapScore, max: 30, label: 'Gap Analysis' };
    score += gapScore;

    // 2. Pre-open OI Change (20pts)
    let oiScore = 0;
    const oiChangePct = stock.optionsData?.oiChangePercent || 0;
    const absOIChange = Math.abs(oiChangePct);

    if (absOIChange > 20) oiScore = 20;
    else if (absOIChange > 10) oiScore = 15;
    else if (absOIChange > 5) oiScore = 10;
    else oiScore = 5;

    if (oiChangePct > 10) signals.push('🟢 High OI accumulation');
    else if (oiChangePct < -10) signals.push('🔴 High OI reduction');

    factors.oiChange = { score: oiScore, max: 20, label: 'OI Change' };
    score += oiScore;

    // 3. Overnight News/Events (20pts)
    const hasPositiveNews = stock.hasPositiveNews || false;
    const hasNegativeNews = stock.hasNegativeNews || false;

    let newsScore = 0;
    if (hasPositiveNews) {
      newsScore = 20;
      signals.push('📰 Positive news');
    } else if (hasNegativeNews) {
      newsScore = 0;
      signals.push('📰 Negative news');
    } else {
      newsScore = 10;
    }

    factors.newsEvent = { score: newsScore, max: 20, label: 'News/Events' };
    score += newsScore;

    // 4. Previous Close Pattern (15pts)
    const priceGapPct = gapPct; // Simply use the same gap parameter

    let priceScore = 0;
    if (priceGapPct > 1.5) priceScore = 15;
    else if (priceGapPct > 0.5) priceScore = 10;
    else if (priceGapPct > -0.5) priceScore = 8;
    else if (priceGapPct > -1.5) priceScore = 5;
    else priceScore = 0;

    factors.priceGap = { score: priceScore, max: 15, label: 'Price Pattern' };
    score += priceScore;

    // 5. FII/DII Sentiment (15pts)
    const fiiFlow = stock.fiiFlow || 0; // net FII flow in points
    let fiiScore = 0;

    if (fiiFlow > 500) {
      fiiScore = 15;
      signals.push('💰 Strong FII buying');
    } else if (fiiFlow < -500) {
      fiiScore = 0;
      signals.push('💰 Strong FII selling');
    } else if (fiiFlow > 0) {
      fiiScore = 10;
    } else {
      fiiScore = 5;
    }

    factors.fiiSentiment = { score: fiiScore, max: 15, label: 'FII Sentiment' };
    score += fiiScore;

    // Determine direction
    let direction = 'NEUTRAL';
    let confidence = 'LOW';

    if (gapPct > 0.5 && oiChangePct > 5) {
      direction = 'BULLISH';
      confidence = score >= 80 ? 'HIGH' : 'MEDIUM';
    } else if (gapPct < -0.5 && oiChangePct < -5) {
      direction = 'BEARISH';
      confidence = score >= 80 ? 'HIGH' : 'MEDIUM';
    }

    // Recommendation
    let recommendation = 'WAIT';
    if (direction === 'BULLISH' && score >= 70) recommendation = 'BUY_AT_OPEN';
    else if (direction === 'BEARISH' && score >= 70) recommendation = 'SELL_AT_OPEN';
    else if (score >= 60) recommendation = 'WATCH_CLOSELY';

    return {
      score: Math.min(score, 100),
      direction,
      confidence,
      recommendation,
      factors,
      signals,
      gap: gapPct,
      oiChange: oiChangePct,
      strategy: this._getPremarketStrategy(direction, score)
    };
  }

  /**
   * OPENING PHASE ANALYSIS (9:00 AM - 9:15 AM IST)
   * Focus: First 15 minutes momentum, volume buildup, reversal patterns
   */
  analyzeOpening(stock, allStocks = []) {
    let score = 0;
    const factors = {};
    const signals = [];

    // 1. Opening Momentum (25pts)
    const changePct = stock.changePercent || 0;
    const openPrice = stock.ltp || stock.open || 0;
    const prevClose = openPrice / (1 + (changePct / 100)); // Derive prev close
    const openHigh = stock.highs && stock.highs.length ? stock.highs[stock.highs.length - 1] : openPrice;
    const openLow = stock.lows && stock.lows.length ? stock.lows[stock.lows.length - 1] : openPrice;

    const openingRange = openHigh - openLow;
    const openingGap = changePct;

    let momentumScore = 0;
    const absOpenGap = Math.abs(openingGap);

    if (absOpenGap > 2) momentumScore = 25;
    else if (absOpenGap > 1.2) momentumScore = 18;
    else if (absOpenGap > 0.5) momentumScore = 12;
    else momentumScore = 6;

    if (openingGap > 1) signals.push('📈 Strong opening momentum');
    else if (openingGap < -1) signals.push('📉 Weak opening');

    factors.openingMomentum = { score: momentumScore, max: 25, label: 'Opening Momentum' };
    score += momentumScore;

    // 2. 15-min Volume (25pts)
    const vol15min = stock.volume || 0; // Total day volume up to 9:15 is basically 15min volume
    // Derive average daily volume from historical array
    const avgDailyVol = Array.isArray(stock.volumes) && stock.volumes.length ? 
      (stock.volumes.slice(-5).reduce((a,b)=>a+b,0) / Math.min(5, stock.volumes.length)) : 1000000;
    const volRatio = vol15min / (avgDailyVol / 26); // roughly 26 fifteen-min periods in 390 min

    let volScore = 0;
    if (volRatio > 3) volScore = 25;
    else if (volRatio > 2) volScore = 20;
    else if (volRatio > 1.2) volScore = 15;
    else if (volRatio > 0.8) volScore = 10;
    else volScore = 5;

    if (volRatio > 2) signals.push('💸 Exceptionally high volume');

    factors.volumeBuildup = { score: volScore, max: 25, label: '15-min Volume' };
    score += volScore;

    // 3. PCR Momentum (20pts)
    const callOI = stock.snapshot?.atm_option?.ce_oi || stock.atmCallOI || 0;
    const putOI = stock.snapshot?.atm_option?.pe_oi || stock.atmPutOI || 0;
    const pcr = stock.snapshot?.atm_option?.pcr || (putOI > 0 ? callOI / putOI : 1);
    
    // Fallback: compare against 1 since we don't have historical PCR
    const pcrBias = pcr - 1.0; 

    let pcrScore = 0;
    if (pcrBias > 0.4) {
      pcrScore = 20;
      signals.push('📊 PCR highly bullish (>1.4)');
    } else if (pcrBias < -0.4) {
      pcrScore = 5;
      signals.push('📊 PCR highly bearish (<0.6)');
    } else if (pcr > 0.9) {
      pcrScore = 15;
    } else if (pcr < 0.7) {
      pcrScore = 10;
    } else {
      pcrScore = 12;
    }

    factors.pcrMomentum = { score: pcrScore, max: 20, label: 'PCR Momentum' };
    score += pcrScore;

    // 4. IV Regime (15pts)
    const currentIV = stock.snapshot?.atm_option?.avg_iv || stock.atmIV || 30;
    const avgHistoricalIV = 25; // Base assumption if real HV not present
    const ivRatio = currentIV / avgHistoricalIV;

    let ivScore = 0;
    if (ivRatio > 1.2) {
      if (openingGap > 0) ivScore = 15; // IV expansion on up move — strong
      else ivScore = 5; // IV expansion on down move — more volatility
      signals.push('📈 IV expanding strongly');
    } else if (ivRatio < 0.8) {
      ivScore = 10; // IV compression
      signals.push('📉 IV compressing');
    } else {
      ivScore = 10;
    }

    factors.ivRegime = { score: ivScore, max: 15, label: 'IV Regime' };
    score += ivScore;

    // 5. Multi-legged move or reversal detection (15pts)
    let priceAction = 0;
    const high15min = openHigh;
    const low15min = openLow;
    const range15min = high15min - low15min;
    const rangePct = (range15min / prevClose) * 100;

    // Is it a reversal pattern (came back towards prev close)?
    const priceFromClose = Math.abs(openPrice - prevClose);
    const reversalPct = (priceFromClose / prevClose) * 100;

    if (rangePct > 1.5 && reversalPct < 0.3) {
      priceAction = 15; // Wide range but returned — high volatility
      signals.push('🔄 Range reversal pattern');
    } else if (rangePct > 1.2) {
      priceAction = 12;
    } else if (rangePct > 0.6) {
      priceAction = 8;
    } else {
      priceAction = 4;
    }

    factors.priceAction = { score: priceAction, max: 15, label: 'Price Action' };
    score += priceAction;

    // Determine direction and confidence
    let direction = 'NEUTRAL';
    let confidence = 'LOW';

    if (momentumScore >= 15 && volScore >= 15 && openingGap > 0.5) {
      direction = 'BULLISH';
      confidence = score >= 75 ? 'HIGH' : score >= 55 ? 'MEDIUM' : 'LOW';
    } else if (momentumScore <= 8 && volScore <= 8 && openingGap < -0.5) {
      direction = 'BEARISH';
      confidence = score >= 75 ? 'HIGH' : score >= 55 ? 'MEDIUM' : 'LOW';
    } else if (score >= 65) {
      direction = 'BULLISH';
      confidence = 'MEDIUM';
    } else if (score <= 35) {
      direction = 'BEARISH';
      confidence = 'MEDIUM';
    }

    // Recommendation
    let recommendation = 'MONITOR';
    if (direction === 'BULLISH' && confidence === 'HIGH') {
      recommendation = 'SHORT_CALL_SELL';
    } else if (direction === 'BEARISH' && confidence === 'HIGH') {
      recommendation = 'SHORT_PUT_SELL';
    } else if (direction === 'BULLISH' && score >= 70) {
      recommendation = 'CE_BUY';
    } else if (direction === 'BEARISH' && score >= 70) {
      recommendation = 'PE_BUY';
    }

    return {
      score: Math.min(score, 100),
      direction,
      confidence,
      recommendation,
      factors,
      signals,
      opening: { gap: openingGap, range: rangePct, volume: volRatio, pcr, iv: currentIV },
      strategy: this._getOpeningStrategy(direction, score, volRatio, pcr)
    };
  }

  /**
   * LIVE SESSION ANALYSIS (post 9:15 AM IST)
   * Focus: Real-time momentum, PCR swings, IV crush/expansion, max pain dynamics
   */
  analyzeLive(stock) {
    let score = 0;
    const factors = {};
    const signals = [];

    // 1. Real-time Price Action (25pts)
    const changePct = stock.changePercent || 0;
    const ltp = stock.ltp || stock.close || 0;
    const prevClose = ltp / (1 + (changePct / 100));
    const open = stock.open || ltp;
    const high = stock.highs && stock.highs.length ? stock.highs[stock.highs.length - 1] : ltp;
    const low = stock.lows && stock.lows.length ? stock.lows[stock.lows.length - 1] : ltp;

    const dayChangePct = changePct;
    const intraRangePct = ((high - low) / prevClose) * 100;

    let priceScore = 0;
    const absDayChange = Math.abs(dayChangePct);

    if (absDayChange > 2.5) priceScore = 25;
    else if (absDayChange > 1.5) priceScore = 18;
    else if (absDayChange > 0.8) priceScore = 12;
    else if (absDayChange > 0.2) priceScore = 8;
    else priceScore = 4;

    if (dayChangePct > 1) signals.push('📈 Strong intraday uptrend');
    else if (dayChangePct < -1) signals.push('📉 Strong intraday downtrend');

    factors.priceAction = { score: priceScore, max: 25, label: 'Price Action' };
    score += priceScore;

    // 2. PCR Oscillation & Max Pain (25pts)
    const callOI = stock.snapshot?.atm_option?.ce_oi || stock.totalCallOI || 0;
    const putOI = stock.snapshot?.atm_option?.pe_oi || stock.totalPutOI || 0;
    const pcr = stock.snapshot?.atm_option?.pcr || (putOI > 0 ? callOI / putOI : 1);
    
    // Fallback bias since historical change from open isn't available from snapshot
    const pcrBias = pcr - 1.0;

    const maxPain = stock.maxPain || ltp;
    const distToMaxPain = Math.abs(ltp - maxPain);
    const distPct = (distToMaxPain / ltp) * 100;

    let pcrScore = 0;
    if (Math.abs(pcrBias) > 0.4) {
      pcrScore = 25; // Extreme PCR swing — major directional shift
      signals.push('🔄 Extreme PCR directional shift detected');
    } else if (pcr > 1.2 && dayChangePct < 0) {
      pcrScore = 15; // Put OI high on down days — reversal risk
      signals.push('⚠️ High put OI on downside (support build)');
    } else if (pcr < 0.8 && dayChangePct > 0) {
      pcrScore = 15; // Call OI dominance on up days
      signals.push('✅ Call OI dominance (resistance build)');
    } else {
      pcrScore = 10;
    }

    // Check if price is near max pain
    if (distPct < 0.5) {
      pcrScore = Math.max(pcrScore, 20);
      signals.push('🎯 Trading at max pain');
    }

    factors.pcrMaxPain = { score: pcrScore, max: 25, label: 'PCR & Max Pain' };
    score += pcrScore;

    // 3. Intraday Volume (20pts)
    const volume = stock.volume || 0;
    const avgVol = Array.isArray(stock.volumes) && stock.volumes.length ? 
      (stock.volumes.slice(-5).reduce((a,b)=>a+b,0) / Math.min(5, stock.volumes.length)) : 1000000;
    // For live session, ratio should be extrapolated based on time of day, but we'll use a simplified daily ratio.
    const volRatio = volume / avgVol;

    let volScore = 0;
    if (volRatio > 2) volScore = 20;
    else if (volRatio > 1.5) volScore = 15;
    else if (volRatio > 1.0) volScore = 12;
    else if (volRatio > 0.7) volScore = 8;
    else volScore = 4;

    if (volRatio > 1.8) signals.push('💸 Extraordinary volume burst');

    factors.volume = { score: volScore, max: 20, label: 'Intraday Volume' };
    score += volScore;

    // 4. IV Crush / Expansion (15pts)
    const currentIV = stock.atmIV || 30;
    const openIV = stock.openIV || 30;
    const ivChange = ((currentIV - openIV) / openIV) * 100;

    let ivScore = 0;
    if (ivChange > 15) {
      ivScore = 15;
      signals.push('📈 IV expansion');
    } else if (ivChange < -15) {
      ivScore = 10;
      signals.push('📉 IV crush');
    } else if (ivChange > 5) {
      ivScore = 12;
    } else if (ivChange < -5) {
      ivScore = 8;
    } else {
      ivScore = 8;
    }

    factors.ivDynamics = { score: ivScore, max: 15, label: 'IV Dynamics' };
    score += ivScore;

    // 5. Momentum Confirmation (15pts)
    // Use actual historical closes if available for meaningful RSI,
    // otherwise fall back to the 4 OHLC points with a "no data" fallback
    const rsiCloses = stock.historicalCloses || stock.closes;
    const rsi = rsiCloses && rsiCloses.length >= 15
      ? TI.computeRSI(rsiCloses, 14)
      : (stock.rsi || 50);
    const macdTrend = stock.macdHistogram || 0;

    let momentumScore = 0;
    if (rsi > 70 && dayChangePct > 0) {
      momentumScore = 15; // Overbought on up move
      signals.push('⚡ Bullish momentum confirmed');
    } else if (rsi < 30 && dayChangePct < 0) {
      momentumScore = 15; // Oversold on down move
      signals.push('⚡ Bearish momentum confirmed');
    } else if ((rsi > 60 && dayChangePct > 0.5) || (rsi < 40 && dayChangePct < -0.5)) {
      momentumScore = 12;
    } else if (Math.abs(dayChangePct) > 0.5) {
      momentumScore = 10;
    } else {
      momentumScore = 5;
    }

    factors.momentum = { score: momentumScore, max: 15, label: 'Momentum' };
    score += momentumScore;

    // Determine direction
    let direction = 'NEUTRAL';
    let confidence = 'LOW';

    if (dayChangePct > 0.8 && pcr < 0.9 && rsi > 55) {
      direction = 'BULLISH';
      confidence = score >= 75 ? 'HIGH' : 'MEDIUM';
    } else if (dayChangePct < -0.8 && pcr > 1.1 && rsi < 45) {
      direction = 'BEARISH';
      confidence = score >= 75 ? 'HIGH' : 'MEDIUM';
    } else if (score >= 70) {
      direction = dayChangePct > 0 ? 'BULLISH' : 'BEARISH';
      confidence = 'MEDIUM';
    }

    // Recommendation based on live data
    let recommendation = 'HOLD';

    if (direction === 'BULLISH' && confidence === 'HIGH') {
      if (volRatio > 1.5) {
        recommendation = 'BUY_CALL';
      } else {
        recommendation = 'BULL_CALL_SPREAD';
      }
    } else if (direction === 'BEARISH' && confidence === 'HIGH') {
      if (volRatio > 1.5) {
        recommendation = 'BUY_PUT';
      } else {
        recommendation = 'BEAR_PUT_SPREAD';
      }
    } else if (score >= 65) {
      recommendation = distToMaxPain < high * 0.02 ? 'STRADDLE' : (dayChangePct > 0 ? 'CALL_BULL' : 'PUT_BEAR');
    }

    return {
      score: Math.min(score, 100),
      direction,
      confidence,
      recommendation,
      factors,
      signals,
      liveMetrics: {
        dayChange: dayChangePct,
        intraRange: intraRangePct,
        volume: volRatio,
        pcr,
        pcrChange: pcrBias,
        maxPain,
        distToMaxPain: distPct,
        iv: currentIV,
        rsi
      },
      strategy: this._getLiveStrategy(direction, score, distPct, volRatio, pcr)
    };
  }

  /**
   * Generate premarket strategy
   */
  _getPremarketStrategy(direction, score) {
    if (direction === 'BULLISH' && score >= 75) {
      return {
        setup: 'Gap-up breakout expected',
        entry: 'Buy at open or on any pullback within first 2 candles',
        target: 'Previous resistance + 0.5% to 1%',
        stopLoss: 'Premarket low - 0.2%',
        timeframe: '5-15 min'
      };
    } else if (direction === 'BEARISH' && score >= 75) {
      return {
        setup: 'Gap-down breakdown expected',
        entry: 'Sell at open or on any bounce within first 2 candles',
        target: 'Previous support - 0.5% to 1%',
        stopLoss: 'Premarket high + 0.2%',
        timeframe: '5-15 min'
      };
    } else {
      return {
        setup: 'Uncertain opening expected',
        entry: 'Wait for first 5 minutes to confirm direction',
        target: 'TBD based on opening candle',
        stopLoss: 'Dynamic based on first candle range',
        timeframe: '10-30 min'
      };
    }
  }

  /**
   * Generate opening phase strategy
   */
  _getOpeningStrategy(direction, score, volRatio, pcr) {
    if (direction === 'BULLISH' && score >= 75) {
      if (volRatio > 2) {
        return {
          setup: 'Strong bullish opening with high volume',
          trade: 'Buy ATM Call or Bull Call Spread',
          entry: 'On any 1-min dip within 9:00-9:15 window',
          target: 'Day high + 0.3% to 0.5%',
          stopLoss: 'Opening low - 0.3%'
        };
      } else {
        return {
          setup: 'Bullish opening with moderate volume',
          trade: 'Bull Call Spread (less capital, defined risk)',
          entry: 'At market or on any dip',
          target: 'Day open + 0.8% to 1.2%',
          stopLoss: 'Opening low - 0.5%'
        };
      }
    } else if (direction === 'BEARISH' && score >= 75) {
      return {
        setup: 'Bearish opening',
        trade: pcr > 1.0 ? 'Buy Put or Bear Put Spread' : 'Sell Call Spread',
        entry: 'On any bounce to opening price',
        target: 'Opening low - 0.5% to 1%',
        stopLoss: 'Opening high + 0.3%'
      };
    } else {
      return {
        setup: 'Range-bound opening, uncertain direction',
        trade: 'Straddle or condor (earn premium)',
        entry: 'Sell ATM Call & Put or iron condor',
        target: 'Profit = 20-30% of max profit',
        stopLoss: 'Opening range ± 0.5%'
      };
    }
  }

  /**
   * Generate live session strategy
   */
  _getLiveStrategy(direction, score, distToMaxPain, volRatio, pcr) {
    if (direction === 'BULLISH' && score >= 75) {
      return {
        setup: 'Strong bullish momentum confirmed',
        trade: volRatio > 1.5 ? 'Buy Call directly' : 'Call Diagonal or Calendar',
        entry: 'On breakout of session high or support retest',
        target: `Day high + ${volRatio > 1.5 ? '0.5' : '1'}%`,
        stopLoss: 'Previous swing low',
        timeframe: '5-60 min',
        caution: distToMaxPain < 0.5 ? '⚠️ Near max pain — watch for reversal' : ''
      };
    } else if (direction === 'BEARISH' && score >= 75) {
      return {
        setup: 'Strong bearish momentum confirmed',
        trade: volRatio > 1.5 ? 'Buy Put directly' : 'Put Diagonal or Calendar',
        entry: 'On breakdown of session low or resistance retest',
        target: `Day low - ${volRatio > 1.5 ? '0.5' : '1'}%`,
        stopLoss: 'Previous swing high',
        timeframe: '5-60 min',
        caution: distToMaxPain < 0.5 ? '⚠️ Near max pain — watch for reversal' : ''
      };
    } else if (score >= 60) {
      return {
        setup: 'Moderate trend with some uncertainty',
        trade: 'Ratio spreading or synthetic long/short',
        entry: 'On trend confirmation',
        target: 'Realistic profit in direction of lesser cost',
        stopLoss: 'Defined by spreads width'
      };
    } else {
      return {
        setup: 'Neutral/choppy market',
        trade: 'Iron Condor, Butterfly, or credit spreads',
        entry: 'At ATM strikes near max pain',
        target: 'Earn 20-30% of credit',
        stopLoss: 'Max debit (defined risk)'
      };
    }
  }

  /**
   * Render analysis report for UI
   */
  renderAnalysisReport(analysis) {
    const { session = 'unknown', score, direction, confidence, recommendation, factors, signals, strategy, sessionTime = '' } = analysis;

    let html = `
    <div class="session-analysis-report">
      <div class="report-header">
        <div><h3>Session: ${session.toUpperCase()}</h3><small>${sessionTime}</small></div>
        <div class="score-badge ${score >= 70 ? 'score-high' : score >= 50 ? 'score-medium' : 'score-low'}">${Math.round(score)}</div>
      </div>

      <div class="analysis-signal">
        <span class="tag ${direction === 'BULLISH' ? 'tag-bullish' : direction === 'BEARISH' ? 'tag-bearish' : 'tag-neutral'}">${direction}</span>
        <span style="font-size:0.85rem;color:var(--text-secondary);">${confidence} Confidence</span>
      </div>

      <div style="margin:16px 0;padding:12px;background:var(--bg-secondary);border-radius:var(--radius-sm);">
        <strong style="display:block;margin-bottom:6px;font-size:0.85rem;color:var(--primary);">📋 Recommendation</strong>
        <h4 style="color:var(--text-primary);">${recommendation}</h4>
        <p style="font-size:0.78rem;color:var(--text-secondary);margin-top:8px;line-height:1.6;">
          ${strategy?.setup || 'Generic trading setup'}<br>
          <strong>Trade:</strong> ${strategy?.trade || 'N/A'}<br>
          <strong>Entry:</strong> ${strategy?.entry || 'N/A'}<br>
          <strong>Target:</strong> ${strategy?.target || 'N/A'}<br>
          <strong>Stop Loss:</strong> ${strategy?.stopLoss || 'N/A'}
        </p>
      </div>

      <div style="margin:16px 0;">
        <strong style="display:block;font-size:0.85rem;margin-bottom:8px;">📊 Factor Breakdown</strong>
        ${Object.entries(factors).map(([key, factor]) => {
          const pct = (factor.score / factor.max) * 100;
          return `
          <div style="margin-bottom:10px;">
            <div style="display:flex;justify-content:space-between;font-size:0.78rem;margin-bottom:2px;">
              <span>${factor.label}</span>
              <span style="font-weight:700;">${factor.score}/${factor.max}</span>
            </div>
            <div style="height:6px;background:var(--bg-secondary);border-radius:3px;overflow:hidden;">
              <div style="height:100%;width:${pct}%;background:linear-gradient(90deg,var(--primary),var(--accent));border-radius:3px;"></div>
            </div>
          </div>
          `;
        }).join('')}
      </div>

      <div style="margin:16px 0;">
        <strong style="display:block;font-size:0.85rem;margin-bottom:8px;">⚡ Key Signals</strong>
        <ul style="list-style:none;padding:0;">
          ${signals.map(sig => `<li style="font-size:0.78rem;padding:4px 0;color:var(--text-secondary);">• ${sig}</li>`).join('')}
        </ul>
      </div>
    </div>
    `;

    return html;
  }

  _getSessionTime() {
    const now = new Date();
    const istTime = new Date(now.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }));
    return istTime.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  // ── Technical Indicator — delegated to shared TI module ──
  _computeRSI(closes, period = 14) {
    return TI.computeRSI(closes, period);
  }

  // Retrieve analysis history for a symbol
  getAnalysisHistory(symbol) {
    return this.analysisHistory.filter(a => a.symbol === symbol);
  }

  // Clear history
  clearHistory() {
    this.analysisHistory = [];
  }
}

// Instantiate globally
const fnoSessionAnalyzer = new FNOSessionAnalyzer();
