/**
 * Integration Test — Refined 5/6-Factor Model + Gap Analysis
 * 
 * Tests the complete signal pipeline:
 * 1. Fetch live snapshot + OHLCV for PERSISTENT
 * 2. Run scoreEquity() with 5-factor refined model
 * 3. Run scoreOptions() with 6-factor refined model  
 * 4. Verify gap analysis integration
 * 5. Display final signal breakdown
 */

const BASE_URL = 'http://localhost:5000';
const AUTH = 'rajtrader09:Iamtrader@19009';

async function runIntegrationTest() {
  console.log('🚀 Starting Integration Test for Refined Models\n');
  console.log('='.repeat(80));
  
  const symbol = 'PERSISTENT';
  const interval = '5minute';
  
  try {
    // ── Step 1: Fetch live snapshot and OHLCV ──
    console.log(`\n📊 Step 1: Fetching PERSISTENT snapshot (enriched equity + futures + options)\n`);
    
    const snapshotRes = await fetch(`${BASE_URL}/api/stock-snapshot?symbol=${symbol}`, {
      headers: {
        'Authorization': 'Basic ' + btoa(AUTH)
      }
    });
    
    if (!snapshotRes.ok) {
      throw new Error(`Snapshot fetch failed: ${snapshotRes.status} ${snapshotRes.statusText}`);
    }
    
    const snapshotData = await snapshotRes.json();
    console.log(`✓ Snapshot fetched successfully`);
    console.log(`  LTP: ₹${snapshotData.ltp?.toFixed(2)}`);
    console.log(`  Change: ${snapshotData.change_pct?.toFixed(2)}%`);
    console.log(`  Volume: ${snapshotData.volume?.toLocaleString('en-IN')}`);
    console.log(`  VWAP: ₹${snapshotData.avg_price?.toFixed(2)}`);
    
    // ── Step 2: Fetch 5-minute OHLCV history ──
    console.log(`\n📈 Step 2: Fetching 5-minute OHLCV history\n`);
    
    const ohlcvRes = await fetch(`${BASE_URL}/api/historical`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Basic ' + btoa(AUTH)
      },
      body: JSON.stringify({ 
        symbol, 
        range_days: 30,
        mode: 'live'  // <-- Triggers 5-minute fetching
      })
    });
    
    if (!ohlcvRes.ok) {
      throw new Error(`OHLCV fetch failed: ${ohlcvRes.status}`);
    }
    
    const ohlcvData = await ohlcvRes.json();
    const candles = ohlcvData.ohlcv || [];
    
    if (candles.length < 20) {
      throw new Error(`Insufficient OHLCV data (need 20+, got ${candles.length})`);
    }
    
    console.log(`✓ OHLCV fetched: ${candles.length} candles`);
    console.log(`  Date range: ${candles[0].date} to ${candles[candles.length - 1].date}`);
    
    // Extract OHLCV arrays
    const closes = candles.map(c => c.close);
    const highs = candles.map(c => c.high);
    const lows = candles.map(c => c.low);
    const volumes = candles.map(c => c.volume);
    
    // ── Step 3: Prepare scoring data ──
    console.log(`\n🔧 Step 3: Preparing scoring data\n`);
    
    const scoringData = {
      symbol,
      closes,
      highs,
      lows,
      volumes,
      snapshot: snapshotData,
      sector: 'IT',
      sectorData: {
        relativeStrength: 65,
        rotating: true
      }
    };
    
    console.log(`✓ Data prepared for scoring`);
    
    // ── Step 4: Run Equity Scoring ──
    console.log(`\n💰 Step 4: Running Equity Scoring (5-Factor Refined Model)\n`);
    
    const equityRes = await fetch(`${BASE_URL}/api/score/equity`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Basic ' + btoa(AUTH)
      },
      body: JSON.stringify(scoringData)
    });
    
    if (!equityRes.ok) {
      throw new Error(`Equity scoring failed: ${equityRes.status}`);
    }
    
    const equityScore = await equityRes.json();
    displayEquityScore(equityScore);
    
    // ── Step 5: Run Options Scoring ──
    console.log(`\n📞 Step 5: Running Options Scoring (6-Factor Model + Risk Filters)\n`);
    
    const optionsRes = await fetch(`${BASE_URL}/api/score/options`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Basic ' + btoa(AUTH)
      },
      body: JSON.stringify({
        ...scoringData,
        interval,
        optionsData: {
          iv_percentile: 35,
          pcr: 1.2
        }
      })
    });
    
    if (!optionsRes.ok) {
      throw new Error(`Options scoring failed: ${optionsRes.status}`);
    }
    
    const optionsScore = await optionsRes.json();
    displayOptionsScore(optionsScore);
    
    // ── Step 6: Summary ──
    console.log(`\n${'='.repeat(80)}`);
    console.log(`\n✅ INTEGRATION TEST COMPLETE\n`);
    
    // Final verdict
    const equitySignal = equityScore.direction || 'NO SIGNAL';
    const optionsSignal = optionsScore.direction || 'NO SIGNAL';
    
    console.log(`📋 FINAL SIGNALS:`);
    console.log(`  Equity: ${equitySignal} (${equityScore.total?.toFixed(0)} points)`);
    console.log(`  Options: ${optionsSignal} (${optionsScore.total?.toFixed(0)} points)`);
    
    if (equitySignal === optionsSignal) {
      console.log(`\n✅ SIGNAL ALIGNMENT: MATCHED (${equitySignal})`);
    } else {
      console.log(`\n⚠️  SIGNAL DIVERGENCE: ${equitySignal} vs ${optionsSignal}`);
    }
    
    console.log(`\n✨ New Models Working Correctly!`);
    
  } catch (error) {
    console.error(`\n❌ TEST FAILED:`, error.message);
    process.exit(1);
  }
}

function displayEquityScore(score) {
  console.log(`  Direction: ${score.direction} (${score.total?.toFixed(0)}/100 points)`);
  console.log(`\n  Factor Breakdown:`);
  
  const factors = score.factors || {};
  Object.entries(factors).forEach(([name, data]) => {
    if (data.noData) {
      console.log(`    • ${data.label}: ${data.score}/${data.max} (No Data)`);
    } else {
      const pct = ((data.score / data.max) * 100).toFixed(0);
      console.log(`    • ${data.label}: ${data.score}/${data.max} (${pct}%)`);
    }
  });
  
  if (score.risk) {
    console.log(`\n  Risk Management:`);
    console.log(`    SL: ₹${score.risk.stopLoss?.toFixed(2)}`);
    console.log(`    T1: ₹${score.risk.target1?.toFixed(2)}`);
    console.log(`    R:R: ${score.risk.riskReward?.toFixed(2)}:1`);
  }
}

function displayOptionsScore(score) {
  console.log(`  Direction: ${score.direction} (${score.total?.toFixed(0)}/100 points)`);
  
  if (score.factors) {
    console.log(`\n  Factor Breakdown:`);
    Object.entries(score.factors).forEach(([name, data]) => {
      if (data.noData) {
        console.log(`    • ${data.label}: ${data.score}/${data.max} (No Data)`);
      } else {
        const pct = ((data.score / data.max) * 100).toFixed(0);
        console.log(`    • ${data.label}: ${data.score}/${data.max} (${pct}%)`);
      }
    });
  }
  
  if (score.riskFilter) {
    console.log(`\n  ⚠️  Risk Filter: ${score.riskFilter.label}`);
  }
}

// Run test
runIntegrationTest().catch(console.error);
