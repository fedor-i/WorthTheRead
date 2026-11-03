"""The local sidecar: a small HTTP API on 127.0.0.1 that the Zotero plugin
calls. Same engine as the MCP server (resolve / fetch / judge / revise /
discover / score), plus local papers (library.py), grounded answers
(answer.py) and an on-disk cache (store.py).

Model work is slow on a laptop, so every model call runs as a JOB: POST
/jobs returns an id at once, GET /jobs/{id} reports status and the result.
Jobs run one at a time — there is one GPU.

Auth: only processes that can read the token file in the data directory may
call it (header X-WYR-Token). It never listens beyond 127.0.0.1.

Run:  wyr-sidecar          (or: python -m worth_your_read.sidecar)
"""
import asyncio
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import __version__
from . import answer as _answer
from . import config as _config
from . import library as _library
from . import render as _render
from . import research as _research_mod
from . import resolve as _resolve
from . import server as _server
from . import store as _store
from . import fetch as _fetch

PORT = int(os.environ.get("WYR_SIDECAR_PORT", "23120"))


def token() -> str:
    """The shared secret, created on first run (readable by this user only)."""
    path = os.path.join(_store.data_dir(), "sidecar.token")
    if not os.path.isfile(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32))
    with open(path) as fh:
        return fh.read().strip()


def _auth(x_wyr_token: str = Header(default="")):
    if not secrets.compare_digest(x_wyr_token, token()):
        raise HTTPException(401, "missing or wrong X-WYR-Token")


app = FastAPI(title="Worth Your Read sidecar", dependencies=[Depends(_auth)])


# --- papers ------------------------------------------------------------------

class Paper(BaseModel):
    """Which paper: a Zotero attachment key or local PDF (offline), and/or a
    reference (DOI / PMID / PMCID / URL) fetched online and cached on disk."""
    attachment_key: str = ""
    pdf_path: str = ""
    reference: str = ""
    title: str = ""


def _load(paper: Paper) -> tuple:
    """(fetched, pages, resolved) — the local copy wins when it has text."""
    fetched, pages, resolved = None, [], {}
    if paper.attachment_key or paper.pdf_path:
        fetched = _library.load(paper.pdf_path, paper.attachment_key)
        pages = fetched.get("pages") or []
        if fetched.get("source") == "none":
            fetched = None
    if fetched is None and paper.reference:
        resolved = _resolve.resolve(paper.reference, paper.title or None)
        fetched = _fetch.fetch_text(resolved)
    if fetched is None:
        raise ValueError("No text for this paper: give an attachment_key, "
                         "pdf_path or reference that can be read.")
    return fetched, pages, resolved


# --- job bodies (run in the worker thread) ------------------------------------

def _verify(args: dict) -> dict:
    paper = Paper(**args.get("paper", {}))
    claim, passes = args["claim"], int(args.get("passes", 1))
    if not (paper.attachment_key or paper.pdf_path):
        # Online reference: the full MCP path (retraction, preprint, wrong paper).
        return asyncio.run(_server._verify(claim, paper.reference, paper.title,
                                           passes=passes))
    fetched, pages, _ = _load(paper)
    r = asyncio.run(_server._judge_consistent(None, claim, fetched, passes))
    r.update({"claim": claim, "reference": paper.reference or paper.attachment_key
              or paper.pdf_path, "fetch_source": fetched.get("source"),
              "sections_available": fetched.get("sections_available", []),
              "has_fulltext": fetched.get("has_fulltext", False),
              "resolved": {"title": paper.title}})
    _library.annotate_pages(r, pages)
    r["summary"] = _render.summary(r)
    return r


def _ask_many(args: dict) -> dict:
    papers, failed, failed_keys = [], [], []
    for spec in args.get("papers", []):
        paper = Paper(**spec)
        label = spec.get("label") or paper.title or paper.reference or paper.attachment_key
        try:
            fetched, pages, _ = _load(paper)
        except Exception:
            failed.append(label)
            failed_keys.append({"label": label, "key": spec.get("item_key", ""), "reason": "load_failed"})
            continue
        papers.append({"label": label, "key": spec.get("item_key", ""),
                       "fetched": fetched, "pages": pages})
    r = _answer.ask_many(args["question"], papers, progress=args.get("_progress"),
                         max_papers=int(args.get("max_papers") or 0) or None)
    r["skipped"] = failed + r["skipped"]
    r["left_out"] = failed_keys + r["left_out"]
    return r


def _ask(args: dict) -> dict:
    fetched, pages, _ = _load(Paper(**args.get("paper", {})))
    r = _answer.ask(args["question"], fetched, check=bool(args.get("check")))
    return _library.annotate_pages(r, pages)


def _summarize(args: dict) -> dict:
    paper = Paper(**args.get("paper", {}))
    fetched, pages, _ = _load(paper)
    r = _library.annotate_pages(_answer.summarize(fetched, check=bool(args.get("check"))), pages)
    r["note_html"] = _answer.note_html(r, paper.title)
    return r


def _revise(args: dict) -> dict:
    return asyncio.run(_server.revise_claim(
        args["claim"], args["reference"], args.get("title", ""),
        max_rounds=int(args.get("max_rounds", 2)),
        broaden=bool(args.get("broaden", False)), want=int(args.get("want", 2))))


def _suggest(args: dict) -> dict:
    return asyncio.run(_server._suggest_sources(
        args["claim"], int(args.get("n", 2)), args.get("exclude", ""),
        int(args.get("breadth", 5)), None))


def _locate(args: dict) -> dict:
    """On-page rectangles of verified quotes, for highlight annotations."""
    paper = Paper(**args.get("paper", {}))
    return {"quotes": _library.locate_quotes(args.get("quotes", []),
                                             paper.attachment_key, paper.pdf_path)}


def _research(args: dict) -> dict:
    """Snowball the citation graph from a question (or seed DOIs, for "find
    similar") until it runs dry; reports progress as it goes."""
    return _research_mod.snowball(
        args.get("question", ""), seed_dois=args.get("seed_dois", []),
        max_rounds=int(args.get("max_rounds", 8)), max_papers=int(args.get("max_papers", 200)),
        progress=args.get("_progress"))


def _find(args: dict) -> dict:
    return _server.find_sources(args["query"], int(args.get("limit", 8)))


def _plan(args: dict) -> dict:
    return _server.plan_search(args["question"], args.get("answers"))


KINDS = {"verify": _verify, "ask": _ask, "summarize": _summarize,
         "revise": _revise, "suggest": _suggest, "find": _find, "plan": _plan,
         "locate": _locate, "research": _research,
         "ask_many": _ask_many}


# --- jobs ----------------------------------------------------------------------

_jobs = {}
_jobs_lock = threading.Lock()
_worker = ThreadPoolExecutor(max_workers=1)   # one model at a time


def _run(job_id: str) -> None:
    job = _jobs[job_id]
    if job["status"] == "cancelled":
        return
    job.update(status="running", started_at=time.time())
    try:
        # Long jobs report progress through this callback (shown by GET /jobs).
        args = {**job["args"], "_progress": lambda p: job.update(progress=p)}
        job["result"] = KINDS[job["kind"]](args)
        job["status"] = "done"
    except Exception as e:  # report, don't crash the worker
        job.update(status="error", error=f"{type(e).__name__}: {e}")
    job["finished_at"] = time.time()


class JobReq(BaseModel):
    kind: str
    args: dict = {}


@app.post("/jobs")
def create_job(req: JobReq):
    if req.kind not in KINDS:
        raise HTTPException(400, f"unknown kind {req.kind!r}; one of {sorted(KINDS)}")
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "kind": req.kind, "args": req.args,
                         "status": "queued", "created_at": time.time()}
    _worker.submit(_run, job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {k: v for k, v in job.items() if k != "args"}


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str):
    """Cancel a queued job (a running model call can't be interrupted)."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] == "queued":
        job["status"] = "cancelled"
    return {"job_id": job_id, "status": job["status"]}


@app.get("/health")
def health():
    ollama_up = None
    if _config.USE_OLLAMA:
        import urllib.request
        try:
            urllib.request.urlopen(_config.OLLAMA_URL + "/api/tags", timeout=2)
            ollama_up = True
        except Exception:
            ollama_up = False
    return {"ok": True, "version": __version__,
            "backend": "ollama" if _config.USE_OLLAMA else
                       ("bedrock" if _config.USE_BEDROCK else "api"),
            "model": _config.OLLAMA_MODEL if _config.USE_OLLAMA else _config.MODEL,
            "ollama_up": ollama_up, "disk_cache": _store.enabled(),
            "queued": sum(1 for j in _jobs.values() if j["status"] in ("queued", "running"))}


def main():
    import uvicorn
    _store.enable()
    token()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
