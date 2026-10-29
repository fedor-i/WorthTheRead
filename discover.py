"""Multi-provider source discovery — the Python port of docs/app.js.

Fan out to OpenAlex + Crossref + Europe PMC in parallel (all keyless), dedupe
candidates by DOI, merge their metadata, and rank by a pure function of
explicit trust signals (provider agreement, query-match rank, citations,
recency, open access, evidence tier) — so the same query always yields the
same order (auditable). Used by the self-improvement loop to find corroborating
papers for a claim, and exposed as a standalone MCP tool.

Every network call is best-effort: a provider that errors or rate-limits
contributes nothing rather than failing the whole search.
"""
import re
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

_REVIEW_RE = re.compile(r"review|meta-analysis|meta analysis", re.I)
_PREPRINT_RE = re.compile(r"preprint|biorxiv|medrxiv|arxiv|posted-content", re.I)
_BANG_RE = re.compile(r"(?:^|\s)(![a-z]+)\b", re.I)

# DuckDuckGo-style !bang: scope a query to one provider.
BANGS = {
    "!oa": "openalex", "!openalex": "openalex",
    "!cr": "crossref", "!crossref": "crossref",
    "!epmc": "epmc", "!europepmc": "epmc", "!pm": "epmc", "!pubmed": "epmc",
}


def clean_doi(d):
    if not d:
        return None
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return re.sub(r'[).,;>"\']+$', "", d).lower()


def _headers():
    return {"User-Agent": config.USER_AGENT, "Accept": "application/json"}


def _get_json(url, params):
    # Polite-pool contact only when the user explicitly configured one.
    if config.CONTACT_EMAIL and "mailto" not in params and url != EPMC:
        params = {**params, "mailto": config.CONTACT_EMAIL}
    r = requests.get(url, params=params, headers=_headers(),
                     timeout=config.HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _openalex_search(query, n):
    j = _get_json(OPENALEX, {"search": query, "per_page": n})
    out = []
    for i, w in enumerate(j.get("results") or []):
        ids = w.get("ids") or {}
        pmid = ids.get("pmid").split("/")[-1] if ids.get("pmid") else None
        loc = (w.get("primary_location") or {}).get("source") or {}
        oa = w.get("open_access") or {}
        out.append({
            "doi": clean_doi(w.get("doi")), "pmcid": None, "pmid": pmid,
            "title": w.get("display_name") or w.get("title"),
            "year": w.get("publication_year"),
            "venue": loc.get("display_name"),
            "citations": w.get("cited_by_count") or 0,
            "type": w.get("type") or "", "is_oa": bool(oa.get("is_oa")),
            "provider": "openalex", "rank": i,
        })
    return out


def _crossref_search(query, n):
    j = _get_json(CROSSREF, {
        "query": query, "rows": n,
        "select": "DOI,title,container-title,issued,is-referenced-by-count,type",
    })
    out = []
    for i, m in enumerate((j.get("message") or {}).get("items") or []):
        parts = (m.get("issued") or {}).get("date-parts") or [[]]
        year = parts[0][0] if parts and parts[0] else None
        out.append({
            "doi": clean_doi(m.get("DOI")), "pmcid": None, "pmid": None,
            "title": (m.get("title") or [None])[0], "year": year,
            "venue": (m.get("container-title") or [None])[0],
            "citations": m.get("is-referenced-by-count") or 0,
            "type": m.get("type") or "", "is_oa": False,
            "provider": "crossref", "rank": i,
        })
    return out


def _epmc_search(query, n):
    j = _get_json(EPMC, {"query": query, "format": "json",
                         "resultType": "lite", "pageSize": n})
    out = []
    results = (j.get("resultList") or {}).get("result") or []
    for i, r in enumerate(results):
        out.append({
            "doi": clean_doi(r.get("doi")), "pmcid": r.get("pmcid"),
            "pmid": r.get("pmid"), "title": r.get("title"),
            "year": int(r["pubYear"]) if r.get("pubYear") else None,
            "venue": r.get("journalTitle"),
            "citations": r.get("citedByCount") or 0,
            "type": r.get("pubType") or "", "is_oa": r.get("isOpenAccess") == "Y",
            "provider": "epmc", "rank": i,
        })
    return out


PROVIDERS = {"openalex": _openalex_search, "crossref": _crossref_search,
             "epmc": _epmc_search}


def _merge_candidates(lists):
    by_key = {}
    for lst in lists:
        for c in lst:
            key = c["doi"] or (c["pmcid"].lower() if c.get("pmcid") else None)
            if not key:
                continue
            m = by_key.get(key)
            if m is None:
                by_key[key] = {**c, "providers": [c["provider"]],
                               "bestRank": c["rank"]}
                continue
            if c["provider"] not in m["providers"]:
                m["providers"].append(c["provider"])
            m["bestRank"] = min(m["bestRank"], c["rank"])
            m["title"] = m["title"] or c["title"]
            m["year"] = m["year"] or c["year"]
            m["venue"] = m["venue"] or c["venue"]
            m["pmcid"] = m["pmcid"] or c["pmcid"]
            m["pmid"] = m["pmid"] or c["pmid"]
            m["citations"] = max(m["citations"] or 0, c["citations"] or 0)
            m["type"] = m["type"] or c["type"]
            m["is_oa"] = m["is_oa"] or c["is_oa"]
    return list(by_key.values())


def _trust_score(c):
    import math
    s = 0.0
    s += len(c.get("providers") or [1]) * 3          # agreement across providers
    rank = c["bestRank"] if c.get("bestRank") is not None else 9
    s += max(0, 6 - rank)                             # query-match (top of a list)
    s += math.log10((c.get("citations") or 0) + 1) * 2   # impact
    if c.get("year"):
        s += max(0, c["year"] - 2000) * 0.05          # mild recency tilt
    if c.get("is_oa"):
        s += 2                                        # full text is verifiable
    if _REVIEW_RE.search(c.get("type") or "") or _REVIEW_RE.search(c.get("title") or ""):
        s += 3                                        # evidence tier
    if _PREPRINT_RE.search(c.get("type") or ""):
        s -= 2                                        # not yet peer-reviewed
    return s


def discover_sources(raw_query, n=3, scope=None, peer_reviewed_only=False):
    """Discover up to `n` candidate sources for a query, trust-ranked.

    Honors a leading !bang (!oa / !crossref / !epmc / !pubmed) to scope the
    search to a single provider. Returns a list of candidate dicts, each with
    doi/pmcid/pmid/title/year/venue/citations/providers and a `_score`.
    """
    query = (raw_query or "").strip()
    m = _BANG_RE.search(query)
    if m and BANGS.get(m.group(1).lower()):
        scope = BANGS[m.group(1).lower()]
        query = re.sub(r"\s+", " ", query.replace(m.group(1), "")).strip()
    if not query:
        return []

    names = [scope] if scope else ["openalex", "crossref", "epmc"]
    per = max(n + 3, 8)

    def _safe(name):
        try:
            return PROVIDERS[name](query, per)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        lists = list(ex.map(_safe, names))

    cands = [c for c in _merge_candidates(lists) if c.get("doi") or c.get("pmcid")]
    if peer_reviewed_only:
        cands = [c for c in cands if not _PREPRINT_RE.search(c.get("type") or "")]
    for c in cands:
        c["_score"] = _trust_score(c)
    cands.sort(key=lambda c: (-c["_score"], str(c.get("doi") or c.get("pmcid"))))
    return cands[:n]
