"""Worth Your Read — MCP server.

Tools an agent can call to vet its own citations (each result leads with a
plain-English `summary` and a markdown `card`; see the contract in render.py):
  - verify_claim: resolve the reference, check retraction, fetch the real
    results and judge the claim with quote-verified evidence.
  - revise_claim / suggest_sources: fix a weak claim (reword it to the
    evidence, add corroborating papers) or find a citation that supports it.
  - score_answer: verify many claims; overall grade + ranked "worth your read".
  - audit_paper: check a paper's own cited sentences against their sources.
  - find_sources / plan_search: discover papers; scope a question first.
  - ask_papers: answer a question from several papers, every finding quoted.

The judge runs "through this chat" by default: the server asks the HOST's model
via MCP sampling (no API key, no local model). With WYR_USE_OLLAMA set it runs
on a local Ollama model instead. If the client doesn't support sampling, it
falls back to an ANTHROPIC_API_KEY / Bedrock call, and if neither is available
it returns INSUFFICIENT with a clear note. The quote-verification guard runs
regardless of backend.

Run (stdio):  python -m worth_your_read.server
"""
import asyncio
import re
from collections import Counter

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import SamplingMessage, TextContent

from . import answer as _answer
from . import audit as _audit
from . import config as _config
from . import discover as _discover
from . import fetch as _fetch
from . import judge as _judge
from . import plan as _plan
from . import render as _render
from . import research as _research
from . import resolve as _resolve
from . import score as _score

mcp = FastMCP("worth-your-read", instructions=(
    "Verify scientific claims against the papers they cite, grounded in the "
    "papers' real results (not just abstracts). Every result leads with a "
    "plain-English `summary`; read `card` for the formatted detail.\n\n"
    "Typical flow:\n"
    "1. verify_claim(claim, reference) — is THIS claim supported by THIS paper? "
    "Flags retractions, overstatement, and wrong-paper citations. Pass "
    "evidence_text=... to check against text you already have.\n"
    "2. If a claim is OVERSTATED or UNSUPPORTED, fix it: revise_claim rewrites "
    "it to match the evidence; suggest_sources finds a citation that actually "
    "supports it (pass the failing DOI as `exclude`).\n"
    "3. score_answer([{claim, reference}, ...]) — grade a whole answer and get a "
    "ranked 'worth your read' source list.\n"
    "4. audit_paper(reference) — check a paper's own introduction against the "
    "sources it cites (fix=True also suggests replacements).\n"
    "5. find_sources(query) discovers papers; plan_search scopes a question "
    "before verifying.\n"
    "6. ask_papers(question, references) answers a question from several "
    "papers, each finding quoted and checked word for word (slow)."))


async def _sample(ctx, system: str, user: str, max_tokens: int) -> str:
    """Ask the host's model via MCP sampling; returns its text ("" if none)."""
    res = await ctx.session.create_message(
        messages=[SamplingMessage(role="user",
                                  content=TextContent(type="text", text=user))],
        system_prompt=system, max_tokens=max_tokens)
    return getattr(res.content, "text", "") or ""


async def _run_judge(ctx, claim: str, fetched: dict) -> dict:
    """Local Ollama (opt-in) first; else host sampling; else api/bedrock."""
    if not fetched.get("full_text"):
        return _judge.no_text_result()

    # 0) Local model via Ollama — explicit opt-in (WYR_USE_OLLAMA), so it takes
    # precedence over host sampling: choosing a local judge is deliberate.
    if _config.USE_OLLAMA:
        try:
            out = _judge.judge_claim_ollama(claim, fetched)
            out["judge_backend"] = "ollama"
            return out
        except Exception as e:
            r = _judge.no_text_result(f"Local Ollama judge failed: {e}")
            r["judge_backend"] = "ollama_error"
            return r

    # 1) Through this chat: ask the host's model via MCP sampling.
    sampling_error = "no client context"
    if ctx is not None:
        try:
            text = await _sample(ctx, _judge.sampling_system(),
                                 _judge.build_user(claim, fetched), 1500)
            if text.strip():
                out = _judge.parse_json_verdict(text, fetched, claim)
                out["judge_backend"] = "client_sampling"
                return out
            sampling_error = "empty sampling response"
        except Exception as e:
            sampling_error = str(e)

    # 2) Fallback: ANTHROPIC_API_KEY / Bedrock.
    try:
        out = _judge.judge_claim(claim, fetched)
        out["judge_backend"] = "bedrock" if _config.USE_BEDROCK else "api"
        return out
    except Exception as e:
        # 3) No judge backend at all — be honest.
        r = _judge.no_text_result(
            "No judge backend available: the client did not support sampling "
            f"({sampling_error}) and no API/Bedrock credentials are set "
            f"({e}). Retrieval and retraction checks still ran.")
        r["judge_backend"] = "none"
        return r


_GENEROSITY = {"SUPPORTED": 3, "OVERSTATED": 2, "UNSUPPORTED": 1, "INSUFFICIENT": 0}
_NO_JUDGE = ("none", "n/a", None)   # judge_backend values where no judge ran


def _reconcile(results: list) -> dict:
    """Majority vote over self-consistency passes. Ties break toward the safer
    (less generous) verdict; confidence is scaled down when the passes disagree,
    so an unstable claim reads as lower-confidence rather than flipping."""
    if len(results) == 1:
        return results[0]
    verds = [r.get("verdict") for r in results]
    cnt = Counter(verds)
    top_n = max(cnt.values())
    tied = [v for v, c in cnt.items() if c == top_n]
    winner = min(tied, key=lambda v: _GENEROSITY.get(v, 0)) if len(tied) > 1 else tied[0]
    rep = max((r for r in results if r.get("verdict") == winner),
              key=lambda r: r.get("confidence") or 0)
    agree = cnt[winner] / len(results)
    rep["confidence"] = round((rep.get("confidence") or 0) * (0.6 + 0.4 * agree), 3)
    rep["passes"] = len(results)
    rep["pass_verdicts"] = verds
    return rep


async def _judge_consistent(ctx, claim: str, fetched: dict, passes: int) -> dict:
    """Run the judge `passes` times and reconcile — self-consistency to remove
    the run-to-run ambiguity on borderline claims. passes=0 is "auto": judge
    once, and only a borderline result (not High confidence, or OVERSTATED,
    where the judge varies most) is judged twice more and put to the vote."""
    if passes == 0:
        first = await _run_judge(ctx, claim, fetched)
        if first.get("judge_backend") in _NO_JUDGE or (
                first.get("band") == "High" and first.get("verdict") != "OVERSTATED"):
            return first
        results = [first] + [await _run_judge(ctx, claim, fetched) for _ in range(2)]
        return _reconcile(results)
    passes = max(1, min(5, passes))
    if passes == 1:
        return await _run_judge(ctx, claim, fetched)
    results = [await _run_judge(ctx, claim, fetched) for _ in range(passes)]
    if all(r.get("judge_backend") in _NO_JUDGE for r in results):
        return results[0]   # no backend actually ran — don't fake a vote
    return _reconcile(results)


def _finish(result: dict) -> dict:
    """Add the plain-English summary and the markdown card to a verdict."""
    result["summary"] = _render.summary(result)
    result["card"] = _render.render_card(result)
    return result


async def _verify(claim: str, reference: str, expected_title: str = "",
                  ctx=None, evidence_text: str = "", passes: int = 1) -> dict:
    """verify_claim's core, shared by every tool, the web app and the sidecar."""
    # Supplied-text mode: judge the claim against text handed in directly (a
    # pasted passage, or a benchmark's gold abstract) with no DOI fetch. The
    # quote-verification guard still runs against this text.
    if evidence_text:
        fetched = {"full_text": "## TEXT\n" + evidence_text,
                   "sections": {"abstract": evidence_text},
                   "sections_available": ["abstract"], "has_results": False,
                   "open_access": False, "source": "supplied_text"}
        result = await _judge_consistent(ctx, claim, fetched, passes)
        result.update({
            "claim": claim, "reference": reference or "(supplied text)",
            "citation_ok": None, "resolved": {}, "retracted": False,
            "concern": False, "is_preprint": False, "published_as": None,
            "has_preprint": None, "verified_from_preprint": None,
            "verified_reference": None, "sections_available": ["abstract"],
            "has_results": False, "has_fulltext": False, "open_access": False,
            "fetch_source": "supplied_text", "resolve_error": None,
        })
        return _finish(result)

    resolved = _resolve.resolve(reference, expected_title or None)

    # Prefer the published version of record: if the cited reference is a
    # preprint that later cleared peer review, verify the claim against the
    # published paper instead, recording where we started.
    verified_from_preprint = None
    pub = resolved.get("published_as") if resolved.get("is_preprint") else None
    if pub and pub.get("doi"):
        pub_resolved = _resolve.resolve(pub["doi"])
        if pub_resolved.get("resolved"):
            verified_from_preprint = resolved.get("doi")
            resolved = pub_resolved

    fetched = _fetch.fetch_text(resolved)

    if resolved.get("retracted"):
        result = {
            "verdict": "UNSUPPORTED", "confidence": 0.9, "band": "High",
            "evidence": [], "evidence_rejected": [], "aspects": [],
            "distortion_type": "none",
            "distortion_note": "",
            "claim_aspects_unmet": ["The cited paper is retracted."],
            "rationale": "Source is retracted; it cannot support the claim.",
            "quotes_verified": 0, "downgraded": False, "judge_backend": "n/a",
        }
    else:
        result = await _judge_consistent(ctx, claim, fetched, passes)

    result.update({
        "claim": claim,
        "reference": reference,
        "citation_ok": resolved.get("citation_ok", False),
        "resolved": {k: resolved.get(k) for k in
                     ("title", "authors", "year", "journal", "doi", "pmcid")},
        "retracted": resolved.get("retracted", False),
        "concern": resolved.get("concern", False),
        "is_preprint": resolved.get("is_preprint", False),
        "published_as": resolved.get("published_as"),
        "has_preprint": resolved.get("has_preprint"),
        "verified_from_preprint": verified_from_preprint,
        "verified_reference": resolved.get("doi") or reference,
        "sections_available": fetched.get("sections_available", []),
        "has_results": fetched.get("has_results", False),
        "has_fulltext": fetched.get("has_fulltext", False),
        "open_access": fetched.get("open_access", False),
        "fetch_source": fetched.get("source"),
        "resolve_error": resolved.get("error"),
    })
    return _finish(result)


@mcp.tool()
async def verify_claim(claim: str, reference: str = "", expected_title: str = "",
                       evidence_text: str = "", passes: int = 1,
                       html: bool = False, ctx: Context = None) -> dict:
    """Verify a scientific CLAIM against the REFERENCE it cites.

    Checks that the link resolves to a real, non-retracted paper, fetches the
    publication's actual results (not just the abstract), and rules whether the
    claim is SUPPORTED / OVERSTATED / UNSUPPORTED / INSUFFICIENT, backed by
    verbatim quotes machine-verified against the source text. The judgement
    runs on the host's own model via MCP sampling when available.

    Args:
        claim: the specific factual claim to check.
        reference: a DOI, PMID, PMCID, or URL for the cited paper.
        expected_title: optional claimed title, to catch wrong-paper citations.
        evidence_text: verify against this text directly instead of fetching a
            reference — for a pasted passage, or when you already hold the
            source text (e.g. a benchmark's gold abstract). The quote guard
            still runs against it.
        passes: self-consistency — judge this many times (odd, e.g. 3) and take
            the majority verdict, to remove run-to-run ambiguity on borderline
            claims; confidence drops when the passes disagree. Default 1.
            0 = auto: judge once, and three times only when the first result
            is borderline (not High confidence, or OVERSTATED).
        html: also return an `html` field — a self-contained, styled page of the
            verdict with verified quotes and links to the full text, to open or
            share. Off by default so the response stays compact.
    """
    result = await _verify(claim, reference, expected_title, ctx,
                           evidence_text=evidence_text, passes=passes)
    if html:
        result["html"] = _render.verdict_html(result)
    return result


@mcp.tool()
async def score_answer(claims: list, min_confidence: float = 0.0,
                       html: bool = False, ctx: Context = None) -> dict:
    """Score a whole answer. Each item is {claim, reference} (optionally
    expected_title). Verifies every claim, then returns an overall confidence
    score and a ranked "worth your read" source list.

    min_confidence (0-1): sources whose supporting verdict falls below this are
    screened out of the list entirely, so you can gather many candidate
    references and keep only the ones that clear your bar.

    html: also return an `html` field — a self-contained scorecard page (overall
    grade, the ranked "worth your read" list with links, and every claim's
    verdict card). Off by default so the response stays compact.
    """
    results = [await _verify(c.get("claim", ""), c.get("reference", ""),
                             c.get("expected_title", ""), ctx) for c in claims]
    scored = _score.score_answer(results, min_confidence=min_confidence)
    scored["summary"] = _render.score_summary(scored)
    scored["card"] = _render.render_scorecard(scored)
    scored["claims"] = results
    if html:
        scored["html"] = _render.scorecard_html(scored, results)
    return scored


async def _run_revise(ctx, claim: str, result: dict):
    """Propose a tightened claim: local Ollama (opt-in) first, then host
    sampling, then api/bedrock — the same order as the judge."""
    if _config.USE_OLLAMA:
        try:
            return _judge.revise_claim_ollama(claim, result) or None
        except Exception:
            return None
    if ctx is not None:
        try:
            text = await _sample(ctx, _judge.revise_system(),
                                 _judge.build_revise_user(claim, result), 400)
            if text.strip():
                return _judge._clean_revision(text)
        except Exception:
            pass
    try:
        return _judge.revise_claim_api(claim, result)
    except Exception:
        return None


def _aggregate(results: list) -> dict:
    """Roll a claim's per-reference verdicts into one, the way the site does:
    SUPPORTED wins if any source supports it; strength grows with the number of
    independent supporting sources."""
    sup = [r for r in results if r["verdict"] == "SUPPORTED"]
    over = [r for r in results if r["verdict"] == "OVERSTATED"]
    unsup = [r for r in results if r["verdict"] == "UNSUPPORTED"]
    verdict = ("SUPPORTED" if sup else "OVERSTATED" if over
               else "UNSUPPORTED" if unsup else "INSUFFICIENT")
    support_count = len(sup)
    strength = ("Strong" if support_count >= 2
                else "Limited" if support_count == 1 else "None")
    confidence = (max((r.get("confidence") or 0) for r in sup) if sup
                  else max((r.get("confidence") or 0) for r in results)
                  if results else 0.0)
    return {"verdict": verdict, "support_count": support_count,
            "total_refs": len(results), "strength": strength,
            "confidence": confidence}


def _worst_result(results: list) -> dict:
    """The result a revision should target: an OVERSTATED one first, then
    UNSUPPORTED, else the primary reference."""
    for want in ("OVERSTATED", "UNSUPPORTED"):
        for r in results:
            if r["verdict"] == want:
                return r
    return results[0]


def _ref_id(r: dict) -> str:
    """The identifier a verdict was checked against: DOI, else PMCID, else
    the reference as given."""
    res = r.get("resolved") or {}
    return res.get("doi") or res.get("pmcid") or r.get("reference")


def _cand_key(c: dict) -> str:
    """Dedupe key for a discovered candidate (lowercased DOI or PMCID)."""
    return (c.get("doi") or c.get("pmcid") or "").lower()


def _cited_keys(results: list) -> set:
    """Lowercased DOIs and PMCIDs already cited by these verdicts."""
    keys = set()
    for r in results:
        res = r.get("resolved") or {}
        if res.get("doi"):
            keys.add(res["doi"].lower())
        if res.get("pmcid"):
            keys.add(res["pmcid"].lower())
    return keys


async def _strengthen(claim: str, existing: list, want: int, ctx) -> list:
    """Discover corroborating papers not already cited and verify the claim
    against them, keeping going until `want` of them SUPPORT the claim (or the
    candidate pool is exhausted). Mirrors the site's strengthenClaim."""
    have = _cited_keys(existing)
    cands = _discover.discover_sources(claim, n=want + len(have) + 6)
    cands = _discover.triage(claim, cands, keep=want + len(have) + 3)
    out, supported = [], 0
    for c in cands:
        ref = c.get("doi") or c.get("pmcid")
        if not ref:
            continue
        if (c.get("doi") or "").lower() in have \
                or (c.get("pmcid") or "").lower() in have:
            continue
        v = await _verify(claim, ref, c.get("title") or "", ctx)
        v["discovered"] = True
        if v["verdict"] == "INSUFFICIENT":
            continue  # couldn't fetch/judge it — don't count or clutter
        out.append(v)
        if v["verdict"] == "SUPPORTED":
            supported += 1
        if supported >= want or len(out) >= want + 4:
            break
    return out


@mcp.tool()
def find_sources(query: str, limit: int = 3, scope: str = "",
                 peer_reviewed_only: bool = False, iterative: bool = True,
                 ctx: Context = None) -> dict:
    """Discover real papers for a query across OpenAlex, Crossref & Europe PMC.

    Round 1 fans out to all three (keyless), dedupes by DOI, and trust-ranks the
    candidates by a pure function of explicit signals — provider agreement,
    query-match rank, citations, recency, open access, evidence tier. Round 2
    (iterative=True) follows the citation trail of the strongest hits — the
    papers they reference and OpenAlex's related works — and folds in those
    co-cited by several seeds, lifting recall on complex questions the way an
    agent-style search does, while the order stays deterministic. Retracted
    papers are dropped. Feed the returned DOIs into verify_claim / score_answer,
    or let revise_claim pull them automatically.

    Each source is flagged with the providers that found it ("openalex-cite"
    means it came from the citation trail), its `cocite` count (how many seeds
    cited it), and `via_trail`.

    Args:
        query: a few keywords describing the claim or topic. A leading !bang
            scopes it to one provider: !oa, !crossref, !epmc, !pubmed.
        limit: how many ranked candidates to return.
        scope: force a single provider ("openalex" / "crossref" / "epmc"); this
            also skips the citation trail.
        peer_reviewed_only: drop preprints (bioRxiv/medRxiv/arXiv).
        iterative: follow the citation trail for higher recall (default True).
    """
    cands = _discover.discover_sources(
        query, n=max(1, limit), scope=(scope or None),
        peer_reviewed_only=peer_reviewed_only, rounds=(2 if iterative else 1))
    sources = [{
        "doi": c.get("doi"), "pmcid": c.get("pmcid"), "pmid": c.get("pmid"),
        "title": c.get("title"), "year": c.get("year"), "venue": c.get("venue"),
        "citations": c.get("citations"), "providers": c.get("providers"),
        "cocite": c.get("cocite", 0),
        "via_trail": c.get("providers") == ["openalex-cite"],
        "open_access": c.get("is_oa"), "trust_score": round(c.get("_score", 0), 2),
    } for c in cands]
    out = {"query": query, "count": len(sources), "sources": sources}
    return {"summary": _render.sources_summary(out), **out,
            "card": _render.sources_card(out)}


@mcp.tool()
async def revise_claim(claim: str, reference: str, expected_title: str = "",
                       max_rounds: int = 2, broaden: bool = True,
                       want: int = 2, ctx: Context = None) -> dict:
    """Verify a claim and run the self-improvement loop on it to convergence.

    Each round the loop can do two things, the same way the site does:
      - revise: if the claim is OVERSTATED or UNSUPPORTED, rewrite it to match
        what the evidence actually establishes and re-verify every reference.
      - broaden: discover up to `want` corroborating papers across OpenAlex,
        Crossref & Europe PMC that aren't already cited and verify the claim
        against them — so a single-source claim becomes multi-source.
    It stops when the claim is supported by `want` independent sources (and
    needs no revision), when nothing changes, or at max_rounds.

    Returns the final (possibly tightened) claim, its aggregate verdict and
    strength, the revision trail, every reference's verdict (discovered ones
    flagged), and the primary verdict card.

    Args:
        claim: the factual claim to check and improve.
        reference: a DOI / PMID / PMCID / URL the claim already cites.
        expected_title: optional claimed title, to catch wrong-paper citations.
        max_rounds: cap on improve rounds (each adds model calls).
        broaden: find corroborating sources (set False to only revise).
        want: how many independent supporting sources to aim for.
    """
    trail, added_refs = [], []
    current = claim
    results = [await _verify(current, reference, expected_title, ctx)]

    def discovered_supporting():
        return sum(1 for r in results
                   if r.get("discovered") and r["verdict"] == "SUPPORTED")

    for round_i in range(max(0, max_rounds) + 1):
        agg = _aggregate(results)
        trail.append({"claim": current, "verdict": agg["verdict"],
                      "strength": agg["strength"],
                      "support_count": agg["support_count"],
                      "total_refs": agg["total_refs"]})
        need_revise = agg["verdict"] in ("OVERSTATED", "UNSUPPORTED")
        need_broaden = broaden and discovered_supporting() < want
        no_backend = bool(results) and results[0].get("judge_backend") == "none"
        if (not need_revise and not need_broaden) \
                or round_i >= max_rounds or no_backend:
            break
        changed = False
        if need_revise:
            revised = await _run_revise(ctx, current, _worst_result(results))
            if revised and revised.strip() != current.strip():
                current = revised
                re_results = []
                for r in results:
                    nv = await _verify(current, _ref_id(r), "", ctx)
                    nv["discovered"] = r.get("discovered", False)
                    re_results.append(nv)
                results = re_results
                changed = True
        if need_broaden:
            fresh = await _strengthen(current, results,
                                      want - discovered_supporting(), ctx)
            if fresh:
                results.extend(fresh)
                added_refs += [_ref_id(f) for f in fresh]
                changed = True
        if not changed:
            break

    agg = _aggregate(results)
    primary = results[0]
    out = {
        "revised": _judge._norm(current) != _judge._norm(claim),
        "summary": "",
        "original_claim": claim,
        "claim": current,
        "verdict": agg["verdict"],
        "confidence": agg["confidence"],
        "strength": agg["strength"],
        "support_count": agg["support_count"],
        "total_refs": agg["total_refs"],
        "added_refs": added_refs,
        "revisions": trail,
        "references": results,
        "verification": primary,
        "card": "",
    }
    out["summary"] = _render.revise_summary(out)
    out["card"] = _render.revise_card(out)
    return out


_STOP = set(
    "a an the of in on for to and or but with without is are was were be been "
    "being this that these those by from as at into than then so not no any all "
    "can could may might also more most using used use via per between within "
    "across their its it they we our new recent here show shows showed suggest "
    "suggests found find results result study paper enables enable enabling "
    "provides provide providing protects protect protecting acts act acting "
    "plays play role roles function functions mechanism mechanisms process "
    "processes reaction centre center effect effects increase increases reduce "
    "reduces improve improves associated related secondary primary pathway "
    "pathways involved involves".split())


def _keywords(text: str, k: int = 6) -> str:
    """A broader keyword query from a claim — the full sentence is often too
    specific to retrieve well. Keep the most DISTINCTIVE terms (hyphenated /
    numeric / longer are more technical) and only a few of them, so this query
    casts a different, wider net than the claim itself."""
    seen, terms = set(), []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", (text or "").lower()):
        if w in _STOP or w in seen:
            continue
        seen.add(w)
        terms.append(w)
    terms.sort(key=lambda w: ("-" in w or any(c.isdigit() for c in w), len(w)),
               reverse=True)
    return " ".join(terms[:k])


async def _suggest_sources(claim: str, n: int, exclude: str, breadth: int,
                           ctx) -> dict:
    skip = (exclude or "").lower().strip().replace("doi:", "")
    pool = max(12, breadth * 3)   # discovery is cheap; cast wide, verify few
    cands = _discover.discover_sources(claim, n=pool)
    seen = {_cand_key(c) for c in cands}
    kw = _keywords(claim)
    if kw and kw.lower() != claim.lower():
        for c in _discover.discover_sources(kw, n=pool):
            key = _cand_key(c)
            if key and key not in seen:
                seen.add(key)
                cands.append(c)
    # Triage on title + abstract first: full verification is the expensive
    # step, so spend it on the few candidates most likely to support the claim.
    cands = [c for c in cands if _cand_key(c) != skip]
    cands = _discover.triage(claim, cands, keep=breadth)
    found, checked = [], 0
    for c in cands:
        ref = c.get("doi") or c.get("pmcid")
        if not ref or ref.lower() == skip or checked >= breadth:
            continue
        checked += 1
        r = await _verify(claim, ref, ctx=ctx)
        if r.get("verdict") in ("SUPPORTED", "OVERSTATED"):
            res = r.get("resolved") or {}
            found.append({
                "doi": c.get("doi"), "pmcid": c.get("pmcid"),
                "title": res.get("title") or c.get("title"),
                "year": res.get("year") or c.get("year"), "venue": c.get("venue"),
                "verdict": r.get("verdict"), "confidence": r.get("confidence"),
                "has_results": r.get("has_results"),
            })
        if sum(1 for x in found if x["verdict"] == "SUPPORTED") >= n:
            break
    found.sort(key=lambda x: (x["verdict"] != "SUPPORTED", -(x["confidence"] or 0)))
    found = found[:n + 2]
    out = {"claim": claim, "checked": checked, "count": len(found), "suggested": found}
    return {"summary": _render.suggest_summary(out), **out,
            "card": _render.suggest_card(out)}


@mcp.tool()
async def suggest_sources(claim: str, n: int = 3, exclude: str = "",
                          breadth: int = 5, ctx: Context = None) -> dict:
    """Find real papers that actually SUPPORT a claim — to add a citation, or
    replace one that doesn't hold up.

    Casts a wide net: discovers candidates across OpenAlex, Crossref, Europe PMC
    & Semantic Scholar (plus the citation trail) from BOTH the full claim and a
    keyword-condensed query, merges them, then verifies the claim against each
    and returns the ones that support it, ranked (SUPPORTED before OVERSTATED,
    then by confidence). Unlike a single-source checker, this helps you FIX a
    weak citation, not just flag it — pass the failing DOI as `exclude`.

    Args:
        claim: the claim to find support for.
        n: how many supporting sources to return.
        exclude: a DOI to skip (e.g. the citation that already failed).
        breadth: how many candidate papers to fully verify after the cheap
            title + abstract triage (wider = more thorough, more model calls;
            each costs about a minute on a local model).
    """
    return await _suggest_sources(claim, n, exclude, breadth, ctx)


@mcp.tool()
async def audit_paper(reference: str, max_claims: int = 8,
                      section: str = "introduc", passes: int = 1,
                      fix: bool = False, ctx: Context = None) -> dict:
    """Audit a paper's own claims against the sources it cites.

    Pulls the paper's full text (Europe PMC JATS), takes a section (the
    introduction by default) and, for each sentence that cites a reference,
    verifies that claim against the paper the authors actually cited — using
    the inline citation links, so attribution is exact, not guessed. Returns a
    per-claim verdict list, a markdown `card`, and a verdict summary. Needs an
    open-access paper with full-text citation links; says so clearly otherwise.

    Args:
        reference: DOI / PMID / PMCID of the paper to audit.
        max_claims: cap on cited sentences to check (each is a judge call).
        section: section-title keyword to audit ("introduc", "discuss", ...).
        passes: self-consistency passes per claim, as in verify_claim.
        fix: for each claim its citation doesn't support, suggest a source
            that does (attached as the claim's `suggestion`).
    """
    async def vf(claim, ref):
        return await _verify(claim, ref, ctx=ctx, passes=passes)

    out = await _audit.run_audit(reference, vf, max_claims=max_claims,
                                 section=section)

    # Fix step: for each claim its own citation doesn't support, go find one
    # that does — so the audit hands back a replacement, not just a red flag.
    if fix and out.get("available"):
        for c in out.get("claims", []):
            if c.get("verdict") in ("OVERSTATED", "UNSUPPORTED", "INSUFFICIENT"):
                cited = (c.get("verified_against") or {}).get("doi", "")
                sug = await _suggest_sources(c["claim"], 1, cited, 3, ctx)
                top = next((s for s in sug["suggested"]
                            if s["verdict"] == "SUPPORTED"), None)
                if top:  # only offer a genuinely supporting replacement
                    c["suggestion"] = top

    out["summary"] = _render.audit_summary(out)
    out["card"] = _audit._render(out)
    return out


def ask_across(question: str, references: list = None, max_papers: int = 6,
               depth: str = "standard", progress=None) -> dict:
    """Answer a question from several papers (answer.ask_many): each paper is
    read, its findings quoted and checked word for word, then combined into
    claims with their sources and a short answer. With no references, the
    top `max_papers` papers find_sources returns for the question are read."""
    refs = [r for r in (references or []) if str(r).strip()]
    if not refs:
        refs = [s["doi"] for s in find_sources(question, limit=max_papers)["sources"] if s.get("doi")]
    papers, left_out = [], []
    for ref in refs:
        try:
            resolved = _resolve.resolve(ref)
            fetched = _fetch.fetch_text(resolved)
        except Exception as e:
            left_out.append({"label": ref, "key": "", "reason": f"load_failed: {type(e).__name__}"})
            continue
        title = re.sub(r"<[^>]+>", "", resolved.get("title") or "").strip()   # Crossref keeps <i> etc.
        papers.append({"label": title or ref, "key": "", "pages": [],
                       "doi": resolved.get("doi") or "", "fetched": fetched})

    def follow(doi, title):
        return {"label": title or doi, "key": "", "doi": doi, "pages": [],
                "fetched": _fetch.fetch_text(_resolve.resolve(doi, title or None))}
    _research.add_paper_facts(papers)          # citation counts pick papers; reviews don't vote
    r = _answer.ask_many(question, papers, progress=progress, max_papers=max_papers or None,
                         follow=follow, depth=depth if depth in _answer.DEPTHS else "standard")
    r["skipped"] = [x["label"] for x in left_out] + r["skipped"]
    r["left_out"] = left_out + r["left_out"]
    r["summary"] = _render.ask_summary(r)
    r["card"] = _render.ask_card(r)
    return r


@mcp.tool()
async def ask_papers(question: str, references: list = None, max_papers: int = 6,
                     depth: str = "standard", ctx: Context = None) -> dict:
    """Answer a research question from several papers at once, with every
    finding quoted from a paper and the quote checked word for word.

    Each paper is read for what it says on the question; the findings are
    combined into claims (merged by the model, or grouped by meaning and
    ranked; see WYR_COMBINE), each listing the papers (and quotes) behind it,
    the papers that disagree, and a short answer on top. Claims no quote backs
    are marked UNGROUNDED; numbers no quote contains are flagged.

    Slow: one model call per paper plus a few to merge, so it runs on the
    server's own backend (ANTHROPIC_API_KEY, Bedrock or a local Ollama model),
    not through this chat.

    Args:
        question: the research question.
        references: DOIs / PMIDs / URLs to read. Empty: find_sources picks them.
        max_papers: read at most this many (the most relevant to the question).
        depth: "quick", "standard" or "deep" (deep also reads key papers the
            chosen ones' introductions cite).
    """
    try:
        return await asyncio.to_thread(ask_across, question, references, max_papers, depth)
    except Exception as e:
        msg = _render.ask_error(e)
        return {"question": question, "error": f"{type(e).__name__}: {e}",
                "summary": msg, "card": msg}


@mcp.tool()
def plan_search(question: str, answers: dict = None) -> dict:
    """Clarify-then-optimize a research question before verifying.

    Two-phase and stateless:
      - Call with just `question` to get clarifying questions (study type,
        population, recency, confidence bar, breadth) that shape the search.
      - Present them to the user, then call again with `answers` (keyed by each
        clarifier id) to get an optimized search plan: query variants, filters,
        a min_confidence threshold, and a target candidate count to feed into
        verify_claim / score_answer.
    """
    out = _plan.plan_search(question, answers)
    out["summary"] = _render.plan_summary(out)
    return out


def main():
    mcp.run()


if __name__ == "__main__":
    main()
