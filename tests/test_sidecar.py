"""Offline tests for the sidecar the Zotero plugin talks to (sidecar.py): auth,
health, and job kinds with the core monkeypatched. No model, no network."""
import os
import time

import pytest
from fastapi.testclient import TestClient

from worth_your_read import answer, config, library, render, sidecar, store

QUOTE = "the smaller the studies conducted, the less likely the findings are to be true"
PAGES = ["Introduction\nSome text.", "Results\nCorollary 1: " + QUOTE + "."]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store.config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "USE_OLLAMA", False)      # /health must not call Ollama
    return TestClient(sidecar.app)


def _headers():
    return {"X-WYR-Token": sidecar.token()}


def _run(c, kind, args):
    job = c.post("/jobs", json={"kind": kind, "args": args}, headers=_headers()).json()
    for _ in range(200):
        j = c.get(f"/jobs/{job['job_id']}", headers=_headers()).json()
        if j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.01)
    raise AssertionError(f"job {kind} never finished: {j}")


def _fetched(text=" ".join(PAGES), fulltext=True):
    return {"full_text": text, "sections": {"results": text}, "sections_available": ["results"],
            "has_results": True, "has_fulltext": fulltext, "open_access": False,
            "source": "zotero_pdf", "pages": PAGES}


# --- auth and health -----------------------------------------------------------------

def test_token_is_created_once_private_and_required(client, tmp_path):
    tok = sidecar.token()
    path = os.path.join(str(tmp_path), "sidecar.token")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert sidecar.token() == tok and len(tok) > 20
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"X-WYR-Token": tok + "x"}).status_code == 401
    assert client.post("/jobs", json={"kind": "plan"}).status_code == 401
    assert client.get("/jobs/abc").status_code == 401


def test_health_reports_backend_without_calling_ollama(client):
    h = client.get("/health", headers=_headers()).json()
    assert h["ok"] is True and h["ollama_up"] is None
    assert h["combine"] == config.COMBINE and "model" in h and "queued" in h


def test_unknown_kind_and_job(client):
    assert client.post("/jobs", json={"kind": "nope"}, headers=_headers()).status_code == 400
    assert client.get("/jobs/missing", headers=_headers()).status_code == 404
    assert client.delete("/jobs/missing", headers=_headers()).status_code == 404


def test_job_error_is_reported_not_raised(client):
    j = _run(client, "ask", {"question": "q", "paper": {}})
    assert j["status"] == "error" and j["error"].startswith("ValueError: No text for this paper")


# --- job kinds call the same core as the MCP ----------------------------------------

def test_verify_local_pdf_gets_pages_summary_card_and_distortion_label(client, monkeypatch):
    monkeypatch.setattr(library, "load", lambda pdf_path, key: _fetched())

    async def judge(ctx, claim, fetched, passes):
        assert ctx is None and passes == 1 and fetched["source"] == "zotero_pdf"
        return {"verdict": "OVERSTATED", "confidence": 0.6, "band": "Medium",
                "distortion_type": "correlation_causation", "distortion_note": "Only an association.",
                "aspects": [{"type": "outcome", "text": "small studies", "status": "ENTAILED",
                             "quote": QUOTE, "close_match": False}],
                "evidence": [{"quote": QUOTE, "stance": "ENTAILS"}]}
    monkeypatch.setattr(sidecar._server, "_judge_consistent", judge)
    j = _run(client, "verify", {"claim": "Small studies cause false findings.",
                                "paper": {"attachment_key": "ABCD1234", "title": "Why most…"}})
    assert j["status"] == "done", j
    r = j["result"]
    assert r["aspects"][0]["page"] == 2 and r["evidence"][0]["page"] == 2
    assert r["summary"] == render.summary(r) and r["card"] == render.render_card(r)
    assert r["distortion_label"] == "correlation→causation"
    assert r["resolved"]["title"] == "Why most…" and r["has_fulltext"] is True


def test_verify_by_reference_takes_the_mcp_path(client, monkeypatch):
    seen = {}

    async def verify(claim, reference, title, passes=1):
        seen.update(claim=claim, reference=reference, passes=passes)
        return {"verdict": "SUPPORTED", "confidence": 0.9, "band": "High", "claim": claim,
                "has_fulltext": True, "aspects": [], "evidence": []}
    monkeypatch.setattr(sidecar._server, "_verify", verify)
    r = _run(client, "verify", {"claim": "c", "paper": {"reference": "10.1/x"}})["result"]
    assert seen == {"claim": "c", "reference": "10.1/x", "passes": 1}
    assert r["summary"].startswith("SUPPORTED") and "distortion_label" not in r


def test_ask_many_matches_server_ask_across(client, monkeypatch):
    def load(paper):
        if paper.attachment_key == "BAD":
            raise OSError("unreadable")
        return _fetched(), PAGES
    monkeypatch.setattr(sidecar, "_load", load)
    monkeypatch.setattr(sidecar._research, "add_paper_facts", lambda papers: None)
    seen = {}

    def ask_many(question, papers, progress=None, max_papers=None, method=None, follow=None,
                 depth="standard"):
        seen.update(question=question, labels=[p["label"] for p in papers], keys=[p["key"] for p in papers],
                    dois=[p["doi"] for p in papers], max_papers=max_papers, method=method,
                    depth=depth, follow=callable(follow))
        progress({"stage": "paper", "paper": 1, "papers": len(papers), "label": papers[0]["label"]})
        return {"question": question, "overview": [], "not_addressed": [], "merged": True,
                "claims": [{"claim": "Small studies are less reliable.", "status": "GROUNDED",
                            "sources": [{"paper": 1, "quote": QUOTE, "verified": True, "page": 2}],
                            "against": [{"paper": 2, "quote": "x", "verified": True, "page": None}],
                            "unsourced_numbers": ["24"]}],
                "papers": [{"n": 1, "label": "A", "key": "K1", "used": True, "abstract_only": False},
                           {"n": 2, "label": "B", "key": "K2", "used": False, "abstract_only": True}],
                "skipped": [], "left_out": [], "confidence": 0.7, "band": "Medium",
                "grounded": 1, "ungrounded": 0}
    monkeypatch.setattr(answer, "ask_many", ask_many)
    j = _run(client, "ask_many", {"question": "Are small studies reliable?", "max_papers": 0,
                                  "depth": "bogus", "papers": [
        {"attachment_key": "A1", "reference": "10.1/a", "label": "A", "item_key": "K1"},
        {"attachment_key": "A2", "reference": "https://x.org/b", "label": "B", "item_key": "K2"},
        {"attachment_key": "BAD", "label": "C", "item_key": "K3"}]})
    assert j["status"] == "done", j
    # Same defaults as server.ask_across: method left to config.COMBINE, 0 -> read all.
    assert seen == {"question": "Are small studies reliable?", "labels": ["A", "B"], "keys": ["K1", "K2"],
                    "dois": ["10.1/a", ""], "max_papers": None, "method": None,
                    "depth": "standard", "follow": True}
    r = j["result"]
    assert r["left_out"] == [{"label": "C", "key": "K3", "reason": "load_failed: OSError"}]
    assert r["skipped"] == ["C"]
    assert r["summary"] == render.ask_summary(r) and r["card"] == render.ask_card(r)
    assert "disputed by [2]" in r["summary"] and "24" in r["summary"]
    assert j["progress"]["stage"] == "paper"


def test_summarize_returns_note_html_with_pages(client, monkeypatch):
    monkeypatch.setattr(library, "load", lambda pdf_path, key: _fetched())
    monkeypatch.setattr(answer, "summarize", lambda fetched, check=False: {
        "question": answer.SUMMARY_QUESTION, "not_addressed": [], "abstract_only": False,
        "claims": [{"claim": "Small studies mislead.", "status": "GROUNDED",
                    "quotes": [{"text": QUOTE, "verified": True}]}],
        "confidence": 0.7, "band": "Medium", "grounded": 1, "ungrounded": 0})
    r = _run(client, "summarize", {"paper": {"attachment_key": "A1", "title": "Ioannidis"}})["result"]
    assert r["claims"][0]["quotes"][0]["page"] == 2
    assert "<h1>Ioannidis</h1>" in r["note_html"] and "(p. 2)" in r["note_html"]


def test_local_pdf_without_text_falls_back_to_reference(monkeypatch):
    monkeypatch.setattr(library, "load", lambda pdf_path, key: {"source": "none", "pages": []})
    monkeypatch.setattr(sidecar._resolve, "resolve", lambda ref, title=None: {"doi": ref})
    monkeypatch.setattr(sidecar._fetch, "fetch_text", lambda resolved: {"source": "online", "ref": resolved["doi"]})
    fetched, pages = sidecar._load(sidecar.Paper(attachment_key="A1", reference="10.1/a"))
    assert fetched == {"source": "online", "ref": "10.1/a"} and pages == []
    with pytest.raises(ValueError):
        sidecar._load(sidecar.Paper(attachment_key="A1"))
