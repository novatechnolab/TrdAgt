/**
 * FNO Session Analyzer — Unit Tests
 * Run with: node test_fno_session.js
 */

// ── Mock TI module (delegated in production) ──
// Assign to globalThis so vm.runInThisContext() can access it
globalThis.TI = {
  computeRSI(closes, period = 14) {
    if (!closes || closes.length < period + 1) return 50;
    let gains = 0, losses = 0;
    for (let i = closes.length - period; i < closes.length; i++) {
      const diff = closes[i] - closes[i - 1];
      if (diff > 0) gains += diff; else losses -= diff;
    }
    const avgGain = gains / period;
    const avgLoss = losses / period;
    if (avgLoss === 0) return 100;
    const rs = avgGain / avgLoss;
    return 100 - (100 / (1 + rs));
  },
  computeEMA(data, period) { return data; },
  computeMACD(closes) { return { macd: 0, signal: 0, histogram: 0 }; },
  computeADX(h, l, c, p = 14) { return 20; },
  computeATR(h, l, c, p = 14) { return 5; },
  computeVolumeRatio(v, p = 20) { return 1.2; },
  computeBollingerWidth(c, p = 20) { return 0; },
  computeBollingerBands(c, p = 20, m = 2) { return {}; },
  computeVWAP(ohlcv) { return 0; },
  computeSupertrend(ohlcv, p = 10, m = 3) { return {}; }
};

// Load the analyzer — use vm.runInThisContext so class/const declarations
// are visible in the top-level scope (plain eval treats them as block-scoped)
const vm = require('vm');
vm.runInThisContext(require('fs').readFileSync('./app/js/fno-session-analyzer.js', 'utf8'));

const analyzer = new FNOSessionAnalyzer();
let passed = 0, failed = 0;

function assert(condition, message) {
  if (condition) {
    console.log(`  ✅ ${message}`);
    passed++;
  } else {
    console.log(`  ❌ FAIL: ${message}`);
    failed++;
  }
}

// ── Test 1: getCurrentSession ──
console.log('\n📋 Test 1: getCurrentSession()');
const session = analyzer.getCurrentSession();
const now = new Date();
const istTime = new Date(now.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' }));
const hours = istTime.getHours();
const minutes = istTime.getMinutes();
const totalMinutes = hours * 60 + minutes;

console.log(`  IST Time: ${hours}:${String(minutes).padStart(2,'0')} (totalMin: ${totalMinutes})`);
console.log(`  Detected Session: ${session}`);

if (totalMinutes >= 930 || totalMinutes < 540) {
  assert(session === 'premarket', `Should be premarket at ${hours}:${String(minutes).padStart(2,'0')} IST`);
} else if (totalMinutes >= 540 && totalMinutes <= 555) {
  assert(session === 'opening', `Should be opening at ${hours}:${String(minutes).padStart(2,'0')} IST`);
} else if (totalMinutes > 555 && totalMinutes < 930) {
  assert(session === 'live', `Should be live at ${hours}:${String(minutes).padStart(2,'0')} IST`);
}

// ── Test 2: analyzePremarket ──
console.log('\n📋 Test 2: analyzePremarket()');
const mockPremarket = {
  symbol: 'RELIANCE',
  gapPct: 2.0,
  oiChangePercent: 15,
  hasPositiveNews: true,
  prevClose: 2500,
  preOpenPrice: 2540,
  fiiFlow: 600,
  closes: Array(30).fill(0).map((_, i) => 2450 + i * 2),
  highs: Array(30).fill(0).map((_, i) => 2460 + i * 2),
  lows: Array(30).fill(0).map((_, i) => 2440 + i * 2),
  volumes: Array(30).fill(1500000),
};

const preResult = analyzer.analyzePremarket(mockPremarket);
console.log(`  Score: ${preResult.score}, Direction: ${preResult.direction}, Confidence: ${preResult.confidence}`);
console.log(`  Recommendation: ${preResult.recommendation}`);
console.log(`  Factors: ${JSON.stringify(Object.entries(preResult.factors).map(([k,v]) => `${k}:${v.score}/${v.max}`))}`);
console.log(`  Signals: ${preResult.signals.join(', ')}`);

assert(typeof preResult.score === 'number', 'Premarket returns numeric score');
assert(preResult.score >= 0 && preResult.score <= 100, `Score ${preResult.score} is within 0-100`);
assert(['BULLISH', 'BEARISH', 'NEUTRAL'].includes(preResult.direction), `Direction is valid: ${preResult.direction}`);
assert(preResult.factors && Object.keys(preResult.factors).length === 5, 'Has 5 factors');
assert(preResult.strategy && preResult.strategy.setup, 'Has strategy with setup');
assert(preResult.signals.length > 0, 'Has signals');

// ── Test 3: analyzeOpening ──
console.log('\n📋 Test 3: analyzeOpening()');
const mockOpening = {
  symbol: 'TCS',
  ltp: 3500,
  open: 3480,
  openingHigh: 3520,
  openingLow: 3470,
  prevClose: 3450,
  volume15min: 150000,
  avgDailyVol: 5000000,
  atmCallOI: 50000,
  atmPutOI: 60000,
  pcrChange: 12,
  atmIV: 28,
  prevCloseIV: 25,
  closes: Array(30).fill(0).map((_, i) => 3400 + i * 3),
  highs: Array(30).fill(0).map((_, i) => 3410 + i * 3),
  lows: Array(30).fill(0).map((_, i) => 3390 + i * 3),
  volumes: Array(30).fill(2000000),
};

const openResult = analyzer.analyzeOpening(mockOpening);
console.log(`  Score: ${openResult.score}, Direction: ${openResult.direction}, Confidence: ${openResult.confidence}`);
console.log(`  Recommendation: ${openResult.recommendation}`);
console.log(`  Opening Metrics: gap=${openResult.opening?.gap?.toFixed(2)}, range=${openResult.opening?.range?.toFixed(2)}, vol=${openResult.opening?.volume?.toFixed(2)}`);

assert(typeof openResult.score === 'number', 'Opening returns numeric score');
assert(openResult.score >= 0 && openResult.score <= 100, `Score ${openResult.score} is within 0-100`);
assert(openResult.factors && Object.keys(openResult.factors).length === 5, 'Has 5 factors');
assert(openResult.opening, 'Has opening metrics');
assert(openResult.strategy, 'Has strategy');

// ── Test 4: analyzeLive ──
console.log('\n📋 Test 4: analyzeLive()');
const mockLive = {
  symbol: 'HDFCBANK',
  ltp: 1650,
  open: 1630,
  high: 1660,
  low: 1620,
  close: 1650,
  prevClose: 1625,
  totalCallOI: 1000000,
  totalPutOI: 800000,
  pcrChangeFromOpen: -5,
  maxPain: 1640,
  volume: 8000000,
  avgDailyVol: 5000000,
  atmIV: 22,
  openIV: 25,
  rsi: 62,
  macdHistogram: 1.5,
  closes: Array(30).fill(0).map((_, i) => 1600 + i * 2),
  highs: Array(30).fill(0).map((_, i) => 1610 + i * 2),
  lows: Array(30).fill(0).map((_, i) => 1590 + i * 2),
  volumes: Array(30).fill(3000000),
};

const liveResult = analyzer.analyzeLive(mockLive);
console.log(`  Score: ${liveResult.score}, Direction: ${liveResult.direction}, Confidence: ${liveResult.confidence}`);
console.log(`  Recommendation: ${liveResult.recommendation}`);
console.log(`  Live Metrics: dayChange=${liveResult.liveMetrics?.dayChange?.toFixed(2)}, vol=${liveResult.liveMetrics?.volume?.toFixed(2)}`);

assert(typeof liveResult.score === 'number', 'Live returns numeric score');
assert(liveResult.score >= 0 && liveResult.score <= 100, `Score ${liveResult.score} is within 0-100`);
assert(liveResult.factors && Object.keys(liveResult.factors).length === 5, 'Has 5 factors');
assert(liveResult.liveMetrics, 'Has live metrics');
assert(liveResult.strategy, 'Has strategy');

// ── Test 5: analyzeStockForSession (orchestrator) ──
console.log('\n📋 Test 5: analyzeStockForSession() orchestrator');
const fullResult = analyzer.analyzeStockForSession(mockPremarket);
// analyzeStockForSession is async but in this test env (no await), it runs sync
// since analyzePremarket is synchronous. Let's handle both.
if (fullResult && fullResult.then) {
  fullResult.then(r => {
    console.log(`  Session routed to: ${r.session}`);
    assert(r.session, 'Has session property from orchestrator');
    assert(r.timestamp, 'Has timestamp');
    assert(r.sessionTime, 'Has sessionTime');
    printSummary();
  });
} else {
  console.log(`  Session routed to: ${fullResult.session}`);
  assert(fullResult.session, 'Has session property from orchestrator');
  assert(fullResult.timestamp, 'Has timestamp');
  assert(fullResult.sessionTime, 'Has sessionTime');
}

// ── Test 6: renderAnalysisReport ──
console.log('\n📋 Test 6: renderAnalysisReport()');
const report = analyzer.renderAnalysisReport(preResult);
assert(typeof report === 'string', 'Returns HTML string');
assert(report.includes('session-analysis-report'), 'Contains report wrapper class');
assert(report.includes('score-badge'), 'Contains score badge');
assert(report.includes('Factor Breakdown'), 'Contains factor breakdown');
assert(report.includes('Key Signals'), 'Contains key signals section');
assert(report.includes('Recommendation'), 'Contains recommendation section');

// ── Test 7: Edge cases ──
console.log('\n📋 Test 7: Edge cases');
const emptyStock = {
  symbol: 'EMPTY',
  closes: Array(30).fill(100),
  highs: Array(30).fill(100),
  lows: Array(30).fill(100),
  volumes: Array(30).fill(0),
};
const emptyPremarket = analyzer.analyzePremarket(emptyStock);
assert(typeof emptyPremarket.score === 'number', 'Empty stock premarket returns score');
assert(!isNaN(emptyPremarket.score), 'Score is not NaN for empty stock');
assert(emptyPremarket.direction, 'Has direction for empty stock');

const emptyOpening = analyzer.analyzeOpening(emptyStock);
assert(typeof emptyOpening.score === 'number', 'Empty stock opening returns score');
assert(!isNaN(emptyOpening.score), 'Score is not NaN for empty opening');

const emptyLive = analyzer.analyzeLive(emptyStock);
assert(typeof emptyLive.score === 'number', 'Empty stock live returns score');
assert(!isNaN(emptyLive.score), 'Score is not NaN for empty live');

// ── Test 8: History tracking ──
console.log('\n📋 Test 8: History tracking');
analyzer.clearHistory();
assert(analyzer.analysisHistory.length === 0, 'History cleared');

// Run a couple analyses to populate history
analyzer.analyzeStockForSession(mockPremarket);
analyzer.analyzeStockForSession(mockLive);
assert(analyzer.analysisHistory.length >= 1, `History has ${analyzer.analysisHistory.length} entries`);

const relHistory = analyzer.getAnalysisHistory('RELIANCE');
assert(relHistory.length >= 1, `RELIANCE has ${relHistory.length} history entries`);

// ── Summary ──
function printSummary() {
  console.log(`\n${'═'.repeat(50)}`);
  console.log(`  Results: ${passed} passed, ${failed} failed`);
  console.log(`${'═'.repeat(50)}`);
  if (failed > 0) process.exit(1);
}

printSummary();
