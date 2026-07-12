# Skill 02 — Grep Before Read
## Rule
Before opening any file, grep for the exact symbol/line you need.
## Patterns
```bash
grep -n "def validate_entry" app/backend/server.py
grep -n "@app.route" app/backend/server.py
grep -rn "from entry_validator|import entry_validator" .
```
## When to use view_file
Only after grep gives you the exact line range.
view_file(path, StartLine=842, EndLine=930)   ← GOOD
view_file(path)                                ← BAD (burns full file)
## Savings: 50x reduction by grepping first
