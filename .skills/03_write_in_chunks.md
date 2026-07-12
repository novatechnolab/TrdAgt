# Skill 03 — Write Large Files in Shell Chunks
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
