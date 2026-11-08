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
- A short answer of 4-5 sentences can't hold 12 statements, so its score has a low ceiling.
- Only 1 disagreement was in the papers read, too few to measure.
- "In papers read" rests on the 8 closest passages, so it can miss a statement.
- 16 of the 40 OCP papers were abstracts only.

## Against PaperQA2 (same 40 papers per review, gemma4:26b)

Full "yes" of the 22 reachable statements (yes or partly in brackets):

| tool | statements | papers cited (ocp / ferroptosis) | seconds (ocp / ferroptosis) |
|---|---|---|---|
| PaperQA2 default | 1/22 (9/22) | | 69 / 74 |
| PaperQA2 wide (`PQA_EVIDENCE_K=30 PQA_MAX_SOURCES=20`) | 5/22 (12/22) | 10 / 10 | 227 / 275 |
| ours, short answer | 5/22 (11/22) | 19 / 32 | 1010 / 1755 (full run) |

Blind reading test (author, 2026-11-07): the short answers from ours and PaperQA2 wide, side by
side with citations hidden and sides switched between questions. Ours was picked as the
better answer on both reviews; trust was called a tie on both. Run with `rival TOOL`
(`run_paperqa.py`); PaperQA2 was not run on the test reviews.
