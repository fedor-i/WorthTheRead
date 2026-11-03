# Worth Your Read

An MCP tool that verifies an answer's claims are actually supported by the
publications they cite. For each claim it: resolves the link, checks the paper
isn't retracted, fetches the paper's **actual results** (not just the abstract),
and rules **SUPPORTED / OVERSTATED / UNSUPPORTED / INSUFFICIENT** — backed by
verbatim quotes that are *machine-verified* against the source text.

The point: give an agent a grounding layer so it stops citing papers that don't
say what it claims.

## Quick install (for a collaborator)

Install straight from GitHub — no clone needed:

```bash
pip install "git+https://github.com/fedor-i/WorthTheRead.git"
```

That installs the `worth-your-read` command (the MCP server). Then wire it into
an MCP client — e.g. Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "worth-your-read": {
      "command": "worth-your-read",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-...", "WYR_USE_BEDROCK": "0" }
    }
  }
}
```

Restart the client and the three tools appear. If `worth-your-read` isn't on
your PATH, use its full path (`pip show -f worth-your-read` locates it) or
`python -m worth_your_read.server`. No API key? If your MCP client supports
**sampling**, the judge runs on the client's own model — omit `ANTHROPIC_API_KEY`
and drop `WYR_USE_BEDROCK`.

## Tools

- **`verify_claim(claim, reference, expected_title="")`** — verify one claim
  against a DOI / PMID / PMCID / URL. Returns structured fields plus a
  ready-to-show markdown `card`.
- **`score_answer(claims, min_confidence=0.0)`** — `claims` is a list of
  `{claim, reference}`. Verifies each, then returns an overall confidence score
  and a ranked **"worth your read"** source list; `min_confidence` screens out
  sources below your bar.
- **`find_sources(query, limit=3, scope="", peer_reviewed_only=False,
  iterative=True)`** — discover real papers for a query across OpenAlex,
  Crossref & Europe PMC in parallel, deduped by DOI and trust-ranked (provider
  agreement, query-match rank, citations, recency, open access, evidence tier).
  Iterative (round 2) follows the **citation trail** of the strongest hits —
  the works they reference plus OpenAlex related works — and folds in papers
  **co-cited by several seeds**, lifting recall on complex questions while the
  order stays deterministic. Retracted papers are dropped. Each source is
  flagged with `cocite` and `via_trail`. A leading `!bang` (`!oa` / `!crossref`
  / `!epmc` / `!pubmed`) scopes it to one provider.
- **`revise_claim(claim, reference, max_rounds=2, broaden=True, want=2)`** —
  the self-improvement loop. Verifies the claim, then each round **revises** it
  (if OVERSTATED/UNSUPPORTED) to match the evidence and **broadens** it by
  discovering up to `want` corroborating papers not already cited — re-verifying
  until the claim is supported by enough independent sources or `max_rounds` is
  hit. Returns the final claim, aggregate verdict + strength, the revision
  trail, and every reference's verdict (discovered ones flagged).
- **`plan_search(question, answers=None)`** — clarify-then-optimize: returns
  clarifying questions, then (with answers) an optimized search plan.

## How it works

| Stage | Source |
| --- | --- |
| Discover sources | OpenAlex + Crossref + Europe PMC + Semantic Scholar (parallel, keyless) + citation-trail round, deduped by DOI, trust-ranked, retractions dropped |
| Resolve link + retraction check | Crossref (`updated-by` field) + NCBI ID Converter |
| Fetch real results | PMC BioC JSON → Europe PMC XML → preprint HTML (bioRxiv/medRxiv, then arXiv/ar5iv) → OA-publisher HTML → **OA PDF** (OpenAlex/Crossref/Semantic Scholar/Unpaywall, parsed with pypdf) → abstract (Europe PMC, then Semantic Scholar). PMCID recovered via Europe PMC when NCBI didn't link it |
| Judge | Claude (forced structured output) |
| Anti-hallucination guard | every quote must be an exact substring of the source, else the verdict is forced to INSUFFICIENT |

If no open-access full text is available, the check runs on the abstract only,
is clearly labelled as such, and confidence is capped.

## Setup

```bash
cd worth_your_read
/opt/homebrew/bin/python3.12 -m venv .venv      # needs Python 3.10+
./.venv/bin/pip install -r requirements.txt
cp .env.example .env                            # add your ANTHROPIC_API_KEY
```

## Auth backends

The judge reaches Claude one of two ways:

1. **Anthropic API key** (default) — set `ANTHROPIC_API_KEY`.
2. **AWS Bedrock** — set `WYR_USE_BEDROCK=1` (Claude Code's `CLAUDE_CODE_USE_BEDROCK=1`
   is honored too). Uses the standard AWS credential chain; set `WYR_AWS_PROFILE`
   to choose a profile and `AWS_REGION` for the region. You must also set
   `WYR_BEDROCK_MODEL` to an inference-profile id your account can invoke:

   ```bash
   aws bedrock list-foundation-models --by-provider anthropic   # find your id
   export WYR_USE_BEDROCK=1
   export WYR_BEDROCK_MODEL=us.anthropic.claude-sonnet-4-5-20250929-v1:0
   export AWS_REGION=us-east-1
   ```

   To force the API-key path even under Claude Code, set `WYR_USE_BEDROCK=0`.
   The Bedrock IAM principal needs `bedrock:InvokeModel`, and calls bill your
   AWS account.
3. **Local Ollama** — set `WYR_USE_OLLAMA=1` and `WYR_OLLAMA_MODEL`
   (e.g. `gemma4:26b`). Nothing leaves the machine except the paper lookups
   themselves. The local judge reads the abstract plus the claim's best-matching
   passages (`WYR_OLLAMA_FOCUS_CHARS`, default 12000; `0` reads everything);
   quotes are still verified against the full text. Rewriting claims
   (`revise_claim`) also runs locally.

## Live website (GitHub Pages)

`docs/` is a **static, client-side** web app — no server. Each visitor pastes
their *own* Anthropic API key (kept only in their browser's localStorage); the
whole pipeline (Crossref + Europe PMC retrieval, the judge via a direct call to
the Anthropic API, the quote-verification guard, threshold, worth-your-read)
runs in the browser.

Enable it: GitHub → **Settings → Pages → Build and deployment → Deploy from a
branch → `main` / `docs`**. The site is then served at
`https://fedor-i.github.io/WorthTheRead/`.

Notes: retrieval uses CORS-enabled endpoints (Crossref, Europe PMC); the judge
calls the Anthropic API directly from the browser (`anthropic-dangerous-direct-
browser-access`), so the visitor's key is exposed to the page — standard for a
bring-your-own-key tool, but say so clearly. Link checks and retraction
detection work without a key; only the judge step needs one.

## Local web app (FastAPI, self-host)

`worth_your_read/web/` is a server version that keeps the key on the server
instead of in the browser:

```bash
pip install "worth-your-read[web] @ git+https://github.com/fedor-i/WorthTheRead.git"
export ANTHROPIC_API_KEY=sk-ant-...   WYR_USE_BEDROCK=0
uvicorn worth_your_read.web.app:app --reload   # http://127.0.0.1:8000
```

## Local sidecar (for the Zotero plugin)

A small HTTP API on `127.0.0.1:23120` (`WYR_SIDECAR_PORT`) with the same
engine, plus papers already on disk (a Zotero attachment's text cache or any
PDF, readable offline), grounded "ask this paper" answers and summaries, and an
on-disk cache in `WYR_DATA_DIR` (default `~/Library/Application Support/WorthYourRead`).

```bash
pip install "worth-your-read[sidecar] @ git+https://github.com/fedor-i/WorthTheRead.git"
WYR_USE_OLLAMA=1 WYR_OLLAMA_MODEL=gemma4:26b wyr-sidecar
```

The Zotero plugin starts `wyr-sidecar` itself when it first needs it (with
Ollama and `gemma4:26b`) and stops it when Zotero quits, so running it by hand
is only needed for other clients or custom environment variables.

Every call needs the header `X-WYR-Token` (the token is created on first run in
`$WYR_DATA_DIR/sidecar.token`, readable only by you). Model work runs as jobs,
one at a time: `POST /jobs {"kind": ..., "args": ...}` returns a `job_id`; poll
`GET /jobs/{job_id}`. Kinds: `verify`, `ask`, `ask_many` (one question across several papers; optional `max_papers`, default all), `summarize`, `revise`, `suggest`,
`find`, `plan`, `locate` (on-page rectangles of quotes, for highlights),
`research` (snowball the citation graph from a question until it runs dry; reports `progress`).
Research uses OpenAlex heavily; without a key OpenAlex allows a small daily
request budget, so set `WYR_OPENALEX_API_KEY` (and `WYR_CONTACT_EMAIL`) for regular use.
OpenAlex responses are cached on disk, so repeat searches cost nothing; a
fresh search typically needs ~50–110 requests (citing papers are looked up 25
at a time). To stay on topic, the local model scores each candidate 0–3 against
the question's *outcome*, and a weaker match (2) is only followed one citation
hop from a direct hit; further out it must be cited by two accepted papers. The
summary says when either limit shaped the result. Optional: `WYR_SEMANTIC_STAGE1=1`
(with the `semantic` extra, scikit-learn) pre-ranks candidates by TF-IDF + LSA
closeness to the question and the papers that answer it directly, instead of
word overlap — on the metformin benchmark it found more direct hits (40 vs 29)
at the same recall and model cost, so it's off by default. Duplicate records of one
paper and correction notices are collapsed. A paper is `{"attachment_key": "ABCD1234"}`, `{"pdf_path": ...}`
or `{"reference": "<DOI>"}`; verified quotes carry the `page` they're on.

## Tests

```bash
# from the project root (one level up); offline, no model needed
./worth_your_read/.venv/bin/python -m pytest worth_your_read/tests
```

## Smoke test

```bash
# from the project root (one level up)
./worth_your_read/.venv/bin/python -m worth_your_read.smoke_test
```

Retrieval + retraction detection run without a key; the judge path runs when
`ANTHROPIC_API_KEY` is set.

## Run as an MCP server (stdio)

```bash
./worth_your_read/.venv/bin/python -m worth_your_read.server
```

Wire it into an MCP client (e.g. Claude Desktop `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "worth-your-read": {
      "command": "/absolute/path/to/worth_your_read/.venv/bin/python",
      "args": ["-m", "worth_your_read.server"],
      "cwd": "/absolute/path/to/ScienceJudge",
      "env": { "ANTHROPIC_API_KEY": "sk-ant-..." }
    }
  }
}
```

## Status

Retrieval, retraction detection, scoring, and card rendering are verified
against live APIs. The judge path (the Claude call) is written but needs an
`ANTHROPIC_API_KEY` to exercise end-to-end.

## Limitations (MVP)

- Open-access full text only (PMC OA); paywalled papers fall back to abstract.
- No PDF parsing, embeddings/RAG (text is truncated), or multi-citation-per-claim.
- The judge is evidence-locating, not peer review; it flags when a claim needs
  domain expertise it lacks.
