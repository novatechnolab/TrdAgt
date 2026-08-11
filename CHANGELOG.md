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
