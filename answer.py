"""Grounded answers about one paper: "ask this paper" and the auto-summary.

The model answers as a list of short, atomic claims, each with the verbatim
passages it rests on. Then the same quote guard the judge uses checks every
passage against the paper's text: a claim with no verified passage is shown as
UNGROUNDED and never counts toward confidence. With check=True each grounded
claim is also put through the full judge, which catches a real quote stretched
into a claim it doesn't support (overstatement).
"""
import html
import json
import re

from . import config
from . import focus as _focus
from . import judge as _judge
from . import score as _score

_SYSTEM = """You answer a QUESTION about one scientific paper using ONLY the \
PAPER TEXT provided. Never use outside knowledge.

Answer as a list of short, ATOMIC claims — one fact per claim, in plain \
language, in the order a reader needs them. For EACH claim give the passage(s) \
from the paper that establish it, copied VERBATIM, word for word, including \
numbers. Prefer RESULTS and CONCLUSION passages over the abstract. Keep \
numbers, units, populations and caveats exactly as the paper states them; do \
not generalize beyond the passage. If the paper does not address part of the \
question, say so in "not_addressed" instead of guessing.

Return ONLY a JSON object: {"claims": [{"claim": "...", "quotes": ["..."]}], \
"not_addressed": ["..."]}"""

SUMMARY_QUESTION = (
    "Summarize this paper for a researcher: the question it asks, the study "
    "design and population or system, the main results with their numbers, and "
    "the limitations the authors state.")


def _complete(system: str, user: str, max_tokens: int = 1500) -> str:
    """One completion on the configured backend: local Ollama, else the
    Anthropic API / Bedrock."""
    if config.USE_OLLAMA:
        return _judge._ollama_chat(system, user, num_predict=max_tokens)
    client, model = _judge._client_and_model()
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, system=system, temperature=0,
        messages=[{"role": "user", "content": user}])
    return "".join(getattr(b, "text", "") for b in msg.content
                   if getattr(b, "type", None) == "text")


def _parse(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    s, e = raw.find("{"), raw.rfind("}")
    try:
        return json.loads(raw[s:e + 1]) if s != -1 and e != -1 else {}
    except Exception:
        return _salvage(raw)


def _salvage(raw: str) -> dict:
    """An answer cut off by the token limit is invalid JSON as a whole, but the
    claims before the cut are complete objects: keep those."""
    dec, claims = json.JSONDecoder(), []
    for m in re.finditer(r'\{\s*"claim"', raw):
        try:
            obj, _ = dec.raw_decode(raw, m.start())
            claims.append(obj)
        except ValueError:
            pass
    return {"claims": claims, "truncated": True} if claims else {"truncated": bool(raw)}


def ask(question: str, fetched: dict, check: bool = False) -> dict:
    """Answer `question` from `fetched` (a fetch_text / library.load dict).

    Returns {question, claims: [{claim, status, quotes: [{text, verified}],
    verdict?, confidence?}], not_addressed, confidence, band, grounded,
    ungrounded, source}."""
    out = {"question": question, "claims": [], "not_addressed": [],
           "confidence": 0.0, "band": "Low", "grounded": 0, "ungrounded": 0,
           "source": fetched.get("source"), "abstract_only": not fetched.get("has_fulltext")}
    text = fetched.get("full_text") or ""
    if not text:
        out["not_addressed"] = ["No paper text could be read."]
        return out
    budget = config.OLLAMA_FOCUS_CHARS if config.USE_OLLAMA else 0
    shown = _focus.focus_text(question, text, budget)
    data = _parse(_complete(_SYSTEM, f"QUESTION:\n{question}\n\nPAPER TEXT:\n{shown}"))

    haystack = _judge._norm(text)
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("claim") or "").strip():
            continue
        quotes = [{"text": q, "verified": _judge._quote_verified(q, haystack)}
                  for q in (c.get("quotes") or []) if isinstance(q, str) and q.strip()]
        grounded = any(q["verified"] for q in quotes)
        out["claims"].append({"claim": c["claim"].strip(),
                              "status": "GROUNDED" if grounded else "UNGROUNDED",
                              "quotes": quotes})
    out["not_addressed"] = [s for s in data.get("not_addressed") or []
                            if isinstance(s, str) and s.strip()]

    if check:
        for c in out["claims"]:
            if c["status"] == "GROUNDED":
                r = _judge.judge_claim(c["claim"], fetched)
                c["verdict"], c["confidence"] = r["verdict"], r["confidence"]
                c["verdict_note"] = r.get("distortion_note") or ""

    _score_answer(out, check)
    return out


# --- one question across several papers ---------------------------------------
# Map, then merge. Each paper is asked on its own (the single-paper `ask`, whose
# quotes are checked against that paper), then one call merges the per-paper
# claims into a synthesis. The merge only points at claim ids — it never writes
# quotes — so every passage in the answer is one already verified in the paper
# it's attributed to. A local model given all the papers in one prompt tends to
# answer from the first obvious one; asked one at a time, every paper speaks.

MAX_PAPERS = 12        # each paper is one model call; beyond this it gets slow

_MERGE_SYSTEM = """You merge findings from SEVERAL papers into one answer to a \
QUESTION. You get numbered CLAIMS, each already checked against the paper in \
brackets ([P1], [P2], ...). Use ONLY these claims; never add outside knowledge.

Write at most 10 short answer claims in plain language, ordered so they answer \
the question. When claims from different papers agree, merge them into one \
answer claim that lists all their ids. When papers disagree or differ \
(population, method, estimate, conclusion), keep that visible: a separate \
answer claim per side, or one that states the disagreement and lists both \
sides' ids. Keep numbers and caveats as the claims state them. Every answer \
claim must list the ids it rests on, and must not say more than those claims \
say. If part of the question is answered by no claim, say so in "not_addressed".

Return ONLY a JSON object: {"claims": [{"claim": "...", "from": ["C1", "C4"]}], \
"not_addressed": ["..."]}"""


def _rank_papers(question: str, papers: list) -> list:
    """Indices of `papers` ordered by how much of their text bears on the question."""
    docs = [(p.get("fetched") or {}).get("full_text") or "" for p in papers]
    scores = _focus.bm25_rank(question, [_focus.focus_text(question, d, 4000) for d in docs])
    return sorted(range(len(papers)), key=lambda i: -scores[i])


def ask_many(question: str, papers: list, progress=None) -> dict:
    """Answer `question` from several papers at once.

    `papers` is a list of {"label", "fetched", "pages"?, "key"?}. Returns
    {question, claims: [{claim, status, sources: [{paper, quote, verified,
    page}]}], papers: [{n, label, key, cited, abstract_only, used, error?}],
    skipped, not_addressed, merged, confidence, band, grounded, ungrounded}."""
    report = lambda **kw: progress and progress(kw)
    readable = [p for p in papers if (p.get("fetched") or {}).get("full_text")]
    out = {"question": question, "claims": [], "not_addressed": [], "papers": [],
           "skipped": [p.get("label", "") for p in papers if p not in readable],
           "merged": False, "confidence": 0.0, "band": "Low", "grounded": 0, "ungrounded": 0}
    if not readable:
        out["not_addressed"] = ["None of the papers had readable text."]
        return out
    order = _rank_papers(question, readable)
    out["skipped"] += [readable[i].get("label", "") for i in order[MAX_PAPERS:]]
    chosen = [readable[i] for i in sorted(order[:MAX_PAPERS])]   # keep the user's order

    # Map: each paper on its own.
    found, gaps = [], []           # found: (paper n, claim text, [verified quotes])
    for n, p in enumerate(chosen, 1):
        report(stage="paper", paper=n, papers=len(chosen), label=p.get("label", ""))
        info = {"n": n, "label": p.get("label", ""), "key": p.get("key", ""), "cited": 0,
                "used": False, "abstract_only": not p["fetched"].get("has_fulltext")}
        try:
            r = ask(question, p["fetched"])
            for c in r["claims"]:
                quotes = [q["text"] for q in c["quotes"] if q["verified"]]
                if quotes:
                    found.append((n, c["claim"], quotes))
            gaps += r["not_addressed"]
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
        out["papers"].append(info)

    def source(n, quote):
        return {"paper": n, "quote": quote, "verified": True, "reattributed": False,
                "page": _library_page(quote, chosen[n - 1].get("pages"))}

    # Merge: one call over the verified per-paper claims.
    merged, used = [], set()
    if len(found) > 1:
        report(stage="merge", papers=len(chosen), claims=len(found))
        listing = "\n".join(f"C{i} [P{n}] {text}" for i, (n, text, _) in enumerate(found, 1))
        titles = "\n".join(f"[P{n}] {p.get('label', '')}" for n, p in enumerate(chosen, 1))
        try:
            data = _parse(_complete(_MERGE_SYSTEM, f"QUESTION:\n{question}\n\nPAPERS:\n{titles}"
                                    f"\n\nCLAIMS:\n{listing}", max_tokens=2000))
        except Exception:
            data = {}
        out["truncated"] = bool(data.get("truncated"))
        for c in data.get("claims") or []:
            if not isinstance(c, dict) or not (c.get("claim") or "").strip():
                continue
            ids = []
            for ref in c.get("from") or []:
                m = re.search(r"\d+", str(ref))
                if m and 1 <= int(m.group(0)) <= len(found) and int(m.group(0)) - 1 not in ids:
                    ids.append(int(m.group(0)) - 1)
            sources = [source(found[i][0], q) for i in ids for q in found[i][2]]
            used.update(ids)
            # The model echoes "[P1, P3]"; the citations come from `sources`.
            text = re.sub(r"\s*\[(?:P\d+[,;\s]*)+\]", "", c["claim"]).strip()
            merged.append({"claim": text, "sources": sources, "extra": False,
                           "status": "GROUNDED" if sources else "UNGROUNDED"})
        out["not_addressed"] = [x for x in data.get("not_addressed") or []
                                if isinstance(x, str) and x.strip()]
        out["merged"] = bool(merged)
    else:
        out["not_addressed"] = gaps[:3]
    # Per-paper findings the merge didn't use are kept, not dropped.
    for i, (n, text, quotes) in enumerate(found):
        if i not in used:
            merged.append({"claim": text, "sources": [source(n, q) for q in quotes],
                           "status": "GROUNDED", "extra": out["merged"]})
    out["claims"] = merged

    for c in merged:
        for n in {s["paper"] for s in c["sources"]}:
            out["papers"][n - 1]["cited"] += 1
    for info in out["papers"]:
        info["used"] = info["cited"] > 0
    out["grounded"] = sum(1 for c in merged if c["status"] == "GROUNDED")
    out["ungrounded"] = len(merged) - out["grounded"]
    if merged:
        # Passages are real, but nothing has judged each claim against them:
        # cap below "High", as for a single unchecked answer.
        conf = min(0.7, out["grounded"] / len(merged))
        if all(p["abstract_only"] for p in out["papers"] if p["used"]):
            conf = min(conf, 0.5)
        out["confidence"] = round(conf, 2)
        out["band"] = _judge.band(out["confidence"])
    return out


def _library_page(quote: str, pages):
    if not pages:
        return None
    from . import library
    return library.page_of(quote, pages)


def _score_answer(out: dict, checked: bool) -> None:
    claims = out["claims"]
    out["grounded"] = sum(1 for c in claims if c["status"] == "GROUNDED")
    out["ungrounded"] = len(claims) - out["grounded"]
    if not claims:
        return
    if checked:
        # Grounded claims count by their verdict; ungrounded ones count as 0.
        conf = sum(_score._VERDICT_WEIGHT.get(c.get("verdict"), 0.0) * (c.get("confidence") or 0)
                   for c in claims) / len(claims)
    else:
        # Unchecked: we only know the passages are real, not that each claim
        # follows from them, so cap below "High".
        conf = min(0.7, out["grounded"] / len(claims))
    if out["abstract_only"]:
        conf = min(conf, 0.5)
    out["confidence"] = round(conf, 2)
    out["band"] = _judge.band(out["confidence"])


def summarize(fetched: dict, check: bool = False) -> dict:
    return ask(SUMMARY_QUESTION, fetched, check=check)


def note_html(result: dict, title: str = "") -> str:
    """A Zotero-note-ready HTML rendering: grounded claims with their quotes
    and pages; ungrounded claims are listed separately, clearly marked."""
    esc = html.escape
    parts = [f"<h1>{esc(title or 'Worth Your Read summary')}</h1>",
             f"<p><b>Confidence:</b> {result.get('band')} ({result.get('confidence'):.2f}) · "
             f"{result.get('grounded')} grounded, {result.get('ungrounded')} ungrounded"
             + (" · abstract only" if result.get("abstract_only") else "") + "</p>"]
    grounded = [c for c in result["claims"] if c["status"] == "GROUNDED"]
    ungrounded = [c for c in result["claims"] if c["status"] != "GROUNDED"]
    if grounded:
        parts.append("<ul>")
        for c in grounded:
            v = f" <i>[{esc(c['verdict'])}]</i>" if c.get("verdict") else ""
            parts.append(f"<li>{esc(c['claim'])}{v}")
            for q in c["quotes"]:
                if q["verified"]:
                    pg = f" (p. {q['page']})" if q.get("page") else ""
                    parts.append(f"<blockquote>“{esc(q['text'])}”{pg}</blockquote>")
            parts.append("</li>")
        parts.append("</ul>")
    if ungrounded:
        parts.append("<p><b>Not verified against the text</b> (no passage matched):</p><ul>")
        parts.extend(f"<li>{esc(c['claim'])}</li>" for c in ungrounded)
        parts.append("</ul>")
    if result.get("not_addressed"):
        parts.append("<p><b>Not addressed by the paper:</b> "
                     + esc("; ".join(result["not_addressed"])) + "</p>")
    return "\n".join(parts)
