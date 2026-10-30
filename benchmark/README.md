# Claim-grounding benchmark

A gold set of claim-paper pairs with the verdict each pair *should* get, plus a
scorer that runs them through the real pipeline and quantifies how the tool
does. It measures the thing Worth Your Read is for: **is a specific claim
actually grounded in the paper it cites — and does the tool catch overstatement,
unsupported claims, retractions, and wrong-paper citations?**

## Files
- `gold.jsonl` — one pair per line: `claim`, `reference` (DOI), optional
  `expected_title`, a `gold` label (`SUPPORTED` / `OVERSTATED` / `UNSUPPORTED` /
  `INSUFFICIENT`), optional `retracted` / `wrong_paper` flags, and a `rationale`
  stating why that label is correct. This is a **seed (v0)**: labels are
  human-reasoned from the papers' own text; grow it by appending lines.
- `run_benchmark.py` — runs each pair through `verify_claim` and scores it.

## Run
```bash
python -m worth_your_read.benchmark.run_benchmark
# or: python benchmark/run_benchmark.py
```
Keyless metrics (retraction recall, wrong-paper recall, citation resolution,
full-text retrieval) always run. Verdict metrics (accuracy, per-class P/R/F1,
confusion matrix, false-SUPPORTED rate) need a judge backend — set
`ANTHROPIC_API_KEY` (or `WYR_USE_BEDROCK`).

## The metric that matters
Besides accuracy, the scorer reports the **false-SUPPORTED rate**: how often an
OVERSTATED, UNSUPPORTED, or retracted claim is wrongly called SUPPORTED. That is
the dangerous error for a trust tool — waving through a claim the evidence
doesn't back — and it's the number to drive toward zero.

## How this relates to existing benchmarks
Scientific-claim and citation benchmarks exist, but each covers one slice; this
one is scoped to the intersection that a citation-trust tool has to get right.

- **SciFact** (Wadden et al., EMNLP 2020) — verify a claim against **abstracts**,
  labels SUPPORTS / REFUTES / NOINFO with rationale sentences (~1.4K claims). The
  closest ancestor, but abstract-level, three flat labels (no *overstated*
  gradation), and no retraction or citation-integrity dimension.
  <https://arxiv.org/abs/2004.14974>
- **Spin / DeSpin** (BioNLP 2020; and a 2024 GPT-4 study) — detect "spin"
  (overstatement) in **RCT abstracts**. Validates that overstatement is a real,
  studied failure mode — our `OVERSTATED` label — but it's biomedical-RCT
  specific and abstract-level, not a general claim↔paper grounding task.
  <https://aclanthology.org/2020.bionlp-1.5>
- **ALCE** (Gao et al., EMNLP 2023) — does a generated answer's citations support
  what it wrote? Attribution quality over retrieved passages, not results-level
  scientific grounding or overstatement. <https://arxiv.org/abs/2305.14627>
- **CiteME** (Press et al., NeurIPS 2024) — can a model *find* the paper behind a
  claim? The recall/attribution axis (what an iterative search tool optimises),
  orthogonal to verifying support.
  <https://proceedings.neurips.cc/paper_files/paper/2024/hash/0ef47f7b768e1a012e3d995ac8d8fac7-Abstract.html>
- **CiteAudit / CiteCheck** (2026) — detect **fabricated / hallucinated**
  references (does the citation point to a real paper, metadata intact). This is
  our wrong-paper/citation-integrity dimension — and notably, retraction status
  is *not* a focus of any of them.

**Where this benchmark is different:** it is the only one (of those surveyed)
that scores, together, (1) a graded support verdict with **overstatement** as a
first-class label, (2) grounding in **full-text results**, not just abstracts,
(3) **retraction-awareness**, and (4) **citation integrity / wrong-paper**, under
a single product-oriented **false-SUPPORTED** safety metric. It is a small seed,
not a peer-reviewed corpus; SciFact-scale claim sets (and the DeSpin spin corpus)
are the natural source to grow it.
