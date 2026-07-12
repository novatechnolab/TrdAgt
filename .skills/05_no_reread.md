# Skill 05 — Never Re-Read Unchanged Files
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
