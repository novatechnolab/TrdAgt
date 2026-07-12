const fs = require('fs');
global.document = {
  getElementById: () => ({ style: {}, addEventListener: () => {} }),
  querySelectorAll: () => [],
  createElement: () => ({}),
  dispatchEvent: () => {}
};
global.window = {
  document: global.document,
  location: { origin: 'http://localhost' },
  gapAnalysisEngine: { computeGapScore: () => ({ gapTier: 0 }) }
};
const scoringEngineCode = fs.readFileSync('/home/rajk/Downloads/TradeSignal/app/js/scoring-engine.js', 'utf8');
eval(scoringEngineCode);

const mockDataNoSnapshot = {
  closes: Array(25).fill(100).map((v, i) => v + i),
  highs: Array(25).fill(105).map((v, i) => v + i),
  lows: Array(25).fill(95).map((v, i) => v + i),
  volumes: Array(25).fill(1000),
  snapshot: {} // no live data
};

const resultEmpty = window.scoringEngine.scoreOptions(mockDataNoSnapshot);
console.log("Empty Snapshot total score:", resultEmpty.total);
