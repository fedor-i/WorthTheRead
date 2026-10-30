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


def _segment_section(sec, ref_map, max_claims):
    """Return (claims, paragraphs). paragraphs is the full section prose as a
    list of paragraphs, each a list of sentence segments {text, claim_idx};
    a segment with claim_idx set is a citing sentence we verify. This lets the
    renderer print the ORIGINAL text with the claims highlighted in place."""
    claims, paragraphs = [], []
    for p in sec.iter():
        if _ln(p) != "p":
            continue
        para = []
        for sent in _SENT.split(_flatten(p)):
            rids = []
            for m in _MARK.findall(sent):
                rids += [r for r in m.split() if r]
            text = re.sub(r"\s+", " ", _MARK.sub("", sent)).strip()
            if not text:
                continue
            seg = {"text": text, "claim_idx": None}
            if rids and len(text) >= 45 and len(claims) < max_claims:
                cited = [{"rid": r, **ref_map.get(r, {"title": None, "doi": None})}
                         for r in dict.fromkeys(rids)]
                seg["claim_idx"] = len(claims)
                claims.append({"claim": text, "cited": cited})
            para.append(seg)
        if para:
            paragraphs.append(para)
    return claims, paragraphs


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
    claims, paragraphs = _segment_section(sec, ref_map, max_claims)
    out["available"] = True
    out["paragraphs"] = paragraphs

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
        sug = c.get("suggestion")
        if sug:
            sd = f" ([{sug['doi']}](https://doi.org/{sug['doi']}))" if sug.get("doi") else ""
            lines.append(f"  → **suggested source [{sug.get('verdict')}]:** "
                         f"{(sug.get('title') or sug.get('doi') or '?')[:80]}{sd}")
    return "\n".join(lines)


_VCOLOR = {"SUPPORTED": "#1b7f3b", "OVERSTATED": "#b8860b",
           "UNSUPPORTED": "#b32020", "INSUFFICIENT": "#777", "SKIPPED": "#777"}


def render_html(out):
    """Standalone HTML: the original section prose with each cited claim
    highlighted by its verdict, plus a numbered detail list underneath."""
    import html as _h
    p = out.get("paper", {})
    title = _h.escape(p.get("title") or p.get("doi") or p.get("pmcid") or "paper")
    doi = p.get("doi")
    if not out.get("available"):
        body = f"<p class='note'>{_h.escape(out.get('note', 'Could not audit this paper.'))}</p>"
        prose = detail = ""
    else:
        claims = out.get("claims", [])
        # prose with highlighted claim spans
        paras = []
        for para in out.get("paragraphs", []):
            spans = []
            for seg in para:
                t = _h.escape(seg["text"])
                ci = seg.get("claim_idx")
                if ci is not None and ci < len(claims):
                    v = claims[ci].get("verdict", "SKIPPED")
                    va = claims[ci].get("verified_against") or {}
                    tip = _h.escape(f"{v} — cites {va.get('title') or va.get('doi') or '?'}")
                    spans.append(f'<span class="claim v-{v}" title="{tip}">{t}'
                                 f'<sup class="n">{ci + 1}</sup></span>')
                else:
                    spans.append(t)
            paras.append("<p>" + " ".join(spans) + "</p>")
        prose = "\n".join(paras)
        # numbered detail cards
        rows = []
        for i, c in enumerate(claims, 1):
            v = c.get("verdict", "SKIPPED")
            va = c.get("verified_against") or {}
            srctitle = _h.escape(va.get("title") or va.get("doi") or c.get("note") or "unresolved")
            srcdoi = va.get("doi")
            srcline = (f'<a href="https://doi.org/{_h.escape(srcdoi)}" target="_blank">{srctitle}</a>'
                       if srcdoi else srctitle)
            why = _h.escape(c.get("rationale") or c.get("note") or "")
            sug = c.get("suggestion")
            sugline = ""
            if sug:
                st = _h.escape(sug.get("title") or sug.get("doi") or "?")
                sd = (f' (<a href="https://doi.org/{_h.escape(sug["doi"])}" target="_blank">{_h.escape(sug["doi"])}</a>)'
                      if sug.get("doi") else "")
                sugline = (f'<div class="src">&rarr; suggested source '
                           f'[{_h.escape(sug.get("verdict", ""))}]: {st}{sd}</div>')
            rows.append(
                f'<div class="row" id="c{i}"><span class="badge v-{v}">{v}</span>'
                f'<div class="rtext"><div class="cl">{i}. {_h.escape(c["claim"])}</div>'
                f'<div class="src">cites: {srcline}</div>'
                f'{f"<div class=why>{why}</div>" if why else ""}{sugline}</div></div>')
        detail = "\n".join(rows)
        summ = " · ".join(f'<b class="v-{k}">{k}</b>: {v}' for k, v in out.get("summary", {}).items())
        body = f'<p class="meta">Section: <b>{_h.escape(out.get("section",""))}</b> · {summ}</p>'
    doiline = f'<p class="meta"><a href="https://doi.org/{_h.escape(doi)}" target="_blank">{_h.escape(doi)}</a> · open-access full text via Europe PMC</p>' if doi else ""
    css = """
    :root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e0;--card:#faf9f7;}
    @media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#ececec;--muted:#9a9a9a;--line:#333;--card:#262628;}}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
    .wrap{max-width:820px;margin:0 auto;padding:24px 16px 80px}
    h1{font-size:1.35rem;margin:.2em 0}.meta{color:var(--muted);font-size:.85rem;margin:.3em 0}
    .prose{margin:1.4em 0}.prose p{margin:.8em 0}
    .claim{border-radius:3px;padding:1px 2px;border-bottom:2px solid}
    .n{font-size:.62rem;font-weight:700;vertical-align:super;opacity:.8;margin-left:1px}
    .row{display:flex;gap:10px;align-items:flex-start;border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;background:var(--card)}
    .badge{font-size:.68rem;font-weight:700;color:#fff;padding:2px 7px;border-radius:20px;white-space:nowrap}
    .cl{font-weight:600}.src{color:var(--muted);font-size:.82rem;margin:.2em 0}.why{font-size:.85rem;margin-top:.3em}
    a{color:inherit}
    """
    for v, col in _VCOLOR.items():
        css += f".v-{v}{{color:{col}}}.claim.v-{v}{{border-bottom-color:{col};background:{col}1a}}.badge.v-{v}{{background:{col}}}\n"
    legend = " ".join(f'<span class="claim v-{v}" style="border:none">{v.title()}</span>'
                      for v in ("SUPPORTED", "OVERSTATED", "UNSUPPORTED", "INSUFFICIENT"))
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Citation audit — {title}</title><style>{css}</style></head><body><div class='wrap'>"
            f"<h1>Citation audit — {title}</h1>{doiline}{body}"
            f"<p class='meta'>Highlight = a claim that cites a reference, coloured by whether that reference supports it: {legend}</p>"
            f"<div class='prose'>{prose}</div><hr>"
            f"<h2 style='font-size:1rem'>Claim-by-claim</h2>{detail}</div></body></html>")


def _main():
    import asyncio
    import sys
    from . import server
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    html_path = next((a.split("=", 1)[1] for a in sys.argv[1:]
                      if a.startswith("--html=")), None)
    ref = args[0] if args else ""
    n = int(args[1]) if len(args) > 1 else 8
    if not ref:
        print("usage: python -m worth_your_read.audit <doi|pmid|pmcid> [max_claims] [--html=out.html]")
        return

    async def vf(claim, reference):
        return await server._verify(claim, reference)

    out = asyncio.run(run_audit(ref, vf, max_claims=n))
    print(_render(out))
    if html_path:
        with open(html_path, "w") as fh:
            fh.write(render_html(out))
        print(f"\nwrote {html_path}")


if __name__ == "__main__":
    _main()
