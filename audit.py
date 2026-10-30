"""Audit a paper's own claims against the sources it cites.

Given a paper (DOI / PMID / PMCID), pull its full text (Europe PMC JATS),
take a section (the introduction by default), and for each sentence that cites
a reference, verify that claim against the paper it points to — the same
resolve + fetch + judge pipeline, run on the paper's own citations.

Attribution is from the JATS inline citation links (<xref ref-type="bibr">), so
each claim is checked against the reference the authors actually cited — no
guessing. Needs full-text JATS with those links (open-access papers in Europe
PMC); degrades with a clear message when they aren't available.
"""
import re
import xml.etree.ElementTree as ET

import requests

from . import config
from . import resolve as _resolve
from . import fetch as _fetch

EPMC_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
CROSSREF = "https://api.crossref.org/works"


def _ln(el):
    return el.tag.split("}")[-1]


def _headers():
    return {"User-Agent": config.USER_AGENT}


def _fetch_jats(pmcid):
    pmcid = pmcid if pmcid.upper().startswith("PMC") else "PMC" + pmcid
    r = requests.get(EPMC_XML.format(pmcid=pmcid), headers=_headers(),
                     timeout=config.HTTP_TIMEOUT)
    if r.status_code != 200:
        return None
    try:
        return ET.fromstring(r.content)
    except ET.ParseError:
        return None


def _ref_map(root):
    """rid -> {title, doi} from the bibliography."""
    out = {}
    for ref in root.iter():
        if _ln(ref) != "ref":
            continue
        rid = ref.get("id")
        if not rid:
            continue
        title = doi = None
        for e in ref.iter():
            if _ln(e) == "article-title" and title is None:
                title = re.sub(r"\s+", " ", "".join(e.itertext())).strip()
            if _ln(e) == "pub-id" and e.get("pub-id-type") == "doi" and not doi:
                doi = (e.text or "").strip()
        out[rid] = {"title": title, "doi": doi}
    return out


def _flatten(el):
    """Paragraph text with bibr citations turned into [[rid]] markers."""
    s = el.text or ""
    for ch in el:
        if _ln(ch) == "xref" and ch.get("ref-type") == "bibr":
            s += f" [[{ch.get('rid', '')}]] "
        elif _ln(ch) != "xref":
            s += _flatten(ch)
        s += ch.tail or ""
    return s


def _find_section(root, keyword):
    kw = keyword.lower()
    best = None
    for sec in root.iter():
        if _ln(sec) != "sec":
            continue
        title = ""
        for t in sec:
            if _ln(t) == "title":
                title = "".join(t.itertext()).strip().lower()
                break
        if kw in title:
            return sec
        if best is None and title in ("", "background"):
            best = sec
    return best


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_MARK = re.compile(r"\[\[([^\]]*)\]\]")


def _claims_from_section(sec, ref_map, max_claims):
    claims = []
    for p in sec.iter():
        if _ln(p) != "p":
            continue
        for sent in _SENT.split(_flatten(p)):
            rids = []
            for m in _MARK.findall(sent):
                rids += [r for r in m.split() if r]
            text = re.sub(r"\s+", " ", _MARK.sub("", sent)).strip()
            if not rids or len(text) < 45:
                continue
            cited = [{"rid": r, **ref_map.get(r, {"title": None, "doi": None})}
                     for r in dict.fromkeys(rids)]
            claims.append({"claim": text, "cited": cited})
            if len(claims) >= max_claims:
                return claims
    return claims


def _doi_from_title(title):
    if not title:
        return None
    try:
        j = requests.get(CROSSREF, params={"query.bibliographic": title, "rows": 1,
                                           "select": "DOI"},
                         headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
                         timeout=config.HTTP_TIMEOUT).json()
        items = (j.get("message") or {}).get("items") or []
        return items[0].get("DOI").lower() if items else None
    except Exception:
        return None


async def run_audit(reference, verify_fn, max_claims=8, section="introduc"):
    """Audit a paper's section: verify each cited claim against its source.

    verify_fn(claim, reference) -> result dict (e.g. server._verify bound to a
    context). Returns {paper, section, available, claims:[...], summary}.
    """
    resolved = _resolve.resolve(reference)
    pmcid = resolved.get("pmcid")
    if not pmcid:
        core = _fetch._epmc_core(resolved)
        if core.get("pmcid"):
            pmcid = core["pmcid"]
    out = {"paper": {"title": resolved.get("title"), "doi": resolved.get("doi"),
                     "pmcid": pmcid},
           "section": section, "available": False, "claims": [], "summary": {}}
    if not pmcid:
        out["note"] = "No open-access full text with citation links is available for this paper."
        return out

    root = _fetch_jats(pmcid)
    if root is None:
        out["note"] = "Full-text JATS (with inline citations) could not be retrieved from Europe PMC."
        return out
    sec = _find_section(root, section)
    if sec is None:
        out["note"] = f"No '{section}' section found."
        return out

    ref_map = _ref_map(root)
    claims = _claims_from_section(sec, ref_map, max_claims)
    out["available"] = True

    counts = {}
    for item in claims:
        primary = next((c for c in item["cited"] if c.get("doi")), None)
        if not primary:
            titled = next((c for c in item["cited"] if c.get("title")), None)
            if titled:
                titled["doi"] = _doi_from_title(titled["title"])
                primary = titled if titled.get("doi") else None
        if not primary:
            item["verdict"] = "SKIPPED"
            item["note"] = "cited reference could not be resolved to a DOI"
            counts["SKIPPED"] = counts.get("SKIPPED", 0) + 1
            continue
        res = await verify_fn(item["claim"], primary["doi"])
        item["verified_against"] = primary
        item["verdict"] = res.get("verdict")
        item["confidence"] = res.get("confidence")
        item["fetch_source"] = res.get("fetch_source")
        item["has_results"] = res.get("has_results")
        item["rationale"] = res.get("rationale")
        item["card"] = res.get("card")
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    out["claims"] = claims
    out["summary"] = counts
    return out


def _render(out):
    p = out["paper"]
    lines = [f"# Citation audit — {p.get('title') or p.get('doi') or p.get('pmcid')}"]
    if not out.get("available"):
        lines.append("\n" + out.get("note", "Could not audit this paper."))
        return "\n".join(lines)
    lines.append(f"\nSection: **{out['section']}** · "
                 + " · ".join(f"{k}: {v}" for k, v in out["summary"].items()))
    for i, c in enumerate(out["claims"], 1):
        va = c.get("verified_against") or {}
        src = f" — cites *{(va.get('title') or va.get('doi') or '?')[:70]}*" if va else ""
        lines.append(f"\n**{i}. [{c.get('verdict')}]**{src}")
        lines.append(f"> {c['claim']}")
        if c.get("rationale"):
            lines.append(f"  _{c['rationale'][:200]}_")
    return "\n".join(lines)


def _main():
    import asyncio
    import sys
    from . import server
    ref = sys.argv[1] if len(sys.argv) > 1 else ""
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    if not ref:
        print("usage: python -m worth_your_read.audit <doi|pmid|pmcid> [max_claims]")
        return

    async def vf(claim, reference):
        return await server._verify(claim, reference)

    out = asyncio.run(run_audit(ref, vf, max_claims=n))
    print(_render(out))


if __name__ == "__main__":
    _main()
