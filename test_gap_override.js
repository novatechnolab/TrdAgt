/**
 * Test script for Gap Override Integration — Refined test data
 */
const fs = require('fs');
const path = require('path');

const tiCode = fs.readFileSync(path.join(__dirname, 'app/js/technical-indicators.js'), 'utf8');
const gapCode = fs.readFileSync(path.join(__dirname, 'app/js/gap-analysis-engine.js'), 'utf8');
const evCode = fs.readFileSync(path.join(__dirname, 'app/js/entry-validator.js'), 'utf8');

global.window = global;
global.globalThis = global;

eval(tiCode);
eval(gapCode);
eval(evCode);

const ev = window.entryValidator;

/**
 * Build realistic gap scenario OHLCV:
 * - 39 candles of normal trending action
 * - 1 final candle that opens with a gap and continues in the gap direction
 *   (wick stays well above/below gap open to avoid counter-move invalidation)
 */
function makeGapScenario(basePrice, gapPct, direction) {
  const candles = [];
  let p = basePrice;
  const isUp = direction === 'CALL';

  // Build 39 candles of normal action
  for (let i = 0; i < 39; i++) {
    const bodySize = p * 0.002;
    let o, c;
    if (isUp) {
      o = p;
      c = p + bodySize;
    } else {
      o = p;
      c = p - bodySize;
    }
    candles.push({
      date: new Date(2026, 3, 15, 9, 15 + i * 5).toISOString(),
      open: +o.toFixed(2),
      high: +(Math.max(o, c) + p * 0.001).toFixed(2),
      low: +(Math.min(o, c) - p * 0.001).toFixed(2),
      close: +c.toFixed(2),
      volume: 100000 + Math.floor(Math.random() * 20000),
    });
    if (isUp) p = c + bodySize * 0.1;
    else p = c - bodySize * 0.1;
  }

  // Last candle: GAP open + continuation
  const prevClose = candles[candles.length - 1].close;
  const gapOpen = +(prevClose * (1 + gapPct / 100)).toFixed(2);

  if (gapPct > 0) {
    // Gap UP: open above prev close, price continues higher
    // Wick low stays at or near gap open (no counter-move below gap open)
    const closeAbove = +(gapOpen + gapOpen * 0.003).toFixed(2);
    candles.push({
      date: new Date(2026, 3, 15, 9, 15 + 39 * 5).toISOString(),
      open: gapOpen,
      high: +(closeAbove + gapOpen * 0.001).toFixed(2),
      low: +(gapOpen - gapOpen * 0.001).toFixed(2),  // Wick stays near gap open
      close: closeAbove,
      volume: 250000,
    });
  } else {
    // Gap DOWN: open below prev close, price continues lower
    const closeBelow = +(gapOpen - Math.abs(gapOpen * 0.003)).toFixed(2);
    candles.push({
      date: new Date(2026, 3, 15, 9, 15 + 39 * 5).toISOString(),
      open: gapOpen,
      high: +(gapOpen + Math.abs(gapOpen * 0.001)).toFixed(2),  // Wick stays near gap open
      low: +(closeBelow - Math.abs(gapOpen * 0.001)).toFixed(2),
      close: closeBelow,
      volume: 250000,
    });
  }
  return candles;
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 1: CALL + Gap Up 2.5% → Override active');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, 2.5, 'CALL');
  const result = ev.validate({ direction: 'CALL', price: candles[candles.length - 1].close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.gapPercent:', result.gapOverride?.gapPercent?.toFixed(2) + '%');
  console.log('  gapOverride.gapTier:', result.gapOverride?.gapTier);
  console.log('  riskLevel:', result.riskLevel);
  console.log('  stopLoss:', result.stopLoss);
  console.log('  confidence:', result.confidence);
  if (result.gapOverride?.invalidated) {
    console.log('  ⚠ INVALIDATED:', result.gapOverride.invalidationReason);
  }
  result.reasoning.filter(r => r.toLowerCase().includes('gap')).forEach(r => console.log('  >', r));
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 2: PUT + Gap Down 1.8% → Override active');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, -1.8, 'PUT');
  const result = ev.validate({ direction: 'PUT', price: candles[candles.length - 1].close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.gapPercent:', result.gapOverride?.gapPercent?.toFixed(2) + '%');
  console.log('  gapOverride.gapTier:', result.gapOverride?.gapTier);
  console.log('  riskLevel:', result.riskLevel);
  console.log('  stopLoss:', result.stopLoss);
  if (result.gapOverride?.invalidated) {
    console.log('  ⚠ INVALIDATED:', result.gapOverride.invalidationReason);
  }
  result.reasoning.filter(r => r.toLowerCase().includes('gap')).forEach(r => console.log('  >', r));
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 3: Direction mismatch → Invalidated');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, -2.0, 'PUT');
  const result = ev.validate({ direction: 'CALL', price: candles[candles.length - 1].close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.invalidated:', result.gapOverride?.invalidated);
  console.log('  invalidationReason:', result.gapOverride?.invalidationReason);
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 4: Small gap 0.5% → Tier 1, no override');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, 0.5, 'CALL');
  const result = ev.validate({ direction: 'CALL', price: candles[candles.length - 1].close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.gapTier:', result.gapOverride?.gapTier);
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 5: No gap → Normal flow');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, 0.1, 'CALL');
  const result = ev.validate({ direction: 'CALL', price: candles[candles.length - 1].close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.gapPercent:', result.gapOverride?.gapPercent?.toFixed(2) + '%');
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 6: Gap filled → Invalidated');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, 2.5, 'CALL');
  // Override the last candle to simulate gap fill (close below prev close)
  const prevClose = candles[candles.length - 2].close;
  const lastCandle = candles[candles.length - 1];
  lastCandle.close = +(prevClose - 1).toFixed(2);
  lastCandle.low = +(prevClose - 3).toFixed(2);
  
  const result = ev.validate({ direction: 'CALL', price: lastCandle.close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.invalidated:', result.gapOverride?.invalidated);
  console.log('  invalidationReason:', result.gapOverride?.invalidationReason);
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 7: Gap override SL below gap open (CALL)');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, 2.0, 'CALL');
  const result = ev.validate({ direction: 'CALL', price: candles[candles.length - 1].close, ohlcv: candles });
  
  if (result.gapOverride?.isOverride) {
    console.log('  ✓ Gap override ACTIVE');
    console.log('  gapOpenPrice:', result.gapOverride.gapOpenPrice?.toFixed(2));
    console.log('  stopLoss:', result.stopLoss);
    console.log('  entryPrice:', result.entryPrice?.toFixed(2));
    console.log('  SL < gapOpen:', result.stopLoss < result.gapOverride.gapOpenPrice);
    console.log('  SL < entryPrice:', result.stopLoss < result.entryPrice);
  } else {
    console.log('  Override not active');
    if (result.gapOverride?.invalidated) {
      console.log('  Reason:', result.gapOverride.invalidationReason);
    }
  }
  console.log('');
}

console.log('══════════════════════════════════════════════════');
console.log('  Test 8: Large gap 5% → Tier 3, override active');
console.log('══════════════════════════════════════════════════');
{
  const candles = makeGapScenario(1000, 5.0, 'CALL');
  const result = ev.validate({ direction: 'CALL', price: candles[candles.length - 1].close, ohlcv: candles });
  console.log('  gapOverride.isOverride:', result.gapOverride?.isOverride);
  console.log('  gapOverride.gapTier:', result.gapOverride?.gapTier);
  console.log('  gapOverride.gapPercent:', result.gapOverride?.gapPercent?.toFixed(2) + '%');
  console.log('  riskLevel:', result.riskLevel);
  if (result.gapOverride?.invalidated) {
    console.log('  ⚠ INVALIDATED:', result.gapOverride.invalidationReason);
  }
  console.log('');
}

console.log('✅ All tests completed.');
