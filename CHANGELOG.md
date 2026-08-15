# TradeSignal — Change Log

---

## [2026-08-09 / 2026-08-10] — Agentic AI Migration & Hardening

### Session Goal
Migrate monolithic `server.py` (11,684 lines) to a multi-agent architecture with autonomous orchestration, centralized alert dispatch, and multi-scanner confluence prediction — without changing any business logic, thresholds, or scanner rules.

### Architecture Added
- `app/backend/agents/` package (new)
  - `base_agent.py` — Thread-isolated BaseAgent with inbox queue, heartbeat, error boundary, lifecycle hooks
  - `orchestrator.py` — Supervisor with watchdog auto-restart (10s cycle, 60s heartbeat timeout)
  - `message_bus.py` — In-process pub/sub broker with fnmatch wildcard topic matching
  - `kite_data_agent.py` — Single KiteWS owner; rate limiter (8 req/s); quote cache; feeds synergy_scanner state
  - `synergy_agent.py` — Bridges to synergy_scanner.get_buy_alerts()
  - `ema_agent.py` — Bridges to ema_crossover_scanner.get_ema_crossover_state() alignment field
  - `fno_trap_agent.py` — Bridges to fno_trap.trap_engine._card_cache
  - `market_agent.py` — Reads compute_bias_score() results via server cache
  - `prediction_agent.py` — 5-min rolling confluence matrix; emits when 2+ scanners agree (>=75% conviction)
  - `alert_dispatch_agent.py` — Centralized Telegram + Discord + SocketIO dispatch; 15-min cross-scanner cooldown; 50ms poll loop
  - `__init__.py` — Package exports
- `app/backend/constants.py` — USE_AGENTIC_WORKFLOW = True feature flag
- `app/backend/tests/` — 5 test modules (13 tests total)

### Files Modified
| File | Change |
|---|---|
| `app/backend/server.py` | D1: tick wiring (non-blocking put_inbox); D2: import path try/except; D9: atexit shutdown; Fix1: gate lazy_start_synergy + lazy_start_ema in agentic mode; Fix5A: cache _agentic_data_agent at startup; wire Telegram/Discord creds into AlertDispatchAgent |
| `app/backend/agents/kite_data_agent.py` | Fix2: feed synergy_scanner._on_ticks() from agent WS; Fix5B: handle_message() for inbox tick batches |
| `app/backend/agents/ema_agent.py` | Fix3: full rewrite — bridge to get_ema_crossover_state() instead of tick-based EMA |
| `app/backend/agents/alert_dispatch_agent.py` | D7: socketio None guard; Fix4: 50ms poll _run_loop override; Discord _send_discord() method + credentials wired |
| `app/backend/agents/synergy_agent.py` | D3: bridge to synergy_scanner.get_buy_alerts() |
| `app/backend/agents/fno_trap_agent.py` | D4: bridge to fno_trap.trap_engine._card_cache |
| `app/backend/agents/market_agent.py` | D5: removed unauthorized +/-0.35% logic; reads compute_bias_score() cache |
| `app/backend/agents/prediction_agent.py` | D8: harden deduplication using agent_name field |
| `app/backend/tests/test_phase5_integration.py` | D6: import fix; updated for new EMAAgent bridge architecture |
| `app/backend/tests/test_phase2_agents.py` | Updated for new EMAAgent architecture (tick to bus delivery test) |

### Defects Fixed (9 from migration audit)
| ID | Issue | Fix |
|---|---|---|
| D1 | No tick wiring to agents | Non-blocking put_inbox from on_ticks() WS callback |
| D2 | Import path breaks by CWD | try/except import in server.py |
| D3 | SynergyAgent stub | Bridge to synergy_scanner.get_buy_alerts() |
| D4 | FNOTrapAgent stub | Bridge to fno_trap.trap_engine._card_cache |
| D5 | Unauthorized +/-0.35% logic in MarketAgent | Removed; reads compute_bias_score() |
| D6 | Test import inconsistency | Normalized to try/except both CWDs |
| D7 | SocketIO None timing risk | Added guard + warning log |
| D8 | Fragile deduplication | agent_name field prioritized in signal payloads |
| D9 | No shutdown hook | atexit.register(orchestrator.stop_all) |

### Latency / Correctness Issues Fixed (7 from architecture audit)
| ID | Issue | Fix |
|---|---|---|
| L1 | 3 simultaneous KiteWS connections (Kite limit: 1) | Gated lazy_start_synergy + lazy_start_ema behind USE_AGENTIC_WORKFLOW |
| L2 | SynergyAgent breaks if scanner WS gated | KiteDataAgent.on_tick_received() calls synergy_scanner._on_ticks(None, ticks) |
| L3 | EMAAgent computed noisy tick-based EMA, conflicted with real scanner | Rewritten to poll get_ema_crossover_state().alignment |
| L4 | Alert path added up to 400ms latency | AlertDispatchAgent._run_loop polls at 50ms instead of 200ms |
| L5 | on_tick_received() synchronous on Kite WS thread | _agentic_data_agent cached at startup; WS uses put_inbox(block=False) |
| L6 | AlertDispatchAgent had no Telegram/Discord credentials | Wired from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / DISCORD_WEBHOOK_URL env vars |
| L7 | AlertDispatchAgent missing Discord support | _send_discord() added with certifi SSL, 2000-char limit, domain normalization |

### Constraints Respected
- Zero business logic changes — all scanner thresholds, rules, and computation unchanged
- Zero UI/API surface changes — all Flask routes, endpoints, and SocketIO events unchanged
- Original alerts unchanged — Telegram/Discord from scanners still fire via server._send_telegram_message()
- Agentic pipeline is additive — agents sit alongside existing scanners, not replacing them

### Test Results
- 13/13 tests PASS
- Run: python -m unittest app.backend.tests.test_agent_framework app.backend.tests.test_phase2_agents app.backend.tests.test_phase3_agents app.backend.tests.test_phase4_agents app.backend.tests.test_phase5_integration

---

*All future sessions modifying this codebase must append an entry to this file per .agents/AGENTS.md Changelog Discipline rule.*

---

## [2026-08-10] — Fix1 Revert: Restore Full Scanner Alert Coverage

### Session Goal
Revert Fix1 (lazy scanner gating) which was incorrectly suppressing all EMA crossover and synergy scanner Telegram/Discord alerts in agentic mode.

### Root Cause
Fix1 assumed KiteDataAgent opened a 4th KiteWS connection. It does not — it receives ticks from server.py's existing on_ticks() callback via put_inbox(). The 3-WS situation (server + synergy + ema) is pre-existing and unchanged by the migration. Gating scanner startup eliminated their entire scan loops and direct alert generation.

### Files Changed
| File | Change |
|---|---|
| `app/backend/server.py` | Reverted lazy_start_ema_crossover_scanner() and lazy_start_synergy_scanner() to start unconditionally in all modes — identical to pre-migration behavior |

### Alert Eligibility
Zero changes to alert eligibility logic. All EMA crossover, pre-cross, live breakout, collision, and synergy scanner alerts now fire exactly as before the migration.

### Test Results
- 13/13 PASS

## 2026-08-13 — Fix: NameError _kite_services_lock on startup
**Goal:** Fix NameError crash in `start_kite_dependent_services` that silently prevented all Kite-dependent services from starting.
**Files changed:**
- `app/backend/server.py`

**Summary:**
- Added missing module-level declarations `_kite_services_lock = threading.Lock()` and `_kite_services_started = False` (after line 389, alongside other lock definitions).
- Fixed broken double-check locking pattern in `start_kite_dependent_services()` — consolidated into a single `with _kite_services_lock:` block with `kite` guard inside, eliminating the race condition.

## [2026-08-14] — 360 Command Center v2 Upgrade

**Session Goal:** Upgrade 360 Command Center from v1 to v2 with 6 visual improvements.

**Files Changed:**
- `app/360-command-center.html` — Full v2 rewrite (732 lines, 48KB)
- `app/360-command-center-v1-backup.html` — Backup of v1 (renamed from .v1.bak)

**Summary of Changes:**
1. **Hot Zone strip** — Top 3 confluent stocks shown as large cards above layout with confluence meter bar (gradient fill), TF/OI/RVOL tags, score badge, inline sparkline
2. **Amber pulse animation** — A-grade alerts fire `amberpulse` keyframe (3 pulses) + continuous amber dot indicator
3. **Row-level heatmap glow** — Table rows have `glow-hi` (green, score≥8 bullish), `glow-bear-hi` (red, score≥8 bearish), `glow-med` (amber tint, score≥5)
4. **8-factor filled bars** — Each confluence factor shown as tall gradient-filled bar (green/red/amber) replacing flat boxes
5. **Inline SVG sparkline** — Premium gain trend sparkline with area fill + endpoint dot in GAIN% column
6. **OI Spurt ranked bar chart** — 8px gradient purple bars, top-1 has glow/shimmer effect
7. **Dynamic layout height** — `fixHeight()` adjusts `.lay` height to account for Hot Zone strip on load + resize

## [2026-08-14] — 360 Command Center Live Backend Integration

**Session Goal:** Wire 360 Command Center to backend agents and REST APIs with full independence from other dashboard pages.

**Files Changed:**
- `app/backend/server.py` — Added 3 lazy_start calls after orch.start_all() (lines 128-133)
- `app/360-command-center.html` — Injected Socket.IO client + fetchAll() + field mappers (967 lines)

**Summary:**
1. `server.py`: `lazy_start_option_scanners()`, `lazy_start_option_gainers_alerts()`, `lazy_start_ema_crossover_scanner()` now auto-start when agentic orchestrator initialises — zero dependency on other pages opening first
2. `360-command-center.html`:
   - Added Socket.IO CDN (v4.7.2)
   - `initSocketIO()` — listens to `tradesignal_alert` from AlertDispatchAgent for real-time A/B/C alerts with amber pulse
   - `startPolling()` — polls `/api/option-gainers-board` (30s), `/spurt` (30s), `/api/live-breakouts` (30s), `/kite/global-quotes` (15s)
   - `mapBoardToStocks()`, `mapAlerts()`, `mapSpurts()`, `mapBreakouts()`, `mapAlertPayload()` — field mappers for all endpoints
   - `setFetchStatus()` — live/stale pill indicator
   - Static sample data arrays converted from `const` to `let` for live overwrite
   - No changes to any agent files or other dashboard pages

## [2026-08-14] — 360 Command Center Added to Sidebar

**Session Goal:** Add 360° Command Center to the left navigation sidebar above Premium Gainers Board.

**Files Changed:**
- `app/index.html` — Inserted nav item at line 84 (above Premium Gainers Board)

**Summary:** Added `🔭 360° Command Center` sidebar link pointing to `/360-command-center.html`, opens in new tab (↗), matching exact style of adjacent nav items.

## [2026-08-14] — 360 Command Center: Live Field Mapper Fix

**Session Goal:** Fix mock data showing by verifying actual API field names and correcting all mappers.

**Root cause:** mapBoardToStocks() used wrong field names (ltp/gain_pct/rvol at stock level, but actual API has best_gain/rvol_ratio; no ltp at stock level — ltp is in contracts[]).

**Files Changed:**
- `app/360-command-center.html` — Cleared all mock arrays + rewrote all integration code with verified field names

**API Field Corrections:**
- `/api/option-gainers-board`: `rvol_ratio` (not `rvol`), `best_gain` (not `gain_pct`), ltp from `contracts[].ltp`, cap/buildup from `/api/futures-buildup`
- `/api/futures-buildup`: `stocks[]` with `buildup`, `cap`, `rvol`, `oi_chg_pct`, `spot_chg_pct`, `cpr.tc/piv/bc`
- `/api/ema-crossovers`: `crossovers{}` with `state_15m/1h/day`, used for both TF confluence AND bulls/bears
- `/kite/global-quotes`: `data.india_vix.price`, `data.gift_nifty.price` (GIFT NIFTY shown when market closed)
- `/spurt`: returns HTML not JSON — replaced with OI data from `/api/futures-buildup` oi_chg_pct
- Static mock arrays: all cleared to `[]`

## [2026-08-14] — 360 Command Center: Live vs EOD Rebuild & Syntax Fix

**Session Goal:** Ensure zero stale/mock data display, handle off-market EOD snapshot rebuilding automatically, serve live data during market hours, and fix JS syntax concatenation error.

**Files Changed:**
- `app/360-command-center.html` — Removed duplicated file concatenation at line 972; added EOD snapshot rebuilding spinner and loading state handling; converted static placeholders (`SOLARINDS/ASTRAL`, dummy stats, hardcoded market index prices) to dynamic containers.

**Summary of Changes:**
1. **Syntax Fix:** Truncated duplicate HTML block appended at line 972 that caused `SyntaxError: Unexpected token '<'` and stopped JavaScript execution.
2. **EOD Rebuilding Support:** Added handler for `data.status === 'loading'` and `data.is_eod_snapshot` to display `📸 Rebuilding EOD Snapshot…` with auto-polling every 30s until background generation completes.
3. **Live vs EOD Status Engine:** Switched top status badge dynamically between `LIVE · [Time]` (green) during market hours and `EOD SNAPSHOT · [Date]` (amber) outside market hours.
4. **Dynamic Sidebars & Stats:** Replaced static HTML mock breakout items and stats with dynamic renderers (`renderSideBreakouts`, `updateSessionStats`).
5. **Zero Regression:** All backend files and other dashboard pages remained 100% untouched.

## [2026-08-14] — 360 Command Center: Adopt Exact OI Spurt Scanner Logic

**Session Goal:** Align OI Spurt % change and rankings with the official NSE underlying Change in OI dataset matching `oi-spurt-scanner.html`.

**Files Changed:**
- `app/360-command-center.html` — Integrated `/api/oi/spurt?min_pct=0` in `fetchBoard()`; sorted descending by `oi_change_pct`; updated `OI_SPURTS`, ticker, and `mapBoardToStocks(..., oiMap)` with true aggregate NSE OI% changes.

**Summary:**
1. Replaced single-contract `/api/futures-buildup` fallback with the dedicated `/api/oi/spurt` dataset used by `oi-spurt-scanner.html`.
2. Sorted descending by `oi_change_pct` to match official NSE ordering (HINDPETRO +54.4%, ALKEM +44.7%, PAGEIND +42.3%, BDL +32.3%, VOLTAS +22.8%, etc.).
3. Mapped `oiMap` into the Unified Master Board and confluence scoring.

## [2026-08-14] — 360 Command Center: Yellow Highlight on OI Spurt Order & Value Changes

**Session Goal:** Highlight both ranking order shifts (reorderings) and OI value updates in yellow across the OI Spurt Board, Marquee Ticker, and Master Board.

**Files Changed:**
- `app/360-command-center.html` — Added rank cache (`_prevOIRankMap`) and value cache (`_prevOIPctMap`); added yellow delta badges (`▲2`, `▼1`, `NEW`), yellow row tint/glow, yellow gradient bar, and pulse animation for live OI% changes.

## [2026-08-14] — 360 Command Center: Expandable Contracts & Bulls (68) / Bears (146) Alignment

**Session Goal:** Implement expandable option contracts sub-rows on the Master Board and adopt authentic 68 Bulls / 146 Bears classification matching `option-gainers-board.html`.

**Files Changed:**
- `app/360-command-center.html` — Added accordion click-to-expand option contract sub-rows (`.contracts-subrow`) showing opening/running tags (`⭐`/`🏃`), strike price, type (`CE`/`PE`), premium flow (`open_prem → ltp`), and % gain; adopted exact `renderCrossovers()` categorization logic from `option-gainers-board.html` to populate 68 Bulls and 146 Bears with `All`, `Aligned`, and `Cross` filters.

## [2026-08-14] — 360 Command Center: Vertical Option Cards Layout (Matching Screenshot 2)

**Session Goal:** Replace horizontal pills with the clean vertical full-width option cards view matching `option-gainers-board.html`.

**Files Changed:**
- `app/360-command-center.html` — Updated `.contracts-subrow` and `.option-card` with full-width vertical layout: gold `⭐` / cyan `🏃` tag, bold strike price `₹1,170`, `PE`/`CE` badges, entry → LTP flow (`₹20.4 → ₹37.9`), and right-aligned gain badge (`🚀 +85.1%` / `🔥 +567.4%`).

## [2026-08-15] — 360 Command Center: PremGain Tab & Complete Futures Buildups

**Session Goal:** Rename default 'All' tab to '🔥 PremGain' (denoting active option gainer stocks) and add complete futures buildup filter tabs (LB, SB, SC, LU, FLAT) covering the full F&O universe.

**Files Changed:**
- `app/360-command-center.html` — Renamed 'All' button to '🔥 PremGain'; added '⚡ Short Cover' (`SC`), '💨 Long Unwind' (`LU`), and '⚖️ Flat B/U' (`FLAT`) filter tabs; merged full ~214 F&O universe from `futMap` so that `LB + SB + SC + LU + FLAT = Total F&O Stocks`; updated session stats with full buildup breakdown.

## [2026-08-15] — 360 Command Center: Consolidated Futures Buildup Tab & Interactive Stats Grid

**Session Goal:** Consolidate multiple buildup buttons into a single '📈 Futures Buildup' tab with an interactive 5-card stats grid (Long Buildup, Short Buildup, Short Cover, Long Unwind, Flat Buildup) that filters stocks on click.

**Files Changed:**
- `app/360-command-center.html` — Replaced individual buildup filter tabs with a single `📈 Futures Buildup` tab; rendered an interactive 5-box stats grid (`.futbld-stats-bar` / `.fb-stat-card`) with color-coded live counts matching the user's design; clicking any stat box filters the table to that buildup with active highlight glow; updated `aTab('bup')` in Alert Feed.

## [2026-08-15] — 360 Command Center: Net Drift Aligned with OI Transition Conviction Engine

**Session Goal:** Align the DRIFT column in 360 Command Center with the authentic OI Transition Conviction Engine (`/api/oi/symbol/<symbol>`) matching the OI Spurt Scanner and Option Gainers Board.

**Files Changed:**
- `app/360-command-center.html` — Added `_driftCache` and `fetchOITransitionDrifts()`; resolved drift using Transition Conviction `net_drift` (`Lift`, `Sink`, `Brk▲`, `Brk▼`, `Rng`, `Neut`) with fallback to spot direction rather than linear regression slope.

## [2026-08-15] — 360 Command Center: INST & E9H Display Logic Aligned with Premium Gainers Board

**Session Goal:** Fix INST and E9H mapping in 360 Command Center to match the exact format of the Premium Gainers Board (`option-gainers-board.html`).

**Files Changed:**
- `app/360-command-center.html` — Fixed INST to display institutional holding percentage (`18%`, `27%`, `42%`, `65%` with cyan highlight for $\ge 50\%$); fixed E9H to render EMA-9 Hold state + minutes as `Y25` (Green = price above 9-EMA for 25 min) and `N80` (Red = price below 9-EMA for 80 min).

## [2026-08-15] — 360 Command Center: Tailored Stock-Level Metrics in Futures Buildup Mode

**Session Goal:** Tailor the Futures Buildup view to stock-level metrics: display actual stock cash/spot price in SPOT PRICE column, suppress option contract accordion subrows, and remove LIN% and GAIN% columns in Futures Buildup mode.

**Files Changed:**
- `app/360-command-center.html` — Dynamically updated `<thead>` and `<tbody>` for `activeFilter === 'futbld'`: renders `SPOT PRICE` with stock's actual cash price (e.g. ₹1,850+ for DALBHARAT), removes `LIN%` and `GAIN%` columns, and disables option contract subrows.

## [2026-08-15] — 360 Command Center: SPOT% Pill Badge & Sparkline Styling

**Session Goal:** Style SPOT% as a rounded pill badge with glowing border and trailing sparkline/direction vector matching the user's design reference.

**Files Changed:**
- `app/360-command-center.html` — Added `.spot-badge` CSS with color-coded borders and glowing background; rendered `SPOT%` within a `.gain-cell` container alongside trailing SVG sparklines (`${sp}`).

## [2026-08-15] — 360 Command Center: In-Line APEX Intraday Chart Integration

**Session Goal:** Enable clicking any stock under Futures Buildup to expand an interactive in-line panel directly below the row displaying the full live APEX Intraday Chart for that stock.

**Files Changed:**
- `app/apex-dashboard.html` — Added embed-mode CSS to cleanly fit within iframe containers without top navigation bar; supports deep-linked `?symbol=<SYM>&embed=1`.
- `app/360-command-center.html` — Added `_activeChartStock`, `toggleStockChart(sym)`, `.chart-subrow`, `.chart-embed-box`, and `.apex-chart-iframe` styles to render the live APEX chart with 5m/15m candles, EMAs, CPR pivots, signals, popout button, and close controls.

## [2026-08-15] — APEX Dashboard: Dynamic Instrument Resolution & URL Symbol Startup

**Session Goal:** Fix APEX Chart defaulting to NIFTY50 when embedded in 360 Command Center by auto-reading `?symbol=<SYM>` from URL query and dynamically resolving Kite instrument tokens for all F&O stocks on demand.

**Files Changed:**
- `app/apex-dashboard.html` — Configured initial active tab and symbol from `?symbol=<SYM>`; added `ensureInstrument(symbol)` to dynamically resolve tokens via `/kite/instruments?search=<SYM>` for all F&O stocks across `fetchCandles`, `fetchLiveQuote`, and `fetchPrevSessionOHLC`.

## [2026-08-15] — 360 Command Center: Native In-Line Intraday Candlestick & CPR Chart Engine

**Session Goal:** Implement a native, high-performance in-line intraday candlestick and CPR chart inside the 360 Command Center that loads in < 50ms without rate limits.

**Files Changed:**
- `app/360-command-center.html` — Replaced heavy iframe embed with a native HTML5 Canvas intraday chart engine displaying 5m/15m candlesticks, volume bars, EMA 9, EMA 21, EMA 50, VWAP, horizontal dashed level lines with right-edge badges for PDH, PDL, TC, Pivot, BC, 5m/15m switcher, interactive hover crosshair HUD, and one-click popout to the full APEX dashboard.

## [2026-08-15] — Option Chain: Automatic Active Expiry Resolution & OI Heatmap Accuracy

**Session Goal:** Fix multi-expiry contract pollution in `/api/option-chain` ensuring all ATM ± 10 strikes in the APEX OI Heatmap belong strictly to the nearest active monthly expiry with correct prices, OI, and PCR.

**Files Changed:**
- `app/backend/server.py` — Updated `/api/option-chain` to resolve and filter by the nearest upcoming active expiry when `expiry` parameter is omitted, eliminating contract price/OI overwrites from far-month expiries.
- `app/apex-dashboard.html` — Added active expiry badge display in `renderOIHeatmap` summary header while preserving the ATM ± 10 strikes display range.



















---

## [2026-08-15] — 360 Command Center: OI Spurt Board — Remove 15-Entry Cap

**Session Goal:** Fix OI Spurt Board displaying fewer entries than the OI Spurt Scanner.

**Files Changed:**
- `app/360-command-center.html` — Replaced `slice(0, 15)` with `filter(f => oi_change_pct >= 5)` in `buildOISpurts()`. Now shows all F&O stocks with OI% ≥ 5%, matching the OI Spurt Scanner left panel behavior.

## [2026-08-15] — 360 Command Center: Board Column Reorder

**Session Goal:** Remove OPT LTP column and move RVOL, FH VOL, E9H to appear after DRIFT column in the Unified Master Board.

**Files Changed:**
- `app/360-command-center.html` — Removed `OPT LTP` column; moved `RVOL`, `FH VOL`, `E9H` columns to appear after `DRIFT` in both the table header and row rendering (default/non-futures-buildup view).
