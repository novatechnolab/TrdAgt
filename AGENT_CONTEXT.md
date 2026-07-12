# TradeSignal — Project Context & Agent Rules

## MANDATORY: Read these skill files before doing any work
- .skills/01_map_first.md
- .skills/02_grep_before_read.md
- .skills/03_write_in_chunks.md
- .skills/04_targeted_view.md
- .skills/05_no_reread.md
- .skills/06_batch_commands.md
- .skills/07_plan_token_budget.md

## Project Summary
- **Backend:** `app/backend/` — Flask + SQLite + Kite API proxy
- **Frontend:** `app/js/` — Vanilla JS SPA
- **Canonical math:** `app/backend/indicators.py` (513 lines)
- **Entry validation:** `app/backend/entry_validator.py` (916 lines)  
- **Gap scoring:** `app/backend/gap_analysis_engine.py` (308 lines)
- **Main server:** `app/backend/server.py` (4278 lines — NEVER read whole file)

## Key line numbers in server.py (saves reading the whole file)
- Imports block: lines 1–40
- indicators import: line 326
- entry_validator import: line 335
- Routes start: line ~411
- `/api/validate-entry` route: line 731
- `/api/validate-entry/backend` route: line 939
- `/api/indicators` route: line 1035

## Migration status (as of Apr 2026)
- Phase 0-6 COMPLETE: indicators.py, gap_analysis_engine.py, entry_validator.py all ported
- `USE_BACKEND_VALIDATOR = true` in trade-cockpit.js (Python path active)
- scoring-engine.js → still JS (Phase 2, future)

## Rules (enforced every session)
1. grep before view_file — always
2. view_file with StartLine+EndLine — always  
3. Large file writes → heredoc chunks (150 lines max each)
4. Batch shell commands — one run_command, not many
5. Never re-read a file you already read this session
