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

## Update: reading ladder (2026-11-08, tuning reviews)

Each paper is now read from its abstract, results and discussion, in a window of
18000 characters instead of 12000, with reference lists left out. Introductions
are not read; instead, the papers they cite for the question are followed
(cites.py) and up to 8 of them read in their own right. 48 papers read per review.

Of the statements in the papers read (21; 22 before):

| level | before | now |
|---|---|---|
| in findings | 50% (11) | 67% (14) |
| in evidence | 36% (8) | 48% (10) |
| in short answer | 23% (5) | 24% (5) |
| short, yes or partly | 50% (11) | 43% (9) |

Lost in reading 11 → 7, in the merge 3 → 4, from the short answer 3 → 5. Per
review: OCP findings 58% → 82%, evidence 33% → 45%; ferroptosis findings 40% →
50%, evidence 40% → 50%. 3 of the 8 followed papers were cited in each answer.

- The short answer stays at 4 sentences while the evidence grew (OCP 14 → 21
  sentences, ferroptosis 20 → 40), so it is now where most is lost.
- Time doubled: OCP 1010 → 2027 s, ferroptosis 1755 → 4183 s. Another job shared
  the machine during part of the run, so not all of that is ours.

## Update: a small model for phones and small PCs (2026-11-11)

Standard depth, 40 papers per review. gemma4:e4b (6.6 GB) against gemma4:26b
(17 GB). e4b thinks only in the steps that need judgment:
`WYR_OLLAMA_THINK=_merge,_overview,_unfaithful`. Statements recovered ("yes"),
of those in the papers read (OCP 12, ferroptosis 11), judged by Claude Opus 4.5:

| review | model | time | findings | evidence | short | short, yes or partly |
|---|---|---|---|---|---|---|
| OCP | 26b | 979 s | 7 | 5 | 4 | 33% |
| OCP | e4b | 805 s | 5 | 4 | 2 | 42% |
| OCP | e4b, defaults, short replies (2026-11-12) | 923 s | 6 | 2 | 2 | 55% |
| ferroptosis | 26b (2026-11-10) | 2102 s | 7 | 6 | 3 | 36% |
| ferroptosis | e4b | 1534 s | 7 | 6 | 4 | 55% |

- The judge is noisy: two OCP e4b answers with identical findings and evidence
  scored 8/6 and 5/4, because all three levels are labelled in one prompt. Read
  differences under about 3 statements as noise.
- Read by hand, e4b reads papers as well as 26b without thinking; its merge is
  vaguer (ferroptosis: "autophagy degrades ferritin" where 26b names NCOA4).
- Thinking matters per step. Off on the short answer, e4b's sentences cited no
  claims and the short answer came out empty; off on the wording check, it
  raised 10 flags on OCP, mostly nitpicks (1, a real one, with thinking). On
  every call it was 2.3x slower (OCP 2233 s) and no better.
- Where 26b's time goes (OCP, from `WYR_CALL_LOG`): reading papers 44%, merge
  32%, vote and support checks 20%; 85% of it is writing output.
- Short agreement and support replies (2026-11-12): listing only the findings
  that agree, disagree or back a claim cut those steps to 38 s on OCP (24
  calls, 1700 output tokens). Their scores stayed within the judge's noise:
  per statement they moved both ways, including at the findings level, which
  that change doesn't touch. e4b now thinks on the wording check by default,
  which adds about 200 s.
