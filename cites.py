"""Follow a paper's introduction to the papers it cites.

A paper's abstract, results and discussion are its own evidence; its
introduction mostly restates other papers. So reading skips the introduction,
and an introduction sentence that bears on the question points instead at the
paper it cites, which can then be read in its own right. Nothing here is ever
quoted as evidence: a wrong link only costs a reading slot, it can't put words
in a paper's mouth.

Links come two ways:
- "xref": PMC's JATS XML keeps every in-text citation tied to its reference
  entry, usually with a DOI. Exact.
- "match": for other papers, the introduction sentence is matched by keywords
  against the titles and abstracts of the paper's references (OpenAlex). Kept
  only when one reference clearly stands out; otherwise no link.
"""
import re
import xml.etree.ElementTree as ET

from . import discover as _discover
from . import fetch as _fetch
from . import focus as _focus
from . import store as _store

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={pmcid}"

SENTENCES_PER_PAPER = 3   # the introduction sentences closest to the question, per paper
MIN_MATCH = 8.0           # BM25 score a "match" link needs, and
MATCH_LEAD = 1.5          # how far ahead of the next reference it must be

_MARK = "\x00"            # wraps a reference id while a paragraph is split into sentences
_RANGE = re.compile(rf"{_MARK}([^{_MARK}]+){_MARK}\s*[–-]\s*{_MARK}([^{_MARK}]+){_MARK}")


def _text_with_marks(el, refs: dict) -> str:
    """A paragraph's text, each citation to a known reference replaced by its marked id."""
    if el.tag.endswith("xref") and el.get("ref-type") == "bibr":
        ids = [r for r in (el.get("rid") or "").split() if r in refs]
        return "".join(f"{_MARK}{r}{_MARK}" for r in ids) + (el.tail or "")
    inner = (el.text or "") + "".join(_text_with_marks(c, refs) for c in el)
    return inner + (el.tail or "")


def _reference(ref) -> dict:
    ids = {p.get("pub-id-type"): (p.text or "").strip() for p in ref.iter() if p.tag.endswith("pub-id")}
    title = next(("".join(t.itertext()) for t in ref.iter() if t.tag.endswith("article-title")), "")
    return {"doi": _discover.clean_doi(ids.get("doi")) or "", "pmid": ids.get("pmid") or "",
            "title": _fetch._strip(title)}


def jats_links(content: bytes) -> list:
    """[{"sentence", "refs": [{doi, pmid, title}]}] for each introduction
    sentence that cites something, from JATS XML. [] if unparseable."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    refs = {r.get("id"): _reference(r) for r in root.iter() if r.tag.endswith("}ref") or r.tag == "ref"}
    order = {rid: i for i, rid in enumerate(refs)}
    rid_at = list(refs)
    intros = [s for body in root.iter() if body.tag.endswith("body") for s in body
              if s.tag.endswith("sec") and (s.get("sec-type") == "intro" or _fetch._bucket_from_title(
                  "".join(s.find("{*}title").itertext()) if s.find("{*}title") is not None else "")
                  == "introduction")]
    out = []
    for sec in intros:
        for p in sec.iter():
            if not p.tag.endswith("}p") and p.tag != "p":
                continue
            text = _fetch._strip((p.text or "") + "".join(_text_with_marks(c, refs) for c in p))
            # "1–4" cites 1, 2, 3 and 4.
            text = _RANGE.sub(lambda m: "".join(f"{_MARK}{r}{_MARK}" for r in rid_at[
                order[m.group(1)]:order[m.group(2)] + 1]) if order.get(m.group(1), 1) <= order.get(m.group(2), 0)
                else m.group(0), text)
            # Citations after the full stop ("...death.1,2 Next") belong to the sentence before.
            text = re.sub(rf"([.!?])((?:[\s,;]*{_MARK}[^{_MARK}]+{_MARK})+)", r"\2\1", text)
            for sentence in _focus._SENT_RE.split(text):
                ids = re.findall(rf"{_MARK}([^{_MARK}]+){_MARK}", sentence)
                clean = _fetch._strip(re.sub(rf"{_MARK}[^{_MARK}]+{_MARK}", "", sentence))
                clean = re.sub(r"\s*[\[(][\s,;–-]*[\])]", "", clean)      # brackets the ids sat in
                # commas and dashes that sat between them
                clean = re.sub(r"(?:\s*[,;])+(?=[.!?]|$)|(?:\s+[,;–-])+(?=\s)|\s+(?=[.!?,;])", "", clean)
                if ids and clean:
                    out.append({"sentence": clean, "refs": [refs[r] for r in dict.fromkeys(ids)]})
    return out


def pmc_links(pmcid: str) -> list:
    """jats_links for a PMC paper, cached; [] when PMC has no XML for it."""
    pmcid = re.sub(r"(?i)^pmc", "", pmcid or "")
    if not pmcid:
        return []
    key = f"cites:pmc:{pmcid}"
    hit = _store.get_response(key)
    if hit is not None:
        return hit
    try:
        links = jats_links(_fetch._get(EFETCH.format(pmcid=pmcid)).content)
    except Exception:
        return []
    _store.put_response(key, links)
    return links


def match_links(sentences: list, works: list) -> list:
    """Link each sentence to the one reference (an OpenAlex work) it clearly
    describes, by keywords; a sentence with no clear match gets no link."""
    if not works:
        return []
    docs = [f"{w['title']} {w.get('abstract') or ''}" for w in works]
    out = []
    for s in sentences:
        scores = _focus.bm25_rank(s, docs)
        ranked = sorted(range(len(works)), key=lambda i: -scores[i])
        best, second = scores[ranked[0]], scores[ranked[1]] if len(ranked) > 1 else 0.0
        if best >= MIN_MATCH and best >= MATCH_LEAD * second:
            w = works[ranked[0]]
            out.append({"sentence": s, "refs": [{"doi": w["doi"], "pmid": "", "title": w["title"]}]})
    return out


def intro_links(fetched: dict, doi: str = "") -> list:
    """The paper's introduction sentences that cite something, with what they cite."""
    if fetched.get("pmcid"):
        links = pmc_links(fetched["pmcid"])
        if links:
            return [dict(link, how="xref") for link in links]
    intro = (fetched.get("sections") or {}).get("introduction") or ""
    if not intro or not doi:
        return []
    from . import research as _research          # research imports a lot; only needed here
    work = _research.fetch_works(dois=[doi])
    refs = _research.fetch_works(ids=work[0]["refs"]) if work else []
    sentences = [s for s in _focus._SENT_RE.split(_fetch._strip(intro)) if len(s) > 40]
    return [dict(link, how="match") for link in match_links(sentences, [w for w in refs if w["doi"]])]


def _reviews(dois: list) -> set:
    """Those of `dois` that OpenAlex calls reviews: secondary again, so not followed."""
    from . import research as _research
    try:
        return {w["doi"] for w in _research.fetch_works(dois=dois) if w["type"] == "review"}
    except Exception:
        return set()


def to_follow(question: str, chosen: list, have: set, limit: int) -> list:
    """The papers the chosen papers' introductions cite for this question,
    not already among `have` (DOIs) and not reviews: [{doi, title, via: [paper
    n], how}], cited by the most chosen papers first, then by how close the
    citing sentence is to the question; at most `limit`.

    `chosen` items are ask_many papers; their DOI is `doi`, else `key` when it
    is one. Only each paper's SENTENCES_PER_PAPER introduction sentences closest
    to the question count, so a passing citation doesn't pull a paper in."""
    links = []
    for n, p in enumerate(chosen, 1):
        doi = _discover.clean_doi(p.get("doi") or (p.get("key") if str(p.get("key", "")).startswith("10.") else ""))
        links += [(n, link) for link in intro_links(p.get("fetched") or {}, doi)]
    if not links:
        return []
    # One ranking over every paper's sentences, so scores compare across papers.
    scores = _focus.bm25_rank(question, [link["sentence"] for _, link in links])
    per_paper, picked = {}, {}
    for score, (n, link) in sorted(zip(scores, links), key=lambda x: -x[0]):
        if score <= 0 or per_paper.get(n, 0) == SENTENCES_PER_PAPER:
            continue
        per_paper[n] = per_paper.get(n, 0) + 1
        for ref in link["refs"]:
            if not ref["doi"] or ref["doi"] in have:
                continue
            row = picked.setdefault(ref["doi"], {"doi": ref["doi"], "title": ref["title"], "via": [],
                                                 "how": link["how"], "score": score})
            if n not in row["via"]:
                row["via"].append(n)
            if link["how"] == "xref":
                row["how"] = "xref"
    rows = sorted(picked.values(), key=lambda r: (-len(r["via"]), -r["score"]))[:limit * 2]
    reviews = _reviews([r["doi"] for r in rows])
    return [{"doi": r["doi"], "title": r["title"], "via": sorted(r["via"]), "how": r["how"]}
            for r in rows if r["doi"] not in reviews][:limit]
