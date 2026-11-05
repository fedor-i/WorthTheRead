# Comparison results, 2026-11-04

Four questions, 68 papers, gemma4:26b for every tool, judged by Claude Opus 4.5
on Bedrock. The judge agreed with the 165 hand labels on keep/no for 155 (93.9%).
PaperQA2 used keyword retrieval. "Wide" is 30 evidence pieces, up to 20 sources.
NotebookLM and ReadCube are not included yet (they need a hand run).

| tool | sentences | uncited | citations | precision | wrong | supported, all sentences | supported, cited sentences | papers cited | minutes |
|---|---|---|---|---|---|---|---|---|---|
| Worth Your Read | 56 | 0 | 107 | 99% | 1 | 95% (93% corrected) | 95% (93% corrected) | 39/68 | 17.7 |
| PaperQA2 | 31 | 6 | 35 | 94% | 2 | 65% | 80% | 16/68 | 4.6 |
| PaperQA2 wide | 32 | 7 | 56 | 89% | 6 | 69% | 88% | 23/68 | 14.9 |

## Update: a short answer on top of the evidence
Ask across papers now opens with a 3-5 sentence answer written from the merged
claims. The claims themselves came out unchanged (same 56 sentences), and each
question took about 15% longer (20.3 minutes in all).

| answer | sentences | uncited | precision | wrong | supported, all sentences | supported, cited sentences | papers cited |
|---|---|---|---|---|---|---|---|
| Worth Your Read short answer | 16 | 0 | 97% | 2 | 81% (75% corrected) | 81% (75% corrected) | 24/68 |
| PaperQA2 | 31 | 6 | 94% | 2 | 65% | 80% | 16/68 |
| PaperQA2 wide | 32 | 7 | 89% | 6 | 69% | 88% | 23/68 |

- With 16 sentences, one sentence moves the score by 6 points, so this doesn't
  separate the tools on support.
- The correction is the same reversed "greater preselection" sentence, which the
  merge passed into the short answer. The wording check flagged it again and the
  judge passed it again.
- The three sentences supported only in part each lacked a place or group word
  (for example "in new users of oral antidiabetic agents").
- Read as answers: metformin now says "not settled" and sets out both sides,
  iron biomass keeps its numbers, iron limits answers by region, and false
  findings is still a compressed list. Sentences are long, and one says "some
  claims state", which is our own term.

## Read these numbers with care
- **Corrected:** the judge passed "greater preselection" although the paper says
  "lesser", so one of our 53 supported sentences is wrong. Our own wording check
  caught it. The judge is equally lenient to every tool, so other tools' scores
  may hide errors of this kind too.
- **"Not in paper" is left out of this table.** The scorer reported 12 for us and
  0 for PaperQA2, but it isn't a fair comparison: PaperQA2's passages are copied
  chunks, so they always match. None of our 12 was made up. Most were text
  formatting (line numbers, hyphens, column breaks). Three quotes contained "..."
  and at least two had small changes (FeXAs for FeAXs), so our quotes aren't
  always strictly word for word.
- **Support is not usefulness.** Our sentences stay close to their quotes, which
  makes them easy to support. PaperQA2 writes more synthesis, so it takes on more
  risk, and its uncited sentences are often framing lines. A reader's view of
  each answer is below.
- **A false statement about the papers can pass.** PaperQA2 said the papers lack
  biomass numbers, when they have them. The judge sees only cited passages, so it
  can't catch that.
- 4 questions, chosen by us.

## Reading the answers
- Iron biomass: ours is clearly better, with real numbers. PaperQA2 said there were none.
- Metformin: PaperQA2 wide is the best answer: both sides and a conclusion. Ours
  covers 17 papers but never answers the question, and conflicts sit far apart.
- Iron limits and false findings: ours is broader and more specific, PaperQA2 reads better.
- One of our metformin citations is a reference-list entry in another paper, not a finding.

## PaperQA2 setup
gemma4 needs think off. Without it every PaperQA2 call ran for the full 15 minutes
and failed. `run_paperqa.py` now uses `ollama_chat/` with think off, `num_ctx`
20000 and 1500 output tokens, the same as our runs.
