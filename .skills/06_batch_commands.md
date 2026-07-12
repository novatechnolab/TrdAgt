# Skill 06 — Batch Shell Commands
## Rule
Combine multiple shell operations into one run_command call.
## Pattern
```bash
# BAD: 3 calls
# ls app/backend
# wc -l app/backend/server.py
# grep -n "@app.route" app/backend/server.py

# GOOD: 1 call
ls app/backend && wc -l app/backend/*.py && grep -n "@app.route" app/backend/server.py | head -20
```
## Session-start audit (one command)
```bash
echo "=== Files ===" && ls app/backend/*.py && \
echo "=== Lines ===" && wc -l app/backend/*.py && \
echo "=== Routes ===" && grep -c "@app.route" app/backend/server.py
```
