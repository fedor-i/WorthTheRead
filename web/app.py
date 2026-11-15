"""FastAPI web surface for Worth Your Read.

Reuses the same engine as the MCP server (resolve / fetch / judge / score /
render) and returns the same result dicts, `summary` and `card` included (see
the contract at the top of render.py). There is no MCP client here, so the
judge uses the server's own backend: a local Ollama model when WYR_USE_OLLAMA
is set, else ANTHROPIC_API_KEY or Bedrock — set WYR_USE_BEDROCK=0 to force the
API path even under Claude Code.

Run:  uvicorn worth_your_read.web.app:app --reload
(from the project root, with ANTHROPIC_API_KEY set)
"""
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import server as _server   # _verify (retraction short-circuit + judge), plan_search, ask_across
from .. import score as _score
from .. import render as _render

app = FastAPI(title="Worth Your Read")
_STATIC = Path(__file__).parent / "static"


class ClaimItem(BaseModel):
    claim: str
    reference: str
    expected_title: str = ""


class PlanReq(BaseModel):
    question: str
    answers: Optional[dict] = None


class ScoreReq(BaseModel):
    claims: list[ClaimItem] = []
    # Results already returned by /api/verify (the page verifies one claim at a
    # time to show progress): scored as they are, without verifying again.
    results: Optional[list[dict]] = None
    min_confidence: float = 0.0


class AskReq(BaseModel):
    question: str
    references: list[str] = []
    max_papers: int = 6
    depth: str = "standard"


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.post("/api/plan")
def api_plan(req: PlanReq):
    """Clarify (no answers) or optimize the search (with answers)."""
    return _server.plan_search(req.question, req.answers)


@app.post("/api/verify")
async def api_verify(req: ClaimItem):
    """Verify a single claim against its reference."""
    return await _server._verify(req.claim, req.reference, req.expected_title, None)


@app.post("/api/score")
async def api_score(req: ScoreReq):
    """Score + rank (threshold-filtered), the same result as score_answer:
    verifies `claims` first, or scores `results` from /api/verify as given."""
    if req.results is not None:
        results = req.results
    else:
        results = [await _server._verify(c.claim, c.reference, c.expected_title, None)
                   for c in req.claims if c.claim.strip() and c.reference.strip()]
    scored = _score.score_answer(results, min_confidence=req.min_confidence)
    scored["summary"] = _render.score_summary(scored)
    scored["card"] = _render.render_scorecard(scored)
    scored["claims"] = results
    return scored


@app.post("/api/ask")
async def api_ask(req: AskReq):
    """Answer a question from several papers, each finding quoted and checked
    word for word (slow: a model call per paper). No references: find them.
    Errors come back like ask_papers': {error, summary}."""
    try:
        return await asyncio.to_thread(_server.ask_across, req.question, req.references,
                                       req.max_papers, req.depth)
    except Exception as e:
        msg = _render.ask_error(e)
        return {"question": req.question, "error": f"{type(e).__name__}: {e}",
                "summary": msg, "card": msg}
