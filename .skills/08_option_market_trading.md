# Skill 08 — Option Market Trading (Real Strategy)
## Rule
When working on option market features, prioritize real-world option trading strategy, trade execution discipline, and risk management over theoretical or academic indicators.

## Objective
Build or improve option trading logic that can support real-life strategies such as directional option trades, iron condors, calendar spreads, and gamma scalping, while ensuring the code is robust enough for actual market conditions.

## Steps
1. Identify the option feature or module in the codebase.
   - Look for `option-chain`, `oi-spurt`, `greeks`, `implied volatility`, `strike`, `expiry`, `delta`, `theta`, `IV`
2. Prefer strategy-backed logic:
   - directional trade: long call/put or credit spread when a strong underlying market signal exists
   - premium capture: sell high IV and manage risk with defined stops
   - volatility play: use option pair constructs when IV skew or time decay is significant
3. Implement practical checks:
   - validate enough liquidity before selecting strikes
   - honor lot-size and margin constraints
   - avoid chasing tiny OI or volume spikes alone
4. Use live data appropriately:
   - fetch real-time quotes and OI data through proxy/backend auth
   - refresh at a sensible frequency for options markets (e.g. 30–120s)
   - respect market hours and expiry rollovers
5. Make signals actionable:
   - produce entry/exit rules, stop-loss and target levels
   - attach reason summary: e.g. "Bullish breakout, call-buy, strong OI buildup, IV contraction"
   - label risk type: directional, income, hedge, volatility
6. Keep the implementation top-grade:
   - handle failed network/API calls cleanly
   - do not hardcode stale fundamental or Greek values
   - prefer modular helper functions for strikes, Greeks, and payoffs

## Anti-patterns
❌ Implementing “options score” without reference to real trade structure or risk limits.
❌ Using only percent change or OI spike heuristics as the trade decision.
❌ Adding UI badges for “high probability” without a concrete strategy or stop-loss.

## When to apply
- Adding or fixing option chain analytics
- Creating OI/IV-based trade signals
- Building real-live option screeners for NSE F&O
- Improving backend handling of option quotes and expiries
