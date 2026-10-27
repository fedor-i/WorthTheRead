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
from mcp.server.fastmcp import Context, FastMCP

from . import config as _config
from . import resolve as _resolve
from . import fetch as _fetch
from . import judge as _judge
from . import score as _score
from . import render as _render
from . import plan as _plan

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


async def _verify(claim: str, reference: str, expected_title: str = "",
                  ctx=None) -> dict:
    resolved = _resolve.resolve(reference, expected_title or None)
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
        result = await _run_judge(ctx, claim, fetched)

    result.update({
        "claim": claim,
        "reference": reference,
        "citation_ok": resolved.get("citation_ok", False),
        "resolved": {k: resolved.get(k) for k in
                     ("title", "authors", "year", "journal", "doi", "pmcid")},
        "retracted": resolved.get("retracted", False),
        "concern": resolved.get("concern", False),
        "sections_available": fetched.get("sections_available", []),
        "has_results": fetched.get("has_results", False),
        "open_access": fetched.get("open_access", False),
        "fetch_source": fetched.get("source"),
        "resolve_error": resolved.get("error"),
    })
    result["card"] = _render.render_card(result)
    return result


@mcp.tool()
async def verify_claim(claim: str, reference: str, expected_title: str = "",
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
    """
    return await _verify(claim, reference, expected_title, ctx)


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
