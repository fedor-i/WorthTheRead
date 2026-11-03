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

MAX_PAPERS = 12        # the plugin's default when the user doesn't choose
MERGE_BATCH = 24       # findings per merge call; past this a local model starts dropping papers

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


# A per-paper "finding" that is about the paper, not the question ("The study
# does not state where nitrogen limits growth"): a gap, not a claim.
_META_CLAIM = re.compile(
    r"\b(?:the|this) (?:study|paper|article|text|excerpt|authors?|review|abstract)\b[^.]*?"
    r"\b(?:does|do|did)(?: not|n't)(?: explicitly| directly| clearly)?"
    r" (?:state|mention|specify|say|address|discuss|provide|report|describe|identify)\b", re.I)


def _merge(question: str, chosen: list, found: list, items: list) -> tuple:
    """One merge call. `items` are (text, {finding ids}); returns the merged
    (text, {finding ids}) — each resting on the findings of the items it
    names — and the model's parsed reply."""
    listing = "\n".join(
        f"C{i} [{', '.join(f'P{n}' for n in sorted({found[j][0] for j in ids}))}] {text}"
        for i, (text, ids) in enumerate(items, 1))
    papers = sorted({found[j][0] for _, ids in items for j in ids})
    titles = "\n".join(f"[P{n}] {chosen[n - 1].get('label', '')}" for n in papers)
    try:
        data = _parse(_complete(_MERGE_SYSTEM, f"QUESTION:\n{question}\n\nPAPERS:\n{titles}"
                                f"\n\nCLAIMS:\n{listing}", max_tokens=2000))
    except Exception:
        data = {}
    merged = []
    for c in data.get("claims") or []:
        if not isinstance(c, dict) or not (c.get("claim") or "").strip():
            continue
        ids = set()
        for ref in c.get("from") or []:
            m = re.search(r"\d+", str(ref))
            if m and 1 <= int(m.group(0)) <= len(items):
                ids |= items[int(m.group(0)) - 1][1]
        # The model echoes "[P1, P3]"; the citations come from the ids.
        merged.append((re.sub(r"\s*\[(?:P\d+[,;\s]*)+\]", "", c["claim"]).strip(), ids))
    return merged, data


_SUPPORT_SYSTEM = """You check which FINDINGS back a SENTENCE. A finding backs \
it only if, on its own, it says what the sentence says about the same thing: \
the same place or region, population, nutrient and outcome. A finding on the \
same topic but about a different region, organism or result does NOT back it. \
Return ONLY a JSON object: {"support": ["C1", "C3"]} — an empty list if none do."""


def _supporting(sentence: str, found: list, ids: set) -> set:
    """The ids, among `ids`, whose findings back `sentence` on their own. The
    merge groups findings by topic, so a sentence about one region can pick up
    findings about another; one short call per sentence prunes them. If the
    check fails to answer, the merge's ids stand."""
    ordered = sorted(ids)
    listing = "\n".join(f"C{k} {found[i][1]}" for k, i in enumerate(ordered, 1))
    try:
        data = _parse(_complete(_SUPPORT_SYSTEM, f"SENTENCE:\n{sentence}\n\nFINDINGS:\n{listing}",
                                max_tokens=200))
    except Exception:
        return ids
    if not isinstance(data.get("support"), list):
        return ids
    keep = set()
    for ref in data["support"]:
        m = re.search(r"\d+", str(ref))
        if m and 1 <= int(m.group(0)) <= len(ordered):
            keep.add(ordered[int(m.group(0)) - 1])
    return keep


def _batches(items: list, found: list, size: int) -> list:
    """Consecutive groups of at most `size` items, never splitting one paper's
    findings when a group can take them whole."""
    runs = []                      # consecutive items from the same paper
    for item in items:
        n = min((found[j][0] for j in item[1]), default=0)
        if runs and runs[-1][0] == n:
            runs[-1][1].append(item)
        else:
            runs.append((n, [item]))
    groups, cur = [], []
    for _, run in runs:
        if cur and len(cur) + len(run) > size:
            groups.append(cur)
            cur = []
        for item in run:
            if len(cur) >= size:
                groups.append(cur)
                cur = []
            cur.append(item)
    return groups + ([cur] if cur else [])


def ask_many(question: str, papers: list, progress=None, max_papers: int = None) -> dict:
    """Answer `question` from several papers at once.

    `papers` is a list of {"label", "fetched", "pages"?, "key"?}. Returns
    {question, claims: [{claim, status, sources: [{paper, quote, verified,
    page}]}], papers: [{n, label, key, cited, abstract_only, used, error?}],
    skipped, left_out: [{label, key, reason}], not_addressed, merged,
    confidence, band, grounded, ungrounded}. Papers beyond `max_papers` (the
    least relevant to the question) are left out, reason "limit"; None reads all.
    However many findings come back, no single merge call sees more than
    MERGE_BATCH of them: larger sets are merged in parts, then the parts merged.
    Each merged claim that rests on several findings is then checked against
    them, and keeps only those that back it; a claim none back is UNGROUNDED."""
    report = lambda **kw: progress and progress(kw)
    readable = [p for p in papers if (p.get("fetched") or {}).get("full_text")]
    out = {"question": question, "claims": [], "not_addressed": [], "papers": [],
           "skipped": [p.get("label", "") for p in papers if p not in readable],
           "left_out": [{"label": p.get("label", ""), "key": p.get("key", ""), "reason": "no_text"}
                        for p in papers if p not in readable],
           "merged": False, "confidence": 0.0, "band": "Low", "grounded": 0, "ungrounded": 0}
    if not readable:
        out["not_addressed"] = ["None of the papers had readable text."]
        return out
    order = _rank_papers(question, readable)
    max_papers = len(readable) if not max_papers else max(1, int(max_papers))
    out["skipped"] += [readable[i].get("label", "") for i in order[max_papers:]]
    out["left_out"] += [{"label": readable[i].get("label", ""), "key": readable[i].get("key", ""),
                         "reason": "limit"} for i in order[max_papers:]]
    chosen = [readable[i] for i in sorted(order[:max_papers])]   # keep the user's order

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
                if _META_CLAIM.search(c["claim"]):
                    gaps.append(c["claim"])
                elif quotes:
                    found.append((n, c["claim"], quotes))
            gaps += r["not_addressed"]
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"
        out["papers"].append(info)

    def source(n, quote):
        return {"paper": n, "quote": quote, "verified": True, "reattributed": False,
                "page": _library_page(quote, chosen[n - 1].get("pages"))}

    # Merge the verified per-paper claims, in parts when there are many: each
    # round merges groups of at most MERGE_BATCH, until one call can take the rest.
    merged, used = [], set()
    if len(found) > 1:
        items, rnd = [(text, {i}) for i, (_, text, _) in enumerate(found)], 0
        while len(items) > MERGE_BATCH:
            rnd += 1
            parts, nxt = _batches(items, found, MERGE_BATCH), []
            for k, part in enumerate(parts, 1):
                report(stage="merge", papers=len(chosen), claims=len(found), part=k, parts=len(parts), round=rnd)
                res, data = _merge(question, chosen, found, part)
                out["truncated"] = out.get("truncated") or bool(data.get("truncated"))
                # A part that failed carries its own findings forward.
                nxt += [r for r in res if r[1]] or part
            if len(nxt) >= len(items):        # no progress: stop shrinking
                break
            items = nxt
        report(stage="merge", papers=len(chosen), claims=len(found))
        final, data = _merge(question, chosen, found, items)
        out["truncated"] = out.get("truncated") or bool(data.get("truncated"))
        for k, (text, ids) in enumerate(final, 1):
            if len(ids) > 1:
                report(stage="check", claim=k, claims=len(final))
                ids = _supporting(text, found, ids)
            sources = [source(found[i][0], q) for i in sorted(ids) for q in found[i][2]]
            used |= ids
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
