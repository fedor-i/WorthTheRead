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
