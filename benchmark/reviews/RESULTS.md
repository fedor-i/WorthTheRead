# Review benchmark results, 2026-11-07

The tool reads up to 40 of the papers each review cites, never the review itself.
The judge is Claude Opus 4.5 on Bedrock. Only "yes" counts below, out of the key
statements found in the papers read. This is the final code (e2a80cf).

| review | key statements | in papers read | in findings | in evidence | in short answer | short, yes or partly | conflicts in short |
|---|---|---|---|---|---|---|---|
| ocp | 12 | 12 | 58% | 33% | 25% | 50% | 0/1 |
| ferroptosis | 12 | 10 | 40% | 40% | 20% | 50% | 0/0 |
| all tuning reviews | 24 | 22 | 50% | 36% | 23% | 50% | 0/1 |

Of the 22 statements in the papers read, 11 never became a finding, 3 more were
dropped in the merge, and 3 more were dropped from the short answer.

- The 3 test reviews are scored but not looked at. Use `table --test` to report.
- No other tool has been run on this, so these numbers have nothing to compare with.
- A short answer of 4-5 sentences can't hold 12 statements, so its score has a low ceiling.
- Only 1 disagreement was in the papers read, too few to measure.
- "In papers read" rests on the 8 closest passages, so it can miss a statement.
- 16 of the 40 OCP papers were abstracts only.
