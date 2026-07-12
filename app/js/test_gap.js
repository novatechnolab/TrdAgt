const fs = require('fs');
const path = require('path');

// Mock window object for browser-based script
global.window = {};

// Load the gap analysis engine
const engineCode = fs.readFileSync(path.join(__dirname, 'gap-analysis-engine.js'), 'utf8');
eval(engineCode);

const engine = window.gapAnalysisEngine;
let passed = 0;
let total = 0;

function assertEqual(testName, actual, expected) {
    total++;
    // Use JSON.stringify for deep or shallow comparison depending on type
    if (actual === expected) {
        console.log(`✅ PASS: ${testName}`);
        passed++;
    } else {
        console.error(`❌ FAIL: ${testName} (Expected: ${expected}, Got: ${actual})`);
    }
}

console.log("Starting Automated Verification Plan for GapAnalysisEngine...\n");

// Test 1: Tier 0 Gap (No action)
const tier0Data = {
    closes: [100, 100.1], // gap is 0.1% (<0.25%)
    snapshot: { open: 100.1, india_vix: 15 }
};
const res1 = engine.computeGapScore(tier0Data);
assertEqual("Tier 0 Gap should return gapTier 0", res1.gapTier, 0);
assertEqual("Tier 0 Gap should return score 0", res1.score, 0);

// Test 2: India VIX Hard Override (>30)
const vixOverrideData = {
    closes: [100, 102], // 2% gap up (Tier 2)
    snapshot: { open: 102, india_vix: 35 } // VIX > 30 triggers WATCH
};
const res2 = engine.computeGapScore(vixOverrideData);
assertEqual("VIX > 30 should force gapTier 2", res2.gapTier, 2);
assertEqual("VIX > 30 should override score to 0", res2.score, 0);
assertEqual("VIX > 30 should trigger 'WATCH' override", res2.override, 'WATCH');

// Test 3: Earnings Miss Gap Up Fade override
const earningsBadData = {
    closes: [100, 104], // 4% gap up
    snapshot: { open: 104, india_vix: 15, earnings_surprise_pct: -15 }
};
const res3 = engine.computeGapScore(earningsBadData);
assertEqual("Earnings Miss Gap Up should trigger 'STRONG FADE'", res3.override, 'STRONG FADE');

// Test 4: Gift Nifty Macro Divergence
const goodMacroData = {
    closes: [100, 102], // 2% gap up, base multiplier = 1.0
    snapshot: { open: 102, india_vix: 15, gift_nifty_premium: 0.8, pre_open_buy_qty: 1000, pre_open_sell_qty: 1000 }
};
const res4 = engine.computeGapScore(goodMacroData);
// pre-open imbalance = 0, gift nifty = +0.8 (>0.5) so +8 points. 
// adx default = 20, rsi default = 50 -> common gap.
// so final score should be 8 * 1.0 multiplier = 8
assertEqual("Gift Nifty Premium > 0.5 on Gap Up adds 8 to score", res4.score, 8);

console.log(`\nVerification Results: ${passed}/${total} passed.`);
if (passed !== total) process.exit(1);
