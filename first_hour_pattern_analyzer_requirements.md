# First-Hour Pattern Continuation/Reversal Analyzer — Requirements Doc

**Version:** v1.0
**Owner:** Raj
**Status:** Draft — script prototype built (`or_continuation_analyzer.py`)

---

## 1. Objective

Given a stock's behavior in the first 45–60 minutes of the trading session (9:15 AM onward), determine — based on historical precedent — whether the stock is more likely to **continue** in that direction for the rest of the day, **reverse**, or **chop** sideways.

The tool should classify today's opening behavior into a specific, repeatable **pattern** (gap type + move direction + move speed) and look up how similar historical days at this stock resolved, rather than relying on a single generic rule (e.g. "high volume = continuation") applied blindly across all setups.

---

## 2. Scope

### In scope
- Single-stock and multi-stock (bulk, 5–10 to full watchlist ~215 names) historical backtest
- Configurable opening window: 45 min or 60 min
- Gap classification (gap up / gap down / flat) vs previous day's close
- First-hour move-speed classification using fixed absolute % tiers
- Historical pattern lookup: outcome distribution (continuation/reversal/chop), average extension %, average retracement %, average volume ratio, per pattern
- Live "today" prediction: classify today's pattern and return the matching historical bucket's stats
- Daily incremental caching so repeated runs don't re-fetch full history

### Out of scope (future phases)
- Options/OI-based confirmation overlay (separate from OI Trap Dashboard framework)
- Automated trade execution or alerting
- Cross-instrument correlation (e.g. Nifty first-hour behavior predicting stock behavior)
- Machine-learning classifier (logistic regression / gradient boosting) — current version is rule-based statistical lookup only

---

## 3. Data Requirements

| Item | Detail |
|---|---|
| Source | Zerodha Kite Connect — `kite.historical_data()` |
| Interval | 5-minute candles (intraday) |
| Lookback | 30–60 days minimum; 90–120 days preferred for statistically meaningful pattern buckets |
| Fields required | `date, open, high, low, close, volume` |
| Previous close | Derived from prior day's last intraday candle (no separate daily-interval call needed) |
| Rate limits | ~3 requests/sec on Kite historical endpoint — must be respected in bulk mode |

---

## 4. Core Logic / Feature Definitions

### 4.1 Opening Range (OR) window
- Default: 9:15 AM – 10:15 AM (60 min), configurable to 45 min (9:15–10:00)
- Captures: OR open, OR high, OR low, OR close, OR volume, OR momentum % `(or_close - or_open) / or_open`

### 4.2 Gap classification
- Compares today's OR open vs previous day's close
- Threshold: ±0.3% (configurable) — below this treated as `flat`, not a genuine gap
- Output: `gap_up`, `gap_down`, `flat`

### 4.3 Move-speed classification (fixed absolute thresholds)
| Tier | Threshold (abs OR momentum %) |
|---|---|
| `normal` | < 1.5% |
| `fast` | 1.5% – 2.5% |
| `very_fast` | 2.5% – 4.0% |
| `extreme` | ≥ 4.0% |

Thresholds are configurable per instrument tier (index constituents vs small-cap F&O behave differently at the same % move — same design principle as the Wall Strength Registry's instrument-tiered thresholds).

### 4.4 Pattern key
`pattern_key = gap_type | or_direction | move_bucket`

Example: `gap_up | bullish | very_fast`

This is the unit of historical lookup — each pattern key is treated as a distinct historical cohort, not lumped with all "up days."

### 4.5 Outcome classification (rest of day, post-OR-window)
- `continuation`: price breaks OR high (if bullish OR) or OR low (if bearish OR), with buffer (5% of OR range) to filter noise, and day closes beyond that level
- `reversal`: price breaks the opposite side of the OR, closing beyond it
- `chop`: neither confirmed — price stays largely within/near the OR

### 4.6 Behavior metrics (beyond win/loss)
- `extension_pct`: how far price extended beyond the OR edge in the OR direction
- `retracement_pct`: how much price gave back / retraced into the OR range before resolving
- `vol_ratio`: OR-window volume vs trailing 5-day average OR-window volume

---

## 5. Statistical Backtest Output

For each `pattern_key` with ≥ 3 (configurable minimum) historical occurrences:
- Sample size
- Outcome distribution (% continuation / reversal / chop)
- Average extension %
- Average retracement %
- Average volume ratio

Patterns with fewer than the minimum sample size are excluded from confident output (flagged as insufficient data) rather than silently reported as if reliable.

---

## 6. Live Prediction Output

After the OR window closes each day, for a given stock:
1. Compute today's gap type, OR direction, move-speed tier → today's `pattern_key`
2. Look up matching historical cohort stats
3. Return: matched pattern key, sample size, historical outcome distribution, average extension/retracement — as **context for decision-making**, not a deterministic signal

---

## 7. Performance / Bulk Requirements

| Scenario | Approx. time |
|---|---|
| Single stock, full historical fetch (45–60 days) | 1–3 seconds |
| 5–10 stocks, full fetch | 10–25 seconds |
| 215 stocks, full historical fetch (rate-limited) | ~2–4 minutes |
| 215 stocks, daily incremental fetch (cached history + append 1 new day) | Target: under 90 seconds |

**Design requirement:** Full 45–60 day backtest should run once (pre-market or EOD), not repeatedly. Daily operation should only fetch the new day's candles and append to cached data (SQLite, consistent with existing dashboard storage).

---

## 8. Known Limitations / Risks

- **Sample size risk**: With 45–60 days of data split across up to ~27 possible pattern combinations (3 gap types × 3 directions × 3+ speed tiers), many buckets will have too few samples to be statistically meaningful. Mitigation: longer lookback (90–120+ days) and/or pooling across similar instruments.
- **Fixed thresholds vs instrument volatility**: Low-volatility stocks may rarely trigger `fast`/`very_fast`/`extreme` tiers; some small-caps may routinely exceed `extreme`. Requires per-instrument-tier threshold overrides for consistency.
- **Backward-looking only**: Pattern stats describe historical tendency, not guaranteed forward behavior — output should be treated as probabilistic context, consistent with the rest of the trading framework (OI Trap Dashboard signals are also conviction scores, not certainties).
- **No cross-confirmation yet**: Does not currently incorporate OI/options data, sector context, or index correlation — purely price/volume pattern based.

---

## 9. Open Items / Future Enhancements

- [ ] Per-instrument-tier threshold configuration (index vs large-cap vs mid/small-cap F&O)
- [ ] Multi-stock pattern pooling (train pattern stats across similar instruments to increase sample size per bucket)
- [ ] Integration with existing 5-min volume baseline module (replace naive rolling average)
- [ ] SQLite caching layer + incremental daily fetch
- [ ] Live intraday updating scorer (continuously refreshed continuation probability as the OR window unfolds, rather than a single judgment at window close)
- [ ] Visual output: OR box vs day range chart for visual pattern review
- [ ] Optional ML classifier once sufficient historical sample size is available (logistic regression on gap %, move %, volume ratio → outcome)
