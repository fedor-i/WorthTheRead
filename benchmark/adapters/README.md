# External-benchmark adapters

Worth Your Read's primitives already cover the core of the public
claim/citation benchmarks — they just speak different I/O. An adapter maps a
benchmark's data onto our primitives and maps our output back to its label
space. The primitives:

- **judge on supplied text** — `verify_claim(claim, evidence_text=...)` rules
  SUPPORTED / OVERSTATED / UNSUPPORTED / INSUFFICIENT against text handed in
  directly (no DOI fetch), quote-guard still applied.
- **discovery** — `find_sources(query, iterative=True)` retrieves + trust-ranks
  real papers (OpenAlex + Crossref + Europe PMC + citation trail).
- **resolve / integrity** — `resolve()` gives citation_ok, resolved(bool),
  retracted, title-match: a reference-integrity signal.

| Benchmark | Their task | Our primitive | Label mapping | Status |
|---|---|---|---|---|
| **SciFact** | claim vs abstract → SUPPORT/CONTRADICT/NOINFO | judge on supplied text | SUPPORTED→SUPPORT, UNSUPPORTED→CONTRADICT, INSUFFICIENT/OVERSTATED→NOINFO | **implemented** (`scifact.py`) |
| **DeSpin / spin** | is an RCT abstract's conclusion spin? | judge: claim = stated conclusion, text = results section | OVERSTATED (esp. distortion=effect_size_spin) → spin; SUPPORTED → no spin | planned |
| **ALCE** | is each generated statement supported by its cited passage? | judge on supplied text, per (statement, passage) | SUPPORTED → citation supported; else not | planned |
| **CiteAudit / CiteCheck** | is a reference real or fabricated/corrupted? | resolve / integrity | resolved & citation_ok & not retracted → valid; else hallucinated/invalid | planned |
| **CiteME** | find the paper a claim cites | discovery (`find_sources`) | gold DOI in top-k candidates → hit; score recall@k | planned |

Each adapter downloads its own data on demand into `scifact_data/` etc.
(git-ignored) and is runnable as `python -m worth_your_read.benchmark.adapters.<name>`.
Verdict-based adapters need a judge backend (`ANTHROPIC_API_KEY` or Bedrock);
the integrity (CiteAudit/CiteCheck) and retrieval (CiteME) adapters are keyless.

Note on comparability: these run our general-purpose judge/discovery zero-shot,
not systems tuned to each benchmark's leaderboard — the point is to measure our
tool on established tasks, not to top their leaderboards.
