# Skill 07 — Plan Token Budget
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
