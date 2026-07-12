# Skill 01 — Map First, Read Later
## Rule
Never read a file before mapping the codebase structure.
## Steps
```bash
# Cheap directory tree
find . -type f | grep -vE "node_modules|.git|__pycache__|.pytest_cache|dist|build" | sort
# Count lines only (no content burned)
wc -l path/to/suspected/large/file.py
# Grep for key symbols before opening anything
grep -rn "def |class |@app.route|export " src/ --include="*.py" --include="*.js" | head -60
```
## Anti-pattern
❌ Opening server.py (4000+ lines) to "understand the structure"
✅ grep -n "@app.route" server.py → see all routes in ~200 tokens
