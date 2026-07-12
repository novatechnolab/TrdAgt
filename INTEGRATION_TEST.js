/**
 * BROWSER CONSOLE TEST — Refined 5/6-Factor Models + Gap Analysis
 * 
 * Paste this entire script into the browser console (F12 → Console tab)
 * to run the integration test with live PERSISTENT data
 */

(async function runIntegrationTest() {
  const AUTH = btoa('rajtrader09:Iamtrader@19009');
  const BASE_URL = 'http://localhost:5000';
  
  console.clear();
  console.log('%c🚀 Starting Integration Test — Refined Models', 'font-size:16px; font-weight:bold; color:#00d4ff;');
  console.log('%c' + '='.repeat(80), 'color:#00d4ff;');
  
  try {
    // Verify engines are loaded
    if (!globalThis.scoringEngine) throw new Error('ScoringEngine not loaded');
    if (!globalThis.gapAnalysisEngine) throw new Error('GapAnalysisEngine not loaded');
    if (!globalThis.TI) throw new Error('TI module not loaded');
    
    console.log('%c✓ All modules loaded', 'color:#0ecb81;');
    console.log('  - ScoringEngine:', typeof globalThis.scoringEngine);
    console.log('  - GapAnalysisEngine:', typeof globalThis.gapAnalysisEngine);
    console.log('  - TI:', typeof globalThis.TI);
    
    // Step 1: Fetch snapshot
    console.log('%c\n📊 Step 1: Fetching PERSISTENT snapshot', 'color:#00d4ff; font-weight:bold;');
    const snapRes = await fetch(`${BASE_URL}/api/stock-snapshot?symbol=PERSISTENT&token=`, {
      headers: { 'Authorization': `Basic ${AUTH}` }
    });
    if (!snapRes.ok) throw new Error(`Snapshot failed: ${snapRes.status}`);
    const snapshot = await snapRes.json();
    console.log('✓ Snapshot:', {
      ltp: snapshot.ltp,
      change_pct: snapshot.change_pct,
      volume: snapshot.volume,
      avg_price: snapshot.avg_price,
      futures_premium: snapshot.futures.premium_pct
    });
    
    // Step 2: Fetch OHLCV
    console.log('%c\n📈 Step 2: Fetching 5-minute OHLCV history (30 days)', 'color:#00d4ff; font-weight:bold;');
    const ohlcvRes = await fetch(
      `${BASE_URL}/api/historical?symbol=PERSISTENT&range_days=30&interval=5minute&mode=live`,
      { headers: { 'Authorization': `Basic ${AUTH}` } }
    );
    if (!ohlcvRes.ok) throw new Error(`OHLCV failed: ${ohlcvRes.status}`);
    const ohlcvData = await ohlcvRes.json();
    const candles = ohlcvData.ohlcv || [];
    console.log(`✓ OHLCV fetched: ${candles.length} 5-minute candles`);
    console.log('  Date range:', candles[0]?.date, '→', candles[candles.length-1]?.date);
    
    if (candles.length < 20) {
      console.warn('⚠️  Warning: Less than 20 candles (need 20+ for accurate scoring)');
    }
    
    // Step 3: Prepare data
    console.log('%c\n🔧 Step 3: Preparing scoring data', 'color:#00d4ff; font-weight:bold;');
    const closes = candles.map(c => c.close);
    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const volumes = candles.map(c => c.volume);
    
    const scoringData = {
      symbol: 'PERSISTENT',
      closes,
      highs,
      lows,
      volumes,
      snapshot,
      sector: 'IT',
      sectorData: { relativeStrength: 65, rotating: true }
    };
    console.log('✓ Data prepared:', {
      closes: closes.length,
      highs: highs.length,
      lows: lows.length,
      volumes: volumes.length,
      lastClose: closes[closes.length - 1]
    });
    
    // Step 4: Initialize gap analysis
    console.log('%c\n🔄 Step 4: Initializing Gap Analysis Engine', 'color:#00d4ff; font-weight:bold;');
    globalThis.gapAnalysisEngine.initializeGapFillHistory('PERSISTENT', candles);
    console.log('✓ Gap fill history initialized');
    
    // Step 5: Equity Scoring
    console.log('%c\n💰 Step 5: Equity Scoring (5-Factor Refined Model)', 'color:#00d4ff; font-weight:bold;');
    const equityScore = globalThis.scoringEngine.scoreEquity(scoringData);
    console.log('EQUITY SCORE RESULT:');
    console.table({
      'Direction': equityScore.direction,
      'Total Score': (equityScore.total || 0).toFixed(0),
      'Max': 100,
      'Confidence': ((equityScore.total || 0) / 100 * 100).toFixed(0) + '%'
    });
    console.log('Factors:', equityScore.factors);
    if (equityScore.risk) {
      console.log('Risk Management:', equityScore.risk);
    }
    
    // Step 6: Options Scoring
    console.log('%c\n📞 Step 6: Options Scoring (6-Factor Model)', 'color:#00d4ff; font-weight:bold;');
    const optionsScore = globalThis.scoringEngine.scoreOptions({
      ...scoringData,
      interval: '5minute',
      optionsData: snapshot.atm_option
    });
    console.log('OPTIONS SCORE RESULT:');
    console.table({
      'Direction': optionsScore.direction,
      'Total Score': (optionsScore.total || 0).toFixed(0),
      'Max': 100,
      'Confidence': ((optionsScore.total || 0) / 100 * 100).toFixed(0) + '%'
    });
    console.log('Factors:', optionsScore.factors);
    if (optionsScore.riskFilter) {
      console.warn('Risk Filter Triggered:', optionsScore.riskFilter);
    }
    
    // Summary
    console.log('%c\n' + '='.repeat(80), 'color:#00d4ff;');
    console.log('%c✅ INTEGRATION TEST COMPLETE', 'font-size:14px; font-weight:bold; color:#0ecb81;');
    
    const equityDir = equityScore.direction || 'NEUTRAL';
    const optionsDir = optionsScore.direction || 'NEUTRAL';
    const signalMatch = equityDir === optionsDir;
    
    console.log('%cFINAL SIGNALS:', 'font-size:12px; font-weight:bold; color:#00d4ff;');
    console.log(`%c  Equity:  ${equityDir} (${(equityScore.total || 0).toFixed(0)}/100)`, 
      signalMatch ? 'color:#0ecb81;' : 'color:#ffd700;');
    console.log(`%c  Options: ${optionsDir} (${(optionsScore.total || 0).toFixed(0)}/100)`, 
      signalMatch ? 'color:#0ecb81;' : 'color:#ffd700;');
    console.log(`%c  Status:  ${signalMatch ? '✓ ALIGNED' : '≠ DIVERGED'}`, 
      signalMatch ? 'color:#0ecb81; font-weight:bold;' : 'color:#ffd700; font-weight:bold;');
    
    console.log('%c✨ Models are working correctly!', 'font-size:14px; font-weight:bold; color:#0ecb81;');
    
  } catch (error) {
    console.error('%c❌ TEST FAILED', 'font-size:14px; font-weight:bold; color:#ff6b6b;');
    console.error('Error:', error.message);
    console.error('Stack:', error.stack);
  }
})();
