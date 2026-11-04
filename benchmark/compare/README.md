# Comparing tools on the same papers

Same questions, same paper texts, one judge, one rubric. Each tool answers
the 4 questions in `questions.json` from the papers our own runs read; every
(sentence, cited passage) pair is labelled by Claude on Bedrock, which never
sees which tool wrote the answer. Then `score_compare.py` scores the tools side by side.

| id | question | papers |
|---|---|---|
| iron-limits | Is ocean primary production limited by iron or nitrogen, and where? | 19 |
| iron-biomass | How much did iron additions raise biomass / productivity? | 19 (same) |
| false-findings | Why might most published findings be false, and what was proposed? | 5 |
| metformin | Does metformin reduce mortality or extend lifespan in humans? | 25 |

Some papers are only an abstract (no open full text); every tool gets the
same text, so that's the same for all.

## Ready now (done once, model-free)
- `papers/<id>/` holds `manifest.json` and `NN.txt` per paper, exported by
  `export_papers.py` from the same on-disk cache our runs read. It's git-ignored
  because these are full papers. To redo it:
  `python benchmark/compare/export_papers.py <copy of zotero.sqlite>`
- PaperQA2 2026.8.12 is installed in `.venv-paperqa/` (git-ignored). Loading all
  the papers works with no model reachable.
- `answers/notebooklm/*.md` and `answers/readcube/*.md` are templates to fill in
  by hand. Re-create them with `manual.py template TOOL`.
- The plumbing is tested offline in `tests/test_compare.py`.

## Running it (from `worth_your_read/`)
Steps 2 and 3 run the local model, and 1 and 5 call Bedrock.

1. **Check the judge first.** Run it against our 165 hand labels (about 40 Bedrock calls):
   ```
   WYR_USE_OLLAMA=0 WYR_USE_BEDROCK=1 AWS_REGION=us-east-1 \
   WYR_BEDROCK_MODEL=us.anthropic.claude-opus-4-5-20251101-v1:0 AWS_BEARER_TOKEN_BEDROCK=... \
   .venv/bin/python benchmark/compare/judge_pairs.py calibrate
   ```
   Go on only if keep/no agreement is high (90%+).
2. **Worth Your Read** (gemma4:26b, about 30 min for all 4):
   `WYR_USE_OLLAMA=1 WYR_USE_BEDROCK=0 WYR_OLLAMA_MODEL=gemma4:26b .venv/bin/python benchmark/compare/run_ours.py`
3. **PaperQA2** (same model):
   `.venv-paperqa/bin/python benchmark/compare/run_paperqa.py`
   - It uses PaperQA2's defaults: 10 evidence pieces and up to 5 sources per answer.
   - Retrieval is keyword ("sparse"), because no Ollama embedding model is installed.
   - For dense retrieval, run `ollama pull nomic-embed-text` and set
     `PQA_EMBEDDING=ollama/nomic-embed-text`.
   - `PQA_MAX_SOURCES=20` lets it cite as widely as we do. Run it both ways
     if the default looks like it's holding it back.
4. **By hand** (NotebookLM, ReadCube Papers, ...):
   - Load the question's `papers/<id>/NN.txt` (or the same PDFs) into the tool.
   - Ask the question word for word.
   - Fill in `answers/<tool>/<id>.md` as its header explains: the answer with [n]
     citations, then `[n] P3 | passage` per citation.
   - Then run `.venv/bin/python benchmark/compare/manual.py parse <tool>`.
5. **Judge** every answer with the same Bedrock environment as step 1:
   `... benchmark/compare/judge_pairs.py run`
   Results are cached, so re-running only judges new sentences.
6. **Score:** `.venv/bin/python benchmark/compare/score_compare.py --each`

## What the table means
- **precision:** the share of citations that back their sentence, wholly or in part.
- **wrong:** citations that don't back their sentence.
- **uncited:** sentences with no citation at all.
- **supported:** sentences whose citations together back all of it, counted over
  every sentence. Uncited sentences count as unsupported, so a tool can't score
  well by citing less.
- **not in paper:** cited passages that are not in the paper's text, plus
  citations to no paper (made up, or cited to the wrong paper). Checked with
  plain text matching, not the judge.
- **papers cited** and **minutes:** coverage and cost.

## Caveats
- 4 questions, chosen by us, from our own runs, so they may suit our tool.
  Add questions from papers it hasn't seen before drawing conclusions.
- A different unit of answer: ours cites findings quoted word for word, while
  PaperQA2 cites 5000-character chunks. A long chunk is easier to call "partly"
  supporting, which works in PaperQA2's favour on precision.
- One judge model. The calibration step shows how far its labels can be trusted.
- PaperQA2 was built and benchmarked on GPT-4-class models. On gemma4:26b it may
  do worse than its published scores, which is the point of this test but
  should be said when reporting.
