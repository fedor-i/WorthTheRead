"""Worth Your Read — MCP server.

Tools an agent can call to vet its own citations:
  - verify_claim(claim, reference): resolve + retraction check + fetch real
    results + judge with quote-verified evidence. Returns structured fields
    plus a ready-to-show markdown `card`.
  - score_answer(claims): aggregate verdicts into an overall confidence score
    and a ranked "worth your read" source list.

The judge runs "through this chat" by default: the server asks the HOST's model
via MCP sampling (no API key, no local model). If the client doesn't support
sampling, it falls back to an ANTHROPIC_API_KEY / Bedrock call, and if neither
is available it returns INSUFFICIENT with a clear note. The quote-verification
guard runs regardless of backend.

Run (stdio):  python -m worth_your_read.server
"""
import re
from collections import Counter

from mcp.server.fastmcp import Context, FastMCP

from . import config as _config
from . import resolve as _resolve
from . import fetch as _fetch
from . import judge as _judge
from . import score as _score
from . import render as _render
from . import plan as _plan
from . import discover as _discover
from . import audit as _audit

mcp = FastMCP("worth-your-read")


async def _run_judge(ctx, claim: str, fetched: dict) -> dict:
    """Prefer host sampling; fall back to api/bedrock; else INSUFFICIENT."""
    if not fetched.get("full_text"):
        return _judge.no_text_result()

    # 1) Through this chat: ask the host's model via MCP sampling.
    if ctx is not None:
        try:
            from mcp.types import SamplingMessage, TextContent
            res = await ctx.session.create_message(
                messages=[SamplingMessage(
                    role="user",
                    content=TextContent(type="text",
                                        text=_judge.build_user(claim, fetched)))],
                system_prompt=_judge.sampling_system(),
                max_tokens=1500,
            )
            text = getattr(res.content, "text", "") or ""
            if text.strip():
                out = _judge.parse_json_verdict(text, fetched)
                out["judge_backend"] = "client_sampling"
                return out
        except Exception as e:
            _sampling_error = str(e)
        else:
            _sampling_error = "empty sampling response"
    else:
        _sampling_error = "no client context"

    # 2) Fallback: ANTHROPIC_API_KEY / Bedrock.
    try:
        out = _judge.judge_claim(claim, fetched)
        out["judge_backend"] = "bedrock" if _config.USE_BEDROCK else "api"
        return out
    except Exception as e:
        # 3) No judge backend at all — be honest.
        r = _judge.no_text_result(
            "No judge backend available: the client did not support sampling "
            f"({_sampling_error}) and no API/Bedrock credentials are set "
            f"({e}). Retrieval and retraction checks still ran.")
        r["judge_backend"] = "none"
        return r


_GENEROSITY = {"SUPPORTED": 3, "OVERSTATED": 2, "UNSUPPORTED": 1, "INSUFFICIENT": 0}


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
    the run-to-run ambiguity on borderline claims."""
    passes = max(1, min(5, passes))
    if passes == 1:
        return await _run_judge(ctx, claim, fetched)
    results = [await _run_judge(ctx, claim, fetched) for _ in range(passes)]
    if all(r.get("judge_backend") in ("none", "n/a", None) for r in results):
        return results[0]   # no backend actually ran — don't fake a vote
    return _reconcile(results)


async def _verify(claim: str, reference: str, expected_title: str = "",
                  ctx=None, evidence_text: str = "", passes: int = 1) -> dict:
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
            "has_results": False, "open_access": False,
            "fetch_source": "supplied_text", "resolve_error": None,
        })
        result["card"] = _render.render_card(result)
        return result

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
        "open_access": fetched.get("open_access", False),
        "fetch_source": fetched.get("source"),
        "resolve_error": resolved.get("error"),
    })
    result["card"] = _render.render_card(result)
    return result


@mcp.tool()
async def verify_claim(claim: str, reference: str = "", expected_title: str = "",
                       evidence_text: str = "", passes: int = 1,
                       ctx: Context = None) -> dict:
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
    """
    return await _verify(claim, reference, expected_title, ctx,
                         evidence_text=evidence_text, passes=passes)


@mcp.tool()
async def score_answer(claims: list, min_confidence: float = 0.0,
                       ctx: Context = None) -> dict:
    """Score a whole answer. Each item is {claim, reference} (optionally
    expected_title). Verifies every claim, then returns an overall confidence
    score and a ranked "worth your read" source list.

    min_confidence (0-1): sources whose supporting verdict falls below this are
    screened out of the list entirely, so you can gather many candidate
    references and keep only the ones that clear your bar.
    """
    results = []
    for c in claims:
        results.append(await _verify(c.get("claim", ""), c.get("reference", ""),
                                     c.get("expected_title", ""), ctx))
    summary = _score.score_answer(results, min_confidence=min_confidence)
    summary["card"] = _render.render_scorecard(summary)
    summary["claims"] = results
    return summary


async def _run_revise(ctx, claim: str, result: dict):
    """Propose a tightened claim: host sampling first, then api/bedrock."""
    if ctx is not None:
        try:
            from mcp.types import SamplingMessage, TextContent
            res = await ctx.session.create_message(
                messages=[SamplingMessage(
                    role="user",
                    content=TextContent(type="text",
                                        text=_judge.build_revise_user(claim, result)))],
                system_prompt=_judge.revise_system(),
                max_tokens=400,
            )
            text = getattr(res.content, "text", "") or ""
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


def _cited_keys(results: list) -> set:
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
    return {"query": query, "count": len(sources), "sources": sources}


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
                    res = r.get("resolved") or {}
                    ref_id = res.get("doi") or res.get("pmcid") or r.get("reference")
                    nv = await _verify(current, ref_id, "", ctx)
                    nv["discovered"] = r.get("discovered", False)
                    re_results.append(nv)
                results = re_results
                changed = True
        if need_broaden:
            fresh = await _strengthen(current, results,
                                      want - discovered_supporting(), ctx)
            if fresh:
                results.extend(fresh)
                for f in fresh:
                    res = f.get("resolved") or {}
                    added_refs.append(res.get("doi") or res.get("pmcid")
                                      or f.get("reference"))
                changed = True
        if not changed:
            break

    agg = _aggregate(results)
    primary = results[0]
    return {
        "revised": _judge._norm(current) != _judge._norm(claim),
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
        "card": primary.get("card"),
    }


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


@mcp.tool()
async def suggest_sources(claim: str, n: int = 3, exclude: str = "",
                          breadth: int = 14, ctx: Context = None) -> dict:
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
        breadth: how many candidate papers to search/verify (wider = more
            thorough, more model calls).
    """
    skip = (exclude or "").lower().strip().replace("doi:", "")
    cands = _discover.discover_sources(claim, n=breadth)
    seen = {(c.get("doi") or c.get("pmcid") or "").lower() for c in cands}
    kw = _keywords(claim)
    if kw and kw.lower() != claim.lower():
        for c in _discover.discover_sources(kw, n=breadth):
            key = (c.get("doi") or c.get("pmcid") or "").lower()
            if key and key not in seen:
                seen.add(key)
                cands.append(c)
    cands.sort(key=lambda c: -(c.get("_score") or 0))
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
    return {"claim": claim, "checked": checked, "count": len(found),
            "suggested": found}


@mcp.tool()
async def audit_paper(reference: str, max_claims: int = 8,
                      section: str = "introduc", passes: int = 1,
                      ctx: Context = None) -> dict:
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
    """
    async def vf(claim, ref):
        return await _verify(claim, ref, ctx=ctx, passes=passes)

    out = await _audit.run_audit(reference, vf, max_claims=max_claims,
                                 section=section)
    out["card"] = _audit._render(out)
    return out


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
    return _plan.plan_search(question, answers)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
