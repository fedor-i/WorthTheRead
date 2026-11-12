"""FastAPI web surface for Worth Your Read.

Reuses the same engine as the MCP server (resolve / fetch / judge / score /
render). Here the judge uses the ANTHROPIC_API_KEY backend — set
WYR_USE_BEDROCK=0 to force the API path even under Claude Code.

Run:  uvicorn worth_your_read.web.app:app --reload
(from the project root, with ANTHROPIC_API_KEY set)
"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import server as _server   # reuse async _verify (retraction short-circuit + judge)
from .. import plan as _plan
from .. import score as _score
from .. import render as _render

app = FastAPI(title="Worth Your Read")
_STATIC = Path(__file__).parent / "static"


class VerifyReq(BaseModel):
    claim: str
    reference: str
    expected_title: str = ""


class PlanReq(BaseModel):
    question: str
    answers: Optional[dict] = None


class ClaimItem(BaseModel):
    claim: str
    reference: str
    expected_title: str = ""


class ScoreReq(BaseModel):
    claims: list[ClaimItem]
    min_confidence: float = 0.0


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


@app.post("/api/plan")
def api_plan(req: PlanReq):
    """Clarify (no answers) or optimize the search (with answers)."""
    return _plan.plan_search(req.question, req.answers)


@app.post("/api/verify")
async def api_verify(req: VerifyReq):
    """Verify a single claim against its reference."""
    return await _server._verify(req.claim, req.reference, req.expected_title, None)


@app.post("/api/score")
async def api_score(req: ScoreReq):
    """Verify every claim, then score + rank (threshold-filtered)."""
    results = []
    for c in req.claims:
        if not (c.claim.strip() and c.reference.strip()):
            continue
        results.append(await _server._verify(c.claim, c.reference,
                                             c.expected_title, None))
    summary = _score.score_answer(results, min_confidence=req.min_confidence)
    summary["card"] = _render.render_scorecard(summary)
    summary["claims"] = results
    return summary


class AskReq(BaseModel):
    question: str
    references: list[str] = []
    max_papers: int = 6
    depth: str = "standard"


@app.post("/api/ask")
def api_ask(req: AskReq):
    """Answer a question from several papers, each finding quoted and checked
    word for word (slow: a model call per paper). No references: find them."""
    return _server.ask_across(req.question, req.references, req.max_papers, req.depth)
