/**
 * OHLCV Factory — Deterministic Test Fixtures
 * Generates reproducible OHLCV candle arrays for each regression scenario.
 * NO Math.random() — all data is fully deterministic.
 *
 * Usage (browser or Node.js):
 *   const { SCENARIOS } = OHLCVFactory;
 */
const OHLCVFactory = (() => {

  // ── Helpers ──────────────────────────────────────────────────────────────
  const r = (n) => Math.round(n * 100) / 100;

  function makeDate(baseDate, minuteOffset) {
    const d = new Date(`${baseDate}T09:15:00+05:30`);
    d.setMinutes(d.getMinutes() + minuteOffset);
    return d.toISOString();
  }

  function buildCandles(baseDate, priceSteps, basePrice, baseVolume) {
    const candles = [];
    let prev = basePrice;
    priceSteps.forEach((step, i) => {
      const open  = r(prev);
      const close = r(prev + step);
      const bullish = close >= open;
      const wick   = Math.abs(step) * 0.15 + 1;
      candles.push({
        date:   makeDate(baseDate, i * 5),
        open,
        high:   r(Math.max(open, close) + wick),
        low:    r(Math.min(open, close) - wick),
        close,
        volume: Math.round(baseVolume + (i % 3) * 50000),
      });
      prev = close;
    });
    return candles;
  }

  // ── Price step sequences ─────────────────────────────────────────────────
  // Pattern guarantees: EMA9>EMA21 (CALL) or EMA9<EMA21 (PUT),
  // RSI in 45-65 range, MACD histogram direction correct.

  // Uptrend with pullbacks → RSI ~55, EMA9>EMA21
  const UPTREND_MIX = [
    +10,+8,-2,+12,+7,-3,+9,+11,-1,+13,
    +6,-2,+10,+8,-3,+11,+5,-2,+9,+7,
    +12,-2,+8,                          // swing HIGH forms here (index 22)
    -4,-3,                              // pullback (smaller bodies than trend)
    -2,                                 // still pulling back
    +6,+8,+5                            // last 3: current candles for entry
  ];

  // Downtrend with bounces → RSI ~45, EMA9<EMA21
  const DOWNTREND_MIX = [
    -10,-8,+2,-12,-7,+3,-9,-11,+1,-13,
    -6,+2,-10,-8,+3,-11,-5,+2,-9,-7,
    -12,+2,-8,                          // swing LOW forms here (index 22)
    +4,+3,                              // pullback up
    +2,
    -6,-8,-5
  ];

  // Consolidation + breakout (TYPE_2)
  const CONSOL_BREAKOUT_CALL = [
    +10,+8,-2,+12,+7,-3,+9,+11,-1,+13, // trend up
    +6,-2,+10,+8,-3,+9,+7,-2,+11,+5,   // more trend
    +1,+0.5,-0.5,+1,+0.5,-0.5,+1,      // consolidation (tiny bodies)
    +0.5,-0.3,                          // still consolidating
    +18                                 // BREAKOUT: close clears high + margin
  ];

  const CONSOL_BREAKDOWN_PUT = [
    -10,-8,+2,-12,-7,+3,-9,-11,+1,-13,
    -6,+2,-10,-8,+3,-9,-7,+2,-11,-5,
    -1,-0.5,+0.5,-1,-0.5,+0.5,-1,
    -0.5,+0.3,
    -18                                 // BREAKDOWN
  ];

  // EMA cross setup (TYPE_3): sideways then sharp cross
  const EMA_CROSS_CALL = [
    -3,-2,+1,-4,-2,+2,-3,-1,+1,-2,     // slight downtrend (EMA9<EMA21)
    -2,+1,-3,-1,+2,-2,-1,+1,-3,+1,
    -2,+1,-2,-1,+1,-3,                  // EMA9 < EMA21 maintained
    +25,+18,+12                         // sharp move: EMA9 crosses above EMA21
  ];

  // Full stack trend continuation (TYPE_4)
  const TREND_CONTINUATION = [
    +8,+7,+6,+8,+9,+7,+8,+6,+9,+8,
    +7,+8,+6,+9,+7,+8,+7,+6,+8,+9,
    +8,+7,+6,+8,+9,+7,+8,+9,+8,+9     // all higher closes, EMA9>21>50
  ];

  // Gap-up scenario (> 1% from prev close)
  const GAP_UP_SCENARIO = (() => {
    const pre = [];
    for (let i = 0; i < 28; i++) {
      pre.push(i % 3 === 2 ? -2 : +7);
    }
    pre.push(+250); // gap: open 1.5% above prior close (on base ~22000)
    pre.push(+10);  // continues bullish after gap
    return pre;
  })();

  // Invalid CALL: EMA9 < EMA21 (downtrend, wrong direction)
  const INVALID_CALL_STEPS = DOWNTREND_MIX;

  // ── Scenario Definitions ──────────────────────────────────────────────────
  const SCENARIOS = {

    /**
     * S1: CALL TYPE_1 — Pullback Entry (RELIANCE-like, ~₹2800)
     * Expected: setupType=TYPE_1, direction=CALL, isValid depends on full score
     */
    S1_CALL_TYPE1_PULLBACK: {
      id: 'S1_CALL_TYPE1_PULLBACK',
      description: 'CALL TYPE_1: Structural swing high with controlled pullback',
      params: {
        symbol: 'RELIANCE',
        direction: 'CALL',
        price: 2856,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 2856, vwap: 2832, volume: 1200000, symbol: 'RELIANCE' },
        optionsData: { oiChangePercent: 6, buildUp: 'long_buildup' },
      },
      ohlcv: buildCandles('2026-04-15', UPTREND_MIX, 2800, 400000),
    },

    /**
     * S2: PUT TYPE_2 — Momentum Breakdown (HDFCBANK-like, ~₹1750)
     */
    S2_PUT_TYPE2_BREAKDOWN: {
      id: 'S2_PUT_TYPE2_BREAKDOWN',
      description: 'PUT TYPE_2: Consolidation breakdown with margin',
      params: {
        symbol: 'HDFCBANK',
        direction: 'PUT',
        price: 1698,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 1698, vwap: 1735, volume: 950000, symbol: 'HDFCBANK' },
        optionsData: { oiChangePercent: 7, buildUp: 'short_buildup' },
      },
      ohlcv: buildCandles('2026-04-15', CONSOL_BREAKDOWN_PUT, 1750, 350000),
    },

    /**
     * S3: CALL TYPE_2 — Momentum Breakout (INFY-like, ~₹1820)
     */
    S3_CALL_TYPE2_BREAKOUT: {
      id: 'S3_CALL_TYPE2_BREAKOUT',
      description: 'CALL TYPE_2: Consolidation breakout with clear margin',
      params: {
        symbol: 'INFY',
        direction: 'CALL',
        price: 1868,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 1868, vwap: 1841, volume: 880000, symbol: 'INFY' },
        optionsData: { oiChangePercent: 4, buildUp: 'long_buildup' },
      },
      ohlcv: buildCandles('2026-04-15', CONSOL_BREAKOUT_CALL, 1820, 300000),
    },

    /**
     * S4: CALL TYPE_3 — EMA Cross (High Risk) (TCS-like, ~₹3900)
     */
    S4_CALL_TYPE3_EMA_CROSS: {
      id: 'S4_CALL_TYPE3_EMA_CROSS',
      description: 'CALL TYPE_3: EMA 9 crosses above EMA 21 with VWAP reclaim',
      params: {
        symbol: 'TCS',
        direction: 'CALL',
        price: 3908,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 3908, vwap: 3889, volume: 600000, symbol: 'TCS' },
        optionsData: { oiChangePercent: 2, buildUp: 'none' },
      },
      ohlcv: buildCandles('2026-04-15', EMA_CROSS_CALL, 3880, 200000),
    },

    /**
     * S5: CALL TYPE_4 — Trend Continuation (NIFTY-like index, ~22000)
     * isIndex = true → VWAP & Volume checks waived
     */
    S5_CALL_TYPE4_TREND: {
      id: 'S5_CALL_TYPE4_TREND',
      description: 'CALL TYPE_4: Full EMA stack + trend continuation (index)',
      params: {
        symbol: 'NIFTY',
        direction: 'CALL',
        price: 22270,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 22270, symbol: 'NIFTY' },
        optionsData: {},
      },
      ohlcv: buildCandles('2026-04-15', TREND_CONTINUATION, 22000, 0),
    },

    /**
     * S6: CALL GAP OVERRIDE — Gap-up > 1% waives EMA/VWAP pre-conditions
     */
    S6_CALL_GAP_OVERRIDE: {
      id: 'S6_CALL_GAP_OVERRIDE',
      description: 'CALL GAP OVERRIDE: Tier 2+ gap-up, EMA stack waived',
      params: {
        symbol: 'TATASTEEL',
        direction: 'CALL',
        price: 22332,
        targetDate: '2026-04-15',
        entryTime: '09:20',
        snapshot: { ltp: 22332, vwap: 22250, volume: 800000, symbol: 'TATASTEEL' },
        optionsData: {},
      },
      ohlcv: buildCandles('2026-04-15', GAP_UP_SCENARIO, 22000, 300000),
    },

    /**
     * S7: INVALID CALL — EMA stack bearish, trying to enter CALL
     * Expected: isValid=false, avoidFlags or preConditions.emaStack=false
     */
    S7_INVALID_CALL_WRONG_TREND: {
      id: 'S7_INVALID_CALL_WRONG_TREND',
      description: 'Invalid CALL: EMA stack is bearish (downtrend)',
      params: {
        symbol: 'WIPRO',
        direction: 'CALL',
        price: 460,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 460, vwap: 480, volume: 700000, symbol: 'WIPRO' },
        optionsData: {},
      },
      ohlcv: buildCandles('2026-04-15', INVALID_CALL_STEPS, 500, 250000),
    },

    /**
     * S8: PUT TYPE_1 — Pullback Entry (downtrend mirror)
     */
    S8_PUT_TYPE1_PULLBACK: {
      id: 'S8_PUT_TYPE1_PULLBACK',
      description: 'PUT TYPE_1: Structural swing low with controlled bounce pullback',
      params: {
        symbol: 'AXISBANK',
        direction: 'PUT',
        price: 1098,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 1098, vwap: 1118, volume: 850000, symbol: 'AXISBANK' },
        optionsData: { oiChangePercent: 8, buildUp: 'short_buildup' },
      },
      ohlcv: buildCandles('2026-04-15', DOWNTREND_MIX, 1150, 320000),
    },

    /**
     * S9: ERROR — Insufficient candles (< 30)
     * Expected: isValid=false, error message about insufficient data
     */
    S9_ERROR_SHORT_CANDLES: {
      id: 'S9_ERROR_SHORT_CANDLES',
      description: 'Error: Only 15 candles — below minimum 30',
      params: {
        symbol: 'RELIANCE',
        direction: 'CALL',
        price: 2820,
        targetDate: '2026-04-15',
        entryTime: '10:30',
        snapshot: { ltp: 2820, vwap: 2810, volume: 400000 },
        optionsData: {},
      },
      ohlcv: buildCandles('2026-04-15', UPTREND_MIX.slice(0, 15), 2800, 400000),
    },

    /**
     * S10: CALL with entryTime replay (time-based truncation path)
     * Tests TI.truncateAtEntryPrice() time-priority branch
     */
    S10_CALL_REPLAY_TRUNCATION: {
      id: 'S10_CALL_REPLAY_TRUNCATION',
      description: 'CALL with entryTime: tests time-based truncation at 10:30',
      params: {
        symbol: 'RELIANCE',
        direction: 'CALL',
        price: 2848,
        targetDate: '2026-04-15',
        entryTime: '10:30',  // mid-session, truncates at candle ~15
        snapshot: { ltp: 2848, vwap: 2830, volume: 900000 },
        optionsData: { oiChangePercent: 5, buildUp: 'long_buildup' },
      },
      // Full session — truncation will cut at 10:30 (candle index ~15)
      ohlcv: buildCandles('2026-04-15', UPTREND_MIX, 2800, 400000),
    },

    /**
     * S11: NIFTY INDEX CALL — no VWAP, no Volume scoring
     */
    S11_NIFTY_INDEX_CALL: {
      id: 'S11_NIFTY_INDEX_CALL',
      description: 'NIFTY index CALL: VWAP and Volume checks must be waived',
      params: {
        symbol: 'NIFTY 50',
        direction: 'CALL',
        price: 22270,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 22270, symbol: 'NIFTY 50' },
        optionsData: {},
      },
      ohlcv: buildCandles('2026-04-15', TREND_CONTINUATION, 22000, 0),
    },

    /**
     * S12: BANKEX INDEX PUT — tests bearish index path
     */
    S12_BANKEX_INDEX_PUT: {
      id: 'S12_BANKEX_INDEX_PUT',
      description: 'BANKEX index PUT: Bearish trend, index path',
      params: {
        symbol: 'NIFTY BANK',
        direction: 'PUT',
        price: 48000,
        targetDate: '2026-04-15',
        entryTime: '11:55',
        snapshot: { ltp: 48000, symbol: 'NIFTY BANK' },
        optionsData: {},
      },
      ohlcv: buildCandles('2026-04-15', DOWNTREND_MIX.map(x => x * 15), 49000, 0),
    },

  };

  return { SCENARIOS, buildCandles, makeDate };
})();

if (typeof module !== 'undefined') module.exports = OHLCVFactory;
