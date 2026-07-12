#!/usr/bin/env python3
"""
setup_skills.py — Drop this in any project root and run once.
Creates the .skills/ folder with all token efficiency skill files.

Usage:
  python setup_skills.py
"""
import os

SKILLS = {
    "README.md": """# Agent Skills — Token Efficiency Pack

Drop this .skills/ folder into any project root.
Tell the agent at session start: "Read the skills in .skills/ before starting."

## Skills
| File | Purpose |
|---|---|
| 01_map_first.md | Map codebase cheaply before reading files |
| 02_grep_before_read.md | Grep for exact lines before opening files |
| 03_write_in_chunks.md | Write large files via shell heredoc chunks |
| 04_targeted_view.md | Read only the lines you need |
| 05_no_reread.md | Never re-read unchanged files |
| 06_batch_commands.md | Batch multiple shell operations into one call |
| 07_plan_token_budget.md | Estimate cost before starting |
""",

    "01_map_first.md": """# Skill 01 — Map First, Read Later
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
""",

    "02_grep_before_read.md": """# Skill 02 — Grep Before Read
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
""",

    "03_write_in_chunks.md": """# Skill 03 — Write Large Files in Shell Chunks
## Rule
For files > 200 lines, use cat heredoc chunks instead of write_to_file.
## Pattern
```bash
cat > output.py << 'PYEOF'
# first 150 lines
PYEOF
echo "PART1_OK"

cat >> output.py << 'PYEOF'
# next 150 lines
PYEOF
echo "PART2_OK"

python -c "import ast; ast.parse(open('output.py').read()); print('SYNTAX_OK')"
```
## Rule: keep each chunk under 150 lines. Always verify after last chunk.
""",

    "04_targeted_view.md": """# Skill 04 — Targeted File Views
## Rule
Always pass StartLine + EndLine to view_file. Never read a whole large file.
## Heuristic
| File size | Max view range |
|---|---|
| < 200 lines | Full file OK |
| 200-500 lines | 150-line windows |
| 500-2000 lines | 80-line windows |
| 2000+ lines | 50-line windows, grep-guided only |
## Anti-pattern
❌ view_file("server.py") → 4278 lines = 17,000 tokens
✅ view_file("server.py", StartLine=731, EndLine=935) → 200 lines = 800 tokens
""",

    "05_no_reread.md": """# Skill 05 — Never Re-Read Unchanged Files
## Rule
If you read a file earlier in the session, do not read it again unless it changed.
## Resume pattern
```bash
git diff --stat HEAD~1          # see what changed
git diff HEAD~1 -- path/to/file # read only changed parts
grep -n "def |class " file.py   # refresh memory cheaply
```
## Mental model to maintain
After reading a file, note key facts:
"server.py: 4278 lines, routes at ~411, entry_validator wired at 918"
"entry_validator.py: 916 lines, validate() at line 1, 17 methods"
""",

    "06_batch_commands.md": """# Skill 06 — Batch Shell Commands
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
echo "=== Files ===" && ls app/backend/*.py && \\
echo "=== Lines ===" && wc -l app/backend/*.py && \\
echo "=== Routes ===" && grep -c "@app.route" app/backend/server.py
```
""",

    "07_plan_token_budget.md": """# Skill 07 — Plan Token Budget
## Token cost reference
| Action | ~Tokens |
|---|---|
| grep result | 100-400 |
| view_file 50-line window | 200-400 |
| view_file full 4000-line file | 15,000-20,000 |
| Shell heredoc write 500 lines | ~400 |
| Re-reading same file twice | 2x waste |

## High-ROI swaps
| Instead of | Do this | Savings |
|---|---|---|
| Reading full server.py | grep -n "@app.route" server.py | ~14,000 tokens |
| write_to_file for 900-line file | 3x heredoc chunks | avoids token limit error |
| Sequential reads | Parallel grep + targeted view | 2x faster |
""",
}

def main():
    skills_dir = os.path.join(os.getcwd(), ".skills")
    os.makedirs(skills_dir, exist_ok=True)
    for filename, content in SKILLS.items():
        path = os.path.join(skills_dir, filename)
        with open(path, "w") as f:
            f.write(content)
        print(f"  ✓ {filename}")
    print(f"\n✅ Skills created in {skills_dir}/")
    print("Usage: tell agent 'Read the skills in .skills/ before starting'")

if __name__ == "__main__":
    main()
