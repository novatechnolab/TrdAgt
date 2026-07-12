# Skill 04 — Targeted File Views
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
