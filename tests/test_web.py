"""Offline tests for the two web surfaces: the FastAPI app (web/app.py) with the
core monkeypatched, and the static site (docs/app.js) kept in step with the
core — its judge prompts, its port of judge.py's quote guard (run in node and
compared with judge.py on the same inputs), and the result-card block and
stylesheet it shares with web/static/index.html. No model and no network."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from worth_your_read import fetch, judge, render, server
from worth_your_read.web import app as web_app

ROOT = Path(__file__).resolve().parents[1]
DOCS_JS = (ROOT / "docs" / "app.js").read_text(encoding="utf-8")
DOCS_HTML = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
WEB_HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")


def _block(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a:text.index(end, a) + len(end)]


def _node(js: str):
    out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --- web/app.py -------------------------------------------------------------------

def _fake_result(claim, reference):
    r = {"verdict": "SUPPORTED", "confidence": 0.9, "band": "High", "claim": claim,
         "reference": reference, "resolved": {"title": "A paper", "doi": reference},
         "has_fulltext": True, "has_results": True, "retracted": False,
         "citation_ok": True, "aspects": [], "evidence": [], "distortion_type": "none"}
    r["summary"] = render.summary(r)
    return r


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def fake_verify(claim, reference, expected_title="", ctx=None, **kw):
        calls.append((claim, reference))
        return _fake_result(claim, reference)

    def fake_ask(question, references=None, max_papers=6, depth="standard", progress=None):
        if question == "boom":
            raise RuntimeError("no backend")
        return {"question": question, "overview": [], "claims": [], "papers": [],
                "summary": "ok", "references": references}

    monkeypatch.setattr(server, "_verify", fake_verify)
    monkeypatch.setattr(server, "ask_across", fake_ask)
    c = TestClient(web_app.app)
    c.calls = calls
    return c


def test_index_serves_the_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "Worth Your Read" in r.text


def test_verify_returns_the_core_result(client):
    r = client.post("/api/verify", json={"claim": "X lowers Y", "reference": "10.1/abc"}).json()
    assert r["verdict"] == "SUPPORTED" and r["summary"].startswith("SUPPORTED · high confidence")
    assert client.calls == [("X lowers Y", "10.1/abc")]


def test_score_verifies_claims_and_adds_summary_and_card(client):
    s = client.post("/api/score", json={"claims": [
        {"claim": "X lowers Y", "reference": "10.1/abc"},
        {"claim": "", "reference": "10.1/skip"}], "min_confidence": 0.5}).json()
    assert client.calls == [("X lowers Y", "10.1/abc")]          # blank claim skipped
    assert s["grade"] == "A" and s["n_claims"] == 1
    assert s["summary"] == render.score_summary(s)
    assert s["card"].startswith("**Overall: grade A")
    assert s["worth_your_read"][0]["doi"] == "10.1/abc" and len(s["claims"]) == 1


def test_score_given_results_does_not_verify_again(client):
    res = [_fake_result("X lowers Y", "10.1/abc")]
    s = client.post("/api/score", json={"results": res}).json()
    assert client.calls == [] and s["n_claims"] == 1 and s["summary"].startswith("Grade A")


def test_plan_goes_through_plan_search(client):
    c = client.post("/api/plan", json={"question": "How does sleep affect memory?"}).json()
    assert c["mode"] == "clarify" and c["clarifying_questions"] and c["summary"]
    p = client.post("/api/plan", json={"question": "How does sleep affect memory?",
                                       "answers": {"min_confidence": "0.85 (strict)"}}).json()
    assert p["mode"] == "plan" and p["min_confidence"] == 0.85 and p["queries"]
    assert p["summary"] == render.plan_summary(p)


def test_ask_passes_through_and_reports_errors(client):
    r = client.post("/api/ask", json={"question": "q", "references": ["10.1/a"]}).json()
    assert r["summary"] == "ok" and r["references"] == ["10.1/a"]
    e = client.post("/api/ask", json={"question": "boom"}).json()
    assert "no backend" in e["error"] and e["summary"] == render.ask_error(
        RuntimeError("no backend"))


# --- docs/app.js and web/static/index.html stay in step with the core ---------------

def _js_template(name: str) -> str:
    return re.search(r"const " + name + r" = `(.*?)`;", DOCS_JS, re.S).group(1)


def test_docs_prompts_match_judge_py():
    assert _js_template("SYSTEM") == judge.sampling_system()
    assert _js_template("REVISE_SYSTEM") == judge.revise_system()


CARDS = ("// ---- result cards:", "// ---- end result cards ")


def test_result_cards_and_styles_are_shared():
    assert _block(DOCS_JS, *CARDS) == _block(WEB_HTML, *CARDS)
    assert _block(DOCS_HTML, "<style>", "</style>") == _block(WEB_HTML, "<style>", "</style>")


@needs_node
def test_card_labels_match_render_py():
    names = ["VERDICT_BADGE", "VERDICT_MEANING", "ASPECT_ICON", "CLOSE_MATCH", "NO_QUOTE",
             "UNSOURCED_NUMBERS", "FULL_TEXT", "ABSTRACT_ONLY", "ABSTRACT_LEVEL", "RETRACTED",
             "RETRACTED_WARNING", "CONCERN", "WRONG_PAPER", "WRONG_PAPER_WARNING",
             "FOUND_BY_SEARCH", "NOTHING_USED", "NO_SOURCES", "PARTLY_SUPPORTS"]
    js = _block(DOCS_JS, *CARDS) + "\nconsole.log(JSON.stringify({" + ",".join(names) + \
        ", DISTORTION_LABELS, summary: summaryLine(%s)}));" % json.dumps(
            {"verdict": "SUPPORTED", "band": "Med", "has_fulltext": False, "citation_ok": None})
    got = _node(js)
    for n in names:
        assert got[n] == getattr(render, n), n
    assert got["DISTORTION_LABELS"] == judge.DISTORTION_LABELS
    assert got["summary"] == render.summary(
        {"verdict": "SUPPORTED", "band": "Med", "has_fulltext": False, "citation_ok": None})


# --- the judge guard: docs/app.js's port vs judge.py --------------------------------

PAPER = ("Essay\nWhy Most Published Research Findings Are False\nSummary for most study "
         "designs and settings, it is more likely for a research claim to be false than "
         "true. The ef\ufb01cacy of the inter-\nvention was 42% (95% CI 30–54) in adults. "
         "Results\nThe probability that a finding is true depends on power and bias.")
FETCHED = fetch.assemble({"sections": {"abstract": PAPER.split("Results")[0],
                                       "results": "Results" + PAPER.split("Results")[1]},
                          "source": "zotero_fulltext", "open_access": False})
QUOTES = [
    "it is more likely for a research claim to be false than true",          # verbatim
    "It is MORE likely for a research claim to be false than true!",         # case/punct
    "the efficacy of the intervention was 42%",                              # ligature + hyphen break
    "for most study designs and settings ... false than true",               # ellipsis pieces
    "for most designs and settings it is more likely for a claim to be false than true",  # 80% words
    "the probability that a finding is true depends on luck alone entirely",  # fabricated
    "short",
]
DATA = [  # raw judgements: (data, claim)
    ({"verdict": "SUPPORTED", "confidence": 1.0,
      "aspects": [{"type": "outcome", "text": "findings false", "status": "ENTAILED", "quote": QUOTES[0]}],
      "evidence": [{"quote": QUOTES[0], "stance": "ENTAILS"}]},
     "Most findings are false, regardless of study design or field."),
    ({"verdict": "SUPPORTED", "confidence": 0.9,
      "aspects": [{"type": "outcome", "text": "a", "status": "ENTAILED", "quote": QUOTES[2]},
                  {"type": "population", "text": "children", "status": "ENTAILED", "quote": "children were not studied at all here"}]},
     "The intervention works in children."),
    ({"verdict": "SUPPORTED", "confidence": 0.8,
      "aspects": [{"type": "population", "text": "adults", "status": "ENTAILED", "quote": QUOTES[2]},
                  {"type": "other", "text": "b", "status": "ABSENT", "quote": ""}, "stray string"]},
     "Adults and teens benefit."),
    ({"verdict": "UNSUPPORTED", "confidence": 0.95,
      "aspects": [{"type": "direction", "text": "decrease", "status": "CONTRADICTED", "quote": QUOTES[4]}]},
     "It always decreases."),
    ({"verdict": "SUPPORTED", "confidence": 0.9, "evidence": [{"quote": QUOTES[5]}]}, "Luck decides."),
    ({"verdict": "OVERSTATED", "confidence": 0.7, "aspects": [],
      "evidence": [{"quote": QUOTES[3], "stance": "ENTAILS"}]}, "Findings are false."),
]
_KEYS = ("verdict", "confidence", "band", "aspects", "claim_aspects_unmet", "distortion_type",
         "distortion_note", "quotes_verified", "downgraded", "verdict_model")


@needs_node
def test_docs_judge_core_matches_judge_py():
    core = _block(DOCS_JS, "// ---- judge core:", "// ---- end judge core ")
    abstract_only = dict(FETCHED, has_fulltext=False)
    js = core + "\nconst F = %s, A = %s, Q = %s, D = %s;\n" % (
        json.dumps(FETCHED), json.dumps(abstract_only), json.dumps(QUOTES), json.dumps(DATA)) + """
const hay = norm(F.full_text), ha = normAlnum(hay);
console.log(JSON.stringify({
  norm: norm(F.full_text), alnum: ha,
  verified: Q.map(q => quoteVerified(q, hay)), verbatim: Q.map(q => verbatim(q, ha)),
  finals: D.map(([d, c]) => finalize(JSON.parse(JSON.stringify(d)), F, c)),
  abstract: D.map(([d, c]) => finalize(JSON.parse(JSON.stringify(d)), A, c)),
  parsed: [parseJsonVerdict("```json\\n" + JSON.stringify(D[0][0]) + "\\n```", F, D[0][1]).verdict,
           parseJsonVerdict("not json", F).verdict],
  revision: [cleanRevision('{"rewritten_claim": "“A tighter claim.”"}'), cleanRevision('"Plain."')],
}));"""
    got = _node(js)
    hay = judge._norm(FETCHED["full_text"])
    ha = judge._norm_alnum(hay)
    assert got["norm"] == hay and got["alnum"] == ha
    assert got["verified"] == [judge._quote_verified(q, hay) for q in QUOTES]
    assert got["verbatim"] == [judge.verbatim(q, ha) for q in QUOTES]
    assert got["verified"][4] and not got["verbatim"][4]      # the fuzzy (80%) path is exercised
    for name, fetched in (("finals", FETCHED), ("abstract", dict(FETCHED, has_fulltext=False))):
        for js_r, (data, claim) in zip(got[name], DATA):
            py = judge.finalize(json.loads(json.dumps(data)), fetched, claim)
            for k in _KEYS:
                if k == "aspects":
                    strip = lambda a: [{x: v for x, v in asp.items() if x != "close_match" or asp.get("quote")}
                                       for asp in a]
                    assert strip(js_r[k]) == strip(py[k]), (name, claim)
                elif k == "confidence":
                    assert js_r[k] == pytest.approx(py[k]), (name, claim)
                else:
                    assert js_r.get(k) == py.get(k), (name, claim, k)
    assert got["parsed"] == [judge.parse_json_verdict("```json\n" + json.dumps(DATA[0][0]) + "\n```",
                                                      FETCHED, DATA[0][1])["verdict"], "INSUFFICIENT"]
    assert got["revision"] == [judge._clean_revision('{"rewritten_claim": "“A tighter claim.”"}'),
                               judge._clean_revision('"Plain."')]
