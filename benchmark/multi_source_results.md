# Multi-source attribution: results (2026-11-03, gemma4:26b)

Question: *Is primary production in the ocean limited by iron or by nitrogen,
and where does each limitation apply?* — 25-paper Zotero collection, 19
readable. `multi_source_experiment.py`.

## A. Check variants on the labelled set (47 pairs, 11 sentences)
Same sentences, the cited quotes as findings; scored by `multi_source_score.py`.

| | precision | recall | recall, partial support | wrong sources kept |
|---|---|---|---|---|
| keep everything | 55% | 100% | 100% | 21 |
| whole-sentence check (`_supporting`) | 100% | 69% | 62% | 0 |
| part check (`_supporting_parts`) | 96% | **89%** | **86%** | 1 (iron-04-05: Tsuda, subarctic, under an equatorial-Pacific sentence) |

Sure labels only: whole-sentence 100% / 78% / 72%; part check 96% / 96% / 94%.
44 s for both checks over 11 sentences.

## B. Whole pipeline, same 47 findings (map: 19 papers, 184 s)
Labelled by hand afterwards (backs / partly / no), not by the scorer.

| method | time | sentences | sentences citing 2+ papers | sources | wrong sources |
|---|---|---|---|---|---|
| merge + whole-sentence check | 67 s | 22 | 2 | 34 | 0 |
| merge + part check | 54 s | 10 | 5 | 18 | 0 |
| group first, then write | 35 s | 9 | 7 | 34 | 1 (F39, a growth rate, under "subarctic growth is iron-limited") |

- **merge + whole-sentence check:** no wrong sources. But the merge ignored "at
  most 10" and returned 22 near-copies of single findings, so it is barely a
  synthesis. The same merge prompt on the same findings gave 10 sentences in
  the other run, so the merge step's output is unstable between runs.
- **merge + part check:** no wrong sources. Each source is tied to the words
  it backs, e.g. "In the Southern Ocean, iron is suggested to limit
  phytoplankton growth" (de Baar) / "growth may differ between regions with
  high and low silicic acid" (Coale).
- **group first:** the most synthesis, and the fastest. But one group mixed
  two regions: the Southeast Pacific gyre station GYR and the North Pacific
  Subtropical Gyre ended up in one sentence that reads as if they were one
  place. Several sentences also cite detail findings they don't state, such
  as Tsuda's species shift under a sentence about broad iron limitation.

Bug found on the way: in `_combine_merge`, the list of merge batches was named
`parts`, which overwrote the `parts` flag. With more than MERGE_BATCH findings
the whole-sentence variant silently ran the part check. Fixed, with a test.

## Takeaway
The part check fixes the partial-support loss (recall 69% → 89%) at the cost
of 1 wrong source in 47 pairs. Group-first synthesises more, but it is less
safe as is.

## C. Part check as the default, live in Zotero (note 106)
The merge produced the same 11 sentences as the labelled run, so its kept
sources score directly against the test set (`multi_source_live_parts.json`):

| | precision | recall | recall, partial support | wrong sources |
|---|---|---|---|---|
| whole-sentence check (`multi_source_baseline.json`) | 100% | 73% | 67% | 0 |
| part check (default) | **100%** | **92%** | **90%** | **0** |

On the sure labels only, the part check scores 100% on every measure. It cut
2 sources, iron-01-03 and iron-04-04, both of them unsure labels. The run took
356 s for 19 papers. Citations sat at their parts, e.g. "Iron supply limits
primary production in one-third of the world ocean (Boyd 2007), specifically in
HNLC areas (Moore 2013; Okin 2011)". One fix followed: a paper backing two
parts (Bonnet: station GYR and station EGY) was cited only at the first. It is
now cited at each part.

## D. Three more questions (batch, 2026-11-03)
Same experiment, three methods each over one shared set of findings. All output
pairs were labelled by hand and added to `multi_source_gold.jsonl` (ids
`ironq-*`, `false-*`, `metf-*`; 118 pairs, `unit: "finding"`).

| question | papers | findings | map | sentences (merge) |
|---|---|---|---|---|
| Q1 iron: how much did iron additions raise biomass/productivity? | 19 | 19 | 126 s | 8 |
| Q2 why most published findings may be false, and responses (5 papers) | 5 | 24 | 73 s | 10 |
| Q3 metformin: mortality / lifespan in humans (Research, top 25 of 60) | 25 | 77 | 280 s | 28 |

Kept sources, merge + check (86 pairs):

| | precision | recall* | wrong sources | good ones cut |
|---|---|---|---|---|
| part check (default) | 97.6% | 100% | 2 (Q3 s13: all-trials RR 0.94 and "no significant effect" under a placebo-trials sentence) | 0 |
| whole-sentence check | 100% | 97.6% | 0 | 2 (Q2 Djulbegovic; Q3 RR 0.91) |

*Recall counts only candidates that at least one method kept, so it is
overstated for both. Precision is reliable.

Group-first + part check: no wrong sources in Q1/Q2, but lower coverage (Q2
dropped Wacholder's genetic findings and the preprint paper). On Q3's 77
findings it wrote **1 sentence**: it does not scale as built.

Where the remaining errors are now — the merged sentence, not its sources:
- **Garbled number:** "24% to 61%" became "2ly% to 61%" (Q3 s9). Added
  `_unsourced_numbers`: a merged claim with a figure that is in none of its
  findings is flagged (`unsourced_numbers`). Over all 128 merged sentences from
  the four questions it flags exactly that one (plus, under the
  whole-sentence check, Q3 s13's 0.94/0.91, whose sources it had cut). No false
  alarms after skipping CO2-style formulas and "type 2"-style labels.
- **Id markers leaked into the text** ("[C6]"): only "[P1]" markers were
  stripped. Now all [P/C/G n] markers are.
- **Wrong scope:** Q3 s13 states the all-trials RR as a placebo-trials figure.
  The numbers exist, so the guard can't see it. It needs a sentence-level check.
- **"Another study":** Q3 s8 presents three results from one meta-analysis as
  different studies.
- **Too long:** Q3's merge gave 28 sentences, and the second merge didn't
  shorten them. The cause is not yet known.
- **Disagreement isn't brought out:** Goodman & Greenland's critique of "most
  findings are false" only appears as a list of causes.

## E. Sentence check: does a merged sentence say only what its findings say? (2026-11-04)
`faithful_gold.jsonl`: the 56 merge + part-check sentences from A–D, each with
its kept findings, labelled faithful or not by hand. 4 are not:
false-s02 ("greater preselection"; the finding says *lesser*), metf-s08 (one
meta-analysis split into "another study" twice), metf-s09 ("2ly%"), metf-s13
(the all-trials RR 0.94 given as a placebo-trials figure).

| check | caught | false alarms | cost |
|---|---|---|---|
| number guard (`_unsourced_numbers`) | 1/4 (s09) | 0/52 | none |
| model check (`_unfaithful`, `experiment.py faithful`) | 2/4 (false-s02, metf-s09) | 0/52 | 692 s, ~12 s per sentence |
| "another study" rule (`_more_studies_than_papers`) | 1/4 (metf-s08) | 0/52; 0 over all 147 saved sentences | none |
| all three | 3/4 | 0/52 | |

- The model check is the only one that sees a reversed direction.
- It misses wrong scope (metf-s13): the sentence's opening "In trials
  comparing metformin to placebo" scopes a number the findings give for all
  trials, and the model doesn't connect the two.
- It also misses "another study" (metf-s08) even though each finding names its
  paper; the rule catches it without a model. "In a second study" (a method,
  in false-s09) is not counted.
- Both now run in `ask_many` on every merged claim (`problems`), shown in red
  in the Ask tab and the note. A figure the number guard already flags is not
  repeated.
